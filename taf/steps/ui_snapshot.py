"""
Live UI snapshot - what the app under test looks like right now, captured before
the Automation Engineer writes a spec.

The page contract is written by hand and goes stale; a model that has not seen the
page fills the gaps with guesses (a "6:30 PM" option that does not exist, a tab
locator that also matches the trip's own tabs). This step opens the app with the
test repository's saved session, walks the states listed in a recipe
(`<page contract>.snapshot.json`), and records each one's accessibility tree.

Read-only by construction (see ui_snapshot.cjs): write requests are aborted and
submit-like clicks refused. A missing recipe, a missing or expired session, or any
failure only skips the step - the spec is then written from the page contract alone.
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

from taf.agents.base import agent_node
from taf.agents.state import QAState
from taf.config import SETTINGS, project_path
from taf.core.text import truncate_middle
from taf.integrations.github.repository import REPO_LOCK, TEST_REPO, run_command
from taf.llm.router import ROUTER
from taf.steps.context import append_log

SCRIPT = Path(__file__).with_name("ui_snapshot.cjs")


def recipe_path() -> Optional[Path]:
    """LIVE_SNAPSHOT_RECIPE, else the page contract's `<name>.snapshot.json`; None when neither exists."""
    if SETTINGS.live_snapshot_recipe:
        path = project_path(SETTINGS.live_snapshot_recipe)
    elif SETTINGS.page_contract_path:
        path = project_path(SETTINGS.page_contract_path).with_suffix(".snapshot.json")
    else:
        return None
    return path if path.is_file() else None


def parse_output(stdout: str) -> Dict[str, Any]:
    """The script prints one JSON line last; anything before it (global setup noise) is ignored."""
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    return {"ok": False, "reason": "the snapshot script printed no result"}


def render_snapshot(result: Dict[str, Any], limit: int) -> str:
    """The captured states as a prompt section, or "" when nothing usable was captured."""
    states = [s for s in result.get("states") or [] if s.get("ok") and s.get("snapshot")]
    if not result.get("ok") or not states:
        return ""
    lines = ["## Live page snapshot (captured from the running app for this run - authoritative)",
             "Each block is the accessibility tree of one state, from `ariaSnapshot()`. Use these roles, "
             "names, option values and formats exactly. Where it disagrees with the page contract, this wins.",
             f"Start URL: {result.get('url', '')}"]
    for state in states:
        lines += ["", f"### {state['name']}", "```yaml", state["snapshot"], "```"]
    failed = [s["name"] for s in result.get("states") or [] if not s.get("ok")]
    if failed:
        lines += ["", f"(Not captured: {', '.join(failed)} - fall back to the page contract for these.)"]
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
    recipe = recipe_path()
    if recipe is None:
        return skipped("no snapshot recipe (set LIVE_SNAPSHOT_RECIPE or add <page contract>.snapshot.json)")
    if shutil.which("node") is None:
        return skipped("node is not on PATH")

    logs: List[str] = []
    with REPO_LOCK:
        result = run_command(["node", str(SCRIPT), str(recipe), state.get("target_url", "")],
                             cwd=TEST_REPO.path, timeout_s=SETTINGS.live_snapshot_timeout_s)
    parsed = parse_output(result.stdout) if not result.timed_out else {"ok": False, "reason": "timed out"}
    captured = [s["name"] for s in parsed.get("states") or [] if s.get("ok")]
    failed = [f"{s['name']}: {s.get('error', '')}" for s in parsed.get("states") or [] if not s.get("ok")]
    if not parsed.get("ok"):
        logs.append(f"[warn] Live UI snapshot skipped - {parsed.get('reason')}. The spec is written from the "
                    "page contract only.")
        return skipped(str(parsed.get("reason")), logs)
    logs.append(f"[info] Live UI snapshot: {len(captured)} state(s) captured from {parsed.get('url', '')}"
                + (f"; not captured: {'; '.join(failed)}" if failed else ""))
    return {
        "live_snapshot": {"available": bool(captured), "url": parsed.get("url", ""), "captured": captured,
                          "failed": failed, "prompt": render_snapshot(parsed, SETTINGS.live_snapshot_max_chars)},
        "execution_logs": append_log(state, logs),
    }
