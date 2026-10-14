"""The LangGraph state shared by every node of one QA run."""
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
    triage_analysis: Dict[str, Any]
    test_cases: List[Dict[str, Any]]
    spec_file_path: str
    git_branch: str
    git_pushed: bool
    callback_delivered: bool
    aborted: bool                       # True → skip remaining cloud nodes
    errors: Annotated[List[str], operator.add]  # reducer: nodes append, never overwrite


    # --- Routing ------------------------------------------------------------
    llm_provider: str                   # per-run router override: claude | ollama | auto | offline
