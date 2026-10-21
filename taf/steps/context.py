"""
Context for the agents, gathered deterministically:

  * gather_context - the ticket's PR diff from the application repository
  * repo_context   - the Playwright test repository's own conventions: config and
                     projects, page objects, fixtures, helpers, an example spec. Read
                     from the base branch, bounded, never written. The test repository
                     is the source of truth for how a spec should look.
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from taf.agents.base import agent_node
from taf.agents.state import QAState
from taf.config import SETTINGS
from taf.core.text import truncate_middle
from taf.integrations.github.pull_requests import PullRequest, fetch_ticket_diff
from taf.integrations.github.repository import REPO_LOCK, TEST_REPO, CommandError
from taf.llm.router import ROUTER

_SKIP_DIRS = {"node_modules", ".git", "test-results", "playwright-report", "test-artifacts", "blob-report", ".auth"}
_CLASS_RE = re.compile(r"export\s+(?:default\s+)?class\s+(\w+)")
_METHOD_RE = re.compile(r"^\s+(?:public\s+)?(?:async\s+)?(?:(get)\s+)?(\w+)\s*\(([^)]*)\)\s*(?::\s*([^{]+?))?\s*\{",
                        re.MULTILINE)
_READONLY_RE = re.compile(r"^\s+(?:public\s+)?readonly\s+(\w+)\s*:", re.MULTILINE)
# A page object whose header comment says it was checked against the live app: that
# comment is its locator map, and is passed to the Automation Engineer verbatim.
_VERIFIED_DOC_RE = re.compile(r"/\*\*((?:(?!\*/).)*?Verified against(?:(?!\*/).)*?)\*/", re.DOTALL)
_PRIVATE_RE = re.compile(r"^\s+private\s+(?:async\s+)?(?:readonly\s+)?(\w+)", re.MULTILINE)
_EXPORT_FN_RE = re.compile(r"export\s+(?:async\s+)?(?:function|const)\s+(\w+)")
_EXTEND_RE = re.compile(r"export\s+const\s+(\w+)\s*=\s*\w+\.extend")
_PROJECT_RE = re.compile(r"name:\s*['\"]([^'\"]+)['\"]")
_NOT_METHODS = {"if", "for", "while", "switch", "catch", "constructor", "function", "return"}


def gather_diff(state: Dict[str, Any]) -> List[str]:
    """Fetch the ticket's PR diff into state['github_diff'] (unless one was given). Returns log notes."""
    if state.get("github_diff"):
        return ["PR diff: provided with the request"]
    prs = [PullRequest(**p) for p in state.get("pull_requests") or []]
    if not prs:
        return ["PR diff: no linked PRs found for this ticket - running without a diff"]
    diff, notes = fetch_ticket_diff(prs)
    state["github_diff"] = diff
    if diff:
        notes.append(f"PR diff: {len(diff):,} characters; condensed per agent before prompting")
    return notes


@agent_node("Context", engine="git", abort_on_error=False, span_type="retriever")
def gather_context(state: QAState) -> Dict[str, Any]:
    work = dict(state)
    notes = gather_diff(work)
    return {"github_diff": work.get("github_diff", ""), "context_notes": notes}


# ------------------------------------------------------------- repo scan --
def _files(root: Path, pattern: str, limit: int = 400) -> List[Path]:
    found: List[Path] = []
    for path in sorted(root.rglob(pattern)):
        if any(part in _SKIP_DIRS for part in path.relative_to(root).parts):
            continue
        found.append(path)
        if len(found) >= limit:
            break
    return found


def _read(path: Path, limit: int = 20_000) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")[:limit]
    except OSError:
        return ""


def _signature(getter: str, name: str, params: str, returns: str) -> str:
    """`openAddInfo(dayLabel: string): Promise<InfoFormPage>`, or `tabs: Locator` for a getter."""
    returns = re.sub(r"\s+", " ", returns or "").strip()
    if getter:
        return f"{name}: {returns}" if returns else name
    params = re.sub(r"\s+", " ", params).strip()
    sig = f"{name}({params})" + (f": {returns}" if returns else "")
    return sig if len(sig) <= 110 else f"{name}(...)" + (f": {returns}" if returns else "")


