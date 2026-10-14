"""
Stage runner shared by the dashboard and the CLI: run some prefix of the pipeline
for one ticket, recording every phase in the run store so the board shows it.

Stages are cumulative, because each needs what the one before it produced:
    triage      -> triage
    architect   -> triage, architect
    automation  -> triage, architect, automation
    full        -> triage, architect, automation, report

The n8n closeout is not a stage: runs started here have no callback URL. The full
n8n flow goes through POST /qa/runs (taf/runs/registry.py).
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from taf.agents.graph import NODES
from taf.config import SETTINGS
from taf.integrations.jira.board import get_board
from taf.llm.router import ROUTER
from taf.runs.store import get_store

STAGE_NODES: Dict[str, List[str]] = {
    "triage": ["triage"],
    "architect": ["triage", "architect"],
    "automation": ["triage", "architect", "automation"],
    "full": ["triage", "architect", "automation", "report"],
}

# run_id -> Event. Set means "stop before the next node".
_CANCELS: Dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()


class StageError(ValueError):
    """The request cannot start (unknown stage, unknown ticket, ticket already running)."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def build_state(run_id: str, ticket_id: str, summary: str = "", description: str = "",
                target_url: str = "", provider: str = "") -> Dict[str, Any]:
    """Initial pipeline state, filled from Jira (or the seed file) where not given explicitly."""
    issue = get_board().issue(ticket_id)
    if issue:
        summary = summary or issue.get("summary", "")
        description = description or issue.get("description", "")
    if not summary:
        raise StageError(f"No summary for {ticket_id}. It is not in Jira or the seed file "
                         f"({SETTINGS.seed_tickets_path}); configure Jira or pass a summary explicitly.")
    state: Dict[str, Any] = {
        "run_id": run_id,
        "ticket_id": ticket_id,
        "summary": summary,
        "description": description,
        "target_url": target_url or SETTINGS.dashboard_target_url,
        "github_diff": "",
        "environment_logs": "",
        "errors": [],
    }
    # Frozen for the whole run: switching the default mid-run affects the next run only.
    state["llm_provider"] = provider or ROUTER.default_provider
    return state


def prepare(ticket_id: str, stage: str, **overrides: str) -> Dict[str, Any]:
    """Validate, create the run record and return the initial state. Raises StageError."""
    if stage not in STAGE_NODES:
        raise StageError(f"Unknown stage {stage!r}; use one of {', '.join(STAGE_NODES)}")
    store = get_store()
    if store.active_run_for(ticket_id):
        raise StageError(f"{ticket_id} already has a run in progress", status_code=409)
    run_id = uuid.uuid4().hex
    state = build_state(run_id, ticket_id, **overrides)
    store.start_run(run_id, ticket_id, state["summary"], stage)
    with _CANCEL_LOCK:
        _CANCELS[run_id] = threading.Event()
    return state


def execute(state: Dict[str, Any], stage: str,
            on_phase: Optional[Callable[[str, str, str], None]] = None) -> Dict[str, Any]:
    """
    Run the stage's nodes in order on the calling thread, recording each phase.
    `on_phase(name, state, detail)` is called on every transition (the CLI prints it).
    Returns the final pipeline state with `_run_state` set to the run's end state.
    """
    run_id, ticket_id = state["run_id"], state["ticket_id"]
    store = get_store()
    cancel = _CANCELS.get(run_id) or threading.Event()
    notify = on_phase or (lambda *_: None)
    failed_at = ""

    try:
        for name in STAGE_NODES[stage]:
            if cancel.is_set():
                store.finish_run(run_id, "cancelled", error=f"cancelled before {name}")
                state["_run_state"] = "cancelled"
                return state
            row = store.phase_started(run_id, ticket_id, name)
            notify(name, "running", "")
            started = time.monotonic()
            try:
                update = NODES[name](state) or {}
            except Exception as exc:  # a node should catch its own, but never trust that
                store.phase_finished(row, "failed", f"{type(exc).__name__}: {exc}")
                store.finish_run(run_id, "failed", error=f"{name}: {exc}")
                notify(name, "failed", str(exc))
                state["_run_state"] = "failed"
                return state
            elapsed = int(time.monotonic() - started)

            if update.get("errors"):
                detail = "; ".join(str(e) for e in update["errors"])[:500]
                state.update(update)
                store.phase_finished(row, "failed", detail, phase_artifact(name, state))
                notify(name, "failed", detail)
                failed_at = name
                break

            state.update(update)
            detail = phase_detail(name, state, elapsed)
            store.phase_finished(row, "ok", detail, phase_artifact(name, state))
            notify(name, "ok", detail)

        if failed_at:
            store.finish_run(run_id, "failed", error=f"stopped at {failed_at}")
            state["_run_state"] = "failed"
            return state

        outcome = "passed" if state.get("execution_passed") else "completed"
        if stage in ("triage", "architect"):
            outcome = "completed"
        store.finish_run(run_id, "completed", outcome=outcome)
        state["_run_state"] = "completed"
        return state
    finally:
        with _CANCEL_LOCK:
            _CANCELS.pop(run_id, None)


