"""Batch export and import for indexed memory chunks.

Export serialises chunks (without embeddings) to a JSON bundle.
Import reads a bundle, re-embeds each chunk, and upserts to storage.

Bundle schema v3 (current):
  * Records carry ``chunk_id`` and ``content_hash`` for cross-instance
    roundtrip fidelity and hash-based dedup.
  * ``relations`` lists the ``chunk_relations`` edges whose both ends are
    records of the bundle, by bundle ``chunk_id``. Import maps each end to the
    row its record landed on, so a restore keeps consolidation coverage.
  * Import supports ``on_conflict`` in {"skip", "update", "duplicate"} to
    resolve hash collisions against the target DB.
  * v2 bundles (no ``relations``) and v1 bundles (no ``chunk_id`` /
    ``content_hash`` fields per record) are still accepted; missing fields are
    derived on import, and such a bundle restores no edges.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args
from uuid import UUID, uuid4

from memtomem.errors import EmbeddingError, EmbeddingPolicyChangedError
from memtomem.models import ORIGIN_CONSOLIDATION_POLICY, Chunk, ChunkMetadata, ChunkType
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.storage.sqlite_visibility import source_hidden
from memtomem.tools.entity_sync import sync_entities_for_chunks

if TYPE_CHECKING:
    from memtomem.config import IndexingConfig
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.storage.base import ChunkKeyRow
    from memtomem.storage.sqlite_backend import SqliteBackend

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------

_BUNDLE_VERSION = "3"

OnConflict = Literal["skip", "update", "duplicate"]
# Derived from the Literal so the type and the runtime validator cannot drift.
_VALID_ON_CONFLICT: frozenset[str] = frozenset(get_args(OnConflict))


@dataclass
class ExportBundle:
    """JSON-serialisable container for exported chunks."""

    version: str = _BUNDLE_VERSION
    exported_at: str = ""
    total_chunks: int = 0
    omitted_held_sources: int = 0
    chunks: list[dict] = field(default_factory=list)
    # ``{"source_id", "target_id", "relation_type"}`` per edge, by bundle
    # ``chunk_id``. Signed with ``chunks`` by the provenance marker.
    relations: list[dict] = field(default_factory=list)
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
            omitted_held_sources=data.get("omitted_held_sources", 0),
            chunks=data.get("chunks", []),
            # Kept as found: ``import_chunks`` rejects a non-list before any
            # write, and verifies the signature over exactly these items.
            relations=data.get("relations", []),
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
    # Bundle edges written, not written (malformed, an end that did not land,
    # guarded for a foreign bundle, or already present), and lost to a failed
    # write. Zero for bundles without relations.
    restored_relations: int = 0
    skipped_relations: int = 0
    failed_relations: int = 0


class ImportPrivacyError(Exception):
    """Raised when a foreign bundle's records hit the redaction guard.

    ADR-0006 Axis F.3: bundles without a valid local-provenance marker are
    scanned per-record on import and the whole import is rejected (atomically,
    mirroring ``mem_batch_add``) if any record contains a secret-shaped value,
    unless ``force_unsafe=True``. ``blocked_records`` is a count of bundle
    items — chunk records and relations — not of regex spans, so each ingress
    can render its native error without echoing the matched bytes.
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
    omitted_held_sources = 0
    for source in sorted(source_files):
        if source_filter and not match_source_filter_substring(source_filter, str(source)):
            continue

        def matches(chunk) -> bool:
            return (
                (not tag_filter or tag_filter in chunk.metadata.tags)
                and (not since or chunk.created_at >= since)
                and (not namespace_filter or chunk.metadata.namespace == namespace_filter)
            )

        # Bundles carry chunks but no source visibility state. Exporting held
        # rows would make deleted or renamed-away content searchable on import,
        # so the read itself leaves a hidden source out: a hold that lands
        # after any check here still keeps its rows out of the bundle.
        chunks = await storage.list_chunks_by_source(
            source, limit=100_000, exclude_hidden=True, as_stored=True
        )
        if not chunks:
            # Count only hidden sources with rows matching these export
            # filters. This read decides that count and nothing else; its
            # rows never reach ``records``.
            if await source_hidden(storage, source):
                held = await storage.list_chunks_by_source(source, limit=100_000, as_stored=True)
                omitted_held_sources += any(matches(chunk) for chunk in held)
            continue
        records.extend(_chunk_to_dict(chunk) for chunk in chunks if matches(chunk))

    # Only edges between exported records: an edge to a chunk the filters or a
    # hold left out would have nothing to land on at import.
    edges = await storage.get_relations_among([UUID(r["chunk_id"]) for r in records])
    relations = [
        {"source_id": str(sid), "target_id": str(tid), "relation_type": rel}
        for sid, tid, rel in edges
    ]

    marker: dict | None = None
    if stamp_provenance:
        key_path = provenance_key_path or provenance.key_path_for_db(storage.db_path)
        marker = provenance.make_marker(
            records, provenance.load_or_create_key_for_export(key_path), relations
        )

    bundle = ExportBundle(
        exported_at=datetime.now(timezone.utc).isoformat(),
        total_chunks=len(records),
        omitted_held_sources=omitted_held_sources,
        chunks=records,
        relations=relations,
        provenance=marker,
    )

    if omitted_held_sources:
        logger.warning(
            "Export omitted %d held or pending source(s); restore them and export again "
            "before treating this bundle as a complete backup",
            omitted_held_sources,
        )
    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(bundle.to_json(), encoding="utf-8")
        logger.info("Exported %d chunks -> %s", len(records), output_path)

    return bundle


