"""
The LangGraph workflow: transitions, bounded repairs, the approval gate, resuming
after a crash, and publishing that never repeats a side effect.
"""
import itertools
import unittest
from pathlib import Path

from taf.agents.automation.agent import max_generations
from taf.config import PROJECT_ROOT
from taf.integrations.github.repository import TEST_REPO, CommandError
from taf.runs import stages
from taf.runs.store import get_store
from taf.steps.publish import publish
from tests.support import DESCRIPTION, FakePullRequests, Workbench, git

_TICKETS = itertools.count(1)


class Crash(BaseException):
    """Escapes every `except Exception` - stands in for the process dying mid-run."""


def new_run(stage: str = "full", provider: str = "ollama"):
    ticket = f"WF-{next(_TICKETS)}"
    state = stages.prepare(ticket, stage, summary=f"{ticket} summary", description=DESCRIPTION,
                           target_url="https://app.example.test", provider=provider, source="seed")
    return ticket, state


def phases(run_id):
    run = next(r for r in get_store().recent_runs() if r["run_id"] == run_id)
    return [(p["phase"], p["state"]) for p in run["phases"]], run


class WorkflowTestCase(unittest.TestCase):
    settings: dict = {}

    def setUp(self):
        self.wb = Workbench(**self.settings).start()
        self.addCleanup(self.wb.close)


class ApprovalAndPublishTest(WorkflowTestCase):
    settings = {"git_push_enabled": True, "pr_create_enabled": True}

    def test_waits_for_approval_then_publishes_once(self):
        ticket, state = new_run()
        final = stages.execute(state, "full")

        self.assertEqual(final["_run_state"], "waiting", final.get("errors"))
        self.assertEqual(final["_approval_request"]["branch"], f"qa/{ticket}")
        self.assertTrue(final["_approval_request"]["validation_ok"])
        self.assertEqual(self.wb.remote_files(f"qa/{ticket}"), [], "nothing may be pushed before approval")
        self.assertTrue(self.wb.worktree_clean(), "validation must not leave files in the shared clone")
        recorded, run = phases(state["run_id"])
        self.assertEqual(recorded[-1], ("approval", "waiting"))
        self.assertEqual(run["state"], "waiting")
        triage = next(p for p in run["phases"] if p["phase"] == "triage")
        self.assertIn("3 criteria", triage["detail"])  # details come from this run's outputs
        self.assertEqual(len(triage["artifact"]["acceptance_criteria"]), 3)
        with self.assertRaises(stages.StageError):  # a waiting ticket blocks a second run
            stages.prepare(ticket, "full", summary="x", source="seed")

        final = stages.resume(state["run_id"], {"approved": True, "by": "reviewer"})

        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        files = self.wb.remote_files(f"qa/{ticket}")
        self.assertIn(f"e2e/qa/{ticket}.spec.ts", files)
        self.assertIn(f"xray/{ticket}/{ticket}.xray.json", files)
        self.assertEqual(len(FakePullRequests.created), 1)
        self.assertFalse(FakePullRequests.created[0]["draft"])
        self.assertEqual(final["publish"]["pr_url"], "https://github.example/pr/100")
        self.assertIn("**Pull request:** https://github.example/pr/100", final["final_report"])
        self.assertIn("**Approval:** approved by reviewer", final["final_report"])
        recorded, run = phases(state["run_id"])
        self.assertEqual([p for p, _ in recorded], ["triage", "architect", "automation", "approval", "publish", "report"])
        self.assertTrue(all(s == "ok" for _, s in recorded), recorded)
        self.assertEqual(self.wb.calls["text"], 1, "the spec is generated once")

        # Publishing again (a resume that re-enters the step) changes nothing.
        commits = self.wb.remote_commits(f"qa/{ticket}")
        again = publish(final)
        self.assertFalse(again["publish"]["new_commit"])
        self.assertFalse(again["publish"]["pr_created"])
        self.assertEqual(self.wb.remote_commits(f"qa/{ticket}"), commits)
        self.assertEqual(len(FakePullRequests.created), 1)

        with self.assertRaises(stages.StageError):
            stages.resume(state["run_id"])  # finished runs cannot be resumed

    def test_reject_publishes_nothing(self):
        ticket, state = new_run()
        self.assertEqual(stages.execute(state, "full")["_run_state"], "waiting")
        with self.assertRaises(stages.StageError):
            stages.resume(state["run_id"])  # needs a decision
        final = stages.resume(state["run_id"], {"approved": False, "by": "reviewer", "note": "wrong scope"})
        self.assertEqual(final["_run_state"], "completed")
        self.assertEqual(final["approval"]["status"], "rejected")
        self.assertEqual(self.wb.remote_files(f"qa/{ticket}"), [])
        self.assertEqual(FakePullRequests.created, [])
        self.assertIn("rejected", final["final_report"])

    def test_failing_playwright_run_opens_a_draft(self):
        self.wb.play_ok = False
        ticket, state = new_run()
        stages.execute(state, "full")
        final = stages.resume(state["run_id"], {"approved": True, "by": "r"})
        self.assertTrue(FakePullRequests.created[0]["draft"])
        self.assertIn("this PR is a draft", FakePullRequests.created[0]["body"])
        self.assertFalse(final["execution_passed"])

    def test_existing_remote_branch_is_built_upon_not_rewritten(self):
        ticket, state = new_run()
        stages.execute(state, "full")
        stages.resume(state["run_id"], {"approved": True, "by": "r"})
        first = self.wb.remote_commits(f"qa/{ticket}")

        self.wb.specs = [self.wb.specs[0].replace("first", "first, revised")]
        state2 = stages.prepare(ticket, "full", summary=f"{ticket} summary", description=DESCRIPTION,
                                provider="ollama", source="seed")
        stages.execute(state2, "full")
        final = stages.resume(state2["run_id"], {"approved": True, "by": "r"})
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertEqual(self.wb.remote_commits(f"qa/{ticket}"), first + 1, "a new commit on top, history kept")
        self.assertEqual(len(FakePullRequests.created), 1, "the open PR is reused")


