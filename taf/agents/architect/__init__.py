"""
Agent 2 - Test Architect. See README.md; prompts are prompt.md and local-model-limits.md.

The code is in agent.py. Names are loaded on first use (`from taf.agents.architect import ...`)
so that importing the package has no side effects - test discovery relies on that.
"""
import importlib
from typing import Any


__all__ = ['SYSTEM_PROMPT', 'LOCAL_LIMIT', 'build_prompt', 'render_test_plan_markdown', 'test_architect']


def __getattr__(name: str) -> Any:
    if name not in __all__:  # e.g. unittest probing for `load_tests` must not import the agent
        raise AttributeError(name)
    return getattr(importlib.import_module("taf.agents.architect.agent"), name)
