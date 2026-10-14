"""
Skills: reusable know-how, kept as Markdown, that is appended to an agent's system
prompt. Adding expertise to an agent means adding a folder, not editing Python.

    skills/<skill-name>/SKILL.md

    ---
    name: playwright-locators
    description: One line on what this teaches.
    agents: automation            # comma list: triage, architect, automation
    ---
    Markdown body - what the agent should know or do.

Skills apply to Claude routes. On local models they are off unless SKILLS_ON_LOCAL
is true, because a small context window fills fast. SKILLS_MAX_CHARS caps the total
appended per agent; skills are taken in folder-name order until the cap is hit.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Tuple

from taf.config import SETTINGS, project_path

log = logging.getLogger("qa.skills")


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    agents: Tuple[str, ...]
    body: str
    path: Path


def _parse(path: Path) -> Skill:
    text = path.read_text(encoding="utf-8")
    meta: Dict[str, str] = {}
    body = text
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        for line in front.strip().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip().lower()] = value.strip()
    agents = tuple(a.strip().lower() for a in meta.get("agents", "").strip("[]").split(",") if a.strip())
    return Skill(name=meta.get("name") or path.parent.name,
                 description=meta.get("description", ""),
                 agents=agents, body=body.strip(), path=path)


def load_skills() -> List[Skill]:
    """Every skills/*/SKILL.md, sorted by folder. Unreadable files are skipped with a warning."""
    root = project_path(SETTINGS.skills_dir)
    if not root.is_dir():
        return []
    skills = []
    for path in sorted(root.glob("*/SKILL.md")):
        try:
            skills.append(_parse(path))
        except (OSError, ValueError) as exc:
            log.warning("Skipping skill %s: %s", path, exc)
    return skills


def skills_for(agent: str, provider: str) -> List[Skill]:
    """The skills that apply to `agent` when it runs on `provider` (claude / ollama / offline)."""
    if not SETTINGS.skills_enabled or provider == "offline":
        return []
    if provider != "claude" and not SETTINGS.skills_on_local:
        return []
    return [s for s in load_skills() if agent in s.agents]


def skills_prompt(agent: str, provider: str) -> str:
    """The block appended to the agent's system prompt (empty string when none apply)."""
    parts: List[str] = []
    used = 0
    for skill in skills_for(agent, provider):
        section = f"### Skill: {skill.name}\n{skill.body}"
        if used + len(section) > SETTINGS.skills_max_chars:
            log.warning("SKILLS_MAX_CHARS=%s reached - skill %r and later ones not applied to %s.",
                        SETTINGS.skills_max_chars, skill.name, agent)
            break
        parts.append(section)
        used += len(section)
    if not parts:
        return ""
    return "\n\n## Skills\nApply the following team practices.\n\n" + "\n\n".join(parts)
