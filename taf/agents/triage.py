"""Agent 1 - Intake Triager: acceptance criteria, impacted areas and risk from ticket + diff + logs."""
from __future__ import annotations

from typing import Any, Dict

from langchain_core.messages import SystemMessage

from taf.agents.base import agent_node
from taf.agents.schemas import TriageAnalysis
from taf.agents.state import QAState
from taf.config import SETTINGS
from taf.core.text import truncate_middle
from taf.integrations.paperclip.control_room import CONTROL_ROOM
from taf.llm.router import ROUTER
from taf.skills import skills_prompt

SYSTEM_PROMPT = """You are the Intake Triager in an autonomous QA team - a principal QA engineer.
You receive a Jira ticket that has just moved to "Ready for QA", the GitHub PR diff that implements it,
and recent Azure / database logs from the target environment.

Your job:
1. Extract the explicit acceptance criteria. If they are missing or vague, derive precise, testable ones
   from the description and the diff, and list the ambiguities under gaps_and_questions.
2. Map the diff to impacted user-facing areas (pages, components, API endpoints, data flows).
3. Identify concrete regression and defect risks introduced by the change (not generic advice).
4. Pull out anything in the environment logs that matters for testing (errors, timeouts, slow queries).
5. Assign an overall risk level.
Be specific and grounded in the provided material; never invent features that are not in the ticket or diff."""


def build_prompt(state: QAState) -> str:
    diff = truncate_middle(state.get("github_diff", ""), SETTINGS.max_diff_chars) or "(no diff provided)"
    env_logs = truncate_middle(state.get("environment_logs", ""), SETTINGS.max_env_log_chars) or "(no logs provided)"
    return f"""# Jira ticket {state['ticket_id']}
## Summary
{state.get('summary', '')}

## Description / acceptance criteria
{state.get('description', '') or '(empty)'}

## Target environment
{state.get('target_url', '')}

## GitHub PR diff
```diff
{diff}
```

## Azure / DB logs
```
{env_logs}
```"""


@agent_node("Intake Triager", engine="llm", abort_on_error=True, route_key="triage")
def intake_triager(state: QAState) -> Dict[str, Any]:
    route = ROUTER.route("triage", state.get("llm_provider"))
    system = SYSTEM_PROMPT + skills_prompt("triage", route.provider)
    analysis = ROUTER.structured(
        state, route,
        [SystemMessage(content=system), ROUTER.context_message(route, build_prompt(state))],
        TriageAnalysis,
    )
    CONTROL_ROOM.emit(state["run_id"], state["ticket_id"], "triage.completed",
                      risk_level=analysis.risk_level, criteria=len(analysis.acceptance_criteria))
    return {"triage_analysis": analysis.model_dump()}
