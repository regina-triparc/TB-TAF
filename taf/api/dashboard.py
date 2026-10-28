"""
Local QA dashboard: the Jira queue joined to live pipeline phase, with controls
that start individual stages and switch the LLM provider.

Deliberately unauthenticated: the service binds to 127.0.0.1, and putting
QA_SERVICE_API_KEY into a URL or cookie to protect a page you open in a browser
would be worse than not protecting it. The /qa/* API keeps its key.
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field

from taf.integrations.jira.board import get_board
from taf.llm.router import ROUTER
from taf.runs import stages
from taf.runs.store import get_store

LOG = logging.getLogger("qa.dashboard")
router = APIRouter(prefix="/dashboard", tags=["dashboard"])

_STATIC = Path(__file__).parent / "static"


class StartRequest(BaseModel):
    ticket_id: str = Field(..., min_length=1, max_length=64)
    stage: str = Field("triage")
    # Optional overrides, used when Jira is not configured.
    summary: str = ""
    description: str = ""
    target_url: str = ""
    # Per-run router override: claude | ollama | auto | offline. Empty = current default.
    provider: str = ""
    # jira | seed | auto. Empty = TICKET_SOURCE.
    source: str = ""


class ProviderRequest(BaseModel):
    provider: str


class DecisionRequest(BaseModel):
    approved: bool
    by: str = Field("dashboard", max_length=100)
    note: str = Field("", max_length=500)


# --------------------------------------------------------------------- API --
@router.get("/api/board")
def board() -> JSONResponse:
    """Everything the page renders: the Jira queue, joined to the latest run."""
    store = get_store()
    jira = get_board().tickets()
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

    rows = [{"jira": ticket, "run": light(latest.get(ticket["key"]))} for ticket in jira["tickets"]]

    # Runs for tickets that are not (or no longer) in the Jira queue still matter.
    seen = {t["key"] for t in jira["tickets"]}
    for ticket_id, run in latest.items():
        if ticket_id not in seen:
            rows.append({"jira": {"key": ticket_id, "summary": run.get("summary", ""),
                                  "status": "(not in queue)", "type": "", "priority": "",
                                  "assignee": "", "updated": "", "labels": [], "url": ""},
                         "run": light(run)})

    routes = ROUTER.routes()
    return JSONResponse({
        "rows": rows,
        "jira_configured": jira["configured"],
        "jira_reason": jira["reason"],
        "jira_fetched_at": jira["fetched_at"],
        "ticket_source": jira["source"],
        "stages": list(stages.STAGE_NODES),
        # Per-agent, so a hybrid run shows which agent runs where.
        "provider": "+".join(sorted({r.provider for r in routes.values()})),
        "provider_default": ROUTER.default_provider,
        "providers": ROUTER.describe()["available"],
        "model": " / ".join(f"{k}:{r.model}" for k, r in routes.items()),
        "routes": {k: {"provider": r.provider, "model": r.model} for k, r in routes.items()},
        "server_time": time.time(),
    })


@router.post("/api/start")
def start(req: StartRequest) -> Dict[str, Any]:
    try:
        if req.provider:
            ROUTER.resolve(req.provider)  # validate before anything is recorded
        state = stages.prepare(req.ticket_id, req.stage, summary=req.summary, description=req.description,
                               target_url=req.target_url, provider=req.provider, source=req.source)
    except stages.StageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    stages.start_in_background(state, req.stage)
    return {"run_id": state["run_id"], "ticket_id": req.ticket_id, "stage": req.stage, "state": "running",
            "provider": ROUTER.resolve(req.provider or None)}


@router.post("/api/cancel/{run_id}")
def cancel(run_id: str) -> Dict[str, Any]:
    """
    Stop before the next node. An LLM call already in flight is not interrupted -
    it has no cancellation channel - so a cancel during a slow generation takes
    effect when that call returns or times out.
    """
    if not stages.cancel(run_id):
        raise HTTPException(status_code=404, detail="No active run with that id")
    return {"run_id": run_id, "cancelling": True,
            "note": "stops before the next stage; an in-flight model call finishes first"}


@router.post("/api/decide/{run_id}")
def decide(run_id: str, req: DecisionRequest) -> Dict[str, Any]:
    """Approve or reject a run waiting at the approval gate; it continues in the background."""
    try:
        stages.resume_in_background(run_id, {"approved": req.approved, "by": req.by, "note": req.note})
    except stages.StageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"run_id": run_id, "approved": req.approved, "state": "running"}


@router.post("/api/resume/{run_id}")
def resume(run_id: str) -> Dict[str, Any]:
    """Continue a crashed or cancelled run from its last completed step."""
    try:
        stages.resume_in_background(run_id, None)
    except stages.StageError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from exc
    return {"run_id": run_id, "state": "running"}


@router.get("/api/provider")
def get_provider() -> Dict[str, Any]:
    return ROUTER.describe()


@router.post("/api/provider")
def set_provider(req: ProviderRequest) -> Dict[str, Any]:
    """Switch the default provider for runs started from now on (not persisted across restarts)."""
    try:
        ROUTER.set_default(req.provider)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return ROUTER.describe()


@router.get("/api/run/{run_id}")
def run_detail(run_id: str) -> Dict[str, Any]:
    """Full record for one run, artifacts included - loaded when a row is expanded."""
    for run in get_store().recent_runs(limit=300):
        if run["run_id"] == run_id:
            return run
    raise HTTPException(status_code=404, detail="No such run")


@router.get("/report/{run_id}")
def report_html(run_id: str) -> FileResponse:
    """The run's HTML report (manual checklist pre-filled from the automation run)."""
    for run in get_store().recent_runs(limit=300):
        if run["run_id"] != run_id:
            continue
        for phase in run.get("phases") or []:
            path = (phase.get("artifact") or {}).get("report_html_path") if phase.get("phase") == "report" else ""
            if path and Path(path).is_file():
                return FileResponse(path, media_type="text/html")
    raise HTTPException(status_code=404, detail="No HTML report for this run")


@router.get("/api/runs")
def runs(limit: int = 50) -> Dict[str, Any]:
    return {"runs": get_store().recent_runs(limit=limit)}


# -------------------------------------------------------------------- page --
@router.get("")
@router.get("/")
def page() -> FileResponse:
    html = _STATIC / "dashboard.html"
    if not html.is_file():
        raise HTTPException(status_code=500, detail=f"dashboard.html missing at {html}")
    return FileResponse(html, media_type="text/html")
