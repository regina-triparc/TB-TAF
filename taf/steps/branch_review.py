"""
`python -m taf review <branch> [--base main]`: the checks a generated spec has to pass,
applied to any branch of the test repository - hand-written specs included.

Read-only. It fetches, diffs `<base>...<branch>` (three dots: only the branch's own
commits count) and reads each changed spec from git, so the working tree is never
touched and nothing is committed, pushed or approved.

    static checks   the same code checks and review rules TB-TAF applies to its own
                    specs (taf/steps/validation.py, taf/steps/review.py), each finding
                    at its line and marked new (a line the branch changed) or pre-existing
    tsc / ESLint    only when the branch is checked out with a clean working tree (the
                    tools read files from disk); type errors in files the branch did not
                    change are listed separately - a changed type can break them
    --run           runs the changed specs (same condition); environment failures
                    (missing env var, login, network) are reported apart from test failures
    --llm           a model review of the diff, through the router; every finding must
                    cite a rule, a bug or a missing case, and file:line

The result is a Markdown report, printed and written to REPORTS_DIR.
"""
from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional, Pattern, Set, Tuple

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from taf.config import SETTINGS, project_path
from taf.core.text import truncate_middle, utc_now
from taf.integrations.github.repository import TEST_REPO, CommandResult, run_command
from taf.steps import review as rules
from taf.steps.playwright_results import parse_json_report, report_path
from taf.steps.validation import _diagnostics_for, code_checks

# Where each review rule's finding sits in the code, to report it at a line.
RULE_LINES: Dict[str, List[Pattern[str]]] = {
    "no fixed sleeps": [re.compile(r"\bwaitForTimeout\s*\(|new Promise\(\s*\w*\s*=>\s*setTimeout")],
    "no .only": [re.compile(r"\b(test|describe)\.only\s*\(")],
    "no hard-coded secrets": [rules._SECRET_RE],
    "no hard-coded data": [rules._OPEN_TRIP_ARG_RE, rules._DAY_LITERAL_RE],
    "trip-level checks need their own trip": [rules._TRIP_LEVEL_RE],
    "no assumed URLs": [rules._ASSUMED_URL_RE],
    "trimmed text": [rules._ANCHORED_TEXT_RE],
    "no hard-coded dates": [rules._DATE_LITERAL_RE],
    "lean spec": [rules._TOP_LEVEL_FN_RE],
    "named, scoped locators": [rules._UNNAMED_ROLE_RE],
    "option values from the UI": [rules._TWELVE_HOUR_OPTION_RE],
    "user-facing locators": [rules._RAW_LOCATOR_RE],
}
# A failure caused by the environment, not by the test: fix the setup, not the spec.
_ENV_FAILURE_RE = re.compile(
    r"(?i)(?:env(?:ironment)? var\w*|process\.env\.\w+)[^\n]*(?:not set|missing|undefined|required)"
    r"|is not set|storageState|ENOENT[^\n]*\.auth|net::ERR_|ECONNREFUSED|ENOTFOUND|getaddrinfo"
    r"|\b401\b|\bunauthori[sz]ed\b|login page|sign[- ]in page")
_HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", re.MULTILINE)


class ReviewError(RuntimeError):
    """The review cannot start (no repository, unknown branch, nothing to diff)."""


@dataclass
class SpecResult:
    path: str
    status: str                                  # A (added) | M (modified) | R (renamed)
    checks: List[Dict[str, Any]] = field(default_factory=list)
    findings: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class BranchReview:
    branch: str
    base: str
    branch_ref: str
    base_ref: str
    changed: List[Tuple[str, str]]
    specs: List[SpecResult]
    tools: List[Dict[str, Any]] = field(default_factory=list)
    other_type_errors: List[str] = field(default_factory=list)
    run: Dict[str, Any] = field(default_factory=dict)
    llm: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    @property
    def blockers(self) -> int:
        """Blockers on lines the branch changed (or failed checks): what the branch introduces."""
        found = sum(1 for s in self.specs for f in s.findings if f["severity"] == "blocker" and f["where"] != "pre-existing")
        failed = sum(1 for s in self.specs for c in s.checks if not c["ok"] and not c.get("skipped"))
        return found + failed + sum(1 for t in self.tools if t.get("ok") is False)


# ------------------------------------------------------------------- git --
def _git(repo: Path, *args: str, check: bool = True) -> CommandResult:
    return run_command(["git", *args], cwd=repo, timeout_s=SETTINGS.git_timeout_s,
                       env=TEST_REPO._git_env(), check=check)


def _ref_exists(repo: Path, ref: str) -> bool:
    return _git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False).ok


