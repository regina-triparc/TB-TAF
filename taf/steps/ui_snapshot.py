"""
Live UI exploration - what the app under test looks like right now, discovered before
the Automation Engineer writes a spec.

Nothing about the app is fixed in TAF: no trip, no row names, no selectors. The explorer
(ui_explorer.cjs) opens the app with the test repository's saved session, opens one item
from the start page (LIVE_EXPLORE_ENTRY, else the first data row), and clicks the controls
the ticket talks about - ranked by keywords taken from the ticket and its test cases -
recording the accessibility tree of whatever each click reveals. The names it sees are
sample data from whichever item it opened; the prompt says so, so they are read as
structure, never copied into a spec.

Read-only by construction (see ui_explorer.cjs): write requests are aborted and
submit-like or destructive clicks refused. A missing session, or any failure, only
skips the step - the spec is then written from the page objects and the page rules.
"""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from taf.agents.base import agent_node
from taf.agents.state import QAState
from taf.config import SETTINGS
from taf.core.text import truncate_middle
from taf.integrations.github.repository import REPO_LOCK, TEST_REPO, run_command
from taf.llm.router import ROUTER
from taf.steps.context import append_log

SCRIPT = Path(__file__).with_name("ui_explorer.cjs")

MAX_KEYWORDS = 30
_QUOTED_RE = re.compile(r"['\"‘“]([^'\"’”\n]{2,40})['\"’”]")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z&-]{2,}")
_STOP = set("""
the and for with that this from into when then than are was were has have had not but any all each every
can may must should will shall does did its it's their there these those who what which while where after
before without within between only also more most less least same such very just over under above below
test tests case cases user users page pages shows show shown display displays displayed open opens opened
click clicks clicked verify ensure check checks given able new one two three via per both other others
expected result results value values field fields item items row rows button buttons visible
""".split())


def explore_keywords(state: QAState) -> List[str]:
    """
    What to look for on the page, from the ticket's own words: quoted UI text first
    ('Add Service', 'Info only'), then the summary's and the test steps' significant words.
    """
    texts = [state.get("summary") or "", state.get("description") or ""]
    for tc in state.get("test_cases") or []:
        texts.append(str(tc.get("summary") or ""))
        for step in tc.get("steps") or []:
            texts += [str(step.get("action") or ""), str(step.get("expected_result") or "")]
    found: List[str] = []

    def add(word: str) -> None:
        word = word.strip().lower()
        if word and word not in found and word not in _STOP and not word.isdigit():
            found.append(word)

    for text in texts:
        for phrase in _QUOTED_RE.findall(text):
            # UI text, not data: no markup, paths, dates or ids.
            if len(phrase.split()) <= 4 and not phrase.strip().startswith(("<", "/", "{")) \
                    and not re.search(r"\d{3,}|\d+[-/:]\d+", phrase):
                add(phrase)
    for text in texts[:1] + texts[2:]:
        for word in _WORD_RE.findall(text):
            add(word)
    return found[:MAX_KEYWORDS]


def parse_output(stdout: str) -> Dict[str, Any]:
    """The script prints one JSON line last; anything before it (global setup noise) is ignored."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {"ok": False, "reason": "the explorer printed no result"}


def render_snapshot(result: Dict[str, Any], limit: int) -> str:
    """The explored states as a prompt section, or "" when nothing usable was captured."""
    states = [s for s in result.get("states") or [] if s.get("ok") and s.get("snapshot")]
    if not result.get("ok") or not states:
        return ""
    lines = ["## Live page snapshot (captured from the running app for this run - authoritative)",
             "The app was explored for this run: each block is the accessibility tree of what one click revealed, "
             "from `ariaSnapshot()`. Use these roles, names, option values and formats exactly. Where a page object "
             "disagrees, the page object is out of date: use the snapshot and add `// ASSUMPTION:`.",
             "Data names here (trips, services, travellers, dates, prices, counts) belong to whichever item the "
             "explorer happened to open. They show the STRUCTURE only: never hard-code them in a spec.",
             f"Start URL: {result.get('url', '')}"]
    if result.get("entry"):
        lines.append(f"Item opened from the start page (sample, do not hard-code): {result['entry']}")
    for state in states:
        lines += ["", f"### {state['name']}", "```yaml", state["snapshot"], "```"]
    failed = [s["name"] for s in result.get("states") or [] if not s.get("ok")]
    if failed:
        lines += ["", f"(Not captured: {', '.join(failed)} - rely on the page objects for these.)"]
    return truncate_middle("\n".join(lines), limit)


@agent_node("Live UI Snapshot", engine="local-cli", abort_on_error=False, span_type="retriever")
def ui_snapshot(state: QAState) -> Dict[str, Any]:
    def skipped(reason: str, logs: Optional[List[str]] = None) -> Dict[str, Any]:
        update: Dict[str, Any] = {"live_snapshot": {"available": False, "reason": reason}}
        if logs is not None:
            update["execution_logs"] = append_log(state, logs)
        return update

    if not SETTINGS.live_snapshot_enabled:
        return skipped("LIVE_SNAPSHOT_ENABLED=false")
    if ROUTER.resolve(state.get("llm_provider")) == "offline":
        return skipped("offline provider")
    if not (state.get("repo_context") or {}).get("available") or TEST_REPO.path is None:
        return skipped("test repository unavailable")
    if shutil.which("node") is None:
        return skipped("node is not on PATH")

    keywords = explore_keywords(state)
    config = {"storageState": "e2e/.auth/state.json", "entry": SETTINGS.live_explore_entry, "keywords": keywords,
              "budget": SETTINGS.live_explore_budget, "maxLines": 80}
    logs: List[str] = []
    with tempfile.TemporaryDirectory(prefix="taf-explore-") as tmp:
        config_path = Path(tmp) / "explore.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        with REPO_LOCK:
            result = run_command(["node", str(SCRIPT), str(config_path), state.get("target_url", "")],
                                 cwd=TEST_REPO.path, timeout_s=SETTINGS.live_snapshot_timeout_s)
    parsed = parse_output(result.stdout) if not result.timed_out else {"ok": False, "reason": "timed out"}
    captured = [s["name"] for s in parsed.get("states") or [] if s.get("ok")]
    failed = [f"{s['name']}: {s.get('error', '')}" for s in parsed.get("states") or [] if not s.get("ok")]
    if not parsed.get("ok"):
        logs.append(f"[warn] Live UI exploration skipped - {parsed.get('reason')}. The spec is written from the "
                    "page objects and page rules only.")
        return skipped(str(parsed.get("reason")), logs)
    logs.append(f"[info] Live UI exploration: {len(captured)} state(s) captured from {parsed.get('url', '')} "
                f"(keywords: {', '.join(keywords[:12]) or 'none'})"
                + (f"; not captured: {'; '.join(failed)}" if failed else ""))
    return {
        "live_snapshot": {"available": bool(captured), "url": parsed.get("url", ""), "entry": parsed.get("entry", ""),
                          "keywords": keywords, "captured": captured, "failed": failed,
                          "prompt": render_snapshot(parsed, SETTINGS.live_snapshot_max_chars)},
        "execution_logs": append_log(state, logs),
    }
