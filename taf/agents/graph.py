"""
The LangGraph workflow for one ticket. Agents (LLM) and deterministic steps:

    gather_context ─▶ classify_ticket ─▶ intake_triager ─▶ clarify_gate   (pauses when the ticket is unclear; opt-in)
         (git)          (rules)             (LLM)               │
                                                                ▼
                    coverage_lookup ─▶ test_architect ─▶ plan_gate   (pauses when the plan is unsure; opt-in)
                  (test repo, read)       (LLM)            │ action skip -> report, regression_only -> run_existing
                                                           ▼   (only with ROUTE_BY_CLASSIFICATION=true)
                                        test_architect ─▶ repo_context ─▶ ui_snapshot ─▶ generate_spec ◀──────┐
                                            (LLM)          (git, read)   (live app, read)     (LLM)            │
                                                                                                 │             │ repair while
                                                                                                 ▼             │ attempts remain
                                                                                           validate_spec ──────┤ (retryable failure,
                                                                                                 │ ok          │  or a review blocker)
                                                                                                 ▼             │
                                                                run_tests ─▶ repair_from_run ─▶ review_spec ───┘
                                                                              (LLM + rerun of the        │
                                                                               failing tests, guarded)   │
                                                                       approval_gate (interrupt) ────────┘
                                                                 │ approved / not required
                                                              publish ─▶ report_synthesizer ─▶ closeout

* An aborted agent (budget, unrecoverable LLM error), a validation failure that
  cannot be retried, or a rejection (at the clarify or the approval gate) jumps to the
  report - a run always explains itself.
* classify_ticket and triage decide an action and a layer (taf/steps/classify.py); they
  change the route only with ROUTE_BY_CLASSIFICATION on. coverage_lookup shows the
  architect the existing tests that match (taf/steps/coverage.py).
* clarify_gate pauses only when CLARIFY_GATE is on and the ticket is unclear
  (taf/steps/clarify.py); otherwise it records the questions and passes through.
* `stop_after` ends a partial run (the dashboard's triage / architect / automation
  stages); "report" stops before the n8n closeout.
* Every step is checkpointed (thread id = run id), so a crashed or paused run resumes
  where it stopped and completed side effects are not repeated.
"""
from __future__ import annotations

import functools
import sqlite3
from typing import Any, Callable, Dict, Optional

from langgraph.graph import END, START, StateGraph

from taf.agents.architect import test_architect
from taf.agents.automation import generate_spec, max_generations
from taf.agents.closeout import closeout
from taf.agents.reporter import report_synthesizer
from taf.agents.state import QAState
from taf.agents.triage import intake_triager
from taf.config import SETTINGS, project_path
from taf.steps.clarify import clarify_gate
from taf.steps.classify import classify_ticket
from taf.steps.coverage import coverage_lookup, plan_gate, run_existing
from taf.steps.context import gather_context, repo_context
from taf.steps.publish import approval_gate, has_output, publish
from taf.steps.review import review_spec
from taf.steps.run_repair import repair_from_run
from taf.steps.ui_snapshot import ui_snapshot
from taf.steps.validation import run_tests, validate_spec

# Graph node -> the phase the dashboard and run history show it under.
NODE_PHASE: Dict[str, str] = {
    "gather_context": "context",
    "classify_ticket": "context",
    "intake_triager": "triage",
    "clarify_gate": "triage",  # a pause here shows as triage "waiting"; the phase list is unchanged
    "coverage_lookup": "architect",
    "test_architect": "architect",
    "plan_gate": "architect",  # a pause here shows as architect "waiting"
    "run_existing": "automation",
    "repo_context": "automation",
    "ui_snapshot": "automation",
    "generate_spec": "automation",
    "validate_spec": "automation",
    "run_tests": "automation",
    "repair_from_run": "automation",
    "review_spec": "automation",
    "approval_gate": "approval",
    "publish": "publish",
    "report_synthesizer": "report",
    "closeout": "closeout",
}

REPORT = "report_synthesizer"


def _finish(state: QAState) -> str:
    """Where a run goes when its automation half ends early: END for partial stages, else the report."""
    return END if state.get("stop_after") in {"triage", "architect", "automation"} else REPORT


def _after_agent(stage: str, next_node: str) -> Callable[[QAState], str]:
    def route(state: QAState) -> str:
        if state.get("aborted"):
            return _finish(state)
        return END if state.get("stop_after") == stage else next_node

    return route


def _after_clarify(state: QAState) -> str:
    if state.get("aborted") or (state.get("clarify") or {}).get("status") == "rejected":
        return _finish(state)
    return "coverage_lookup"


