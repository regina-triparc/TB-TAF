"""
FastAPI application.

    GET  /health            configuration sanity check (never reveals secrets)
    POST /qa/runs           n8n entry point: full pipeline in the background, 202 + run id
    GET  /qa/runs/{run_id}  status of an n8n run
    /dashboard/...          local board (taf/api/dashboard.py)

Run with `uvicorn main:app` (main.py re-exports this app) or `python -m taf serve`.
"""
from __future__ import annotations

import asyncio
import hmac
import re
import shutil
import uuid
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field, HttpUrl, field_validator

from taf import __version__
from taf.agents.state import QAState
from taf.api import dashboard
from taf.config import SETTINGS, log
from taf.core.text import utc_now
from taf.integrations.github.repository import TEST_REPO
from taf.integrations.jira.board import get_board
from taf.integrations.paperclip.control_room import CONTROL_ROOM
from taf.llm.router import ROUTER
from taf.observability.tracing import TRACER
from taf.runs.registry import RUNS, execute_qa_run
from taf.runs.store import get_store

_TICKET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class QARunRequest(BaseModel):
    """Payload n8n sends after catching the Jira 'Ready for QA' webhook."""

    ticket_id: str = Field(..., examples=["SHOP-1234"], description="Jira issue key.")
    summary: str = Field(..., min_length=1, max_length=1000)
    description: str = Field(default="", description="Jira description incl. acceptance criteria.")
    github_diff: str = Field(default="", description="Unified diff of the source PR.")
    target_url: HttpUrl = Field(..., description="Base URL of the environment under test.")
    n8n_callback_url: HttpUrl = Field(..., description="n8n webhook that receives the final verdict.")
    azure_logs: Optional[str] = Field(default=None, description="Recent Azure App Service / App Insights logs.")
    db_logs: Optional[str] = Field(default=None, description="Recent database logs / slow-query output.")
    provider: Optional[str] = Field(default=None, description="Router override: claude | ollama | auto | offline.")

    @field_validator("ticket_id")
    @classmethod
    def _safe_ticket_id(cls, value: str) -> str:
        # The ticket id becomes a git branch name and a file name: reject anything unsafe.
        value = value.strip()
        if not _TICKET_ID_RE.match(value) or ".." in value or value.endswith((".lock", ".")):
            raise ValueError("ticket_id must look like a Jira key (letters, digits, '-', '_', '.').")
        return value


class QARunAccepted(BaseModel):
    run_id: str
    ticket_id: str
    status: str
    status_url: str


def require_api_key(x_api_key: Optional[str] = Header(default=None)) -> None:
    """Shared-secret auth for n8n → service calls (enabled when QA_SERVICE_API_KEY is set)."""
    expected = SETTINGS.qa_service_api_key
    if expected and not (x_api_key and hmac.compare_digest(x_api_key, expected)):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid or missing X-API-Key.")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup: verify the Paperclip connection once and surface misconfiguration early."""
    if CONTROL_ROOM.enabled:
        status_ = await asyncio.to_thread(CONTROL_ROOM.health)
        if not status_.get("reachable"):
            log.warning("Paperclip not reachable at startup: %s", status_.get("error"))
        else:
            log.info("Paperclip connected as agent %r (company %s)", status_.get("agent"), status_.get("company_id"))
            for warning in status_.get("warnings", []):
                log.warning("Paperclip: %s", warning)
    else:
        log.info("Paperclip not configured (PAPERCLIP_API_URL / PAPERCLIP_API_KEY) - events go to the log only.")

    # Runs still marked 'running' belong to a process that no longer exists.
    orphaned = get_store().mark_stale_running_as_interrupted()
    if orphaned:
        log.warning("Marked %d run(s) interrupted by the previous shutdown.", orphaned)
    log.info("LLM provider: %s -> %s", ROUTER.default_provider, ROUTER.resolve())
    log.info("Dashboard at /dashboard on this server (http://%s:%s/dashboard unless --port overrides PORT)",
             SETTINGS.host, SETTINGS.port)
    log.info("Langfuse tracing %s", TRACER.status)
    yield
    TRACER.shutdown()  # bounded: an unreachable Langfuse cannot hold up the shutdown