def resolve_ref(repo: Path, name: str, prefer_remote: bool) -> str:
    """origin/<name> or <name>, whichever exists (remote first for the base, local first for the branch)."""
    order = [f"origin/{name}", name] if prefer_remote else [name, f"origin/{name}"]
    for ref in order:
        if _ref_exists(repo, ref):
            return ref
    raise ReviewError(f"No branch {name!r} in {repo} (tried {', '.join(order)}).")


def changed_files(repo: Path, base_ref: str, branch_ref: str) -> List[Tuple[str, str]]:
    """(status, path) for every file the branch changes, deletions left out."""
    out = _git(repo, "diff", "--name-status", "-M", f"{base_ref}...{branch_ref}").stdout
    files = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and not parts[0].startswith("D"):
            files.append((parts[0][0], parts[-1]))
    return files


def added_lines(repo: Path, base_ref: str, branch_ref: str, path: str) -> Set[int]:
    """Line numbers (in the branch's version) the branch added or changed in `path`."""
    diff = _git(repo, "diff", "-U0", f"{base_ref}...{branch_ref}", "--", path).stdout
    lines: Set[int] = set()
    for start, count in _HUNK_RE.findall(diff):
        n = int(count) if count else 1
        lines.update(range(int(start), int(start) + n))
    return lines


# ---------------------------------------------------------------- checks --
def locate(rule: str, code: str) -> List[int]:
    """1-based lines where a rule's pattern occurs, comments left out."""
    patterns = RULE_LINES.get(rule) or []
    found = []
    for n, line in enumerate(code.splitlines(), start=1):
        stripped = line.strip()
        if stripped.startswith(("//", "*", "/*")):
            continue
        if any(p.search(line) for p in patterns):
            found.append(n)
    return found


def review_code(path: str, code: str, changed: Set[int], repo_root: Optional[Path]) -> SpecResult:
    """The static checks and review rules on one spec, findings placed at their lines."""
    result = SpecResult(path=path, status="")
    result.checks = [c for c in code_checks(code, path, repo_root)]
    for f in rules.rule_review(code, [], [], {}):
        lines = locate(f["rule"], code)
        where = ("new" if any(n in changed for n in lines) else "pre-existing") if lines else "file"
        result.findings.append({**f, "lines": lines, "where": where})
    return result


def _checked_out(repo: Path, branch_ref: str) -> Tuple[bool, str]:
    """Can tools read the branch from disk? Only when it is checked out and the tree is clean."""
    head = _git(repo, "rev-parse", "HEAD", check=False).stdout.strip()
    tip = _git(repo, "rev-parse", branch_ref, check=False).stdout.strip()
    if not head or head != tip:
        return False, f"{branch_ref} is not checked out - tsc, ESLint and --run read files from disk"
    if _git(repo, "status", "--porcelain", check=False).stdout.strip():
        return False, "the working tree has uncommitted changes - tsc, ESLint and --run would not see the branch as committed"
    return True, ""


def tool_checks(repo: Path, review: BranchReview, target_url: str) -> None:
    ts = [repo / p for _, p in review.changed if p.endswith((".ts", ".tsx"))]
    if not ts:
        return
    with _repo_path(repo):
        typed = TEST_REPO.typecheck(ts[0], target_url)
        if typed is None:
            review.tools.append({"name": "tsc --noEmit", "ok": None, "detail": "no TypeScript compiler / tsconfig in the repo"})
        else:
            rel = [p for _, p in review.changed if p.endswith((".ts", ".tsx"))]
            ours = _diagnostics_for(typed, rel)
            others = [line for line in f"{typed.stdout}\n{typed.stderr}".splitlines()
                      if re.search(r"\berror TS\d+", line) and line not in ours]
            review.tools.append({"name": "tsc --noEmit", "ok": not ours and not typed.timed_out,
                                 "detail": "\n".join(ours[:40]) or ("timed out" if typed.timed_out else "")})
            review.other_type_errors = others[:40]
        linted = TEST_REPO.lint(ts, target_url)
        if linted is None:
            review.tools.append({"name": "eslint", "ok": None, "detail": "no ESLint set up in the repo"})
        else:
            rel = [p for _, p in review.changed if p.endswith((".ts", ".tsx"))]
            ours = _diagnostics_for(linted, rel)
            review.tools.append({"name": "eslint", "ok": linted.ok and linted.returncode != 2,
                                 "detail": "\n".join(ours[:40]) or truncate_middle(linted.stdout, 2000)})


