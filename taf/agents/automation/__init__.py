"""
Agent 3 - TS Automation Engineer. See README.md; prompts are prompt.md and repair-prompt.md.

The code is in agent.py. Names are loaded on first use (`from taf.agents.automation import ...`)
so that importing the package has no side effects - test discovery relies on that.
"""
import importlib
from typing import Any


__all__ = ['SYSTEM_PROMPT', 'REPAIR_PROMPT', 'OFFLINE_LOG', 'automatable_cases', 'generate_spec', 'build_messages',
           'max_generations', 'page_contract_block', 'parse_spec', 'repair_messages', 'repo_conventions_block',
           'validate_spec']


def __getattr__(name: str) -> Any:
    if name not in __all__:  # e.g. unittest probing for `load_tests` must not import the agent
        raise AttributeError(name)
    return getattr(importlib.import_module("taf.agents.automation.agent"), name)
