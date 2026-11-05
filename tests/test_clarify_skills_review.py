"""
The clarify gate after triage, run-aware skill selection, and `taf review <branch>`.
"""
import itertools
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from taf.runs import stages
from taf.runs.store import get_store
from taf.skills import diff_paths, select_skills, skills_used
from taf.steps import clarify
from taf.steps.branch_review import review_branch, to_markdown
from tests.support import DESCRIPTION, Workbench, git, patched_settings

_TICKETS = itertools.count(1)
VAGUE = "Customers say the itinerary PDF export is not great. Make it better and faster."


def new_run(description: str = VAGUE, stage: str = "full"):
    ticket = f"CL-{next(_TICKETS)}"
    state = stages.prepare(ticket, stage, summary=f"{ticket} export", description=description,
                           target_url="https://app.example.test", provider="ollama", source="seed")
    return ticket, state


def run_row(run_id):
    return next(r for r in get_store().recent_runs() if r["run_id"] == run_id)


# ---------------------------------------------------------------- clarify --
class ClarifyChecksTest(unittest.TestCase):
    def state(self, **extra):
        base = {"ticket_id": "T-1", "summary": "Export", "description": DESCRIPTION, "target_url": "https://x.test",
                "github_diff": "diff --git a/a.ts b/a.ts\n+x", "triage_analysis": {"acceptance_criteria": ["a"]}}
        return {**base, **extra}

    def test_clear_ticket_has_no_blocking_question(self):
        self.assertEqual(clarify.check_questions(self.state()), [])
        self.assertEqual(clarify.clarify_request(self.state())["reasons"], [])

    def test_each_deterministic_check(self):
        questions = clarify.check_questions(self.state(description=VAGUE, github_diff="", target_url=""))
        self.assertEqual(len(questions), 3)
        self.assertTrue(all(q["blocking"] for q in questions))
        self.assertIn("no acceptance criteria", questions[0]["text"])
        self.assertIn("No PR diff", questions[1]["text"])
        self.assertIn("No target URL", questions[2]["text"])

    def test_route_unknown_to_the_page_contract(self):
        contract = Path(tempfile.mkdtemp()) / "contract.md"
        self.addCleanup(shutil.rmtree, contract.parent, ignore_errors=True)
        contract.write_text("Pages: /tsp/itinerary and /trips\nimport x from '../pages/a.page';\n", encoding="utf-8")
        with patched_settings(page_contract_path=str(contract)):
            ok = clarify.check_questions(self.state(description=DESCRIPTION + "Open /tsp/itinerary/day-2 first."))
            bad = clarify.check_questions(self.state(description=DESCRIPTION + "Open /reports/pdf first."))
        self.assertEqual(ok, [])
        self.assertIn("/reports/pdf", bad[0]["text"])

    def test_triage_questions_and_low_confidence(self):
        state = self.state(triage_analysis={"acceptance_criteria": ["a"], "confidence": 0.4, "questions": [
            {"text": "Which formats?", "blocking": True, "criterion_ref": "AC-1"}, {"text": "Copy?", "blocking": False}]})
        request = clarify.clarify_request(state)
        self.assertEqual([q["id"] for q in request["questions"]], ["Q1", "Q2"])
        self.assertEqual(len(request["reasons"]), 2)  # one blocking question + confidence below 0.7

    def test_decisions(self):
        self.assertEqual(clarify.validate_decision({"answers": {"Q1": " PDF "}})["answers"], {"Q1": "PDF"})
        listed = clarify.validate_decision({"action": "answer", "answers": [{"id": "Q2", "answer": "yes"}]})
        self.assertEqual(listed["answers"], {"Q2": "yes"})
        self.assertEqual(clarify.validate_decision({"action": "proceed"})["action"], "proceed")
        for bad in ({"action": "answer", "answers": {"Q1": "  "}}, {"approved": True}, "yes"):
            with self.assertRaises(ValueError):
                clarify.validate_decision(bad)

    def test_prompt_block_only_when_there_is_something_to_say(self):
        self.assertEqual(clarify.clarifications_block({}), "")
        block = clarify.clarifications_block({"clarifications": [{"id": "Q1", "question": "Which?", "answer": "PDF"}],
                                              "clarify": {"risks": ["Speed target?"]}})
        self.assertIn("Answer: PDF", block)
        self.assertIn("Speed target?", block)


class ClarifyGateOffTest(unittest.TestCase):
    def setUp(self):
        self.wb = Workbench(require_approval=False).start()
        self.addCleanup(self.wb.close)

    def test_default_records_the_questions_and_does_not_pause(self):
        _, state = new_run()
        final = stages.execute(state, "full")
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertEqual(final["clarify"]["status"], "not_required")
        self.assertIn("CLARIFY_GATE=false", final["clarify"]["note"])
        self.assertIn("**Clarify gate:** not asked", final["final_report"])
        self.assertIn("**Open questions (unanswered):**", final["final_report"])


