"""The per-run usage report: tokens, cost, model and time for every stage of a ticket's run."""
import csv
import json
import unittest
import uuid
from pathlib import Path

from taf.llm.budget import BUDGET
from taf.llm.pricing import TokenUsage
from taf.runs import stages
from taf.runs.usage import USAGE, UsageLedger, report_stem, usage_dir
from tests.support import Workbench
from tests.test_workflow import DESCRIPTION


def read_report(ticket_id: str, run_id: str) -> dict:
    return json.loads((usage_dir() / f"{report_stem(ticket_id, run_id)}.json").read_text(encoding="utf-8"))


def csv_rows(run_id: str) -> list:
    with (usage_dir() / "usage.csv").open(newline="", encoding="utf-8") as fh:
        return [r for r in csv.DictReader(fh) if r["run_id"] == run_id]


class LedgerTest(unittest.TestCase):
    def test_calls_are_booked_per_stage_agent_and_model(self):
        ledger, run_id = UsageLedger(), uuid.uuid4().hex
        ledger.begin(run_id, "USG-1", "claude", {"triage": {"provider": "claude", "model": "claude-opus-5-5"}})
        ledger.set_stage(run_id, "triage")
        ledger.record_call(run_id, "Intake Triager", "claude", "claude-opus-5-5",
                           TokenUsage(input_tokens=1000, output_tokens=200, cache_read_tokens=400), 1.5)
        ledger.stage_finished(run_id, "triage", 3.25, "ok")
        ledger.set_stage(run_id, "automation")
        for _ in range(2):
            ledger.record_call(run_id, "TS Automation Engineer", "claude", "claude-sonnet-5-5",
                               TokenUsage(input_tokens=500, output_tokens=100, cache_write_5m_tokens=50), 0.5)
        ledger.record_call(run_id, "Test Reviewer", "ollama", "qwen", TokenUsage(input_tokens=10, output_tokens=5))
        ledger.stage_finished(run_id, "automation", 61.0, "ok")

        report = ledger.snapshot(run_id)
        self.assertEqual([s["stage"] for s in report["stages"]], ["triage", "automation"])
        triage, automation = report["stages"]
        self.assertEqual(triage["duration_s"], 3.2)
        self.assertEqual(triage["totals"]["total_tokens"], 1200)
        self.assertEqual(triage["totals"]["cache_read_tokens"], 400)
        self.assertAlmostEqual(triage["totals"]["cost_usd"], 0.015)
        engineer = next(m for m in automation["models"] if m["agent"] == "TS Automation Engineer")
        self.assertEqual((engineer["calls"], engineer["model"], engineer["cache_write_tokens"]),
                         (2, "claude-sonnet-5-5", 100))
        self.assertEqual(len(automation["models"]), 2, "one row per agent and model")
        self.assertEqual(report["totals"]["calls"], 4)
        self.assertEqual(report["totals"]["total_tokens"], 1200 + 1200 + 15)
        self.assertEqual(report["duration_s"], 64.2)

        path = ledger.write_report(run_id, "completed")
        markdown = Path(path).read_text(encoding="utf-8")
        self.assertIn("| triage | ok | 3.2s | `claude-opus-5-5` | 1 |", markdown)
        self.assertIn("| automation | ok | 1m 01s |", markdown)
        self.assertIn("| automation | Test Reviewer | ollama | `qwen` |", markdown)
        rows = csv_rows(run_id)
        self.assertEqual(len(rows), 3)
        self.assertEqual({r["model"] for r in rows}, {"claude-opus-5-5", "claude-sonnet-5-5", "qwen"})
        self.assertIsNone(ledger.snapshot(run_id), "a finished run is released")

    def test_resumed_run_continues_the_saved_report(self):
        ledger, run_id = UsageLedger(), uuid.uuid4().hex
        ledger.begin(run_id, "USG-2")
        ledger.set_stage(run_id, "triage")
        ledger.record_call(run_id, "Intake Triager", "ollama", "qwen", TokenUsage(input_tokens=7, output_tokens=3))
        ledger.stage_finished(run_id, "triage", 2.0, "ok")
        ledger.write_report(run_id, "cancelled")

        resumed = UsageLedger()  # a fresh process
        resumed.begin(run_id, "USG-2")
        resumed.stage_finished(run_id, "architect", 4.0, "ok")
        resumed.write_report(run_id, "completed")

        report = read_report("USG-2", run_id)
        self.assertEqual([s["stage"] for s in report["stages"]], ["triage", "architect"])
        self.assertEqual(report["totals"]["total_tokens"], 10)
        self.assertEqual(report["run_state"], "completed")
        self.assertEqual(len(csv_rows(run_id)), 2, "the resumed run replaces its earlier rows")

    def test_budget_record_books_into_the_current_stage(self):
        run_id = uuid.uuid4().hex
        USAGE.begin(run_id, "USG-3")
        USAGE.set_stage(run_id, "architect")
        BUDGET.record(run_id, "USG-3", "Test Architect", TokenUsage(input_tokens=100, output_tokens=50),
                      "claude-opus-5-5", "claude")
        BUDGET.release(run_id)
        architect = USAGE.snapshot(run_id)["stages"][0]
        USAGE.write_report(run_id, "completed")
        self.assertEqual(architect["stage"], "architect")
        self.assertEqual(architect["models"][0]["model"], "claude-opus-5-5")
        self.assertAlmostEqual(architect["totals"]["cost_usd"], (100 * 4 + 50 * 20) / 1_000_000)

    def test_calls_outside_a_recorded_run_are_ignored(self):
        ledger = UsageLedger()
        ledger.record_call("nope", "a", "claude", "m", TokenUsage(input_tokens=1))
        self.assertIsNone(ledger.snapshot("nope"))
        self.assertIsNone(ledger.write_report("nope", "completed"))


class StageRunReportTest(unittest.TestCase):
    def setUp(self):
        self.wb = Workbench(git_push_enabled=True).start()
        self.addCleanup(self.wb.close)

    def test_every_run_writes_a_report_with_each_stage_timed(self):
        ticket = f"USG-{uuid.uuid4().hex[:6]}"
        state = stages.prepare(ticket, "full", summary=f"{ticket} summary", description=DESCRIPTION,
                               target_url="https://app.example.test", provider="ollama", source="seed")
        waiting = stages.execute(state, "full")
        self.assertEqual(waiting["_run_state"], "waiting")
        self.assertTrue(Path(waiting["usage_report_path"]).exists(), "a paused run already has a report")
        self.assertEqual(read_report(ticket, state["run_id"])["run_state"], "waiting")

        final = stages.resume(state["run_id"], {"approved": True, "by": "reviewer"})

        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        report = read_report(ticket, state["run_id"])
        self.assertEqual(report["run_state"], "completed")
        self.assertEqual(report["ticket_id"], ticket)
        self.assertEqual([s["stage"] for s in report["stages"]],
                         ["context", "triage", "architect", "automation", "approval", "publish", "report"])
        self.assertTrue(all(s["state"] == "ok" for s in report["stages"]), report["stages"])
        self.assertTrue(all(s["duration_s"] >= 0 for s in report["stages"]))
        self.assertEqual(report["routes"]["triage"]["provider"], "ollama")
        self.assertEqual({r["stage"] for r in csv_rows(state["run_id"])},
                         {"context", "triage", "architect", "automation", "approval", "publish", "report"})


if __name__ == "__main__":
    unittest.main()