def _chunk_to_dict(chunk: Chunk) -> dict:
    meta = chunk.metadata
    origin: dict[str, str] = {"origin": meta.origin} if meta.origin else {}
    return {
        # Writer provenance (#2161), emitted only when set. A summary that
        # round-trips without it would come back unowned, and the policy would
        # then fail every regeneration of that source closed. Import admits it
        # only from a verified self-export — see ``_dict_to_chunk``.
        **origin,
        # v2 additions: chunk_id + content_hash survive the roundtrip so
        # importers can dedup / preserve identity across instances.
        "chunk_id": str(chunk.id),
        "content_hash": chunk.content_hash,
        "content": chunk.content,
        "source_file": str(meta.source_file),
        "heading_hierarchy": list(meta.heading_hierarchy),
        "retrieval_context": meta.retrieval_context,
        "redaction_count": meta.redaction_count,
        "source_read_only": meta.source_read_only,
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
    retrievable surface here (content + context + heading + ``source_file`` + ``tags``),
    not just ``content`` as on the locally-derived-metadata write surfaces
    (``mem_add`` / ``mem_batch_add``). Self-exports skip this scan entirely, so
    the wider coverage never affects round-trip fidelity — it only closes the
    metadata-smuggling vector on genuinely foreign bundles.
    """
    return "\n".join(
        [
            chunk.content,
            chunk.metadata.retrieval_context,
            *chunk.metadata.heading_hierarchy,
            str(chunk.metadata.source_file),
            *chunk.metadata.tags,
        ]
    )


def _enforce_import_redaction(
    parsed: list[tuple[Chunk, str | None]],
    relations: list[tuple[str, str, str]],
    *,
    force_unsafe: bool,
    surface: str,
) -> None:
    """Per-record redaction gate for a foreign bundle (ADR-0006 Axis F.2).

    Scans the full retrievable surface of each record (see
    :func:`_import_scan_text`), and each well-formed relation's
    ``relation_type``, which is stored and printed by ``mem_related``. The edge
    ends are not scanned: they are only looked up among the bundle's own
    ``chunk_id`` values and never stored. Relations are scanned as found, before
    any is dropped for an end that does not land, so the decision covers what
    the bundle carries. Mirrors ``mem_batch_add``'s transactional shape:
    collect each record's decision with ``record_outcome=False``, reject the
    whole import on any block (recording ``blocked`` per blocked record and
    raising :class:`ImportPrivacyError`), and only commit per-record ``pass`` /
    ``bypassed`` counters once the import is known to proceed. ``scope="user"``
    because import upserts user-tier storage rows, never a git-tracked
    ``project_shared`` tier — so ``blocked_project_shared`` cannot arise here.
    """
    from memtomem import privacy

    # One decision set over records and relations, so a blocked relation stops
    # the import before any record's ``pass`` is counted.
    texts = [(_import_scan_text(chunk), len(chunk.content)) for chunk, _ in parsed]
    texts += [(relation_type, len(relation_type)) for _, _, relation_type in relations]
    decisions: list[tuple[str, int, int]] = []  # (decision, hit_count, content_chars)
    for text, content_chars in texts:
        guard = privacy.enforce_write_guard(
            text,
            surface=surface,
            force_unsafe=force_unsafe,
            scope="user",
            record_outcome=False,
        )
        decisions.append((guard.decision, len(guard.hits), content_chars))

    blocked = [d for d in decisions if d[0] == "blocked"]
    if blocked:
        for _ in blocked:
            privacy.record("blocked", surface)
        raise ImportPrivacyError(blocked_records=len(blocked))

    for decision, hit_count, content_chars in decisions:
        if decision == "bypassed":
            privacy.record("bypassed", surface)
            privacy.emit_bypass_audit(
                surface=surface,
                content_chars=content_chars,
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
    extract_entities: bool = True,
    indexing_config: IndexingConfig | None = None,
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
        source_file, created_at). The existing UUID is preserved. Each row
        is written once; a further record matched to it counts as
        ``conflict_skipped``.

    When several rows hold a record's content, the record matches the one at
    the key it writes, else (verified self-export only) the one its
    ``chunk_id`` names, else one no other record took — see
    :func:`_match_conflicts`.
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

    Relations (v3 bundles) are restored after the ids are settled: each end is
    mapped from its bundle ``chunk_id`` to the row that record landed on — a
    new row, or the existing row a hash conflict matched. An edge is written
    only when both ends exist, inside the same transaction as the chunks, and
    an edge the store already holds for that pair is kept. A foreign bundle
    restores only edges between rows this import newly added: it may describe
    its own chunks, never link rows the store already had.

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
        extract_entities: Write ``chunk_entities`` for the imported chunks.
            Carries ``indexing.extract_entities`` from callers that hold the
            config; the default keeps direct callers (and tests) on the same
            behaviour as the indexer.
    Returns:
        ImportStats with total / imported / skipped / failed / conflict_skipped
        / updated counts, and restored / skipped / failed relation counts.
    Raises:
        ImportPrivacyError: a foreign bundle has secret-bearing record(s) and
            ``force_unsafe`` is not set.
        ValueError: the bundle's ``relations`` is not a list.
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

    if not isinstance(bundle.relations, list):
        raise ValueError("Bundle 'relations' must be a list. No records were imported.")
    relations = _parse_relations(bundle.relations)

    if not bundle.chunks:
        return ImportStats(0, 0, 0, 0, skipped_relations=len(bundle.relations))

    # ADR-0006 Axis F.3: a valid local-provenance marker (verified over the raw
    # bundle records, before any parse/mutation) means this is a self-export, so
    # the redaction re-scan is skipped to preserve a deterministic round-trip.
    # Any absent/invalid marker → foreign → run the per-record gate below.
    from memtomem import provenance

    key_path = provenance_key_path or provenance.key_path_for_db(storage.db_path)
    is_self_export = provenance.verify_marker(
        bundle.chunks,
        bundle.provenance,
        provenance.load_key_for_verify(key_path),
        bundle.relations,
    )

    skipped = 0
    parsed: list[tuple[Chunk, str | None]] = []  # (chunk, bundle_chunk_id_or_None)
    keys: list[tuple[str, str, str, int]] = []  # each parsed record's destination key

    for idx, record in enumerate(bundle.chunks):
        try:
            chunk, bundle_chunk_id = _dict_to_chunk(
                record, namespace_override=namespace, allow_origin=is_self_export
            )
            # Inside the boundary: ``norm_path`` raises on a path it cannot
            # resolve (an embedded NUL), and that is one malformed record.
            key = _destination_key(chunk)
            parsed.append((chunk, bundle_chunk_id))
            keys.append(key)
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
            skipped_relations=len(bundle.relations),
        )

    # Foreign bundles run the F.2 redaction gate on every well-formed record
    # and relation (malformed ones were already dropped and are never scanned).
    if not is_self_export:
        _enforce_import_redaction(parsed, relations, force_unsafe=force_unsafe, surface=surface)

    if indexing_config is not None and indexing_config.hard_max_chunk_tokens:
        from memtomem.chunking.bounded import TokenBudget

        budget = TokenBudget(indexing_config)
        for chunk, _ in parsed:
            prefix = chunk.metadata.retrieval_context or " > ".join(
                chunk.metadata.heading_hierarchy
            )
            if (
                budget.count(chunk.content) > budget.body
                or budget.count(prefix) > budget.context
                or budget.count(chunk.retrieval_content, special=True) > budget.model
            ):
                raise ValueError(
                    "Import exceeds configured chunk token budgets; reindex the original source "
                    "with bounded chunking before exporting. No records were imported."
                )

    conflict_skipped = 0
    updated = 0

    # Chunks this import genuinely adds, as opposed to hash-matched rows that
    # already existed. Only the new ones get entities written below: a
    # hash-matched row holds content the store already had, and its entities may
    # have come from an LLM scan, which is strictly richer than the regex pass.
    # Overwriting those would quietly downgrade them (backfilling rows that have
    # none is the separate question ``mem_entity_scan`` answers).
    newly_imported: list[Chunk] = []
    # Rows an update-conflict is about to overwrite in place. ``upsert_chunks``
    # writes ``origin`` like any other column, so an incoming record — which
    # carries None for everything except a verified self-export — would strip
    # the provenance the stored row already had, and the one-shot backfill that
    # could have re-adopted it has long recorded itself done (#2161). The
    # stored value always wins: an import may replace a chunk's content, never
    # its writer.
    origin_carryover: list[Chunk] = []
    # Bundle ``chunk_id`` -> the row that record lands on, for the relation
    # ends. A skipped conflict lands on the existing row as much as an update.
    landed: dict[str, UUID] = {}

    if on_conflict == "duplicate":
        # Back-compat path: every record gets a fresh UUID, no hash check
        # at this layer. Since #691 the storage UNIQUE index +
        # ``INSERT OR IGNORE`` collapses any rows that would have shared
        # ``(namespace, source_file, content_hash, start_line)``, so this
        # mode no longer materialises duplicate rows even though the
        # caller surface still accepts it.
        to_upsert = [c for c, _ in parsed]
        newly_imported = list(to_upsert)
        landed = {bid: c.id for c, bid in parsed if bid is not None}
    else:
        all_hashes = [c.content_hash for c, _ in parsed]
        matches = _match_conflicts(
            parsed,
            keys,
            await storage.get_chunk_rows_by_hashes(all_hashes),
            trust_ids=is_self_export,
        )

        to_upsert = []
        for (chunk, bundle_chunk_id), match in zip(parsed, matches):
            if match is not None:
                existing_id, writes = match
                if bundle_chunk_id is not None:
                    landed[bundle_chunk_id] = existing_id
                # One write per row: ``upsert_chunks`` replaces a row's vector
                # once and then inserts one per occurrence, so the same id twice
                # in a batch fails and rolls the whole import back.
                if on_conflict == "skip" or not writes:
                    conflict_skipped += 1
                    continue
                # on_conflict == "update": reuse the existing row's UUID so
                # upsert_chunks hits the UPDATE branch, preserving identity.
                chunk.id = existing_id
                updated += 1
                to_upsert.append(chunk)
                origin_carryover.append(chunk)
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
                if bundle_chunk_id is not None:
                    landed[bundle_chunk_id] = chunk.id
                to_upsert.append(chunk)
                newly_imported.append(chunk)

    if origin_carryover:
        stored = await storage.get_chunks_batch([c.id for c in origin_carryover])
        for chunk in origin_carryover:
            prior = stored.get(chunk.id)
            if prior is not None:
                chunk.metadata = replace(chunk.metadata, origin=prior.metadata.origin)

    edges = _land_relations(
        relations,
        landed,
        new_ids=None if is_self_export else {c.id for c in newly_imported},
    )

    imported = failed = 0
    restored_relations = failed_relations = 0
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
                skipped_relations=len(bundle.relations),
            )

    # Also with nothing to upsert: re-importing a bundle into a store that kept
    # its chunks but lost their edges is a write of edges alone.
    if to_upsert or edges:
        try:
            # One transaction so a failure in any part leaves nothing behind:
            # without it, a chunk write that committed before the entity or
            # edge write failed would be reported as a failed import while its
            # rows stayed in the store.
            async with storage.transaction():
                if to_upsert:
                    await storage.upsert_chunks(to_upsert)
                    # Imported content never passes through the indexing engine,
                    # so this is the only place its entities can be written (#2155).
                    await sync_entities_for_chunks(
                        storage, newly_imported, enabled=extract_entities
                    )
                written = await storage.insert_relations(edges) if edges else 0
            # "imported" counts only genuinely new rows; updates are tracked
            # separately so callers can distinguish merge from overwrite.
            imported = len(to_upsert) - updated
            restored_relations = written
        except EmbeddingPolicyChangedError:
            # Not a per-import failure: the store left this process's policy,
            # and the caller has to see the recovery the error names.
            raise
        except Exception as exc:
            logger.error("Upsert failed during import: %s", exc)
            failed = len(to_upsert)
            imported = 0
            updated = 0
            failed_relations = len(edges)

    return ImportStats(
        total_chunks=len(bundle.chunks),
        imported_chunks=imported,
        skipped_chunks=skipped,
        failed_chunks=failed,
        conflict_skipped_chunks=conflict_skipped,
        updated_chunks=updated,
        restored_relations=restored_relations,
        skipped_relations=len(bundle.relations) - restored_relations - failed_relations,
        failed_relations=failed_relations,
    )


def _parse_relations(items: list) -> list[tuple[str, str, str]]:
    """Return the well-formed bundle relations as ``(source, target, type)``.

    Well-formed is a dict with three ``str`` fields, nothing more: ``mem_link``
    writes any ``relation_type``, empty included, and a self-export has to bring
    back what was written. The ends are bundle ``chunk_id`` values, resolved
    later against the records' own ids.
    """
    parsed: list[tuple[str, str, str]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        source_id = item.get("source_id")
        target_id = item.get("target_id")
        relation_type = item.get("relation_type")
        if (
            isinstance(source_id, str)
            and isinstance(target_id, str)
            and isinstance(relation_type, str)
        ):
            parsed.append((source_id, target_id, relation_type))
    return parsed


def _land_relations(
    relations: list[tuple[str, str, str]],
    landed: dict[str, UUID],
    *,
    new_ids: set[UUID] | None,
) -> list[tuple[UUID, UUID, str]]:
    """Map each relation's ends onto the rows their records landed on.

    Drops an edge with an end that did not land (a malformed record, an id no
    record carries) and one whose ends landed on the same row, as two
    same-content records matching one existing row do. *new_ids*, for a foreign
    bundle, is the rows this import adds: an edge with an end outside it would
    link a row the store already had, which the bundle cannot vouch for — a
    ``consolidated_into`` edge onto a user's note hides it from
    ``mem_consolidate``. ``None`` (a verified self-export) admits both kinds.
    """
    edges: list[tuple[UUID, UUID, str]] = []
    for source, target, relation_type in relations:
        source_id = landed.get(source)
        target_id = landed.get(target)
        if source_id is None or target_id is None or source_id == target_id:
            continue
        if new_ids is not None and not (source_id in new_ids and target_id in new_ids):
            continue
        edges.append((source_id, target_id, relation_type))
    return edges


def _destination_key(chunk: Chunk) -> tuple[str, str, str, int]:
    """The ``UNIQUE(namespace, source_file, content_hash, start_line)`` key *chunk* writes.

    The path goes through ``norm_path``, as ``upsert_chunks`` stores it.
    """
    meta = chunk.metadata
    return (chunk.content_hash, meta.namespace, norm_path(meta.source_file), meta.start_line)


def _match_conflicts(
    parsed: list[tuple[Chunk, str | None]],
    keys: list[tuple[str, str, str, int]],
    rows_by_hash: dict[str, list[ChunkKeyRow]],
    *,
    trust_ids: bool,
) -> list[tuple[UUID, bool] | None]:
    """Pick, per record, the existing row holding its content, or None.

    *keys* holds each record's :func:`_destination_key`. Each match is
    ``(row id, writes)``. Records are grouped by the key they
    would write, and every record of a group gets the same row: two rows
    updated onto one key would fail the uniqueness index and roll the import
    back. Each pass runs over all groups before the next, and a row one group
    takes is not offered to another:

    1. the row already at the group's key — always first, since an update
       that moved any other row there would collide with it;
    2. for a verified self-export only, the row whose id a record carries (a
       namespace override, a moved source). A foreign bundle never names the
       row an update overwrites;
    3. the first row of that content no group took, else the first row of
       that content — the content exists, so the record skips or updates onto
       it as before.

    ``writes`` is set on the first record of the group that took the row, so
    an update writes each row once. A group that only shares a row taken by
    another (case 3 with every row taken) lands there and writes nothing.
    """
    groups: dict[tuple[str, str, str, int], list[int]] = {}
    for index, key in enumerate(keys):
        groups.setdefault(key, []).append(index)

    # Indexed once, so a bundle of many records with one text stays linear.
    at_key: dict[tuple[str, str, str, int], UUID] = {}
    hash_of: dict[UUID, str] = {}
    for content_hash, rows in rows_by_hash.items():
        for row in rows:
            at_key[(content_hash, row.namespace, row.source_file, row.start_line)] = row.id
            hash_of[row.id] = content_hash

    owned: dict[tuple[str, str, str, int], UUID] = {}
    shared: dict[tuple[str, str, str, int], UUID] = {}
    taken: set[UUID] = set()

    def take(key: tuple[str, str, str, int], row_id: UUID) -> None:
        owned[key] = row_id
        taken.add(row_id)

    for key in groups:
        occupant = at_key.get(key)
        if occupant is not None:
            take(key, occupant)

    if trust_ids:
        for key, members in groups.items():
            if key in owned:
                continue
            for index in members:
                try:
                    candidate = UUID(parsed[index][1] or "")
                except ValueError:
                    continue
                if hash_of.get(candidate) == key[0] and candidate not in taken:
                    take(key, candidate)
                    break

    # Rows are only ever taken, so each hash's first free row moves forward.
    cursor: dict[str, int] = {}
    for key in groups:
        bucket = rows_by_hash.get(key[0])
        if key in owned or not bucket:
            continue
        position = cursor.get(key[0], 0)
        while position < len(bucket) and bucket[position].id in taken:
            position += 1
        cursor[key[0]] = position
        if position < len(bucket):
            take(key, bucket[position].id)
        else:
            shared[key] = bucket[0].id

    matches: list[tuple[UUID, bool] | None] = []
    first = {members[0] for members in groups.values()}
    for index, key in enumerate(keys):
        if key in owned:
            matches.append((owned[key], index in first))
        elif key in shared:
            matches.append((shared[key], False))
        else:
            matches.append(None)
    return matches


def _dict_to_chunk(
    record: dict,
    namespace_override: str | None = None,
    *,
    allow_origin: bool = False,
) -> tuple[Chunk, str | None]:
    """Parse one bundle record. Returns ``(chunk, bundle_chunk_id_or_None)``.

    The second element is the bundle's ``chunk_id`` string if present (v2),
    separated so the caller can decide whether to preserve the UUID based on
    ``on_conflict`` and ``preserve_ids``.

    ``allow_origin`` gates writer provenance (#2161) and is set only for a
    bundle whose local-provenance marker verified. ``origin`` is an ownership
    proof the consolidation policy deletes on, so accepting it from a bundle
    anyone can author would hand back the forgery the typed column exists to
    prevent: a crafted record at a virtual summary path would import as
    policy-owned. Foreign bundles therefore import ``origin=None``, and even a
    self-export is held to the known constant.
    """
    ns = namespace_override or record.get("namespace", "default")
    # A number or boolean would be stored in SQLite's text form of it, which
    # no Python conversion reproduces, so the conflict match could miss the
    # row already at that key. Every export writes a string.
    if not isinstance(ns, str):
        raise TypeError("namespace must be a string")
    raw_origin = record.get("origin")
    origin = (
        ORIGIN_CONSOLIDATION_POLICY
        if allow_origin and raw_origin == ORIGIN_CONSOLIDATION_POLICY
        else None
    )
    meta = ChunkMetadata(
        source_file=Path(record["source_file"]),
        heading_hierarchy=tuple(record.get("heading_hierarchy", [])),
        retrieval_context=str(record.get("retrieval_context", "")),
        redaction_count=max(0, int(record.get("redaction_count", 0))),
        source_read_only=bool(record.get("source_read_only", False)),
        chunk_type=ChunkType(record.get("chunk_type", "raw_text")),
        start_line=int(record.get("start_line", 0)),
        end_line=int(record.get("end_line", 0)),
        language=record.get("language", "en"),
        tags=tuple(record.get("tags", [])),
        namespace=ns,
        origin=origin,
    )
    created_at = (
        datetime.fromisoformat(record["created_at"])
        if "created_at" in record
        else datetime.now(timezone.utc)
    )
    # Chunk timestamps are always timezone-aware (see ``_row_to_chunk``); a
    # naive bundle value is read as UTC, matching the storage layer. An
    # offset-aware value stays as-is — it is correct in Python, and the
    # storage boundary renders it in UTC.
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=timezone.utc)
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
