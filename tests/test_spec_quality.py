"""
The spec-quality additions: live UI snapshot, repair from the run (and its guard),
the per-test JSON results, the repo scanner's page-object docs, and the review rules
that enforce the spec-authoring skill.
"""
import itertools
import json
import tempfile
import unittest
from pathlib import Path

from taf.agents.automation.agent import build_messages
from taf.integrations.github.repository import with_json_reporter
from taf.llm.router import ROUTER
from taf.runs import stages
from taf.steps.context import render_repo_context, scan_repository
from taf.steps.playwright_results import counts, parse_json_report
from taf.steps.review import rule_review
from taf.steps.run_repair import changed, test_bodies, test_lines, weakened
from taf.steps.ui_snapshot import parse_output, render_snapshot
from tests.support import DESCRIPTION, SPEC, Workbench, write_json_report

_TICKETS = itertools.count(1)

FIXED = SPEC.replace("test('[TC-001] first'", "test('[TC-001] first (repaired)'")
WEAKENED = SPEC.replace("test('[TC-001] first', async ({ page }) => { await expect(page).toHaveURL(/.+/); });",
                        "test('[TC-001] first', async ({ page }) => { await page.goto('/'); });")
QUARANTINED = SPEC.replace("test('[TC-001] first', async ({ page }) => { await expect(page).toHaveURL(/.+/); });",
                           "test('[TC-001] first', async () => { test.fixme(true, 'no price on the page'); });")


def new_run():
    ticket = f"SQ-{next(_TICKETS)}"
    return ticket, stages.prepare(ticket, "full", summary=f"{ticket} summary", description=DESCRIPTION,
                                  target_url="https://app.example.test", provider="ollama", source="seed")


class RepairGuardTest(unittest.TestCase):
    def test_bodies_are_split_per_test_case(self):
        bodies = test_bodies(SPEC)
        self.assertEqual(sorted(bodies), ["TC-001", "TC-002", "TC-003"])
        self.assertIn("first", bodies["TC-001"])
        self.assertNotIn("second", bodies["TC-001"])

    def test_dropping_an_expectation_is_weakening(self):
        reasons = weakened(SPEC, WEAKENED, passing=["TC-002", "TC-003"])
        self.assertEqual(reasons, ["TC-001 went from 1 to 0 expect() calls"])

    def test_quarantining_a_failing_test_is_allowed(self):
        self.assertEqual(weakened(SPEC, QUARANTINED, passing=["TC-002", "TC-003"]), [])

    def test_quarantining_a_passing_test_is_not(self):
        reasons = weakened(SPEC, QUARANTINED, passing=["TC-001"])
        self.assertEqual(reasons, ["TC-001 passed but was turned into fixme/skip"])

    def test_removing_a_test_is_weakening(self):
        without = SPEC.replace("  test('[TC-003] third', async ({ page }) => { await expect(page).toHaveURL(/.+/); });\n", "")
        self.assertIn("TC-003 was removed", weakened(SPEC, without, passing=[]))

    def test_changed_and_rerun_lines(self):
        self.assertEqual(changed(SPEC, FIXED), ["TC-001"])
        # SPEC: line 1 import, line 2 describe, tests on lines 3-5.
        self.assertEqual(test_lines(SPEC, ["TC-002", "TC-001"]), [3, 4])
        spaced = SPEC.replace("\n  test('[TC-002]", "\n\n\n  test('[TC-002]")
        self.assertEqual(test_lines(spaced, ["TC-002"]), [6], "blank lines before a test are not counted as its start")


class RunRepairWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.wb = Workbench(require_approval=False, run_repair_attempts=2).start()
        self.addCleanup(self.wb.close)

    def test_a_repair_that_fixes_the_failing_test_is_kept(self):
        self.wb.specs = [SPEC, FIXED]
        self.wb.play_results = [{"TC-001": "failed", "TC-002": "passed", "TC-003": "passed"}, {"TC-001": "passed"}]
        _, state = new_run()
        final = stages.execute(state, "full")

        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        repair = final["run_repair"]
        self.assertTrue(repair["accepted"])
        self.assertEqual(repair["attempts"][0]["outcome"], "kept")
        self.assertEqual(repair["attempts"][0]["fixed"], ["TC-001"])
        self.assertEqual(repair["before"], {"failed": 1, "passed": 2})
        self.assertEqual(repair["after"], {"passed": 3})
        self.assertTrue(final["execution_passed"])
        self.assertIn("(repaired)", final["generated_typescript_code"])
        self.assertEqual(self.wb.play_grep[1], ["TC-001"], "only the failing test is rerun")
        repair_turn = self.wb.text_messages[1][-1].content[0]["text"]
        self.assertIn("TC-001 broke", repair_turn)
        self.assertIn("Never change what a test EXPECTS", repair_turn)

    def test_a_repair_that_drops_an_assertion_is_rejected_and_never_run(self):
        self.wb.specs = [SPEC, WEAKENED]
        self.wb.play_results = [{"TC-001": "failed", "TC-002": "passed", "TC-003": "passed"}]
        _, state = new_run()
        final = stages.execute(state, "full")

        attempts = final["run_repair"]["attempts"]
        self.assertEqual([a["outcome"] for a in attempts], ["rejected", "rejected"])
        self.assertIn("TC-001 went from 1 to 0 expect() calls", attempts[0]["reason"])
        self.assertEqual(self.wb.calls["play"], 1, "a weakened repair must not be run")
        self.assertEqual(final["generated_typescript_code"].strip(), SPEC.strip())
        self.assertFalse(final["execution_passed"])
        second_turn = self.wb.text_messages[2][-1].content[0]["text"]
        self.assertIn("Your previous repair was rejected", second_turn)

    def test_quarantine_counts_as_handled_but_not_as_passed(self):
        self.wb.specs = [SPEC, QUARANTINED]
        self.wb.play_results = [{"TC-001": "failed", "TC-002": "passed", "TC-003": "passed"}, {"TC-001": "skipped"}]
        _, state = new_run()
        final = stages.execute(state, "full")

        attempt = final["run_repair"]["attempts"][0]
        self.assertEqual(attempt["outcome"], "kept")
        self.assertEqual(attempt["quarantined"], ["TC-001"])
        self.assertEqual(final["run_repair"]["after"], {"passed": 2, "skipped": 1})
        self.assertIn("test.fixme(true, 'no price on the page')", final["generated_typescript_code"])


class PlaywrightResultsTest(unittest.TestCase):
    def test_json_report_is_parsed_per_test(self):
        path = Path(tempfile.mkdtemp()) / "r.json"
        write_json_report(path, {"TC-001": "passed", "TC-002": "failed", "TC-003": "skipped"})
        tests = parse_json_report(path)
        self.assertEqual([(t["tc"], t["status"]) for t in tests],
                         [("TC-001", "passed"), ("TC-002", "failed"), ("TC-003", "skipped")])
        self.assertEqual(tests[1]["error"], "TC-002 broke")
        self.assertEqual(counts(tests), {"passed": 1, "failed": 1, "skipped": 1})

    def test_missing_report_is_empty(self):
        self.assertEqual(parse_json_report(Path(tempfile.mkdtemp()) / "none.json"), [])

    def test_json_reporter_is_added_next_to_the_configured_one(self):
        self.assertEqual(with_json_reporter(["--reporter=line", "--project=qa-agent"]),
                         ["--reporter=line,json", "--project=qa-agent"])
        self.assertEqual(with_json_reporter(["--project=qa-agent"]), ["--project=qa-agent", "--reporter=line,json"])
        self.assertEqual(with_json_reporter(["--reporter=list,json"]), ["--reporter=list,json"])


