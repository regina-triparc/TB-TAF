"""
What kind of work a ticket needs: an action and a layer.

    action   create | update | extend | regression_only | skip
    layer    api | ui | both | none

classify_ticket runs before triage and reads deterministic signals only:
    * the Jira issue type (Bug -> bugfix), labels, linked bugs,
    * PR titles (`fix:` / `feat:` prefixes),
    * diff paths: CLASSIFY_API_GLOBS (controllers, DTOs, handlers, validators) -> api;
      code under DIFF_FOCUS_PATHS -> ui; only CLASSIFY_NONE_GLOBS (config, CI, docs,
      tests) -> none.
The signals go into the triage prompt, so the existing triage call confirms or
corrects them without an extra model call. `reconcile` merges the two: triage wins
where it answered, and when it disagrees with the signals on the layer or the change
type its confidence is capped at 0.5 - the clarify gate then pauses (when on).

Unknown stays today's path: layer ui, action create. The classification is always
recorded; it changes the route only with ROUTE_BY_CLASSIFICATION=true (taf/agents/graph.py).
"""
from __future__ import annotations

import fnmatch
import re
from typing import Any, Dict, List, Optional

from taf.agents.base import agent_node
from taf.agents.state import QAState
from taf.config import SETTINGS
from taf.skills import diff_paths

ACTIONS = ("create", "update", "extend", "regression_only", "skip")
LAYERS = ("api", "ui", "both", "none")
CHANGE_TYPES = ("feature", "bugfix", "refactor", "chore")
DISAGREEMENT_CONFIDENCE = 0.5

_BUG_LABELS = {"bug", "bugfix", "defect", "hotfix", "regression"}
_FIX_TITLE_RE = re.compile(r"^\s*(?:fix|hotfix|bugfix)(?:\([^)]*\))?!?:", re.IGNORECASE)
_FEAT_TITLE_RE = re.compile(r"^\s*feat(?:\([^)]*\))?!?:", re.IGNORECASE)
_CHORE_TITLE_RE = re.compile(r"^\s*(?:chore|docs|ci|build|test|style)(?:\([^)]*\))?!?:", re.IGNORECASE)
_REFACTOR_TITLE_RE = re.compile(r"^\s*refactor(?:\([^)]*\))?!?:", re.IGNORECASE)


def _globs(raw: str) -> List[str]:
    return [g.strip() for g in (raw or "").split(",") if g.strip()]


def _match(path: str, globs: List[str]) -> bool:
    name = path.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(path, g) or fnmatch.fnmatch(name, g) for g in globs)


def path_kinds(paths: List[str]) -> Dict[str, List[str]]:
    """Diff paths sorted into api / ui / none / other."""
    api, none, focus = _globs(SETTINGS.classify_api_globs), _globs(SETTINGS.classify_none_globs), \
        _globs(SETTINGS.diff_focus_paths)
    kinds: Dict[str, List[str]] = {"api": [], "ui": [], "none": [], "other": []}
    for path in paths:
        if _match(path, none):
            kinds["none"].append(path)
        elif _match(path, api):
            kinds["api"].append(path)
        elif any(path.startswith(f.rstrip("*")) for f in focus) or path.endswith((".html", ".scss", ".css")):
            kinds["ui"].append(path)
        else:
            kinds["other"].append(path)
    return kinds


