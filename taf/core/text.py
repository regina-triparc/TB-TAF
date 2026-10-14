"""Text helpers: timestamps, ANSI stripping, truncation, code-fence and message extraction."""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import List

from langchain_core.messages import BaseMessage

_ANSI_RE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
_CODE_FENCE_RE = re.compile(r"```(?:typescript|ts|tsx|javascript|js)?[ \t]*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def strip_ansi(text: str) -> str:
    """Remove terminal colour codes so logs are readable by LLMs and in Jira comments."""
    return _ANSI_RE.sub("", text)


def truncate_middle(text: str, limit: int) -> str:
    """
    Keep the head and (larger) tail of a long text. Test/CI logs put the verdict at
    the end, so the tail is weighted 2:1 over the head.
    """
    if not text or len(text) <= limit:
        return text or ""
    head = limit // 3
    tail = limit - head
    return f"{text[:head]}\n\n... [truncated {len(text) - limit:,} characters] ...\n\n{text[-tail:]}"


def extract_code_block(text: str) -> str:
    """Return the largest fenced code block from an LLM answer, or the raw text if unfenced."""
    blocks = _CODE_FENCE_RE.findall(text or "")
    code = max(blocks, key=len) if blocks else (text or "")
    return code.strip() + "\n"


def message_text(message: BaseMessage) -> str:
    """Flatten a LangChain message's content (str or list of content blocks) into plain text."""
    content = message.content
    if isinstance(content, str):
        return content
    parts: List[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "".join(parts)
