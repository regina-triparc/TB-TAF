"""
The operational envelope every pipeline node runs in.

`@agent_node(...)` makes a node:
  * skip its work if an upstream node already aborted the run (cloud nodes only),
  * emit agent.started / agent.completed / agent.failed to the Control Room,
  * turn BudgetExceededError and unexpected exceptions into state errors instead
    of crashing the graph - a run always reaches the report.
"""
from __future__ import annotations

import functools
import logging
import time
from typing import Any, Callable, Dict, Optional

from taf.agents.state import QAState
from taf.config import redact
from taf.integrations.paperclip.control_room import CONTROL_ROOM
from taf.llm.budget import BUDGET, BudgetExceededError
from taf.llm.router import ROUTER

log = logging.getLogger("qa")

Node = Callable[[QAState], Dict[str, Any]]


def agent_node(name: str, engine: str, abort_on_error: bool, route_key: Optional[str] = None) -> Callable[[Node], Node]:
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
            try:
                update = fn(state) or {}
                CONTROL_ROOM.emit(
                    run_id, ticket_id, "agent.completed", agent=name, engine=label,
                    duration_s=round(time.monotonic() - started, 1), cloud_tokens_run_total=BUDGET.run_usage(run_id),
                )
                return update
            except BudgetExceededError as exc:
                message = f"{name}: BUDGET GUARD - {exc}"
                log.error("[%s] %s", ticket_id, message)
                CONTROL_ROOM.emit(run_id, ticket_id, "agent.failed", agent=name, reason="budget_exceeded", error=str(exc))
                return {"errors": [message], "aborted": True, "execution_passed": False}
            except Exception as exc:  # noqa: BLE001 - a node must never take down the graph
                message = f"{name}: {type(exc).__name__}: {redact(str(exc))}"
                log.exception("[%s] %s", ticket_id, message)
                CONTROL_ROOM.emit(run_id, ticket_id, "agent.failed", agent=name, error=message)
                failure: Dict[str, Any] = {"errors": [message]}
                if abort_on_error:
                    failure.update({"aborted": True, "execution_passed": False})
                return failure

        return wrapper

    return decorator
