"""
Agent 4 - Report Synthesizer: raw terminal logs -> Markdown QA report.

The verdict header is built in Python, so the local model can only format the log
details and can never flip a result. When the run has parsed Playwright results, the
Results and Failures sections are built from them too; the model then writes only the
infrastructure notes and recommendations. If Ollama is down (or the run is offline) a
deterministic report is produced instead - the run still completes.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

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
# The prompt's Results/Failures instructions, dropped when those sections are built from parsed results.
_RESULTS_PART_RE = re.compile(r"### Results\n.*?(?=### Infrastructure issues)", re.DOTALL)
RESULTS_PROMPT_PART = (_RESULTS_PART_RE.search(SYSTEM_PROMPT) or [""])[0]
RESULTS_GIVEN_NOTE = ("\nThe Results and Failures sections are already written from the parsed test results "
                      "below - do not repeat them. Write only the other sections; use these results, not the "
                      "log, for what passed and failed.\n\n")


def strip_results_sections(text: str) -> str:
    """Drop any ### Results / ### Failures section the model wrote anyway."""
    return re.sub(r"### (Results|Failures)\b.*?(?=^### |\Z)", "", text, flags=re.DOTALL | re.MULTILINE).strip()

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


_STATUS_LABEL = {"passed": "✅ Passed", "failed": "❌ Failed", "timedOut": "❌ Timed out", "skipped": "⏭ Skipped",
                 "interrupted": "⚠ Interrupted", "flaky": "⚠ Flaky"}


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ").strip()


def _key_error_line(error: str) -> str:
    """
    The gist of a Playwright error: its first line (the failed expectation or thrown
    message), plus the Locator / Expected / Received lines when it has them.
    """
    lines = [l.strip() for l in (error or "").split("Call log", 1)[0].splitlines() if l.strip()]
    if not lines:
        return ""
    parts = [lines[0]]
    for prefix in ("Locator:", "Expected", "Received", "Error: element"):
        hit = next((l for l in lines[1:] if l.startswith(prefix)), "")
        if hit and hit not in parts:
            parts.append(hit)
    return " · ".join(p[:200] for p in parts)[:500]


def results_sections(tests: List[Dict[str, Any]]) -> str:
    """
    ### Results and ### Failures, built from the run's parsed Playwright results -
    never from a model: a small local model reading a truncated log invented
    passes and durations (TB-777: 8 failures reported as 11 passes).
    """
    rows = ["### Results", "| Test | Status | Duration |", "| --- | --- | --- |"]
    failures: List[str] = []
    for t in tests:
        status = str(t.get("status") or "")
        duration = f"{(t.get('duration_ms') or 0) / 1000:.1f}s"
        rows.append(f"| {_cell(t.get('title', ''))} | {_STATUS_LABEL.get(status, status)} | {duration} |")
        if status in {"failed", "timedOut", "interrupted"}:
            line = _key_error_line(t.get("error") or "")
            failures.append(f"- **{_cell(t.get('title', ''))}**" + (f"\n  `{line.replace('`', chr(39))}`" if line else ""))
    rows += ["", "### Failures", *(failures or ["None."])]
    return "\n".join(rows)


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
    header += classification_lines(state) + clarify_lines(state) + skills_lines(state)
    if errors:
        header += ["", "### Pipeline errors"] + [f"- {e}" for e in errors]

    tests = (state.get("playwright") or {}).get("tests") or []
    # With parsed results, Results/Failures are exact and the model only writes the
    # infrastructure notes and recommendations; without them it summarises the log as before.
    results = results_sections(tests) if tests else ""
    if not logs.strip():
        body = results or "_No tests were executed in this run._"
    elif offline:
        body = (results + "\n\n" if results else "") + fallback_report_body(state, "Offline provider")
    else:
        try:
            prompt = (SYSTEM_PROMPT if not results else
                      SYSTEM_PROMPT.replace(RESULTS_PROMPT_PART, "") + RESULTS_GIVEN_NOTE + results)
            response = ROUTER.report_model().invoke([
                SystemMessage(content=prompt),
                HumanMessage(content=f"Overall verdict (authoritative): {'PASSED' if passed else 'FAILED'}\n\n"
                                     f"```\n{truncate_middle(logs, SETTINGS.max_exec_log_chars_for_llm)}\n```"),
            ], config=report_config(state))
            local_usage = token_usage(response)
            USAGE.record_call(run_id, "Report Synthesizer", "ollama", SETTINGS.ollama_model, local_usage)
            CONTROL_ROOM.emit(run_id, ticket_id, "llm.usage", agent="Report Synthesizer",
                              provider="ollama", model=SETTINGS.ollama_model, billing_type="fixed",
                              input_tokens=local_usage.input_tokens, output_tokens=local_usage.output_tokens,
                              cost_cents=0)
            text = message_text(response).strip()
            if results:
                body = results + ("\n\n" + strip_results_sections(text) if text else "")
            else:
                body = text or fallback_report_body(state)
        except Exception as exc:  # noqa: BLE001 - Ollama down/timeout must not fail the run
            log.warning("[%s] Local LLM unavailable (%s) - using fallback report.", ticket_id, exc)
            CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent="Report Synthesizer",
                              message=f"Ollama unavailable, fallback report used: {type(exc).__name__}")
            body = results or fallback_report_body(state)

    return {"final_report": "\n".join(header) + "\n\n" + body, "execution_passed": passed,
            "trace_url": trace_url, "report_html_path": html_path}


