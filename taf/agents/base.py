"""
The operational envelope every pipeline node runs in.

`@agent_node(...)` makes a node:
  * skip its work if an upstream node already aborted the run (cloud nodes only),
  * emit agent.started / agent.completed / agent.failed to the Control Room,
  * record a Langfuse span for the step (a no-op when tracing is off),
  * turn BudgetExceededError and unexpected exceptions into state errors instead
    of crashing the graph - a run always reaches the report.
LangGraph's own control-flow exceptions (an approval `interrupt()`) pass through.
"""
from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable, Dict, Optional

from langgraph.errors import GraphBubbleUp

from taf.agents.state import QAState
from taf.config import redact
from taf.integrations.paperclip.control_room import CONTROL_ROOM
from taf.llm.budget import BUDGET, BudgetExceededError
from taf.llm.router import ROUTER
from taf.observability.tracing import TRACER

log = logging.getLogger("qa")

Node = Callable[[QAState], Dict[str, Any]]


def span_summary(update: Dict[str, Any]) -> Dict[str, Any]:
    """What a step produced, as sizes and flags - never the full text of prompts, specs or logs."""
    out: Dict[str, Any] = {}
    for key, value in update.items():
        if key == "errors":
            out["errors"] = [redact(str(e))[:300] for e in value]
        elif isinstance(value, (bool, int, float)) or value is None:
            out[key] = value
        elif isinstance(value, str):
            out[key] = value if len(value) <= 120 else f"[{len(value):,} chars]"
        elif isinstance(value, list):
            out[key] = f"[{len(value)} items]"
        elif isinstance(value, dict):
            small = {k: v for k, v in value.items() if isinstance(v, (bool, int, float, str)) and len(str(v)) <= 200}
            out[key] = small or f"[{len(value)} keys]"
    return out


def agent_node(name: str, engine: str, abort_on_error: bool, route_key: Optional[str] = None,
               span_type: str = "agent") -> Callable[[Node], Node]:
    def decorator(fn: Node) -> Node:
        @functools.wraps(fn)
        def wrapper(state: QAState) -> Dict[str, Any]:
            run_id, ticket_id = state["run_id"], state["ticket_id"]
            # Label the engine from the agent's actual provider for THIS run, so a
            # hybrid or switched run is honest about which agent ran where.
            label = engine
            if route_key:
                label = engine.replace("llm", ROUTER.route(route_key, state.get("llm_provider")).provider)
            if abort_on_error and state.get("aborted"):
                CONTROL_ROOM.emit(run_id, ticket_id, "agent.skipped", agent=name, reason="run aborted upstream")
                return {}
            CONTROL_ROOM.emit(run_id, ticket_id, "agent.started", agent=name, engine=label)
            started = time.monotonic()
            with TRACER.node(state, fn.__name__, as_type=span_type,
                             metadata={"agent": name, "engine": label, "ticket_id": ticket_id}) as span:
                try:
                    update = fn(state) or {}
                    CONTROL_ROOM.emit(
                        run_id, ticket_id, "agent.completed", agent=name, engine=label,
                        duration_s=round(time.monotonic() - started, 1), cloud_tokens_run_total=BUDGET.run_usage(run_id),
                    )
                    span.update(output=span_summary(update),
                                level="WARNING" if update.get("errors") else "DEFAULT")
                    return update
                except GraphBubbleUp:
                    span.update(output={"status": "paused (waiting for input)"})
                    raise  # LangGraph interrupt / control flow - not an error
                except BudgetExceededError as exc:
                    message = f"{name}: BUDGET GUARD - {exc}"
                    log.error("[%s] %s", ticket_id, message)
                    CONTROL_ROOM.emit(run_id, ticket_id, "agent.failed", agent=name, reason="budget_exceeded", error=str(exc))
                    span.update(level="ERROR", status_message=message[:500])
                    return {"errors": [message], "aborted": True, "execution_passed": False}
                except Exception as exc:  # noqa: BLE001 - a node must never take down the graph
                    message = f"{name}: {type(exc).__name__}: {redact(str(exc))}"
                    log.exception("[%s] %s", ticket_id, message)
                    CONTROL_ROOM.emit(run_id, ticket_id, "agent.failed", agent=name, error=message)
                    span.update(level="ERROR", status_message=message[:500])
                    failure: Dict[str, Any] = {"errors": [message]}
                    if abort_on_error:
                        failure.update({"aborted": True, "execution_passed": False})
                    return failure

        return wrapper

    return decorator