app = FastAPI(
    lifespan=lifespan,
    title="Autonomous QA Multi-Agent Framework",
    version=__version__,
    description="Jira → LangGraph agents (Claude / Ollama via the LLM router) → Playwright → n8n / Xray.",
)
app.include_router(dashboard.router)


@app.get("/health")
def health() -> Dict[str, Any]:
    """Liveness + configuration sanity check (never reveals secrets)."""
    repo_path = TEST_REPO.path
    routing = ROUTER.describe()
    return {
        "status": "ok",
        "time": utc_now(),
        "version": __version__,
        "llm_provider": routing["default"],
        "llm_router": routing,
        # Kept under its old name for scripts written against v1.
        "claude_routes": routing["routes"],
        "prompt_cache_ttl": SETTINGS.prompt_cache_ttl,
        "spec_repair_attempts": SETTINGS.spec_repair_attempts,
        "claude_configured": bool(SETTINGS.anthropic_api_key),
        "ollama": {"base_url": SETTINGS.ollama_base_url, "model": SETTINGS.ollama_model},
        "test_repo": {
            "path": str(repo_path) if repo_path else None,
            "exists": bool(repo_path and repo_path.is_dir()),
            "is_git_clone": bool(repo_path and (repo_path / ".git").is_dir()),
            "push_enabled": SETTINGS.git_push_enabled,
            "github_token_configured": bool(SETTINGS.github_token),
        },
        "tools": {name: bool(shutil.which(name)) for name in ("git", "pnpm", "npm", "npx")},
        "test_repo_mode": {"no_git": SETTINGS.no_git,
                           "package_manager": SETTINGS.package_manager,
                           "skip_install": SETTINGS.skip_dependency_install},
        "ticket_source": {"setting": SETTINGS.ticket_source,
                          "resolved": get_board().resolve_source()},
        "source_repo": {"url": SETTINGS.source_repo_url or None,
                        "local_path": SETTINGS.source_repo_local_path,
                        "fetch_pr_diffs": SETTINGS.fetch_pr_diffs},
        "paperclip": CONTROL_ROOM.health(),
        "budget": {"per_run": SETTINGS.max_cloud_tokens_per_run, "per_day": SETTINGS.max_cloud_tokens_per_day},
    }


@app.post(
    "/qa/runs",
    response_model=QARunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
def start_qa_run(request: QARunRequest, background_tasks: BackgroundTasks) -> QARunAccepted:
    """
    Accept a ticket and run the pipeline asynchronously. Playwright runs can take many
    minutes, so n8n gets an immediate 202 and the verdict arrives via n8n_callback_url.
    """
    if request.provider:
        try:
            ROUTER.resolve(request.provider)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    run_id = uuid.uuid4().hex
    conflict = RUNS.try_start(run_id, request.ticket_id)
    if conflict:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A QA run for {request.ticket_id} is already in progress (run_id={conflict}).",
        )

    env_logs = "\n\n".join(
        f"### {label}\n{content}" for label, content in (("Azure logs", request.azure_logs), ("DB logs", request.db_logs))
        if content
    )
    initial_state: QAState = {
        "run_id": run_id,
        "ticket_id": request.ticket_id,
        "summary": request.summary,
        "description": request.description,
        "github_diff": request.github_diff,
        "target_url": str(request.target_url).rstrip("/"),
        "n8n_callback_url": str(request.n8n_callback_url),
        "environment_logs": env_logs,
        "test_plan": "",
        "generated_typescript_code": "",
        "execution_logs": "",
        "execution_passed": False,
        "xray_csv_content": "",
        "xray_tests_json": [],
        "final_report": "",
        "aborted": False,
        "errors": [],
    }
    # Frozen for the whole run: switching the default mid-run affects the next run only.
    initial_state["llm_provider"] = request.provider or ROUTER.default_provider
    # Sync function → FastAPI runs it in its threadpool after the response is sent.
    background_tasks.add_task(execute_qa_run, initial_state)
    CONTROL_ROOM.emit(run_id, request.ticket_id, "run.queued", summary=request.summary)
    return QARunAccepted(run_id=run_id, ticket_id=request.ticket_id, status="queued", status_url=f"/qa/runs/{run_id}")


@app.get("/qa/runs/{run_id}", dependencies=[Depends(require_api_key)])
def get_qa_run(run_id: str) -> Dict[str, Any]:
    run = RUNS.get(run_id)
    if not run:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Unknown run_id.")
    return run
