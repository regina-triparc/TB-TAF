"""
The PR diff behind a ticket, read from the application's source repository.

Jira's development panel (or a seed ticket's `pull_requests`) says which PRs
implement a ticket; this module fetches them with git into a blobless bare clone
(SOURCE_REPO_LOCAL_PATH, created on first use) and returns their combined unified
diff. git authenticates with the machine's normal sign-in, so no GitHub API token
is needed, and no working copy anyone uses is touched.

A real PR diff is far larger than any prompt budget (TB-601's PR #740 alone is
~370k characters, mostly backend). `condense()` turns it into something a model
can use: the full list of changed files, then the full hunks of the files most
useful for UI tests (templates, then components, under DIFF_FOCUS_PATHS), each
capped, until the budget is spent. Agents call `diff_for_prompt()`.
"""
from __future__ import annotations

import fnmatch
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from taf.config import SETTINGS, project_path
from taf.integrations.github.repository import run_command

log = logging.getLogger("qa.prdiff")

_PR_URL_RE = re.compile(r"github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)", re.IGNORECASE)
_PR_REF_RE = re.compile(r"\b([\w.-]+/[\w.-]+)#(\d+)\b")
_FILE_HEADER_RE = re.compile(r"^diff --git a/(.+?) b/(.+)$", re.MULTILINE)


@dataclass(frozen=True)
class PullRequest:
    number: int
    repo: str            # "owner/name"
    status: str = ""     # MERGED | OPEN | DECLINED | "" when unknown
    url: str = ""

    @property
    def usable(self) -> bool:
        return self.status.upper() not in {"DECLINED", "CLOSED"}


