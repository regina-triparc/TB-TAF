"""LLM router decisions, skill loading, Xray output and the offline eval run."""
import os
import unittest
from unittest import mock

from taf.agents.schemas import TestPlan
from taf.config import Settings
from taf.integrations.jira.xray import build_xray_csv, build_xray_tests_json
from taf.llm.router import LLMRouter
from taf.skills import load_skills, skills_prompt


def router_with(**env):
    with mock.patch.dict(os.environ, env):
        return LLMRouter(Settings())


class RouterTest(unittest.TestCase):
    def test_auto_without_key_is_ollama(self):
        self.assertEqual(router_with(LLM_PROVIDER="auto", ANTHROPIC_API_KEY="").resolve(), "ollama")

    def test_auto_with_key_is_claude(self):
        self.assertEqual(router_with(LLM_PROVIDER="auto", ANTHROPIC_API_KEY="sk-ant-test-key").resolve(), "claude")

    def test_cloud_agents_pin_inside_local_run(self):
        routes = router_with(LLM_PROVIDER="ollama", CLOUD_AGENTS="automation").routes()
        self.assertEqual(routes["triage"].provider, "ollama")
        self.assertEqual(routes["automation"].provider, "claude")

    def test_offline_ignores_pins(self):
        routes = router_with(LLM_PROVIDER="offline", CLOUD_AGENTS="all").routes()
        self.assertEqual({r.provider for r in routes.values()}, {"offline"})

    def test_per_run_override_beats_default(self):
        router = router_with(LLM_PROVIDER="claude")
        self.assertEqual(router.route("triage", "ollama").provider, "ollama")
        self.assertEqual(router.route("triage").provider, "claude")

    def test_runtime_switch_and_validation(self):
        router = router_with(LLM_PROVIDER="claude")
        router.set_default("ollama")
        self.assertEqual(router.resolve(), "ollama")
        with self.assertRaises(ValueError):
            router.set_default("gpt")

    def test_claude_without_key_fails_clearly(self):
        router = router_with(LLM_PROVIDER="claude", ANTHROPIC_API_KEY="")
        with self.assertRaisesRegex(RuntimeError, "ANTHROPIC_API_KEY"):
            router.chat_model(router.route("triage"))


class SkillsTest(unittest.TestCase):
    def test_skills_load(self):
        names = {s.name for s in load_skills()}
        self.assertTrue({"playwright-locators", "playwright-spec-structure", "risk-based-test-design"} <= names)

    def test_skills_apply_to_claude_only_by_default(self):
        self.assertIn("playwright-locators", skills_prompt("automation", "claude"))
        self.assertEqual(skills_prompt("automation", "ollama"), "")
        self.assertEqual(skills_prompt("automation", "offline"), "")
        self.assertNotIn("playwright-locators", skills_prompt("triage", "claude"))


class XrayTest(unittest.TestCase):
    def test_csv_and_json(self):
        plan = TestPlan.model_validate({
            "strategy": "s", "in_scope": ["a"],
            "test_cases": [{"test_id": "TC-001", "summary": "Add, with comma", "kind": "functional",
                            "category": "happy-path", "priority": "High", "automatable": True,
                            "steps": [{"action": "do", "expected_result": "see"},
                                      {"action": "do 2", "expected_result": "see 2"}]}],
        })
        csv_text = build_xray_csv(plan, "TB-1")
        self.assertEqual(len(csv_text.strip().splitlines()), 3)  # header + one row per step
        tests = build_xray_tests_json(plan, "TB-1")
        self.assertEqual(tests[0]["fields"]["project"]["key"], "TB")
        self.assertEqual(len(tests[0]["steps"]), 2)


class EvalsTest(unittest.TestCase):
    def test_offline_evals_pass(self):
        from evals.runner import load_cases, run_case
        for case in load_cases():
            result = run_case(case, "offline", "automation")
            self.assertTrue(result["passed"], result)


if __name__ == "__main__":
    unittest.main()