def clarify_lines(state: QAState) -> List[str]:
    """The clarify gate's outcome: answers given, questions left open, and why it paused."""
    gate = state.get("clarify") or {}
    if not gate.get("status"):
        return []
    labels = {"passed": "passed (ticket clear)", "not_required": "not asked", "answered": "answered",
              "proceeded": "proceeded without answers", "rejected": "rejected - no tests designed"}
    lines = ["", f"**Clarify gate:** {labels.get(gate['status'], gate['status'])}"
             + (f" by {gate['by']}" if gate.get("by") else "")
             + f" · triage confidence {float(gate.get('confidence', 1.0)):.2f}"
             + (f" ({gate['note']})" if gate.get("note") else "") + "  "]
    answers = state.get("clarifications") or []
    if answers:
        lines += ["", "**Answers from the team:**"] + [f"- {a['id']}: {a['question']} → {a['answer']}" for a in answers]
    answered = {a["id"] for a in answers}
    still_open = [q for q in gate.get("questions") or [] if q["id"] not in answered]
    if still_open:
        lines += ["", "**Open questions (unanswered):**"]
        lines += [f"- {q['id']}{' (blocking)' if q.get('blocking') else ''}: {q['text']}" for q in still_open]
    if gate.get("risks"):
        lines += ["", "**Tested without an answer (risks):**"] + [f"- {r}" for r in gate["risks"]]
    return lines


def classification_lines(state: QAState) -> List[str]:
    """Action, layer and why; the existing tests the plan builds on; the plan gate."""
    cls = state.get("classification") or {}
    if not cls:
        return []
    lines = ["", f"**Classification:** action `{cls.get('action')}`, layer `{cls.get('layer')}`, "
                 f"{cls.get('change_type')} - {cls.get('rationale', '')}"
             + (f" ({cls['note']})" if cls.get("note") else "")
             + (f" - overridden by {cls['overridden_by']}" if cls.get("overridden_by") else "") + "  "]
    if not SETTINGS.route_by_classification and cls.get("action") in {"skip", "regression_only"}:
        lines.append(f"_ROUTE_BY_CLASSIFICATION=false: generated a new spec anyway (today's path)._  ")
    coverage = state.get("coverage") or {}
    if coverage.get("available"):
        lines.append(f"**Existing coverage:** {coverage.get('specs', 0)} spec(s), {coverage.get('tests', 0)} test(s) "
                     f"indexed; {len(state.get('coverage_candidates') or [])} candidate(s) shown to the architect  ")
    on_existing = [c for c in state.get("test_cases") or [] if c.get("disposition") in {"update", "reuse"}]
    if on_existing:
        lines += ["", "**Cases on existing tests:**"]
        lines += [f"- {c['test_id']} {c['disposition']}s `{c.get('target_spec')}` - {c.get('target_test')}"
                  + (f" ({c['xray_key']})" if c.get("xray_key") else "") for c in on_existing]
    review = state.get("plan_review") or {}
    if review.get("status") and review["status"] != "passed":
        lines.append(f"**Plan gate:** {review['status']}" + (f" by {review['by']}" if review.get("by") else "")
                     + (f" - {'; '.join(review.get('reasons') or [])}" if review.get("reasons") else "") + "  ")
    run = state.get("regression_run") or {}
    if run:
        lines.append("**Regression run (existing specs):** " + (f"skipped - {run['skipped']}" if run.get("skipped")
                     else f"{', '.join(run.get('specs') or [])} - {'passed' if run.get('passed') else 'did not pass'}")
                     + "  ")
    return lines


def skills_lines(state: QAState) -> List[str]:
    """Skills each agent was given, and the ones left out with the reason."""
    used = state.get("skills_used") or {}
    if not used:
        return []
    lines = ["", "**Skills:**"]
    for agent, record in used.items():
        loaded = ", ".join(record.get("loaded") or []) or "none"
        refs = f"; references: {', '.join(record['references'])}" if record.get("references") else ""
        skipped = "; ".join(f"{s['name']} ({s['reason']})" for s in record.get("skipped") or [])
        lines.append(f"- {agent}: {loaded}{refs}" + (f" - skipped: {skipped}" if skipped else ""))
    return lines


def report_config(state: QAState) -> Dict[str, Any]:
    """Langfuse callbacks and labels for the local report model's call."""
    return {"callbacks": TRACER.langchain_callbacks(), "run_name": "Report Synthesizer",
            "tags": ["agent:report", "provider:ollama"],
            "metadata": {"run_id": state.get("run_id", ""), "ticket_id": state.get("ticket_id", ""),
                         "agent": "report", "provider": "ollama", "model": SETTINGS.ollama_model}}
