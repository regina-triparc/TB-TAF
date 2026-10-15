"""
LLM router: decides which model serves each agent, and is the only place that
calls a model.

    provider   triage / architect / automation          report synthesizer
    ---------  ---------------------------------------  -------------------
    claude     Claude (model + effort per agent)        Ollama, fallback text
    ollama     Ollama (OLLAMA_MODEL or per-agent model) Ollama, fallback text
    auto       claude if ANTHROPIC_API_KEY is set, else ollama
    offline    built-in deterministic stand-in          fallback text

The provider is chosen, most specific first, from:
    1. a per-run override  (`--provider` on the CLI, `provider` on /dashboard/api/start)
    2. the process default (LLM_PROVIDER, switchable at runtime: dashboard selector,
       POST /dashboard/api/provider)
and then CLOUD_AGENTS pins individual agents to Claude inside a local run.

Agents never construct clients themselves: they call `ROUTER.structured(...)` or
`ROUTER.text(...)`, which also apply the budget guard and the stop-reason checks.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Type

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from pydantic import BaseModel

from taf.config import LLM_PROVIDERS, SETTINGS, Settings
from taf.llm import offline
from taf.llm.budget import BUDGET
from taf.llm.pricing import MODEL_PRICING, token_usage
from taf.observability.tracing import TRACER

log = logging.getLogger("qa.router")

AGENT_LABELS: Dict[str, str] = {
    "triage": "Intake Triager",
    "architect": "Test Architect",
    "automation": "TS Automation Engineer",
}
# Routes outside the main pipeline: the optional LLM review of a generated spec.
EXTRA_LABELS: Dict[str, str] = {"review": "Test Reviewer"}
_VALID_EFFORTS = {"low", "medium", "high", "xhigh", "max"}


class LLMOutputError(RuntimeError):
    """Raised when an LLM answer is refused, truncated, or cannot be parsed."""


@dataclass(frozen=True)
class AgentRoute:
    """Which model an agent runs on, how hard it thinks, and whether it caches."""

    key: str
    label: str
    model: str
    effort: str          # Claude output_config.effort; "n/a" for local providers
    max_tokens: int      # includes adaptive-thinking tokens on Claude, so leave headroom
    cache_context: bool  # mark the per-run context block with cache_control
    provider: str        # "claude" | "ollama" | "offline" - the concrete client

    @property
    def is_local(self) -> bool:
        return self.provider != "claude"


class LLMRouter:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._lock = threading.Lock()
        self._default = settings.llm_provider
        self._clients: Dict[tuple, Any] = {}
        self._warn_on_bad_config()

    # ------------------------------------------------------------ provider --
    @property
    def default_provider(self) -> str:
        with self._lock:
            return self._default

    def set_default(self, provider: str) -> str:
        """Switch the process-wide default at runtime. Runs already in flight keep their routes."""
        provider = (provider or "").strip().lower()
        if provider not in LLM_PROVIDERS:
            raise ValueError(f"provider must be one of {', '.join(LLM_PROVIDERS)}")
        with self._lock:
            self._default = provider
        log.info("Default LLM provider switched to %s (%s)", provider, self.resolve(provider))
        return provider

    def resolve(self, provider: Optional[str] = None) -> str:
        """The concrete provider: 'auto' becomes claude or ollama, an empty override means the default."""
        chosen = (provider or "").strip().lower() or self.default_provider
        if chosen not in LLM_PROVIDERS:
            raise ValueError(f"Unknown LLM provider {chosen!r}; use one of {', '.join(LLM_PROVIDERS)}")
        if chosen == "auto":
            return "claude" if self.settings.anthropic_api_key else "ollama"
        return chosen

    def _pinned_to_cloud(self, key: str) -> bool:
        pinned = {a.strip().lower() for a in self.settings.cloud_agents.split(",") if a.strip()}
        return key in pinned or "all" in pinned

    def _cache_enabled(self, key: str) -> bool:
        agents = {a.strip().lower() for a in self.settings.prompt_cache_agents.split(",")}
        return "all" in agents or key in agents

    # -------------------------------------------------------------- routes --
    def route(self, key: str, provider: Optional[str] = None) -> AgentRoute:
        s = self.settings
        label = AGENT_LABELS.get(key) or EXTRA_LABELS[key]
        concrete = self.resolve(provider)
        if concrete == "offline":
            return AgentRoute(key, label, "offline-stub", "n/a", 0, False, "offline")
        if concrete == "ollama" and not self._pinned_to_cloud(key):
            local_override = {"triage": s.ollama_triage_model,
                              "architect": s.ollama_architect_model,
                              "automation": s.ollama_automation_model,
                              "review": s.ollama_review_model}[key]
            return AgentRoute(key, label, local_override or s.ollama_model, "n/a",
                              s.ollama_max_tokens, False, "ollama")
        model, effort, max_tokens = {
            "triage": (s.claude_triage_model, s.claude_triage_effort, s.claude_triage_max_tokens),
            "architect": (s.claude_architect_model, s.claude_architect_effort, s.claude_architect_max_tokens),
            "automation": (s.claude_automation_model, s.claude_automation_effort, s.claude_automation_max_tokens),
            "review": (s.claude_review_model, s.claude_review_effort, s.claude_review_max_tokens),
        }[key]
        return AgentRoute(key, label, model, effort, max_tokens, self._cache_enabled(key), "claude")

    def routes(self, provider: Optional[str] = None) -> Dict[str, AgentRoute]:
        return {key: self.route(key, provider) for key in AGENT_LABELS}

    def describe(self, provider: Optional[str] = None) -> Dict[str, Any]:
        """JSON-friendly routing table for /health, the dashboard and `taf providers`."""
        return {
            "default": self.default_provider,
            "resolved": self.resolve(provider),
            "available": list(LLM_PROVIDERS),
            "claude_configured": bool(self.settings.anthropic_api_key),
            "routes": {k: {"provider": r.provider, "model": r.model, "effort": r.effort,
                           "max_tokens": r.max_tokens, "prompt_cache": r.cache_context}
                       for k, r in self.routes(provider).items()},
            "report": ({"provider": "offline", "model": "built-in fallback"}
                       if self.resolve(provider) == "offline"
                       else {"provider": "ollama", "model": self.settings.ollama_model}),
        }

    # ------------------------------------------------------------- clients --
    def chat_model(self, route: AgentRoute) -> Any:
        """One cached LangChain client per (provider, model, cap, effort)."""
        cache_key = (route.provider, route.model, route.max_tokens, route.effort)
        with self._lock:
            client = self._clients.get(cache_key)
        if client is not None:
            return client
        s = self.settings
        if route.provider == "ollama":
            from langchain_openai import ChatOpenAI
            client = ChatOpenAI(model=route.model, base_url=s.ollama_base_url, api_key="ollama",
                                temperature=0.0, max_tokens=route.max_tokens,
                                timeout=s.ollama_timeout_s, max_retries=1)
        elif route.provider == "claude":
            if not s.anthropic_api_key:
                raise RuntimeError("ANTHROPIC_API_KEY is not configured. Set it, or switch the "
                                   "provider to ollama / offline (LLM_PROVIDER or --provider).")
            from langchain_anthropic import ChatAnthropic
            # No temperature/top_p: Claude 5.x thinks adaptively; depth is steered with effort.
            client = ChatAnthropic(model=route.model, api_key=s.anthropic_api_key,
                                   max_tokens=route.max_tokens, reasoning_effort=route.effort,
                                   timeout=s.claude_timeout_s, max_retries=3)
        else:
            raise RuntimeError(f"No LangChain client for provider {route.provider!r}")
        with self._lock:
            self._clients.setdefault(cache_key, client)
        return client

    def report_model(self) -> Any:
        """The free local model used by the Report Synthesizer."""
        route = AgentRoute("report", "Report Synthesizer", self.settings.ollama_model, "n/a",
                           0, False, "ollama")
        cache_key = ("report", route.model)
        with self._lock:
            client = self._clients.get(cache_key)
        if client is None:
            from langchain_openai import ChatOpenAI
            client = ChatOpenAI(model=route.model, base_url=self.settings.ollama_base_url,
                                api_key="ollama", temperature=0.0,
                                timeout=self.settings.ollama_timeout_s, max_retries=1)
            with self._lock:
                self._clients.setdefault(cache_key, client)
        return client

    # ------------------------------------------------------------- prompts --
    def cache_control(self) -> Dict[str, str]:
        """Explicit prompt-cache breakpoint. 5m writes cost 1.25x input, 1h writes 2x; reads 0.05-0.1x."""
        control: Dict[str, str] = {"type": "ephemeral"}
        if self.settings.prompt_cache_ttl == "1h":
            control["ttl"] = "1h"
        return control

    def context_message(self, route: AgentRoute, text: str) -> HumanMessage:
        """
        The large per-run context (ticket, diff, plan) as the first user turn. On routes
        with caching enabled it carries a cache breakpoint, so every later request that
        re-sends the same prefix (repair turns, retries) reads it from cache.
        """
        block: Dict[str, Any] = {"type": "text", "text": text}
        if route.cache_context:
            block["cache_control"] = self.cache_control()
        return HumanMessage(content=[block])

    # --------------------------------------------------------------- calls --
    def text(self, state: Dict[str, Any], route: AgentRoute, messages: List[BaseMessage]) -> AIMessage:
        """Budget-guarded free-text call; returns the AIMessage so it can be replayed."""
        if route.provider == "offline":
            return offline.text(route.key, state, messages)
        BUDGET.ensure_available(state["run_id"])
        response = self.chat_model(route).invoke(messages, config=invoke_config(state, route))
        BUDGET.record(state["run_id"], state["ticket_id"], route.label, token_usage(response),
                      route.model, route.provider)
        _check_stop_reason(route, response)
        return response

    def structured(self, state: Dict[str, Any], route: AgentRoute, messages: List[BaseMessage],
                   schema: Type[BaseModel]) -> BaseModel:
        """
        Budget-guarded call returning a validated Pydantic object via native structured
        outputs (json_schema). Forced tool use is rejected by Opus 5.5, so it is not used.
        `include_raw=True` keeps the raw AIMessage so usage is still billed.
        """
        if route.provider == "offline":
            return offline.structured(route.key, state, schema)
        BUDGET.ensure_available(state["run_id"])
        runnable = self.chat_model(route).with_structured_output(schema, method="json_schema", include_raw=True)
        result = runnable.invoke(messages, config=invoke_config(state, route))
        raw = result.get("raw")
        BUDGET.record(state["run_id"], state["ticket_id"], route.label, token_usage(raw),
                      route.model, route.provider)
        _check_stop_reason(route, raw)
        parsed = result.get("parsed")
        if parsed is None:
            raise LLMOutputError(f"{route.label}: could not parse structured output: {result.get('parsing_error')}")
        return parsed

    # -------------------------------------------------------------- checks --
    def _warn_on_bad_config(self) -> None:
        cfg = logging.getLogger("qa.config")
        if self.settings.prompt_cache_ttl not in {"5m", "1h"}:
            cfg.warning("PROMPT_CACHE_TTL=%r is not '5m' or '1h' - using 5m.", self.settings.prompt_cache_ttl)
        if self.resolve() != "claude":
            return
        for route in self.routes().values():
            if route.provider != "claude":
                continue
            if route.effort not in _VALID_EFFORTS:
                cfg.warning("CLAUDE_%s_EFFORT=%r is not one of %s - the API will reject it.",
                            route.key.upper(), route.effort, sorted(_VALID_EFFORTS))
            if route.model not in MODEL_PRICING:
                cfg.warning("No price known for %s (%s) - add it via CLAUDE_PRICING_JSON.",
                            route.model, route.label)


def invoke_config(state: Dict[str, Any], route: AgentRoute) -> Dict[str, Any]:
    """
    LangChain run config for one model call. The Langfuse handler (when tracing is on)
    records the call as a generation under the current workflow step: model, provider,
    token usage, latency and the masked prompt/response.
    """
    return {
        "callbacks": TRACER.langchain_callbacks(),
        "run_name": route.label,
        "tags": [f"agent:{route.key}", f"provider:{route.provider}"],
        "metadata": {"run_id": state.get("run_id", ""), "ticket_id": state.get("ticket_id", ""),
                     "agent": route.key, "provider": route.provider, "model": route.model,
                     "effort": route.effort},
    }


def _check_stop_reason(route: AgentRoute, message: Any) -> None:
    metadata = getattr(message, "response_metadata", None) or {}
    stop_reason = metadata.get("stop_reason")
    if metadata.get("finish_reason") == "length":  # OpenAI-compatible servers (Ollama)
        stop_reason = "max_tokens"
    if stop_reason == "refusal":
        raise LLMOutputError(f"{route.label}: {route.model} declined the request (stop_reason=refusal).")
    if stop_reason == "max_tokens":
        knob = "OLLAMA_MAX_TOKENS" if route.provider == "ollama" else f"CLAUDE_{route.key.upper()}_MAX_TOKENS"
        raise LLMOutputError(f"{route.label}: output hit max_tokens={route.max_tokens} - raise {knob}"
                             + ("" if route.provider == "ollama" else " or lower the effort") + ".")


ROUTER = LLMRouter(SETTINGS)
