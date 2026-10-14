"""
Cloud spend guard. Every Claude call is checked before (caps, Paperclip pause) and
booked after (usage, cost) - this is what stops a runaway repair loop.
"""
from __future__ import annotations

import threading
from datetime import date
from typing import Dict

from taf.config import SETTINGS, Settings
from taf.integrations.paperclip.control_room import CONTROL_ROOM, ControlRoom
from taf.llm.pricing import TokenUsage, price_cents


class BudgetExceededError(RuntimeError):
    """Raised before a cloud LLM call when a token cap or the Paperclip budget is exhausted."""


class TokenBudget:
    """
    Thread-safe cloud spend accountant.

    * `ensure_available` runs BEFORE every Claude call. It refuses the call once the
      per-run or per-day token cap is reached, or once Paperclip has paused the agent
      or its monthly budget is used up - this is what stops runaway spending loops.
    * `record` runs AFTER every call with the provider-reported usage, prices it and
      reports it to Paperclip's cost ledger.
    A single call can overshoot a cap by at most `CLAUDE_MAX_TOKENS` output tokens
    plus its prompt, so size the caps with that headroom in mind.
    """

    def __init__(self, settings: Settings, control_room: ControlRoom) -> None:
        self._per_run_cap = settings.max_cloud_tokens_per_run
        self._per_day_cap = settings.max_cloud_tokens_per_day
        self._control_room = control_room
        self._lock = threading.Lock()
        self._run_usage: Dict[str, int] = {}
        self._cent_carry: Dict[str, float] = {}  # sub-cent remainders, so small calls aren't lost
        self._run_cost_cents: Dict[str, float] = {}
        self._run_cache_read: Dict[str, int] = {}
        self._day = date.today()
        self._day_usage = 0

    def _roll_day(self) -> None:
        today = date.today()
        if today != self._day:
            self._day, self._day_usage = today, 0

    def ensure_available(self, run_id: str) -> None:
        with self._lock:
            self._roll_day()
            used_run = self._run_usage.get(run_id, 0)
            if self._per_run_cap > 0 and used_run >= self._per_run_cap:
                raise BudgetExceededError(
                    f"Per-run cloud token cap reached ({used_run:,}/{self._per_run_cap:,} tokens)."
                )
            if self._per_day_cap > 0 and self._day_usage >= self._per_day_cap:
                raise BudgetExceededError(
                    f"Daily cloud token cap reached ({self._day_usage:,}/{self._per_day_cap:,} tokens)."
                )
        reason = self._control_room.budget_block_reason()  # outside the lock: it may do HTTP
        if reason:
            raise BudgetExceededError(reason)

    def record(self, run_id: str, ticket_id: str, agent: str, usage: TokenUsage, model: str,
               provider: str = "anthropic") -> None:
        exact_cents = price_cents(model, usage, local=(provider == "ollama"))
        with self._lock:
            self._roll_day()
            self._run_usage[run_id] = self._run_usage.get(run_id, 0) + usage.budget_tokens
            self._day_usage += usage.budget_tokens
            self._run_cost_cents[run_id] = self._run_cost_cents.get(run_id, 0.0) + exact_cents
            self._run_cache_read[run_id] = self._run_cache_read.get(run_id, 0) + usage.cache_read_tokens
            carried = self._cent_carry.get(run_id, 0.0) + exact_cents
            cost_cents = int(carried)
            self._cent_carry[run_id] = carried - cost_cents
            run_total, day_total = self._run_usage[run_id], self._day_usage
        self._control_room.emit(
            run_id, ticket_id, "llm.usage",
            agent=agent, provider=provider, model=model, billing_type="metered_api",
            # Paperclip's ledger: inputTokens = billed-at-full-or-write-rate, cached = cache reads.
            input_tokens=usage.input_tokens - usage.cache_read_tokens,
            cached_input_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_5m_tokens + usage.cache_write_1h_tokens,
            output_tokens=usage.output_tokens, cost_cents=cost_cents,
            run_total_tokens=run_total, run_cap=self._per_run_cap,
            day_total_tokens=day_total, day_cap=self._per_day_cap,
        )

    def run_cost_usd(self, run_id: str) -> float:
        with self._lock:
            return self._run_cost_cents.get(run_id, 0.0) / 100

    def run_cache_read_tokens(self, run_id: str) -> int:
        with self._lock:
            return self._run_cache_read.get(run_id, 0)

    def run_usage(self, run_id: str) -> int:
        with self._lock:
            return self._run_usage.get(run_id, 0)

    def release(self, run_id: str) -> int:
        """Forget a finished run's counters (the daily total is kept) and return its final usage."""
        with self._lock:
            self._cent_carry.pop(run_id, None)
            self._run_cost_cents.pop(run_id, None)
            self._run_cache_read.pop(run_id, None)
            return self._run_usage.pop(run_id, 0)


BUDGET = TokenBudget(SETTINGS, CONTROL_ROOM)
