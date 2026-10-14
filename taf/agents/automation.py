"""
Agent 3 - TS Automation Engineer: writes a Playwright TypeScript spec, repairs it
until Playwright can load it, then (in the test repo) commits and runs it.

`build_messages` + `parse_spec` are the model-facing half and are reused by the
evals, which grade a spec without touching any repository.
"""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from taf.agents.base import agent_node
from taf.agents.state import QAState
from taf.config import SETTINGS, project_path
from taf.core.text import extract_code_block, message_text, truncate_middle
from taf.integrations.github.pull_requests import diff_for_prompt
from taf.integrations.github.repository import REPO_LOCK, TEST_REPO, CommandError
from taf.integrations.paperclip.control_room import CONTROL_ROOM
from taf.llm.budget import BudgetExceededError
from taf.llm.router import ROUTER, AgentRoute, LLMOutputError
from taf.skills import skills_prompt

log = logging.getLogger("qa.automation")

SYSTEM_PROMPT = """You are a senior TypeScript SDET writing Playwright Test specs.
Produce ONE complete, compilable `.spec.ts` file for the automatable test cases you are given.

Hard requirements:
- TypeScript only. First line: `import { test, expect } from '@playwright/test';`
- Wrap everything in `test.describe('<TICKET_ID>: <summary>', () => { ... })`.
- One `test('[TC-XXX] <summary>', async ({ page }) => { ... })` per automatable test case; keep the TC id
  prefix in the title so results trace back to Xray.
- Resolve the base URL as: `const BASE_URL = process.env.BASE_URL ?? '<target url>';` and navigate with
  `page.goto(BASE_URL + '/path')` or `page.goto(BASE_URL)`.
- Use resilient, user-facing locators (getByRole, getByLabel, getByText, getByTestId) and web-first
  assertions (`await expect(locator).toBeVisible()`); never use `page.waitForTimeout` or fixed sleeps.
- Tests must be independent and idempotent; use `test.step()` to mirror the plan's steps.
- Non-functional checks must use only built-in Playwright APIs (e.g. `page.evaluate` on
  `performance.getEntriesByType('navigation')` for load budgets, keyboard navigation / ARIA checks for a11y).
- Never hard-code secrets. If credentials are required read them from `process.env` and call
  `test.skip(!process.env.X, 'reason')` when missing.
- Where the exact selector is unknown, choose the most likely accessible role/name from the diff and add a
  short `// ASSUMPTION:` comment.
Return ONLY the file content inside a single ```typescript fenced block."""

REPAIR_PROMPT = """Playwright could not load the spec you wrote. This is the output of
`playwright test {spec} --list`, which only loads and collects the tests (no browser was started):

```
{error}
```

Fix the problem and return the COMPLETE corrected file in a single ```typescript block.
Change only what is needed to make the spec load; keep the test ids, titles and intent."""


def validate_spec(code: str) -> str:
    """Basic structural validation of the generated spec; auto-fixes a missing import."""
    if "@playwright/test" not in code:
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


def automatable_cases(state: QAState) -> List[Dict[str, Any]]:
    return [tc for tc in state.get("test_cases", []) if tc.get("automatable")]


def build_messages(state: QAState, route: AgentRoute) -> List[BaseMessage]:
    """System prompt + the large, stable per-run context (cache breakpoint on Claude)."""
    prompt = f"""TICKET_ID: {state['ticket_id']}
Summary: {state.get('summary', '')}
Target URL: {state.get('target_url', '')}

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
```{page_contract_block()}"""
    system = SYSTEM_PROMPT + skills_prompt("automation", route.provider)
    return [SystemMessage(content=system), ROUTER.context_message(route, prompt)]


