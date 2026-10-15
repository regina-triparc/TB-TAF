"""Xray test cases are written to the test repo next to the spec, and exported from there."""
import contextlib
import io
import json
import subprocess
import unittest

from taf.cli import main as cli
from taf.integrations.jira import xray
from taf.runs import stages
from tests.support import Workbench, git


def planned_state():
    """A TB-601 state that has been through triage + architect (offline plan)."""
    state = stages.prepare("TB-601", "architect", source="seed", provider="offline")
    return stages.execute(state, "architect")


class XrayFilesTest(unittest.TestCase):
    def test_ticket_files(self):
        files = xray.ticket_files(planned_state())
        self.assertEqual(sorted(files), ["TB-601.test-plan.md", "TB-601.xray.csv", "TB-601.xray.json"])
        self.assertTrue(json.loads(files["TB-601.xray.json"]))
        self.assertIn("python -m taf xray export TB-601", files["TB-601.test-plan.md"])
        self.assertEqual(xray.json_file("TB-601"), "xray/TB-601/TB-601.xray.json")

    def test_no_plan_no_files(self):
        self.assertEqual(xray.ticket_files({"ticket_id": "X-1"}), {})


class PublishWritesXrayTest(unittest.TestCase):
    """A full run (approval not required: nothing is pushed) on TB-601 against a temp test repo."""

    def run_full(self, **settings):
        self.wb = Workbench(require_approval=True, **settings).start()
        self.addCleanup(self.wb.close)
        state = stages.prepare("TB-601", "full", source="seed", provider="ollama")
        final = stages.execute(state, "full")
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        return final

    def test_in_place_writes_spec_and_xray_uncommitted(self):
        final = self.run_full(no_git=True)
        repo = self.wb.repo
        self.assertTrue((repo / "e2e/qa/TB-601.spec.ts").is_file())
        for name in ("TB-601.xray.json", "TB-601.xray.csv", "TB-601.test-plan.md"):
            self.assertTrue((repo / "xray/TB-601" / name).is_file(), name)
        self.assertEqual(final["xray_files"][0].split("/")[:2], ["xray", "TB-601"])
        status = git(repo, "status", "--porcelain")
        self.assertIn("e2e/", status)  # left for the person, not committed

    def test_managed_clone_commits_spec_and_xray_together(self):
        self.run_full(no_git=False)
        repo = self.wb.repo
        self.assertEqual(git(repo, "branch", "--show-current").strip(), "qa/TB-601")
        files = git(repo, "show", "--name-only", "--format=%s", "HEAD").splitlines()
        self.assertIn("Playwright spec and Xray test cases", files[0])
        self.assertIn("e2e/qa/TB-601.spec.ts", files)
        self.assertIn("xray/TB-601/TB-601.xray.json", files)

    def test_export_reads_merged_version_and_is_dry_run(self):
        self.run_full(no_git=False)
        repo = self.wb.repo
        git(repo, "checkout", "-q", "main")
        git(repo, "merge", "-q", "--no-ff", "-m", "merge", "qa/TB-601")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli(["xray", "export", "TB-601", "--ref", "main", "--test-repo", str(repo)])
        self.assertEqual(code, 0, out.getvalue())
        self.assertIn("main:xray/TB-601/TB-601.xray.json", out.getvalue())
        self.assertIn("Dry run: nothing was sent", out.getvalue())

    def test_export_refuses_unmerged(self):
        self.wb = Workbench().start()
        self.addCleanup(self.wb.close)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli(["xray", "export", "TB-601", "--ref", "main", "--test-repo", str(self.wb.repo)])
        self.assertEqual(code, 1)
        self.assertIn("Has the qa/TB-601 pull request been merged", out.getvalue())


if __name__ == "__main__":
    unittest.main()
