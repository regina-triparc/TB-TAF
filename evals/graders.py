"""
Deterministic graders. Each returns a list of checks:

    {"name": str, "passed": bool, "hard": bool, "detail": str}

Hard checks are requirements (the output is unusable without them); soft checks
are quality signals that are reported but never fail a run. Graders read only the
agents' output, never a model, so the same output always gets the same score.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

Check = Dict[str, Any]


def check(name: str, passed: bool, detail: str = "", hard: bool = True) -> Check:
    return {"name": name, "passed": bool(passed), "hard": hard, "detail": detail}


def _missing(haystack: str, needles: List[str]) -> List[str]:
    low = haystack.lower()
    return [n for n in needles if n.lower() not in low]


def grade_triage(triage: Dict[str, Any], expect: Dict[str, Any]) -> List[Check]:
    criteria = triage.get("acceptance_criteria") or []
    checks = [
        check("criteria count", len(criteria) >= expect.get("min_criteria", 1),
               f"{len(criteria)} found, need >= {expect.get('min_criteria', 1)}"),
    ]
    if "risk_level_in" in expect:
        checks.append(check("risk level", triage.get("risk_level") in expect["risk_level_in"],
                             f"{triage.get('risk_level')!r}, expected one of {expect['risk_level_in']}"))
    if expect.get("must_mention"):
        text = " ".join(criteria + (triage.get("impacted_areas") or []) + (triage.get("risks") or []))
        missing = _missing(text, expect["must_mention"])
        checks.append(check("mentions key facts", not missing, f"missing: {missing}" if missing else "all present"))
    checks.append(check("risks are specific", all(len(r) > 15 for r in triage.get("risks") or [""]),
                         "every risk is more than a few words", hard=False))
    return checks


def grade_plan(cases: List[Dict[str, Any]], expect: Dict[str, Any]) -> List[Check]:
    automatable = [c for c in cases if c.get("automatable")]
    ids = [c.get("test_id", "") for c in cases]
    expected_ids = [f"TC-{n:03d}" for n in range(1, len(cases) + 1)]
    checks = [
        check("case count", expect.get("min_cases", 1) <= len(cases) <= expect.get("max_cases", 50),
               f"{len(cases)} cases, allowed {expect.get('min_cases', 1)}-{expect.get('max_cases', 50)}"),
        check("automatable count", len(automatable) >= expect.get("min_automatable", 1),
               f"{len(automatable)} automatable, need >= {expect.get('min_automatable', 1)}"),
        check("sequential ids", ids == expected_ids, f"{ids[:5]}..." if ids != expected_ids else "TC-001..."),
        check("every case has steps", all(c.get("steps") for c in cases), ""),
    ]
    vague = [c.get("test_id") for c in cases for s in c.get("steps") or []
             if re.search(r"\bworks? (correctly|properly|as expected)\b", s.get("expected_result", ""), re.I)]
    checks.append(check("no vague expected results", not vague, f"vague in {sorted(set(vague))}" if vague else "",
                         hard=False))
    if expect.get("must_cover"):
        text = " ".join(f"{c.get('summary', '')} " + " ".join(s.get("expected_result", "") for s in c.get("steps") or [])
                        for c in cases)
        missing = _missing(text, expect["must_cover"])
        checks.append(check("covers key behaviours", not missing, f"missing: {missing}" if missing else "all covered"))
    return checks


def grade_spec(code: str, expect: Dict[str, Any]) -> List[Check]:
    tests = re.findall(r"\btest\(\s*['\"`]\[TC-\d+\]", code)
    fixme = len(re.findall(r"\btest\.fixme\(", code))
    checks = [
        check("spec produced", bool(code.strip()), f"{len(code):,} chars"),
        check("test count", len(tests) >= expect.get("min_tests", 1),
               f"{len(tests)} [TC-xxx] tests, need >= {expect.get('min_tests', 1)}"),
    ]
    for needle in expect.get("require", []):
        checks.append(check(f"contains {needle!r}", needle in code))
    for needle in expect.get("forbid", []):
        checks.append(check(f"avoids {needle!r}", needle not in code))
    for needle in expect.get("prefer", []):
        checks.append(check(f"uses {needle!r}", needle in code, hard=False))
    checks.append(check("real tests, not placeholders", fixme <= max(1, len(tests) // 4),
                         f"{fixme} test.fixme of {len(tests)}", hard=False))
    return checks


def score(checks: List[Check]) -> Dict[str, Any]:
    hard = [c for c in checks if c["hard"]]
    soft = [c for c in checks if not c["hard"]]
    return {
        "passed": all(c["passed"] for c in hard),
        "hard": f"{sum(c['passed'] for c in hard)}/{len(hard)}",
        "soft": f"{sum(c['passed'] for c in soft)}/{len(soft)}",
    }
