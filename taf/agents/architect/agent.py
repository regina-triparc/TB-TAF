"""Agent 2 - Test Architect: a risk-based test plan, plus its Xray CSV and bulk-import JSON."""
from __future__ import annotations

import json
from typing import Any, Dict

from langchain_core.messages import SystemMessage

from taf.agents.base import agent_node
from taf.agents.prompts import load_prompt
from taf.agents.schemas import TestPlan
from taf.agents.state import QAState
from taf.config import SETTINGS
from taf.integrations.github.pull_requests import diff_for_prompt
from taf.integrations.jira.xray import build_xray_csv, build_xray_tests_json
from taf.llm.router import ROUTER
from taf.skills import skills_prompt

SYSTEM_PROMPT = load_prompt(__file__, "prompt.md")

# A small local model generates every token of the plan under a JSON-schema grammar,
# so plan length is the dominant cost: a 20-case plan does not finish inside any sane
# timeout on CPU. Cap it, and keep the steps terse. Cloud models do not need this.
LOCAL_LIMIT = load_prompt(__file__, "local-model-limits.md")


def build_prompt(state: QAState, provider: str = "claude") -> str:
    return f"""# Ticket {state['ticket_id']}: {state.get('summary', '')}
Target URL: {state.get('target_url', '')}

## Triage analysis
```json
{json.dumps(state.get('triage_analysis', {}), indent=2)}
```

## Original description
{state.get('description', '') or '(empty)'}

## Diff (abridged)
```diff
{diff_for_prompt(state, 1 / 3, provider)}
```"""


def render_test_plan_markdown(plan: TestPlan) -> str:
    lines = ["## Strategy", plan.strategy, "", "## Scope", "**In scope**"]
    lines += [f"- {item}" for item in plan.in_scope]
    if plan.out_of_scope:
        lines += ["", "**Out of scope**"] + [f"- {item}" for item in plan.out_of_scope]
    lines += ["", "## Test cases"]
    for case in plan.test_cases:
        auto = "automated" if case.automatable else "manual"
        lines += ["", f"### {case.test_id} - {case.summary}",
                  f"*{case.kind} / {case.category} / priority {case.priority} / {auto}*"]
        if case.preconditions:
            lines.append(f"**Preconditions:** {case.preconditions}")
        for n, step in enumerate(case.steps, start=1):
            data = f" (data: `{step.data}`)" if step.data else ""
            lines.append(f"{n}. {step.action}{data} → _{step.expected_result}_")
    return "\n".join(lines)


@agent_node("Test Architect", engine="llm", abort_on_error=True, route_key="architect")
def test_architect(state: QAState) -> Dict[str, Any]:
    route = ROUTER.route("architect", state.get("llm_provider"))
    system = SYSTEM_PROMPT
    if route.provider == "ollama":
        system += LOCAL_LIMIT.format(max_cases=SETTINGS.ollama_max_test_cases)
    system += skills_prompt("architect", route.provider)
    plan = ROUTER.structured(
        state, route,
        [SystemMessage(content=system), ROUTER.context_message(route, build_prompt(state, route.provider))],
        TestPlan,
    )
    assert isinstance(plan, TestPlan)
    return {
        "test_plan": render_test_plan_markdown(plan),
        "test_cases": [case.model_dump() for case in plan.test_cases],
        "xray_csv_content": build_xray_csv(plan, state["ticket_id"]),
        "xray_tests_json": build_xray_tests_json(plan, state["ticket_id"]),
    }
