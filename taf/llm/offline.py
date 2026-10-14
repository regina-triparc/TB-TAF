"""
The `offline` provider: a deterministic stand-in for a model.

It exists so that anyone who clones the repo can push the seeded ticket through
the whole pipeline - router, agents, graph, run store, dashboard, evals - with no
API key, no Ollama and no network. It does not analyse anything: criteria are
lifted from the ticket text, the plan is one case per criterion, and the spec is a
`test.fixme` placeholder. Output is labelled as such everywhere it appears.

Never use it for real QA. Its job is to prove the plumbing works.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Type

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import BaseModel

OFFLINE_NOTICE = ("Offline provider: no model analysed this ticket. Output is derived "
                  "mechanically from the ticket text to exercise the pipeline.")

_NUMBERED = re.compile(r"^\s*\d+[.)]\s+(.+?)\s*$")
_BULLET = re.compile(r"^\s*[-*•]\s+(.+?)\s*$")
_HEADING = re.compile(r"^\s*([A-Z][^:\n]{2,60}):\s*$")
MAX_CASES = 8


def _criteria(state: Dict[str, Any]) -> List[str]:
    description = state.get("description") or ""
    # Numbered lines are the criteria when a ticket has them; bullets only otherwise
    # (tickets also use bullets for notes, known defects and touched areas).
    found = [m.group(1) for line in description.splitlines() if (m := _NUMBERED.match(line))]
    if not found:
        found = [m.group(1) for line in description.splitlines() if (m := _BULLET.match(line))]
    if not found:
        sentences = re.split(r"(?<=[.!?])\s+", description.strip())
        found = [s for s in sentences if len(s) > 20][:5]
    return found or [state.get("summary") or "The change behaves as described in the ticket."]


def _areas(state: Dict[str, Any]) -> List[str]:
    description = state.get("description") or ""
    areas = [m.group(1).strip() for line in description.splitlines() if (m := _HEADING.match(line))]
    areas = [a for a in areas if a.upper() != "ACCEPTANCE CRITERIA"]
    return areas or ["(not analysed offline)"]


def _triage(state: Dict[str, Any]) -> Dict[str, Any]:
    areas = _areas(state)
    return {
        "feature_summary": state.get("summary") or state.get("ticket_id", ""),
        "acceptance_criteria": _criteria(state),
        "impacted_areas": areas,
        "risk_level": "medium",
        "risks": [f"Regression in {a}" for a in areas if not a.startswith("(")] or ["(not analysed offline)"],
        "environment_findings": [],
        "gaps_and_questions": [OFFLINE_NOTICE],
    }


def _plan(state: Dict[str, Any]) -> Dict[str, Any]:
    criteria = (state.get("triage_analysis") or {}).get("acceptance_criteria") or _criteria(state)
    cases = []
    for n, criterion in enumerate(criteria[:MAX_CASES], start=1):
        short = criterion if len(criterion) <= 90 else criterion[:87] + "..."
        cases.append({
            "test_id": f"TC-{n:03d}",
            "summary": short,
            "kind": "functional",
            "category": "happy-path",
            "priority": "High" if n <= 2 else "Medium",
            "automatable": True,
            "preconditions": "",
            "acceptance_criteria_refs": [f"AC-{n}"],
            "steps": [{"action": f"Exercise: {criterion}", "data": "",
                       "expected_result": criterion}],
        })
    return {
        "strategy": f"{OFFLINE_NOTICE} One functional case per acceptance criterion.",
        "in_scope": [c["summary"] for c in cases[:3]],
        "out_of_scope": [],
        "test_cases": cases,
    }


def _spec(state: Dict[str, Any]) -> str:
    ticket = state.get("ticket_id", "TICKET")
    title = json.dumps(f"{ticket}: {state.get('summary', '')}")
    base = json.dumps(state.get("target_url") or "http://localhost")
    tests = []
    for case in [c for c in state.get("test_cases") or [] if c.get("automatable")][:MAX_CASES]:
        name = json.dumps(f"[{case.get('test_id')}] {case.get('summary')}")
        tests.append(
            f"  test({name}, async ({{ page }}) => {{\n"
            f"    test.fixme(true, 'Offline provider placeholder - generate with a real model.');\n"
            f"    await page.goto(BASE_URL);\n"
            f"    await expect(page).toHaveURL(/.+/);\n"
            f"  }});"
        )
    body = "\n\n".join(tests) or "  test.fixme('no automatable cases', async () => {});"
    return (
        "import { test, expect } from '@playwright/test';\n\n"
        f"// {OFFLINE_NOTICE}\n"
        f"const BASE_URL = process.env.BASE_URL ?? {base};\n\n"
        f"test.describe({title}, () => {{\n{body}\n}});\n"
    )


def structured(agent: str, state: Dict[str, Any], schema: Type[BaseModel]) -> BaseModel:
    builders = {"triage": _triage, "architect": _plan}
    if agent not in builders:
        raise RuntimeError(f"Offline provider has no structured output for {agent!r}")
    return schema.model_validate(builders[agent](state))


def text(agent: str, state: Dict[str, Any], messages: List[BaseMessage]) -> AIMessage:
    if agent != "automation":
        raise RuntimeError(f"Offline provider has no text output for {agent!r}")
    return AIMessage(content=f"```typescript\n{_spec(state)}```")
