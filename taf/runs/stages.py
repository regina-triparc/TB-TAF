"""
Stage runner shared by the dashboard and the CLI: run the LangGraph workflow for
one ticket - all of it or a prefix - recording every phase in the run store so the
board shows it.

A run can pause three times, each time waiting on a person: at the clarify gate
after triage (run state `needs_clarification`, only with CLARIFY_GATE on), at the plan
gate after the architect (`needs_plan_review`, only with PLAN_GATE on) and at the
approval gate before publishing (`waiting`).

Stages are cumulative, because each needs what the one before it produced:
    triage      -> triage
    architect   -> triage, architect
    automation  -> triage, architect, automation (generate, validate, run, review)
    full        -> ... automation, approval, publish, report

The graph is checkpointed per run (thread id = run id), so:
  * a run paused at the approval gate is continued with `resume(run_id, decision)`,
    one paused at the clarify gate with `resume(run_id, {"action": "answer", "answers": {...}})`,
  * a run that crashed or was cancelled is continued with `resume(run_id)`, from the
    step after the last one that completed - finished side effects are not redone.

The n8n closeout is not a stage: runs started here have no callback URL. The full
n8n flow goes through POST /qa/runs (taf/runs/registry.py).
"""
from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from langgraph.types import Command

from taf.agents.graph import NODE_PHASE, qa_graph, thread_config
from taf.config import SETTINGS
from taf.integrations.jira.board import get_board
from taf.llm.budget import BUDGET
from taf.llm.router import ROUTER
from taf.observability.tracing import TRACER
from taf.runs.store import get_store
from taf.steps import clarify, coverage
from taf.runs.usage import USAGE
from taf.steps.context import gather_diff  # noqa: F401  (re-exported: used by callers and tests)

STAGE_NODES: Dict[str, List[str]] = {
    "triage": ["triage"],
    "architect": ["triage", "architect"],
    "automation": ["triage", "architect", "automation"],
    "full": ["triage", "architect", "automation", "approval", "publish", "report"],
}
# The graph's `stop_after` for each stage.
STOP_AFTER = {"triage": "triage", "architect": "architect", "automation": "automation", "full": "report"}

# What a paused run's state is called, by what it waits for.
PAUSED_STATES = {"approval": "waiting", "clarification": "needs_clarification", "plan": "needs_plan_review"}

# run_id -> Event. Set means "stop before the next node".
_CANCELS: Dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()

OnPhase = Callable[[str, str, str], None]


class StageError(ValueError):
    """The request cannot start (unknown stage, unknown ticket, ticket already running)."""

    def __init__(self, message: str, status_code: int = 422) -> None:
        super().__init__(message)
        self.status_code = status_code


def build_state(run_id: str, ticket_id: str, summary: str = "", description: str = "",
                target_url: str = "", provider: str = "", source: str = "") -> Dict[str, Any]:
    """
    Initial pipeline state, filled from the ticket source (Jira or the seed file)
    where not given explicitly. The PR diff is NOT fetched here - that takes
    seconds to minutes and is the graph's first step, on the run's own thread.
    """
    board = get_board()
    try:
        resolved = board.resolve_source(source)
    except ValueError as exc:
        raise StageError(str(exc)) from exc
    if resolved == "jira" and not board.config.configured:
        raise StageError(f"TICKET_SOURCE is jira but {board.config.missing} is not set in .env")
    issue = board.issue(ticket_id, resolved)
    prs = []
    if issue:
        summary = summary or issue.get("summary", "")
        description = description or issue.get("description", "")
        prs = issue.get("pull_requests") or []
    issue = issue or {}
    if not summary:
        where = "Jira" if resolved == "jira" else f"the seed file ({SETTINGS.seed_tickets_path})"
        raise StageError(f"No summary for {ticket_id}: not found in {where}. "
                         f"Pass --summary/--description, or use --source {'seed' if resolved == 'jira' else 'jira'}.")
    state: Dict[str, Any] = {
        "run_id": run_id,
        "ticket_id": ticket_id,
        "summary": summary,
        "description": description,
        "target_url": target_url or SETTINGS.dashboard_target_url,
        "github_diff": "",
        "environment_logs": "",
        "errors": [],
    }
    # Frozen for the whole run: switching the default mid-run affects the next run only.
    state["llm_provider"] = provider or ROUTER.default_provider
    state["ticket_source"] = resolved
    state["pull_requests"] = [{"number": p.number, "repo": p.repo, "status": p.status, "url": p.url,
                               **({"title": p.title} if getattr(p, "title", "") else {})} for p in prs]
    # Classification signals (taf/steps/classify.py).
    state["issue_type"] = str(issue.get("type") or "")
    state["labels"] = list(issue.get("labels") or [])
    state["linked_issues"] = list(issue.get("linked_issues") or [])
    return state


