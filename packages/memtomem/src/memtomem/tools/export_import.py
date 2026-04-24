"""Batch export and import for indexed memory chunks.

Export serialises chunks (without embeddings) to a JSON bundle.
Import reads a bundle, re-embeds each chunk, and upserts to storage.

Bundle schema v2 (current):
  * Records carry ``chunk_id`` and ``content_hash`` for cross-instance
    roundtrip fidelity and hash-based dedup.
  * Import supports ``on_conflict`` in {"skip", "update", "duplicate"} to
    resolve hash collisions against the target DB.
  * v1 bundles (no ``chunk_id`` / ``content_hash`` fields per record) are
    still accepted; missing fields are derived on import.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args
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
# Derived from the Literal so the type and the runtime validator cannot drift.
_VALID_ON_CONFLICT: frozenset[str] = frozenset(get_args(OnConflict))


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
    # New in v2 — zero for v1-shaped imports so back-compat callers still work.
    conflict_skipped_chunks: int = 0
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
        # v2 additions: chunk_id + content_hash survive the roundtrip so
        # importers can dedup / preserve identity across instances.
        "chunk_id": str(chunk.id),
        "content_hash": chunk.content_hash,
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
    storage: SqliteBackend,
    embedder: EmbeddingProvider,
    input_path: Path,
    namespace: str | None = None,
    on_conflict: OnConflict = "skip",
    preserve_ids: bool = False,
) -> ImportStats:
    """Import chunks from a JSON bundle file.

    Each chunk is re-embedded and upserted. Conflict resolution against the
    target DB's existing ``content_hash`` set is controlled by ``on_conflict``:

      * ``"skip"`` (default, idempotent): records whose content already
        exists in the DB are dropped. Re-importing the same bundle is a
        no-op; merging bundles with overlap adds only the unique side.
      * ``"update"``: records matching an existing hash overwrite that
        existing row's metadata (tags, namespace, heading hierarchy,
        source_file, created_at). The existing UUID is preserved.
      * ``"duplicate"``: no hash check — every record is inserted with a
        fresh UUID. This is the pre-v2 behaviour and produces row-level
        duplicates when re-importing or merging overlapping bundles.

    For non-conflicting records, UUID assignment is controlled by
    ``preserve_ids``: when True *and* the bundle is v2 (carries
    ``chunk_id``) *and* that UUID is not already claimed by a different
    chunk in the DB, the bundle's UUID is preserved. Otherwise a fresh
    UUID is assigned. In ``duplicate`` mode the flag is ignored — fresh
    UUIDs always.

    Args:
        storage: StorageBackend instance.
        embedder: EmbeddingProvider instance.
        input_path: Path to a JSON bundle produced by export_chunks().
        namespace: Override the namespace for all imported chunks.
        on_conflict: Strategy for hash collisions. See above.
        preserve_ids: Opt-in UUID preservation for new inserts (v2 bundles).
    Returns:
        ImportStats with total / imported / skipped / failed / conflict_skipped
        / updated counts.
    """
    if on_conflict not in _VALID_ON_CONFLICT:
        raise ValueError(
            f"on_conflict must be one of {sorted(_VALID_ON_CONFLICT)}, got {on_conflict!r}"
        )

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

    skipped = 0
    parsed: list[tuple[Chunk, str | None]] = []  # (chunk, bundle_chunk_id_or_None)

    for record in bundle.chunks:
        try:
            chunk, bundle_chunk_id = _dict_to_chunk(record, namespace_override=namespace)
            parsed.append((chunk, bundle_chunk_id))
        except Exception as exc:
            logger.warning("Skipping malformed record: %s", exc)
            skipped += 1

    if not parsed:
        return ImportStats(
            total_chunks=len(bundle.chunks),
            imported_chunks=0,
            skipped_chunks=skipped,
            failed_chunks=0,
        )

    conflict_skipped = 0
    updated = 0

    if on_conflict == "duplicate":
        # Back-compat path: every record gets a fresh UUID, no hash check.
        to_upsert = [c for c, _ in parsed]
    else:
        all_hashes = [c.content_hash for c, _ in parsed]
        existing = await storage.get_chunk_ids_by_hashes(all_hashes)

        to_upsert = []
        for chunk, bundle_chunk_id in parsed:
            existing_id = existing.get(chunk.content_hash)
            if existing_id is not None:
                if on_conflict == "skip":
                    conflict_skipped += 1
                    continue
                # on_conflict == "update": reuse the existing row's UUID so
                # upsert_chunks hits the UPDATE branch, preserving identity.
                chunk.id = existing_id
                updated += 1
                to_upsert.append(chunk)
            else:
                if preserve_ids and bundle_chunk_id:
                    try:
                        candidate = UUID(bundle_chunk_id)
                    except ValueError:
                        candidate = uuid4()
                    # Avoid stomping an unrelated existing row that happens
                    # to share this UUID (different content).
                    clash = await storage.get_chunks_batch([candidate])
                    if candidate in clash:
                        candidate = uuid4()
                    chunk.id = candidate
                to_upsert.append(chunk)

    imported = failed = 0
    if to_upsert:
        contents = [c.content for c in to_upsert]
        try:
            embeddings = await embedder.embed_texts(contents)
            for chunk, emb in zip(to_upsert, embeddings):
                chunk.embedding = emb
        except Exception as exc:
            logger.error("Embedding failed during import: %s", exc)
            return ImportStats(
                total_chunks=len(bundle.chunks),
                imported_chunks=0,
                skipped_chunks=skipped,
                failed_chunks=len(to_upsert),
                conflict_skipped_chunks=conflict_skipped,
                updated_chunks=0,
            )

        try:
            await storage.upsert_chunks(to_upsert)
            # "imported" counts only genuinely new rows; updates are tracked
            # separately so callers can distinguish merge from overwrite.
            imported = len(to_upsert) - updated
        except Exception as exc:
            logger.error("Upsert failed during import: %s", exc)
            failed = len(to_upsert)
            imported = 0
            updated = 0

    return ImportStats(
        total_chunks=len(bundle.chunks),
        imported_chunks=imported,
        skipped_chunks=skipped,
        failed_chunks=failed,
        conflict_skipped_chunks=conflict_skipped,
        updated_chunks=updated,
    )


def _dict_to_chunk(record: dict, namespace_override: str | None = None) -> tuple[Chunk, str | None]:
    """Parse one bundle record. Returns ``(chunk, bundle_chunk_id_or_None)``.

    The second element is the bundle's ``chunk_id`` string if present (v2),
    separated so the caller can decide whether to preserve the UUID based on
    ``on_conflict`` and ``preserve_ids``.
    """
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
    # content_hash recomputed by Chunk.__post_init__ if blank — trusting the
    # bundle here would skip NFC normalisation and let a tampered bundle
    # smuggle a hash/content mismatch past dedup. Always recompute.
    chunk = Chunk(
        content=record["content"],
        metadata=meta,
        id=uuid4(),
        created_at=created_at,
    )
    bundle_chunk_id = record.get("chunk_id")
    return chunk, bundle_chunk_id if isinstance(bundle_chunk_id, str) else None
