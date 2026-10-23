"""
The run's HTML report: a manual checklist a QA can work through, pre-filled from the
automation run.

One self-contained file per run (styles, data and screenshots inline), so it can be
opened from disk, attached to Jira or published as-is. It holds:
  - the automation results: one row per Playwright test, its error and screenshots;
  - one card per test case: preconditions, steps, a copyable rerun command and the
    case as JSON, and each step's expected result as an assertion to mark Pass, Fail,
    Blocked or Unverified - pre-marked from the automated test where there is one;
  - the gaps: criteria no case covers, manual-only and not-run cases, review findings,
    open questions from triage, pipeline errors;
  - a Markdown results table for Jira that follows the marks.

Everything here is deterministic: no model writes into this report.
"""
from __future__ import annotations

import base64
import json
import logging
import mimetypes
from pathlib import Path
from typing import Any, Dict, List, Optional

from taf.agents.state import QAState
from taf.config import SETTINGS, project_path, redact
from taf.core.text import utc_now
from taf.steps.review import covered_criteria

log = logging.getLogger("qa")

TEMPLATE = Path(__file__).with_name("report-template.html")
_DATA_MARKER = "/*__REPORT_DATA__*/null"
_MAX_SCREENSHOT_BYTES = 2_000_000


def _data_uri(path: str) -> Optional[str]:
    """A screenshot inlined, so the report survives Playwright clearing test-results/."""
    try:
        raw = Path(path).read_bytes()
    except OSError:
        return None
    if len(raw) > _MAX_SCREENSHOT_BYTES:
        return None
    mime = mimetypes.guess_type(path)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def _spec_rel(state: QAState) -> str:
    """The spec's path inside the test repo, for the rerun command."""
    spec = state.get("spec_file_path") or ""
    if not spec:
        return f"{SETTINGS.test_spec_dir}/{state.get('ticket_id', 'TICKET')}.spec.ts"
    path = Path(spec)
    if SETTINGS.test_repo_local_path:
        try:
            return path.resolve().relative_to(project_path(SETTINGS.test_repo_local_path).resolve()).as_posix()
        except ValueError:
            pass
    parts = path.as_posix().split("/")
    anchor = SETTINGS.test_spec_dir.split("/")[0]
    return "/".join(parts[parts.index(anchor):]) if anchor in parts else path.name


def build_report_data(state: QAState) -> Dict[str, Any]:
    """Everything the template renders, as plain JSON."""
    cases = state.get("test_cases") or []
    tests = (state.get("playwright") or {}).get("tests") or []
    triage = state.get("triage_analysis") or {}
    criteria: List[str] = triage.get("acceptance_criteria") or []
    passed = bool(state.get("execution_passed")) and not state.get("aborted")

    budget = SETTINGS.report_max_screenshots
    by_tc: Dict[str, Dict[str, Any]] = {}
    automation: List[Dict[str, Any]] = []
    for test in tests:
        shots = []
        for path in test.get("screenshots") or []:
            if budget <= 0:
                break
            uri = _data_uri(path)
            if uri:
                shots.append({"src": uri, "name": Path(path).name})
                budget -= 1
        row = {"tc": test.get("tc", ""), "title": test.get("title", ""), "status": test.get("status", ""),
               "duration_ms": test.get("duration_ms", 0), "error": redact(test.get("error") or ""),
               "screenshots": shots}
        automation.append(row)
        if row["tc"]:
            by_tc[row["tc"]] = row

    ran = bool(tests)
    case_rows, manual_only, not_run = [], [], []
    for case in cases:
        tc = case.get("test_id", "")
        auto = by_tc.get(tc)
        if not case.get("automatable"):
            manual_only.append(tc)
        elif ran and not auto:
            not_run.append(tc)
        case_rows.append({**{k: case.get(k) for k in ("test_id", "summary", "kind", "category", "priority",
                                                      "automatable", "preconditions")},
                          "ac_refs": case.get("acceptance_criteria_refs") or [],
                          "steps": case.get("steps") or [],
                          "automation": {k: auto[k] for k in ("status", "duration_ms", "error")} if auto else None,
                          "json": case})

    covered = covered_criteria(criteria, cases)
    uncovered = [{"n": n, "text": text} for n, text in enumerate(criteria, start=1) if n not in covered]
    validation = state.get("validation") or {}
    failed_checks = [c.get("name") for c in validation.get("checks") or [] if not c.get("ok") and not c.get("skipped")]

    return {
        "ticket_id": state.get("ticket_id", ""),
        "summary": state.get("summary", ""),
        "target_url": state.get("target_url", ""),
        "run_id": state.get("run_id", ""),
        "finished": utc_now(),
        "passed": passed,
        "ran": ran,
        "counts": (state.get("playwright") or {}).get("counts") or {},
        "branch": state.get("git_branch", ""),
        "pr_url": (state.get("publish") or {}).get("pr_url", ""),
        "trace_url": state.get("trace_url", ""),
        "spec": _spec_rel(state),
        "project": "qa-agent",
        "risk_level": triage.get("risk_level", ""),
        "feature_summary": triage.get("feature_summary", ""),
        "criteria": criteria,
        "automation": automation,
        "cases": case_rows,
        "gaps": {
            "uncovered_criteria": uncovered,
            "manual_only": manual_only,
            "not_run": not_run,
            "questions": triage.get("gaps_and_questions") or [],
            "risks": triage.get("risks") or [],
            "review": (state.get("review") or {}).get("findings") or [],
            "validation_failed": failed_checks,
            "errors": [redact(str(e)) for e in state.get("errors") or []],
        },
        "xray": state.get("xray_tests_json") or [],
    }


def render_html(data: Dict[str, Any]) -> str:
    # "</" inside a <script> would end it early; JSON allows the escaped form.
    payload = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    title = f"{data.get('ticket_id') or 'QA'} QA Report"
    return (TEMPLATE.read_text(encoding="utf-8")
            .replace("__TITLE__", title.replace("<", "&lt;"))
            .replace(_DATA_MARKER, payload))


def write_html_report(state: QAState) -> str:
    """Write the run's HTML report; returns its path, or "" if it could not be written."""
    try:
        out_dir = project_path(SETTINGS.reports_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{state.get('ticket_id', 'run')}-{(state.get('run_id') or 'run')[:8]}.html"
        path.write_text(render_html(build_report_data(state)), encoding="utf-8")
        return str(path)
    except Exception as exc:  # noqa: BLE001 - the Markdown report must still be returned
        log.warning("[%s] HTML report not written: %s", state.get("ticket_id"), exc)
        return ""