def _member_name(member: str) -> str:
    return re.match(r"\w+", member).group(0) if re.match(r"\w+", member) else member


def _import_path(spec_dir: Path, target: Path) -> str:
    """The relative module specifier a spec in `spec_dir` would use for `target`."""
    text = Path(os.path.relpath(target.with_suffix(""), spec_dir)).as_posix()
    return text if text.startswith(".") else f"./{text}"


def scan_repository(root: Path, spec_dir_rel: str) -> Dict[str, Any]:
    """Facts about the test repository's conventions. Pure reads, bounded output."""
    spec_dir = (root / spec_dir_rel).resolve()
    facts: Dict[str, Any] = {"available": True, "spec_dir": spec_dir_rel}

    config = next((root / n for n in ("playwright.config.ts", "playwright.config.js", "playwright.config.mjs")
                   if (root / n).is_file()), None)
    facts["playwright_config"] = config.name if config else ""
    config_text = _read(config) if config else ""
    facts["projects"] = sorted(set(_PROJECT_RE.findall(config_text)))[:30]
    facts["config_excerpt"] = truncate_middle(config_text, 1500)

    page_objects = []
    for path in _files(root, "*.ts"):
        rel = path.relative_to(root).as_posix()
        if path.name.endswith(".spec.ts") or path.name.endswith(".d.ts"):
            continue
        is_page = ".page." in path.name or "/pages/" in f"/{rel}" or "page-objects" in rel
        if not is_page:
            continue
        text = _read(path)
        classes = _CLASS_RE.findall(text)
        if not classes:
            continue
        private = set(_PRIVATE_RE.findall(text))
        members = [name for name in _READONLY_RE.findall(text)]
        # Signatures, not bare names: a model that sees `dayTitle` alone calls `.innerText()`
        # on what is really a Promise<string>.
        members += [_signature(getter, name, params, returns) for getter, name, params, returns in _METHOD_RE.findall(text)]
        methods = [m for m in dict.fromkeys(members)
                   if _member_name(m) not in _NOT_METHODS and _member_name(m) not in private][:30]
        ctor = re.search(r"^\s+constructor\s*\(([^)]*)\)", text, re.MULTILINE)
        if ctor:
            params = re.sub(r"\s+", " ", re.sub(r"\b(?:private|public|protected|readonly)\s+", "", ctor.group(1)))
            params = params.strip().rstrip(",").strip()
            methods.insert(0, f"new {classes[0]}({params})")
        entry: Dict[str, Any] = {"path": rel, "import": _import_path(spec_dir, path), "classes": classes,
                                 "methods": methods}
        verified = _VERIFIED_DOC_RE.search(text)
        if verified:
            doc = "\n".join(line.strip().removeprefix("*").rstrip() for line in verified.group(1).splitlines())
            entry["doc"] = truncate_middle(doc.strip(), 1600)
        page_objects.append(entry)
    facts["page_objects"] = page_objects[:40]

    fixtures = []
    for path in _files(root, "*.ts"):
        if path.name.endswith(".spec.ts"):
            continue
        text = _read(path)
        names = _EXTEND_RE.findall(text)
        if names or "fixture" in path.name.lower():
            fixtures.append({"path": path.relative_to(root).as_posix(), "import": _import_path(spec_dir, path),
                             "exports": names or _EXPORT_FN_RE.findall(text)[:8]})
    facts["fixtures"] = fixtures[:20]

    helpers = []
    for path in _files(root, "*.ts"):
        rel = path.relative_to(root).as_posix()
        if path.name.endswith(".spec.ts") or not re.search(r"(^|/)(helpers|utils)/", rel):
            continue
        exports = _EXPORT_FN_RE.findall(_read(path))[:8]
        if exports:
            helpers.append({"path": rel, "import": _import_path(spec_dir, path), "exports": exports})
    facts["helpers"] = helpers[:25]

    specs = [p.relative_to(root).as_posix() for p in _files(root, "*.spec.ts")]
    facts["existing_specs"] = specs[:60]
    # One existing spec outside the agent's own folder, as a style example.
    example = next((root / s for s in specs if not s.startswith(spec_dir_rel.strip("/") + "/")), None)
    facts["example_spec"] = {"path": example.relative_to(root).as_posix(),
                             "excerpt": truncate_middle(_read(example), 1800)} if example else {}
    return facts