class ClarifyGateOnTest(unittest.TestCase):
    def setUp(self):
        self.wb = Workbench(require_approval=False, clarify_gate=True).start()
        self.addCleanup(self.wb.close)

    def pause(self):
        ticket, state = new_run()
        final = stages.execute(state, "full")
        self.assertEqual(final["_run_state"], "needs_clarification", final.get("errors"))
        return ticket, state, final

    def test_unclear_ticket_pauses_before_design_and_resumes_with_answers(self):
        ticket, state, final = self.pause()
        request = final["_clarify_request"]
        self.assertEqual(request["kind"], "clarification")
        self.assertTrue(any("no acceptance criteria" in q["text"] for q in request["questions"]))
        self.assertNotIn("test_cases", final, "nothing is designed before the questions are answered")
        self.assertEqual(self.wb.calls["text"], 0)
        run = run_row(state["run_id"])
        self.assertEqual(run["state"], "needs_clarification")
        triage = next(p for p in run["phases"] if p["phase"] == "triage")
        self.assertEqual(triage["state"], "waiting")
        self.assertEqual(triage["artifact"]["clarify"]["status"], "waiting")
        self.assertEqual(stages.pending_clarification(state["run_id"])["questions"], request["questions"])
        with self.assertRaises(stages.StageError):  # still blocks a second run of the ticket
            stages.prepare(ticket, "full", summary="x", source="seed")
        with self.assertRaises(stages.StageError):  # an approval is not an answer
            stages.resume(state["run_id"], {"approved": True})
        with self.assertRaises(stages.StageError):
            stages.resume(state["run_id"])

        final = stages.resume(state["run_id"], {"action": "answer", "answers": {"Q1": "PDF loads in under 2 s"},
                                                "by": "qa-lead"})
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertEqual(final["clarify"]["status"], "answered")
        self.assertEqual(final["clarifications"][0]["answer"], "PDF loads in under 2 s")
        self.assertTrue(final["test_cases"])
        prompt = str(self.wb.text_messages[0])
        self.assertIn("Clarifications from the team", prompt)
        self.assertIn("PDF loads in under 2 s", prompt)
        self.assertIn("**Answers from the team:**", final["final_report"])
        phases = [p["phase"] for p in run_row(state["run_id"])["phases"]]
        self.assertEqual(phases[:2], ["triage", "architect"])

    def test_proceed_records_the_questions_as_risks(self):
        _, state, _ = self.pause()
        final = stages.resume(state["run_id"], {"action": "proceed", "by": "qa"})
        self.assertEqual(final["clarify"]["status"], "proceeded")
        self.assertTrue(final["clarify"]["risks"])
        self.assertIn("**Tested without an answer (risks):**", final["final_report"])
        self.assertIn("Still open", str(self.wb.text_messages[0]))

    def test_reject_goes_to_the_report(self):
        _, state, _ = self.pause()
        final = stages.resume(state["run_id"], {"action": "reject", "by": "qa", "note": "ticket is a draft"})
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertEqual(final["clarify"]["status"], "rejected")
        self.assertNotIn("test_cases", final)
        self.assertIn("rejected - no tests designed", final["final_report"])

    def test_triage_only_stage_never_reaches_the_gate(self):
        _, state = new_run(stage="triage")
        final = stages.execute(state, "triage")
        self.assertEqual(final["_run_state"], "completed")
        self.assertNotIn("clarify", final)