class N8nPathTest(WorkflowTestCase):
    settings = {"git_push_enabled": True}

    def test_n8n_run_pauses_for_approval_and_resumes_into_closeout(self):
        import uuid

        from taf.runs.registry import RUNS, execute_qa_run

        run_id = uuid.uuid4().hex
        RUNS.try_start(run_id, "N8N-1")
        execute_qa_run({"run_id": run_id, "ticket_id": "N8N-1", "summary": "N8N-1", "description": DESCRIPTION,
                        "target_url": "https://app.example.test", "n8n_callback_url": "", "github_diff": "",
                        "environment_logs": "", "errors": [], "llm_provider": "ollama"})
        self.assertEqual(RUNS.get(run_id)["status"], "waiting_approval")
        final = stages.resume(run_id, {"approved": True, "by": "r"})
        self.assertIn("N8N-1.spec.ts", " ".join(self.wb.remote_files("qa/N8N-1")))
        self.assertTrue(any("no n8n_callback_url" in e for e in final["errors"]), "closeout ran after the report")


class RepairLoopTest(WorkflowTestCase):
    settings = {"require_approval": False}

    def test_load_failure_is_repaired(self):
        self.wb.list_ok = [False, True]
        _, state = new_run()
        final = stages.execute(state, "full")
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertEqual(final["spec_attempts"], 2)
        self.assertEqual(self.wb.calls["text"], 2)
        repair_turn = self.wb.text_messages[1][-1].content[0]["text"]
        self.assertIn("SyntaxError: Unexpected token", repair_turn)
        self.assertEqual(final["approval"]["status"], "not_required")
        self.assertTrue(final["publish"]["committed"])
        self.assertFalse(final["publish"]["pushed"])

    def test_repairs_are_bounded_and_nothing_is_published(self):
        self.wb.list_ok = [False]
        ticket, state = new_run()
        final = stages.execute(state, "full")
        self.assertEqual(final["_run_state"], "failed")
        self.assertEqual(self.wb.calls["text"], max_generations())
        self.assertFalse(final["validation"]["ok"])
        self.assertEqual(self.wb.calls["play"], 0)
        self.assertNotIn("publish", final)
        self.assertEqual(git(self.wb.repo, "branch", "--list", f"qa/{ticket}").strip(), "")
        recorded, _ = phases(state["run_id"])
        self.assertEqual([p for p, _ in recorded], ["triage", "architect", "automation", "report"])
        self.assertIn("Static validation failed", final["final_report"])

    def test_unsafe_spec_is_never_loaded(self):
        unsafe = "import { execSync } from 'child_process';\n" + self.wb.specs[0]
        self.wb.specs = [unsafe, self.wb.specs[0]]
        _, state = new_run()
        final = stages.execute(state, "full")
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertEqual(self.wb.calls["list"], 1, "only the safe second attempt was loaded")
        self.assertIn("child_process", self.wb.text_messages[1][-1].content[0]["text"])

    def test_missing_module_is_rejected(self):
        broken = "import { Nope } from '../pages/nope.page';\n" + self.wb.specs[0]
        self.wb.specs = [broken]
        _, state = new_run()
        final = stages.execute(state, "full")
        failed = [c for c in final["validation"]["checks"] if not c["ok"]]
        self.assertEqual(failed[0]["name"], "imports resolve")
        self.assertEqual(self.wb.calls["list"], 0)

    def test_environment_failure_is_not_retried(self):
        self.wb.deps_ok = False
        _, state = new_run()
        final = stages.execute(state, "full")
        self.assertEqual(final["_run_state"], "failed")
        self.assertEqual(self.wb.calls["text"], 1)
        self.assertFalse(final["validation"]["retryable"])

    def test_review_blocker_triggers_a_repair(self):
        sleepy = self.wb.specs[0].replace("await expect(page)", "await page.waitForTimeout(500); await expect(page)", 1)
        self.wb.specs = [sleepy, self.wb.specs[0]]
        _, state = new_run()
        final = stages.execute(state, "full")
        self.assertEqual(self.wb.calls["text"], 2)
        self.assertIn("no fixed sleeps", self.wb.text_messages[1][-1].content[0]["text"])
        self.assertFalse(final["review"]["blocking"])


