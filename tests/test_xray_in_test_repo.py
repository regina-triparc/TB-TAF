"""Xray test cases are written to the test repo next to the spec, and exported from there."""
import contextlib
import dataclasses
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from langchain_core.messages import AIMessage

from taf.agents.automation import automation_engineer
from taf.cli import main as cli
from taf.config import SETTINGS
from taf.integrations.github.repository import TEST_REPO
from taf.integrations.jira import xray
from taf.llm.router import ROUTER
from taf.runs import stages

SPEC = """import { test, expect } from '@playwright/test';
test.describe('TB-601', () => {
  test('[TC-001] x', async ({ page }) => { await expect(page).toHaveURL(/.+/); });
});
"""


def git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                   env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"})


def planned_state():
    """A TB-601 state that has been through triage + architect (offline plan)."""
    state = stages.prepare("TB-601", "architect", source="seed", provider="offline")
    state = stages.execute(state, "architect")
    state["llm_provider"] = "ollama"  # the automation route under test; its model call is stubbed
    return state


class XrayFilesTest(unittest.TestCase):
    def test_ticket_files(self):
        files = xray.ticket_files(planned_state())
        self.assertEqual(sorted(files), ["TB-601.test-plan.md", "TB-601.xray.csv", "TB-601.xray.json"])
        self.assertTrue(json.loads(files["TB-601.xray.json"]))
        self.assertIn("python -m taf xray export TB-601", files["TB-601.test-plan.md"])
        self.assertEqual(xray.json_file("TB-601"), "xray/TB-601/TB-601.xray.json")

    def test_no_plan_no_files(self):
        self.assertEqual(xray.ticket_files({"ticket_id": "X-1"}), {})


class AutomationWritesXrayTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="taf-testrepo-"))
        git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "e2e" / "qa").mkdir(parents=True)
        (self.repo / "README.md").write_text("test repo\n")
        git(self.repo, "add", "-A"); git(self.repo, "commit", "-q", "-m", "init")
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)

    def run_automation(self, **settings):
        patched = dataclasses.replace(SETTINGS, test_repo_local_path=str(self.repo), test_spec_dir="e2e/qa",
                                      git_push_enabled=False, **settings)
        state = planned_state()
        with mock.patch.object(TEST_REPO, "settings", patched), mock.patch.object(TEST_REPO, "path", self.repo), \
                mock.patch.object(ROUTER, "text", return_value=AIMessage(content=f"```typescript\n{SPEC}```")), \
                mock.patch.object(TEST_REPO, "install_dependencies", return_value=False):
            return automation_engineer(state)

    def test_in_place_writes_spec_and_xray_uncommitted(self):
        update = self.run_automation(no_git=True)
        self.assertTrue((self.repo / "e2e/qa/TB-601.spec.ts").is_file())
        for name in ("TB-601.xray.json", "TB-601.xray.csv", "TB-601.test-plan.md"):
            self.assertTrue((self.repo / "xray/TB-601" / name).is_file(), name)
        self.assertEqual(update["xray_files"][0].split("/")[:2], ["xray", "TB-601"])
        status = subprocess.run(["git", "status", "--porcelain"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertIn("e2e/", status)  # left for the person, not committed

    def test_managed_clone_commits_spec_and_xray_together(self):
        self.run_automation(no_git=False)
        branch = subprocess.run(["git", "branch", "--show-current"], cwd=self.repo, capture_output=True, text=True).stdout
        self.assertEqual(branch.strip(), "qa/TB-601")
        files = subprocess.run(["git", "show", "--name-only", "--format=%s", "HEAD"], cwd=self.repo,
                               capture_output=True, text=True).stdout.splitlines()
        self.assertIn("Playwright spec and Xray test cases", files[0])
        self.assertIn("e2e/qa/TB-601.spec.ts", files)
        self.assertIn("xray/TB-601/TB-601.xray.json", files)

    def test_export_reads_merged_version_and_is_dry_run(self):
        self.run_automation(no_git=False)
        git(self.repo, "checkout", "-q", "main")
        git(self.repo, "merge", "-q", "--no-ff", "-m", "merge", "qa/TB-601")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli(["xray", "export", "TB-601", "--ref", "main", "--test-repo", str(self.repo)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("main:xray/TB-601/TB-601.xray.json", out.getvalue())
        self.assertIn("Dry run: nothing was sent", out.getvalue())

    def test_export_refuses_unmerged(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli(["xray", "export", "TB-601", "--ref", "main", "--test-repo", str(self.repo)])
        self.assertEqual(code, 1)
        self.assertIn("Has the qa/TB-601 pull request been merged", out.getvalue())


if __name__ == "__main__":
    unittest.main()
