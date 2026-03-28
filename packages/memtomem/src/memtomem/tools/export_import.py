"""Batch export and import for indexed memory chunks.

Export serialises chunks (without embeddings) to a JSON bundle.
Import reads a bundle, re-embeds each chunk, and upserts to storage.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from memtomem.models import Chunk, ChunkMetadata, ChunkType

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

_BUNDLE_VERSION = "1"


@dataclass
class ExportBundle:
    """JSON-serialisable container for exported chunks."""

    version: str = _BUNDLE_VERSION
    exported_at: str = ""
    total_chunks: int = 0
    chunks: list[dict] = field(default_factory=list)

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, indent=indent)

    @classmethod
    def from_json(cls, text: str) -> ExportBundle:
        data = json.loads(text)
        return cls(
            version=data.get("version", _BUNDLE_VERSION),
            exported_at=data.get("exported_at", ""),
            total_chunks=data.get("total_chunks", 0),
            chunks=data.get("chunks", []),
        )


@dataclass
class ImportStats:
    total_chunks: int
    imported_chunks: int
    skipped_chunks: int
    failed_chunks: int


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def export_chunks(
    storage: object,
    output_path: Path | None = None,
    source_filter: str | None = None,
    tag_filter: str | None = None,
    since: datetime | None = None,
    namespace_filter: str | None = None,
) -> ExportBundle:
    """Export indexed chunks to an ExportBundle (and optionally to a JSON file).

    Args:
        storage: StorageBackend instance.
        output_path: If given, write JSON to this path.
        source_filter: Only include chunks whose source_file contains this substring.
        tag_filter: Only include chunks that have this exact tag.
        since: Only include chunks created at or after this datetime.
    Returns:
        ExportBundle with the selected chunks.
    """
    source_files = await storage.get_all_source_files()  # type: ignore[union-attr]

    records: list[dict] = []
    for source in sorted(source_files):
        if source_filter and source_filter not in str(source):
            continue
        chunks = await storage.list_chunks_by_source(source, limit=100_000)  # type: ignore[union-attr]
        for chunk in chunks:
            if tag_filter and tag_filter not in chunk.metadata.tags:
                continue
            if since and chunk.created_at < since:
                continue
            if namespace_filter and chunk.metadata.namespace != namespace_filter:
                continue
            records.append(_chunk_to_dict(chunk))

    bundle = ExportBundle(
        exported_at=datetime.now(timezone.utc).isoformat(),
        total_chunks=len(records),
        chunks=records,
    )

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(bundle.to_json(), encoding="utf-8")
        logger.info("Exported %d chunks -> %s", len(records), output_path)

    return bundle


def _chunk_to_dict(chunk: Chunk) -> dict:
    meta = chunk.metadata
    return {
        "content": chunk.content,
        "source_file": str(meta.source_file),
        "heading_hierarchy": list(meta.heading_hierarchy),
        "chunk_type": meta.chunk_type.value,
        "start_line": meta.start_line,
        "end_line": meta.end_line,
        "language": meta.language,
        "tags": list(meta.tags),
        "namespace": meta.namespace,
        "created_at": chunk.created_at.isoformat(),
    }


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------


async def import_chunks(
    storage: object,
    embedder: object,
    input_path: Path,
    namespace: str | None = None,
) -> ImportStats:
    """Import chunks from a JSON bundle file.

    Each chunk is re-embedded and upserted with a fresh UUID.
    Chunks whose source file no longer exists on disk are imported as-is
    (path is preserved in metadata for traceability).

    Args:
        storage: StorageBackend instance.
        embedder: EmbeddingProvider instance.
        input_path: Path to a JSON bundle produced by export_chunks().
    Returns:
        ImportStats with counts of imported / skipped / failed chunks.
    """
    _MAX_IMPORT_BYTES = 100 * 1024 * 1024  # 100 MB
    file_size = input_path.stat().st_size
    if file_size > _MAX_IMPORT_BYTES:
        raise ValueError(
            f"Import file too large ({file_size:,} bytes). "
            f"Maximum allowed is {_MAX_IMPORT_BYTES:,} bytes (100 MB)."
        )

    text = input_path.read_text(encoding="utf-8")
    bundle = ExportBundle.from_json(text)

    if not bundle.chunks:
        return ImportStats(0, 0, 0, 0)

    imported = skipped = failed = 0
    batch: list[Chunk] = []

    for record in bundle.chunks:
        try:
            chunk = _dict_to_chunk(record, namespace_override=namespace)
            batch.append(chunk)
        except Exception as exc:
            logger.warning("Skipping malformed record: %s", exc)
            skipped += 1

    if batch:
        # Embed in one shot for efficiency
        contents = [c.content for c in batch]
        try:
            embeddings = await embedder.embed_texts(contents)  # type: ignore[union-attr]
            for chunk, emb in zip(batch, embeddings):
                chunk.embedding = emb
        except Exception as exc:
            logger.error("Embedding failed during import: %s", exc)
            return ImportStats(
                total_chunks=len(bundle.chunks),
                imported_chunks=0,
                skipped_chunks=skipped,
                failed_chunks=len(batch),
            )

        try:
            await storage.upsert_chunks(batch)  # type: ignore[union-attr]
            imported = len(batch)
        except Exception as exc:
            logger.error("Upsert failed during import: %s", exc)
            failed = len(batch)

    return ImportStats(
        total_chunks=len(bundle.chunks),
        imported_chunks=imported,
        skipped_chunks=skipped,
        failed_chunks=failed,
    )


def _dict_to_chunk(record: dict, namespace_override: str | None = None) -> Chunk:
    ns = namespace_override or record.get("namespace", "default")
    meta = ChunkMetadata(
        source_file=Path(record["source_file"]),
        heading_hierarchy=tuple(record.get("heading_hierarchy", [])),
        chunk_type=ChunkType(record.get("chunk_type", "raw_text")),
        start_line=int(record.get("start_line", 0)),
        end_line=int(record.get("end_line", 0)),
        language=record.get("language", "en"),
        tags=tuple(record.get("tags", [])),
        namespace=ns,
    )
    created_at = (
        datetime.fromisoformat(record["created_at"])
        if "created_at" in record
        else datetime.now(timezone.utc)
    )
    return Chunk(
        content=record["content"],
        metadata=meta,
        id=uuid4(),  # fresh ID -- avoid collision with existing chunks
        created_at=created_at,
    )