class LiveSnapshotTest(unittest.TestCase):
    RESULT = {"ok": True, "url": "https://app.example.test/tsp/1/itinerary", "states": [
        {"name": "Add Info form", "ok": True, "snapshot": '- dialog "Add Info":\n  - combobox "Start"'},
        {"name": "Start dropdown", "ok": True, "snapshot": "96 options: 00:00, 00:15 ... 23:45"},
        {"name": "Hotel sheet", "ok": False, "error": "timeout"}]}

    def test_output_is_the_last_json_line(self):
        out = "[live-inventory] noise\n{not json\n" + json.dumps(self.RESULT) + "\n"
        self.assertEqual(parse_output(out)["url"], self.RESULT["url"])
        self.assertFalse(parse_output("nothing here")["ok"])

    def test_render_lists_captured_states_and_names_the_missing_ones(self):
        text = render_snapshot(self.RESULT, 10_000)
        self.assertIn("### Add Info form", text)
        self.assertIn("96 options: 00:00", text)
        self.assertIn("Not captured: Hotel sheet", text)
        self.assertEqual(render_snapshot({"ok": False, "reason": "expired"}, 10_000), "")

    def test_snapshot_is_in_the_automation_prompt_only_when_captured(self):
        route = ROUTER.route("automation", "ollama")
        base = {"ticket_id": "T-1", "test_cases": [{"test_id": "TC-001", "automatable": True}]}
        with_snapshot = dict(base, live_snapshot={"available": True, "prompt": render_snapshot(self.RESULT, 10_000)})
        prompt = lambda state: "".join(str(m.content) for m in build_messages(state, route))
        heading = "## Live page snapshot (captured from the running app"
        self.assertIn(heading, prompt(with_snapshot))
        self.assertIn("96 options: 00:00", prompt(with_snapshot))
        self.assertNotIn(heading, prompt(base))


class RepoScanTest(unittest.TestCase):
    def test_getters_readonly_fields_and_verified_docs_are_listed(self):
        root = Path(tempfile.mkdtemp())
        (root / "e2e" / "qa").mkdir(parents=True)
        (root / "e2e" / "pages").mkdir(parents=True)
        (root / "e2e" / "pages" / "form.page.ts").write_text(
            "/**\n * The form.\n *\n * Verified against UAT on 2026-10-21:\n *     dialog \"Add Info\"\n */\n"
            "export class FormPage {\n  readonly dialog: Locator;\n  constructor(private readonly page: Page) {}\n"
            "  get title(): Locator {\n    return x;\n  }\n  async submit(): Promise<void> {\n  }\n"
            "  private async helper(): Promise<void> {\n  }\n}\n")
        facts = scan_repository(root, "e2e/qa")
        po = facts["page_objects"][0]
        self.assertEqual(po["methods"], ["new FormPage(page: Page)", "dialog", "title: Locator", "submit(): Promise<void>"])
        self.assertIn('dialog "Add Info"', po["doc"])
        text = render_repo_context(facts, 10_000)
        self.assertLess(text.index("Verified page objects"), text.index("### Page objects"))


class SpecRulesTest(unittest.TestCase):
    def findings(self, code):
        return {f["rule"]: f for f in rule_review(code, [], [], {})}

    def test_helper_functions_in_the_spec_are_flagged(self):
        code = SPEC + "\nfunction infoRow(page) { return page; }\nasync function addInfo() {}\n"
        self.assertIn("infoRow, addInfo", self.findings(code)["lean spec"]["detail"])

    def test_unnamed_page_level_role_locator_is_flagged(self):
        code = SPEC + "\nawait expect(page.getByRole('tab')).toHaveText(['Details']);\n"
        self.assertIn("'tab'", self.findings(code)["named, scoped locators"]["detail"])
        scoped = SPEC + "\nawait expect(sheet.getByRole('tab')).toHaveText(['Details']);\n"
        self.assertNotIn("named, scoped locators", self.findings(scoped))
        landmark = SPEC + "\nconst main = page.getByRole('main');\n"
        self.assertNotIn("named, scoped locators", self.findings(landmark))

    def test_twelve_hour_option_is_flagged(self):
        code = SPEC + "\nawait form.pickTime('Start', '6:30 PM');\n"
        self.assertIn("6:30 PM", self.findings(code)["option values from the UI"]["detail"])
        self.assertNotIn("option values from the UI", self.findings(SPEC + "\nawait form.pickTime('Start', '18:30');\n"))


if __name__ == "__main__":
    unittest.main()