def run_specs(repo: Path, review: BranchReview, target_url: str) -> None:
    """Run the changed specs; split environment failures from test failures."""
    run_id = f"review-{uuid.uuid4().hex[:8]}"
    tests: List[Dict[str, Any]] = []
    with _repo_path(repo):
        for spec in review.specs:
            logs: List[str] = []
            report = report_path(run_id, Path(spec.path).stem)
            TEST_REPO.run_playwright(repo / spec.path, target_url, logs, json_report=report)
            for t in parse_json_report(report):
                tests.append({**t, "spec": spec.path})
            if not report.is_file():
                tests.append({"spec": spec.path, "title": "(no report)", "status": "failed",
                              "error": truncate_middle("\n".join(logs), 1500)})
    failed = [t for t in tests if t["status"] == "failed"]
    env = [t for t in failed if _ENV_FAILURE_RE.search(t.get("error") or "")]
    review.run = {"passed": sum(t["status"] == "passed" for t in tests),
                  "skipped": sum(t["status"] == "skipped" for t in tests),
                  "environment_failures": env, "test_failures": [t for t in failed if t not in env]}


class LLMFinding(BaseModel):
    severity: Literal["blocker", "major", "minor"]
    cites: str = Field(description="The rule broken, the bug it would miss, or the missing case. Required.")
    file: str = Field(description="Path of the spec, as in the diff.")
    line: int = Field(description="Line in the branch's version of the file.")
    issue: str
    suggestion: str


class LLMBranchReview(BaseModel):
    summary: str = Field(description="Two sentences: is this branch fit to merge, and why.")
    findings: List[LLMFinding] = Field(default_factory=list)


def llm_review(repo: Path, review: BranchReview, provider: Optional[str]) -> None:
    from taf.llm.router import ROUTER

    route = ROUTER.route("review", provider)
    if route.provider == "offline":
        review.llm = {"skipped": "offline provider - no model review"}
        return
    paths = [s.path for s in review.specs]
    diff = _git(repo, "diff", f"{review.base_ref}...{review.branch_ref}", "--", *paths).stdout
    rule_names = ", ".join(sorted(RULE_LINES))
    system = rules.REVIEW_PROMPT + (
        "\n\nYou are reviewing a branch's diff of hand-written specs. Report a finding only when you can cite "
        f"a rule ({rule_names}, or one above), a bug the test would miss, or a missing case - and give the file and "
        "the line in the branch's version. No style opinions without a citation.")
    state = {"run_id": f"review-{uuid.uuid4().hex[:8]}", "ticket_id": review.branch, "llm_provider": provider or ""}
    result = ROUTER.structured(state, route, [SystemMessage(content=system),
                                              HumanMessage(content=f"```diff\n{truncate_middle(diff, 60_000)}\n```")],
                               LLMBranchReview)
    assert isinstance(result, LLMBranchReview)
    kept = [f.model_dump() for f in result.findings if f.cites.strip() and f.file in paths and f.line > 0]
    review.llm = {"model": route.model, "summary": result.summary, "findings": kept,
                  "dropped": len(result.findings) - len(kept)}


class _repo_path:
    """Point TEST_REPO at the reviewed repository for the tool helpers, then restore it."""

    def __init__(self, repo: Path) -> None:
        self.repo, self.saved = repo, None

    def __enter__(self) -> None:
        self.saved = TEST_REPO.path
        TEST_REPO.path = self.repo

    def __exit__(self, *_: Any) -> None:
        TEST_REPO.path = self.saved


# ------------------------------------------------------------------- run --
def review_branch(branch: str, base: str = "", repo: Optional[Path] = None, fetch: bool = True,
                  run: bool = False, llm: bool = False, provider: Optional[str] = None,
                  target_url: str = "") -> BranchReview:
    repo = (repo or TEST_REPO.path)
    if repo is None or not (Path(repo) / ".git").exists():
        raise ReviewError(f"No git repository at {repo} (TEST_REPO_LOCAL_PATH or --test-repo).")
    repo = Path(repo).resolve()
    base = base or SETTINGS.test_repo_base_branch
    notes: List[str] = []
    if fetch:
        if "origin" in _git(repo, "remote", check=False).stdout.split():
            fetched = _git(repo, "fetch", "--quiet", "origin", check=False)
            if not fetched.ok:
                notes.append(f"git fetch failed - reviewing the refs as they are locally: {fetched.stderr.strip()[:200]}")
        else:
            notes.append("no 'origin' remote - nothing fetched")
    base_ref = resolve_ref(repo, base, prefer_remote=True)
    branch_ref = resolve_ref(repo, branch, prefer_remote=False)
    changed = changed_files(repo, base_ref, branch_ref)
    review = BranchReview(branch=branch, base=base, branch_ref=branch_ref, base_ref=base_ref,
                          changed=changed, specs=[], notes=notes)
    on_disk, why_not = _checked_out(repo, branch_ref)
    for status, path in changed:
        if not path.endswith(".spec.ts"):
            continue
        code = _git(repo, "show", f"{branch_ref}:{path}").stdout
        result = review_code(path, code, added_lines(repo, base_ref, branch_ref, path), repo if on_disk else None)
        result.status = status
        review.specs.append(result)
    if not on_disk:
        notes.append(f"Imports were not resolved and tsc / ESLint / --run did not run: {why_not}.")
    target_url = target_url or SETTINGS.dashboard_target_url
    if on_disk:
        tool_checks(repo, review, target_url)
    if run:
        if on_disk:
            run_specs(repo, review, target_url)
        else:
            review.run = {"skipped": why_not}
    if llm and review.specs:
        try:
            llm_review(repo, review, provider)
        except Exception as exc:  # noqa: BLE001 - an optional reviewer must not fail the review
            review.llm = {"skipped": f"{type(exc).__name__}: {exc}"}
    return review


