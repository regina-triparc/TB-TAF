"""
What the test repository already covers - looked up before the plan is designed, so
the Test Architect can update or reuse a test instead of duplicating it.

    build_index        every *.spec.ts: test titles, tags, Xray keys, page objects and
                       fixtures used, API endpoints called, feature folder. Deterministic,
                       cached per test-repo commit (not cached while specs have local edits).
    rank_candidates    the existing tests that match the ticket: its key and linked bugs,
                       triage's impacted areas, the components and endpoints the diff
                       touches. Scored in Python - no model searches the suite.
    coverage_lookup    the graph node (read-only, under the repo lock).
    plan_gate          after the plan: pauses when its confidence is low, a bugfix has no
                       existing test to update, or a case updates a spec that does not
                       exist - with PLAN_GATE on. Off: the reasons are only reported.

`python -m taf coverage report` lists existing tests without an Xray key tag
({ tag: '@TB-1234' }), the traceability backfill.
"""
from __future__ import annotations

import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from langgraph.types import interrupt

from taf.agents.base import agent_node
from taf.agents.state import QAState
from taf.config import SETTINGS
from taf.core.text import utc_now
from taf.integrations.github.repository import REPO_LOCK, TEST_REPO, run_command
from taf.skills import diff_paths

INDEX_VERSION = 2  # bump when the index format changes: old cache entries are not reused
_SKIP_DIRS = {"node_modules", ".git", "test-results", "playwright-report", "test-artifacts", "blob-report", ".auth"}
_TITLE_RE = re.compile(r"\btest(?:\.(?:only|skip|fixme|fail))?\(\s*(['\"`])(.+?)\1")
_DESCRIBE_RE = re.compile(r"\btest\.describe(?:\.\w+)?\(\s*(['\"`])(.+?)\1")
_TAG_RE = re.compile(r"['\"`](@[A-Za-z][\w.-]*)['\"`]")
_KEY_RE = re.compile(r"\b(?!TC-)[A-Z][A-Z0-9]+-\d+\b")
_IMPORT_RE = re.compile(r"""import\s+(?:type\s+)?(?:[\w*{}\s,]+?\s+from\s+)?['"]([^'"]+)['"]""")
_ENDPOINT_RE = re.compile(r"['\"`](?:\*\*)?(/api/[^'\"`\s?]*)")
_WORD_RE = re.compile(r"[A-Za-z][a-z0-9]+|[A-Z]+(?![a-z])")
_STOP = {"the", "and", "for", "with", "from", "page", "test", "tests", "spec", "component", "service", "index",
         "module", "html", "scss", "src", "app", "apps", "lib", "libs", "should", "when", "can", "are", "not",
         "new", "user", "into", "that", "this", "has", "have", "can't", "api", "controller", "dto", "e2e"}


# ------------------------------------------------------------------ index --
def _tokens(text: str) -> Set[str]:
    words = {w.lower() for w in _WORD_RE.findall(text or "")}
    return {w for w in words if len(w) > 2 and w not in _STOP}


def index_spec(rel: str, code: str) -> Dict[str, Any]:
    imports = _IMPORT_RE.findall(code)
    matches = list(_TITLE_RE.finditer(code))
    titles = [m.group(2) for m in matches]
    # A test's own keys: in its title, or in the tags between its title and its body.
    test_keys: Dict[str, List[str]] = {}
    for m in matches:
        head = code[m.end():m.end() + 300].split("=>", 1)[0]
        test_keys[m.group(2)] = sorted(set(_KEY_RE.findall(" ".join([m.group(2), *_TAG_RE.findall(head)]))))
    tags = sorted(set(_TAG_RE.findall(code)))
    keys = sorted(set(_KEY_RE.findall(" ".join(titles + tags + [Path(rel).stem]))))
    return {
        "path": rel,
        "folder": str(Path(rel).parent.as_posix()),
        "describes": [m.group(2) for m in _DESCRIBE_RE.finditer(code)],
        "titles": titles,
        "test_keys": test_keys,
        "file_keys": sorted(set(_KEY_RE.findall(Path(rel).stem))),
        "tags": tags,
        "xray_keys": keys,
        "page_objects": sorted({i.rsplit("/", 1)[-1] for i in imports if "page" in i.lower()}),
        "fixtures": sorted({i.rsplit("/", 1)[-1] for i in imports if "fixture" in i.lower()}),
        "endpoints": sorted(set(_ENDPOINT_RE.findall(code))),
    }


