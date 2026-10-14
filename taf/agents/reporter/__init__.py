"""
Agent 4 - Report Synthesizer. See README.md; the system prompt is prompt.md.

The code is in agent.py. Names are loaded on first use (`from taf.agents.reporter import ...`)
so that importing the package has no side effects - test discovery relies on that.
"""
import importlib
from typing import Any


__all__ = ['SYSTEM_PROMPT', 'fallback_report_body', 'parse_playwright_counts', 'report_synthesizer']


def __getattr__(name: str) -> Any:
    if name not in __all__:  # e.g. unittest probing for `load_tests` must not import the agent
        raise AttributeError(name)
    return getattr(importlib.import_module("taf.agents.reporter.agent"), name)
