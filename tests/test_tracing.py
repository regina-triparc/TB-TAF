"""
Langfuse tracing: off without keys, harmless when Langfuse fails, masked when on.

The "real client" test runs the actual Langfuse SDK with an in-memory OpenTelemetry
exporter instead of the network, so what would be sent can be inspected.
"""
import dataclasses
import json
import time
import unittest
from unittest import mock

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage

from taf.config import SETTINGS
from taf.observability import tracing
from taf.observability.tracing import Tracer, mask_value, scrub
from taf.runs import stages
from tests.support import DESCRIPTION


class MaskTest(unittest.TestCase):
    def test_tokens_are_redacted(self):
        text = ("key sk-ant-api03-abcdefghijklmnop and ghp_" + "a" * 36 + " Authorization: Bearer abc.def.ghi123 "
                "password=hunter22")
        out = scrub(text)
        for leaked in ("sk-ant-api03", "ghp_aaaa", "abc.def.ghi123", "hunter22"):
            self.assertNotIn(leaked, out)
        self.assertIn("Authorization: Bearer ***REDACTED***", out)

    def test_capture_modes(self):
        long = "x" * 10_000
        self.assertLess(len(mask_value({"a": [long]}, "truncated", 1000)["a"][0]), 1100)
        self.assertIn("not captured", mask_value(long, "metadata", 1000))
        self.assertEqual(mask_value("short id", "metadata", 1000), "short id")
        self.assertEqual(len(mask_value(long, "full", 1000)), 10_000)

    def test_configured_secret_is_redacted(self):
        with mock.patch.object(SETTINGS.__class__, "secrets", property(lambda _: ["s3cr3t-value-123"])):
            self.assertEqual(scrub("token s3cr3t-value-123 end"), "token ***REDACTED*** end")


class DisabledTest(unittest.TestCase):
    def test_no_keys_means_no_op(self):
        tracer = Tracer(SETTINGS)
        self.assertFalse(tracer.enabled)
        self.assertIn("not set", tracer.status)
        self.assertEqual(tracer.langchain_callbacks(), [])
        self.assertEqual(tracer.trace_id("run"), "")
        with tracer.run({"run_id": "r", "ticket_id": "T-1"}) as root:
            with tracer.node({"run_id": "r"}, "step") as span:
                span.update(output={"x": 1})
            root.update(output={})
        tracer.score("r", "x", 1)
        tracer.flush()

    def test_missing_package_disables_tracing(self):
        settings = dataclasses.replace(SETTINGS, langfuse_public_key="pk", langfuse_secret_key="sk")
        with mock.patch.dict("sys.modules", {"langfuse": None}):
            tracer = Tracer(settings)
            self.assertFalse(tracer.enabled)
            self.assertIn("could not start", tracer.status)


class _Broken:
    """A Langfuse client whose every call fails, or hangs."""

    def __getattr__(self, name):
        def fail(*_, **__):
            if name in ("flush", "shutdown", "get_trace_url"):
                time.sleep(30)
            raise ConnectionError(f"langfuse down ({name})")
        return fail


class FailureTest(unittest.TestCase):
    def test_a_failing_langfuse_never_breaks_or_stalls_a_run(self):
        settings = dataclasses.replace(SETTINGS, langfuse_flush_timeout_s=1)
        broken = Tracer(settings, client=_Broken())
        state = stages.prepare("TR-1", "full", summary="TR-1", description=DESCRIPTION, provider="offline",
                               source="seed")
        started = time.monotonic()
        with mock.patch.object(tracing, "TRACER", broken), \
                mock.patch("taf.agents.base.TRACER", broken), mock.patch("taf.runs.stages.TRACER", broken), \
                mock.patch("taf.llm.router.TRACER", broken), mock.patch("taf.agents.reporter.agent.TRACER", broken):
            final = stages.execute(state, "full")
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        self.assertIn("## QA Report - TR-1", final["final_report"])
        self.assertLess(time.monotonic() - started, 10, "flush / trace url must be bounded")