def _spec_files(root: Path) -> List[Path]:
    found = []
    for path in sorted(root.rglob("*.spec.ts")):
        if not any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            found.append(path)
    return found


def _cache_key(root: Path) -> Optional[str]:
    """The test repo's commit, or None when specs have local edits (or there is no git)."""
    head = run_command(["git", "rev-parse", "HEAD"], cwd=root, timeout_s=30)
    if not head.ok:
        return None
    dirty = run_command(["git", "status", "--porcelain", "--", "*.spec.ts"], cwd=root, timeout_s=30)
    if not dirty.ok or dirty.stdout.strip():
        return None
    return hashlib.sha256(f"{INDEX_VERSION}|{root.resolve()}|{head.stdout.strip()}".encode()).hexdigest()[:16]


def _cache_path(key: str) -> Path:
    return Path(tempfile.gettempdir()) / "taf-coverage" / f"{key}.json"


def build_index(root: Path) -> Dict[str, Any]:
    """Every spec in the repository, indexed (from the cache when the commit has been indexed before)."""
    key = _cache_key(root)
    if key:
        try:
            cached = json.loads(_cache_path(key).read_text(encoding="utf-8"))
            return {**cached, "cached": True}
        except (OSError, ValueError):
            pass
    specs = []
    for path in _spec_files(root):
        try:
            specs.append(index_spec(path.relative_to(root).as_posix(), path.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError):
            continue
    index = {"available": True, "key": key or "", "specs": specs, "built_at": utc_now()}
    if key:
        try:
            _cache_path(key).parent.mkdir(parents=True, exist_ok=True)
            _cache_path(key).write_text(json.dumps(index), encoding="utf-8")
        except OSError:
            pass
    return {**index, "cached": False}


def untagged(index: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Specs with tests but no Xray key in a tag, title or file name."""
    return [{"path": s["path"], "tests": len(s["titles"])} for s in index.get("specs") or []
            if s["titles"] and not s["xray_keys"]]


# ---------------------------------------------------------------- ranking --
def query_terms(state: Dict[str, Any]) -> Dict[str, Any]:
    triage = state.get("triage_analysis") or {}
    keys = {state.get("ticket_id") or ""} | {i.get("key") for i in state.get("linked_issues") or [] if i.get("key")}
    words: Set[str] = set()
    for area in triage.get("impacted_areas") or []:
        words |= _tokens(area)
    for path in diff_paths(state):
        words |= _tokens(Path(path).stem.replace(".", " "))
    endpoints = set(_ENDPOINT_RE.findall(state.get("github_diff") or ""))
    return {"keys": {k for k in keys if k}, "words": words, "endpoints": endpoints}


def rank_candidates(index: Dict[str, Any], state: Dict[str, Any], limit: int = 0) -> List[Dict[str, Any]]:
    """Existing tests ranked by how well they match the ticket: [{spec, test, xray_key, score, reason}]."""
    terms = query_terms(state)
    limit = limit or SETTINGS.coverage_max_candidates
    out: List[Dict[str, Any]] = []
    for spec in index.get("specs") or []:
        spec_words = _tokens(" ".join([spec["path"], *spec["describes"], *spec["page_objects"], *spec["fixtures"]]))
        endpoint_hits = sorted({e for e in terms["endpoints"] if any(e.startswith(x) or x.startswith(e)
                                                                       for x in spec["endpoints"])})
        file_keys = set(spec.get("file_keys") or [])
        own_keys = spec.get("test_keys") or {}
        for title in spec["titles"] or [""]:
            words = spec_words | _tokens(title)
            shared = sorted(terms["words"] & words)
            test_keys = set(own_keys.get(title, _KEY_RE.findall(title)))
            key_hits = sorted(terms["keys"] & (test_keys | file_keys))
            score = 0.0
            reasons = []
            if key_hits:
                score += 0.6
                reasons.append(f"tagged {', '.join(key_hits)}")
            if endpoint_hits:
                score += 0.3
                reasons.append(f"calls {', '.join(endpoint_hits[:3])}")
            if shared and terms["words"]:
                score += 0.4 * min(1.0, len(shared) / max(3, min(len(terms["words"]), 8)))
                reasons.append(f"shares {', '.join(shared[:6])}")
            if score <= 0:
                continue
            keys = sorted(test_keys | file_keys)
            out.append({"spec": spec["path"], "test": title, "xray_key": keys[0] if len(keys) == 1 else "",
                        "score": round(min(score, 1.0), 2), "reason": "; ".join(reasons)})
    out.sort(key=lambda c: (-c["score"], c["spec"], c["test"]))
    return out[:limit]


def candidates_block(state: Dict[str, Any]) -> str:
    """Existing tests for the architect prompt; empty when there are none."""
    candidates = state.get("coverage_candidates") or []
    if not candidates:
        return ""
    classification = state.get("classification") or {}
    lines = ["", "", "## Existing tests that may already cover this",
             f"Ticket classified as action `{classification.get('action', 'create')}`, layer "
             f"`{classification.get('layer', 'ui')}`. For each case set `disposition`: `update` an existing test "
             "below (give target_spec, target_test and xray_key), `reuse` one that already covers it, or `new`."]
    lines += [f"- `{c['spec']}` - \"{c['test']}\"" + (f" [{c['xray_key']}]" if c["xray_key"] else "")
              + f" (score {c['score']}: {c['reason']})" for c in candidates]
    return "\n".join(lines)


@agent_node("Coverage Lookup", engine="rules", abort_on_error=False, span_type="retriever")
def coverage_lookup(state: QAState) -> Dict[str, Any]:
    """Read-only: index the test repository's specs and rank those that match the ticket."""
    if TEST_REPO.path is None or not TEST_REPO.path.is_dir():
        return {"coverage": {"available": False, "reason": "no test repository (TEST_REPO_LOCAL_PATH)"},
                "coverage_candidates": []}
    with REPO_LOCK:
        index = build_index(TEST_REPO.path.resolve())
    candidates = rank_candidates(index, state)
    return {"coverage": {"available": True, "specs": len(index["specs"]), "tests": sum(len(s["titles"])
                                                                                        for s in index["specs"]),
                         "cached": index.get("cached", False), "key": index.get("key", "")},
            "coverage_candidates": candidates}


# ------------------------------------------------------------- plan gate --
PLAN_DONE = {"passed", "not_required", "continued", "rejected"}


def plan_reasons(state: Dict[str, Any]) -> List[str]:
    reasons = []
    confidence = float(state.get("plan_confidence", 1.0) or 1.0)
    if confidence < SETTINGS.plan_confidence_threshold:
        reasons.append(f"plan confidence {confidence:.2f} < PLAN_CONFIDENCE_THRESHOLD "
                       f"{SETTINGS.plan_confidence_threshold:.2f}")
    candidates = state.get("coverage_candidates") or []
    if state.get("action") == "update" and not any(c["score"] >= SETTINGS.coverage_match_threshold for c in candidates):
        reasons.append(f"action is update but no existing test scores >= COVERAGE_MATCH_THRESHOLD "
                       f"{SETTINGS.coverage_match_threshold}")
    known = {c["spec"] for c in candidates}
    missing = [c["test_id"] for c in state.get("test_cases") or []
               if c.get("disposition") in {"update", "reuse"} and c.get("target_spec") not in known]
    if missing:
        reasons.append(f"{', '.join(missing)} update or reuse a spec the lookup did not find")
    return reasons


def plan_request(state: Dict[str, Any]) -> Dict[str, Any]:
    return {"kind": "plan", "ticket_id": state["ticket_id"], "classification": state.get("classification") or {},
            "candidates": state.get("coverage_candidates") or [],
            "cases": [{k: c.get(k) for k in ("test_id", "summary", "disposition", "target_spec", "target_test",
                                             "xray_key")} for c in state.get("test_cases") or []],
            "reasons": plan_reasons(state)}


def validate_plan_decision(decision: Any) -> Dict[str, Any]:
    """{"action": "continue"|"reject", "override": {"action", "layer"}, "dispositions": {TC-001: {...}}}."""
    if not isinstance(decision, dict):
        raise ValueError("a plan decision is an object with an 'action'")
    action = str(decision.get("action") or ("continue" if decision.get("approved") else
                                            "reject" if decision.get("approved") is False else "")).lower()
    if action not in {"continue", "reject"}:
        raise ValueError("action must be continue or reject")
    override = {k: str(v).lower() for k, v in (decision.get("override") or {}).items() if v}
    from taf.steps.classify import ACTIONS, LAYERS
    if override.get("action") and override["action"] not in ACTIONS:
        raise ValueError(f"override.action must be one of {', '.join(ACTIONS)}")
    if override.get("layer") and override["layer"] not in LAYERS:
        raise ValueError(f"override.layer must be one of {', '.join(LAYERS)}")
    dispositions = decision.get("dispositions") or {}
    if not isinstance(dispositions, dict):
        raise ValueError("dispositions is an object {\"TC-001\": {\"disposition\": \"update\", ...}}")
    for tc, d in dispositions.items():
        if not isinstance(d, dict) or d.get("disposition", "new") not in {"new", "update", "reuse"}:
            raise ValueError(f"{tc}: disposition must be new, update or reuse")
    return {"action": action, "override": {k: override[k] for k in ("action", "layer") if k in override},
            "dispositions": dispositions, "by": str(decision.get("by") or "unknown")[:100],
            "note": str(decision.get("note") or "")[:500]}


@agent_node("Plan Gate", engine="human", abort_on_error=False, span_type="guardrail")
def plan_gate(state: QAState) -> Dict[str, Any]:
    if (state.get("plan_review") or {}).get("status") in PLAN_DONE:
        return {}
    request = plan_request(state)
    record: Dict[str, Any] = {"reasons": request["reasons"], "at": utc_now()}
    if not request["reasons"]:
        return {"plan_review": {**record, "status": "passed"}}
    if not SETTINGS.plan_gate:
        return {"plan_review": {**record, "status": "not_required",
                                "note": "PLAN_GATE=false - " + "; ".join(request["reasons"]) + ", not asked"}}
    decision = validate_plan_decision(interrupt(request))
    record.update({"by": decision["by"], "note": decision["note"]})
    if decision["action"] == "reject":
        return {"plan_review": {**record, "status": "rejected"}}
    update: Dict[str, Any] = {"plan_review": {**record, "status": "continued", "overrides": decision["override"],
                                              "dispositions": decision["dispositions"]}}
    if decision["override"]:
        classification = {**(state.get("classification") or {}), **decision["override"],
                          "overridden_by": decision["by"]}
        update.update({"classification": classification, **decision["override"]})
    if decision["dispositions"]:
        cases = []
        for case in state.get("test_cases") or []:
            change = decision["dispositions"].get(case.get("test_id")) or {}
            cases.append({**case, **{k: str(v) for k, v in change.items()
                                     if k in {"disposition", "target_spec", "target_test", "xray_key"}}})
        update["test_cases"] = cases
    return update


# ------------------------------------------------------- regression_only --
@agent_node("Regression Runner", engine="local-cli", abort_on_error=False, span_type="tool")
def run_existing(state: QAState) -> Dict[str, Any]:
    """action regression_only: run the existing specs that matched, generate nothing (ROUTE_BY_CLASSIFICATION)."""
    from taf.llm.router import ROUTER
    from taf.steps.context import append_log
    from taf.steps.playwright_results import counts, parse_json_report, report_path

    if ROUTER.resolve(state.get("llm_provider")) == "offline":
        return {"regression_run": {"skipped": "offline provider - nothing is run"}, "execution_passed": False}
    specs = sorted({c["spec"] for c in state.get("coverage_candidates") or []
                    if c["score"] >= SETTINGS.coverage_match_threshold})
    if not specs or TEST_REPO.path is None:
        return {"regression_run": {"skipped": "no existing spec matched the ticket", "specs": []},
                "execution_passed": False}
    logs: List[str] = []
    tests: List[Dict[str, Any]] = []
    passed = True
    with REPO_LOCK:
        for n, spec in enumerate(specs, start=1):
            report = report_path(state["run_id"], f"regression-{n}")
            passed = TEST_REPO.run_playwright(TEST_REPO.path / spec, state.get("target_url", ""), logs,
                                              json_report=report) and passed
            tests += [{**t, "spec": spec} for t in parse_json_report(report)]
    run_counts = counts(tests)
    passed = passed and bool(run_counts.get("passed"))
    return {"regression_run": {"specs": specs, "passed": passed, "counts": run_counts},
            "playwright": {"passed": passed, "counts": run_counts, "tests": tests},
            "execution_passed": passed, "execution_logs": append_log(state, logs)}
