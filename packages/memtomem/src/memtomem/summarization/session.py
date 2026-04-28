"""Episodic session summarization (RFC P1 Phase B).

When ``mem_session_end`` is called without an explicit ``summary=``, the
server collects the chunks added during the session, asks the configured
``LLMProvider`` for a short narrative summary, and Phase A's persistence
helper promotes the result to ``archive:session:<id>``.

This module owns only the LLM call. Selection of which chunks to feed,
the gating thresholds, and the file-write happen at the
``mem_session_end`` call site so that the helper stays test-friendly
(synchronous prompt build + a single ``llm.generate`` await).
"""

from __future__ import annotations

from importlib import resources
from typing import TYPE_CHECKING

from memtomem.llm.utils import strip_llm_response

if TYPE_CHECKING:
    from memtomem.llm.base import LLMProvider
    from memtomem.models import Chunk


class SessionTooLargeError(Exception):
    """Raised when the assembled prompt body exceeds ``max_input_chars``.

    Phase B v1 picks Open-Question-2 option (c): refuse auto-summary for
    sessions that won't fit in a single LLM call and let the caller
    provide a manual ``summary=`` instead. Hierarchical / windowed
    summarization is deferred.
    """


_PROMPT_RES = "memtomem.summarization.prompts"


def _load_system_prompt() -> str:
    return resources.files(_PROMPT_RES).joinpath("session.md").read_text(encoding="utf-8")


def _format_chunks_for_prompt(chunks: list[Chunk]) -> str:
    """Render chunks newest-first, separated by a stable delimiter.

    Each block is prefixed with the chunk's source file (when present)
    so the model can attribute facts. Heading hierarchy is included
    when available because the chunker preserves it as the most
    informative structural signal.
    """
    parts: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        meta = chunk.metadata
        header_bits: list[str] = [f"chunk {idx}"]
        if meta.source_file:
            header_bits.append(str(meta.source_file))
        if meta.heading_hierarchy:
            header_bits.append(" > ".join(meta.heading_hierarchy))
        header = " | ".join(header_bits)
        parts.append(f"--- {header} ---\n{chunk.content.strip()}")
    return "\n\n".join(parts)


async def summarize_session(
    session_id: str,
    chunks: list[Chunk],
    *,
    llm: LLMProvider,
    max_tokens: int = 500,
    max_input_chars: int = 60_000,
) -> str:
    """Produce a short narrative summary of a session's chunks.

    Args:
        session_id: Session identifier (used for the user-prompt header
            so a model with ID-leaning behavior can ground attribution).
        chunks: Chunks added during the session, ordered newest-first by
            the caller.
        llm: An initialized ``LLMProvider``. Caller is responsible for
            having checked ``LLMConfig.enabled``.
        max_tokens: Output cap passed straight to the provider.
        max_input_chars: Hard cap on the assembled chunk body. When the
            body exceeds this, raise ``SessionTooLargeError`` so the
            caller can fall back to skipping auto-summary.

    Returns:
        The summary text with provider chrome stripped via
        ``strip_llm_response``. May be an empty string when the model
        declined to produce content; callers treat that the same as
        "skip auto-summary" via ``not summary`` check.
    """
    if not chunks:
        return ""

    body = _format_chunks_for_prompt(chunks)
    if len(body) > max_input_chars:
        raise SessionTooLargeError(
            f"session {session_id} chunk body is {len(body)} chars, "
            f"exceeds max_input_chars={max_input_chars}"
        )

    system = _load_system_prompt()
    user = (
        f"Session id: {session_id}\n"
        f"Chunks added during the session ({len(chunks)} total, newest first):\n\n"
        f"{body}\n"
    )
    raw = await llm.generate(user, system=system, max_tokens=max_tokens)
    return strip_llm_response(raw).strip()
