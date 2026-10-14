"""
Local QA dashboard: the Jira queue joined to live pipeline phase, with controls
that start individual stages.

Mounted by main.py on the existing FastAPI app, so it shares the service's
process, settings and LLM clients. It is deliberately unauthenticated: the
service binds to 127.0.0.1, and putting QA_SERVICE_API_KEY into a URL or cookie
to protect a page you open in a browser would be worse than not protecting it.
The /qa/* API keeps its key.

Stages are cumulative, because each needs what the one before it produced:
    triage      -> triage
    architect   -> triage, architect
    automation  -> triage, architect, automation
    full        -> every node, including the report and the n8n callback
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

import jira_board
import run_store

LOG = logging.getLogger("qa.dashboard")
router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_STATIC = Path(__file__).parent / "static"

# Which nodes each stage runs, in order.
STAGE_NODES: Dict[str, List[str]] = {
    "triage": ["triage"],
    "architect": ["triage", "architect"],
    "automation": ["triage", "architect", "automation"],
    "full": ["triage", "architect", "automation", "report"],
}

# run_id -> Event. Set means "stop before the next node".
_CANCELS: Dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()


class StartRequest(BaseModel):
    ticket_id: str = Field(..., min_length=1, max_length=64)
    stage: str = Field("triage")
    # Optional overrides, used when Jira is not configured.
    summary: str = ""
    description: str = ""
    target_url: str = ""


def _node_callables() -> Dict[str, Any]:
    """Imported lazily: main.py imports this module, so a top-level import loops."""
    import main
    return {
        "triage": main.intake_triager,
        "architect": main.test_architect,
        "automation": main.automation_engineer,
        "report": main.report_synthesizer,
    }


def _build_state(run_id: str, req: StartRequest) -> Dict[str, Any]:
    import main

    summary, description = req.summary, req.description
    issue = jira_board.get_board().issue(req.ticket_id)
    if issue:
        summary = summary or issue.get("summary", "")
        description = description or issue.get("description", "")
    if not summary:
        raise HTTPException(
            status_code=422,
            detail=(f"No summary for {req.ticket_id}. Configure Jira "
                    f"(JIRA_BASE_URL / JIRA_EMAIL / JIRA_API_TOKEN) or pass summary explicitly."),
        )
    return {
        "run_id": run_id,
        "ticket_id": req.ticket_id,
        "summary": summary,
        "description": description,
        "target_url": req.target_url or os.getenv("DASHBOARD_TARGET_URL", "").strip(),
        "github_diff": "",
        "environment_logs": "",
        "errors": [],
    }


def _execute(run_id: str, req: StartRequest, state: Dict[str, Any]) -> None:
    """Run the stage's nodes in order on a worker thread, recording each phase."""
    store = run_store.get_store()
    nodes = _node_callables()
    cancel = _CANCELS[run_id]
    failed_at = ""

    try:
        for name in STAGE_NODES[req.stage]:
            if cancel.is_set():
                store.finish_run(run_id, "cancelled", error=f"cancelled before {name}")
                return
            row = store.phase_started(run_id, req.ticket_id, name)
            started = time.monotonic()
            try:
                update = nodes[name](state) or {}
            except Exception as exc:  # a node should catch its own, but never trust that
                store.phase_finished(row, "failed", f"{type(exc).__name__}: {exc}")
                store.finish_run(run_id, "failed", error=f"{name}: {exc}")
                return
            elapsed = int(time.monotonic() - started)

            if update.get("errors"):
                detail = "; ".join(str(e) for e in update["errors"])[:500]
                store.phase_finished(row, "failed", detail, _phase_artifact(name, state))
                failed_at = name
                state.update(update)
                break

            state.update(update)
            store.phase_finished(row, "ok", _phase_detail(name, state, elapsed),
                                 _phase_artifact(name, state))

        if failed_at:
            store.finish_run(run_id, "failed", error=f"stopped at {failed_at}")
            return

        outcome = "passed" if state.get("execution_passed") else "completed"
        if req.stage in ("triage", "architect"):
            outcome = "completed"
        store.finish_run(run_id, "completed", outcome=outcome)
    finally:
        with _CANCEL_LOCK:
            _CANCELS.pop(run_id, None)


