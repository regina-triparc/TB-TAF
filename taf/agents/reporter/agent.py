"""
Agent 4 - Report Synthesizer: raw terminal logs -> Markdown QA report.

The verdict header is built in Python, so the local model can only format the log
details and can never flip a result. If Ollama is down (or the run is offline) a
deterministic report is produced instead - the run still completes.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict

from langchain_core.messages import HumanMessage, SystemMessage

from taf.agents.base import agent_node
from taf.agents.prompts import load_prompt
from taf.agents.reporter.html_report import write_html_report
from taf.agents.state import QAState
from taf.config import SETTINGS
from taf.core.text import message_text, truncate_middle, utc_now
from taf.integrations.paperclip.control_room import CONTROL_ROOM
from taf.llm.budget import BUDGET
from taf.llm.pricing import token_usage
from taf.llm.router import ROUTER
from taf.observability.tracing import TRACER
from taf.runs.usage import USAGE

log = logging.getLogger("qa")

SYSTEM_PROMPT = load_prompt(__file__, "prompt.md")

_PW_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|flaky|skipped|did not run|interrupted)", re.IGNORECASE)


def parse_playwright_counts(logs: str) -> Dict[str, int]:
    """Extract the final Playwright summary counters (e.g. '3 passed', '1 failed')."""
    counts: Dict[str, int] = {}
    for number, label in _PW_COUNT_RE.findall(logs or ""):
        counts[label.lower()] = int(number)  # last occurrence wins = final summary line
    return counts


def fallback_report_body(state: QAState, reason: str = "Local report synthesizer unavailable") -> str:
    """Deterministic report used when the local LLM is unavailable (or the run is offline)."""
    logs = state.get("execution_logs", "")
    failure_lines = [
        line.strip() for line in logs.splitlines()
        if re.search(r"(✘|\berror\b|failed|timed out|\[error\])", line, re.IGNORECASE)
    ][:40]
    body = ["### Results", f"_{reason} - showing extracted log lines._", ""]
    body += ["### Failures / errors", "```", *(failure_lines or ["(none detected)"]), "```"]
    body += ["", "### Log tail", "```", logs[-4000:], "```"]
    return "\n".join(body)


@agent_node("Report Synthesizer", engine="qwen-local", abort_on_error=False)
def report_synthesizer(state: QAState) -> Dict[str, Any]:
    ticket_id, run_id = state["ticket_id"], state["run_id"]
    provider = state.get("llm_provider")
    passed = bool(state.get("execution_passed")) and not state.get("aborted")
    logs = state.get("execution_logs", "")
    counts = (state.get("playwright") or {}).get("counts") or parse_playwright_counts(logs)
    errors = state.get("errors", [])
    trace_url = state.get("trace_url") or TRACER.trace_url(run_id)

    header = [
        f"## QA Report - {ticket_id}",
        f"**Status:** {'✅ QA Passed' if passed else '❌ QA Failed'}  ",
        f"**Summary:** {state.get('summary', '')}  ",
        f"**Target:** {state.get('target_url', '')}  ",
        f"**Run ID:** `{run_id}` · **Finished:** {utc_now()}  ",
    ]
    validation = state.get("validation") or {}
    if validation.get("checks"):
        failed = [c["name"] for c in validation["checks"] if not c.get("ok") and not c.get("skipped")]
        verdict = "passed" if validation.get("ok") else f"FAILED ({', '.join(failed)})"
        header.append(f"**Static validation:** {verdict} after {validation.get('attempt', 1)} attempt(s)  ")
    review = state.get("review") or {}
    if review.get("findings"):
        severities: Dict[str, int] = {}
        for f in review["findings"]:
            severities[f["severity"]] = severities.get(f["severity"], 0) + 1
        header.append("**Review:** " + ", ".join(f"{n} {k}" for k, n in severities.items()) + "  ")
    approval = state.get("approval") or {}
    if approval.get("status"):
        header.append(f"**Approval:** {approval['status']}"
                      + (f" by {approval['by']}" if approval.get("by") else "")
                      + (f" ({approval['note']})" if approval.get("note") else "") + "  ")
    if state.get("git_branch"):
        header.append(f"**Spec branch:** `{state['git_branch']}` (pushed: {'yes' if state.get('git_pushed') else 'no'})  ")
    pr_url = (state.get("publish") or {}).get("pr_url")
    if pr_url:
        header.append(f"**Pull request:** {pr_url}  ")
    if trace_url:
        header.append(f"**Trace:** {trace_url}  ")
    html_path = write_html_report({**state, "trace_url": trace_url})
    if html_path:
        header.append(f"**HTML report:** `{html_path}`  ")
    if counts:
        header.append("**Playwright:** " + ", ".join(f"{v} {k}" for k, v in counts.items()) + "  ")
    cache_reads = BUDGET.run_cache_read_tokens(run_id)
    header.append(f"**Cloud cost:** ${BUDGET.run_cost_usd(run_id):,.4f} · {BUDGET.run_usage(run_id):,} tokens"
                  + (f" + {cache_reads:,} read from prompt cache" if cache_reads else "") + "  ")
    offline = ROUTER.resolve(provider) == "offline"
    header.append("**Models:** " + ", ".join(f"{r.label} → `{r.model}` ({r.provider})"
                                             for r in ROUTER.routes(provider).values())
                  + (", Report Synthesizer → built-in fallback" if offline
                     else f", Report Synthesizer → `{SETTINGS.ollama_model}` (local)"))

    triage = state.get("triage_analysis") or {}
    if triage:
        header += ["", f"**Risk level:** {triage.get('risk_level', 'n/a')} - {triage.get('feature_summary', '')}"]
        if triage.get("gaps_and_questions"):
            header += ["", "**Open questions for the team:**"] + [f"- {q}" for q in triage["gaps_and_questions"]]
    if errors:
        header += ["", "### Pipeline errors"] + [f"- {e}" for e in errors]

    if not logs.strip():
        body = "_No tests were executed in this run._"
    elif offline:
        body = fallback_report_body(state, "Offline provider")
    else:
        try:
            response = ROUTER.report_model().invoke([
                SystemMessage(content=SYSTEM_PROMPT),
                HumanMessage(content=f"Overall verdict (authoritative): {'PASSED' if passed else 'FAILED'}\n\n"
                                     f"```\n{truncate_middle(logs, SETTINGS.max_exec_log_chars_for_llm)}\n```"),
            ], config=report_config(state))
            local_usage = token_usage(response)
            USAGE.record_call(run_id, "Report Synthesizer", "ollama", SETTINGS.ollama_model, local_usage)
            CONTROL_ROOM.emit(run_id, ticket_id, "llm.usage", agent="Report Synthesizer",
                              provider="ollama", model=SETTINGS.ollama_model, billing_type="fixed",
                              input_tokens=local_usage.input_tokens, output_tokens=local_usage.output_tokens,
                              cost_cents=0)
            body = message_text(response).strip() or fallback_report_body(state)
        except Exception as exc:  # noqa: BLE001 - Ollama down/timeout must not fail the run
            log.warning("[%s] Local LLM unavailable (%s) - using fallback report.", ticket_id, exc)
            CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent="Report Synthesizer",
                              message=f"Ollama unavailable, fallback report used: {type(exc).__name__}")
            body = fallback_report_body(state)

    return {"final_report": "\n".join(header) + "\n\n" + body, "execution_passed": passed,
            "trace_url": trace_url, "report_html_path": html_path}


def report_config(state: QAState) -> Dict[str, Any]:
    """Langfuse callbacks and labels for the local report model's call."""
    return {"callbacks": TRACER.langchain_callbacks(), "run_name": "Report Synthesizer",
            "tags": ["agent:report", "provider:ollama"],
            "metadata": {"run_id": state.get("run_id", ""), "ticket_id": state.get("ticket_id", ""),
                         "agent": "report", "provider": "ollama", "model": SETTINGS.ollama_model}}
