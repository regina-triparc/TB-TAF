"""
Prompts live next to each agent as Markdown (taf/agents/<agent>/*.md), so they can
be read and edited without touching Python. The file is sent exactly as written,
minus the single trailing newline every text file ends with.
"""
from __future__ import annotations

import functools
from pathlib import Path


@functools.lru_cache(maxsize=None)
def load_prompt(agent_file: str, name: str) -> str:
    """`load_prompt(__file__, "prompt.md")` from an agent module."""
    text = (Path(agent_file).parent / name).read_text(encoding="utf-8")
    return text[:-1] if text.endswith("\n") else text
