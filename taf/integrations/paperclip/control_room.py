"""
Paperclip Control Room: mirrors every QA run into Paperclip (issue per run, live
comments, cost ledger) and answers "may we spend?" before each Claude call.

Optional. With PAPERCLIP_API_URL / PAPERCLIP_API_KEY empty every event goes to the
service log only and nothing here touches the network.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests

from taf.config import SETTINGS, Settings, redact
from taf.core.http import retrying_session as _retrying_session
from taf.core.text import utc_now


def paperclip_timestamp() -> str:
    """ISO-8601 UTC with a 'Z' suffix - Paperclip's zod `.datetime()` rejects '+00:00' offsets."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")

# Triage risk level → Paperclip issue priority (Paperclip: critical | high | medium | low).
_RISK_TO_PAPERCLIP_PRIORITY = {"critical": "critical", "high": "high", "medium": "medium", "low": "low"}


class PaperclipError(RuntimeError):
    """A Paperclip API call failed (network error or HTTP >= 400)."""


class PaperclipClient:
    """
    Minimal synchronous client for Paperclip's REST API (`<base>/api/...`).

    Authenticates as the QA agent with an agent API key (`Authorization: Bearer`).
    An agent key may read its own record (`/agents/me`), create and update issues in
    its company, comment on them, and report its *own* cost events - exactly the
    surface this service needs.
    """

    def __init__(self, base_url: str, api_key: str, timeout_s: int = 10) -> None:
        self.base_url = base_url
        self.enabled = bool(base_url and api_key)
        self._headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        self._timeout_s = timeout_s
        self._local = threading.local()  # requests.Session is not thread-safe: one per thread

    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = self._local.session = _retrying_session(total_retries=2)
        return session

    def request(self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
                timeout_s: Optional[int] = None) -> Any:
        url = f"{self.base_url}/api{path}"
        try:
            response = self._session().request(
                method, url, json=body, headers=self._headers, timeout=timeout_s or self._timeout_s
            )
        except requests.RequestException as exc:
            raise PaperclipError(f"{method} {path}: {redact(str(exc))}") from exc
        if response.status_code >= 400:
            raise PaperclipError(f"{method} {path} -> HTTP {response.status_code}: {response.text[:300]}")
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return None