def to_markdown(review: BranchReview) -> str:
    lines = [f"# Review of `{review.branch}` against `{review.base}`",
             f"_{utc_now()} - `{review.base_ref}...{review.branch_ref}` - read-only: nothing was committed, "
             "pushed or approved_", ""]
    specs = review.specs
    lines.append(f"**Changed files:** {len(review.changed)} | **changed specs:** {len(specs)} | "
                 f"**blocking problems introduced:** {review.blockers}")
    for note in review.notes:
        lines.append(f"> {note}")
    if not specs:
        lines += ["", "No spec files changed on this branch."]
    for spec in specs:
        lines += ["", f"## `{spec.path}` ({ {'A': 'added', 'M': 'modified', 'R': 'renamed'}.get(spec.status, spec.status) })"]
        failed = [c for c in spec.checks if not c["ok"] and not c.get("skipped")]
        lines.append("**Static checks:** " + ("all passed" if not failed else
                                             "; ".join(f"FAILED {c['name']}: {c['detail']}" for c in failed)))
        if not spec.findings:
            lines.append("**Review rules:** no findings")
        for f in sorted(spec.findings, key=lambda f: (f["where"] != "new", f["severity"] != "blocker")):
            at = ", ".join(f"{spec.path}:{n}" for n in f["lines"][:8]) or spec.path
            lines.append(f"- **{f['severity']}** ({f['where']}) {f['rule']} - {f['detail']}  \n  at {at}")
    if review.tools:
        lines += ["", "## Type check and lint (changed files)"]
        for t in review.tools:
            state = "skipped" if t["ok"] is None else "ok" if t["ok"] else "FAILED"
            lines.append(f"- {state}: {t['name']}" + (f"\n```\n{t['detail']}\n```" if t["detail"] and t["ok"] is not True else ""))
    if review.other_type_errors:
        lines += ["", "## Type errors in files this branch did not change",
                  "_A changed type or helper can break them; they may also be pre-existing._", "```",
                  *review.other_type_errors, "```"]
    if review.run:
        lines += ["", "## Run of the changed specs"]
        if review.run.get("skipped"):
            lines.append(f"not run: {review.run['skipped']}")
        else:
            lines.append(f"{review.run['passed']} passed, {review.run['skipped']} skipped, "
                         f"{len(review.run['test_failures'])} test failure(s), "
                         f"{len(review.run['environment_failures'])} environment failure(s)")
            for label, key in (("Environment (fix the setup, not the spec)", "environment_failures"),
                               ("Test failures", "test_failures")):
                if review.run[key]:
                    lines += [f"**{label}:**"] + [f"- `{t['spec']}` {t['title']}: {(t.get('error') or '')[:300]}"
                                                  for t in review.run[key]]
    if review.llm:
        lines += ["", "## Model review"]
        if review.llm.get("skipped"):
            lines.append(f"skipped: {review.llm['skipped']}")
        else:
            lines.append(f"_{review.llm['model']}_ - {review.llm['summary']}")
            lines += [f"- **{f['severity']}** {f['file']}:{f['line']} - {f['issue']} (cites: {f['cites']}) "
                      f"- {f['suggestion']}" for f in review.llm["findings"]]
            if review.llm.get("dropped"):
                lines.append(f"_{review.llm['dropped']} finding(s) without a citation or a file:line were dropped._")
    return "\n".join(lines) + "\n"


def write_report(review: BranchReview) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", review.branch).strip("-") or "branch"
    out = project_path(SETTINGS.reports_dir) / f"review-{slug}-{utc_now().replace(':', '').replace('-', '')[:15]}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_markdown(review), encoding="utf-8")
    return out


def as_json(review: BranchReview) -> str:
    from dataclasses import asdict
    return json.dumps(asdict(review), indent=2, default=str)
