"""
Test harness for the workflow: a throwaway test repository with its own bare
`origin`, model calls answered by the offline provider (plans) and a canned spec,
and the slow commands (npm install, Playwright) stubbed. Nothing leaves the machine.
"""
import contextlib
import dataclasses
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from langchain_core.messages import AIMessage

from taf.config import SETTINGS
from taf.integrations.github.pr_api import PullRequestRef
from taf.integrations.github.repository import TEST_REPO, CommandResult
from taf.llm import offline
from taf.llm.router import ROUTER

SPEC = """import { test, expect } from '@playwright/test';
test.describe('WF', () => {
  test('[TC-001] first', async ({ page }) => { await expect(page).toHaveURL(/.+/); });
  test('[TC-002] second', async ({ page }) => { await expect(page).toHaveURL(/.+/); });
  test('[TC-003] third', async ({ page }) => { await expect(page).toHaveURL(/.+/); });
});
"""

DESCRIPTION = """ACCEPTANCE CRITERIA
1. The first thing works.
2. The second thing works.
3. The third thing works.
"""

GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(cwd, *args) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env={**os.environ, **GIT_ENV}).stdout


def answer(code: str = SPEC) -> AIMessage:
    return AIMessage(content=f"```typescript\n{code}```")


def write_json_report(path: Path, outcomes: Dict[str, str]) -> None:
    """A minimal Playwright JSON report: one spec per [TC-xxx] title with the given status."""
    import json
    status = {"passed": "expected", "failed": "unexpected", "skipped": "skipped"}
    specs = [{"title": f"[{tc}] case", "tests": [{"projectName": "qa-agent", "status": status[s],
                                                    "results": [{"duration": 10, "error": {"message": f"{tc} broke"}
                                                                 if s == "failed" else None, "attachments": []}]}]}
             for tc, s in outcomes.items()]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"suites": [{"title": "spec", "specs": specs, "suites": []}]}), encoding="utf-8")


def ok_result(args=("playwright",), ok: bool = True, out: str = "") -> CommandResult:
    return CommandResult(args=list(args), returncode=0 if ok else 1, stdout=out, stderr="" if ok else out,
                         duration_s=0.1)


@contextlib.contextmanager
def patched_settings(**overrides: Any):
    """Replace SETTINGS in every taf module that imported it (it is a frozen dataclass)."""
    new = dataclasses.replace(SETTINGS, **overrides)
    with contextlib.ExitStack() as stack:
        for name, module in list(sys.modules.items()):
            if name.startswith("taf") and getattr(module, "SETTINGS", None) is SETTINGS:
                stack.enter_context(mock.patch.object(module, "SETTINGS", new))
        stack.enter_context(mock.patch.object(TEST_REPO, "settings", new))
        stack.enter_context(mock.patch.object(ROUTER, "settings", new))
        yield new


class FakePullRequests:
    """Stands in for PullRequestClient: remembers what it was asked, opens each branch's PR once."""
    instances: List["FakePullRequests"] = []
    open_prs: Dict[str, PullRequestRef] = {}
    created: List[Dict[str, Any]] = []

    def __init__(self, *_: Any) -> None:
        FakePullRequests.instances.append(self)

    def ensure_pull_request(self, branch: str, title: str, body: str, draft: bool) -> PullRequestRef:
        if branch in self.open_prs:
            existing = self.open_prs[branch]
            return PullRequestRef(existing.number, existing.url, False, existing.draft)
        number = 100 + len(self.created)
        ref = PullRequestRef(number, f"https://github.example/pr/{number}", True, draft)
        FakePullRequests.open_prs[branch] = ref
        FakePullRequests.created.append({"branch": branch, "title": title, "body": body, "draft": draft})
        return ref

    @classmethod
    def reset(cls) -> None:
        cls.instances, cls.open_prs, cls.created = [], {}, []


