"""Structured outputs the agents must return. The router validates every answer against these."""
from __future__ import annotations

from typing import List, Literal

from pydantic import BaseModel, Field


class TriageAnalysis(BaseModel):
    """Output of Agent 1 - the Intake Triager."""

    feature_summary: str = Field(description="Plain-language summary of what changed and why.")
    acceptance_criteria: List[str] = Field(description="Explicit, testable acceptance criteria (derived if missing).")
    impacted_areas: List[str] = Field(description="Pages, components, APIs or data flows touched by the diff.")
    risk_level: Literal["low", "medium", "high", "critical"]
    risks: List[str] = Field(description="Concrete regression / defect risks worth testing.")
    environment_findings: List[str] = Field(
        default_factory=list, description="Relevant signals from Azure / DB logs (errors, slow queries, ...)."
    )
    gaps_and_questions: List[str] = Field(
        default_factory=list, description="Ambiguities in the ticket that QA should flag to the team."
    )


class TestStep(BaseModel):
    action: str = Field(description="What the tester / automation does.")
    data: str = Field(default="", description="Input data for the step, if any.")
    expected_result: str = Field(description="Observable expected outcome.")


class TestCase(BaseModel):
    test_id: str = Field(description="Stable ID such as TC-001.")
    summary: str = Field(description="One-line test title.")
    kind: Literal["functional", "non-functional"]
    category: str = Field(description="e.g. happy-path, negative, edge-case, regression, performance, accessibility, security.")
    priority: Literal["Highest", "High", "Medium", "Low", "Lowest"]
    automatable: bool = Field(description="True if this can be automated with Playwright against the target URL.")
    preconditions: str = Field(default="")
    acceptance_criteria_refs: List[str] = Field(default_factory=list, description="Which acceptance criteria this covers.")
    steps: List[TestStep] = Field(min_length=1)


class TestPlan(BaseModel):
    """Output of Agent 2 - the Test Architect."""

    strategy: str = Field(description="Short overall test strategy.")
    in_scope: List[str]
    out_of_scope: List[str] = Field(default_factory=list)
    test_cases: List[TestCase] = Field(min_length=1)
