"""Agent 5 - Closeout: POST the verdict to n8n, which updates Jira and imports the tests into Xray."""
from __future__ import annotations

import logging
from typing import Any, Dict

import requests

from taf.agents.base import agent_node
from taf.agents.state import QAState
from taf.config import SETTINGS, redact
from taf.core.http import retrying_session
from taf.llm.budget import BUDGET

log = logging.getLogger("qa")


@agent_node("Closeout", engine="http", abort_on_error=False)
def closeout(state: QAState) -> Dict[str, Any]:
    ticket_id = state["ticket_id"]
    payload = {
        "ticket_id": ticket_id,
        "status": "QA Passed" if state.get("execution_passed") else "QA Failed",
        "report_markdown": state.get("final_report", ""),
        "xray_csv_content": state.get("xray_csv_content", ""),
        "xray_tests_json": state.get("xray_tests_json", []),
        # Extra context - n8n can ignore it, but it helps idempotency and traceability.
        "metadata": {
            "run_id": state["run_id"],
            "git_branch": state.get("git_branch", ""),
            "git_pushed": state.get("git_pushed", False),
            "cloud_tokens_used": BUDGET.run_usage(state["run_id"]),
            "cloud_cost_usd": round(BUDGET.run_cost_usd(state["run_id"]), 4),
            "cache_read_tokens": BUDGET.run_cache_read_tokens(state["run_id"]),
            "aborted": bool(state.get("aborted")),
            "errors": state.get("errors", []),
        },
    }
    url = state.get("n8n_callback_url", "")
    if not url:
        return {"callback_delivered": False, "errors": ["Closeout: no n8n_callback_url provided."]}

    try:
        response = retrying_session().post(url, json=payload, timeout=SETTINGS.callback_timeout_s)
    except requests.RequestException as exc:
        return {"callback_delivered": False, "errors": [f"Closeout: n8n callback failed: {redact(str(exc))}"]}
    if response.status_code >= 400:
        return {
            "callback_delivered": False,
            "errors": [f"Closeout: n8n callback returned HTTP {response.status_code}: {response.text[:300]}"],
        }
    log.info("[%s] n8n callback delivered (%s): %s", ticket_id, response.status_code, payload["status"])
    return {"callback_delivered": True}
