"""The LangGraph state shared by every node of one QA run (checkpointed after every node)."""
from __future__ import annotations

import operator
from typing import Annotated, Any, Dict, List, TypedDict


class QAState(TypedDict, total=False):
    # --- Required contract (from the brief) --------------------------------
    ticket_id: str
    summary: str
    description: str
    github_diff: str
    target_url: str
    test_plan: str                      # Markdown rendering of the plan
    generated_typescript_code: str
    execution_logs: str
    execution_passed: bool
    xray_csv_content: str
    xray_tests_json: List[Dict[str, Any]]  # Xray Cloud bulk-import payload
    xray_files: List[str]                  # where it was written in the test repo (XRAY_DIR/<TICKET>/...)
    final_report: str
    report_html_path: str               # the HTML report (manual checklist + automation results)

    # --- Operational fields -------------------------------------------------
    run_id: str
    n8n_callback_url: str
    environment_logs: str               # Azure / DB logs gathered by n8n
    ticket_source: str                  # jira | seed
    pull_requests: List[Dict[str, Any]]  # the ticket's linked application PRs
    context_notes: List[str]
    triage_analysis: Dict[str, Any]
    test_cases: List[Dict[str, Any]]
    spec_file_path: str
    git_branch: str
    git_pushed: bool
    callback_delivered: bool
    aborted: bool                       # True → skip remaining cloud nodes
    errors: Annotated[List[str], operator.add]  # reducer: nodes append, never overwrite

    # --- Workflow (taf/agents/graph.py) ---------------------------------------
    stop_after: str                     # triage | architect | automation | report; "" = through closeout
    repo_context: Dict[str, Any]        # the test repo's conventions: config, fixtures, page objects
    live_snapshot: Dict[str, Any]       # live accessibility tree of the app: {"available", "captured", "prompt"}
    playwright: Dict[str, Any]          # last run: {"passed", "counts", "tests": [{"title", "tc", "status", "error"}]}
    run_repair: Dict[str, Any]          # repairs from the run: {"attempts": [...], "accepted", "before", "after"}
    spec_attempts: int                  # generations so far (1 + repairs)
    spec_feedback: str                  # why the last spec was rejected - the next repair's input
    validation: Dict[str, Any]          # deterministic checks: {"ok", "retryable", "checks": [...]}
    review: Dict[str, Any]              # {"findings": [...], "blocking": bool, "llm": {...}}
    approval: Dict[str, Any]            # {"status": approved|rejected|not_required|skipped, "by", "note"}
    clarify: Dict[str, Any]             # clarify gate: {"status": passed|not_required|answered|proceeded|rejected,
                                        #  "questions": [...], "confidence", "risks", "by", "note"}
    clarifications: List[Dict[str, Any]]  # answers from the team: [{"id", "question", "answer"}]
    issue_type: str                     # Jira issue type (Story, Bug, ...)
    labels: List[str]                   # Jira labels
    linked_issues: List[Dict[str, Any]]  # Jira issue links: [{"key", "type", "relation"}]
    classification_signals: Dict[str, Any]  # taf/steps/classify.py, before triage
    classification: Dict[str, Any]      # {"action", "layer", "change_type", "rationale", "sources", ...}
    action: str                         # create | update | extend | regression_only | skip
    layer: str                          # api | ui | both | none
    coverage: Dict[str, Any]            # the test repo's coverage index: {"available", "sha", "specs", "cached"}
    coverage_candidates: List[Dict[str, Any]]  # existing tests that may cover the ticket, ranked
    plan_confidence: float              # the Test Architect's confidence in its plan (0-1)
    plan_review: Dict[str, Any]         # plan gate: {"status", "reasons", "by", "note", "overrides"}
    regression_run: Dict[str, Any]      # regression_only: the existing specs that were run, and results
    skills_used: Dict[str, Any]         # per agent: {"loaded": [...], "skipped": [{"name", "reason"}], "references"}
    publish: Dict[str, Any]             # {"branch", "commit_sha", "pushed", "pr_url", "pr_number", ...}
    trace_url: str                      # Langfuse trace, when tracing is on

    # --- Routing ------------------------------------------------------------
    llm_provider: str                   # per-run router override: claude | ollama | auto | offline