def prepare(ticket_id: str, stage: str, **overrides: str) -> Dict[str, Any]:
    """Validate, create the run record and return the initial state. Raises StageError."""
    if stage not in STAGE_NODES:
        raise StageError(f"Unknown stage {stage!r}; use one of {', '.join(STAGE_NODES)}")
    store = get_store()
    if store.active_run_for(ticket_id):
        raise StageError(f"{ticket_id} already has a run in progress or waiting for approval", status_code=409)
    run_id = uuid.uuid4().hex
    state = build_state(run_id, ticket_id, **overrides)
    state["stop_after"] = STOP_AFTER[stage]
    store.start_run(run_id, ticket_id, state["summary"], stage)
    _register_cancel(run_id)
    return state


def _register_cancel(run_id: str) -> threading.Event:
    with _CANCEL_LOCK:
        return _CANCELS.setdefault(run_id, threading.Event())


# ------------------------------------------------------------- recording --
class PhaseRecorder:
    """
    Turns the graph's per-node task events into the store's per-phase rows: several
    graph nodes (generate, validate, run, review, repairs) make one 'automation' phase.
    """

    def __init__(self, run_id: str, ticket_id: str, notify: OnPhase, values: Callable[[], Dict[str, Any]]) -> None:
        self.store = get_store()
        self.run_id, self.ticket_id = run_id, ticket_id
        self.notify, self.values = notify, values
        self.phase: Optional[str] = None
        self.row: Optional[int] = None
        self.started = time.monotonic()
        self.errors: List[str] = []
        open_row = self.store.open_phase(run_id)  # resuming a run paused at approval
        if open_row:
            self.row, self.phase = open_row

    def node_started(self, node: str) -> None:
        phase = NODE_PHASE.get(node, node)
        if phase == self.phase:
            return
        self.close()
        self.phase, self.errors, self.started = phase, [], time.monotonic()
        USAGE.set_stage(self.run_id, phase)
        if phase != "context":  # context is shown live, not stored - as before
            self.row = self.store.phase_started(self.run_id, self.ticket_id, phase)
        self.notify(phase, "running", "")

    def node_finished(self, node: str, result: Any, error: Any) -> None:
        if error:
            self.errors.append(f"{node}: {error}")
        if isinstance(result, dict) and result.get("errors"):
            self.errors += [str(e) for e in result["errors"]]

    def close(self, outcome: str = "ok") -> None:
        if self.phase is None:
            return
        state = self.values()
        took = time.monotonic() - self.started
        elapsed = int(took)
        failed = bool(self.errors) or outcome == "failed"
        final = {"waiting": "waiting", "cancelled": "cancelled"}.get(outcome, "failed" if failed else "ok")
        USAGE.stage_finished(self.run_id, self.phase, took, final)
        USAGE.set_stage(self.run_id, None)
        if self.phase == "context":
            self.notify("context", "ok", "; ".join(state.get("context_notes") or []))
        else:
            detail = "; ".join(self.errors)[:500] if failed and self.errors else phase_detail(self.phase, state, elapsed)
            if self.row is not None:
                if final == "waiting":
                    self.store.phase_update(self.row, "waiting", detail, phase_artifact(self.phase, state))
                else:
                    self.store.phase_finished(self.row, final, detail, phase_artifact(self.phase, state))
            self.notify(self.phase, final, detail)
        self.phase, self.row = None, None


# --------------------------------------------------------------- running --
def execute(state: Dict[str, Any], stage: str, on_phase: Optional[OnPhase] = None) -> Dict[str, Any]:
    """
    Run the stage on the calling thread through the checkpointed graph.
    `on_phase(name, state, detail)` is called on every transition (the CLI prints it).
    Returns the pipeline state with `_run_state` set: completed | failed | cancelled | waiting.
    """
    state = dict(state)
    state.setdefault("stop_after", STOP_AFTER[stage])
    return _drive(state["run_id"], state["ticket_id"], state, on_phase, resumed=False)


