"""The service starts, the dashboard serves, and runs can be started and the provider switched over HTTP."""
import time
import unittest

from fastapi.testclient import TestClient

from main import app
from taf.llm.router import ROUTER


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client_cm = TestClient(app)
        cls.client = cls.client_cm.__enter__()

    @classmethod
    def tearDownClass(cls):
        cls.client_cm.__exit__(None, None, None)
        ROUTER.set_default("offline")

    def test_health(self):
        body = self.client.get("/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["llm_router"]["resolved"], "offline")
        self.assertIn("triage", body["claude_routes"])

    def test_dashboard_page_and_board(self):
        self.assertEqual(self.client.get("/dashboard").status_code, 200)
        board = self.client.get("/dashboard/api/board").json()
        self.assertIn("TB-601", [r["jira"]["key"] for r in board["rows"]])
        self.assertIn("offline", board["providers"])

    def test_switch_provider(self):
        resp = self.client.post("/dashboard/api/provider", json={"provider": "ollama"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["routes"]["triage"]["provider"], "ollama")
        self.assertEqual(self.client.post("/dashboard/api/provider", json={"provider": "gpt"}).status_code, 422)
        self.client.post("/dashboard/api/provider", json={"provider": "offline"})

    def test_start_run_from_dashboard(self):
        resp = self.client.post("/dashboard/api/start",
                                json={"ticket_id": "TB-601", "stage": "architect", "provider": "offline"})
        self.assertEqual(resp.status_code, 200, resp.text)
        run_id = resp.json()["run_id"]
        deadline = time.time() + 30
        while time.time() < deadline:
            run = self.client.get(f"/dashboard/api/run/{run_id}").json()
            if run["state"] != "running":
                break
            time.sleep(0.2)
        self.assertEqual(run["state"], "completed")
        self.assertEqual([p["phase"] for p in run["phases"]], ["triage", "architect"])

    def test_decide_and_resume_validate_the_run(self):
        resp = self.client.post("/dashboard/api/decide/does-not-exist", json={"approved": True})
        self.assertEqual(resp.status_code, 409)
        resp = self.client.post("/dashboard/api/resume/does-not-exist")
        self.assertEqual(resp.status_code, 409)

    def test_dashboard_approval_resumes_a_waiting_run(self):
        from taf.runs import stages
        from tests.support import DESCRIPTION, Workbench

        wb = Workbench(git_push_enabled=True).start()
        self.addCleanup(wb.close)
        state = stages.prepare("API-1", "full", summary="API-1", description=DESCRIPTION, provider="ollama",
                               source="seed")
        self.assertEqual(stages.execute(state, "full")["_run_state"], "waiting")
        board = self.client.get("/dashboard/api/board").json()
        row = next(r for r in board["rows"] if r["jira"]["key"] == "API-1")
        self.assertEqual(row["run"]["state"], "waiting")

        resp = self.client.post(f"/dashboard/api/decide/{state['run_id']}",
                                json={"approved": False, "by": "tester", "note": "not now"})
        self.assertEqual(resp.status_code, 200, resp.text)
        deadline = time.time() + 30
        while time.time() < deadline:
            run = self.client.get(f"/dashboard/api/run/{state['run_id']}").json()
            if run["state"] not in ("running", "waiting"):
                break
            time.sleep(0.2)
        self.assertEqual(run["state"], "completed")
        approval = next(p for p in run["phases"] if p["phase"] == "approval")
        self.assertEqual(approval["artifact"]["decision"]["status"], "rejected")
        self.assertEqual(wb.remote_files("qa/API-1"), [])

    def test_n8n_endpoint_validates(self):
        resp = self.client.post("/qa/runs", json={"ticket_id": "../etc", "summary": "x",
                                                  "target_url": "https://a.test", "n8n_callback_url": "https://b.test"})
        self.assertEqual(resp.status_code, 422)


if __name__ == "__main__":
    unittest.main()
