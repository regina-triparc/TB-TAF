"""
Ticket classification (action, layer), the existing-coverage lookup, the plan gate
and routing by classification.
"""
import itertools
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from taf.agents import graph
from taf.runs import stages
from taf.runs.store import get_store
from taf.steps import classify, coverage
from tests.support import DESCRIPTION, Workbench, git, patched_settings

_TICKETS = itertools.count(1)


def diff(*paths: str) -> str:
    return "".join(f"diff --git a/{p} b/{p}\n+++ b/{p}\n@@ -1 +1 @@\n+x\n" for p in paths)


class SignalsTest(unittest.TestCase):
    def test_bug_issue_type_is_an_update(self):
        s = classify.signals({"issue_type": "Bug", "github_diff": diff("apps/web/src/a.component.ts")})
        self.assertEqual((s["change_type"], s["action"], s["layer"]), ("bugfix", "update", "ui"))

    def test_layers_from_diff_paths(self):
        api = diff("src/Api/Controllers/TripsController.cs", "src/Api/Dtos/TripDto.cs")
        self.assertEqual(classify.signals({"github_diff": api})["layer"], "api")
        both = api + diff("apps/web/src/trip.component.html")
        self.assertEqual(classify.signals({"github_diff": both})["layer"], "both")
        docs = diff("README.md", ".github/workflows/ci.yml", "e2e/a.spec.ts")
        s = classify.signals({"github_diff": docs})
        self.assertEqual((s["layer"], s["action"], s["change_type"]), ("none", "skip", "chore"))
        self.assertIsNone(classify.signals({"github_diff": diff("libs/core/util.ts")})["layer"])
        self.assertIsNone(classify.signals({})["layer"])

    def test_labels_titles_and_links(self):
        self.assertEqual(classify.signals({"labels": ["Hotfix"]})["change_type"], "bugfix")
        self.assertEqual(classify.signals({"pull_requests": [{"title": "fix(itinerary): day"}]})["action"], "update")
        self.assertEqual(classify.signals({"pull_requests": [{"title": "feat: info"}]})["change_type"], "feature")
        s = classify.signals({"issue_type": "Story", "linked_issues": [{"key": "TB-9", "type": "Bug"}]})
        self.assertIn("linked bug(s) TB-9", s["reasons"])

    def test_triage_confirms_or_disagrees(self):
        state = {"classification_signals": classify.signals({"issue_type": "Bug",
                                                              "github_diff": diff("apps/a.component.html")})}
        agreed = classify.reconcile(state, {"change_type": "bugfix", "layer": "ui"})
        self.assertEqual((agreed["action"], agreed["disagreements"]), ("update", []))
        self.assertEqual(classify.capped_confidence({"confidence": 0.9}, agreed), 0.9)
        disagreed = classify.reconcile(state, {"change_type": "feature", "layer": "api", "action": "create"})
        self.assertEqual(disagreed["layer"], "api")
        self.assertEqual(set(disagreed["disagreements"]), {"layer", "change_type"})
        self.assertEqual(classify.capped_confidence({"confidence": 0.9}, disagreed), 0.5)

    def test_unknown_is_todays_path(self):
        record = classify.reconcile({"classification_signals": classify.signals({})}, {"action": "nonsense"})
        self.assertEqual((record["action"], record["layer"]), ("create", "ui"))
        self.assertEqual(record["sources"]["layer"], "default")


SPEC = """import { test, expect } from '@playwright/test';
import { ItineraryPage } from '../pages/itinerary.page';
import { tripFixture } from '../fixtures/trip.fixture';
test.describe('Itinerary date picker', () => {
  test('keeps the edited day', { tag: '@TB-310' }, async ({ page }) => {
    await page.waitForResponse('**/api/trips/');
  });
  test('[TC-002] shows the info card', async () => {});
});
"""


class CoverageTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="taf-cov-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "e2e" / "itinerary").mkdir(parents=True)
        (self.repo / "e2e" / "itinerary" / "date-picker.spec.ts").write_text(SPEC, encoding="utf-8")
        (self.repo / "e2e" / "cart.spec.ts").write_text("test('opens the cart', async () => {});\n", encoding="utf-8")
        (self.repo / "node_modules" / "x").mkdir(parents=True)
        (self.repo / "node_modules" / "x" / "skip.spec.ts").write_text("test('no', () => {});\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "specs")

    def test_index_and_cache(self):
        index = coverage.build_index(self.repo)
        self.assertEqual([s["path"] for s in index["specs"]], ["e2e/cart.spec.ts", "e2e/itinerary/date-picker.spec.ts"])
        spec = index["specs"][1]
        self.assertEqual(spec["xray_keys"], ["TB-310"])
        self.assertEqual(spec["page_objects"], ["itinerary.page"])
        self.assertEqual(spec["fixtures"], ["trip.fixture"])
        self.assertEqual(spec["endpoints"], ["/api/trips/"])
        self.assertFalse(index["cached"])
        self.assertTrue(coverage.build_index(self.repo)["cached"])
        (self.repo / "e2e" / "cart.spec.ts").write_text("test('changed', async () => {});\n", encoding="utf-8")
        self.assertFalse(coverage.build_index(self.repo)["cached"], "local edits are never served from the cache")
        self.assertEqual(coverage.untagged(coverage.build_index(self.repo)), [{"path": "e2e/cart.spec.ts", "tests": 1}])

    def test_ranks_matching_tests_first(self):
        index = coverage.build_index(self.repo)
        state = {"ticket_id": "BUG-1", "linked_issues": [{"key": "TB-310", "type": "Story"}],
                 "triage_analysis": {"impacted_areas": ["Itinerary date picker"]},
                 "github_diff": diff("apps/web/date-picker.component.ts") + "+ this.http.get('/api/trips/42')\n"}
        ranked = coverage.rank_candidates(index, state)
        top = ranked[0]
        self.assertEqual((top["spec"], top["test"], top["xray_key"]),
                         ("e2e/itinerary/date-picker.spec.ts", "keeps the edited day", "TB-310"))
        self.assertIn("tagged TB-310", top["reason"])
        self.assertNotIn("e2e/cart.spec.ts", [c["spec"] for c in ranked])
        self.assertIn("e2e/itinerary/date-picker.spec.ts", coverage.candidates_block({"coverage_candidates": ranked}))

    def test_plan_reasons_and_decisions(self):
        state = {"action": "update", "coverage_candidates": [], "plan_confidence": 0.4,
                 "test_cases": [{"test_id": "TC-001", "disposition": "update", "target_spec": "e2e/x.spec.ts"}]}
        self.assertEqual(len(coverage.plan_reasons(state)), 3)
        self.assertEqual(coverage.validate_plan_decision({"approved": True})["action"], "continue")
        for bad in ({"action": "go"}, {"action": "continue", "override": {"layer": "db"}},
                    {"action": "continue", "dispositions": {"TC-001": {"disposition": "maybe"}}}):
            with self.assertRaises(ValueError):
                coverage.validate_plan_decision(bad)


def new_run(**extra):
    ticket = f"CC-{next(_TICKETS)}"
    state = stages.prepare(ticket, "full", summary=f"{ticket} summary", description=DESCRIPTION,
                           target_url="https://app.example.test", provider="ollama", source="seed")
    state.update(extra)
    return ticket, state


class PlanGateTest(unittest.TestCase):
    def setUp(self):
        self.wb = Workbench(require_approval=False, plan_gate=True).start()
        self.addCleanup(self.wb.close)

    def test_bugfix_without_a_test_to_update_pauses_after_the_plan(self):
        _, state = new_run(issue_type="Bug")
        final = stages.execute(state, "full")
        self.assertEqual(final["_run_state"], "needs_plan_review", final.get("errors"))
        request = final["_plan_request"]
        self.assertEqual(request["classification"]["action"], "update")
        self.assertIn("no existing test", request["reasons"][0])
        self.assertEqual(self.wb.calls["text"], 0, "no spec before the plan is reviewed")
        run = next(r for r in get_store().recent_runs() if r["run_id"] == state["run_id"])
        self.assertEqual(run["state"], "needs_plan_review")
        architect = next(p for p in run["phases"] if p["phase"] == "architect")
        self.assertEqual((architect["state"], architect["artifact"]["plan_review"]["status"]), ("waiting", "waiting"))
        with self.assertRaises(stages.StageError):
            stages.resume(state["run_id"], {"action": "answer", "answers": {"Q1": "x"}})

        final = stages.resume(state["run_id"], {"action": "continue", "override": {"action": "create"}, "by": "qa"})
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertEqual((final["action"], final["classification"]["overridden_by"]), ("create", "qa"))
        self.assertEqual(self.wb.calls["text"], 1)
        self.assertIn("**Plan gate:** continued by qa", final["final_report"])
        self.assertIn("**Classification:** action `create`", final["final_report"])

    def test_reject_goes_to_the_report(self):
        _, state = new_run(issue_type="Bug")
        stages.execute(state, "full")
        final = stages.resume(state["run_id"], {"action": "reject", "by": "qa"})
        self.assertEqual(final["plan_review"]["status"], "rejected")
        self.assertEqual(self.wb.calls["text"], 0)
        self.assertIn("final_report", final)

    def test_a_clear_story_passes_through(self):
        _, state = new_run(issue_type="Story")
        final = stages.execute(state, "full")
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertEqual(final["plan_review"]["status"], "passed")
        self.assertTrue(final["coverage"]["available"])


class RoutingTest(unittest.TestCase):
    def test_classification_only_reroutes_when_switched_on(self):
        self.wb = Workbench(require_approval=False).start()
        self.addCleanup(self.wb.close)
        _, state = new_run(github_diff=diff("README.md", "docs/setup.md"))
        final = stages.execute(state, "full")
        self.assertEqual(final["action"], "skip")
        self.assertEqual(self.wb.calls["text"], 1, "off: today's path - a spec is still generated")
        self.assertIn("ROUTE_BY_CLASSIFICATION=false", final["final_report"])

    def test_skip_goes_to_the_report_when_switched_on(self):
        self.wb = Workbench(require_approval=False, route_by_classification=True).start()
        self.addCleanup(self.wb.close)
        _, state = new_run(github_diff=diff("README.md", "docs/setup.md"))
        final = stages.execute(state, "full")
        self.assertEqual(self.wb.calls["text"], 0, "skip: no new spec")
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertIn("**Classification:** action `skip`, layer `none`", final["final_report"])

    def test_after_plan_routes(self):
        with patched_settings(route_by_classification=True):
            self.assertEqual(graph._after_plan({"action": "regression_only"}), "run_existing")
            self.assertEqual(graph._after_plan({"action": "skip"}), graph.REPORT)
            self.assertEqual(graph._after_plan({"action": "update"}), "repo_context")
        with patched_settings(route_by_classification=False):
            self.assertEqual(graph._after_plan({"action": "regression_only"}), "repo_context")

    def test_regression_run_uses_matching_specs_only(self):
        runs = []
        with patched_settings(coverage_match_threshold=0.3), \
                mock.patch.object(coverage.TEST_REPO, "path", Path(tempfile.gettempdir())), \
                mock.patch.object(coverage.TEST_REPO, "run_playwright",
                                  side_effect=lambda spec, url, logs, json_report=None: runs.append(spec) or True):
            out = coverage.run_existing({"run_id": "r", "ticket_id": "T", "llm_provider": "ollama",
                                         "coverage_candidates": [{"spec": "e2e/a.spec.ts", "score": 0.9},
                                                                 {"spec": "e2e/b.spec.ts", "score": 0.1}]})
        self.assertEqual([Path(r).name for r in runs], ["a.spec.ts"])
        self.assertEqual(out["regression_run"]["specs"], ["e2e/a.spec.ts"])
        self.assertFalse(out["execution_passed"], "no test result reported - not a pass")


if __name__ == "__main__":
    unittest.main()
