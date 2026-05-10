"""Unit tests for the per-source AI summary module.

Covers:
- ``compute_source_signature`` purity / order-independence
- ``build_summary_prompt`` language directive presence
- ``maybe_update_ai_summary`` skip rules (off, no LLM, signature match,
  language drift)
- LLM exception fail-soft (cache untouched, no raise)
- ``regenerate_for_paths`` overrides signature skip and writes the new
  language tag
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem.indexing.summarizer import (
    build_summary_prompt,
    compute_source_signature,
    maybe_update_ai_summary,
    regenerate_for_paths,
)


def _make_chunk(content: str, content_hash: str):
    """Lightweight stand-in for ``memtomem.models.Chunk`` — only the
    attributes the summarizer touches need to exist."""
    chunk = MagicMock()
    chunk.content = content
    chunk.content_hash = content_hash
    return chunk


def _make_config(
    *,
    auto_summarize: bool = True,
    language: str = "en",
    max_chars: int = 3000,
    max_tokens: int = 256,
) -> MagicMock:
    cfg = MagicMock()
    cfg.auto_summarize = auto_summarize
    cfg.summary_language = language
    cfg.summary_max_input_chars = max_chars
    cfg.summary_max_tokens = max_tokens
    return cfg


# ---- compute_source_signature ------------------------------------------------


def test_signature_stable_for_same_chunks_regardless_of_order():
    """Signature is over the *set* of chunk hashes, not the input order —
    a chunker that reorders sections must still produce the same signature
    so the cache stays warm. (Why this matters: the differ keys cache hits
    off this; if order mattered, every chunker pass with reorder-by-default
    semantics would force a regenerate.)"""
    chunks_a = [_make_chunk("a", "h1"), _make_chunk("b", "h2"), _make_chunk("c", "h3")]
    chunks_b = [_make_chunk("c", "h3"), _make_chunk("a", "h1"), _make_chunk("b", "h2")]
    assert compute_source_signature(chunks_a) == compute_source_signature(chunks_b)


def test_signature_changes_when_one_hash_changes():
    """Single chunk content_hash flip → signature must differ. Without
    this the indexing pipeline would skip regeneration on real content
    edits (the failure mode that motivated using the signature in the
    first place)."""
    chunks_a = [_make_chunk("a", "h1"), _make_chunk("b", "h2")]
    chunks_b = [_make_chunk("a", "h1"), _make_chunk("b", "h2-new")]
    assert compute_source_signature(chunks_a) != compute_source_signature(chunks_b)


def test_signature_format_is_hex_sha256():
    chunks = [_make_chunk("body", "hash")]
    sig = compute_source_signature(chunks)
    assert len(sig) == 64
    int(sig, 16)  # raises if not hex


# ---- build_summary_prompt ---------------------------------------------------


def test_prompt_includes_known_language_label_for_korean():
    """Pin that ``ko`` resolves to ``Korean`` in both system and user
    prompts. Mid-flight drop of the user-prompt language directive is the
    typical regression — small models obey the user prompt more
    consistently than the system prompt, so it's load-bearing."""
    system, user = build_summary_prompt("Body text.", "ko")
    assert "Korean" in system
    assert "Korean" in user


def test_prompt_falls_back_to_english_for_empty_language():
    system, user = build_summary_prompt("Body.", "")
    assert "English" in system
    assert "English" in user


def test_prompt_uses_uppercase_raw_for_unknown_code():
    """Unknown ISO codes (regional variants, future locales) get
    capitalised verbatim rather than silently downgrading to English —
    matches the user's intent better than a fallback would."""
    system, user = build_summary_prompt("Body.", "xx-yy")
    # Code is uppercased in our table miss path
    assert "XX-YY" in system or "XX-YY" in user


# ---- maybe_update_ai_summary -----------------------------------------------


@pytest.mark.asyncio
async def test_disabled_skips_llm():
    """``auto_summarize=False`` → no LLM call, no storage write, no read."""
    storage = MagicMock()
    storage.get_ai_summary = AsyncMock()
    storage.set_ai_summary = AsyncMock()
    llm = MagicMock()
    llm.generate = AsyncMock()
    config = _make_config(auto_summarize=False)
    chunks = [_make_chunk("body", "h1")]

    await maybe_update_ai_summary(storage, llm, Path("/tmp/x.md"), chunks, config)

    llm.generate.assert_not_called()
    storage.get_ai_summary.assert_not_called()
    storage.set_ai_summary.assert_not_called()


