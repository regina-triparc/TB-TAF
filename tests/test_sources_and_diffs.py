"""Ticket source selection, PR discovery and diff condensing - all offline."""
import unittest

from taf.integrations.github.pull_requests import (
    PullRequest, condense, fetch_ticket_diff, prs_from_seed, prs_from_text, repo_slug, split_files,
)
from taf.integrations.jira.board import get_board
from taf.runs import stages


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
