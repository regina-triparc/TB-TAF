"""Agent 1 - Intake Triager: acceptance criteria, impacted areas and risk from ticket + diff + logs."""
from __future__ import annotations

from typing import Any, Dict

from langchain_core.messages import SystemMessage

from taf.agents.base import agent_node
from taf.agents.prompts import load_prompt
from taf.agents.schemas import TriageAnalysis
from taf.agents.state import QAState
from taf.config import SETTINGS
from taf.core.text import truncate_middle
from taf.integrations.github.pull_requests import diff_for_prompt
from taf.integrations.paperclip.control_room import CONTROL_ROOM
from taf.llm.router import ROUTER
from taf.skills import skills_prompt

SYSTEM_PROMPT = load_prompt(__file__, "prompt.md")


def build_prompt(state: QAState, provider: str = "claude") -> str:
    diff = diff_for_prompt(state, 1.0, provider) or "(no diff provided)"
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
        [SystemMessage(content=system), ROUTER.context_message(route, build_prompt(state, route.provider))],
        TriageAnalysis,
    )
    CONTROL_ROOM.emit(state["run_id"], state["ticket_id"], "triage.completed",
                      risk_level=analysis.risk_level, criteria=len(analysis.acceptance_criteria))
    return {"triage_analysis": analysis.model_dump()}