def pending_interrupt(run_id: str) -> Optional[Dict[str, Any]]:
    """What a paused run is waiting on (its `kind`: approval | clarification), or None."""
    snapshot = qa_graph().get_state(thread_config(run_id))
    for task in snapshot.tasks:
        for item in task.interrupts:
            value = dict(item.value) if isinstance(item.value, dict) else {"request": item.value}
            value.setdefault("kind", "approval")
            return value
    return None


def pending_approval(run_id: str) -> Optional[Dict[str, Any]]:
    """The approval request a paused run is waiting on, or None."""
    request = pending_interrupt(run_id)
    return request if request and request["kind"] == "approval" else None


def pending_clarification(run_id: str) -> Optional[Dict[str, Any]]:
    """The questions a run paused at the clarify gate is waiting on, or None."""
    request = pending_interrupt(run_id)
    return request if request and request["kind"] == "clarification" else None


def _check_decision(run_id: str, decision: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """A decision exactly when the run waits for one, of the kind it waits for. Raises StageError."""
    request = pending_interrupt(run_id)
    if request is None:
        if decision is not None:
            raise StageError(f"Run {run_id} is not waiting for a decision.", status_code=409)
        return None
    if request["kind"] == "clarification":
        if decision is None:
            raise StageError(f"Run {run_id} is waiting for answers to its questions: answer, proceed or reject.",
                             status_code=409)
        try:
            return clarify.validate_decision(decision)
        except ValueError as exc:
            raise StageError(f"Run {run_id}: {exc}") from exc
    if request["kind"] == "plan":
        if decision is None:
            raise StageError(f"Run {run_id} is waiting for its plan to be reviewed: continue or reject.",
                             status_code=409)
        try:
            return coverage.validate_plan_decision(decision)
        except ValueError as exc:
            raise StageError(f"Run {run_id}: {exc}") from exc
    if decision is None:
        raise StageError(f"Run {run_id} is waiting for approval: approve or reject it.", status_code=409)
    if "approved" not in decision:
        raise StageError(f"Run {run_id} is waiting for approval, not for answers: approve or reject it.",
                         status_code=409)
    return decision


def resume(run_id: str, decision: Optional[Dict[str, Any]] = None,
           on_phase: Optional[OnPhase] = None) -> Dict[str, Any]:
    """
    Continue a run from its last checkpoint. A run paused at the approval gate needs
    `decision` ({"approved": bool, "by": str, "note": str}); a crashed or cancelled one
    must not get one.
    """
    snapshot = qa_graph().get_state(thread_config(run_id))
    if not snapshot.values:
        raise StageError(f"No checkpoint for run {run_id} (QA_CHECKPOINT_DB_PATH={SETTINGS.qa_checkpoint_db_path}).",
                         status_code=404)
    if not snapshot.next:
        raise StageError(f"Run {run_id} has already finished.", status_code=409)
    decision = _check_decision(run_id, decision)
    waiting = decision is not None
    ticket_id = snapshot.values["ticket_id"]
    store = get_store()
    if store.run(run_id) is None:  # e.g. an n8n run, which the store does not track
        store.start_run(run_id, ticket_id, snapshot.values.get("summary", ""), "full")
    store.set_run_state(run_id, "running")
    _register_cancel(run_id)
    return _drive(run_id, ticket_id, Command(resume=decision) if waiting else None, on_phase, resumed=True)


def _drive(run_id: str, ticket_id: str, graph_input: Any, on_phase: Optional[OnPhase],
           resumed: bool) -> Dict[str, Any]:
    graph, config = qa_graph(), thread_config(run_id)
    store = get_store()
    cancel = _register_cancel(run_id)
    notify = on_phase or (lambda *_: None)
    # The run's state as the stream reports it. Reading the checkpoint mid-run can lag
    # (checkpoints are written asynchronously), so phase details use this instead.
    current: Dict[str, Any] = dict(graph_input) if isinstance(graph_input, dict) else         dict(graph.get_state(config).values)
    provider = current.get("llm_provider") or ""
    USAGE.begin(run_id, ticket_id, provider, ROUTER.describe(provider)["routes"])
    recorder = PhaseRecorder(run_id, ticket_id, notify, lambda: current)
    trace_state = current
    interrupted: Any = None
    crashed: Optional[BaseException] = None
    cancelled = False
    try:
        with TRACER.run(trace_state, resumed=resumed) as root:
            try:
                for mode, event in graph.stream(graph_input, config, stream_mode=["tasks", "updates"]):
                    if mode == "tasks" and "input" in event:  # a node is about to start
                        if cancel.is_set():
                            cancelled = True
                            break
                        recorder.node_started(event["name"])
                    elif mode == "tasks":
                        recorder.node_finished(event["name"], event.get("result"), event.get("error"))
                    elif mode == "updates":
                        if "__interrupt__" in event:
                            interrupted = event["__interrupt__"]
                        for node, update in event.items():
                            if node.startswith("__") or not isinstance(update, dict):
                                continue
                            for key, value in update.items():
                                current[key] = (current.get(key) or []) + value if key == "errors" else value
            except Exception as exc:  # noqa: BLE001 - nodes catch their own; this is the last resort
                crashed = exc

            final = {**current, **dict(graph.get_state(config).values)}
            if interrupted is not None:
                request = pending_interrupt(run_id) or {"kind": "approval"}
                kind = request["kind"]
                paused = PAUSED_STATES.get(kind, "waiting")
                recorder.close("waiting")
                store.set_run_state(run_id, paused)
                final["_run_state"] = paused
                final[{"clarification": "_clarify_request", "plan": "_plan_request"}.get(kind, "_approval_request")] \
                    = request
                final["usage_report_path"] = USAGE.write_report(run_id, "waiting", release=False)
                root.update(output={"status": f"waiting for {kind}"})
                return final
            if cancelled:
                recorder.close("cancelled")
                store.finish_run(run_id, "cancelled", error="cancelled - resume with `python -m taf resume`")
                final["_run_state"] = "cancelled"
                final["usage_report_path"] = USAGE.write_report(run_id, "cancelled")
                return final
            if crashed is not None:
                recorder.errors.append(f"{type(crashed).__name__}: {crashed}")
                recorder.close("failed")
                store.finish_run(run_id, "failed", error=f"crashed: {type(crashed).__name__}: {crashed}"[:500])
                final["_run_state"] = "failed"
                final["usage_report_path"] = USAGE.write_report(run_id, "failed")
                return final

            recorder.close()
            errors = final.get("errors") or []
            run_state = "failed" if errors else "completed"
            outcome = "passed" if final.get("execution_passed") else "completed"
            store.finish_run(run_id, run_state, outcome=outcome if run_state == "completed" else "",
                             error="; ".join(str(e) for e in errors)[:500])
            final["_run_state"] = run_state
            final["usage_report_path"] = USAGE.write_report(run_id, run_state)
            _score(run_id, final)
            root.update(output={"run_state": run_state, "execution_passed": bool(final.get("execution_passed")),
                                "pr_url": (final.get("publish") or {}).get("pr_url"), "errors": len(errors)},
                        level="ERROR" if errors else "DEFAULT")
            BUDGET.release(run_id)
            return final
    finally:
        with _CANCEL_LOCK:
            _CANCELS.pop(run_id, None)
        TRACER.flush()


def _score(run_id: str, state: Dict[str, Any]) -> None:
    """Workflow outcomes as Langfuse scores on the run's trace (no-op when tracing is off)."""
    validation = state.get("validation") or {}
    if validation.get("checks") or validation.get("ok") is False:
        TRACER.score(run_id, "static_validation", 1 if validation.get("ok") else 0, "BOOLEAN")
        TRACER.score(run_id, "spec_attempts", int(state.get("spec_attempts") or 0))
    if state.get("playwright"):
        TRACER.score(run_id, "playwright_passed", 1 if state.get("execution_passed") else 0, "BOOLEAN")
    review = state.get("review") or {}
    if "findings" in review:
        TRACER.score(run_id, "review_findings", len(review["findings"]))
    approval = (state.get("approval") or {}).get("status")
    if approval:
        TRACER.score(run_id, "approval", approval, "CATEGORICAL")
    pr_url = (state.get("publish") or {}).get("pr_url")
    if pr_url:
        TRACER.score(run_id, "pull_request_opened", 1, "BOOLEAN", comment=pr_url)


def start_in_background(state: Dict[str, Any], stage: str) -> None:
    threading.Thread(target=execute, args=(state, stage),
                     name=f"qa-{state['ticket_id']}", daemon=True).start()


def resume_in_background(run_id: str, decision: Optional[Dict[str, Any]]) -> None:
    """Validate synchronously (raises StageError), then continue on a worker thread."""
    snapshot = qa_graph().get_state(thread_config(run_id))
    if not snapshot.values or not snapshot.next:
        raise StageError(f"Run {run_id} cannot be resumed (no checkpoint, or already finished).", status_code=409)
    decision = _check_decision(run_id, decision)
    threading.Thread(target=resume, args=(run_id, decision), name=f"qa-resume-{run_id[:8]}", daemon=True).start()


def cancel(run_id: str) -> bool:
    """Ask a running stage to stop before its next node. False if no such active run."""
    with _CANCEL_LOCK:
        event = _CANCELS.get(run_id)
    if event is None:
        return False
    event.set()
    return True


def phase_artifact(name: str, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    What this agent actually produced, for the board's expandable summary. Only the
    fields a reader needs - the full state carries prompts and raw logs that are too
    large to re-serialise on every poll.
    """
    if name == "triage":
        t = state.get("triage_analysis") or {}
        return {
            "risk_level": t.get("risk_level"),
            "feature_summary": t.get("feature_summary"),
            "acceptance_criteria": t.get("acceptance_criteria") or [],
            "risks": t.get("risks") or [],
            "impacted_areas": t.get("impacted_areas") or [],
            "gaps_and_questions": t.get("gaps_and_questions") or [],
            "clarify": clarify_artifact(state),
            "classification": state.get("classification"),
            "skills": (state.get("skills_used") or {}).get("triage"),
            "ticket_source": state.get("ticket_source"),
            "pull_requests": state.get("pull_requests") or [],
            "context_notes": state.get("context_notes") or [],
        }
    if name == "architect":
        cases = state.get("test_cases") or []
        return {
            "total": len(cases),
            "automatable": sum(1 for c in cases if c.get("automatable")),
            "skills": (state.get("skills_used") or {}).get("architect"),
            "classification": state.get("classification"),
            "coverage": state.get("coverage"),
            "candidates": state.get("coverage_candidates") or [],
            "plan_confidence": state.get("plan_confidence"),
            "plan_review": plan_review_artifact(state),
            "cases": [
                {
                    "test_id": c.get("test_id"),
                    "summary": c.get("summary"),
                    "priority": c.get("priority"),
                    "kind": c.get("kind"),
                    "category": c.get("category"),
                    "automatable": bool(c.get("automatable")),
                    "disposition": c.get("disposition") or "new",
                    "target_spec": c.get("target_spec") or "",
                    "target_test": c.get("target_test") or "",
                    "steps": [
                        {"action": s_.get("action"), "data": s_.get("data"),
                         "expected_result": s_.get("expected_result")}
                        for s_ in (c.get("steps") or [])
                    ],
                }
                for c in cases
            ],
        }
    if name == "automation":
        logs = state.get("execution_logs") or ""
        validation = state.get("validation") or {}
        return {
            "git_branch": state.get("git_branch"),
            "git_pushed": bool(state.get("git_pushed")),
            "spec_file_path": state.get("spec_file_path"),
            "xray_files": state.get("xray_files") or [],
            "execution_passed": bool(state.get("execution_passed")),
            "spec_attempts": state.get("spec_attempts"),
            "skills": (state.get("skills_used") or {}).get("automation"),
            "validation": {"ok": validation.get("ok"), "skipped": validation.get("skipped"),
                           "checks": [{"name": c.get("name"), "ok": c.get("ok"), "skipped": c.get("skipped"),
                                       "detail": (c.get("detail") or "")[:800]}
                                      for c in validation.get("checks") or []]},
            "review": state.get("review") or {},
            "code": (state.get("generated_typescript_code") or "")[:20000],
            # The tail is where Playwright prints its pass/fail summary.
            "log_tail": logs[-6000:],
        }
    if name == "approval":
        from taf.steps.publish import approval_request

        return {"request": approval_request(state), "decision": state.get("approval") or {}}
    if name == "publish":
        return {"publish": state.get("publish") or {}}
    if name == "report":
        return {"report_markdown": (state.get("final_report") or "")[:20000], "trace_url": state.get("trace_url"),
                "report_html_path": state.get("report_html_path") or ""}
    return None


def clarify_artifact(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The clarify gate's record; while the run is paused there, the questions it asks."""
    if not state.get("triage_analysis"):
        return None
    record = dict(state.get("clarify") or {})
    if not record:
        request = clarify.clarify_request(state)
        paused = request["reasons"] and SETTINGS.clarify_gate and state.get("stop_after") != "triage"
        record = {"status": "waiting" if paused else "not checked", "questions": request["questions"],
                  "confidence": request["confidence"], "threshold": request["threshold"],
                  "reasons": request["reasons"]}
    record["answers"] = state.get("clarifications") or []
    return record


def plan_review_artifact(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The plan gate's record; while the run is paused there, why it asks."""
    if not state.get("test_cases"):
        return None
    record = dict(state.get("plan_review") or {})
    if not record:
        reasons = coverage.plan_reasons(state)
        paused = reasons and SETTINGS.plan_gate and state.get("stop_after") != "architect"
        record = {"status": "waiting" if paused else "not checked", "reasons": reasons}
    return record


def phase_detail(name: str, state: Dict[str, Any], elapsed_s: int) -> str:
    """One short human-readable line per finished phase, shown on the board."""
    if name == "triage":
        t = state.get("triage_analysis") or {}
        prs = ", ".join(f"#{p['number']}" for p in state.get("pull_requests") or []) or "no PRs"
        diff = "diff" if state.get("github_diff") else "no diff"
        gate = state.get("clarify") or {}
        asked = (f", {len(gate.get('questions') or [])} question(s): {gate['status']}"
                 if gate.get("status") in {"answered", "proceeded", "rejected"} else "")
        if not gate and state.get("triage_analysis") and SETTINGS.clarify_gate and state.get("stop_after") != "triage":
            request = clarify.clarify_request(state)
            asked = f", waiting for answers to {len(request['questions'])} question(s)" if request["reasons"] else ""
        return (f"{elapsed_s}s - risk {t.get('risk_level', '?')}, "
                f"{len(t.get('acceptance_criteria') or [])} criteria ({state.get('ticket_source', '?')}; {prs}; {diff})"
                + asked)
    if name == "architect":
        cases = state.get("test_cases") or []
        auto = sum(1 for c in cases if c.get("automatable"))
        reuse = sum(1 for c in cases if c.get("disposition") in {"update", "reuse"})
        gate = (state.get("plan_review") or {}).get("status")
        cls = state.get("classification") or {}
        return (f"{elapsed_s}s - {len(cases)} cases, {auto} automatable"
                + (f", {reuse} on existing tests" if reuse else "")
                + (f" ({cls.get('action')}/{cls.get('layer')})" if cls else "")
                + (f", plan {gate}" if gate in {"continued", "rejected"} else
                   ", waiting for plan review" if not gate and SETTINGS.plan_gate and coverage.plan_reasons(state)
                   and state.get("stop_after") != "architect" else ""))
    if name == "automation":
        validation = state.get("validation") or {}
        checked = ("validation skipped" if validation.get("skipped") else
                   "validation ok" if validation.get("ok") else "validation FAILED")
        return (f"{elapsed_s}s - {state.get('spec_attempts') or 0} attempt(s), {checked}, "
                f"{'tests passed' if state.get('execution_passed') else 'tests did not pass'}")
    if name == "approval":
        approval = state.get("approval") or {}
        return approval.get("status", "waiting for approval") + (f" - {approval['note']}" if approval.get("note") else "")
    if name == "publish":
        record = state.get("publish") or {}
        if record.get("skipped"):
            return f"skipped - {record['skipped']}"
        return (f"branch {record.get('branch') or '-'}, pushed={bool(record.get('pushed'))}"
                + (f", PR {record['pr_url']}" if record.get("pr_url") else ""))
    return f"{elapsed_s}s"