def render_repo_context(facts: Dict[str, Any], limit: int) -> str:
    """The conventions as a prompt section for the Automation Engineer."""
    if not facts.get("available"):
        return ""
    lines = ["## Test repository conventions (authoritative - follow them)",
             f"Spec folder: `{facts.get('spec_dir')}`. Playwright config: `{facts.get('playwright_config') or '-'}`;"
             f" projects: {', '.join(facts.get('projects') or []) or '-'}."]
    if facts.get("page_objects"):
        # Verified ones first: they are the most useful part, and the section is truncated from the middle.
        verified = [po for po in facts["page_objects"] if po.get("doc")]
        if verified:
            lines += ["", "### Verified page objects - what they wrap (checked against the live app)"]
            for po in verified:
                lines += ["", f"`{', '.join(po['classes'])}` (`{po['path']}`):", "```", po["doc"], "```"]
        lines += ["", "### Page objects (import with the path shown; reuse instead of raw locators)"]
        for po in sorted(facts["page_objects"], key=lambda po: not po.get("doc")):
            methods = f" - {', '.join(po['methods'])}" if po.get("methods") else ""
            lines.append(f"- `import {{ {', '.join(po['classes'])} }} from '{po['import']}'`{methods}")
    if facts.get("fixtures"):
        lines += ["", "### Fixtures"]
        lines += [f"- `{f['import']}` exports {', '.join(f['exports']) or '(see file)'}" for f in facts["fixtures"]]
    if facts.get("helpers"):
        lines += ["", "### Helpers"]
        lines += [f"- `{h['import']}`: {', '.join(h['exports'])}" for h in facts["helpers"]]
    if facts.get("example_spec"):
        lines += ["", f"### Example spec in this repository (`{facts['example_spec']['path']}`)",
                  "```typescript", facts["example_spec"]["excerpt"], "```"]
    return truncate_middle("\n".join(lines), limit)


@agent_node("Repository Context", engine="git", abort_on_error=False, span_type="retriever")
def repo_context(state: QAState) -> Dict[str, Any]:
    """
    Make the test repository ready (clone, base branch, dependencies) and read its
    conventions. Failures are recorded, not fatal: the spec is still generated, and
    validation reports why it could not run.
    """
    if ROUTER.resolve(state.get("llm_provider")) == "offline":
        return {"repo_context": {"available": False, "reason": "offline provider - test repository not used"}}
    logs: List[str] = []
    facts: Dict[str, Any]
    with REPO_LOCK:
        try:
            TEST_REPO.ensure_ready(logs)
            TEST_REPO.sync_base(logs, fetch=True)
            deps_ok = TEST_REPO.install_dependencies(state.get("target_url", ""), logs)
            assert TEST_REPO.path is not None
            facts = scan_repository(TEST_REPO.path.resolve(), SETTINGS.test_spec_dir)
            facts["deps_ok"] = deps_ok
            facts["has_tsc"] = TEST_REPO.has_tool("tsc")
            facts["has_eslint"] = TEST_REPO.has_tool("eslint")
        except CommandError as exc:
            if exc.result is not None:
                logs.append(exc.result.as_log())
            logs.append(f"[error] {exc}")
            facts = {"available": False, "reason": str(exc)}
    facts["prompt"] = render_repo_context(facts, SETTINGS.repo_context_max_chars)
    return {"repo_context": facts, "execution_logs": append_log(state, logs)}


def append_log(state: QAState, logs: List[str], limit: int = 200_000) -> str:
    """Accumulate a step's command log onto the run's execution log (bounded)."""
    combined = "\n\n".join(x for x in [state.get("execution_logs", ""), *logs] if x)
    return truncate_middle(combined, limit)


def repo_available(state: QAState) -> Optional[str]:
    """None when validation can use the test repository; otherwise why not."""
    facts = state.get("repo_context") or {}
    if not facts.get("available"):
        return facts.get("reason") or "test repository unavailable"
    if not facts.get("deps_ok"):
        return "test repository dependencies are not installed (npm/pnpm install failed)"
    return None