class ControlRoom:
    """
    Mirrors every QA run into the Paperclip Control Room:

        run.started       → issue created: "[TICKET] summary", in_progress, assigned to the QA agent
        triage.completed  → issue priority set from the triage risk level
        agent.* / log     → issue comments (the live execution feed on the board)
        llm.usage         → cost event (tokens + cost_cents) on the agent's ledger
        run.completed     → issue moved to done (QA Passed) or blocked (QA Failed) + final report
        run.crashed       → issue moved to blocked + error

    Delivery happens on ONE background worker, so events stay in order and a slow or
    offline Paperclip can never block or fail a QA run. The only synchronous call is
    `budget_block_reason()`, because the budget must be checked *before* money is spent.
    Every event is also written to the service log, with or without Paperclip.
    """

    _COMMENT_EVENTS = {"agent.completed", "agent.failed", "agent.skipped", "agent.log"}
    _AGENT_CACHE_TTL_S = 30.0

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client = PaperclipClient(settings.paperclip_api_url, settings.paperclip_api_key)
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="paperclip")
        self._log = logging.getLogger("qa.paperclip")
        self._lock = threading.Lock()
        self._identity: Optional[Dict[str, str]] = None      # {"agent_id", "company_id", "name"}
        self._project_id: Optional[str] = None
        self._issues: Dict[str, str] = {}                     # run_id → Paperclip issue id
        self._agent_cache: Tuple[float, Optional[Dict[str, Any]]] = (0.0, None)
        self._budget_check_skip_until = 0.0  # back-off after Paperclip was unreachable

    @property
    def enabled(self) -> bool:
        return self.client.enabled

    # ------------------------------------------------------------ identity --
    def _get_me(self, fresh: bool = False) -> Dict[str, Any]:
        """GET /agents/me (cached briefly): status, pauseReason, budget and runtimeConfig."""
        fetched_at, agent = self._agent_cache
        if not fresh and agent is not None and time.monotonic() - fetched_at < self._AGENT_CACHE_TTL_S:
            return agent
        agent = self.client.request("GET", "/agents/me", timeout_s=5) or {}
        self._agent_cache = (time.monotonic(), agent)
        return agent

    def _ensure_identity(self) -> Dict[str, str]:
        """Discover the agent id and company id from the API key (no IDs to configure)."""
        with self._lock:
            if self._identity:
                return self._identity
        me = self._get_me(fresh=True)
        identity = {
            "agent_id": str(me.get("id") or ""),
            "company_id": str(me.get("companyId") or ""),
            "name": str(me.get("name") or ""),
        }
        if not identity["agent_id"] or not identity["company_id"]:
            raise PaperclipError("/agents/me returned no id/companyId - is PAPERCLIP_API_KEY an *agent* API key?")
        project_id = self._resolve_project(identity["company_id"])
        with self._lock:
            self._identity, self._project_id = identity, project_id
        return identity

    def _resolve_project(self, company_id: str) -> Optional[str]:
        """PAPERCLIP_PROJECT may be a project UUID or its display name."""
        wanted = self.settings.paperclip_project.strip()
        if not wanted:
            return None
        if _UUID_RE.match(wanted):
            return wanted
        projects = self.client.request("GET", f"/companies/{company_id}/projects") or []
        if isinstance(projects, dict):  # tolerate paginated / wrapped responses
            projects = projects.get("items") or projects.get("projects") or projects.get("data") or []
        for project in projects:
            if isinstance(project, dict) and str(project.get("name", "")).strip().lower() == wanted.lower():
                return project.get("id")
        self._log.warning("Paperclip project %r not found - issues will be created without a project.", wanted)
        return None

    # -------------------------------------------------------------- events --
    def emit(self, run_id: str, ticket_id: str, event: str, **data: Any) -> None:
        loggable = {k: v for k, v in data.items() if k != "final_report"}
        self._log.info("[%s] %s %s", ticket_id, event, json.dumps(loggable, default=str)[:500])
        if self.enabled:
            self._pool.submit(self._deliver, run_id, ticket_id, event, data)

    def _deliver(self, run_id: str, ticket_id: str, event: str, data: Dict[str, Any]) -> None:
        try:
            self._handle(run_id, ticket_id, event, data)
        except PaperclipError as exc:
            self._log.warning("[%s] Paperclip %s not recorded: %s", ticket_id, event, exc)
        except Exception:  # noqa: BLE001 - the Control Room must never affect a QA run
            self._log.exception("[%s] Unexpected error delivering %s to Paperclip", ticket_id, event)

    def _handle(self, run_id: str, ticket_id: str, event: str, data: Dict[str, Any]) -> None:
        identity = self._ensure_identity()
        company_id = identity["company_id"]

        if event == "run.started":
            body: Dict[str, Any] = {
                "title": f"[{ticket_id}] {data.get('summary', '')}".strip()[:250],
                "description": (
                    f"Autonomous QA run for Jira **{ticket_id}**.\n\n"
                    f"- Target: {data.get('target_url', '')}\n"
                    f"- Run ID: `{run_id}`\n"
                    f"- Started: {utc_now()}"
                ),
                "status": "in_progress",          # Paperclip requires an assignee for in_progress
                "priority": "medium",             # refined after triage
                "assigneeAgentId": identity["agent_id"],
                "idempotencyKey": run_id,         # safe to retry: replays return the same issue
                "allowDuplicate": True,           # re-runs of a ticket reuse the same title
            }
            if self._project_id:
                body["projectId"] = self._project_id
            issue = self.client.request("POST", f"/companies/{company_id}/issues", body) or {}
            if issue.get("id"):
                with self._lock:
                    self._issues[run_id] = issue["id"]
            return

        with self._lock:
            issue_id = self._issues.get(run_id)

        if event == "llm.usage":
            self._post_cost_event(identity, issue_id, data)
            return
        if issue_id is None:  # issue creation failed or Paperclip was offline at run start
            return

        if event == "triage.completed":
            priority = _RISK_TO_PAPERCLIP_PRIORITY.get(str(data.get("risk_level", "")).lower())
            if priority:
                self.client.request("PATCH", f"/issues/{issue_id}", {"priority": priority})
        elif event in self._COMMENT_EVENTS:
            self.client.request("POST", f"/issues/{issue_id}/comments", {"body": self._comment_text(event, data)})
        elif event in {"run.completed", "run.crashed"}:
            passed = event == "run.completed" and data.get("outcome") == "QA Passed"
            comment = data.get("final_report") or f"**Run crashed:** {data.get('error', 'unknown error')}"
            self.client.request(
                "PATCH", f"/issues/{issue_id}",
                {"status": "done" if passed else "blocked", "comment": comment},
            )
            with self._lock:
                self._issues.pop(run_id, None)

    @staticmethod
    def _comment_text(event: str, data: Dict[str, Any]) -> str:
        agent = data.get("agent", "Agent")
        if event == "agent.completed":
            return (f"✅ **{agent}** finished in {data.get('duration_s', '?')}s "
                    f"({data.get('engine', '')}; cloud tokens so far: {data.get('cloud_tokens_run_total', 0):,})")
        if event == "agent.failed":
            return f"❌ **{agent}** failed: {data.get('error', 'unknown error')}"
        if event == "agent.skipped":
            return f"⏭️ **{agent}** skipped: {data.get('reason', '')}"
        return f"**{agent}:** {data.get('message', '')}".strip() or "(empty log line)"

    def _post_cost_event(self, identity: Dict[str, str], issue_id: Optional[str], data: Dict[str, Any]) -> None:
        body: Dict[str, Any] = {
            "agentId": identity["agent_id"],       # agents may only report their own costs
            "provider": data["provider"],
            "biller": data["provider"],
            "billingType": data.get("billing_type", "metered_api"),
            "model": data["model"],
            "inputTokens": int(data.get("input_tokens", 0)),
            "cachedInputTokens": int(data.get("cached_input_tokens", 0)),
            "outputTokens": int(data.get("output_tokens", 0)),
            "costCents": int(data.get("cost_cents", 0)),
            "occurredAt": data.get("occurred_at") or paperclip_timestamp(),
        }
        if issue_id:
            body["issueId"] = issue_id
        if self._project_id:
            body["projectId"] = self._project_id
        self.client.request("POST", f"/companies/{identity['company_id']}/cost-events", body)

    # -------------------------------------------------------------- budget --
    def budget_block_reason(self) -> Optional[str]:
        """
        Why Paperclip forbids further spending (agent paused, e.g. by its budget hard-stop,
        or monthly budget used up), or None. Fails OPEN when Paperclip is unreachable -
        the service's own per-run / per-day caps still apply in that case.
        """
        if not (self.enabled and self.settings.paperclip_enforce_budget):
            return None
        if time.monotonic() < self._budget_check_skip_until:
            return None
        try:
            me = self._get_me()
        except PaperclipError as exc:
            # Don't pay the connection-retry delay before every Claude call during an outage.
            self._budget_check_skip_until = time.monotonic() + self._AGENT_CACHE_TTL_S
            self._log.warning("Paperclip unreachable for budget check (%s) - local caps only for %.0fs.",
                              exc, self._AGENT_CACHE_TTL_S)
            return None
        status = str(me.get("status") or "")
        if status in {"paused", "terminated"}:
            return f"Paperclip agent is {status} (reason: {me.get('pauseReason') or 'not given'})."
        budget = int(me.get("budgetMonthlyCents") or 0)
        spent = int(me.get("spentMonthlyCents") or 0)
        if budget > 0 and spent >= budget:
            return f"Paperclip monthly budget used up (${spent / 100:,.2f} of ${budget / 100:,.2f})."
        return None

    # -------------------------------------------------------------- health --
    def health(self) -> Dict[str, Any]:
        if not self.enabled:
            return {"configured": False}
        try:
            me = self._get_me(fresh=True)
            identity = self._ensure_identity()
        except PaperclipError as exc:
            return {"configured": True, "reachable": False, "error": str(exc)}

        warnings: List[str] = []
        runtime_config = me.get("runtimeConfig")
        heartbeat: Dict[str, Any] = {}
        if isinstance(runtime_config, dict) and isinstance(runtime_config.get("heartbeat"), dict):
            heartbeat = runtime_config["heartbeat"]
        # Mirrors Paperclip's isHeartbeatWakeOnDemandEnabled(): first key set wins, default true.
        wake_value: Any = next(
            (heartbeat[k] for k in ("wakeOnDemand", "wakeOnAssignment", "wakeOnOnDemand", "wakeOnAutomation")
             if heartbeat.get(k) is not None),
            True,
        )
        wake_on_demand: Any = "unknown" if runtime_config is None else str(wake_value).lower() not in {"false", "0", "no"}
        heartbeat_enabled: Any = "unknown" if runtime_config is None else str(heartbeat.get("enabled", False)).lower() in {"true", "1", "yes"}
        if wake_on_demand is True:
            warnings.append("Turn OFF 'Wake on demand' for this agent - otherwise Paperclip tries to run it "
                            "every time a QA issue is assigned to it.")
        if heartbeat_enabled is True:
            warnings.append("Turn OFF the heartbeat timer for this agent - n8n starts runs, not Paperclip.")
        if self.settings.paperclip_project and not self._project_id:
            warnings.append(f"PAPERCLIP_PROJECT {self.settings.paperclip_project!r} was not found.")
        return {
            "configured": True,
            "reachable": True,
            "agent": identity["name"],
            "agent_status": me.get("status"),
            "company_id": identity["company_id"],
            "project_id": self._project_id,
            "wake_on_demand": wake_on_demand,
            "heartbeat_enabled": heartbeat_enabled,
            "budget_monthly_cents": me.get("budgetMonthlyCents"),
            "spent_monthly_cents": me.get("spentMonthlyCents"),
            "enforce_budget": self.settings.paperclip_enforce_budget,
            "warnings": warnings,
        }


CONTROL_ROOM = ControlRoom(SETTINGS)
