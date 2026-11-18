"""Which chunk an appending tool's entry became (#3292, #3315).

A tool that appends one entry and then wants to point at it (an edge, a link)
reads ``IndexingStats.appended_chunk_ids``, not ``new_chunk_ids``: the latter
covers the whole file's re-index, so it holds a pending edit's chunk and
misses an entry whose chunk kept an existing id.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from memtomem.server.tools._id_access import resolve_chunk

if TYPE_CHECKING:
    from uuid import UUID

    from memtomem.models import IndexingStats
    from memtomem.server.context import AppContext


async def own_appended_chunk_id(
    app: AppContext,
    stats: IndexingStats,
    appended: list[str],
    *,
    allow_merged: bool,
) -> UUID | None:
    """The id of the one chunk this call's entry became, or ``None`` when unsure.

    ``appended`` is what ``_mem_add_core``'s ``on_appended`` collected. This
    answers only when:

    * the call appended one block and the indexer attributes exactly one
      chunk to it;
    * the attribution is ``exact``, or ``merged`` when ``allow_merged`` (a
      short entry folded into the section before it, so the chunk also holds
      earlier text);
    * the chunk is inside the caller's boundary (ADR-0036); and
    * the block, projected on its own the way the re-index chunks a file
      (same chunker, merging and bounding), is one non-empty chunk whose
      content that chunk contains.

    Anything else (an entry split into several chunks, a file another writer
    changed between the append and the re-index) is ``None``.
    """
    from memtomem.indexing.privacy_gate import _read_text_view

    if len(appended) != 1 or len(stats.appended_chunk_ids) != 1:
        return None
    if stats.appended_attribution != "exact" and not (
        allow_merged and stats.appended_attribution == "merged"
    ):
        return None
    chunk = await resolve_chunk(app, stats.appended_chunk_ids[0])
    if chunk is None:
        return None
    # The re-index reads the file with universal newlines; project what it read.
    projected = app.index_engine._chunk_projected_content(
        chunk.metadata.source_file, _read_text_view(appended[0])
    )
    if len(projected) != 1 or not projected[0].content.strip():
        return None
    if projected[0].content not in chunk.content:
        return None
    return chunk.id
