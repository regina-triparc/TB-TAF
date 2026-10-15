"""
Agent 3 - TS Automation Engineer: writes the Playwright TypeScript spec.

It only generates text. Writing files, validating, running Playwright and
publishing are deterministic workflow steps (taf/steps/), and the graph sends the
spec back here with the problem when a check fails - at most SPEC_REPAIR_ATTEMPTS
times. The repair turn re-sends the same cached context, so on Claude it is read
from the prompt cache.

`build_messages` + `parse_spec` are the model-facing half and are reused by the
evals, which grade a spec without touching any repository.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from taf.agents.base import agent_node
from taf.agents.prompts import load_prompt
from taf.agents.state import QAState
from taf.config import SETTINGS, project_path
from taf.core.text import extract_code_block, message_text, truncate_middle
from taf.integrations.github.pull_requests import diff_for_prompt
from taf.integrations.paperclip.control_room import CONTROL_ROOM
from taf.llm.router import ROUTER, AgentRoute, LLMOutputError
from taf.skills import skills_prompt

log = logging.getLogger("qa.automation")

SYSTEM_PROMPT = load_prompt(__file__, "prompt.md")

REPAIR_PROMPT = load_prompt(__file__, "repair-prompt.md")

OFFLINE_LOG = ("[info] Offline provider: placeholder spec and test cases generated and NOT "
               "written to the test repository; Playwright was not run. Switch the provider "
               "to claude or ollama for a real run.")


def max_generations() -> int:
    """First attempt plus SPEC_REPAIR_ATTEMPTS repairs (0-3)."""
    return 1 + max(0, min(SETTINGS.spec_repair_attempts, 3))


def validate_spec(code: str) -> str:
    """Basic structural validation of the generated spec; auto-fixes a missing import."""
    if "@playwright/test" not in code and not re.search(r"^\s*import\s+\{[^}]*\btest\b", code, re.MULTILINE):
        code = "import { test, expect } from '@playwright/test';\n\n" + code
    if not re.search(r"\btest\s*\(", code):
        raise LLMOutputError("Generated spec contains no `test(` blocks.")
    if "```" in code:
        raise LLMOutputError("Generated spec still contains markdown fences.")
    return code


def parse_spec(answer: AIMessage) -> Tuple[Optional[str], Optional[str]]:
    """Extract and structurally validate the spec: (code, None) or (None, problem)."""
    try:
        return validate_spec(extract_code_block(message_text(answer))), None
    except LLMOutputError as exc:
        return None, str(exc)


def page_contract_block() -> str:
    """
    Real roles and accessible names from the running app, appended to the prompt.
    A model that has never seen the page invents selectors that compile and then
    match nothing; this is the ground truth that stops it.
    """
    if not SETTINGS.page_contract_path:
        return ""
    path = project_path(SETTINGS.page_contract_path)
    try:
        text = path.read_text(encoding="utf8")
    except OSError as exc:
        log.warning("PAGE_CONTRACT_PATH unreadable (%s)", exc)
        return ""
    header = ("\n\n## Page contract - REAL selectors from the live app\n"
              "These are authoritative. Use these roles and names verbatim; do not invent others.\n\n")
    return header + truncate_middle(text, 8000)


def repo_conventions_block(state: QAState) -> str:
    """The test repository's conventions (page objects, fixtures, example spec) from repo_context."""
    text = (state.get("repo_context") or {}).get("prompt") or ""
    return f"\n\n{text}" if text else ""


def automatable_cases(state: QAState) -> List[Dict[str, Any]]:
    return [tc for tc in state.get("test_cases", []) if tc.get("automatable")]


def build_messages(state: QAState, route: AgentRoute) -> List[BaseMessage]:
    """System prompt + the large, stable per-run context (cache breakpoint on Claude)."""
    prompt = f"""TICKET_ID: {state['ticket_id']}
Summary: {state.get('summary', '')}
Target URL: {state.get('target_url', '')}
Spec file: {SETTINGS.test_spec_dir.strip('/')}/{state['ticket_id']}.spec.ts

## Triage analysis
```json
{json.dumps(state.get('triage_analysis', {}), indent=2)}
```

## Automatable test cases
```json
{json.dumps(automatable_cases(state), indent=2)}
```

## Diff (for selectors, routes and copy text)
```diff
{diff_for_prompt(state, 1 / 2, route.provider)}
```{repo_conventions_block(state)}{page_contract_block()}"""
    system = SYSTEM_PROMPT + skills_prompt("automation", route.provider)
    return [SystemMessage(content=system), ROUTER.context_message(route, prompt)]


def repair_messages(state: QAState, route: AgentRoute) -> List[BaseMessage]:
    """The previous spec and why it was rejected, as one extra turn on the cached context."""
    previous = state.get("generated_typescript_code") or "(no usable spec was produced)"
    rel_spec = f"{SETTINGS.test_spec_dir.strip('/')}/{state['ticket_id']}.spec.ts"
    block: Dict[str, Any] = {"type": "text",
                             "text": REPAIR_PROMPT.format(spec=rel_spec, error=state.get("spec_feedback", ""))}
    if route.cache_context:
        block["cache_control"] = ROUTER.cache_control()
    return [AIMessage(content=f"```typescript\n{previous}\n```"), HumanMessage(content=[block])]


@agent_node("TS Automation Engineer", engine="llm", abort_on_error=True, route_key="automation")
def generate_spec(state: QAState) -> Dict[str, Any]:
    """
    Generate (or, when `spec_feedback` is set, repair) the spec. A spec that cannot be
    parsed is not an error here: validation rejects it and the graph decides whether
    another attempt is allowed.
    """
    ticket_id, run_id = state["ticket_id"], state["run_id"]
    route = ROUTER.route("automation", state.get("llm_provider"))
    attempt = int(state.get("spec_attempts") or 0) + 1
    cases = automatable_cases(state)
    if not cases:
        return {"spec_attempts": attempt, "generated_typescript_code": "", "spec_feedback": "",
                "errors": [f"{route.label}: no automatable test cases in plan."]}

    messages = build_messages(state, route)
    repairing = attempt > 1 and bool(state.get("spec_feedback"))
    if repairing:
        messages += repair_messages(state, route)
        CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent=route.label,
                          message=f"Repair attempt {attempt - 1}/{max_generations() - 1}")
    answer = ROUTER.text(state, route, messages)
    code, problem = parse_spec(answer)

    update: Dict[str, Any] = {
        "spec_attempts": attempt,
        "generated_typescript_code": code or "",
        # Feedback is consumed: validation sets it again if this attempt fails too.
        "spec_feedback": problem or "",
    }
    if route.provider == "offline":
        update["execution_logs"] = OFFLINE_LOG + (f"\n[error] {problem}" if problem else "")
        update["execution_passed"] = False
    return update
