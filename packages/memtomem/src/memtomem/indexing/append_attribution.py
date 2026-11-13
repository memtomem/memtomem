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
whole file is that text, maps the region to the chunks whose source lines it
touches.
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

    ``None`` when the pre-image's own text is not a prefix of the result: a
    pre-image ending in a lone ``\\r`` folds with an appended ``\\n`` into one
    newline, and the boundary is then nowhere in the text.
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
    """The chunks of ``chunks`` that hold ``region``, and how cleanly they do.

    ``content`` is the text the indexer read and chunked, and ``chunks`` the
    chunks it produced from it, in file order, with the ids the diff assigned.
    ``committed_ids`` are the ids whose rows this run left in the store.

    * ``exact``: the appended text starts on a fresh line, and every chunk
      touching it lies within it.
    * ``merged``: chunks touch the appended text, but at least one also holds
      text from before it (a short entry merged into the previous section, an
      entry header that lands in the previous section's chunk, an append that
      starts mid-line).
    * ``unverified``, with no ids: the file is not the expected text (another
      writer's bytes are in it), the appended text is blank, no chunk touches
      it, a chunk's line range is unknown, or a touching chunk was not
      committed.
    """
    if content != region.expected_text:
        return (), "unverified"
    tail = content[region.start :]
    leading = len(tail) - len(tail.lstrip("\n"))
    first = region.start + leading
    last = len(content.rstrip("\n")) - 1
    if not tail.strip() or last < first:
        return (), "unverified"
    first_line = content.count("\n", 0, first) + 1
    last_line = content.count("\n", 0, last) + 1

    # A chunker that cannot place its chunks reports ``0..0``; such a chunk can
    # touch any line, so nothing about the file is attributable.
    if any(chunk.metadata.start_line < 1 for chunk in chunks):
        return (), "unverified"
    touching = [
        chunk
        for chunk in chunks
        if chunk.metadata.start_line <= last_line and chunk.metadata.end_line >= first_line
    ]
    if not touching:
        return (), "unverified"
    if any(chunk.id not in committed_ids for chunk in touching):
        return (), "unverified"

    fresh_line = first == 0 or content[first - 1] == "\n"
    within = all(
        chunk.metadata.start_line >= first_line and chunk.metadata.end_line <= last_line
        for chunk in touching
    )
    ids = tuple(chunk.id for chunk in touching)
    return ids, "exact" if fresh_line and within else "merged"