# ----------------------------------------------------------------- skills --
class SkillSelectionTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="taf-skills-"))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.skill("a-plain", "agents: automation", "Plain rules.")
        self.skill("b-api", "agents: automation\nlayers: api\npriority: 5", "API rules.")
        self.skill("c-negative", "agents: automation\ntriggers: negative, src/**/*.cs  # globs or categories",
                   "Negative rules.")
        self.skill("d-update", "agents: automation\nactions: update, regression", "Update rules.")
        refs = self.root / "b-api" / "references"
        refs.mkdir()
        (refs / "negative.md").write_text("---\ntriggers: negative\n---\nEach required field omitted.", encoding="utf-8")
        (refs / "never.md").write_text("Loaded only with triggers.", encoding="utf-8")

    def skill(self, name, front, body):
        (self.root / name).mkdir()
        (self.root / name / "SKILL.md").write_text(f"---\nname: {name}\n{front}\n---\n{body}\n", encoding="utf-8")

    def select(self, state, provider="claude", **settings):
        with patched_settings(skills_dir=str(self.root), **settings):
            return select_skills("automation", provider, state)

    def test_ui_run_skips_api_skill_and_says_why(self):
        sel = self.select({})
        self.assertEqual(sel.record["loaded"], ["a-plain"])
        reasons = {s["name"]: s["reason"] for s in sel.record["skipped"]}
        self.assertIn("layer ui", reasons["b-api"])
        self.assertIn("no trigger", reasons["c-negative"])
        self.assertIn("action create", reasons["d-update"])
        self.assertNotIn("API rules", sel.prompt)

    def test_api_run_loads_it_first_with_triggered_reference(self):
        state = {"layer": "api", "test_cases": [{"category": "negative"}]}
        sel = self.select(state)
        self.assertEqual(sel.record["loaded"], ["b-api", "a-plain", "c-negative"])  # priority first
        self.assertEqual(sel.record["references"], ["b-api/negative"])
        self.assertIn("Each required field omitted.", sel.prompt)
        self.assertNotIn("Loaded only with triggers", sel.prompt)

    def test_diff_globs_actions_and_both_layer(self):
        diff = "diff --git a/src/Api/Trips.cs b/src/Api/Trips.cs\n+++ b/src/Api/Trips.cs\n"
        self.assertEqual(diff_paths({"github_diff": diff}), ["src/Api/Trips.cs"])
        sel = self.select({"github_diff": diff, "layer": "both", "action": "regression_only"})
        self.assertEqual(sel.record["loaded"], ["b-api", "a-plain", "c-negative", "d-update"])

    def test_no_references_on_local_routes(self):
        sel = self.select({"layer": "api", "test_cases": [{"category": "negative"}]}, provider="ollama",
                          skills_on_local=True)
        self.assertIn("b-api", sel.record["loaded"])
        self.assertEqual(sel.record["references"], [])

    def test_budget_overflow_is_recorded(self):
        sel = self.select({"layer": "api"}, skills_max_chars=40)
        self.assertEqual(sel.record["loaded"], ["b-api"])
        self.assertTrue(any("SKILLS_MAX_CHARS" in s["reason"] for s in sel.record["skipped"]))

    def test_record_merges_per_agent(self):
        with patched_settings(skills_dir=str(self.root)):
            used = skills_used({"skills_used": {"triage": {"loaded": ["x"]}}}, "automation", "claude")
        self.assertEqual(set(used), {"triage", "automation"})


# ----------------------------------------------------------------- review --
CLEAN = """import { test, expect } from '@playwright/test';
test('opens', async ({ page }) => {
  await expect(page.getByRole('main')).toBeVisible();
});
"""


class BranchReviewTest(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="taf-review-"))
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.repo)], check=True)
        (self.repo / "e2e").mkdir()
        (self.repo / "e2e" / "a.spec.ts").write_text(CLEAN + "test.only('old', async () => {});\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "base")
        git(self.repo, "checkout", "-q", "-b", "feature")
        (self.repo / "e2e" / "a.spec.ts").write_text(
            CLEAN + "test.only('old', async () => {});\n"
            "test('waits', async ({ page }) => {\n  await page.waitForTimeout(500);\n});\n", encoding="utf-8")
        (self.repo / "README.md").write_text("docs\n", encoding="utf-8")
        git(self.repo, "add", "-A")
        git(self.repo, "commit", "-q", "-m", "feature")
        git(self.repo, "checkout", "-q", "main")

    def test_reports_the_new_sleep_at_its_line_and_changes_nothing(self):
        head = git(self.repo, "rev-parse", "HEAD")
        review = review_branch("feature", base="main", repo=self.repo, fetch=False)
        self.assertEqual([s.path for s in review.specs], ["e2e/a.spec.ts"])
        found = {f["rule"]: f for f in review.specs[0].findings}
        self.assertEqual(found["no fixed sleeps"]["lines"], [7])
        self.assertEqual(found["no fixed sleeps"]["where"], "new")
        self.assertEqual(found["no .only"]["where"], "pre-existing")
        self.assertEqual(review.blockers, 1, "only what the branch introduces counts")
        self.assertTrue(any("not checked out" in n for n in review.notes))
        text = to_markdown(review)
        self.assertIn("e2e/a.spec.ts:7", text)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD"), head)
        self.assertEqual(git(self.repo, "status", "--porcelain"), "")

    def test_unknown_branch(self):
        from taf.steps.branch_review import ReviewError
        with self.assertRaises(ReviewError):
            review_branch("nope", base="main", repo=self.repo, fetch=False)


if __name__ == "__main__":
    unittest.main()
