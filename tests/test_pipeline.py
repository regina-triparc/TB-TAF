"""The seeded ticket goes through every stage, via the stage runner and via the full LangGraph."""
import unittest
import uuid

from taf.agents.graph import qa_graph, thread_config
from taf.integrations.jira.board import get_board
from taf.runs import stages
from taf.runs.store import get_store


class SeedTest(unittest.TestCase):
    def test_seed_has_tb_601(self):
        board = get_board().tickets()
        self.assertFalse(board["configured"])
        self.assertIn("TB-601", [t["key"] for t in board["tickets"]])
        self.assertIn("ACCEPTANCE CRITERIA", get_board().issue("TB-601")["description"])


class StageRunnerTest(unittest.TestCase):
    def test_full_stage_offline(self):
        state = stages.prepare("TB-601", "full")
        final = stages.execute(state, "full")

        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertGreaterEqual(len(final["triage_analysis"]["acceptance_criteria"]), 10)
        self.assertTrue(final["test_cases"])
        self.assertTrue(final["xray_tests_json"])
        self.assertIn("test.describe", final["generated_typescript_code"])
        self.assertIn("NOT written to the test repository", final["execution_logs"])
        self.assertIn("## QA Report - TB-601", final["final_report"])
        self.assertTrue(final["report_html_path"].endswith(".html"))
        self.assertIn("TB-601 QA Report", open(final["report_html_path"], encoding="utf-8").read())

        run = next(r for r in get_store().recent_runs() if r["run_id"] == state["run_id"])
        self.assertEqual([p["phase"] for p in run["phases"]], ["triage", "architect", "automation", "report"])
        self.assertTrue(all(p["state"] == "ok" for p in run["phases"]))

    def test_provider_is_frozen_at_start(self):
        from taf.llm.router import ROUTER
        state = stages.prepare("TB-601", "triage")
        self.assertEqual(state["llm_provider"], ROUTER.default_provider)
        stages.execute(state, "triage")

    def test_unknown_ticket_is_rejected(self):
        with self.assertRaises(stages.StageError):
            stages.prepare("NOPE-1", "triage")

    def test_unknown_stage_is_rejected(self):
        with self.assertRaises(stages.StageError):
            stages.prepare("TB-601", "deploy")


class GraphTest(unittest.TestCase):
    def test_full_graph_reaches_closeout(self):
        issue = get_board().issue("TB-601")
        run_id = uuid.uuid4().hex
        final = qa_graph().invoke({
            "run_id": run_id, "ticket_id": "TB-601", "summary": issue["summary"],
            "description": issue["description"], "target_url": "https://app.example.test",
            "github_diff": "", "environment_logs": "", "n8n_callback_url": "", "errors": [],
            "llm_provider": "offline",
        }, config=thread_config(run_id))
        self.assertIn("## QA Report - TB-601", final["final_report"])
        self.assertFalse(final["callback_delivered"])
        self.assertTrue(any("no n8n_callback_url" in e for e in final["errors"]))


if __name__ == "__main__":
    unittest.main()
