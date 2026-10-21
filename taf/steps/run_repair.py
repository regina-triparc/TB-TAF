"""
Repair from the run: failing tests go back to the Automation Engineer with what
Playwright saw when they failed, and the corrected spec is run again.

Static validation only proves a spec loads. Most generated tests that fail do so
because a guess about the page was wrong (a "6:30 PM" option in a 24-hour picker,
an index that points at the toolbar button). The failure's `error-context.md` holds
the page snapshot that answers the guess, so a repair turn with it usually fixes the
test - but a repair loop left alone also learns to make tests pass by weakening them.
So every repair is checked before it is run and kept only if it is better:

  rejected   a test lost expectations without becoming an explicit `test.fixme`
             (quarantine), or a test that passed was changed into fixme/skip
  rerun      only the failing tests plus any passing test whose code changed (by line)
  kept       at least one failing test now passes or is quarantined, and no test
             that passed now fails

Up to RUN_REPAIR_ATTEMPTS rounds; a rejected round tells the model why.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

from taf.agents.automation.agent import RUN_REPAIR_PROMPT, build_messages, parse_spec
from taf.agents.base import agent_node
from taf.agents.state import QAState
from taf.config import SETTINGS
from taf.core.text import truncate_middle
from taf.integrations.github.repository import REPO_LOCK, TEST_REPO, CommandError
from taf.integrations.jira import xray
from taf.llm.router import ROUTER
from taf.steps.context import append_log
from taf.steps.playwright_results import TestResult, counts, failure_context, parse_json_report, report_path
from taf.steps.validation import code_checks, repo_checks

_TEST_START_RE = re.compile(r"^\s*test(?:\.(?:fixme|skip|fail|only))?\(\s*(['\"`])[^\n]*?\[(TC-\d+)\]", re.MULTILINE)
_EXPECT_RE = re.compile(r"\bexpect(?:\.soft|\.poll)?\s*\(")
_QUARANTINE_RE = re.compile(r"\btest\.(fixme|skip)\s*\(")

FAILURES_MAX_CHARS = 24_000


def test_bodies(code: str) -> Dict[str, str]:
    """Each `[TC-xxx]` test's source, from its `test(` line to the next test (or the end)."""
    starts = [(m.start(), m.group(2)) for m in _TEST_START_RE.finditer(code)]
    bodies: Dict[str, str] = {}
    for i, (start, tc) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(code)
        bodies[tc] = code[start:end]
    return bodies


def _quarantined(body: str) -> bool:
    return bool(_QUARANTINE_RE.search(body))


def _normalise(body: str) -> str:
    return re.sub(r"\s+", " ", body).strip()


def weakened(before: str, after: str, passing: List[str]) -> List[str]:
    """
    Why the repaired spec is not acceptable, one reason per test: a test lost
    `expect(` calls without being quarantined, a passing test became fixme/skip,
    or a test disappeared.
    """
    old, new = test_bodies(before), test_bodies(after)
    reasons = []
    for tc, body in old.items():
        if tc not in new:
            reasons.append(f"{tc} was removed")
            continue
        was, now = len(_EXPECT_RE.findall(body)), len(_EXPECT_RE.findall(new[tc]))
        if tc in passing and _quarantined(new[tc]) and not _quarantined(body):
            reasons.append(f"{tc} passed but was turned into fixme/skip")
        elif now < was and not _quarantined(new[tc]):
            reasons.append(f"{tc} went from {was} to {now} expect() calls")
    return reasons


def changed(before: str, after: str) -> List[str]:
    old, new = test_bodies(before), test_bodies(after)
    return [tc for tc in new if _normalise(new[tc]) != _normalise(old.get(tc, ""))]


def test_lines(code: str, tcs: List[str]) -> List[int]:
    """1-based line of each listed test's `test(` call - Playwright's `<spec>:<line>` filter."""
    wanted, lines = set(tcs), []
    for match in _TEST_START_RE.finditer(code):
        if match.group(2) in wanted:
            start = match.start() + match.group(0).index("test")
            lines.append(code.count("\n", 0, start) + 1)
    return sorted(lines)


def repair_turn(state: QAState, code: str, failing: List[TestResult], rejection: str) -> List[BaseMessage]:
    route = ROUTER.route("automation", state.get("llm_provider"))
    rel_spec = f"{SETTINGS.test_spec_dir.strip('/')}/{state['ticket_id']}.spec.ts"
    failures = truncate_middle("\n\n".join(failure_context(t) for t in failing), FAILURES_MAX_CHARS)
    text = RUN_REPAIR_PROMPT.format(
        failing=", ".join(t["tc"] or t["title"] for t in failing), failures=failures, spec=rel_spec,
        rejection=f"Your previous repair was rejected: {rejection}\n\n" if rejection else "")
    block: Dict[str, Any] = {"type": "text", "text": text}
    if route.cache_context:
        block["cache_control"] = ROUTER.cache_control()
    return [*build_messages(state, route), AIMessage(content=f"```typescript\n{code}\n```"),
            HumanMessage(content=[block])]


