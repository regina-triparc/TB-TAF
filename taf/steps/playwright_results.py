"""
Per-test results from Playwright's JSON report, and the failure context it points to.

`run_tests` and `repair_from_run` both run Playwright with the JSON reporter writing to
a file (see TestRepository.run_playwright). Each failed test usually carries an
`error-context.md` attachment: the error, the page's accessibility snapshot at the
moment it failed, and the failing source line. That snapshot is what a repair needs:
it shows the real roles, names and values where the spec guessed.
"""
from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from taf.core.text import truncate_middle

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_TC_RE = re.compile(r"\[(TC-\d+)\]")
TEST_START_RE = re.compile(r"^\s*test(?:\.(?:fixme|skip|fail|only))?\(\s*(['\"`])[^\n]*?\[(TC-\d+)\]", re.MULTILINE)

TestResult = Dict[str, Any]


def report_path(run_id: str, label: str) -> Path:
    """Where a run's JSON report goes: outside the test repository, one file per Playwright run."""
    return Path(tempfile.gettempdir()) / "taf-playwright" / run_id / f"{label}.json"


def _status(test: Dict[str, Any]) -> str:
    outcome = test.get("status")  # expected | unexpected | flaky | skipped
    if outcome == "skipped":
        return "skipped"
    if outcome in {"expected", "flaky"}:
        return "passed"
    return "failed"


def _walk(suite: Dict[str, Any], out: List[TestResult]) -> None:
    for spec in suite.get("specs") or []:
        for test in spec.get("tests") or []:
            if test.get("projectName") == "setup":
                continue
            results = test.get("results") or [{}]
            last = results[-1]
            error = (last.get("error") or {}).get("message") or ""
            attachments = last.get("attachments") or []
            context = next((a.get("path") for a in attachments
                            if a.get("name") == "error-context" and a.get("path")), None)
            screenshots = [a["path"] for a in attachments
                           if a.get("path") and str(a.get("contentType", "")).startswith("image/")]
            title = spec.get("title", "")
            tc = _TC_RE.search(title)
            out.append({"title": title, "tc": tc.group(1) if tc else "", "status": _status(test),
                        "duration_ms": sum(int(r.get("duration") or 0) for r in results),
                        "error": _ANSI_RE.sub("", error)[:2000], "error_context": context or "",
                        "screenshots": screenshots})
    for child in suite.get("suites") or []:
        _walk(child, out)


def parse_json_report(path: Path) -> List[TestResult]:
    """Every test in the report with its final status; [] when the report is missing or unreadable."""
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    out: List[TestResult] = []
    for suite in report.get("suites") or []:
        _walk(suite, out)
    return out


def test_bodies(code: str) -> Dict[str, str]:
    """Each `[TC-xxx]` test's source, from its `test(` line to the next test (or the end)."""
    starts = [(m.start(), m.group(2)) for m in TEST_START_RE.finditer(code)]
    bodies: Dict[str, str] = {}
    for i, (start, tc) in enumerate(starts):
        end = starts[i + 1][0] if i + 1 < len(starts) else len(code)
        bodies[tc] = code[start:end]
    return bodies


def run_passed(tests: List[TestResult]) -> bool:
    """A run passes when at least one test passed and none failed: all skipped is not a pass."""
    statuses = {t["status"] for t in tests}
    return "passed" in statuses and "failed" not in statuses


def counts(tests: List[TestResult]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for test in tests:
        out[test["status"]] = out.get(test["status"], 0) + 1
    return out


def _section(text: str, heading: str) -> str:
    match = re.search(rf"^# {re.escape(heading)}\s*$(.*?)(?=^# |\Z)", text, re.MULTILINE | re.DOTALL)
    return match.group(1).strip() if match else ""


def failure_context(test: TestResult, snapshot_chars: int = 3000) -> str:
    """One failed test, for the repair prompt: error, failing line, and the page at failure."""
    parts = [f"### {test['title']}", "Error:", "```", truncate_middle(test.get("error") or "(no message)", 1200), "```"]
    path: Optional[str] = test.get("error_context")
    text = ""
    if path:
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    if text:
        source = _section(text, "Test source")
        failing = [line for line in source.splitlines() if line.startswith(">")]
        if failing:
            parts += ["Failing line:", "```", failing[0], "```"]
        snapshot = _section(text, "Page snapshot")
        if snapshot:
            parts += ["Page snapshot when it failed:", truncate_middle(snapshot, snapshot_chars)]
    return "\n".join(parts)