def start_in_background(state: Dict[str, Any], stage: str) -> None:
    threading.Thread(target=execute, args=(state, stage),
                     name=f"qa-{state['ticket_id']}", daemon=True).start()


def cancel(run_id: str) -> bool:
    """Ask a running stage to stop before its next node. False if no such active run."""
    with _CANCEL_LOCK:
        event = _CANCELS.get(run_id)
    if event is None:
        return False
    event.set()
    return True


def phase_artifact(name: str, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    What this agent actually produced, for the board's expandable summary. Only the
    fields a reader needs - the full state carries prompts and raw logs that are too
    large to re-serialise on every poll.
    """
    if name == "triage":
        t = state.get("triage_analysis") or {}
        return {
            "risk_level": t.get("risk_level"),
            "feature_summary": t.get("feature_summary"),
            "acceptance_criteria": t.get("acceptance_criteria") or [],
            "risks": t.get("risks") or [],
            "impacted_areas": t.get("impacted_areas") or [],
            "gaps_and_questions": t.get("gaps_and_questions") or [],
        }
    if name == "architect":
        cases = state.get("test_cases") or []
        return {
            "total": len(cases),
            "automatable": sum(1 for c in cases if c.get("automatable")),
            "cases": [
                {
                    "test_id": c.get("test_id"),
                    "summary": c.get("summary"),
                    "priority": c.get("priority"),
                    "kind": c.get("kind"),
                    "category": c.get("category"),
                    "automatable": bool(c.get("automatable")),
                    "steps": [
                        {"action": s_.get("action"), "data": s_.get("data"),
                         "expected_result": s_.get("expected_result")}
                        for s_ in (c.get("steps") or [])
                    ],
                }
                for c in cases
            ],
        }
    if name == "automation":
        logs = state.get("execution_logs") or ""
        return {
            "git_branch": state.get("git_branch"),
            "git_pushed": bool(state.get("git_pushed")),
            "spec_file_path": state.get("spec_file_path"),
            "execution_passed": bool(state.get("execution_passed")),
            "code": (state.get("generated_typescript_code") or "")[:20000],
            # The tail is where Playwright prints its pass/fail summary.
            "log_tail": logs[-6000:],
        }
    if name == "report":
        return {"report_markdown": (state.get("final_report") or "")[:20000]}
    return None


def phase_detail(name: str, state: Dict[str, Any], elapsed_s: int) -> str:
    """One short human-readable line per finished phase, shown on the board."""
    if name == "triage":
        t = state.get("triage_analysis") or {}
        return (f"{elapsed_s}s - risk {t.get('risk_level', '?')}, "
                f"{len(t.get('acceptance_criteria') or [])} criteria")
    if name == "architect":
        cases = state.get("test_cases") or []
        auto = sum(1 for c in cases if c.get("automatable"))
        return f"{elapsed_s}s - {len(cases)} cases, {auto} automatable"
    if name == "automation":
        branch = state.get("git_branch") or "-"
        return (f"{elapsed_s}s - branch {branch}, "
                f"{'tests passed' if state.get('execution_passed') else 'tests did not pass'}")
    return f"{elapsed_s}s"