@pytest.mark.asyncio
async def test_no_llm_provider_skips_quietly():
    """Even with ``auto_summarize=True``, ``llm=None`` is a valid no-op
    — server may have LLM disabled at runtime."""
    storage = MagicMock()
    storage.get_ai_summary = AsyncMock()
    storage.set_ai_summary = AsyncMock()
    config = _make_config()
    chunks = [_make_chunk("body", "h1")]

    await maybe_update_ai_summary(storage, None, Path("/tmp/x.md"), chunks, config)

    storage.set_ai_summary.assert_not_called()


@pytest.mark.asyncio
async def test_no_chunks_no_cached_summary_is_pure_noop():
    """An empty chunk list with no prior cache → no LLM call, no
    storage writes. Engine's ``maybe_update_ai_summary`` is invoked
    after every reindex, including ones that produce zero chunks for
    a previously-unseen file (e.g., empty markdown skipped by the
    chunker); the helper must not pretend a summary exists."""
    storage = MagicMock()
    storage.get_ai_summary = AsyncMock(return_value=None)
    storage.set_ai_summary = AsyncMock()
    storage.delete_ai_summary = AsyncMock()
    llm = MagicMock()
    llm.generate = AsyncMock()
    config = _make_config()

    await maybe_update_ai_summary(storage, llm, Path("/tmp/x.md"), [], config)

    llm.generate.assert_not_called()
    storage.set_ai_summary.assert_not_called()
    storage.delete_ai_summary.assert_not_called()


@pytest.mark.asyncio
async def test_zero_chunk_reindex_clears_existing_cache():
    """When a previously-summarised source is reindexed into zero chunks
    (file emptied, became unchunkable, or hit an exclusion filter), the
    cached prose is now derived from content that no longer exists. The
    transactional ``delete_chunks`` cleanup is gated on
    ``not _in_transaction`` to support the rewrite case, so this hook
    is the *only* place that catches a genuine "now empty" reindex —
    leaving the cache here exposes stale, source-derived prose via
    ``/api/sources`` after the chunks themselves are gone (privacy +
    correctness regression)."""
    storage = MagicMock()
    storage.get_ai_summary = AsyncMock(
        return_value={"summary": "stale prose", "signature": "old-sig", "language": "en"}
    )
    storage.set_ai_summary = AsyncMock()
    storage.delete_ai_summary = AsyncMock()
    llm = MagicMock()
    llm.generate = AsyncMock()
    config = _make_config()

    await maybe_update_ai_summary(storage, llm, Path("/tmp/x.md"), [], config)

    # No regeneration attempted; cache cleared so the next /api/sources
    # call falls back to the heuristic excerpt (which itself returns
    # nothing for a zero-chunk source — exactly the right behaviour).
    llm.generate.assert_not_called()
    storage.set_ai_summary.assert_not_called()
    storage.delete_ai_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_signature_match_skips_llm_even_when_language_differs():
    """Cache hit on signature → skip the LLM even if the cached record's
    language doesn't match the current ``summary_language``. Language
    drift is intentionally NOT a regenerate trigger here — if it were,
    flipping ``summary_language`` would silently rebuild every cached
    summary on the next reindex (each one a billable LLM call). The bulk
    regenerate endpoint owns the language-flip path."""
    chunks = [_make_chunk("body", "h1"), _make_chunk("more", "h2")]
    sig = compute_source_signature(chunks)
    storage = MagicMock()
    storage.get_ai_summary = AsyncMock(
        return_value={"summary": "old", "signature": sig, "language": "en"}
    )
    storage.set_ai_summary = AsyncMock()
    llm = MagicMock()
    llm.generate = AsyncMock()
    config = _make_config(language="ko")  # cached as "en", current is "ko"

    await maybe_update_ai_summary(storage, llm, Path("/tmp/x.md"), chunks, config)

    llm.generate.assert_not_called()
    storage.set_ai_summary.assert_not_called()