def run_candidate(state: QAState, code: str, tcs: List[str], label: str,
                  logs: List[str]) -> Tuple[Optional[str], List[TestResult]]:
    """Statically validate and run `code` (only `tcs`). Returns (problem, results)."""
    ticket_id = state["ticket_id"]
    spec_rel = f"{SETTINGS.test_spec_dir.strip('/')}/{ticket_id}.spec.ts"
    assert TEST_REPO.path is not None
    problems = [c for c in code_checks(code, spec_rel, TEST_REPO.path.resolve()) if not c["ok"]]
    if problems:
        return "; ".join(f"{c['name']}: {c['detail']}" for c in problems), []
    report = report_path(state["run_id"], label)
    with REPO_LOCK:
        try:
            with TEST_REPO.ticket_files_on_base(ticket_id, code, xray.ticket_dir(ticket_id), {}, logs) as (spec, _):
                assert spec is not None
                listing = [c for c in repo_checks(spec, state.get("target_url", ""), logs)
                           if not c["ok"] and not c["skipped"]]
                if listing:
                    return "; ".join(f"{c['name']}: {c['detail'][:500]}" for c in listing), []
                TEST_REPO.run_playwright(spec, state.get("target_url", ""), logs, json_report=report,
                                         lines=test_lines(code, tcs))
        except CommandError as exc:
            return f"test repository: {exc}", []
    return None, parse_json_report(report)


def merge(tests: List[TestResult], rerun: List[TestResult]) -> List[TestResult]:
    by_tc = {t["tc"]: t for t in rerun if t["tc"]}
    return [by_tc.get(t["tc"], t) for t in tests]


@agent_node("Run Repair", engine="llm", abort_on_error=False, route_key="automation")
def repair_from_run(state: QAState) -> Dict[str, Any]:
    playwright = state.get("playwright") or {}
    tests: List[TestResult] = list(playwright.get("tests") or [])
    code = state.get("generated_typescript_code") or ""
    if (SETTINGS.run_repair_attempts <= 0 or ROUTER.resolve(state.get("llm_provider")) == "offline"
            or not code or not (state.get("validation") or {}).get("ok") or TEST_REPO.path is None):
        return {}
    failing = [t for t in tests if t["status"] == "failed" and t["tc"]]
    if not failing:
        return {}

    logs: List[str] = []
    before = counts(tests)
    attempts: List[Dict[str, Any]] = []
    rejection = ""
    for n in range(1, SETTINGS.run_repair_attempts + 1):
        record: Dict[str, Any] = {"attempt": n, "failing_before": [t["tc"] for t in failing]}
        answer = ROUTER.text(state, ROUTER.route("automation", state.get("llm_provider")),
                             repair_turn(state, code, failing, rejection))
        candidate, problem = parse_spec(answer)
        passing = [t["tc"] for t in tests if t["status"] == "passed"]
        if candidate is None:
            record.update(outcome="rejected", reason=f"no usable spec: {problem}")
        elif reasons := weakened(code, candidate, passing):
            record.update(outcome="rejected", reason="; ".join(reasons))
        else:
            rerun_tcs = [t["tc"] for t in failing] + [tc for tc in changed(code, candidate) if tc in passing]
            problem, results = run_candidate(state, candidate, rerun_tcs, f"repair-{n}", logs)
            status = {t["tc"]: t["status"] for t in results}
            fixed = [t["tc"] for t in failing if status.get(t["tc"]) == "passed"]
            quarantined = [t["tc"] for t in failing if status.get(t["tc"]) == "skipped"]
            broke = [tc for tc in rerun_tcs if tc in passing and status.get(tc) != "passed"]
            record.update(rerun=rerun_tcs, fixed=fixed, quarantined=quarantined, broke=broke)
            if problem:
                record.update(outcome="rejected", reason=f"does not load: {problem}")
            elif not results:
                record.update(outcome="rejected", reason="the rerun produced no results")
            elif broke:
                record.update(outcome="rejected", reason=f"previously passing tests now fail: {broke}")
            elif not fixed and not quarantined:
                record.update(outcome="rejected", reason="no failing test was fixed")
            else:
                record["outcome"] = "kept"
                code, tests = candidate, merge(tests, results)
                failing = [t for t in tests if t["status"] == "failed" and t["tc"]]
        attempts.append(record)
        rejection = record.get("reason", "") if record["outcome"] == "rejected" else ""
        logs.append(f"[info] Run repair {n}: {record['outcome']}"
                    + (f" - {record['reason']}" if record.get("reason") else "")
                    + (f" - fixed {record['fixed']}" if record.get("fixed") else "")
                    + (f", quarantined {record['quarantined']}" if record.get("quarantined") else ""))
        if not failing:
            break

    if SETTINGS.no_git:
        # In-place mode keeps whatever was written last, which may be a rejected candidate.
        with REPO_LOCK:
            TEST_REPO.write_spec(state["ticket_id"], code, logs)

    after = counts(tests)
    passed = bool(tests) and all(t["status"] != "failed" for t in tests)
    return {
        "generated_typescript_code": code,
        "execution_passed": passed,
        "execution_logs": append_log(state, logs),
        "playwright": {**playwright, "passed": passed, "counts": after, "tests": tests},
        "run_repair": {"attempts": attempts, "before": before, "after": after,
                       "accepted": any(a["outcome"] == "kept" for a in attempts)},
    }