def repo_slug(url: str) -> str:
    """'https://github.com/Owner/Repo.git' -> 'owner/repo' (lowercase, for comparisons)."""
    match = re.search(r"github\.com[/:]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url or "", re.IGNORECASE)
    return match.group(1).lower() if match else ""


def prs_from_text(text: str) -> List[PullRequest]:
    """PRs written in a description as URLs or as owner/repo#123."""
    found = {(m.group(1).lower(), int(m.group(2))) for m in _PR_URL_RE.finditer(text or "")}
    found |= {(m.group(1).lower(), int(m.group(2))) for m in _PR_REF_RE.finditer(text or "")}
    return [PullRequest(number=n, repo=r) for r, n in sorted(found, key=lambda x: x[1])]


def prs_from_seed(items: Iterable[Any], default_repo: str) -> List[PullRequest]:
    """A seed ticket's `pull_requests`: numbers, URLs, or {"number", "repo", "status"} objects."""
    prs: List[PullRequest] = []
    for item in items or []:
        if isinstance(item, int) or (isinstance(item, str) and item.isdigit()):
            prs.append(PullRequest(number=int(item), repo=default_repo))
        elif isinstance(item, str):
            prs += prs_from_text(item)
        elif isinstance(item, dict) and item.get("number"):
            prs.append(PullRequest(number=int(item["number"]), repo=(item.get("repo") or default_repo).lower(),
                                   status=item.get("status", ""), url=item.get("url", "")))
    return prs


# ------------------------------------------------------------------ fetching --
class SourceRepo:
    """Blobless bare clone of the application repo, used only to compute PR diffs."""

    def __init__(self, url: str, local_path: str, base_branch: str) -> None:
        self.url = url
        self.slug = repo_slug(url)
        self.path = project_path(local_path).resolve() if local_path else None
        self.base_branch = base_branch

    @property
    def configured(self) -> bool:
        return bool(self.url and self.slug and self.path)

    def _git(self, *args: str, cwd: Optional[Path] = None, timeout_s: int = 300):
        env = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never"}
        return run_command(["git", *args], cwd=cwd or self.path, timeout_s=timeout_s, env=env)

    def ensure_clone(self) -> None:
        assert self.path is not None
        if (self.path / "HEAD").is_file():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        result = self._git("clone", "--bare", "--filter=blob:none", self.url, str(self.path),
                           cwd=self.path.parent, timeout_s=900)
        if not result.ok:
            raise RuntimeError(f"cannot clone {self.url}: {result.stderr.strip()[-300:]}")

    def diff(self, number: int) -> str:
        """Unified diff of one PR: base..head, or the merge commit's own change if it was merge-committed."""
        ref, base = f"refs/pull/{number}/head", f"refs/heads/{self.base_branch}"
        fetch = self._git("fetch", "--quiet", "origin", f"+{ref}:{ref}", f"+{base}:{base}")
        if not fetch.ok:
            raise RuntimeError(f"cannot fetch PR #{number}: {fetch.stderr.strip()[-300:]}")
        head = self._git("rev-parse", ref).stdout.strip()
        # A merge-committed PR: its change is exactly merge^1..merge^2. Squash and
        # rebase merges leave no such commit; their fork point is the merge-base.
        merges = self._git("log", "--merges", "--format=%H %P", "-n", "2000", base).stdout.splitlines()
        merge = next((line.split()[0] for line in merges if len(line.split()) == 3 and line.split()[2] == head), "")
        if merge:
            frm, to = f"{merge}^1", f"{merge}^2"
        else:
            frm, to = self._git("merge-base", base, head).stdout.strip(), head
        result = self._git("diff", "--no-color", "--no-ext-diff", frm, to, timeout_s=600)
        if not result.ok:
            raise RuntimeError(f"cannot diff PR #{number}: {result.stderr.strip()[-300:]}")
        return result.stdout


def source_repo() -> SourceRepo:
    return SourceRepo(SETTINGS.source_repo_url, SETTINGS.source_repo_local_path, SETTINGS.source_base_branch)


def fetch_ticket_diff(prs: List[PullRequest]) -> Tuple[str, List[str]]:
    """
    Combined diff of the ticket's usable PRs in the configured source repo, and notes
    for the run log. Never raises: a missing clone or access problem returns ("", notes).
    """
    repo = source_repo()
    if not SETTINGS.fetch_pr_diffs:
        return "", ["PR diff: disabled (FETCH_PR_DIFFS=false)"]
    if not repo.configured:
        return "", ["PR diff: SOURCE_REPO_URL not set - running without a diff"]
    wanted = [p for p in prs if p.usable and (not p.repo or p.repo == repo.slug)]
    skipped = [f"#{p.number} ({p.status.lower() or p.repo})" for p in prs if p not in wanted]
    notes = [f"PR diff: skipped {', '.join(skipped)}"] if skipped else []
    if not wanted:
        return "", notes + ["PR diff: no linked PRs in " + repo.slug]
    try:
        repo.ensure_clone()
    except Exception as exc:  # noqa: BLE001 - access problems must not stop the run
        log.warning("PR diff unavailable: %s", exc)
        return "", notes + [f"PR diff unavailable: {exc}"]
    parts: List[str] = []
    for pr in sorted(set(wanted), key=lambda p: p.number):
        try:
            parts.append(repo.diff(pr.number))
            notes.append(f"PR diff: #{pr.number} fetched")
        except Exception as exc:  # noqa: BLE001
            log.warning("PR #%s diff unavailable: %s", pr.number, exc)
            notes.append(f"PR diff: #{pr.number} unavailable ({exc})")
    return "\n".join(parts), notes


# ---------------------------------------------------------------- condensing --
@dataclass
class FileDiff:
    path: str
    text: str
    added: int
    removed: int


def split_files(diff: str) -> List[FileDiff]:
    starts = [m.start() for m in _FILE_HEADER_RE.finditer(diff or "")]
    files: List[FileDiff] = []
    for i, start in enumerate(starts):
        block = diff[start: starts[i + 1] if i + 1 < len(starts) else len(diff)]
        path = _FILE_HEADER_RE.match(block).group(2)
        lines = block.splitlines()
        added = sum(1 for line in lines if line.startswith("+") and not line.startswith("+++"))
        removed = sum(1 for line in lines if line.startswith("-") and not line.startswith("---"))
        files.append(FileDiff(path, block, added, removed))
    return files


def _globs(value: str) -> List[str]:
    return [g.strip() for g in value.split(",") if g.strip()]


def _rank(path: str, focus: List[str]) -> int:
    """Lower is more useful for writing UI tests."""
    name = path.lower()
    in_focus = any(name.startswith(f.lower()) for f in focus) if focus else True
    test_file = bool(re.search(r"(\.spec\.|\.test\.|tests?/|\.tests?\.)", name))
    if name.endswith(".html"):
        kind = 0                       # templates: roles, labels, visible text
    elif name.endswith((".ts", ".tsx", ".js", ".jsx", ".vue")):
        kind = 1
    elif name.endswith((".json",)) and "i18n" in name:
        kind = 1                       # copy text
    elif name.endswith((".scss", ".css", ".json")):
        kind = 3
    else:
        kind = 4                       # backend and everything else
    return (0 if in_focus else 10) + kind + (5 if test_file else 0)


def condense(diff: str, budget: int) -> str:
    """Fit a unified diff into `budget` characters, keeping what matters for UI tests."""
    if not diff or len(diff) <= budget:
        return diff or ""
    files = split_files(diff)
    if not files:  # not a git-style diff: plain head/tail cut
        head = budget // 3
        return f"{diff[:head]}\n... [diff truncated] ...\n{diff[-(budget - head):]}"
    exclude, focus = _globs(SETTINGS.diff_exclude_globs), _globs(SETTINGS.diff_focus_paths)
    shown = sorted((f for f in files if not any(fnmatch.fnmatch(f.path.lower(), g.lower()) for g in exclude)),
                   key=lambda f: (_rank(f.path, focus), -(f.added + f.removed)))

    # Overview first: every file, so the model knows the full scope even when hunks are cut.
    total_add, total_del = sum(f.added for f in files), sum(f.removed for f in files)
    listing = [f"# PR diff condensed for the prompt: {len(files)} files changed (+{total_add} -{total_del}).",
               "# Full hunks follow for the most UI-relevant files; others are listed only."]
    list_budget = budget // 4
    for f in sorted(files, key=lambda f: (_rank(f.path, focus), f.path)):
        line = f"#  +{f.added:<5} -{f.removed:<5} {f.path}"
        if sum(len(x) + 1 for x in listing) + len(line) > list_budget:
            listing.append(f"#  ... and {len(files) - (len(listing) - 2)} more files")
            break
        listing.append(line)
    out = "\n".join(listing) + "\n"

    per_file = max(1200, budget // 4)
    for f in shown:
        remaining = budget - len(out)
        if remaining < 400:
            break
        text = f.text if len(f.text) <= min(per_file, remaining) else \
            f.text[:min(per_file, remaining) - 60] + f"\n... [{f.path}: rest of this file's diff cut]\n"
        out += text if text.endswith("\n") else text + "\n"
    return out[:budget]


def diff_for_prompt(state: Dict[str, Any], share: float, provider: str) -> str:
    """The ticket's diff, condensed to this agent's share of the budget for its provider."""
    budget = int(SETTINGS.max_diff_chars * share)
    if provider == "ollama":
        budget = min(budget, int(SETTINGS.ollama_max_diff_chars * share))
    return condense(state.get("github_diff", ""), max(budget, 1000))