@pytest.mark.asyncio
async def test_signature_miss_calls_llm_and_writes_record():
    """No cached record → LLM is called and a new record is persisted
    in the *current* configured language."""
    chunks = [_make_chunk("body", "h1")]
    storage = MagicMock()
    storage.get_ai_summary = AsyncMock(return_value=None)
    storage.set_ai_summary = AsyncMock()
    llm = MagicMock()
    llm.generate = AsyncMock(return_value="LLM-generated summary.")
    config = _make_config(language="ko")

    await maybe_update_ai_summary(storage, llm, Path("/tmp/x.md"), chunks, config)

    llm.generate.assert_awaited_once()
    storage.set_ai_summary.assert_awaited_once()
    call_kwargs = storage.set_ai_summary.call_args.kwargs
    assert call_kwargs["summary"] == "LLM-generated summary."
    assert call_kwargs["language"] == "ko"
    assert call_kwargs["signature"] == compute_source_signature(chunks)


@pytest.mark.asyncio
async def test_llm_exception_does_not_propagate_when_no_cache():
    """LLM raise + no prior cache → log + return; nothing persisted,
    nothing deleted. Guarantees indexing never blocks on summarisation
    and the absence of a cache row stays absent (no phantom delete
    that would mask a real bug)."""
    chunks = [_make_chunk("body", "h1")]
    storage = MagicMock()
    storage.get_ai_summary = AsyncMock(return_value=None)
    storage.set_ai_summary = AsyncMock()
    storage.delete_ai_summary = AsyncMock()
    llm = MagicMock()
    llm.generate = AsyncMock(side_effect=RuntimeError("Ollama down"))
    config = _make_config()

    # Must not raise — caller (engine) never wraps this.
    await maybe_update_ai_summary(storage, llm, Path("/tmp/x.md"), chunks, config)

    storage.set_ai_summary.assert_not_called()
    storage.delete_ai_summary.assert_not_called()


@pytest.mark.asyncio
async def test_llm_failure_clears_stale_cache_on_signature_drift():
    """Cached signature mismatches the new chunks (content drifted) and
    the LLM call fails → the stale prose must be cleared so
    ``/api/sources`` falls back to the heuristic excerpt instead of
    rendering an out-of-date AI summary against the new chunk set.

    This is the privacy/correctness path: pre-fix, a user editing a
    file while the LLM provider was down would still see the *prior*
    AI summary on the Source tab — labelled with the ✨ marker — even
    though it no longer described the file's contents."""
    chunks = [_make_chunk("new body", "hash-new")]
    storage = MagicMock()
    storage.get_ai_summary = AsyncMock(
        return_value={"summary": "old prose", "signature": "hash-old", "language": "en"}
    )
    storage.set_ai_summary = AsyncMock()
    storage.delete_ai_summary = AsyncMock()
    llm = MagicMock()
    llm.generate = AsyncMock(side_effect=RuntimeError("Ollama down"))
    config = _make_config()

    await maybe_update_ai_summary(storage, llm, Path("/tmp/x.md"), chunks, config)

    storage.set_ai_summary.assert_not_called()
    # Stale prose must be evicted — that's the load-bearing assertion.
    storage.delete_ai_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_empty_llm_output_clears_stale_cache_on_signature_drift():
    """Symmetric to the exception path: an LLM that returns
    whitespace/empty on a signature-drifted source must also evict the
    stale cache. Different upstream failure mode (model returned
    nothing instead of raising), same downstream contract — heuristic
    fallback rather than mismatched prose."""
    chunks = [_make_chunk("new body", "hash-new")]
    storage = MagicMock()
    storage.get_ai_summary = AsyncMock(
        return_value={"summary": "old prose", "signature": "hash-old", "language": "en"}
    )
    storage.set_ai_summary = AsyncMock()
    storage.delete_ai_summary = AsyncMock()
    llm = MagicMock()
    llm.generate = AsyncMock(return_value="   \n\n  ")
    config = _make_config()

    await maybe_update_ai_summary(storage, llm, Path("/tmp/x.md"), chunks, config)

    storage.set_ai_summary.assert_not_called()
    storage.delete_ai_summary.assert_awaited_once()


