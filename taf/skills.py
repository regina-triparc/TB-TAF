"""
Skills: reusable know-how, kept as Markdown, that is appended to an agent's system
prompt. Adding expertise to an agent means adding a folder, not editing Python.

    skills/<skill-name>/SKILL.md
    skills/<skill-name>/references/*.md      optional long material, loaded only when selected

    ---
    name: playwright-locators
    description: One line on what this teaches.
    agents: automation            # comma list: triage, architect, automation
    # Optional - a skill without them always applies (all 8 original skills):
    layers: api                   # api, ui: the run's layer must be one of them
    actions: create, update       # create, update, regression: the run's action must match
    triggers: src/**/*.cs, negative   # diff path globs or test-case categories; one must match
    priority: 10                  # higher loads first when the budget is tight (default 0)
    ---
    Markdown body - what the agent should know or do.

A reference file may carry its own `triggers:` front matter; it is appended to its
skill only when one of them matches (no triggers: never loaded automatically).

Skills apply to Claude routes. On local models they are off unless SKILLS_ON_LOCAL
is true, and references are never loaded there, because a small context window fills
fast. SKILLS_MAX_CHARS caps the total appended per agent: skills are taken by
priority, then folder name, and one that does not fit is skipped - never silently:
`select_skills` records what was loaded and skipped, and why, in the run's
`skills_used` (shown on the dashboard, in the report and on the Langfuse span).

Until tickets are classified (Gap 3), a run's layer is `ui` and its action `create`,
which is what every run is today.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from taf.config import SETTINGS, project_path

log = logging.getLogger("qa.skills")

DEFAULT_LAYER = "ui"
DEFAULT_ACTION = "create"
_DIFF_PATH_RE = re.compile(r"^(?:diff --git a/\S+ b/(\S+)|\+\+\+ b/(\S+))", re.MULTILINE)


@dataclass(frozen=True)
class Reference:
    name: str
    triggers: Tuple[str, ...]
    body: str


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    agents: Tuple[str, ...]
    body: str
    path: Path
    layers: Tuple[str, ...] = ()
    actions: Tuple[str, ...] = ()
    triggers: Tuple[str, ...] = ()
    priority: int = 0
    references: Tuple[Reference, ...] = field(default_factory=tuple)


def _front_matter(text: str) -> Tuple[Dict[str, str], str]:
    meta: Dict[str, str] = {}
    body = text
    if text.startswith("---"):
        _, front, body = text.split("---", 2)
        for line in front.strip().splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                meta[key.strip().lower()] = value.strip()
    return meta, body.strip()


def _list(meta: Dict[str, str], key: str, lower: bool = True) -> Tuple[str, ...]:
    raw = meta.get(key, "").split(" #", 1)[0].strip().strip("[]")  # allow a trailing comment
    return tuple((v.strip().lower() if lower else v.strip()) for v in raw.split(",") if v.strip())


def _parse_reference(path: Path) -> Reference:
    meta, body = _front_matter(path.read_text(encoding="utf-8"))
    return Reference(name=f"{path.parent.parent.name}/{path.stem}", triggers=_list(meta, "triggers", lower=False),
                     body=body)


def _parse(path: Path, default_agents: Tuple[str, ...] = (), default_priority: int = 0) -> Skill:
    meta, body = _front_matter(path.read_text(encoding="utf-8"))
    try:
        priority = int((meta.get("priority") or str(default_priority)).split(" #", 1)[0])
    except ValueError:
        priority = 0
    references = []
    for ref in sorted((path.parent / "references").glob("*.md")):
        try:
            references.append(_parse_reference(ref))
        except (OSError, ValueError) as exc:
            log.warning("Skipping skill reference %s: %s", ref, exc)
    return Skill(name=meta.get("name") or path.parent.name,
                 description=meta.get("description", ""),
                 agents=_list(meta, "agents") or default_agents, body=body, path=path,
                 layers=_list(meta, "layers"), actions=_list(meta, "actions"),
                 triggers=_list(meta, "triggers", lower=False), priority=priority,
                 references=tuple(references))


# Skills from the test repo are Claude Code skills: no `agents:` line, and they are the
# project's own rules for the specs TAF writes there, so they go to the automation agent
# (unless they name their agents) and load before TAF's generic skills.
TEST_REPO_SKILL_AGENTS = ("automation",)
TEST_REPO_SKILL_PRIORITY = 50


def test_repo_skills_dir() -> Optional[Path]:
    """<TEST_REPO_LOCAL_PATH>/.claude/skills, when enabled and present."""
    if not SETTINGS.skills_from_test_repo or not SETTINGS.test_repo_local_path:
        return None
    root = Path(SETTINGS.test_repo_local_path).expanduser() / ".claude" / "skills"
    return root if root.is_dir() else None


def load_skills() -> List[Skill]:
    """
    Every skills/*/SKILL.md, sorted by folder, then the test repo's .claude/skills/*/SKILL.md.
    A test-repo skill whose name TAF already has is skipped. Unreadable files are skipped
    with a warning.
    """
    sources: List[Tuple[Path, Dict[str, Any]]] = [(project_path(SETTINGS.skills_dir), {})]
    extra = test_repo_skills_dir()
    if extra:
        sources.append((extra, {"default_agents": TEST_REPO_SKILL_AGENTS,
                                "default_priority": TEST_REPO_SKILL_PRIORITY}))
    skills: List[Skill] = []
    for root, defaults in sources:
        if not root.is_dir():
            continue
        for path in sorted(root.glob("*/SKILL.md")):
            try:
                skill = _parse(path, **defaults)
            except (OSError, ValueError) as exc:
                log.warning("Skipping skill %s: %s", path, exc)
                continue
            if any(s.name == skill.name for s in skills):
                log.warning("Skipping skill %s: a skill named %r is already loaded", path, skill.name)
                continue
            skills.append(skill)
    return skills


def skills_for(agent: str, provider: str) -> List[Skill]:
    """The skills that apply to `agent` when it runs on `provider` (claude / ollama / offline)."""
    if not SETTINGS.skills_enabled or provider == "offline":
        return []
    if provider != "claude" and not SETTINGS.skills_on_local:
        return []
    return [s for s in load_skills() if agent in s.agents]


# ------------------------------------------------------------- selection --
def diff_paths(state: Dict[str, Any]) -> List[str]:
    """Files the run's PR diff touches."""
    found = [a or b for a, b in _DIFF_PATH_RE.findall(state.get("github_diff") or "")]
    return list(dict.fromkeys(p for p in found if p and p != "/dev/null"))


