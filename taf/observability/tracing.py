"""
Langfuse tracing - optional, and never allowed to break a run.

    run      one trace per QA run; the trace id is derived from the run id, so a run
             resumed after a crash or an approval lands in the same trace
    node     one span per workflow step (triage, generate_spec, validate_spec, ...)
    llm      a generation per model call, captured by Langfuse's LangChain callback
             inside the router - so model, token usage and latency come from the call
             itself, not from the wrapping step
    scores   validation, Playwright and approval outcomes on the trace

Tracing is on when LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are set (and
LANGFUSE_ENABLED is not false). Without them, without the package, or when Langfuse
fails, every method here is a no-op that logs one warning.

Everything sent passes through `mask`: known secrets and token-like strings are
redacted, and with LANGFUSE_CAPTURE=truncated (default) long text such as prompts,
diffs and logs is cut to LANGFUSE_MAX_FIELD_CHARS; `metadata` drops it entirely.
"""
from __future__ import annotations

import contextlib
import logging
import re
import threading
from typing import Any, Dict, Iterator, List, Optional, Tuple

from taf.config import SETTINGS, Settings, redact

log = logging.getLogger("qa.tracing")

# Token shapes that may appear in tool output or prompts even when they are not one
# of our configured secrets.
_SECRET_PATTERNS = [
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{10,}"),
    re.compile(r"(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"(?i)\b(authorization\s*:\s*(?:bearer|basic)\s+)[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"(?i)\b((?:password|passwd|secret|api[_-]?key|token)\s*[=:]\s*)[^\s'\"]{4,}"),
]
_MAX_DEPTH = 12
_METADATA_TEXT_LIMIT = 200


def scrub(text: str) -> str:
    """Configured secrets plus common token shapes, replaced."""
    text = redact(text)
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "***REDACTED***", text)
    return text


