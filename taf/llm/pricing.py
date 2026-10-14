"""Token usage extraction and per-model pricing (USD per million tokens)."""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict

from taf.config import SETTINGS


@dataclass(frozen=True)
class TokenUsage:
    """
    Provider-reported token usage for one LLM response, with the prompt-cache split.

    `input_tokens` is the TOTAL prompt size (LangChain adds cache reads and writes back
    in); the cache fields break it down so each part can be priced at its own rate.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0

    @property
    def uncached_input_tokens(self) -> int:
        return max(0, self.input_tokens - self.cache_read_tokens
                   - self.cache_write_5m_tokens - self.cache_write_1h_tokens)

    @property
    def budget_tokens(self) -> int:
        """Tokens counted against the local caps. Cache reads bill at 5-10% and are excluded."""
        return self.input_tokens - self.cache_read_tokens + self.output_tokens


def token_usage(message: Any) -> TokenUsage:
    """Extract LangChain's normalised `usage_metadata` from an AIMessage (zeros if absent)."""
    usage = getattr(message, "usage_metadata", None) or {}
    details = usage.get("input_token_details") or {}
    write_5m = int(details.get("ephemeral_5m_input_tokens") or 0)
    write_1h = int(details.get("ephemeral_1h_input_tokens") or 0)
    generic_write = int(details.get("cache_creation") or 0)  # older SDKs: no TTL split
    if generic_write and not (write_5m or write_1h):
        if SETTINGS.prompt_cache_ttl == "1h":
            write_1h = generic_write
        else:
            write_5m = generic_write
    return TokenUsage(
        input_tokens=int(usage.get("input_tokens") or 0),
        output_tokens=int(usage.get("output_tokens") or 0),
        cache_read_tokens=int(details.get("cache_read") or 0),
        cache_write_5m_tokens=write_5m,
        cache_write_1h_tokens=write_1h,
    )


# USD per million tokens, from https://platform.claude.com/docs/en/about-claude/pricing
# (checked 2026-09-30). Override or add models with CLAUDE_PRICING_JSON, e.g.
# {"claude-opus-5-5": {"input": 4, "output": 20, "cache_write_5m": 5, "cache_write_1h": 8, "cache_read": 0.2}}
MODEL_PRICING: Dict[str, Dict[str, float]] = {
    "claude-fable-5-1":  {"input": 10.0, "output": 50.0, "cache_write_5m": 12.50, "cache_write_1h": 20.0, "cache_read": 0.25},
    "claude-opus-5-5":   {"input": 4.0,  "output": 20.0, "cache_write_5m": 5.00,  "cache_write_1h": 8.0,  "cache_read": 0.20},
    "claude-sonnet-5-5": {"input": 2.0,  "output": 10.0, "cache_write_5m": 2.50,  "cache_write_1h": 4.0,  "cache_read": 0.20},
    "claude-haiku-4-5":  {"input": 1.0,  "output": 5.0,  "cache_write_5m": 1.25,  "cache_write_1h": 2.0,  "cache_read": 0.10},
}
if SETTINGS.claude_pricing_json:
    try:
        MODEL_PRICING.update(json.loads(SETTINGS.claude_pricing_json))
    except (ValueError, TypeError) as _exc:
        logging.getLogger("qa.config").warning("Ignoring invalid CLAUDE_PRICING_JSON: %s", _exc)


def price_cents(model: str, usage: TokenUsage, local: bool = False) -> float:
    """Exact (fractional) cost of one call in US cents, pricing each cache tier separately."""
    if local:
        return 0.0  # local models are free; don't price them like the dearest Claude model
    prices = MODEL_PRICING.get(model) or MODEL_PRICING.get(re.sub(r"-\d{8}$", "", model))
    if prices is None:
        # Unknown model: price it like the most expensive known model rather than as free.
        prices = max(MODEL_PRICING.values(), key=lambda p: p["output"])
        logging.getLogger("qa.budget").warning("No price for model %r - using the highest known rate.", model)
    usd = (
        usage.uncached_input_tokens * prices["input"]
        + usage.cache_write_5m_tokens * prices["cache_write_5m"]
        + usage.cache_write_1h_tokens * prices["cache_write_1h"]
        + usage.cache_read_tokens * prices["cache_read"]
        + usage.output_tokens * prices["output"]
    ) / 1_000_000
    return usd * 100
