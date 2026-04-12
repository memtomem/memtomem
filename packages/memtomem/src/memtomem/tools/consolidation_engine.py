"""Consolidation engine — heuristic summary generation + out-of-ctx apply.

This module is callable from both MCP tool context (``mem_consolidate_apply``)
and the policy engine (``execute_auto_consolidate``) because it only depends
on the storage layer — no ``AppContext`` required.

The summary is deterministic and chunk-type aware. Keyword-boosted regex
(reused from ``entity_extraction``) picks decision/action lines over a plain
first-sentence fallback, and checklist chunks are rendered as item counts
rather than truncated prose. See ``docs/guides/user-guide.md`` "Consolidation"
section and ``feedback_compression_priority.md`` for the rationale: nothing
is lost, originals kept by default, source hash embedded for idempotent
re-runs.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from memtomem.models import Chunk, ChunkMetadata, ChunkType
from memtomem.tools.entity_extraction import _ACTION_RE, _DECISION_RE

if TYPE_CHECKING:
    from memtomem.llm.base import LLMProvider
    from memtomem.storage.base import StorageBackend

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────

DEFAULT_SUMMARY_NAMESPACE = "archive:summary"
DECAY_FLOOR = 0.3  # keep_originals=False → importance_score floor (never below)
CONSOLIDATED_SUFFIX = ".consolidated.md"
_SOURCE_HASH_RE = re.compile(r"Source hash:\s*`([a-f0-9]+)`")
_CHECKLIST_ITEM_RE = re.compile(r"^\s*-\s*\[\s*[ xX]?\s*\]\s+(.+)$", re.MULTILINE)


# ── Bullet extraction ────────────────────────────────────────────────


def _first_sentence(text: str, max_len: int = 160) -> str:
    """Return the first sentence of ``text``, capped to ``max_len`` chars.

    Splits on ``. ? !`` followed by whitespace, or a blank line. Leading
    markdown list/heading markers are stripped so the caller doesn't get a
    doubly-prefixed bullet like ``- - [ ] item``.
    """
    cleaned = text.strip().lstrip("-*#\t ").strip()
    if not cleaned:
        return ""
    parts = re.split(r"(?<=[.?!])\s+|\n\n", cleaned, maxsplit=1)
    return parts[0][:max_len].strip()


def extract_bullet(chunk: Chunk) -> str:
    """Return a single markdown bullet summarizing one chunk.

    Extraction priority (first match wins):

    1. **Label**: deepest ``heading_hierarchy`` entry, or the first ``#``-line
       of content if no hierarchy.
    2. **Keyword boost**: a "Decision: …" or "Action: …" line anywhere in
       the body (regex reused from ``entity_extraction``). Wins over
       first-sentence fallback because it usually carries the load.
    3. **Checklist**: if two or more ``- [ ]`` / ``- [x]`` items exist, render
       as ``N items (first, second…)`` instead of cutting the first line.
    4. **First sentence**: as a last resort.

    Output shape: ``**{label}** — {sentence}``, ``**{label}**``,
    ``{sentence}``, or ``{first_120_chars}`` — whichever is populated.
    """
    h = chunk.metadata.heading_hierarchy
    label: str | None = h[-1] if h else None
    body = chunk.content.strip()

    # If no heading_hierarchy but content starts with a heading, use that.
    if label is None:
        first_line = body.split("\n", 1)[0].strip()
        if first_line.startswith("#"):
            label = first_line.lstrip("#").strip() or None
            body = body.split("\n", 1)[1].strip() if "\n" in body else ""

    # 1. Keyword boost — decision wins over action wins over fallback.
    boosted: str | None = None
    dec = _DECISION_RE.search(body)
    if dec:
        boosted = f"Decision: {dec.group(1).strip()[:140]}"
    else:
        act = _ACTION_RE.search(body)
        if act:
            # _ACTION_RE has 3 capture groups (TODO:, -[ ], Action item:)
            captured = (act.group(1) or act.group(2) or act.group(3) or "").strip()
            if captured:
                boosted = f"Action: {captured[:140]}"

    # 2. Checklist fallback — only if no keyword boost landed.
    if boosted is None:
        items = _CHECKLIST_ITEM_RE.findall(body)
        if len(items) >= 2:
            previews = ", ".join(i.strip()[:60] for i in items[:2])
            tail = "…" if len(items) > 2 else ""
            boosted = f"{len(items)} items ({previews}{tail})"

    sentence = boosted or _first_sentence(body, max_len=160)

    if label and sentence:
        return f"**{label}** — {sentence}"
    if label:
        return f"**{label}**"
    if sentence:
        return sentence
    return body[:120].replace("\n", " ") or "(empty chunk)"


# ── Source hash for idempotency ──────────────────────────────────────


def compute_source_hash(chunk_ids: list[UUID] | list[str]) -> str:
    """Return a stable 16-char SHA256 hash of a sorted chunk id list.

    Sorting ensures the hash is order-independent so that two runs that
    happen to receive chunks in different order still collide. 16 chars
    (64 bits) is more than enough to distinguish incremental edits — we're
    not defending against adversaries, just against stale re-runs.
    """
    ids_sorted = sorted(str(cid) for cid in chunk_ids)
    joined = ",".join(ids_sorted).encode()
    return hashlib.sha256(joined).hexdigest()[:16]


def parse_source_hash(summary_content: str) -> str | None:
    """Extract the ``Source hash`` value from a previously generated summary.

    Returns ``None`` if the field is missing (e.g. hand-edited summary, or
    a summary from before this feature). A missing hash is treated as stale
    by the caller — the summary is regenerated.
    """
    m = _SOURCE_HASH_RE.search(summary_content)
    return m.group(1) if m else None


# ── Summary template ─────────────────────────────────────────────────


def make_heuristic_summary(
    chunks: list[Chunk],
    source: Path,
    max_bullets: int = 20,
) -> str:
    """Build a deterministic markdown summary for a group of chunks.

    Bullet count is capped at ``max_bullets``; remaining chunks are counted
    in an ellipsis line so the summary still faithfully reports group size.
    The ``Source hash`` line is a load-bearing idempotency marker — don't
    remove or move it without updating ``parse_source_hash``.
    """
    if not chunks:
        raise ValueError("make_heuristic_summary: cannot summarize empty chunk list")

    bullets = [extract_bullet(c) for c in chunks[:max_bullets]]
    extra = max(0, len(chunks) - max_bullets)

    # Temporal range — text-only. See feedback_compression_priority.md for why
    # we don't add a typed ChunkMetadata field here.
    created_ats = [c.created_at for c in chunks]
    range_start = min(created_ats).date().isoformat()
    range_end = max(created_ats).date().isoformat()

    source_hash = compute_source_hash([c.id for c in chunks])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines: list[str] = [
        f"# Consolidated: {source.name}",
        "",
        f"Auto-generated consolidation of {len(chunks)} chunks from `{source}`.",
        "",
        "## Contents",
        "",
    ]
    lines.extend(f"- {b}" for b in bullets)
    if extra > 0:
        lines.append(f"- … and {extra} more")
    lines.extend(
        [
            "",
            "## Metadata",
            "",
            f"- Source: `{source}`",
            f"- Chunks: {len(chunks)}",
            f"- Range: {range_start} ~ {range_end}",
            f"- Source hash: `{source_hash}`",
            f"- Generated: {now}",
            "- Strategy: heuristic",
        ]
    )
    return "\n".join(lines)


_CONSOLIDATION_SYSTEM_PROMPT = (
    "You are a memory consolidation assistant. Given a set of memory chunks "
    "from the same source file, produce a concise summary that preserves all "
    "key facts, decisions, and action items. Output markdown. Do NOT invent "
    "information not present in the input."
)


async def make_llm_summary(
    chunks: list[Chunk],
    source: Path,
    llm: LLMProvider,
    max_bullets: int = 20,
    max_tokens: int = 1024,
) -> str:
    """Build an LLM-generated markdown summary for a group of chunks.

    Wraps the LLM output in the same template as ``make_heuristic_summary``
    so that idempotency (source hash) and metadata work identically.
    """
    if not chunks:
        raise ValueError("make_llm_summary: cannot summarize empty chunk list")

    # Build prompt with chunk content
    chunk_texts = []
    for i, c in enumerate(chunks[:max_bullets], 1):
        chunk_texts.append(f"--- Chunk {i} ---\n{c.content[:2000]}")
    prompt = (
        f"Summarize the following {len(chunks)} memory chunks from `{source}`.\n\n"
        + "\n\n".join(chunk_texts)
    )

    llm_output = await llm.generate(
        prompt, system=_CONSOLIDATION_SYSTEM_PROMPT, max_tokens=max_tokens
    )

    # Wrap in same template as heuristic — same metadata block for idempotency.
    created_ats = [c.created_at for c in chunks]
    range_start = min(created_ats).date().isoformat()
    range_end = max(created_ats).date().isoformat()
    source_hash = compute_source_hash([c.id for c in chunks])
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    lines: list[str] = [
        f"# Consolidated: {source.name}",
        "",
        llm_output.strip(),
        "",
        "## Metadata",
        "",
        f"- Source: `{source}`",
        f"- Chunks: {len(chunks)}",
        f"- Range: {range_start} ~ {range_end}",
        f"- Source hash: `{source_hash}`",
        f"- Generated: {now}",
        "- Strategy: llm",
    ]
    return "\n".join(lines)


# ── Apply (storage mutations) ────────────────────────────────────────


async def link_consolidation_relations(
    storage: StorageBackend,
    source_ids: list[str],
    summary_id: UUID,
) -> int:
    """Link original chunks to the summary via ``consolidated_into`` edges.

    Shared by both ``apply_consolidation`` (policy-driven path) and
    ``mem_consolidate_apply`` (agent-driven path): they diverge in how they
    create the summary chunk (virtual upsert vs. file-first via ``mem_add``)
    but converge here to establish the relation graph.

    Invalid UUIDs are logged at DEBUG (harmless — they can appear if scratch
    state leaks stale data). Storage-level exceptions are logged at WARNING
    and skipped so a single bad row can't tank the whole group.
    """
    linked = 0
    for cid in source_ids:
        try:
            await storage.add_relation(UUID(cid), summary_id, "consolidated_into")
            linked += 1
        except (ValueError, TypeError):
            logger.debug("consolidation: skipping invalid UUID %r", cid)
        except Exception:
            logger.warning("consolidation: failed to link %r", cid, exc_info=True)
    return linked


async def source_has_consolidation_relations(
    storage: StorageBackend,
    chunk_ids: list[UUID],
) -> bool:
    """Return True if any chunk in the group already has a ``consolidated_into``
    edge — the idempotency fallback used by ``execute_auto_consolidate``.

    Used specifically for the "has this source been consolidated by anyone?"
    question when the policy handler's own virtual summary is absent (e.g.
    the agent ran ``mem_consolidate_apply`` for this source already). The
    ``any`` threshold is intentional: even a partial consolidation from the
    agent's end is enough to defer to their work rather than overwriting it
    with a heuristic summary. Source-hash staleness on the policy-owned
    virtual summary is a separate signal and takes priority when the virtual
    summary exists.
    """
    for cid in chunk_ids:
        try:
            related = await storage.get_related(cid)
        except Exception:
            logger.debug("get_related failed for %r", cid, exc_info=True)
            continue
        if any(rel == "consolidated_into" for _, rel in related):
            return True
    return False


def _make_summary_chunk(
    group: dict,
    summary: str,
    summary_namespace: str,
) -> Chunk:
    """Build the summary ``Chunk`` for ``apply_consolidation``.

    The virtual ``source_file`` is ``{original}.consolidated.md`` so the
    summary is reachable via ``list_chunks_by_source`` on the derived path
    for idempotency checks, without ever touching the filesystem.
    """
    source = Path(group["source"])
    source_name = source.name
    virtual_path = source.parent / f"{source_name}{CONSOLIDATED_SUFFIX}"
    return Chunk(
        content=summary,
        metadata=ChunkMetadata(
            source_file=virtual_path,
            chunk_type=ChunkType.MARKDOWN_SECTION,
            tags=("consolidated", "summary", "heuristic"),
            namespace=summary_namespace,
            heading_hierarchy=(f"Consolidated: {source_name}",),
        ),
    )


async def apply_consolidation(
    storage: StorageBackend,
    group: dict,
    summary: str,
    keep_originals: bool = True,
    summary_namespace: str = DEFAULT_SUMMARY_NAMESPACE,
) -> UUID:
    """Create a virtual summary chunk for ``group`` and link originals to it.

    Used by ``execute_auto_consolidate`` (policy-driven, unattended). The
    agent-driven ``mem_consolidate_apply`` deliberately does NOT go through
    here: it keeps the file-first flow via ``mem_add`` so the user's
    ``memory_dirs`` reflect their own consolidation work in git / rsync /
    plain file browsing. The two paths converge only in
    ``link_consolidation_relations``.

    The summary lives as a virtual chunk at ``<source>.consolidated.md``
    under ``summary_namespace`` — nothing is written to disk — which gives
    ``execute_auto_consolidate`` a stable lookup key for source-hash-based
    idempotency and staleness regeneration.

    Args:
        storage: Storage backend implementing ``upsert_chunks``,
            ``add_relation``, ``get_importance_scores``,
            ``update_importance_scores``.
        group: Dict with at minimum ``source`` (str path) and ``chunk_ids``
            (list of UUID strings). ``namespace`` / ``chunk_count`` are
            accepted but not required.
        summary: The markdown summary text. The policy handler supplies
            ``make_heuristic_summary`` output; custom callers may pass any
            string with the expected Metadata block format.
        keep_originals: If ``False``, apply a soft decay to originals by
            halving their importance score with a ``DECAY_FLOOR`` floor of
            0.3 so already-low chunks don't get evicted instantly. Never a
            hard delete.
        summary_namespace: Namespace for the new summary chunk. Default
            ``archive:summary``.

    Returns:
        The UUID of the newly created summary chunk.

    Raises:
        StorageError: if the summary upsert fails (caller should decide
            whether to continue to the next group or abort).
    """
    summary_chunk = _make_summary_chunk(group, summary, summary_namespace)
    await storage.upsert_chunks([summary_chunk])

    source_ids = [str(cid) for cid in group.get("chunk_ids", [])]
    await link_consolidation_relations(storage, source_ids, summary_chunk.id)

    if not keep_originals and source_ids:
        scores = await storage.get_importance_scores(source_ids)
        if scores:
            floored = {cid: max(score * 0.5, DECAY_FLOOR) for cid, score in scores.items()}
            await storage.update_importance_scores(floored)

    return summary_chunk.id