class ResumeAfterCrashTest(WorkflowTestCase):
    settings = {"require_approval": False}

    def test_resume_continues_without_repeating_completed_steps(self):
        self.wb.play_raises = Crash()
        _, state = new_run()
        with self.assertRaises(Crash):
            stages.execute(state, "full")
        self.assertEqual((self.wb.calls["text"], self.wb.calls["play"]), (1, 1))

        final = stages.resume(state["run_id"])
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertEqual(self.wb.calls["text"], 1, "the spec was not generated again")
        self.assertEqual(self.wb.calls["play"], 2, "the interrupted step ran again")
        self.assertTrue(final["publish"]["committed"])


class StagesTest(WorkflowTestCase):
    def test_automation_stage_stops_before_approval(self):
        _, state = new_run(stage="automation")
        final = stages.execute(state, "automation")
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertTrue(final["validation"]["ok"])
        self.assertNotIn("approval", final)
        self.assertNotIn("final_report", final)

    def test_offline_never_touches_the_test_repo(self):
        _, state = new_run(provider="offline")
        final = stages.execute(state, "full")
        self.assertEqual(final["_run_state"], "completed")
        self.assertEqual(self.wb.calls["list"], 0)
        self.assertIn("NOT written to the test repository", final["execution_logs"])
        self.assertTrue(self.wb.worktree_clean())


class OrchestratorGuardTest(unittest.TestCase):
    def test_test_repo_inside_this_project_is_refused(self):
        from unittest import mock

        for path in (PROJECT_ROOT / "search-e2e", PROJECT_ROOT, PROJECT_ROOT.parent):
            with mock.patch.object(TEST_REPO, "path", Path(path)):
                with self.assertRaises(CommandError):
                    TEST_REPO.assert_outside_orchestrator()


if __name__ == "__main__":
    unittest.main()
