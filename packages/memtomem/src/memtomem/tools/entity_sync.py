"""Write ``chunk_entities`` alongside a chunk write (#2145, #2155).

The indexing engine was the first caller (#2145); this module exists because it
is not the only writer. Anything that stores *new or changed chunk content* owes
the same entity write, and the two that reached ``storage.upsert_chunks``
directly — importing a bundle and creating a consolidation summary — were
writing content the extractor never saw.

Both are coverage gaps rather than corruption: import's ``on_conflict="update"``
path matches on ``content_hash``, so the row it reuses holds the content it
already had. That is also why import syncs only the chunks it genuinely adds —
rewriting a matched row's entities would delete whatever was there, including a
richer ``mem_entity_scan`` LLM pass, and replace it with the regex result.

Writers that only rewrite *metadata* (tags, namespace) on unchanged content have
no business here — their entities are still accurate, and re-extracting would be
wasted work. ``tests/test_entity_write_coverage.py`` is the guard that makes
every ``upsert_chunks`` caller state which of the two it is.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Any

from memtomem.tools.entity_extraction import extract_entities

if TYPE_CHECKING:
    from memtomem.models import Chunk

# Every entity-writer method this module calls. Probed together: the storage ABC
# declares only the read half of the entity surface (see ``storage/base.py``) and
# the rest is reached by duck typing, so probing one name and calling three would
# turn a partial backend's clean degrade into an ``AttributeError`` halfway
# through a chunk write.
_REQUIRED_STORAGE_METHODS = (
    "upsert_entities",
    "delete_entities_for_chunk",
    "filter_persisted_chunk_ids",
)


async def sync_entities_for_chunks(
    storage: Any,
    chunks: Sequence[Chunk],
    *,
    enabled: bool = True,
) -> int:
    """Rewrite ``chunk_entities`` for chunks whose content was just written.

    Returns the number of entity rows written.

    Call this *after* the ``upsert_chunks`` that stored ``chunks``, and prefer
    calling it inside the same transaction where the caller has one:
    ``upsert_entities`` composes under an open transaction, so entities and their
    chunk then commit together or not at all.

    Pass only chunks whose content is new or changed. A chunk whose stored
    content did not move keeps accurate entities, so re-extracting it costs work
    and buys nothing — which is why the engine passes ``diff_result.to_upsert``
    and neither the ``unchanged`` nor the ``metadata_only`` bucket.

    A chunk that now extracts to nothing has its rows deleted rather than left
    alone: the content that produced them is gone, and a stale row boosts the
    chunk for a query it no longer matches. ``upsert_entities`` early-returns on
    an empty list, so the delete has to be explicit — the same asymmetry
    ``mem_entity_scan`` handles under ``overwrite``.

    Extraction is the *regex* path (stdlib ``re``, no model load, no I/O), never
    ``extract_entities_with_llm``: this runs on every chunk write, and the LLM
    extractor stays the opt-in quality upgrade reached through
    ``mem_entity_scan(overwrite=True)``.

    Args:
        storage: Storage backend. One without the entity-writer surface degrades
            to writing no entities rather than raising.
        chunks: The chunks just written, with content in hand.
        enabled: ``False`` returns 0 without touching the database — used to
            honour ``indexing.extract_entities``.
    """
    if not enabled or not chunks:
        return 0

    if not all(hasattr(storage, name) for name in _REQUIRED_STORAGE_METHODS):
        return 0

    # ``upsert_chunks`` inserts ``OR IGNORE``, so a chunk handed to this call may
    # have lost the #691 uniqueness race and have no row — writing its entities
    # would trip the foreign key and roll back the chunk write that did succeed.
    # The winning process writes that content's entities under the surviving id.
    persisted = await storage.filter_persisted_chunk_ids([str(c.id) for c in chunks])

    written = 0
    for chunk in chunks:
        chunk_id = str(chunk.id)
        if chunk_id not in persisted:
            continue
        entities = extract_entities(chunk.content)
        if not entities:
            await storage.delete_entities_for_chunk(chunk_id)
            continue
        written += await storage.upsert_entities(
            chunk_id,
            [
                {
                    "entity_type": e.entity_type,
                    "entity_value": e.entity_value,
                    "confidence": e.confidence,
                    "position": e.position,
                }
                for e in entities
            ],
        )
    return written
