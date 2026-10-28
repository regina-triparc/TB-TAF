"""The HTML report: automation results, per-case checklist, gaps, screenshots inlined."""
import json
import re
import tempfile
import unittest
from pathlib import Path

from taf.agents.reporter.html_report import build_report_data, render_html

PNG = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010806000000"
                    "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082")


def case(tc, automatable=True, refs=("AC-1",)):
    return {"test_id": tc, "summary": f"{tc} summary", "kind": "functional", "category": "happy-path",
            "priority": "High", "automatable": automatable, "preconditions": "",
            "acceptance_criteria_refs": list(refs),
            "steps": [{"action": "Do it", "data": "", "expected_result": "It </script> works"}]}


class HtmlReportTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        shot = Path(self.tmp.name) / "test-failed-1.png"
        shot.write_bytes(PNG)
        self.state = {
            "ticket_id": "TB-1", "run_id": "r1", "summary": "S", "target_url": "https://t.example",
            "execution_passed": False,
            "triage_analysis": {"acceptance_criteria": ["First works.", "Second works.", "Third works."],
                                "gaps_and_questions": ["Which locale?"], "risk_level": "medium"},
            "test_cases": [case("TC-001"), case("TC-002", refs=("AC-2",)), case("TC-003", automatable=False, refs=())],
            "playwright": {"counts": {"passed": 1}, "tests": [
                {"title": "[TC-001] a", "tc": "TC-001", "status": "failed", "duration_ms": 1200,
                 "error": "boom", "screenshots": [str(shot)]}]},
            "review": {"findings": [{"severity": "major", "rule": "criteria coverage", "detail": "AC-3"}]},
        }

    def tearDown(self):
        self.tmp.cleanup()

    def test_data_has_results_and_gaps(self):
        data = build_report_data(self.state)
        self.assertEqual(data["cases"][0]["automation"]["status"], "failed")
        self.assertTrue(data["automation"][0]["screenshots"][0]["src"].startswith("data:image/png;base64,"))
        self.assertEqual([c["n"] for c in data["gaps"]["uncovered_criteria"]], [3])
        self.assertEqual(data["gaps"]["manual_only"], ["TC-003"])
        self.assertEqual(data["gaps"]["not_run"], ["TC-002"])
        self.assertEqual(data["gaps"]["questions"], ["Which locale?"])

    def test_render_embeds_parseable_data(self):
        html = render_html(build_report_data(self.state))
        self.assertIn("<title>TB-1 QA Report</title>", html)
        payload = re.search(r'<script type="application/json" id="report-data">(.*?)</script>', html, re.S).group(1)
        self.assertEqual(json.loads(payload)["ticket_id"], "TB-1")
        self.assertNotIn("It </script> works", html)


if __name__ == "__main__":
    unittest.main()
