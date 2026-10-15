"""
Deterministic graders. Each returns a list of checks:

    {"name": str, "passed": bool, "hard": bool, "detail": str, "dimension": str}

Hard checks are requirements (the output is unusable without them); soft checks
are quality signals that are reported but never fail a run. Graders read only the
agents' output, never a model, so the same output always gets the same score.

`dimension` groups checks for comparison across runs:
    requirements    criteria extracted, key facts present, risk level
    traceability    criteria -> test cases -> [TC-xxx] tests
    scenarios       plan size, steps, specific expected results, behaviours covered
    duplicates      redundant cases or tests
    ambiguity       open questions raised for vague tickets
    selectors       user-facing locators, no raw CSS/XPath
    conventions     required / forbidden constructs, page objects and fixtures reused
    safety_scope    no Node APIs or dynamic code, nothing outside the ticket's scope
    typescript      compiles / lints / loads in the test repository (--with-repo)
    execution       the generated tests pass (--execute) - not proof of coverage
"""
from __future__ import annotations

import re
from itertools import combinations
from typing import Any, Dict, List

Check = Dict[str, Any]


def check(name: str, passed: bool, detail: str = "", hard: bool = True, dimension: str = "") -> Check:
    return {"name": name, "passed": bool(passed), "hard": hard, "detail": detail, "dimension": dimension}


def _missing(haystack: str, needles: List[str]) -> List[str]:
    low = haystack.lower()
    return [n for n in needles if n.lower() not in low]


def _tokens(text: str) -> set:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(t) > 2}


def _similar(a: str, b: str, threshold: float) -> bool:
    ta, tb = _tokens(a), _tokens(b)
    return bool(ta and tb) and len(ta & tb) / len(ta | tb) >= threshold


def grade_triage(triage: Dict[str, Any], expect: Dict[str, Any]) -> List[Check]:
    criteria = triage.get("acceptance_criteria") or []
    checks = [
        check("criteria count", len(criteria) >= expect.get("min_criteria", 1),
              f"{len(criteria)} found, need >= {expect.get('min_criteria', 1)}", dimension="requirements"),
    ]
    if "risk_level_in" in expect:
        checks.append(check("risk level", triage.get("risk_level") in expect["risk_level_in"],
                            f"{triage.get('risk_level')!r}, expected one of {expect['risk_level_in']}",
                            dimension="requirements"))
    if expect.get("must_mention"):
        text = " ".join(criteria + (triage.get("impacted_areas") or []) + (triage.get("risks") or []))
        missing = _missing(text, expect["must_mention"])
        checks.append(check("mentions key facts", not missing, f"missing: {missing}" if missing else "all present",
                            dimension="requirements"))
    questions = triage.get("gaps_and_questions") or []
    if expect.get("ambiguous"):
        need = expect.get("min_questions", 1)
        checks.append(check("raises open questions", len(questions) >= need,
                            f"{len(questions)} question(s), need >= {need}", dimension="ambiguity"))
        if expect.get("should_ask_about"):
            missing = _missing(" ".join(questions), expect["should_ask_about"])
            checks.append(check("asks about the gaps", not missing, f"not asked: {missing}" if missing else "",
                                hard=False, dimension="ambiguity"))
    dupes = [(a, b) for a, b in combinations(criteria, 2) if _similar(a, b, 0.8)]
    checks.append(check("no duplicate criteria", not dupes, f"{len(dupes)} near-duplicate pair(s)", hard=False,
                        dimension="duplicates"))
    checks.append(check("risks are specific", all(len(r) > 15 for r in triage.get("risks") or [""]),
                        "every risk is more than a few words", hard=False, dimension="requirements"))
    return checks


def grade_plan(cases: List[Dict[str, Any]], expect: Dict[str, Any], criteria: List[str] = None) -> List[Check]:
    criteria = criteria or []
    automatable = [c for c in cases if c.get("automatable")]
    ids = [c.get("test_id", "") for c in cases]
    expected_ids = [f"TC-{n:03d}" for n in range(1, len(cases) + 1)]
    checks = [
        check("case count", expect.get("min_cases", 1) <= len(cases) <= expect.get("max_cases", 50),
              f"{len(cases)} cases, allowed {expect.get('min_cases', 1)}-{expect.get('max_cases', 50)}",
              dimension="scenarios"),
        check("automatable count", len(automatable) >= expect.get("min_automatable", 1),
              f"{len(automatable)} automatable, need >= {expect.get('min_automatable', 1)}", dimension="scenarios"),
        check("sequential ids", ids == expected_ids, f"{ids[:5]}..." if ids != expected_ids else "TC-001...",
              dimension="traceability"),
        check("every case has steps", all(c.get("steps") for c in cases), "", dimension="scenarios"),
    ]
    vague = [c.get("test_id") for c in cases for s in c.get("steps") or []
             if re.search(r"\bworks? (correctly|properly|as expected)\b", s.get("expected_result", ""), re.I)]
    checks.append(check("no vague expected results", not vague, f"vague in {sorted(set(vague))}" if vague else "",
                        hard=False, dimension="scenarios"))
    if expect.get("must_cover"):
        text = " ".join(f"{c.get('summary', '')} " + " ".join(s.get("expected_result", "") for s in c.get("steps") or [])
                        for c in cases)
        missing = _missing(text, expect["must_cover"])
        checks.append(check("covers key behaviours", not missing, f"missing: {missing}" if missing else "all covered",
                            dimension="scenarios"))

    # Traceability: every case cites a criterion, and every criterion is cited.
    refs = [r.upper() for c in cases for r in c.get("acceptance_criteria_refs") or []]
    uncited = [c.get("test_id") for c in cases if not c.get("acceptance_criteria_refs")]
    checks.append(check("every case cites a criterion", not uncited, f"no refs: {uncited}" if uncited else "",
                        hard=expect.get("require_traceability", True), dimension="traceability"))
    if criteria:
        uncovered = [f"AC-{n}" for n in range(1, len(criteria) + 1)
                     if f"AC-{n}" not in refs and not any(criteria[n - 1][:40].upper() in r for r in refs)]
        checks.append(check("every criterion is tested", not uncovered,
                            f"criteria without a case: {uncovered}" if uncovered else f"{len(criteria)} covered",
                            hard=False, dimension="traceability"))

    texts = [f"{c.get('summary', '')} " + " ".join(s.get("action", "") + " " + s.get("expected_result", "")
                                                   for s in c.get("steps") or []) for c in cases]
    dupes = [(cases[i].get("test_id"), cases[j].get("test_id"))
             for i, j in combinations(range(len(cases)), 2) if _similar(texts[i], texts[j], 0.85)]
    checks.append(check("no redundant cases", not dupes, f"near-duplicates: {dupes[:5]}" if dupes else "",
                        hard=False, dimension="duplicates"))
    return checks


