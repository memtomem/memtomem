"""Tools: mem_entity_scan, mem_entity_search."""

from __future__ import annotations

import logging

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
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
    ctx: CtxType = None,  # type: ignore[assignment]
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
    from fnmatch import fnmatch

    from memtomem.tools.entity_extraction import extract_entities

    app = _get_app(ctx)
    storage = app.storage

    # Get all source files
    sources = await storage.get_all_source_files()

    # Filter by namespace/source
    total_chunks = 0
    total_entities = 0
    scanned_sources = 0
    entity_type_counts: dict[str, int] = {}

    for source in sources:
        if source_filter and not fnmatch(str(source), source_filter):
            continue

        chunks = await storage.list_chunks_by_source(source)
        if namespace:
            chunks = [c for c in chunks if c.metadata.namespace == namespace]
        if not chunks:
            continue

        scanned_sources += 1

        for chunk in chunks:
            if not overwrite:
                existing = await storage.get_entities_for_chunk(str(chunk.id))
                if existing:
                    continue

            entities = extract_entities(chunk.content, entity_types)
            if not entities:
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

    # Format result
    lines = [
        f"Entity scan {'(dry run) ' if dry_run else ''}complete",
        f"- Sources scanned: {scanned_sources}",
        f"- Chunks with entities: {total_chunks}",
        f"- Total entities found: {total_entities}",
    ]
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
    ctx: CtxType = None,  # type: ignore[assignment]
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
        return "Error: limit must be between 1 and 500."

    app = _get_app(ctx)
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
