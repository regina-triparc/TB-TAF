"""
In-memory registry and background runner for the n8n flow (POST /qa/runs): one
full LangGraph run per ticket, with duplicate-run protection and status polling.
Resets on restart; the dashboard's history lives in the SQLite run store instead.
"""
from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional

from taf.agents.graph import qa_graph, thread_config
from taf.agents.state import QAState
from taf.config import redact
from taf.core.text import utc_now
from taf.integrations.paperclip.control_room import CONTROL_ROOM
from taf.llm.budget import BUDGET
from taf.observability.tracing import TRACER

log = logging.getLogger("qa")


class RunRegistry:
    """Tracks run status for GET /qa/runs/{id} and prevents duplicate runs per ticket."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: Dict[str, Dict[str, Any]] = {}

    def try_start(self, run_id: str, ticket_id: str) -> Optional[str]:
        """Register a run; returns the conflicting run_id if the ticket is already running."""
        with self._lock:
            for existing_id, run in self._runs.items():
                if run["ticket_id"] == ticket_id and run["status"] in {"queued", "running"}:
                    return existing_id
            self._runs[run_id] = {"run_id": run_id, "ticket_id": ticket_id, "status": "queued", "created_at": utc_now()}
            return None

    def update(self, run_id: str, **fields: Any) -> None:
        with self._lock:
            self._runs.setdefault(run_id, {}).update(fields)

    def get(self, run_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            run = self._runs.get(run_id)
            return dict(run) if run else None


RUNS = RunRegistry()


def execute_qa_run(initial_state: QAState) -> None:
    """Background task: run the full LangGraph pipeline for one ticket."""
    run_id, ticket_id = initial_state["run_id"], initial_state["ticket_id"]
    RUNS.update(run_id, status="running", started_at=utc_now())
    CONTROL_ROOM.emit(run_id, ticket_id, "run.started", summary=initial_state.get("summary", ""),
                      target_url=initial_state.get("target_url", ""))
    try:
        with TRACER.run(initial_state, extra_tags=["n8n"]):
            final: QAState = qa_graph().invoke(initial_state, config=thread_config(run_id))
        if final.get("__interrupt__"):
            # Paused at the approval gate; `python -m taf resume <run_id> --approve` (or the
            # dashboard) continues it, and the closeout then calls n8n back.
            RUNS.update(run_id, status="waiting_approval", git_branch="", errors=final.get("errors", []))
            CONTROL_ROOM.emit(run_id, ticket_id, "run.waiting", reason="approval required")
            return
        outcome = "QA Passed" if final.get("execution_passed") else "QA Failed"
        RUNS.update(
            run_id,
            status="completed",
            outcome=outcome,
            finished_at=utc_now(),
            git_branch=final.get("git_branch", ""),
            callback_delivered=final.get("callback_delivered", False),
            cloud_tokens_used=BUDGET.run_usage(run_id),
            cloud_cost_usd=round(BUDGET.run_cost_usd(run_id), 4),
            errors=final.get("errors", []),
            final_report=final.get("final_report", ""),
        )
        CONTROL_ROOM.emit(run_id, ticket_id, "run.completed", outcome=outcome,
                          cloud_tokens_used=BUDGET.run_usage(run_id),
                          callback_delivered=final.get("callback_delivered", False),
                          final_report=final.get("final_report", ""))
    except Exception as exc:  # noqa: BLE001 - last-resort guard; nodes already catch their own errors
        log.exception("[%s] Run %s crashed", ticket_id, run_id)
        RUNS.update(run_id, status="crashed", finished_at=utc_now(), errors=[redact(str(exc))])
        CONTROL_ROOM.emit(run_id, ticket_id, "run.crashed", error=redact(str(exc)))
    finally:
        BUDGET.release(run_id)
        TRACER.flush()
