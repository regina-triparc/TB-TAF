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
    publish: Dict[str, Any]             # {"branch", "commit_sha", "pushed", "pr_url", "pr_number", ...}
    trace_url: str                      # Langfuse trace, when tracing is on

    # --- Routing ------------------------------------------------------------
    llm_provider: str                   # per-run router override: claude | ollama | auto | offline