@agent_node("TS Automation Engineer", engine="llm+local-cli", abort_on_error=True, route_key="automation")
def automation_engineer(state: QAState) -> Dict[str, Any]:
    ticket_id, run_id = state["ticket_id"], state["run_id"]
    target_url = state.get("target_url", "")
    route = ROUTER.route("automation", state.get("llm_provider"))
    agent = route.label
    if not automatable_cases(state):
        return {
            "execution_passed": False,
            "execution_logs": "[error] The test plan contains no automatable test cases - nothing was executed.",
            "errors": ["TS Automation Engineer: no automatable test cases in plan."],
        }

    # ---- 3a. The model writes the Playwright spec ---------------------------
    messages = build_messages(state, route)
    answer = ROUTER.text(state, route, messages)

    if route.provider == "offline":
        # The placeholder must never overwrite a real spec in someone's test repo.
        code, problem = parse_spec(answer)
        return {
            "generated_typescript_code": code or "",
            "execution_passed": False,
            "execution_logs": ("[info] Offline provider: placeholder spec generated and NOT written to the "
                               "test repository; Playwright was not run. Switch the provider to "
                               "claude or ollama for a real run." + (f"\n[error] {problem}" if problem else "")),
        }

    # ---- 3b. Validate -> repair -> commit -> run (serialised on the shared clone) --
    logs: List[str] = []
    errors: List[str] = []
    code: Optional[str] = None
    spec_path: Optional[Path] = None
    branch, pushed, loaded, passed = "", False, False, False
    max_repairs = max(0, min(SETTINGS.spec_repair_attempts, 3))  # 1 + 3 cache breakpoints max
    repairs = 0

    CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent=agent, message="Waiting for test repository lock")
    with REPO_LOCK:
        try:
            TEST_REPO.ensure_ready(logs)
            branch = TEST_REPO.prepare_branch(ticket_id, logs)
            deps_ok = TEST_REPO.install_dependencies(target_url, logs)

            while True:
                candidate, problem = parse_spec(answer)
                if candidate is not None:
                    code = candidate
                    spec_path = TEST_REPO.write_spec(ticket_id, code, logs)
                    if not deps_ok:
                        break  # cannot validate or run without node_modules
                    listing = TEST_REPO.list_tests(spec_path, target_url)
                    logs.append(listing.as_log())
                    if listing.ok:
                        loaded = True
                        break
                    problem = truncate_middle(f"{listing.stdout}\n{listing.stderr}".strip(), 6000)

                if repairs >= max_repairs:
                    break
                CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent=agent,
                                  message=f"Spec did not load - repair attempt {repairs + 1}/{max_repairs}")
                rel_spec = f"{SETTINGS.test_spec_dir}/{ticket_id}.spec.ts"
                repair_block: Dict[str, Any] = {"type": "text",
                                                "text": REPAIR_PROMPT.format(spec=rel_spec, error=problem)}
                if route.cache_context:
                    repair_block["cache_control"] = ROUTER.cache_control()  # next repair reuses this turn too
                messages += [answer, HumanMessage(content=[repair_block])]
                try:
                    answer = ROUTER.text(state, route, messages)
                except (BudgetExceededError, LLMOutputError) as exc:
                    errors.append(f"{agent}: repair stopped - {exc}")
                    break
                repairs += 1

            if spec_path is not None:
                pushed = TEST_REPO.commit_and_push(ticket_id, spec_path, branch, logs)
            if loaded:
                CONTROL_ROOM.emit(run_id, ticket_id, "agent.log", agent=agent,
                                  message=f"Spec loads (repairs: {repairs}); branch {branch} "
                                          f"(pushed={pushed}); running Playwright")
                passed = TEST_REPO.run_playwright(spec_path, target_url, logs)
            elif deps_ok:
                message = f"Spec still fails to load after {repairs} repair attempt(s) - tests not executed."
                logs.append(f"[error] {message}")
                errors.append(f"{agent}: {message}")
        except CommandError as exc:
            if exc.result is not None:
                logs.append(exc.result.as_log())
            logs.append(f"[error] {exc}")
            errors.append(f"{agent}: {exc}")
            passed = False

    return {
        "generated_typescript_code": code or "",
        "spec_file_path": str(spec_path) if spec_path else "",
        "git_branch": branch,
        "git_pushed": pushed,
        "execution_logs": "\n\n".join(logs),
        "execution_passed": passed,
        "errors": errors,
    }
