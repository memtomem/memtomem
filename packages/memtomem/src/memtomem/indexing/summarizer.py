"""Per-source AI summary generation for the Source-tab preview.

Hooked into the indexing pipeline so a 2-3 sentence prose summary is produced
for each source the first time it's indexed and refreshed only when the
underlying chunks change. Cached records live in ``_memtomem_meta`` keyed by
``ai_summary:{normalised_path}`` (see ``storage.sqlite_backend``).

Two write paths exist:

* :func:`maybe_update_ai_summary` — called per file from the indexing
  engine. Skips the LLM whenever the content signature is unchanged, even
  if the configured ``summary_language`` differs from the cached record's
  language. Language drift is left for the explicit bulk-regenerate flow
  (so a config flip doesn't silently re-spend the LLM budget).
* :func:`regenerate_for_paths` — invoked from the bulk-regenerate Web
  endpoint. Bypasses the signature-skip and rewrites cached records in
  the requested target language.

LLM failures are always *fail-soft*: we log a warning and leave the
existing cache row untouched. Indexing must never block on summarization.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Sequence

from memtomem.llm.utils import strip_llm_response

if TYPE_CHECKING:
    from memtomem.config import IndexingConfig
    from memtomem.llm.base import LLMProvider
    from memtomem.models import Chunk
    from memtomem.storage.sqlite_backend import SqliteBackend

logger = logging.getLogger(__name__)


# How many leading chunks to feed the LLM. The first few sections capture
# what the file is about; later sections rarely change the high-level
# summary but inflate the prompt linearly. Five is enough for typical
# READMEs / notes while staying under the input-char cap on most files.
_MAX_CHUNKS_FOR_PROMPT = 5

# ISO code → human-readable language name used in the prompt directive.
# Falls back to the raw code (capitalised) when the user supplies a code
# we don't recognise — keeps the surface honest for arbitrary locales.
_LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "ko": "Korean",
    "ja": "Japanese",
    "zh": "Chinese",
    "es": "Spanish",
    "fr": "French",
    "de": "German",
    "pt": "Portuguese",
    "ru": "Russian",
    "it": "Italian",
}


def _language_label(code: str) -> str:
    code = (code or "").strip().lower()
    if code in _LANGUAGE_NAMES:
        return _LANGUAGE_NAMES[code]
    # Unknown code — capitalise the raw value so the prompt still reads
    # naturally ("write the summary in xx-YY"). Empty falls back to English
    # so we never emit an unbounded directive.
    return code.upper() if code else "English"


def compute_source_signature(chunks: Sequence["Chunk"]) -> str:
    """SHA256 hex digest over the sorted ``content_hash`` set of *chunks*.

    Pure function — order-independent, deterministic, and bound to the
    indexer's existing change-detection signal (per-chunk content hashes
    drive the differ in :mod:`memtomem.indexing.differ`). Mirroring that
    signal here keeps the two systems impossible to drift apart.
    """
    digest = hashlib.sha256()
    for h in sorted(c.content_hash for c in chunks):
        digest.update(h.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def build_summary_prompt(body: str, language: str) -> tuple[str, str]:
    """Return ``(system, user)`` prompts for a 2-3 sentence source summary.

    The language directive is included in *both* halves so smaller models
    that ignore the system prompt still see it. Asking the model to omit
    preamble/markdown reduces the work :func:`strip_llm_response` has to
    do downstream.
    """
    label = _language_label(language)
    system = (
        f"You are a concise summarization assistant. Always reply in {label}. "
        "Output the summary text only — no preamble, no headings, no markdown."
    )
    user = (
        f"Summarize the following document in 2-3 sentences in {label}. "
        "Focus on what the document is about, not its length or structure.\n\n"
        f"---\n{body}"
    )
    return system, user


def _build_body(chunks: Sequence["Chunk"], max_chars: int) -> str:
    """Join the leading chunk bodies and clamp to *max_chars*."""
    joined = "\n\n".join(c.content for c in chunks[:_MAX_CHUNKS_FOR_PROMPT] if c.content)
    return joined[:max_chars]


async def _generate(
    llm: "LLMProvider",
    body: str,
    language: str,
    max_tokens: int,
) -> str | None:
    """Call the LLM and post-process. Returns ``None`` on any failure."""
    system, user = build_summary_prompt(body, language)
    try:
        raw = await llm.generate(user, system=system, max_tokens=max_tokens)
    except Exception as exc:
        # Never propagate — the caller's flow (indexing, bulk regenerate)
        # must continue regardless of LLM availability.
        logger.warning("LLM summary call failed: %s", exc)
        return None
    text = strip_llm_response(raw or "").strip()
    return text or None


async def maybe_update_ai_summary(
    storage: "SqliteBackend",
    llm: "LLMProvider | None",
    source_path: Path,
    chunks: Sequence["Chunk"],
    config: "IndexingConfig",
) -> None:
    """Generate or refresh the AI summary for *source_path* if needed.

    Skip rules — earliest applies first:

    1. ``auto_summarize=False`` or no LLM provider → no-op (feature off).
    2. No chunks → drop any stale cache and return (the source has been
       emptied or became unchunkable; the previously summarised content
       no longer exists, so leaving the row would expose prose for a
       non-existent file).
    3. Content signature matches the cached record's signature → skip
       even when the cached record's language differs from the current
       ``summary_language``. The bulk-regenerate flow handles language
       drift; doing it here would spend LLM cost on every reindex
       whenever a user flips ``summary_language``.

    Failure modes:

    * Empty body after truncation (content collapsed to whitespace):
      same shape as zero chunks — drop any stale cache, no LLM call.
    * LLM error or empty output on a *signature-drifted* source: drop
      the stale cache so the Source-tab falls back to the heuristic
      excerpt instead of presenting prose summarising prior content.
      Without this clear, ``/api/sources`` would keep rendering the
      old AI summary for the new chunk set, which reads as a silent
      data-integrity bug to users.
    """
    if not config.auto_summarize or llm is None:
        return

    if not chunks:
        # Source emptied or became unchunkable. Engine's transactional
        # ``delete_chunks`` defers cache cleanup to this hook (so a
        # rewrite isn't spuriously cleared mid-transaction); a genuine
        # "now empty" reindex hits this branch and clears the row.
        if await storage.get_ai_summary(source_path) is not None:
            await storage.delete_ai_summary(source_path)
        return

    signature = compute_source_signature(chunks)
    cached = await storage.get_ai_summary(source_path)
    if cached and cached.get("signature") == signature:
        return

    body = _build_body(chunks, config.summary_max_input_chars)
    if not body.strip():
        # Same shape as zero chunks — content drifted into emptiness.
        # Cached prose (if any) describes content that no longer exists,
        # so drop it.
        if cached is not None:
            await storage.delete_ai_summary(source_path)
        return

    summary = await _generate(llm, body, config.summary_language, config.summary_max_tokens)
    if summary is None:
        # LLM error or empty output. ``cached`` here can only be either
        # ``None`` (no prior cache → nothing to do) or a record whose
        # signature mismatched the new chunks (we'd have early-returned
        # otherwise). The mismatched record is, by definition, prose
        # about the *prior* content — keeping it would let the Source
        # tab render an out-of-date summary against the new chunk set.
        # Clear it so the heuristic excerpt takes over until the next
        # successful refresh.
        if cached is not None:
            await storage.delete_ai_summary(source_path)
        return

    await storage.set_ai_summary(
        source_path,
        summary=summary,
        signature=signature,
        language=config.summary_language,
    )


async def regenerate_for_paths(
    storage: "SqliteBackend",
    llm: "LLMProvider | None",
    paths: Sequence[Path],
    chunks_for_path: Callable[[Path], Awaitable[Sequence["Chunk"]]],
    config: "IndexingConfig",
    progress: Callable[[int, int, int], None] | None = None,
) -> dict:
    """Force-regenerate AI summaries for the given paths in the current language.

    Used by the bulk language-drift regeneration endpoint. Bypasses the
    signature-skip in :func:`maybe_update_ai_summary` so existing entries
    are rewritten in ``config.summary_language`` even when content hasn't
    changed. ``chunks_for_path`` is injected by the caller (storage's
    :meth:`list_chunks_by_source` typically) so this module stays thin.

    The optional *progress* callback is fired after each path with
    ``(processed, total, failed)`` — the Web endpoint plumbs that into
    the polling status.

    Returns a counter dict ``{processed, skipped, failed}``. *skipped* is
    incremented when a path produces no chunks (e.g., the file was
    deleted between drift detection and regeneration).
    """
    total = len(paths)
    processed = 0
    skipped = 0
    failed = 0

    if llm is None or not config.auto_summarize:
        # The endpoint validates these before calling, but defend in
        # depth so a misuse can't silently "succeed" with zero work.
        return {"processed": 0, "skipped": total, "failed": 0}

    for path in paths:
        chunks = await chunks_for_path(path)
        if not chunks:
            skipped += 1
            if progress is not None:
                progress(processed, total, failed)
            continue

        body = _build_body(chunks, config.summary_max_input_chars)
        if not body.strip():
            skipped += 1
            if progress is not None:
                progress(processed, total, failed)
            continue

        summary = await _generate(llm, body, config.summary_language, config.summary_max_tokens)
        if summary is None:
            failed += 1
            if progress is not None:
                progress(processed, total, failed)
            continue

        signature = compute_source_signature(chunks)
        await storage.set_ai_summary(
            path,
            summary=summary,
            signature=signature,
            language=config.summary_language,
        )
        processed += 1
        if progress is not None:
            progress(processed, total, failed)

    return {"processed": processed, "skipped": skipped, "failed": failed}