def case_categories(state: Dict[str, Any]) -> Set[str]:
    return {str(c.get("category") or "").strip().lower() for c in state.get("test_cases") or [] if c.get("category")}


def _matches(triggers: Tuple[str, ...], paths: List[str], categories: Set[str]) -> List[str]:
    """The triggers that fire: a glob matching a diff path, or a test-case category."""
    hits = []
    for trigger in triggers:
        if trigger.lower() in categories or any(fnmatch.fnmatch(p, trigger) for p in paths):
            hits.append(trigger)
    return hits


def _action_ok(wanted: Tuple[str, ...], action: str) -> bool:
    # "regression" also covers the classified action "regression_only".
    return any(action == a or action.startswith(a + "_") for a in wanted)


@dataclass
class Selection:
    prompt: str
    record: Dict[str, Any]


def select_skills(agent: str, provider: str, state: Optional[Dict[str, Any]] = None) -> Selection:
    """
    The skills (and references) for `agent` in this run, fitted to SKILLS_MAX_CHARS, and
    a record of what was loaded and skipped and why. `state` None = no run context: only
    skills without layers/actions/triggers can match, as before this selection existed.
    """
    state = state or {}
    layer = str(state.get("layer") or DEFAULT_LAYER).lower()
    action = str(state.get("action") or DEFAULT_ACTION).lower()
    paths, categories = diff_paths(state), case_categories(state)
    record: Dict[str, Any] = {"layer": layer, "action": action, "loaded": [], "skipped": [], "references": []}
    candidates: List[Tuple[Skill, List[str]]] = []
    for skill in skills_for(agent, provider):
        layers = set(skill.layers)
        if layers and layer not in layers and not (layer == "both" and layers & {"api", "ui"}):
            record["skipped"].append({"name": skill.name, "reason": f"layer {layer} not in {sorted(layers)}"})
            continue
        if skill.actions and not _action_ok(skill.actions, action):
            record["skipped"].append({"name": skill.name, "reason": f"action {action} not in {list(skill.actions)}"})
            continue
        if skill.triggers and not _matches(skill.triggers, paths, categories):
            record["skipped"].append({"name": skill.name, "reason": "no trigger matched the diff or the plan"})
            continue
        refs = []
        if provider == "claude":  # no references on local routes: their context is small
            refs = [r for r in skill.references if r.triggers and _matches(r.triggers, paths, categories)]
        candidates.append((skill, [r.name for r in refs]))
    # Priority first; the stable sort keeps folder order among equals (the old behaviour).
    candidates.sort(key=lambda c: -c[0].priority)

    parts: List[str] = []
    used = 0
    by_name = {r.name: r for s, _ in candidates for r in s.references}
    for skill, ref_names in candidates:
        section = f"### Skill: {skill.name}\n{skill.body}"
        if used + len(section) > SETTINGS.skills_max_chars:
            log.warning("SKILLS_MAX_CHARS=%s reached - skill %r not applied to %s.",
                        SETTINGS.skills_max_chars, skill.name, agent)
            record["skipped"].append({"name": skill.name, "reason": f"over SKILLS_MAX_CHARS={SETTINGS.skills_max_chars}"})
            continue
        for ref_name in ref_names:
            extra = f"\n\n#### Reference: {ref_name}\n{by_name[ref_name].body}"
            if used + len(section) + len(extra) > SETTINGS.skills_max_chars:
                record["skipped"].append({"name": ref_name, "reason": "reference over SKILLS_MAX_CHARS"})
                continue
            section += extra
            record["references"].append(ref_name)
        parts.append(section)
        used += len(section)
        record["loaded"].append(skill.name)
    prompt = ("\n\n## Skills\nApply the following team practices.\n\n" + "\n\n".join(parts)) if parts else ""
    return Selection(prompt=prompt, record=record)


def skills_prompt(agent: str, provider: str, state: Optional[Dict[str, Any]] = None) -> str:
    """The block appended to the agent's system prompt (empty string when none apply)."""
    return select_skills(agent, provider, state).prompt


def skills_used(state: Dict[str, Any], agent: str, provider: str) -> Dict[str, Any]:
    """The run's `skills_used` with this agent's selection added (for the state update)."""
    return {**(state.get("skills_used") or {}), agent: select_skills(agent, provider, state).record}
