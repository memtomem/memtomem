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

from memtomem.errors import EmbeddingError
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
    # Local-provenance marker (ADR-0006 Axis F.3). Present on self-exports;
    # ``None`` on hand-crafted / pre-F.3 bundles, which then import as foreign.
    # See ``memtomem.provenance``.
    provenance: dict | None = None

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
            provenance=data.get("provenance"),
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


class ImportPrivacyError(Exception):
    """Raised when a foreign bundle's records hit the redaction guard.

    ADR-0006 Axis F.3: bundles without a valid local-provenance marker are
    scanned per-record on import and the whole import is rejected (atomically,
    mirroring ``mem_batch_add``) if any record contains a secret-shaped value,
    unless ``force_unsafe=True``. ``blocked_records`` is a *record* count (not a
    regex-span count) so each ingress can render its native error without
    echoing the matched bytes.
    """

    def __init__(self, blocked_records: int) -> None:
        self.blocked_records = blocked_records
        super().__init__(
            f"{blocked_records} bundle record(s) match privacy pattern(s); import "
            "rejected. Retry with force_unsafe=True to bypass (audit-logged)."
        )


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
    provenance_key_path: Path | None = None,
    stamp_provenance: bool = True,
) -> ExportBundle:
    """Export indexed chunks to an ExportBundle (and optionally to a JSON file).

    The bundle is stamped with a local-provenance marker (ADR-0006 Axis F.3) so
    a re-import on this same install round-trips unchanged (the redaction gate
    is skipped for verified self-exports); see ``memtomem.provenance``.

    Args:
        storage: StorageBackend instance.
        output_path: If given, write JSON to this path.
        source_filter: Only include chunks whose source_file contains this substring.
        tag_filter: Only include chunks that have this exact tag.
        since: Only include chunks created at or after this datetime.
        provenance_key_path: Override the per-install key file location (the
            sidecar next to the DB by default). Intended for tests.
        stamp_provenance: Sign the bundle with the per-install key (default).
            Pass ``False`` for count-only callers (e.g. the ``/export/stats``
            preview) so a read-shaped request neither creates the key file nor
            fails on a key-file problem unrelated to counting.
    Returns:
        ExportBundle with the selected chunks; a provenance marker is attached
        when ``stamp_provenance`` is set.
    """
    from memtomem import provenance
    from memtomem.search.pipeline import match_source_filter_substring

    source_files = await storage.get_all_source_files()

    # Substring-only contract with negation — see
    # ``match_source_filter_substring`` for the separator-fold rule (#720).
    records: list[dict] = []
    for source in sorted(source_files):
        if source_filter and not match_source_filter_substring(source_filter, str(source)):
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

    marker: dict | None = None
    if stamp_provenance:
        key_path = provenance_key_path or provenance.key_path_for_db(storage.db_path)
        marker = provenance.make_marker(records, provenance.load_or_create_key_for_export(key_path))

    bundle = ExportBundle(
        exported_at=datetime.now(timezone.utc).isoformat(),
        total_chunks=len(records),
        chunks=records,
        provenance=marker,
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


def _import_scan_text(chunk: Chunk) -> str:
    """The retrievable surface scanned on a foreign import (ADR-0006 Axis F.3).

    Import is the one write surface where *every* field — including metadata —
    arrives verbatim from an untrusted bundle and is then embedded
    (``retrieval_content`` = heading hierarchy + content), stored, and
    retrievable. So the foreign-bundle redaction scan covers the full
    retrievable surface here (content + heading + ``source_file`` + ``tags``),
    not just ``content`` as on the locally-derived-metadata write surfaces
    (``mem_add`` / ``mem_batch_add``). Self-exports skip this scan entirely, so
    the wider coverage never affects round-trip fidelity — it only closes the
    metadata-smuggling vector on genuinely foreign bundles.
    """
    return "\n".join(
        [chunk.retrieval_content, str(chunk.metadata.source_file), *chunk.metadata.tags]
    )


def _enforce_import_redaction(
    parsed: list[tuple[Chunk, str | None]],
    *,
    force_unsafe: bool,
    surface: str,
) -> None:
    """Per-record redaction gate for a foreign bundle (ADR-0006 Axis F.2).

    Scans the full retrievable surface of each record (see
    :func:`_import_scan_text`). Mirrors ``mem_batch_add``'s transactional shape:
    collect each record's decision with ``record_outcome=False``, reject the
    whole import on any block (recording ``blocked`` per blocked record and
    raising :class:`ImportPrivacyError`), and only commit per-record ``pass`` /
    ``bypassed`` counters once the import is known to proceed. ``scope="user"``
    because import upserts user-tier storage rows, never a git-tracked
    ``project_shared`` tier — so ``blocked_project_shared`` cannot arise here.
    """
    from memtomem import privacy

    decisions: list[tuple[str, int]] = []  # (decision, hit_count)
    for chunk, _ in parsed:
        guard = privacy.enforce_write_guard(
            _import_scan_text(chunk),
            surface=surface,
            force_unsafe=force_unsafe,
            scope="user",
            record_outcome=False,
        )
        decisions.append((guard.decision, len(guard.hits)))

    blocked = [d for d in decisions if d[0] == "blocked"]
    if blocked:
        for _ in blocked:
            privacy.record("blocked", surface)
        raise ImportPrivacyError(blocked_records=len(blocked))

    for (decision, hit_count), (chunk, _) in zip(decisions, parsed):
        if decision == "bypassed":
            privacy.record("bypassed", surface)
            privacy.emit_bypass_audit(
                surface=surface,
                content_chars=len(chunk.content),
                hits=hit_count,
            )
        else:
            privacy.record("pass", surface)


async def import_chunks(
    storage: SqliteBackend,
    embedder: EmbeddingProvider,
    input_path: Path,
    namespace: str | None = None,
    on_conflict: OnConflict = "skip",
    preserve_ids: bool = False,
    force_unsafe: bool = False,
    provenance_key_path: Path | None = None,
    surface: str = "import",
) -> ImportStats:
    """Import chunks from a JSON bundle file.

    Trust boundary (ADR-0006 Axis F.3): a bundle this install exported carries a
    valid local-provenance marker and round-trips unchanged. A bundle with an
    absent or invalid marker is *foreign* — every well-formed record's full
    retrievable surface (content + heading + ``source_file`` + ``tags``; see
    :func:`_import_scan_text`) is scanned with ``privacy.enforce_write_guard``
    and the whole import is rejected with :class:`ImportPrivacyError` if any
    record matches a secret pattern, unless ``force_unsafe=True`` (bypass is
    audit-logged). The scan runs before any embed/upsert, so rejection is atomic
    — no partial import.

    Each chunk is re-embedded and upserted. Conflict resolution against the
    target DB's existing ``content_hash`` set is controlled by ``on_conflict``:

      * ``"skip"`` (default, idempotent): records whose content already
        exists in the DB are dropped. Re-importing the same bundle is a
        no-op; merging bundles with overlap adds only the unique side.
      * ``"update"``: records matching an existing hash overwrite that
        existing row's metadata (tags, namespace, heading hierarchy,
        source_file, created_at). The existing UUID is preserved.
      * ``"duplicate"``: no hash check at the import layer — every record
        is sent to ``upsert_chunks`` with a fresh UUID. This was the
        pre-v2 behaviour, but since #691 the storage layer now enforces
        ``UNIQUE(namespace, source_file, content_hash, start_line)`` via
        ``INSERT OR IGNORE``: rows that would have produced duplicates
        are silently dropped at insert time. The mode is kept for
        back-compat with existing callers but no longer materialises
        duplicate rows on disk.

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
        force_unsafe: Bypass the redaction gate for a foreign bundle whose
            records match secret patterns (audit-logged).
        provenance_key_path: Override the per-install key file location (the
            sidecar next to the DB by default). Intended for tests.
        surface: Audit/counter label for the redaction guard ("mem_import",
            "web_api_import", …).
    Returns:
        ImportStats with total / imported / skipped / failed / conflict_skipped
        / updated counts.
    Raises:
        ImportPrivacyError: a foreign bundle has secret-bearing record(s) and
            ``force_unsafe`` is not set.
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

    # ADR-0006 Axis F.3: a valid local-provenance marker (verified over the raw
    # bundle records, before any parse/mutation) means this is a self-export, so
    # the redaction re-scan is skipped to preserve a deterministic round-trip.
    # Any absent/invalid marker → foreign → run the per-record gate below.
    from memtomem import provenance

    key_path = provenance_key_path or provenance.key_path_for_db(storage.db_path)
    is_self_export = provenance.verify_marker(
        bundle.chunks, bundle.provenance, provenance.load_key_for_verify(key_path)
    )

    skipped = 0
    parsed: list[tuple[Chunk, str | None]] = []  # (chunk, bundle_chunk_id_or_None)

    for idx, record in enumerate(bundle.chunks):
        try:
            chunk, bundle_chunk_id = _dict_to_chunk(record, namespace_override=namespace)
            parsed.append((chunk, bundle_chunk_id))
        except Exception as exc:
            # Never interpolate ``exc`` into the log: parse errors raised by
            # ``datetime.fromisoformat`` / ``int()`` / ``ChunkType(...)`` embed
            # the offending field value, which in a *foreign* bundle could be a
            # secret-shaped string. Log the record index and exception class
            # only — matched bytes must not reach the log sink.
            logger.warning("Skipping malformed bundle record %d (%s)", idx, type(exc).__name__)
            skipped += 1

    if not parsed:
        return ImportStats(
            total_chunks=len(bundle.chunks),
            imported_chunks=0,
            skipped_chunks=skipped,
            failed_chunks=0,
        )

    # Foreign bundles run the F.2 redaction gate on every well-formed record
    # (malformed records were already dropped above and are never scanned).
    if not is_self_export:
        _enforce_import_redaction(parsed, force_unsafe=force_unsafe, surface=surface)

    conflict_skipped = 0
    updated = 0

    if on_conflict == "duplicate":
        # Back-compat path: every record gets a fresh UUID, no hash check
        # at this layer. Since #691 the storage UNIQUE index +
        # ``INSERT OR IGNORE`` collapses any rows that would have shared
        # ``(namespace, source_file, content_hash, start_line)``, so this
        # mode no longer materialises duplicate rows even though the
        # caller surface still accepts it.
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
        # Embed ``retrieval_content`` (heading-hierarchy prefix + body), matching
        # what the index engine embeds at ingest time (``IndexEngine`` uses
        # ``c.retrieval_content``). Embedding plain ``c.content`` here would store
        # a *different* vector for a chunk than the one it would get if indexed
        # natively, so the same content_hash would retrieve differently after an
        # export -> import roundtrip.
        contents = [c.retrieval_content for c in to_upsert]
        try:
            embeddings = await embedder.embed_texts(contents)
            if len(embeddings) != len(contents):
                # Same truncation class as the index engine (issue #1563): a
                # short array would ``zip``-drop the trailing chunks' vectors
                # while ``upsert_chunks`` still commits their content_hash,
                # leaving hash-poisoned BM25-only rows that never re-embed.
                # Fail loud so the ``except`` below returns the zero-import
                # failure path before any upsert.
                raise EmbeddingError(
                    f"Embedder returned {len(embeddings)} vectors for "
                    f"{len(contents)} chunks during import; refusing to store "
                    "a truncated result."
                )
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