def _after_plan(state: QAState) -> str:
    if state.get("aborted") or (state.get("plan_review") or {}).get("status") == "rejected":
        return _finish(state)
    if SETTINGS.route_by_classification:
        if state.get("action") == "skip":
            return _finish(state)
        if state.get("action") == "regression_only":
            return "run_existing"
    return "repo_context"


def _after_generate(state: QAState) -> str:
    return _finish(state) if state.get("aborted") else "validate_spec"


def _attempts_left(state: QAState) -> bool:
    return int(state.get("spec_attempts") or 0) < max_generations()


def _after_validate(state: QAState) -> str:
    validation = state.get("validation") or {}
    if validation.get("ok"):
        return "run_tests"
    if validation.get("retryable") and _attempts_left(state):
        return "generate_spec"
    return _finish(state)


def _after_review(state: QAState) -> str:
    review = state.get("review") or {}
    if review.get("blocking") and _attempts_left(state) and state.get("spec_feedback"):
        return "generate_spec"
    if state.get("stop_after") == "automation":
        return END
    return "approval_gate" if has_output(state) else REPORT


def _after_approval(state: QAState) -> str:
    status = (state.get("approval") or {}).get("status")
    return "publish" if status in {"approved", "not_required"} else REPORT


def _after_report(state: QAState) -> str:
    return END if state.get("stop_after") == "report" else "closeout"


def build_graph(checkpointer: Any = None):
    graph = StateGraph(QAState)
    for name, fn in (("gather_context", gather_context), ("intake_triager", intake_triager),
                     ("classify_ticket", classify_ticket), ("clarify_gate", clarify_gate),
                     ("coverage_lookup", coverage_lookup), ("plan_gate", plan_gate),
                     ("run_existing", run_existing),
                     ("test_architect", test_architect), ("repo_context", repo_context),
                     ("ui_snapshot", ui_snapshot), ("generate_spec", generate_spec),
                     ("validate_spec", validate_spec), ("run_tests", run_tests),
                     ("repair_from_run", repair_from_run), ("review_spec", review_spec),
                     ("approval_gate", approval_gate), ("publish", publish),
                     (REPORT, report_synthesizer), ("closeout", closeout)):
        graph.add_node(name, fn)

    graph.add_edge(START, "gather_context")
    graph.add_edge("gather_context", "classify_ticket")
    graph.add_edge("classify_ticket", "intake_triager")
    graph.add_conditional_edges("intake_triager", _after_agent("triage", "clarify_gate"),
                                ["clarify_gate", REPORT, END])
    graph.add_conditional_edges("clarify_gate", _after_clarify, ["coverage_lookup", REPORT, END])
    graph.add_edge("coverage_lookup", "test_architect")
    graph.add_conditional_edges("test_architect", _after_agent("architect", "plan_gate"),
                                ["plan_gate", REPORT, END])
    graph.add_conditional_edges("plan_gate", _after_plan, ["repo_context", "run_existing", REPORT, END])
    graph.add_conditional_edges("run_existing", _finish, [REPORT, END])
    graph.add_edge("repo_context", "ui_snapshot")
    graph.add_edge("ui_snapshot", "generate_spec")
    graph.add_conditional_edges("generate_spec", _after_generate, ["validate_spec", REPORT, END])
    graph.add_conditional_edges("validate_spec", _after_validate, ["run_tests", "generate_spec", REPORT, END])
    graph.add_edge("run_tests", "repair_from_run")
    graph.add_edge("repair_from_run", "review_spec")
    graph.add_conditional_edges("review_spec", _after_review, ["generate_spec", "approval_gate", REPORT, END])
    graph.add_conditional_edges("approval_gate", _after_approval, ["publish", REPORT])
    graph.add_edge("publish", REPORT)
    graph.add_conditional_edges(REPORT, _after_report, ["closeout", END])
    graph.add_edge("closeout", END)
    return graph.compile(checkpointer=checkpointer)


def sqlite_checkpointer(path: Optional[str] = None) -> Any:
    """Checkpoints in their own SQLite file (QA_CHECKPOINT_DB_PATH); qa_runs.db is not touched."""
    from langgraph.checkpoint.sqlite import SqliteSaver

    db_path = project_path(path or SETTINGS.qa_checkpoint_db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # The saver serialises access with its own lock; runs execute on worker threads.
    return SqliteSaver(sqlite3.connect(str(db_path), check_same_thread=False))


@functools.lru_cache(maxsize=1)
def qa_graph():
    """Compiled once, on first use, with the SQLite checkpointer."""
    return build_graph(sqlite_checkpointer())


def thread_config(run_id: str, recursion_limit: int = 60) -> Dict[str, Any]:
    return {"configurable": {"thread_id": run_id}, "recursion_limit": recursion_limit}
