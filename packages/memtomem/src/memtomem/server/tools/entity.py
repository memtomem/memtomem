"""Tools: mem_entity_scan, mem_entity_search."""

from __future__ import annotations

import logging

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

logger = logging.getLogger(__name__)


@mcp.tool()
@tool_handler
@register("entity")
async def mem_entity_scan(
    namespace: str | None = None,
    source_filter: str | None = None,
    entity_types: list[str] | None = None,
    overwrite: bool = False,
    dry_run: bool = False,
    ctx: CtxType = None,
) -> str:
    """Scan indexed chunks and extract structured entities (people, dates, decisions, etc.).

    Entities are stored in a searchable index for later retrieval via mem_entity_search.

    Args:
        namespace: Only scan chunks in this namespace
        source_filter: Only scan chunks from matching source files (glob)
        entity_types: Entity types to extract (default: all). Options: person, date, decision, action_item, technology, concept
        overwrite: Replace existing entities for scanned chunks (default: false, skip already-scanned)
        dry_run: Preview extraction without saving (default: false)
    """
    from memtomem.tools.entity_extraction import _VALID_ENTITY_TYPES, extract_entities_with_llm

    # Validate before anything can write. An unknown type is silently ignored by
    # the extractor, so a typo makes every chunk look entity-less — and under
    # ``overwrite`` that is indistinguishable from "this content no longer has
    # entities", which clears the rows. A misspelling must not be able to erase
    # an extraction pass.
    if entity_types is not None:
        invalid = sorted(set(entity_types) - set(_VALID_ENTITY_TYPES))
        if invalid:
            return (
                f"Error: unknown entity type(s): {', '.join(invalid)}. "
                f"Valid types: {', '.join(sorted(_VALID_ENTITY_TYPES))}."
            )

    app = await _get_app_initialized(ctx)
    storage = app.storage

    # Get all source files
    sources = await storage.get_all_source_files()

    # Filter by namespace/source
    total_chunks = 0
    total_entities = 0
    scanned_sources = 0
    cleared_chunks = 0
    entity_type_counts: dict[str, int] = {}
    # Every ``upsert_entities`` / ``delete_entities_for_chunk`` below commits on
    # its own, so a failure part-way through still leaves ranking inputs changed.
    # Tracked here and flushed in ``finally`` so the search cache can never
    # outlive the rows it was computed from.
    mutated = False

    # Glob-only contract — see ``match_source_filter_glob`` for the
    # separator-fold rule (#720) and rationale for not sharing the
    # substring-aware ``match_source_filter``.
    from memtomem.search.pipeline import match_source_filter_glob

    try:
        for source in sources:
            if source_filter and not match_source_filter_glob(source_filter, str(source)):
                continue

            chunks = await storage.list_chunks_by_source(source)
            if namespace:
                chunks = [c for c in chunks if c.metadata.namespace == namespace]
            if not chunks:
                continue

            scanned_sources += 1

            # Pre-fetch already-extracted chunk IDs (single query instead of N).
            # Needed under ``overwrite`` too: it identifies the chunks whose old
            # rows an empty extraction has to clear.
            already_extracted = await storage.get_extracted_chunk_ids([str(c.id) for c in chunks])

            for chunk in chunks:
                if not overwrite and str(chunk.id) in already_extracted:
                    continue

                entities = await extract_entities_with_llm(
                    chunk.content, entity_types, app.llm_provider
                )
                if not entities:
                    # An overwrite pass that now extracts nothing must clear the
                    # chunk's old rows. Skipping the write would strand entities
                    # from a previous scan of different content, and stale rows
                    # boost the chunk for a query it no longer matches.
                    if overwrite and str(chunk.id) in already_extracted:
                        cleared_chunks += 1
                        if not dry_run:
                            await storage.delete_entities_for_chunk(str(chunk.id))
                            mutated = True
                    continue

                total_chunks += 1
                total_entities += len(entities)

                for e in entities:
                    entity_type_counts[e.entity_type] = entity_type_counts.get(e.entity_type, 0) + 1

                if not dry_run:
                    await storage.upsert_entities(
                        str(chunk.id),
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
                    mutated = True
    finally:
        # Entities are a ranking input once the Stage-7b boost is enabled, so
        # any committed write invalidates cached search results — same contract
        # as the other explicit bulk mutations (import, consolidate). In
        # ``finally`` because the writes above are already committed even when a
        # later source raises.
        if mutated:
            app.search_pipeline.invalidate_cache()

    # Format result
    lines = [
        f"Entity scan {'(dry run) ' if dry_run else ''}complete",
        f"- Sources scanned: {scanned_sources}",
        f"- Chunks with entities: {total_chunks}",
        f"- Total entities found: {total_entities}",
    ]
    if cleared_chunks:
        lines.append(
            f"- Chunks cleared (no entities on re-scan): {cleared_chunks}"
            + (" (dry run — not applied)" if dry_run else "")
        )
    if entity_type_counts:
        lines.append("- By type:")
        for etype, count in sorted(entity_type_counts.items(), key=lambda x: -x[1]):
            lines.append(f"    {etype}: {count}")

    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("entity")
async def mem_entity_search(
    entity_type: str | None = None,
    value: str | None = None,
    namespace: str | None = None,
    limit: int = 20,
    ctx: CtxType = None,
) -> str:
    """Search for chunks containing specific entities.

    Find chunks that mention a person, date, decision, action item, or technology.

    Args:
        entity_type: Filter by type (person, date, decision, action_item, technology, concept)
        value: Search for entities matching this value (substring match)
        namespace: Namespace scope
        limit: Maximum results (default 20)
    """
    if not 1 <= limit <= 500:
        return f"Error: limit must be between 1 and 500, got {limit}."

    app = await _get_app_initialized(ctx)
    results = await app.storage.search_entities(
        entity_type=entity_type,
        value=value,
        namespace=namespace,
        limit=limit,
    )

    if not results:
        parts = []
        if entity_type:
            parts.append(f"type={entity_type}")
        if value:
            parts.append(f"value='{value}'")
        return f"No entities found{' for ' + ', '.join(parts) if parts else ''}."

    lines = [f"Found {len(results)} entities:"]
    for r in results:
        ns_badge = f" [{r['namespace']}]" if r["namespace"] != "default" else ""
        lines.append(
            f"\n- **{r['entity_type']}**: {r['entity_value']} "
            f"(confidence={r['confidence']:.0%}){ns_badge}"
        )
        lines.append(f"  Source: {r['source_file']}")
        if r["content_preview"]:
            lines.append(f"  Context: {r['content_preview']}...")

    return "\n".join(lines)