_LOCATOR_RAW = re.compile(r"\.locator\(\s*['\"`](?![a-z]+=)([^'\"`]+)['\"`]")
_LOCATOR_SEMANTIC = re.compile(r"\.getBy(Role|Label|Text|TestId|Placeholder|AltText|Title)\(")


def grade_spec(code: str, expect: Dict[str, Any], cases: List[Dict[str, Any]] = None) -> List[Check]:
    from taf.steps.validation import code_checks

    cases = cases or []
    tests = re.findall(r"\btest\(\s*['\"`]\[TC-\d+\]", code)
    titles = re.findall(r"\btest\(\s*['\"`](.+?)['\"`]", code)
    fixme = len(re.findall(r"\btest\.fixme\(", code))
    checks = [
        check("spec produced", bool(code.strip()), f"{len(code):,} chars", dimension="conventions"),
        check("test count", len(tests) >= expect.get("min_tests", 1),
              f"{len(tests)} [TC-xxx] tests, need >= {expect.get('min_tests', 1)}", dimension="scenarios"),
    ]
    for c in code_checks(code, "e2e/qa/x.spec.ts", None) if code.strip() else []:
        checks.append(check(c["name"], c["ok"], c["detail"], dimension="safety_scope"))
    for needle in expect.get("require", []):
        checks.append(check(f"contains {needle!r}", needle in code, dimension="conventions"))
    for needle in expect.get("forbid", []):
        checks.append(check(f"avoids {needle!r}", needle not in code, dimension="conventions"))
    for needle in expect.get("prefer", []):
        checks.append(check(f"uses {needle!r}", needle in code, hard=False, dimension="conventions"))
    for needle in expect.get("out_of_scope", []):
        checks.append(check(f"stays out of {needle!r}", needle.lower() not in code.lower(), hard=False,
                            dimension="safety_scope"))

    planned = {c.get("test_id") for c in cases if c.get("automatable")}
    if planned:
        missing = sorted(planned - set(re.findall(r"\[(TC-\d+)\]", code)))
        checks.append(check("every automatable case has a test", not missing, f"missing: {missing}" if missing else "",
                            hard=False, dimension="traceability"))
    dupes = sorted({t for t in titles if titles.count(t) > 1})
    checks.append(check("no duplicate tests", not dupes, f"duplicate titles: {dupes[:5]}" if dupes else "",
                        hard=False, dimension="duplicates"))
    raw, semantic = len(_LOCATOR_RAW.findall(code)), len(_LOCATOR_SEMANTIC.findall(code))
    checks.append(check("user-facing locators", raw <= semantic, f"{semantic} getBy* vs {raw} CSS/XPath",
                        hard=False, dimension="selectors"))
    checks.append(check("real tests, not placeholders", fixme <= max(1, len(tests) // 4),
                        f"{fixme} test.fixme of {len(tests)}", hard=False, dimension="conventions"))
    for module in expect.get("reuse", []):
        checks.append(check(f"reuses {module!r}", module in code, hard=False, dimension="conventions"))
    return checks


def grade_repo_checks(results: List[Dict[str, Any]]) -> List[Check]:
    """Map taf.steps.validation repo checks (playwright --list, tsc, eslint) onto eval checks."""
    return [check(r["name"], r["ok"] or r.get("skipped", False),
                  ("skipped - " if r.get("skipped") else "") + (r.get("detail") or "")[:400], dimension="typescript")
            for r in results]


def score(checks: List[Check]) -> Dict[str, Any]:
    hard = [c for c in checks if c["hard"]]
    soft = [c for c in checks if not c["hard"]]
    dims: Dict[str, List[int]] = {}
    for c in checks:
        if c.get("dimension"):
            got = dims.setdefault(c["dimension"], [0, 0])
            got[0] += int(c["passed"])
            got[1] += 1
    return {
        "passed": all(c["passed"] for c in hard),
        "hard": f"{sum(c['passed'] for c in hard)}/{len(hard)}",
        "soft": f"{sum(c['passed'] for c in soft)}/{len(soft)}",
        "dimensions": {k: f"{v[0]}/{v[1]}" for k, v in sorted(dims.items())},
    }