class Workbench:
    """
    One temp test repository (+ bare origin) and the mocks around a workflow run.
    Configure the stubs through attributes before calling `start()`.
    """

    def __init__(self, **settings: Any) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="taf-wb-"))
        self.origin = self.root / "origin.git"
        self.repo = self.root / "tests-repo"
        subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(self.origin)], check=True)
        subprocess.run(["git", "clone", "-q", str(self.origin), str(self.repo)], check=True, capture_output=True)
        (self.repo / "e2e" / "qa").mkdir(parents=True)
        (self.repo / "e2e" / "pages").mkdir(parents=True)
        (self.repo / "e2e" / "pages" / "landing.page.ts").write_text(
            "export class LandingPage {\n  async open() {\n  }\n}\n")
        (self.repo / "package.json").write_text('{"devDependencies": {"@playwright/test": "^1"}}\n')
        (self.repo / "README.md").write_text("tests\n")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "init")
        git(self.repo, "push", "-q", "origin", "main")
        self.settings = dict(test_repo_local_path=str(self.repo), test_repo_url="https://github.com/acme/e2e.git",
                             test_spec_dir="e2e/qa", test_repo_base_branch="main", git_push_enabled=False,
                             pr_create_enabled=False, require_approval=True, no_git=False, spec_repair_attempts=2,
                             review_with_llm=False, live_snapshot_enabled=False, run_repair_attempts=0)
        self.settings.update(settings)
        self.specs: List[str] = [SPEC]          # answers, in order (the last one repeats)
        self.list_ok: List[bool] = [True]        # `playwright --list` results, in order (last repeats)
        self.play_ok: bool = True
        # Per-run test outcomes for the JSON report, in order (the last repeats):
        # {"TC-001": "passed" | "failed" | "skipped"}. None derives them from play_ok.
        self.play_results: Optional[List[Dict[str, str]]] = None
        self.play_grep: List[Optional[str]] = []
        self.play_raises: Optional[BaseException] = None
        self.deps_ok = True
        self.calls: Dict[str, int] = {"text": 0, "list": 0, "play": 0}
        self.text_messages: List[Any] = []
        self._stack = contextlib.ExitStack()
        FakePullRequests.reset()

    # ---- stubs ---------------------------------------------------------------
    def _text(self, state, route, messages):
        self.text_messages.append(messages)
        code = self.specs[min(self.calls["text"], len(self.specs) - 1)]
        self.calls["text"] += 1
        return answer(code)

    def _list(self, spec_path, target_url):
        ok = self.list_ok[min(self.calls["list"], len(self.list_ok) - 1)]
        self.calls["list"] += 1
        return ok_result(ok=ok, out="Total: 3 tests in 1 file" if ok else "SyntaxError: Unexpected token (3:5)")

    def _play(self, spec_path, target_url, logs, json_report=None, lines=None):
        self.calls["play"] += 1
        selected = None
        if lines:
            source = Path(spec_path).read_text(encoding="utf-8").splitlines()
            selected = {tc for n in lines for tc in ("TC-001", "TC-002", "TC-003") if f"[{tc}]" in source[n - 1]}
        self.play_grep.append(sorted(selected) if selected is not None else None)
        if self.play_raises is not None:
            exc, self.play_raises = self.play_raises, None
            raise exc
        if self.play_results is not None:
            outcomes = self.play_results[min(self.calls["play"] - 1, len(self.play_results) - 1)]
        else:
            outcomes = {"TC-001": "passed" if self.play_ok else "failed", "TC-002": "passed", "TC-003": "passed"}
        if selected is not None:
            outcomes = {tc: s for tc, s in outcomes.items() if tc in selected}
        if json_report is not None:
            write_json_report(json_report, outcomes)
        ok = all(s != "failed" for s in outcomes.values())
        failed = sum(s == "failed" for s in outcomes.values())
        logs.append(f"{len(outcomes) - failed} passed" if ok else f"{failed} failed\n{len(outcomes) - failed} passed")
        return ok

    def start(self) -> "Workbench":
        s = self._stack
        s.enter_context(patched_settings(**self.settings))
        s.enter_context(mock.patch.object(TEST_REPO, "path", self.repo.resolve()))
        s.enter_context(mock.patch.object(ROUTER, "structured",
                                          side_effect=lambda st, route, msgs, schema: offline.structured(route.key, st, schema)))
        s.enter_context(mock.patch.object(ROUTER, "text", side_effect=self._text))
        s.enter_context(mock.patch.object(TEST_REPO, "install_dependencies", side_effect=lambda *_: self.deps_ok))
        s.enter_context(mock.patch.object(TEST_REPO, "list_tests", side_effect=self._list))
        s.enter_context(mock.patch.object(TEST_REPO, "typecheck", return_value=None))
        s.enter_context(mock.patch.object(TEST_REPO, "lint", return_value=None))
        s.enter_context(mock.patch.object(TEST_REPO, "run_playwright", side_effect=self._play))
        s.enter_context(mock.patch("taf.steps.publish.PullRequestClient", FakePullRequests))
        # The report's local model would be the real Ollama: use the deterministic fallback.
        s.enter_context(mock.patch.object(ROUTER, "report_model", side_effect=RuntimeError("no model in tests")))
        return self

    def close(self) -> None:
        self._stack.close()
        shutil.rmtree(self.root, ignore_errors=True)

    # ---- helpers -------------------------------------------------------------
    def remote_files(self, branch: str) -> List[str]:
        out = subprocess.run(["git", "ls-tree", "-r", "--name-only", branch], cwd=self.origin,
                             capture_output=True, text=True)
        return out.stdout.split() if out.returncode == 0 else []

    def remote_commits(self, branch: str) -> int:
        out = subprocess.run(["git", "rev-list", "--count", branch], cwd=self.origin, capture_output=True, text=True)
        return int(out.stdout.strip()) if out.returncode == 0 else 0

    def worktree_clean(self) -> bool:
        return git(self.repo, "status", "--porcelain").strip() == ""