def signals(state: Dict[str, Any]) -> Dict[str, Any]:
    """Deterministic classification signals. `layer` / `change_type` are None when nothing says."""
    reasons: List[str] = []
    issue_type = str(state.get("issue_type") or "").strip()
    labels = [str(label).lower() for label in state.get("labels") or []]
    linked_bugs = [i.get("key") for i in state.get("linked_issues") or []
                   if str(i.get("type") or "").lower() == "bug" and i.get("key")]
    titles = [str(p.get("title") or "") for p in state.get("pull_requests") or [] if p.get("title")]

    change_type: Optional[str] = None
    if issue_type.lower() == "bug":
        change_type = "bugfix"
        reasons.append("Jira issue type is Bug")
    elif _BUG_LABELS & set(labels):
        change_type = "bugfix"
        reasons.append(f"label {sorted(_BUG_LABELS & set(labels))[0]!r}")
    elif any(_FIX_TITLE_RE.match(t) for t in titles):
        change_type = "bugfix"
        reasons.append("a PR title starts with fix:")
    elif any(_FEAT_TITLE_RE.match(t) for t in titles):
        change_type = "feature"
        reasons.append("a PR title starts with feat:")
    elif any(_REFACTOR_TITLE_RE.match(t) for t in titles):
        change_type = "refactor"
        reasons.append("a PR title starts with refactor:")
    elif titles and all(_CHORE_TITLE_RE.match(t) for t in titles):
        change_type = "chore"
        reasons.append("every PR title is chore / docs / ci / test")
    elif issue_type.lower() in {"story", "feature", "new feature", "epic", "improvement"}:
        change_type = "feature"
        reasons.append(f"Jira issue type is {issue_type}")
    if linked_bugs and change_type != "bugfix":
        reasons.append(f"linked bug(s) {', '.join(linked_bugs[:5])}")

    paths = diff_paths(state)
    kinds = path_kinds(paths)
    layer: Optional[str] = None
    if paths:
        code = kinds["api"] or kinds["ui"] or kinds["other"]
        if kinds["api"] and kinds["ui"]:
            layer = "both"
        elif kinds["api"]:
            layer = "api"
        elif kinds["ui"]:
            layer = "ui"
        elif not code:
            layer = "none"
        if layer:
            counts = ", ".join(f"{len(v)} {k}" for k, v in kinds.items() if v)
            reasons.append(f"diff paths: {counts}")
        if layer == "none" and change_type is None:
            change_type = "chore"

    action: Optional[str] = None
    if layer == "none":
        action = "skip"
        reasons.append("only config, CI, docs or tests changed - nothing new to test")
    elif change_type == "bugfix":
        action = "update"
        reasons.append("a bugfix updates the test that should have caught it")
    return {"change_type": change_type, "layer": layer, "action": action, "issue_type": issue_type,
            "labels": labels, "linked_bugs": linked_bugs, "pr_titles": titles, "paths": kinds,
            "reasons": reasons}


def signals_prompt(state: Dict[str, Any]) -> str:
    """The signals as a triage prompt section: confirm or correct them."""
    s = state.get("classification_signals") or {}
    if not s:
        return ""
    found = {k: s.get(k) for k in ("change_type", "layer", "action")}
    return ("\n\n## Classification signals (deterministic - confirm or correct)\n"
            f"{found} - because: {'; '.join(s.get('reasons') or ['no signal'])}.\n"
            "Fill change_type, action, layer and classification_rationale. None above means no signal: decide "
            "from the ticket and the diff.")


def _pick(value: Any, allowed: tuple) -> Optional[str]:
    value = str(value or "").strip().lower()
    return value if value in allowed else None


def reconcile(state: Dict[str, Any], triage: Dict[str, Any]) -> Dict[str, Any]:
    """The run's classification from the signals and triage's answer; caps triage confidence on disagreement."""
    s = state.get("classification_signals") or {}
    said = {"change_type": _pick(triage.get("change_type"), CHANGE_TYPES),
            "action": _pick(triage.get("action"), ACTIONS), "layer": _pick(triage.get("layer"), LAYERS)}
    disagreements = [k for k in ("layer", "change_type") if s.get(k) and said[k] and s[k] != said[k]]
    chosen = {k: said[k] or s.get(k) for k in said}
    sources = {k: "triage" if said[k] else "signals" if s.get(k) else "default" for k in said}
    chosen["layer"] = chosen["layer"] or "ui"           # unknown: today's path
    chosen["action"] = chosen["action"] or "create"
    chosen["change_type"] = chosen["change_type"] or "feature"
    rationale = str(triage.get("classification_rationale") or "").strip() or "; ".join(s.get("reasons") or [])
    record = {**chosen, "sources": sources, "rationale": rationale or "no signal - today's default path",
              "signals": {k: s.get(k) for k in ("change_type", "layer", "action")},
              "disagreements": disagreements}
    if disagreements:
        record["note"] = (f"triage and the signals disagree on {', '.join(disagreements)} - triage confidence "
                          f"capped at {DISAGREEMENT_CONFIDENCE}")
    return record


def capped_confidence(triage: Dict[str, Any], record: Dict[str, Any]) -> float:
    try:
        confidence = float(triage.get("confidence", 1.0))
    except (TypeError, ValueError):
        confidence = 1.0
    return min(confidence, DISAGREEMENT_CONFIDENCE) if record.get("disagreements") else confidence


@agent_node("Classifier", engine="rules", abort_on_error=False, span_type="guardrail")
def classify_ticket(state: QAState) -> Dict[str, Any]:
    return {"classification_signals": signals(state)}
