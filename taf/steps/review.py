"""
Review of a validated spec: does it test what the plan says, the way this test
repository writes tests?

The rules here are deterministic and always run. An optional LLM review
(REVIEW_WITH_LLM=true) adds judgement on conventions and maintainability. A
`blocker` from either sends the spec back for a repair while attempts remain;
otherwise findings are reported (and listed in the pull request) - review never
replaces the static validation gate.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any, Dict, List, Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from taf.agents.automation.agent import automatable_cases, max_generations
from taf.agents.base import agent_node
from taf.agents.prompts import load_prompt
from taf.agents.state import QAState
from taf.config import SETTINGS
from taf.core.text import truncate_middle
from taf.llm.router import ROUTER

Finding = Dict[str, str]

REVIEW_PROMPT = load_prompt(__file__, "review-prompt.md")

_TITLE_RE = re.compile(r"\btest(?:\.(?:only|skip|fixme|fail))?\(\s*(['\"`])(.+?)\1")
_TC_RE = re.compile(r"\[(TC-\d+)\]")
_RAW_LOCATOR_RE = re.compile(r"\.locator\(\s*['\"`](?![a-z]+=)([^'\"`]+)['\"`]")
_SEMANTIC_RE = re.compile(r"\.getBy(Role|Label|Text|TestId|Placeholder|AltText|Title)\(")
_SECRET_RE = re.compile(r"(?i)(password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"][^'\"]{4,}['\"]")
_TOP_LEVEL_FN_RE = re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)", re.MULTILINE)
_UNNAMED_ROLE_RE = re.compile(r"\bpage\.getByRole\(\s*['\"](\w+)['\"]\s*\)")
_TWELVE_HOUR_OPTION_RE = re.compile(
    r"(?:pickTime\([^)]*?|getByRole\(\s*['\"]option['\"]\s*,\s*\{\s*name:\s*)['\"](\d{1,2}:\d{2}\s?[AP]M)['\"]", re.I)


def finding(severity: str, rule: str, detail: str) -> Finding:
    return {"severity": severity, "rule": rule, "detail": detail}


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def covered_criteria(criteria: List[str], cases: List[Dict[str, Any]]) -> set:
    """
    1-based positions of the acceptance criteria the plan's cases reference. Models
    write references as "AC-2", "AC2", "2" or "#2" (the criterion's position in the
    triage list), or quote the criterion's text - all are accepted.
    """
    covered = set()
    for case in cases:
        for ref in case.get("acceptance_criteria_refs") or []:
            ref = str(ref)
            if len(ref.strip()) <= 12:
                match = re.search(r"(\d+)", ref)
                if match and 1 <= int(match.group(1)) <= len(criteria):
                    covered.add(int(match.group(1)))
                continue
            quoted = _norm(ref)
            for n, criterion in enumerate(criteria, start=1):
                text = _norm(criterion)
                if len(text) >= 8 and (text[:40] in quoted or quoted[:40] in text):
                    covered.add(n)
    return covered


def rule_review(code: str, cases: List[Dict[str, Any]], criteria: List[str],
                repo_facts: Dict[str, Any]) -> List[Finding]:
    """Deterministic findings: blocker | major | minor."""
    out: List[Finding] = []
    if re.search(r"\bwaitForTimeout\s*\(|new Promise\(\s*\w*\s*=>\s*setTimeout", code):
        out.append(finding("blocker", "no fixed sleeps", "uses waitForTimeout / setTimeout - use web-first assertions"))
    if re.search(r"\b(test|describe)\.only\s*\(", code):
        out.append(finding("blocker", "no .only", "test.only / describe.only would silence the rest of the suite"))
    if _SECRET_RE.search(code):
        out.append(finding("blocker", "no hard-coded secrets", "a credential-like literal is in the spec - "
                                                               "read it from process.env"))

    titles = [m.group(2) for m in _TITLE_RE.finditer(code)]
    duplicates = [t for t, n in Counter(titles).items() if n > 1]
    if duplicates:
        out.append(finding("major", "unique tests", f"duplicate test titles: {duplicates[:5]}"))
    in_spec = set(_TC_RE.findall(code))
    missing = [c.get("test_id") for c in cases if c.get("automatable") and c.get("test_id") not in in_spec]
    if missing:
        out.append(finding("major", "traceability", f"automatable cases with no [TC-xxx] test: {missing}"))

    has_refs = any(c.get("acceptance_criteria_refs") for c in cases)
    covered = covered_criteria(criteria, cases)
    uncovered = [f"AC-{n}" for n in range(1, len(criteria) + 1) if n not in covered]
    if criteria and has_refs and uncovered:
        out.append(finding("major", "criteria coverage", f"criteria no test case references: {uncovered}"))

    helpers = _TOP_LEVEL_FN_RE.findall(code)
    if helpers:
        out.append(finding("major", "lean spec", f"{len(helpers)} helper function(s) in the spec "
                                                 f"({', '.join(helpers[:6])}) - use or extend a page object"))
    # Landmarks occur once per page, so an unnamed one is unambiguous.
    unnamed = sorted(set(_UNNAMED_ROLE_RE.findall(code)) - {"main", "banner", "contentinfo", "navigation"})
    if unnamed:
        out.append(finding("major", "named, scoped locators",
                           f"page.getByRole({', '.join(repr(r) for r in unnamed)}) has no name and no scope - "
                           "it matches every such element on the page"))
    twelve_hour = sorted(set(_TWELVE_HOUR_OPTION_RE.findall(code)))
    if twelve_hour:
        out.append(finding("major", "option values from the UI",
                           f"picks {twelve_hour} - check the option format in the live snapshot "
                           "(the Info time pickers list 24-hour HH:mm)"))

    raw = _RAW_LOCATOR_RE.findall(code)
    semantic = len(_SEMANTIC_RE.findall(code))
    if raw and len(raw) > semantic:
        out.append(finding("minor", "user-facing locators",
                           f"{len(raw)} CSS/XPath locators vs {semantic} getBy* locators"))
    fixme = len(re.findall(r"\btest\.(fixme|skip)\(", code))
    if titles and fixme > max(1, len(titles) // 4):
        out.append(finding("minor", "real tests", f"{fixme} of {len(titles)} tests are fixme/skip"))
    if repo_facts.get("page_objects"):
        modules = {po["import"] for po in repo_facts["page_objects"]}
        if not any(m in code for m in modules):
            out.append(finding("minor", "reuse page objects",
                               "the repo has page objects but the spec imports none of them"))
    return out


class ReviewFinding(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    issue: str = Field(description="What is wrong, with the test id or line it concerns.")
    suggestion: str = Field(description="How to fix it.")


class SpecReview(BaseModel):
    summary: str = Field(description="Two sentences: is this spec fit to merge, and why.")
    findings: List[ReviewFinding] = Field(default_factory=list)


def llm_review(state: QAState) -> Dict[str, Any]:
    route = ROUTER.route("review", state.get("llm_provider"))
    context = f"""# {state['ticket_id']}: {state.get('summary', '')}

