"""Batch export and import for indexed memory chunks.

Export serialises chunks (without embeddings) to a JSON bundle.
Import reads a bundle, re-embeds each chunk, and upserts to storage.

Bundle schema versions
----------------------
- ``version="1"`` (legacy): minimal fields — no content_hash, no chunk_id.
  Re-importing always created duplicate rows (fresh UUID each time) and
  cross-PC merges with identical content left duplicated rows.
- ``version="2"`` (current): each record additionally carries ``content_hash``
  (sha256 of NFC content) and ``chunk_id`` (original UUID). ``import_chunks``
  uses these for the ``on_conflict`` decision (``skip``/``update``/
  ``duplicate``) so repeated imports are idempotent by default.

v1 bundles still import. Missing ``content_hash`` is recomputed from content
on the fly (Chunk.__post_init__); missing ``chunk_id`` falls back to a fresh
UUID. ``on_conflict`` works either way — the hash comparison uses the
recomputed value.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from uuid import UUID, uuid4

from memtomem.models import Chunk, ChunkMetadata, ChunkType

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.storage.sqlite_backend import SqliteBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

_BUNDLE_VERSION = "2"

OnConflict = Literal["skip", "update", "duplicate"]


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
    skipped_duplicates: int = 0
    updated_chunks: int = 0


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


async def export_chunks(
    storage: SqliteBackend,
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
    source_files = await storage.get_all_source_files()

    records: list[dict] = []
    for source in sorted(source_files):
        if source_filter and source_filter not in str(source):
            continue
        chunks = await storage.list_chunks_by_source(source, limit=100_000)
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
        "chunk_id": str(chunk.id),
        "content": chunk.content,
        "content_hash": chunk.content_hash,
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
    storage: SqliteBackend,
    embedder: EmbeddingProvider,
    input_path: Path,
    namespace: str | None = None,
    on_conflict: OnConflict = "skip",
) -> ImportStats:
    """Import chunks from a JSON bundle file.

    Each surviving chunk is re-embedded and upserted.

    Args:
        storage: StorageBackend instance.
        embedder: EmbeddingProvider instance.
        input_path: Path to a JSON bundle produced by export_chunks().
        namespace: If given, overrides the namespace of every imported chunk.
        on_conflict: How to handle incoming chunks whose ``content_hash``
            already exists in storage.

            - ``"skip"`` *(default)*: drop the incoming chunk. Makes re-import
              of the same bundle idempotent. UUID from the bundle (v2) is
              preserved on inserted chunks.
            - ``"update"``: reuse the existing row's UUID so the upsert
              refreshes metadata/embedding for that content. Non-matching
              chunks insert with bundle UUID (v2) or a fresh UUID (v1).
            - ``"duplicate"``: v1 back-compat. Always assign a fresh UUID
              and insert, even if content_hash collides. Produces duplicate
              rows per identical content.

    Returns:
        ImportStats with per-record outcomes.
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

    skipped_malformed = 0
    parsed: list[Chunk] = []
    for record in bundle.chunks:
        try:
            chunk = _dict_to_chunk(
                record,
                namespace_override=namespace,
                preserve_uuid=(on_conflict != "duplicate"),
            )
            parsed.append(chunk)
        except Exception as exc:
            logger.warning("Skipping malformed record: %s", exc)
            skipped_malformed += 1

    if not parsed:
        return ImportStats(
            total_chunks=len(bundle.chunks),
            imported_chunks=0,
            skipped_chunks=skipped_malformed,
            failed_chunks=0,
        )

    skipped_duplicates = 0
    updated = 0
    batch: list[Chunk]

    if on_conflict == "duplicate":
        batch = parsed
    else:
        existing_hash_to_id = await storage.get_content_hash_to_id()
        batch = []
        seen_in_batch: set[str] = set()
        for chunk in parsed:
            h = chunk.content_hash
            if h in existing_hash_to_id:
                if on_conflict == "skip":
                    skipped_duplicates += 1
                    continue
                # on_conflict == "update": reuse existing UUID so upsert UPDATEs
                try:
                    chunk.id = UUID(existing_hash_to_id[h])
                except ValueError:
                    pass
                updated += 1
                batch.append(chunk)
            elif h in seen_in_batch:
                # Duplicate content within the bundle itself — first wins.
                skipped_duplicates += 1
            else:
                seen_in_batch.add(h)
                batch.append(chunk)

    imported = 0
    failed = 0

    if batch:
        contents = [c.content for c in batch]
        try:
            embeddings = await embedder.embed_texts(contents)
            for chunk, emb in zip(batch, embeddings):
                chunk.embedding = emb
        except Exception as exc:
            logger.error("Embedding failed during import: %s", exc)
            return ImportStats(
                total_chunks=len(bundle.chunks),
                imported_chunks=0,
                skipped_chunks=skipped_malformed,
                failed_chunks=len(batch),
                skipped_duplicates=skipped_duplicates,
                updated_chunks=0,
            )

        try:
            await storage.upsert_chunks(batch)
            imported = len(batch) - updated
        except Exception as exc:
            logger.error("Upsert failed during import: %s", exc)
            failed = len(batch)
            imported = 0
            updated = 0

    return ImportStats(
        total_chunks=len(bundle.chunks),
        imported_chunks=imported,
        skipped_chunks=skipped_malformed,
        failed_chunks=failed,
        skipped_duplicates=skipped_duplicates,
        updated_chunks=updated,
    )


def _dict_to_chunk(
    record: dict,
    namespace_override: str | None = None,
    *,
    preserve_uuid: bool = False,
) -> Chunk:
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

    chunk_id: UUID
    raw_id = record.get("chunk_id") if preserve_uuid else None
    if raw_id:
        try:
            chunk_id = UUID(str(raw_id))
        except (ValueError, TypeError):
            chunk_id = uuid4()
    else:
        chunk_id = uuid4()

    # content_hash is intentionally not propagated from the record — Chunk's
    # __post_init__ recomputes it from content, which prevents a tampered
    # bundle from smuggling a mismatched hash past the on_conflict check.
    return Chunk(
        content=record["content"],
        metadata=meta,
        id=chunk_id,
        created_at=created_at,
    )