def _phase_artifact(name: str, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    What this agent actually produced, for the board's expandable summary.

    Only the fields a reader needs to judge the output - the full state carries
    prompts and raw logs that are too large to re-serialise on every poll.
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


def _phase_detail(name: str, state: Dict[str, Any], elapsed_s: int) -> str:
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


# --------------------------------------------------------------------- API --
@router.get("/api/board")
def board() -> JSONResponse:
    """Everything the page renders: the Jira queue, joined to the latest run."""
    store = run_store.get_store()
    jira = jira_board.get_board().tickets()
    latest = store.latest_by_ticket()

    def light(run):
        """Strip artifacts: the board polls every few seconds, details load on expand."""
        if not run:
            return None
        slim = dict(run)
        slim["phases"] = [{k: v for k, v in p.items() if k != "artifact"}
                          for p in run.get("phases", [])]
        slim["has_artifacts"] = any(p.get("artifact") for p in run.get("phases", []))
        return slim

    rows = []
    for ticket in jira["tickets"]:
        rows.append({"jira": ticket, "run": light(latest.get(ticket["key"]))})

    # Runs for tickets that are not (or no longer) in the Jira queue still matter.
    seen = {t["key"] for t in jira["tickets"]}
    for ticket_id, run in latest.items():
        if ticket_id not in seen:
            rows.append({"jira": {"key": ticket_id, "summary": run.get("summary", ""),
                                  "status": "(not in queue)", "type": "", "priority": "",
                                  "assignee": "", "updated": "", "labels": [], "url": ""},
                         "run": light(run)})

    import main
    return JSONResponse({
        "rows": rows,
        "jira_configured": jira["configured"],
        "jira_reason": jira["reason"],
        "jira_fetched_at": jira["fetched_at"],
        "stages": list(STAGE_NODES),
        # Per-agent, so a hybrid run shows which agent runs where.
        "provider": "+".join(sorted({r.provider for r in main.AGENT_ROUTES.values()})),
        "model": " / ".join(f"{k}:{r.model}" for k, r in main.AGENT_ROUTES.items()),
        "routes": {k: {"provider": r.provider, "model": r.model}
                   for k, r in main.AGENT_ROUTES.items()},
        "server_time": time.time(),
    })


@router.post("/api/start")
def start(req: StartRequest) -> Dict[str, Any]:
    if req.stage not in STAGE_NODES:
        raise HTTPException(status_code=422, detail=f"Unknown stage {req.stage!r}")
    store = run_store.get_store()
    if store.active_run_for(req.ticket_id):
        raise HTTPException(status_code=409, detail=f"{req.ticket_id} already has a run in progress")

    run_id = uuid.uuid4().hex
    state = _build_state(run_id, req)
    store.start_run(run_id, req.ticket_id, state["summary"], req.stage)
    with _CANCEL_LOCK:
        _CANCELS[run_id] = threading.Event()

    threading.Thread(target=_execute, args=(run_id, req, state),
                     name=f"qa-{req.ticket_id}", daemon=True).start()
    return {"run_id": run_id, "ticket_id": req.ticket_id, "stage": req.stage, "state": "running"}


@router.post("/api/cancel/{run_id}")
def cancel(run_id: str) -> Dict[str, Any]:
    """
    Stop before the next node. An LLM call already in flight is not interrupted -
    it has no cancellation channel - so a cancel during a slow generation takes
    effect when that call returns or times out.
    """
    with _CANCEL_LOCK:
        event = _CANCELS.get(run_id)
    if event is None:
        raise HTTPException(status_code=404, detail="No active run with that id")
    event.set()
    return {"run_id": run_id, "cancelling": True,
            "note": "stops before the next stage; an in-flight model call finishes first"}


@router.get("/api/run/{run_id}")
def run_detail(run_id: str) -> Dict[str, Any]:
    """Full record for one run, artifacts included - loaded when a row is expanded."""
    for run in run_store.get_store().recent_runs(limit=300):
        if run["run_id"] == run_id:
            return run
    raise HTTPException(status_code=404, detail="No such run")


@router.get("/api/runs")
def runs(limit: int = 50) -> Dict[str, Any]:
    return {"runs": run_store.get_store().recent_runs(limit=limit)}


# -------------------------------------------------------------------- page --
@router.get("")
@router.get("/")
def page() -> FileResponse:
    html = _STATIC / "dashboard.html"
    if not html.is_file():
        raise HTTPException(status_code=500, detail=f"dashboard.html missing at {html}")
    return FileResponse(html, media_type="text/html")