## Acceptance criteria
{json.dumps((state.get('triage_analysis') or {}).get('acceptance_criteria') or [], indent=2)}

## Automatable test cases
{json.dumps(automatable_cases(state), indent=2)}

{(state.get('repo_context') or {}).get('prompt', '')}

## Spec under review
```typescript
{truncate_middle(state.get('generated_typescript_code') or '', 30_000)}
```"""
    result = ROUTER.structured(state, route, [SystemMessage(content=REVIEW_PROMPT), HumanMessage(content=context)],
                               SpecReview)
    assert isinstance(result, SpecReview)
    return {"model": route.model, "provider": route.provider, "summary": result.summary,
            "findings": [f.model_dump() for f in result.findings]}


@agent_node("Test Reviewer", engine="rules", abort_on_error=False, span_type="evaluator")
def review_spec(state: QAState) -> Dict[str, Any]:
    if ROUTER.resolve(state.get("llm_provider")) == "offline" or not state.get("generated_typescript_code"):
        return {"review": {"findings": [], "blocking": False, "skipped": "no spec to review"}}
    criteria = (state.get("triage_analysis") or {}).get("acceptance_criteria") or []
    findings = rule_review(state.get("generated_typescript_code") or "", state.get("test_cases") or [],
                           criteria, state.get("repo_context") or {})
    review: Dict[str, Any] = {"findings": findings}
    errors: List[str] = []
    if SETTINGS.review_with_llm:
        try:
            review["llm"] = llm_review(state)
            findings += [finding(f["severity"], "llm review", f"{f['issue']} - {f['suggestion']}")
                         for f in review["llm"]["findings"]]
        except Exception as exc:  # noqa: BLE001 - an optional reviewer must not fail the run
            errors.append(f"Test Reviewer: LLM review skipped - {type(exc).__name__}: {exc}")
    blockers = [f for f in findings if f["severity"] == "blocker"]
    review["blocking"] = bool(blockers)
    update: Dict[str, Any] = {"review": review}
    if blockers and int(state.get("spec_attempts") or 0) < max_generations():
        update["spec_feedback"] = "Review blockers:\n" + "\n".join(f"- {f['rule']}: {f['detail']}" for f in blockers)
    if errors:
        review["warnings"] = errors
    return update
