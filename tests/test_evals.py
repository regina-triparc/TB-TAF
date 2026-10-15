"""The eval framework: graders, versioned results, comparison, and the judge's bookkeeping."""
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from evals import graders, runner
from evals.judge import DIMENSIONS, Score, Verdict, judge, judge_prompt


def case(test_id, summary, refs, automatable=True):
    return {"test_id": test_id, "summary": summary, "automatable": automatable, "acceptance_criteria_refs": refs,
            "steps": [{"action": f"do {summary}", "expected_result": f"{summary} is shown"}]}


def failed(checks):
    return {c["name"] for c in checks if not c["passed"]}


class GraderTest(unittest.TestCase):
    def test_traceability(self):
        criteria = ["A works", "B works", "C works"]
        cases = [case("TC-001", "alpha flow", ["AC-1"]), case("TC-002", "beta flow", [])]
        checks = graders.grade_plan(cases, {}, criteria)
        self.assertIn("every case cites a criterion", failed(checks))
        self.assertIn("every criterion is tested", failed(checks))

    def test_redundant_cases_are_flagged(self):
        cases = [case("TC-001", "filter hotels by price range", ["AC-1"]),
                 case("TC-002", "filter hotels by price range", ["AC-2"])]
        checks = graders.grade_plan(cases, {}, ["x", "y"])
        self.assertIn("no redundant cases", failed(checks))
        self.assertFalse(next(c for c in checks if c["name"] == "no redundant cases")["hard"])

    def test_ambiguity(self):
        checks = graders.grade_triage({"acceptance_criteria": ["x"], "gaps_and_questions": []},
                                      {"ambiguous": True, "min_questions": 1})
        self.assertIn("raises open questions", failed(checks))
        checks = graders.grade_triage({"acceptance_criteria": ["x"], "gaps_and_questions": ["How fast is faster?"],
                                       "risks": ["Export times out for long itineraries"]},
                                      {"ambiguous": True, "should_ask_about": ["faster", "branding"]})
        self.assertEqual(failed(checks), {"asks about the gaps"})

    def test_spec_safety_selectors_and_scope(self):
        code = ("import { execSync } from 'child_process';\nimport { test } from '@playwright/test';\n"
                "test('[TC-001] a', async ({ page }) => { await page.locator('#buy').click(); });\n"
                "test('[TC-001] a', async ({ page }) => { await page.goto('/checkout'); });\n")
        checks = graders.grade_spec(code, {"out_of_scope": ["checkout"]},
                                    [case("TC-001", "a", ["AC-1"]), case("TC-002", "b", ["AC-2"])])
        self.assertTrue({"no Node APIs or dynamic code", "user-facing locators", "no duplicate tests",
                         "stays out of 'checkout'", "every automatable case has a test"} <= failed(checks))
        hard = {c["name"] for c in checks if c["hard"] and not c["passed"]}
        self.assertEqual(hard, {"no Node APIs or dynamic code"})

    def test_score_groups_dimensions(self):
        checks = [graders.check("a", True, dimension="x"), graders.check("b", False, hard=False, dimension="x")]
        self.assertEqual(graders.score(checks)["dimensions"], {"x": "1/2"})


class RunnerTest(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = runner.main(list(argv))
        return code, out.getvalue()

    def test_offline_run_records_versions_and_compares(self):
        tmp = Path(tempfile.mkdtemp())
        a, b = tmp / "a.json", tmp / "b.json"
        code, _ = self.run_cli("--provider", "offline", "--case", "tb-601", "--label", "before", "--out", str(a))
        self.assertEqual(code, 0)
        self.run_cli("--provider", "offline", "--case", "tb-601", "--label", "after", "--out", str(b))
        meta = json.loads(a.read_text())["metadata"]
        for key in ("dataset_version", "prompts", "models", "git", "created_at"):
            self.assertIn(key, meta)
        self.assertIn("taf/agents/automation/prompt.md", meta["prompts"])
        self.assertEqual(meta["label"], "before")

        code, text = self.run_cli("compare", str(a), str(b))
        self.assertEqual(code, 0)
        self.assertIn("label: 'before' -> 'after'", text)
        self.assertIn("per dimension", text)

    def test_evals_do_not_trace_into_production(self):
        from taf.observability import tracing

        fresh = tracing.Tracer(tracing.SETTINGS)
        with mock.patch.object(tracing, "TRACER", fresh):
            runner._isolate_tracing(False)
            self.assertFalse(fresh.settings.langfuse_enabled)
            fresh2 = tracing.Tracer(tracing.SETTINGS)
            with mock.patch.object(tracing, "TRACER", fresh2):
                runner._isolate_tracing(True)
                self.assertEqual(fresh2.settings.langfuse_environment, "eval")

    def test_every_case_is_valid(self):
        for c in runner.load_cases():
            self.assertTrue({"id", "ticket", "expect"} <= set(c), c.get("id"))


class ReportTest(unittest.TestCase):
    def test_report_embeds_both_runs_and_outputs(self):
        tmp = Path(tempfile.mkdtemp())
        a, b = tmp / "a.json", tmp / "b.json"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            runner.main(["--provider", "offline", "--case", "tb-601", "--label", "local", "--out", str(a)])
            runner.main(["--provider", "offline", "--case", "tb-601", "--label", "cloud </script>", "--out", str(b)])
            code = runner.main(["report", str(a), str(b), "--out", str(tmp / "r.html")])
        self.assertEqual(code, 0)
        result = json.loads(a.read_text())["results"][0]
        self.assertTrue(result["outputs"]["spec"])
        self.assertIn("tokens", result["stages"]["triage"])
        page = (tmp / "r.html").read_text(encoding="utf-8")
        self.assertIn('"label": "local"', page)
        self.assertNotIn("cloud </script>", page, "data must not be able to close the script tag")


class JudgeTest(unittest.TestCase):
    def test_prompt_is_versioned(self):
        prompt = judge_prompt()
        self.assertEqual(prompt["name"], "scenario_quality.v1")
        self.assertEqual(len(prompt["sha256"]), 12)

    def test_judge_records_model_and_prompt(self):
        from taf.llm.router import ROUTER

        verdict = Verdict(**{d: Score(score=4, rationale="ok") for d in DIMENSIONS})
        with mock.patch.object(ROUTER, "structured", return_value=verdict):
            result = judge({"run_id": "eval-x", "ticket_id": "T-1"}, "ollama")
        self.assertEqual(result["mean"], 4.0)
        self.assertEqual(result["prompt"], "scenario_quality.v1")
        self.assertEqual(result["provider"], "ollama")
        self.assertTrue(result["model"])

    def test_offline_has_no_judge(self):
        self.assertIn("skipped", judge({"run_id": "eval-x"}, "offline"))


if __name__ == "__main__":
    unittest.main()
