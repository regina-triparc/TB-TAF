"""Ticket source selection, PR discovery and diff condensing - all offline."""
import tests  # noqa: F401 - pins the offline test environment before taf is imported, however the suite is run
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from taf.integrations.github.pull_requests import (
    PullRequest, SourceRepo, condense, fetch_ticket_diff, prs_from_seed, prs_from_text, repo_slug, source_repo,
    split_files,
)
from taf.integrations.jira.board import get_board
from taf.runs import stages
from tests.support import git, patched_settings


def file_diff(path: str, lines: int, line: str = "+ changed line") -> str:
    body = "\n".join(line for _ in range(lines))
    return (f"diff --git a/{path} b/{path}\nindex 1..2 100644\n--- a/{path}\n+++ b/{path}\n"
            f"@@ -0,0 +1,{lines} @@\n{body}\n")


class PrDiscoveryTest(unittest.TestCase):
    def test_repo_slug(self):
        self.assertEqual(repo_slug("https://github.com/TravelEdge/TripArc.TripBuilder.git"),
                         "traveledge/triparc.tripbuilder")

    def test_prs_from_text(self):
        prs = prs_from_text("Merged in TravelEdge/TripArc.TripBuilder#740 and "
                            "https://github.com/TravelEdge/TripArc.TripBuilder/pull/764.")
        self.assertEqual([p.number for p in prs], [740, 764])

    def test_prs_on_the_old_repo_move_to_triparc_platform(self):
        new_url = "https://github.com/TripArc-Platform/TripArc.TripBuilder/pull/740"
        by_repo = PullRequest(740, "traveledge/triparc.tripbuilder", "MERGED")
        by_url = PullRequest(740, "", url="https://github.com/TravelEdge/TripArc.TripBuilder/pull/740")
        stale_repo = PullRequest(740, "TravelEdge/TripArc.TripBuilder", url=new_url)
        for pr in (by_repo, by_url, stale_repo):
            self.assertEqual((pr.repo, pr.url), ("triparc-platform/triparc.tripbuilder", new_url))
        self.assertEqual([p.repo for p in prs_from_text("Merged in TravelEdge/TripArc.TripBuilder#740.")],
                         ["triparc-platform/triparc.tripbuilder"])

    def test_an_old_source_repo_url_points_at_the_new_repo(self):
        with patched_settings(source_repo_url="https://github.com/TravelEdge/TripArc.TripBuilder.git"):
            repo = source_repo()
        self.assertEqual(repo.url, "https://github.com/TripArc-Platform/TripArc.TripBuilder.git")
        self.assertEqual(repo.slug, PullRequest(740, "traveledge/triparc.tripbuilder").repo)

    def test_prs_on_other_repos_are_left_alone(self):
        pr = PullRequest(12, "o/r", url="https://github.com/o/r/pull/12")
        self.assertEqual((pr.repo, pr.url), ("o/r", "https://github.com/o/r/pull/12"))

    def test_prs_from_seed(self):
        prs = prs_from_seed([12, {"number": 13, "status": "DECLINED"}], "o/r")
        self.assertEqual([(p.number, p.repo, p.usable) for p in prs], [(12, "o/r", True), (13, "o/r", False)])

    def test_seed_ticket_carries_its_prs(self):
        issue = get_board().issue("TB-601", "seed")
        self.assertEqual(sorted(p.number for p in issue["pull_requests"]), [740, 764])

    def test_no_source_repo_means_no_diff_but_no_failure(self):
        diff, notes = fetch_ticket_diff([PullRequest(740, "o/r", "MERGED")])
        self.assertEqual(diff, "")
        self.assertTrue(any("SOURCE_REPO_URL not set" in n for n in notes))


class BareCloneDiffTest(unittest.TestCase):
    """PR diffs come from a bare clone; Git 2.55 refuses bare repos it has to discover."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="taf-src-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        up = self.tmp / "upstream"
        up.mkdir()
        git(up, "init", "-q", "-b", "main")
        (up / "app.ts").write_text("export const a = 1;\n", encoding="utf-8")
        git(up, "add", ".")
        git(up, "commit", "-q", "-m", "base")
        git(up, "switch", "-q", "-c", "feature")
        (up / "app.ts").write_text("export const a = 2;\n", encoding="utf-8")
        git(up, "commit", "-q", "-am", "change")
        git(up, "update-ref", "refs/pull/7/head", "HEAD")
        git(up, "switch", "-q", "main")
        self.repo = SourceRepo(up.as_uri(), str(self.tmp / "src.git"), "main")

    def test_diff_works_with_safe_bare_repository_explicit(self):
        explicit = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "safe.bareRepository",
                    "GIT_CONFIG_VALUE_0": "explicit"}
        with mock.patch.dict(os.environ, explicit):
            self.repo.ensure_clone()
            diff = self.repo.diff(7)
        self.assertIn("diff --git a/app.ts b/app.ts", diff)
        self.assertIn("+export const a = 2;", diff)


class SourceSelectionTest(unittest.TestCase):
    def test_seed_source(self):
        self.assertEqual(get_board().resolve_source("seed"), "seed")
        self.assertEqual(get_board().tickets(source="seed")["source"], "seed")

    def test_jira_source_without_credentials_is_a_clear_error(self):
        with self.assertRaisesRegex(stages.StageError, "TICKET_SOURCE is jira"):
            stages.prepare("TB-601", "triage", source="jira")

    def test_run_records_source_and_prs(self):
        state = stages.prepare("TB-601", "triage", source="seed")
        self.assertEqual(state["ticket_source"], "seed")
        self.assertEqual(sorted(p["number"] for p in state["pull_requests"]), [740, 764])
        final = stages.execute(state, "triage")
        self.assertEqual(final["_run_state"], "completed")
        self.assertTrue(any("SOURCE_REPO_URL not set" in n for n in final["context_notes"]))


class CondenseTest(unittest.TestCase):
    def setUp(self):
        self.diff = (file_diff("services/gateway/Big.cs", 3000)
                     + file_diff("apps/tsp/src/info.component.html", 20, '+<button aria-label="Add Service">')
                     + file_diff("apps/tsp/src/info.component.ts", 40)
                     + file_diff("apps/tsp/src/info.component.spec.ts", 40)
                     + file_diff("package-lock.json", 500))

    def test_small_diff_is_untouched(self):
        small = file_diff("apps/a.html", 3)
        self.assertEqual(condense(small, 10_000), small)

    def test_budget_and_priorities(self):
        out = condense(self.diff, 4000)
        self.assertLessEqual(len(out), 4000)
        self.assertIn("5 files changed", out)                       # full overview always present
        shown = [f.path for f in split_files(out)]
        self.assertEqual(shown[0], "apps/tsp/src/info.component.html")  # templates first
        self.assertNotIn("package-lock.json", shown)                # excluded: listed, never shown
        self.assertIn("package-lock.json", out)
        self.assertIn('aria-label="Add Service"', out)


if __name__ == "__main__":
    unittest.main()