class RealClientTest(unittest.TestCase):
    """The Langfuse SDK itself, exporting to memory: trace ids, nesting, masking, generations."""

    @classmethod
    def setUpClass(cls):
        from langfuse import Langfuse
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        # The SDK keeps one client per public key, so one client (and exporter) for the class.
        cls.exporter = InMemorySpanExporter()
        settings = dataclasses.replace(SETTINGS, langfuse_capture="truncated", langfuse_max_field_chars=500)
        cls.tracer = Tracer(settings)
        cls.client = Langfuse(public_key="pk-lf-unit-test", secret_key="sk-lf-unit-test",
                              base_url="http://127.0.0.1:9", span_exporter=cls.exporter, mask=cls.tracer.mask)
        cls.tracer._client, cls.tracer._init_done = cls.client, True

    @classmethod
    def tearDownClass(cls):
        cls.tracer.shutdown()

    def setUp(self):
        self.tracer.flush()
        self.exporter.clear()
        self.scores = []
        # Scores and trace links go over Langfuse's HTTP API, not OpenTelemetry: record them here.
        patches = [mock.patch.object(self.tracer, "score", side_effect=lambda *a, **k: self.scores.append(a)),
                   mock.patch.object(self.tracer, "trace_url", return_value="https://langfuse.example/trace/x")]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)

    def spans(self):
        self.tracer.flush()
        return list(self.exporter.get_finished_spans())

    def test_run_step_and_llm_generation(self):
        from langfuse import Langfuse

        state = {"run_id": "run-abc", "ticket_id": "TB-9", "summary": "s", "llm_provider": "claude"}
        model = GenericFakeChatModel(messages=iter([AIMessage(
            content="spec here", usage_metadata={"input_tokens": 12, "output_tokens": 5, "total_tokens": 17})]))
        secret_prompt = "use ghp_" + "b" * 36 + " please " + "y" * 3000
        with self.tracer.run(state) as root:
            with self.tracer.node(state, "generate_spec", as_type="agent") as step:
                model.invoke([HumanMessage(content=secret_prompt)],
                             config={"callbacks": self.tracer.langchain_callbacks(), "run_name": "TS Automation Engineer"})
                step.update(output={"spec_attempts": 1})
            root.update(output={"run_state": "completed"})
        spans = self.spans()
        names = [s.name for s in spans]
        self.assertIn("qa-run", names)
        self.assertIn("generate_spec", names)
        self.assertIn("TS Automation Engineer", names)

        expected = Langfuse.create_trace_id(seed="run-abc")
        self.assertEqual({format(s.context.trace_id, "032x") for s in spans}, {expected})
        by_name = {s.name: s for s in spans}
        self.assertEqual(by_name["generate_spec"].parent.span_id, by_name["qa-run"].context.span_id)
        self.assertEqual(by_name["TS Automation Engineer"].parent.span_id, by_name["generate_spec"].context.span_id)

        everything = json.dumps([dict(s.attributes) for s in spans], default=str)
        self.assertNotIn("ghp_bbbb", everything)
        self.assertIn("REDACTED", everything)
        self.assertNotIn("y" * 600, everything, "long prompt text is truncated")
        self.assertIn("TB-9", everything)          # session / metadata
        self.assertIn("run-abc", everything)
        generation = dict(by_name["TS Automation Engineer"].attributes)
        self.assertEqual(generation.get("langfuse.observation.type"), "generation")

    def test_resumed_run_lands_in_the_same_trace(self):
        state = {"run_id": "run-resume", "ticket_id": "TB-9"}
        with self.tracer.run(state):
            pass
        with self.tracer.run(state, resumed=True):
            pass
        spans = self.spans()
        self.assertEqual(len({s.context.trace_id for s in spans}), 1)
        self.assertEqual(sorted(s.name for s in spans), ["qa-run", "qa-run.resume"])

    def test_workflow_steps_are_traced(self):
        from tests.support import Workbench

        wb = Workbench(require_approval=False).start()
        self.addCleanup(wb.close)
        state = stages.prepare("TR-2", "full", summary="TR-2", description=DESCRIPTION, provider="ollama",
                               source="seed")
        with mock.patch("taf.agents.base.TRACER", self.tracer), mock.patch("taf.runs.stages.TRACER", self.tracer), \
                mock.patch("taf.agents.reporter.agent.TRACER", self.tracer):
            final = stages.execute(state, "full")
        self.assertEqual(final["_run_state"], "completed", final.get("errors"))
        names = {s.name for s in self.spans()}
        self.assertIn(("static_validation"), [a[1] for a in self.scores])
        self.assertIn("**Trace:** https://langfuse.example/trace/x", final["final_report"])
        for step in ("qa-run", "gather_context", "intake_triager", "test_architect", "repo_context", "generate_spec",
                     "validate_spec", "run_tests", "review_spec", "approval_gate", "publish", "report_synthesizer"):
            self.assertIn(step, names)


if __name__ == "__main__":
    unittest.main()
