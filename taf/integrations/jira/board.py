"""
Jira queue for the dashboard: the tickets sitting in QA right now.

The QA service itself never needed Jira - n8n hands it a ticket id and it gets on
with the run. The board does need it, so this module is the service's own small,
read-only Jira client: one JQL search on a timer, cached, plus a by-key fetch for
the full description a pipeline run needs.

Credentials are optional. With none configured every call returns an empty board
and a `reason` the UI shows, rather than raising - a missing token must not take
the dashboard down.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

from taf.config import SETTINGS, TICKET_SOURCES, project_path
from taf.integrations.github.pull_requests import PullRequest, prs_from_seed, prs_from_text, repo_slug

LOG = logging.getLogger("qa.jira")

DEFAULT_JQL = 'status in ("Ready for QA", "IN QA") ORDER BY updated DESC'
_FIELDS = "summary,status,issuetype,priority,assignee,updated,labels"


class JiraConfig:
    def __init__(self) -> None:
        self.base_url = os.getenv("JIRA_BASE_URL", "").strip().rstrip("/")
        self.email = os.getenv("JIRA_EMAIL", "").strip()
        self.token = os.getenv("JIRA_API_TOKEN", "").strip()
        self.jql = os.getenv("JIRA_JQL", "").strip() or DEFAULT_JQL
        self.poll_seconds = int(os.getenv("JIRA_POLL_SECONDS", "60") or 60)
        self.timeout = int(os.getenv("JIRA_TIMEOUT_SECONDS", "20") or 20)

    @property
    def configured(self) -> bool:
        return bool(self.base_url and self.email and self.token)

    @property
    def missing(self) -> str:
        gaps = [n for n, v in (("JIRA_BASE_URL", self.base_url),
                               ("JIRA_EMAIL", self.email),
                               ("JIRA_API_TOKEN", self.token)) if not v]
        return ", ".join(gaps)


# data/seed_tickets.json by default (SEED_TICKETS_PATH). Ships with TB-601 so a fresh
# clone has one ticket to push through the pipeline with no Jira access at all.
_SEED_PATH = project_path(SETTINGS.seed_tickets_path)


def _seed_tickets() -> List[Dict[str, Any]]:
    """
    Offline fallback for the queue.

    Jira Cloud's JQL search needs an app the site may not have installed, and a
    token takes a minute to create - neither should leave the board blank. A
    hand-maintained seed file lets the dashboard and its controls work before any
    credential exists. Ignored entirely once Jira is configured.
    """
    if not _SEED_PATH.is_file():
        return []
    try:
        data = json.loads(_SEED_PATH.read_text(encoding="utf8"))
    except (OSError, ValueError) as exc:
        LOG.warning("Ignoring unreadable %s: %s", _SEED_PATH.name, exc)
        return []
    tickets = data.get("tickets") if isinstance(data, dict) else data
    return tickets if isinstance(tickets, list) else []


def _simplify(issue: Dict[str, Any], base_url: str) -> Dict[str, Any]:
    f = issue.get("fields") or {}
    status = (f.get("status") or {}).get("name") or "Unknown"
    assignee = (f.get("assignee") or {}).get("displayName") or "Unassigned"
    return {
        "key": issue.get("key", ""),
        "summary": f.get("summary") or "",
        "status": status,
        "type": (f.get("issuetype") or {}).get("name") or "",
        "priority": (f.get("priority") or {}).get("name") or "",
        "assignee": assignee,
        "updated": f.get("updated") or "",
        "labels": f.get("labels") or [],
        "url": f"{base_url}/browse/{issue.get('key', '')}" if base_url else "",
    }


class JiraBoard:
    """Polls one JQL query, caches the result for `poll_seconds`."""

    def __init__(self) -> None:
        self.config = JiraConfig()
        self._lock = threading.Lock()
        self._cache: List[Dict[str, Any]] = []
        self._fetched_at: float = 0.0
        self._reason: str = ""

    def _auth(self) -> Tuple[str, str]:
        return (self.config.email, self.config.token)

    def resolve_source(self, source: Optional[str] = None) -> str:
        """'jira' or 'seed' for this call: an explicit source, else TICKET_SOURCE; auto picks by credentials."""
        chosen = (source or SETTINGS.ticket_source or "auto").lower()
        if chosen not in TICKET_SOURCES:
            raise ValueError(f"ticket source must be one of {', '.join(TICKET_SOURCES)}")
        if chosen == "auto":
            return "jira" if self.config.configured else "seed"
        return chosen

    def tickets(self, force: bool = False, source: Optional[str] = None) -> Dict[str, Any]:
        """Cached queue. Always returns a dict; never raises."""
        cfg = self.config
        resolved = self.resolve_source(source)
        if resolved == "seed" or not cfg.configured:
            seeded = _seed_tickets()
            if resolved == "seed" and cfg.configured:
                reason = f"Demo mode (TICKET_SOURCE=seed): {len(seeded)} ticket(s) from {_SEED_PATH.name}"
            else:
                reason = f"Jira not configured - set {cfg.missing} in .env"
                if seeded:
                    reason += f" (showing {len(seeded)} ticket(s) from {_SEED_PATH.name})"
            return {"tickets": seeded, "fetched_at": None, "configured": False,
                    "reason": reason, "source": "seed"}

        with self._lock:
            fresh = (time.time() - self._fetched_at) < cfg.poll_seconds
            if fresh and not force and self._fetched_at:
                return {"tickets": list(self._cache), "fetched_at": self._fetched_at,
                        "configured": True, "reason": self._reason, "source": "jira"}

        try:
            resp = requests.get(
                f"{cfg.base_url}/rest/api/3/search/jql",
                params={"jql": cfg.jql, "fields": _FIELDS, "maxResults": 100},
                auth=self._auth(),
                headers={"Accept": "application/json"},
                timeout=cfg.timeout,
            )
            if resp.status_code != 200:
                reason = f"Jira returned {resp.status_code}: {resp.text[:200]}"
                LOG.warning(reason)
                with self._lock:
                    self._reason = reason
                    return {"tickets": list(self._cache), "fetched_at": self._fetched_at or None,
                            "configured": True, "reason": reason, "source": "jira"}
            issues = resp.json().get("issues", [])
            simplified = [_simplify(i, cfg.base_url) for i in issues]
            with self._lock:
                self._cache = simplified
                self._fetched_at = time.time()
                self._reason = ""
            return {"tickets": simplified, "fetched_at": self._fetched_at,
                    "configured": True, "reason": "", "source": "jira"}
        except requests.RequestException as exc:
            reason = f"Jira unreachable: {exc}"
            LOG.warning(reason)
            with self._lock:
                self._reason = reason
                return {"tickets": list(self._cache), "fetched_at": self._fetched_at or None,
                        "configured": True, "reason": reason, "source": "jira"}

    def issue(self, key: str, source: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """
        Full issue for a pipeline run, including `description` and `pull_requests`
        (list of PullRequest). None when the ticket cannot be found in the source.
        """
        cfg = self.config
        if self.resolve_source(source) == "seed":
            ticket = next((t for t in _seed_tickets() if t.get("key") == key), None)
            if ticket is None:
                return None
            ticket = dict(ticket)
            default_repo = repo_slug(SETTINGS.source_repo_url)
            prs = prs_from_seed(ticket.get("pull_requests") or [], default_repo)
            ticket["pull_requests"] = prs or prs_from_text(ticket.get("description", ""))
            return ticket
        if not cfg.configured:
            LOG.warning("TICKET_SOURCE=jira but %s is not set", cfg.missing)
            return None
        try:
            resp = requests.get(
                f"{cfg.base_url}/rest/api/3/issue/{key}",
                params={"fields": "summary,description,status,issuetype,priority,assignee"},
                auth=self._auth(),
                headers={"Accept": "application/json"},
                timeout=cfg.timeout,
            )
            if resp.status_code != 200:
                LOG.warning("Jira issue %s returned %s", key, resp.status_code)
                return None
            data = resp.json()
            out = _simplify(data, cfg.base_url)
            out["description"] = adf_to_text((data.get("fields") or {}).get("description"))
            out["pull_requests"] = self.pull_requests(data.get("id", "")) or prs_from_text(out["description"])
            return out
        except requests.RequestException as exc:
            LOG.warning("Jira issue %s unreachable: %s", key, exc)
            return None


    def pull_requests(self, issue_id: str) -> List[PullRequest]:
        """
        PRs linked to an issue in Jira's development panel (GitHub for Jira app).
        The detail endpoint only answers for the exact integration type, which the
        summary endpoint names, so ask the summary first. Empty list on any failure.
        """
        cfg = self.config
        if not issue_id:
            return []
        base, auth = f"{cfg.base_url}/rest/dev-status/latest/issue", self._auth()
        try:
            summary = requests.get(f"{base}/summary", params={"issueId": issue_id}, auth=auth, timeout=cfg.timeout)
            kinds = list(((summary.json().get("summary") or {}).get("pullrequest") or {})
                         .get("byInstanceType", {}).keys()) if summary.ok else []
            prs: List[PullRequest] = []
            for kind in kinds:
                detail = requests.get(f"{base}/detail", auth=auth, timeout=cfg.timeout,
                                      params={"issueId": issue_id, "applicationType": kind, "dataType": "pullrequest"})
                if not detail.ok:
                    continue
                for block in detail.json().get("detail") or []:
                    for pr in block.get("pullRequests") or []:
                        url = pr.get("url", "")
                        found = prs_from_text(url)
                        if found:
                            prs.append(PullRequest(number=found[0].number, repo=found[0].repo,
                                                   status=str(pr.get("status", "")), url=url))
            return prs
        except (requests.RequestException, ValueError) as exc:
            LOG.warning("Jira development panel unavailable for issue %s: %s", issue_id, exc)
            return []


def adf_to_text(node: Any) -> str:
    """
    Flatten Atlassian Document Format to plain text.

    The agents read the description as prose, so structure beyond line breaks and
    list bullets is noise. A plain string (older Jira, or already-flattened input)
    passes straight through.
    """
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return str(node)

    kind = node.get("type")
    if kind == "text":
        return node.get("text", "")
    if kind == "hardBreak":
        return "\n"

    inner = adf_to_text(node.get("content"))
    if kind in {"paragraph", "heading"}:
        return inner + "\n\n"
    if kind == "listItem":
        return f"- {inner.strip()}\n"
    if kind in {"bulletList", "orderedList", "codeBlock", "blockquote", "panel"}:
        return inner + "\n"
    if kind == "tableRow":
        return inner.strip() + "\n"
    if kind == "tableCell" or kind == "tableHeader":
        return inner.strip() + " | "
    return inner


_BOARD: Optional[JiraBoard] = None
_BOARD_LOCK = threading.Lock()


def get_board() -> JiraBoard:
    global _BOARD
    with _BOARD_LOCK:
        if _BOARD is None:
            _BOARD = JiraBoard()
        return _BOARD
