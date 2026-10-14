"""
The LangGraph pipeline for one ticket:

    START -> intake_triager -> test_architect -> automation_engineer -> report_synthesizer -> closeout -> END

If a cloud node aborts (budget, unrecoverable LLM error) the graph jumps straight to
the report, so n8n always receives a "QA Failed" callback with an explanation.
"""
from __future__ import annotations

import functools
from typing import Callable

from langgraph.graph import END, START, StateGraph

from taf.agents.architect import test_architect
from taf.agents.automation import automation_engineer
from taf.agents.closeout import closeout
from taf.agents.reporter import report_synthesizer
from taf.agents.state import QAState
from taf.agents.triage import intake_triager

# Node functions by stage name - the dashboard and CLI run stages one at a time.
NODES = {
    "triage": intake_triager,
    "architect": test_architect,
    "automation": automation_engineer,
    "report": report_synthesizer,
    "closeout": closeout,
}


def _continue_or_report(next_node: str) -> Callable[[QAState], str]:
    """Route to `next_node` unless the run aborted, in which case jump to the report."""

    def router(state: QAState) -> str:
        return "report_synthesizer" if state.get("aborted") else next_node

    return router


def build_graph():
    graph = StateGraph(QAState)
    graph.add_node("intake_triager", intake_triager)
    graph.add_node("test_architect", test_architect)
    graph.add_node("automation_engineer", automation_engineer)
    graph.add_node("report_synthesizer", report_synthesizer)
    graph.add_node("closeout", closeout)

    graph.add_edge(START, "intake_triager")
    graph.add_conditional_edges(
        "intake_triager", _continue_or_report("test_architect"),
        {"test_architect": "test_architect", "report_synthesizer": "report_synthesizer"},
    )
    graph.add_conditional_edges(
        "test_architect", _continue_or_report("automation_engineer"),
        {"automation_engineer": "automation_engineer", "report_synthesizer": "report_synthesizer"},
    )
    graph.add_edge("automation_engineer", "report_synthesizer")
    graph.add_edge("report_synthesizer", "closeout")
    graph.add_edge("closeout", END)
    return graph.compile()


@functools.lru_cache(maxsize=1)
def qa_graph():
    """Compiled once, on first use."""
    return build_graph()