def mask_value(value: Any, capture: str, limit: int, depth: int = 0) -> Any:
    """Recursively redact (and, depending on `capture`, shorten or drop) text in a payload."""
    if depth > _MAX_DEPTH:
        return "[nested too deep]"
    if isinstance(value, str):
        value = scrub(value)
        if capture == "metadata" and len(value) > _METADATA_TEXT_LIMIT:
            return f"[{len(value):,} chars not captured: LANGFUSE_CAPTURE=metadata]"
        if capture != "full" and len(value) > limit:
            half = max(limit // 2, 1)
            return f"{value[:half]}\n...[{len(value) - 2 * half:,} chars truncated]...\n{value[-half:]}"
        return value
    if isinstance(value, dict):
        return {k: mask_value(v, capture, limit, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask_value(v, capture, limit, depth + 1) for v in value]
    return value


def _bounded(fn: Any, timeout_s: float, what: str) -> Any:
    """Run `fn` on a daemon thread, waiting at most `timeout_s`; Langfuse being down must not hang a run."""
    box: Dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except Exception as exc:  # noqa: BLE001
            box["error"] = exc

    worker = threading.Thread(target=target, name=f"langfuse-{what}", daemon=True)
    worker.start()
    worker.join(timeout_s)
    if worker.is_alive():
        log.warning("Langfuse %s did not finish within %ss - continuing without it.", what, timeout_s)
        return None
    if "error" in box:
        log.warning("Langfuse %s failed: %s", what, scrub(str(box["error"])))
        return None
    return box.get("value")


class _Span:
    """A handle on a span that swallows tracing errors. `span` is None when tracing is off."""

    def __init__(self, tracer: "Tracer", span: Any = None) -> None:
        self._tracer, self._span = tracer, span

    @property
    def id(self) -> Optional[str]:
        return getattr(self._span, "id", None)

    def update(self, **fields: Any) -> None:
        if self._span is None:
            return
        try:
            self._span.update(**fields)
        except Exception as exc:  # noqa: BLE001
            self._tracer._failed("span update", exc)


class Tracer:
    def __init__(self, settings: Settings, client: Any = None) -> None:
        self.settings = settings
        self._client = client
        self._init_done = client is not None
        self._disabled = ""
        self._lock = threading.Lock()
        self._roots: Dict[str, Tuple[str, Optional[str]]] = {}  # run_id -> (trace_id, root span id)
        self._warned = False

    # ------------------------------------------------------------ client --
    def _get_client(self) -> Any:
        with self._lock:
            if self._init_done:
                return self._client
            self._init_done = True
            s = self.settings
            if not s.langfuse_enabled:
                self._disabled = "LANGFUSE_ENABLED=false"
            elif not (s.langfuse_public_key and s.langfuse_secret_key):
                self._disabled = "LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY not set"
            else:
                try:
                    from langfuse import Langfuse

                    self._client = Langfuse(
                        public_key=s.langfuse_public_key, secret_key=s.langfuse_secret_key,
                        base_url=s.langfuse_base_url or None, timeout=s.langfuse_timeout_s,
                        environment=s.langfuse_environment or None, mask=self.mask,
                    )
                except Exception as exc:  # noqa: BLE001 - ImportError, bad config, ...
                    self._disabled = f"could not start the Langfuse client: {scrub(str(exc))}"
            if self._disabled:
                log.info("Langfuse tracing off: %s", self._disabled)
            return self._client

    @property
    def enabled(self) -> bool:
        return self._get_client() is not None

    @property
    def status(self) -> str:
        return "on" if self.enabled else f"off ({self._disabled})"

    def _failed(self, what: str, exc: BaseException) -> None:
        if not self._warned:
            log.warning("Langfuse %s failed (%s) - the run continues; further tracing errors are logged at debug.",
                        what, scrub(f"{type(exc).__name__}: {exc}"))
            self._warned = True
        else:
            log.debug("Langfuse %s failed: %s", what, exc)

    def mask(self, *, data: Any, **_: Any) -> Any:
        """Langfuse's mask hook: applied to every input, output and metadata payload."""
        try:
            return mask_value(data, self.settings.langfuse_capture, self.settings.langfuse_max_field_chars)
        except Exception:  # noqa: BLE001 - never ship unmasked data because masking broke
            return "[masking failed - payload dropped]"

    # ---------------------------------------------------------------- ids --
    def trace_id(self, run_id: str) -> str:
        """The run's trace id: deterministic, so resumes and evaluations correlate by run id."""
        if not self.enabled:
            return ""
        try:
            from langfuse import Langfuse

            return Langfuse.create_trace_id(seed=run_id)
        except Exception as exc:  # noqa: BLE001
            self._failed("trace id", exc)
            return ""

    @staticmethod
    def _attributes(state: Dict[str, Any], extra_tags: Optional[List[str]] = None) -> Dict[str, Any]:
        tags = ["taf", f"provider:{state.get('llm_provider') or 'default'}", *(extra_tags or [])]
        return {
            "session_id": str(state.get("ticket_id") or "")[:200] or None,
            "trace_name": "qa-run",
            "tags": tags,
            "metadata": {
                "run_id": str(state.get("run_id", "")),
                "ticket_id": str(state.get("ticket_id", "")),
                "target_repo": (SETTINGS.test_repo_url or SETTINGS.test_repo_local_path or "")[-200:],
                "base_branch": SETTINGS.test_repo_base_branch,
            },
        }

    # ------------------------------------------------------------- spans --
    @contextlib.contextmanager
    def run(self, state: Dict[str, Any], resumed: bool = False,
            extra_tags: Optional[List[str]] = None) -> Iterator[_Span]:
        """The root span of one run (or of one resumption of it)."""
        run_id = str(state.get("run_id", ""))
        client = self._get_client()
        trace_id = self.trace_id(run_id) if client else ""
        stack = contextlib.ExitStack()
        handle = _Span(self)
        if client and trace_id:
            try:
                from langfuse import propagate_attributes

                span = stack.enter_context(client.start_as_current_observation(
                    trace_context={"trace_id": trace_id}, name="qa-run.resume" if resumed else "qa-run",
                    as_type="agent",
                    input={"ticket_id": state.get("ticket_id"), "summary": state.get("summary"),
                           "target_url": state.get("target_url"), "stop_after": state.get("stop_after")},
                ))
                stack.enter_context(propagate_attributes(**self._attributes(state, extra_tags)))
                handle = _Span(self, span)
                with self._lock:
                    self._roots[run_id] = (trace_id, handle.id)
            except Exception as exc:  # noqa: BLE001
                self._failed("run start", exc)
                self._close(stack)
                stack = contextlib.ExitStack()
        try:
            yield handle
        except BaseException as exc:
            handle.update(level="ERROR", status_message=scrub(f"{type(exc).__name__}: {exc}")[:500])
            raise
        finally:
            with self._lock:
                self._roots.pop(run_id, None)
            self._close(stack)

    @contextlib.contextmanager
    def node(self, state: Dict[str, Any], name: str, as_type: str = "span",
             metadata: Optional[Dict[str, Any]] = None) -> Iterator[_Span]:
        """A span for one workflow step, parented to the run's root span."""
        client = self._get_client()
        run_id = str(state.get("run_id", ""))
        stack = contextlib.ExitStack()
        handle = _Span(self)
        if client:
            with self._lock:
                trace_id, parent = self._roots.get(run_id, ("", None))
            trace_id = trace_id or self.trace_id(run_id)
            if trace_id:
                try:
                    from langfuse import propagate_attributes

                    context: Dict[str, str] = {"trace_id": trace_id}
                    if parent:
                        context["parent_span_id"] = parent
                    span = stack.enter_context(client.start_as_current_observation(
                        trace_context=context, name=name, as_type=as_type, metadata=metadata or {}))
                    stack.enter_context(propagate_attributes(**self._attributes(state)))
                    handle = _Span(self, span)
                except Exception as exc:  # noqa: BLE001
                    self._failed("span start", exc)
                    self._close(stack)
                    stack = contextlib.ExitStack()
        try:
            yield handle
        finally:
            self._close(stack)

    def _close(self, stack: contextlib.ExitStack) -> None:
        try:
            stack.close()
        except Exception as exc:  # noqa: BLE001
            self._failed("span end", exc)

    # ------------------------------------------------------ llm / scores --
    def langchain_callbacks(self) -> List[Any]:
        """Langfuse's LangChain handler for one model call; it nests under the current step span."""
        if not self.enabled:
            return []
        try:
            from langfuse.langchain import CallbackHandler

            return [CallbackHandler()]
        except Exception as exc:  # noqa: BLE001
            self._failed("callback handler", exc)
            return []

    def score(self, run_id: str, name: str, value: Any, data_type: str = "NUMERIC", comment: str = "",
              environment: Optional[str] = None) -> None:
        client = self._get_client()
        trace_id = self.trace_id(run_id) if client else ""
        if not trace_id:
            return
        try:
            client.create_score(trace_id=trace_id, name=name, value=value, data_type=data_type,
                                comment=scrub(comment)[:1000] or None, environment=environment)
        except Exception as exc:  # noqa: BLE001
            self._failed("score", exc)

    def trace_url(self, run_id: str) -> str:
        """Link to the run's trace (asks Langfuse for the project id once; bounded)."""
        client = self._get_client()
        trace_id = self.trace_id(run_id) if client else ""
        if not trace_id:
            return ""
        return _bounded(lambda: client.get_trace_url(trace_id=trace_id),
                        self.settings.langfuse_flush_timeout_s, "trace url") or ""

    def flush(self) -> None:
        """Send buffered spans, waiting at most LANGFUSE_FLUSH_TIMEOUT_SECONDS."""
        client = self._client if self._init_done else None
        if client is not None:
            _bounded(client.flush, self.settings.langfuse_flush_timeout_s, "flush")

    def shutdown(self) -> None:
        client = self._client if self._init_done else None
        if client is not None:
            _bounded(client.shutdown, self.settings.langfuse_flush_timeout_s, "shutdown")

    def check(self) -> Tuple[bool, str]:
        """For `taf doctor`: are the credentials accepted?"""
        client = self._get_client()
        if client is None:
            return False, self._disabled
        ok = _bounded(client.auth_check, self.settings.langfuse_flush_timeout_s, "auth check")
        return bool(ok), "credentials accepted" if ok else "auth check failed or timed out"


TRACER = Tracer(SETTINGS)