@pytest.mark.asyncio
async def test_llm_failure_no_cache_does_not_call_delete():
    """When the LLM fails and no prior cache exists, we must NOT issue
    a defensive ``delete_ai_summary`` — that path takes a write lock
    on the SQLite file for nothing on every failure during the initial
    summarisation pass (large corpora hitting a transient Ollama). Pin
    the gate explicitly."""
    chunks = [_make_chunk("body", "h1")]
    storage = MagicMock()
    storage.get_ai_summary = AsyncMock(return_value=None)
    storage.set_ai_summary = AsyncMock()
    storage.delete_ai_summary = AsyncMock()
    llm = MagicMock()
    llm.generate = AsyncMock(side_effect=RuntimeError("Ollama down"))
    config = _make_config()

    await maybe_update_ai_summary(storage, llm, Path("/tmp/x.md"), chunks, config)

    storage.delete_ai_summary.assert_not_called()


# ---- regenerate_for_paths ---------------------------------------------------


@pytest.mark.asyncio
async def test_regenerate_overrides_signature_skip():
    """The bulk regenerate path bypasses the signature-skip — even when
    the cached signature matches, it still calls the LLM and writes a
    new record in the requested language. Without this, language drift
    would never resolve once cached."""
    chunks = [_make_chunk("body", "h1")]
    sig = compute_source_signature(chunks)

    async def chunks_for(_p):
        return chunks

    storage = MagicMock()
    storage.get_ai_summary = AsyncMock(
        return_value={"summary": "old en", "signature": sig, "language": "en"}
    )
    storage.set_ai_summary = AsyncMock()
    llm = MagicMock()
    llm.generate = AsyncMock(return_value="새 한국어 요약.")
    config = _make_config(language="ko")

    result = await regenerate_for_paths(storage, llm, [Path("/tmp/a.md")], chunks_for, config)

    assert result == {"processed": 1, "skipped": 0, "failed": 0}
    llm.generate.assert_awaited_once()
    call_kwargs = storage.set_ai_summary.call_args.kwargs
    assert call_kwargs["language"] == "ko"
    assert call_kwargs["summary"] == "새 한국어 요약."


@pytest.mark.asyncio
async def test_regenerate_counts_failures_and_skips():
    """One LLM failure → counted as failed, doesn't abort the run.
    Empty chunks for a path → counted as skipped. Both keep iterating
    so a single bad file can't kill the bulk job."""

    async def chunks_for(p):
        # Distinct body text per path so the fake LLM can identify
        # which file's prompt it's seeing without needing extra plumbing.
        if p.name == "good.md":
            return [_make_chunk("normal good content", "h-good")]
        if p.name == "bad.md":
            return [_make_chunk("BADMARKER content", "h-bad")]
        return []  # missing.md → empty

    storage = MagicMock()
    storage.get_ai_summary = AsyncMock(return_value=None)
    storage.set_ai_summary = AsyncMock()
    llm = MagicMock()

    async def fake_generate(prompt, **kwargs):
        if "BADMARKER" in prompt:
            raise RuntimeError("model refused")
        return "good summary"

    llm.generate = AsyncMock(side_effect=fake_generate)
    config = _make_config()

    paths = [Path("/tmp/good.md"), Path("/tmp/bad.md"), Path("/tmp/missing.md")]
    result = await regenerate_for_paths(storage, llm, paths, chunks_for, config)

    assert result == {"processed": 1, "skipped": 1, "failed": 1}


@pytest.mark.asyncio
async def test_regenerate_progress_callback_fires_per_path():
    """Progress callback fires once per path (after success or skip /
    failure) so the Web status endpoint sees monotonic progress."""

    async def chunks_for(_p):
        return [_make_chunk("body", "h1")]

    storage = MagicMock()
    storage.get_ai_summary = AsyncMock(return_value=None)
    storage.set_ai_summary = AsyncMock()
    llm = MagicMock()
    llm.generate = AsyncMock(return_value="ok")
    config = _make_config()

    calls: list[tuple[int, int, int]] = []

    def progress(processed: int, total: int, failed: int) -> None:
        calls.append((processed, total, failed))

    paths = [Path("/tmp/a.md"), Path("/tmp/b.md"), Path("/tmp/c.md")]
    await regenerate_for_paths(storage, llm, paths, chunks_for, config, progress=progress)

    assert len(calls) == 3
    # Final call reflects total processed; total stays constant.
    assert calls[-1][0] == 3
    assert all(c[1] == 3 for c in calls)
