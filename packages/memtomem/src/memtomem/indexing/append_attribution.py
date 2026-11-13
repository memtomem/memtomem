"""Which chunks an append produced, judged from the text the indexer chunked (#3292).

A caller that appends an entry and re-indexes the file gets back
``IndexingStats.new_chunk_ids``: every chunk the whole-file re-index upserted.
That set cannot name the entry's own chunks. It holds chunks from pending edits
elsewhere in the file, and it misses the entry's chunk when the chunk matches an
existing one by content hash and keeps that chunk's id.

The answer has to come from the same text the indexer read and chunked, not
from a recomputation afterwards. The appending caller describes its write as an
:class:`AppendRegion` (the whole text the file should read as, and where its
own bytes begin). The engine compares that with what it read and, only when the
whole file is that text, maps the region to the chunks whose line spans it
touches.

Spans are the only position a chunk reliably carries through the pipeline, so
the answer is given only where a span says what a chunk holds: Markdown chunks
(the engine checks the chunker), no overlap anywhere in the file (overlap copies
text between neighbours without moving their spans), and a boundary on a line
start (a span cannot split a line). Everywhere else the append is
``unverified``.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal
from uuid import UUID

if TYPE_CHECKING:
    from memtomem.models import Chunk

Attribution = Literal["exact", "merged", "unverified"]


@dataclass(frozen=True, slots=True)
class AppendRegion:
    """An append, as the indexer should read the file after it.

    ``expected_text`` is the whole file in the indexer's view (UTF-8 with
    replacement, universal newlines). ``start`` is the offset in it where the
    appended text begins.
    """

    expected_text: str
    start: int


def text_after_append(pre_image: bytes, appended: str) -> str:
    """The text a file reads as after ``appended`` is written in text mode on ``pre_image``.

    The write applies the platform newline, and the indexer decodes with
    replacement and universal newlines; this composes the two.
    """
    from memtomem.indexing.redaction_exemption import indexer_text

    written = appended.replace("\n", os.linesep).encode("utf-8")
    return indexer_text(pre_image + written, errors="replace")


def append_region(pre_image: bytes, appended: str) -> AppendRegion | None:
    """The region ``appended`` occupies once written in text mode after ``pre_image``.

    ``pre_image`` must be the bytes the caller read under the file's lock right
    before appending. Decoding uses replacement, as the preflight and the
    indexer's fallback read do, so a file with invalid UTF-8 that the preflight
    let through cannot make this raise after the append is already durable.

    ``None`` when the pre-image's own text is not a prefix of the result, so
    the boundary is nowhere in the text. Universal newlines make that unlikely
    (a trailing lone ``\\r`` reads as ``\\n`` with or without what follows it),
    so this is a guard, not a known case.
    """
    from memtomem.indexing.redaction_exemption import indexer_text

    expected = text_after_append(pre_image, appended)
    prefix = indexer_text(pre_image, errors="replace")
    if not expected.startswith(prefix):
        return None
    return AppendRegion(expected_text=expected, start=len(prefix))


def attribute_append(
    content: str,
    region: AppendRegion,
    chunks: Sequence[Chunk],
    committed_ids: Collection[UUID],
) -> tuple[tuple[UUID, ...], Attribution]:
    """The chunks of ``chunks`` whose spans touch ``region``, and how cleanly.

    ``content`` is the text the indexer read and chunked, and ``chunks`` the
    chunks it produced from it, in file order, with the ids the diff assigned.
    ``committed_ids`` are the ids whose rows this run left in the store.

    The appended lines run from the first line holding non-whitespace text of
    the append to the last: a whitespace-only line before a heading belongs to
    the previous section's span, so counting it would touch that section.

    * ``exact``: every touching chunk's span starts within the appended lines.
      Such a chunk holds only appended text, though it can still take its
      heading context from before the append, when a split puts the appended
      lines of a continued section in a fragment of their own.
    * ``merged``: a touching chunk's span starts before them (a short entry
      merged into the previous section, an entry header that lands in the
      previous section's chunk, an entry that continues the section before it
      in the same chunk).
    * ``unverified``, with no ids: the file is not the expected text (another
      writer's bytes are in it), the region is out of range or blank, the
      append starts mid-line, any chunk carries overlap or has no valid line
      range, no chunk touches the region, or a touching chunk was not
      committed.
    """
    if content != region.expected_text or not 0 <= region.start <= len(content):
        return (), "unverified"
    tail = content[region.start :]
    if not tail.strip():
        return (), "unverified"
    text_at = region.start + len(tail) - len(tail.lstrip())
    first = content.rfind("\n", 0, text_at) + 1
    last = len(content.rstrip()) - 1
    # Spans are whole lines: an append whose first text line began before it
    # shares that line with the text before it.
    if first < region.start:
        return (), "unverified"
    # ``0..0`` is a chunk no span places, and an inverted span places nothing.
    # Overlap copies text between neighbours without moving their spans, so a
    # span no longer says what the chunk holds.
    if any(
        chunk.metadata.start_line < 1
        or chunk.metadata.end_line < chunk.metadata.start_line
        or chunk.metadata.overlap_before
        or chunk.metadata.overlap_after
        for chunk in chunks
    ):
        return (), "unverified"
    first_line = content.count("\n", 0, first) + 1
    last_line = content.count("\n", 0, last) + 1

    touching = [
        chunk
        for chunk in chunks
        if chunk.metadata.start_line <= last_line and chunk.metadata.end_line >= first_line
    ]
    if not touching:
        return (), "unverified"
    if any(chunk.id not in committed_ids for chunk in touching):
        return (), "unverified"
    ids = tuple(chunk.id for chunk in touching)
    exact = all(chunk.metadata.start_line >= first_line for chunk in touching)
    return ids, "exact" if exact else "merged"
