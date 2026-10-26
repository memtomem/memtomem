"""Batch export and import for indexed memory chunks.

Export serialises chunks (without embeddings) to a JSON bundle.
Import reads a bundle, re-embeds each chunk, and upserts to storage.

Bundle schema v4 (current):
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
from dataclasses import dataclass, field, fields, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Literal, get_args
from uuid import UUID, uuid4

from memtomem.errors import EmbeddingError, EmbeddingPolicyChangedError
from memtomem.models import ORIGIN_CONSOLIDATION_POLICY, Chunk, ChunkMetadata, ChunkType
from memtomem.storage.base import WRITER_WAIT_S
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.storage.sqlite_export import read_export
from memtomem.tools.bundle_format import (
    BUNDLE_FORMAT,
    BUNDLE_VERSION,
    BundleValidationError,
    MAX_BUNDLE_BYTES,
    StoredBundleValidationError,
    enforce_size,
    invalid,
    load_json,
    map_chunk,
    timestamp,
    validate_records,
    validate_root_map,
)
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

_BUNDLE_VERSION = BUNDLE_VERSION

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
    format: str | None = BUNDLE_FORMAT
    omitted_self_relations: int = 0
    # Transient output for the immediate Web response, never a wire field.
    _export_json: str | None = field(default=None, init=False, repr=False, compare=False)

    def to_json(self, *, indent: int = 2) -> str:
        payload = {f.name: getattr(self, f.name) for f in fields(self) if f.init}
        if self.format is None:
            payload.pop("format")
        text = json.dumps(payload, ensure_ascii=False, indent=indent, allow_nan=False)
        enforce_size(text)
        self._export_json = text
        return text

    @classmethod
    def from_json(cls, text: str) -> ExportBundle:
        data = load_json(text)
        version = data.get("version", "1")
        validate_records(data["chunks"], data.get("relations", []), version)
        return cls(
            version=version,
            format=data.get("format"),
            exported_at=data.get("exported_at", ""),
            total_chunks=len(data["chunks"]),
            omitted_held_sources=data.get("omitted_held_sources", 0),
            chunks=data["chunks"],
            relations=data.get("relations", []),
            provenance=data.get("provenance"),
            omitted_self_relations=data.get("omitted_self_relations", 0),
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
    warnings: list[str] = field(default_factory=list)
    missing_source_files: int = 0
    unavailable_source_files: int = 0


class ImportChangedError(ValueError):
    """The prepared import no longer matches the writer transaction snapshot."""


class ImportPrivacyError(Exception):
    """Raised when imported records hit the redaction guard.

    ADR-0006 Axis F.3: bundles without a valid local-provenance marker are
    scanned per-record on import and the whole import is rejected (atomically,
    mirroring ``mem_batch_add``) if any record contains a secret-shaped value,
    unless a private restore uses ``force_unsafe=True``. Shared restores are
    always scanned and cannot bypass the guard. ``blocked_records`` is a count of bundle
    items — chunk records and relations — not of regex spans, so each ingress
    can render its native error without echoing the matched bytes.
    """

    def __init__(self, blocked_records: int, *, shared_blocked: bool = False) -> None:
        self.blocked_records = blocked_records
        self.shared_blocked = shared_blocked
        hint = (
            "Privacy bypass is unavailable for project_shared."
            if shared_blocked
            else "Retry with force_unsafe=True to bypass (audit-logged)."
        )
        super().__init__(
            f"{blocked_records} bundle record(s) match privacy pattern(s); import rejected. {hint}"
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

    chunks, edges, omitted_held_sources = await read_export(
        storage,
        source_filter=source_filter,
        tag_filter=tag_filter,
        since=since,
        namespace_filter=namespace_filter,
    )
    records = [_export_chunk_record(chunk) for chunk in chunks]
    repaired_overlap = sum(
        chunk.metadata.overlap_before != record["overlap_before"]
        or chunk.metadata.overlap_after != record["overlap_after"]
        for chunk, record in zip(chunks, records)
    )
    if repaired_overlap:
        logger.warning("Export bounded stale overlap metadata for %d chunk(s)", repaired_overlap)
    relations = [
        {"source_id": str(sid), "target_id": str(tid), "relation_type": rel}
        for sid, tid, rel in edges
        if sid != tid
    ]
    omitted_self_relations = len(edges) - len(relations)
    # Do not publish a backup whose stored scalar metadata is already invalid.
    try:
        validate_records(records, relations, BUNDLE_VERSION)
    except BundleValidationError as exc:
        label = exc.where
        if label.startswith("record "):
            label = f"chunk {records[int(label.split()[1])]['chunk_id']}"
        raise StoredBundleValidationError(
            f"Invalid stored {label} field {exc.field}. No bundle exported."
        ) from exc

    marker: dict | None = None
    if stamp_provenance:
        key_path = provenance_key_path or provenance.key_path_for_db(storage.db_path)
        marker = provenance.make_bundle_marker(
            records,
            provenance.load_or_create_key_for_export(key_path),
            relations,
            format=BUNDLE_FORMAT,
            version=BUNDLE_VERSION,
        )

    bundle = ExportBundle(
        exported_at=datetime.now(timezone.utc).isoformat(),
        total_chunks=len(records),
        omitted_held_sources=omitted_held_sources,
        chunks=records,
        relations=relations,
        provenance=marker,
        omitted_self_relations=omitted_self_relations,
    )

    if omitted_self_relations:
        logger.warning("Export omitted %d self-relation(s)", omitted_self_relations)
    if omitted_held_sources:
        logger.warning(
            "Export omitted %d held or pending source(s); restore them and export again "
            "before treating this bundle as a complete backup",
            omitted_held_sources,
        )
    text = bundle.to_json()  # The same byte ceiling applies to downloads and file exports.
    if output_path is not None:
        import stat

        # Resolve parents only: resolving the leaf would silently follow a symlink.
        output_path = output_path.expanduser()
        output_path = output_path.parent.resolve() / output_path.name

        def check_target():
            try:
                mode = output_path.lstat().st_mode
            except FileNotFoundError:
                return
            if not stat.S_ISREG(mode):
                raise ValueError("Export destination must be a regular file, not a symlink.")

        check_target()
        from memtomem.context._atomic import atomic_write_bytes

        atomic_write_bytes(
            output_path, text.encode("utf-8"), mode=0o600, before_replace=check_target
        )
        logger.info("Exported %d chunks -> %s", len(records), output_path)

    return bundle


def _export_chunk_record(chunk: Chunk) -> dict:
    """Bound historical impossible counts in an export copy, never in storage.

    Keep this separate from _chunk_to_dict: import's post-carry validation must
    still reject an invalid merged row instead of validating a repaired copy.
    """
    record = _chunk_to_dict(chunk)
    for key in ("overlap_before", "overlap_after"):
        value = record[key]
        if type(value) is int:
            record[key] = min(max(value, 0), len(chunk.content))
    return record


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
        "updated_at": chunk.updated_at.isoformat(),
        "scope": meta.scope,
        "project_root": str(meta.project_root) if meta.project_root is not None else None,
        "valid_from_unix": meta.valid_from_unix,
        "valid_to_unix": meta.valid_to_unix,
        "overlap_before": meta.overlap_before,
        "overlap_after": meta.overlap_after,
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
    (``mem_add`` / ``mem_batch_add``). Private self-exports skip unchanged fields;
    transformed metadata and all shared records are scanned too.
    """
    return "\n".join(
        [
            chunk.content,
            chunk.metadata.retrieval_context,
            *chunk.metadata.heading_hierarchy,
            str(chunk.metadata.source_file),
            *chunk.metadata.tags,
            chunk.metadata.namespace,
            chunk.metadata.language,
            str(chunk.metadata.project_root) if chunk.metadata.project_root is not None else "",
        ]
    )


def _enforce_import_redaction(
    parsed: list[tuple[Chunk, str | None]],
    relations: list[tuple[str, str, str]],
    *,
    force_unsafe: bool,
    surface: str,
    trusted: bool = False,
    changed_metadata: list[tuple[str, str]] | None = None,
) -> None:
    """Only explicit allow decisions pass; shared restores never inherit an exemption."""
    from memtomem import privacy

    texts = [
        (_import_scan_text(c), c.metadata.scope)
        for c, _ in parsed
        if not trusted or c.metadata.scope == "project_shared"
    ]
    scopes = {bid: c.metadata.scope for c, bid in parsed}
    for sid, tid, rel in relations:
        scope = (
            "project_shared" if "project_shared" in (scopes.get(sid), scopes.get(tid)) else "user"
        )
        if not trusted or scope == "project_shared":
            texts.append((rel, scope))
    texts += changed_metadata or []
    decisions = []
    for text, scope in texts:
        guard = privacy.enforce_write_guard(
            text,
            surface=surface,
            force_unsafe=force_unsafe,
            scope=scope,
            record_outcome=False,
        )
        allowed = guard.decision == "pass" or (
            guard.decision == "bypassed" and scope != "project_shared"
        )
        decisions.append((guard, len(text), allowed, scope))
    blocked = [item for item in decisions if not item[2]]
    if blocked:
        for guard, _, _, _ in blocked:
            privacy.record(guard.decision, surface)
        raise ImportPrivacyError(
            len(blocked), shared_blocked=any(d[3] == "project_shared" for d in blocked)
        )
    for guard, chars, _, _ in decisions:
        privacy.record(guard.decision, surface)
        if guard.decision == "bypassed":
            privacy.emit_bypass_audit(surface=surface, content_chars=chars, hits=len(guard.hits))


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
    project_root_map: dict[str, str] | None = None,
    confirm_project_shared: bool = False,
) -> ImportStats:
    """Restore a validated bundle atomically, retaining project and temporal boundaries.

    v1-v3 user bundles remain readable; missing update fields retain local values.
    Project records require v4 and registered destination roots. Mapping keys name
    exact source project roots, never arbitrary source prefixes. Shared restores
    require explicit consent and a full privacy scan even for a signed self-export.
    Malformed records reject the whole import before embedding or writing.
    """
    if not isinstance(on_conflict, str) or on_conflict not in _VALID_ON_CONFLICT:
        raise ValueError("Invalid on_conflict. Use skip, update, or duplicate.")
    for label, value in (
        ("preserve_ids", preserve_ids),
        ("force_unsafe", force_unsafe),
        ("confirm_project_shared", confirm_project_shared),
    ):
        if type(value) is not bool:
            raise invalid("options", label)
    if namespace is not None and not isinstance(namespace, str):
        raise invalid("options", "namespace")
    if namespace is not None:
        try:
            namespace.encode("utf-8")
            if "\x00" in namespace:
                raise ValueError()
        except (ValueError, UnicodeError) as exc:
            raise invalid("options", "namespace") from exc
    root_map = validate_root_map(project_root_map)
    with input_path.open("rb") as stream:
        raw = stream.read(MAX_BUNDLE_BYTES + 1)
    if len(raw) > MAX_BUNDLE_BYTES:
        raise ValueError("Import file too large (maximum 100 MiB).")
    try:
        bundle = ExportBundle.from_json(raw.decode("utf-8"))
    except UnicodeError as exc:
        raise invalid("JSON", "UTF-8") from exc
    relations = _parse_relations(bundle.relations)
    from memtomem import provenance

    key_path = provenance_key_path or provenance.key_path_for_db(storage.db_path)
    is_self_export = provenance.verify_bundle_marker(
        bundle.chunks,
        bundle.provenance,
        provenance.load_key_for_verify(key_path),
        bundle.relations,
        format=bundle.format,
        version=bundle.version,
    )
    parsed: list[tuple[Chunk, str | None]] = []
    changed_metadata: list[tuple[str, str]] = []
    for index, record in enumerate(bundle.chunks):
        try:
            chunk, bid = _dict_to_chunk(
                record, namespace_override=namespace, allow_origin=is_self_export
            )
            map_chunk(
                chunk,
                bundle.version,
                root_map,
                indexing_config,
                record_index=index,
                source_file=record["source_file"],
                project_root=record.get("project_root"),
                self_export=is_self_export,
            )
            _destination_key(chunk)
        except BundleValidationError:
            raise
        except Exception as exc:
            raise invalid(f"record {index}", "metadata or source path") from exc
        parsed.append((chunk, bid))
        if is_self_export and chunk.metadata.scope != "project_shared":
            changed = []
            for field_name in ("source_file", "project_root", "namespace"):
                value = getattr(chunk.metadata, field_name)
                old = record.get(field_name)
                if (str(value) if value is not None else None) != old:
                    changed.append(str(value) if value is not None else "")
            if changed:
                changed_metadata.append(("\n".join(changed), chunk.metadata.scope))
    shared = any(c.metadata.scope == "project_shared" for c, _ in parsed)
    if shared and not confirm_project_shared:
        raise ValueError("project_shared restore requires confirm_project_shared=True.")
    hashes = [c.content_hash for c, _ in parsed]
    initial_rows = await storage.get_chunk_rows_by_hashes(hashes)
    initial_ids = [r.id for rows in initial_rows.values() for r in rows]
    initial_chunks = await storage.get_chunks_batch(initial_ids)
    matches = _scoped_matches(
        parsed, initial_rows, trust_ids=is_self_export, duplicate=on_conflict == "duplicate"
    )
    to_upsert, newly_imported = [], []
    landed: dict[str, UUID] = {}
    updated = conflict_skipped = 0
    written_ids: set[UUID] = set()
    pending: dict[tuple[str, str, str, int], UUID] = {}
    for (chunk, bid), record, match in zip(parsed, bundle.chunks, matches):
        key = _destination_key(chunk)
        if key in pending:
            if bid is not None:
                landed[bid] = pending[key]
            conflict_skipped += 1
            continue
        if match is not None:
            existing_id, writes = match
            if bid is not None:
                landed[bid] = existing_id
            if on_conflict != "update" or not writes:
                conflict_skipped += 1
                continue
            prior = initial_chunks.get(existing_id)
            if prior is None:
                raise ValueError("Import target is unavailable; retry after restoring its source.")
            chunk.id = existing_id
            _carry_local_metadata(
                chunk,
                prior,
                record if namespace is None else {**record, "namespace": namespace},
                legacy=bundle.version != "4",
            )
            updated += 1
        else:
            if preserve_ids and bid and on_conflict != "duplicate":
                candidate = UUID(bid)
                if candidate not in written_ids and not await storage.get_chunks_batch([candidate]):
                    chunk.id = candidate
            newly_imported.append(chunk)
        if chunk.id in written_ids:
            raise invalid("records", "duplicate destination identity")
        written_ids.add(chunk.id)
        pending[_destination_key(chunk)] = chunk.id
        if bid is not None:
            landed[bid] = chunk.id
        to_upsert.append(chunk)

    # Validate the final post-carryover metadata too, before embeddings.
    for index, (chunk, _) in enumerate(parsed):
        try:
            validate_records([_chunk_to_dict(chunk)], [], BUNDLE_VERSION)
        except BundleValidationError as exc:
            raise invalid(f"record {index}", exc.field) from exc
    _scoped_matches(parsed, initial_rows, trust_ids=is_self_export)
    _enforce_import_redaction(
        parsed,
        relations,
        force_unsafe=force_unsafe,
        surface=surface,
        trusted=is_self_export,
        changed_metadata=changed_metadata,
    )
    if indexing_config is not None and indexing_config.hard_max_chunk_tokens:
        from memtomem.chunking.bounded import TokenBudget

        budget = TokenBudget(indexing_config)
        for chunk in to_upsert:
            prefix = chunk.metadata.retrieval_context or " > ".join(
                chunk.metadata.heading_hierarchy
            )
            if (
                budget.count(chunk.content) > budget.body
                or budget.count(prefix) > budget.context
                or budget.count(chunk.retrieval_content, special=True) > budget.model
            ):
                raise ValueError(
                    "Import exceeds configured chunk token budgets. No records imported."
                )

    stats = ImportStats(len(parsed), 0, 0, 0, conflict_skipped_chunks=conflict_skipped)
    if bundle.version != "4":
        stats.warnings.append("legacy_metadata_missing")
    missing, unavailable, checked = set(), set(), set()
    for chunk, _ in parsed:
        if (
            chunk.metadata.origin == ORIGIN_CONSOLIDATION_POLICY
            or chunk.metadata.source_file in checked
        ):
            continue
        checked.add(chunk.metadata.source_file)
        try:
            if not chunk.metadata.source_file.is_file():
                missing.add(str(chunk.metadata.source_file))
        except OSError:
            unavailable.add(str(chunk.metadata.source_file))
    stats.missing_source_files, stats.unavailable_source_files = len(missing), len(unavailable)
    if missing:
        stats.warnings.append("missing_source_files")
    if unavailable:
        stats.warnings.append("unavailable_source_files")
    if to_upsert:
        try:
            embeddings = await embedder.embed_texts([c.retrieval_content for c in to_upsert])
            if len(embeddings) != len(to_upsert):
                raise EmbeddingError("Embedder returned the wrong number of vectors.")
            for chunk, embedding in zip(to_upsert, embeddings):
                chunk.embedding = embedding
        except Exception:
            logger.error("Embedding failed during import")
            stats.failed_chunks = len(to_upsert)
            stats.skipped_relations = len(relations)
            return stats
    edges = _land_relations(
        relations, landed, new_ids=None if is_self_export else {c.id for c in newly_imported}
    )
    if to_upsert or edges:
        try:
            async with storage.transaction(wait=WRITER_WAIT_S):
                current_rows = await storage.get_chunk_rows_by_hashes(hashes)
                current_chunks = await storage.get_chunks_batch(initial_ids)
                if current_rows != initial_rows or current_chunks != initial_chunks:
                    raise ImportChangedError(
                        "Import target changed during preparation. No records imported; retry."
                    )
                new_ids = [c.id for c in newly_imported]
                if await storage.get_chunks_batch(new_ids):
                    raise ImportChangedError(
                        "Import identity changed during preparation. No records imported; retry."
                    )
                if shared:
                    from memtomem import privacy

                    privacy.emit_project_shared_confirmation(surface=surface, mechanism="param")
                if to_upsert:
                    await storage.upsert_chunks(to_upsert)
                    await sync_entities_for_chunks(
                        storage, newly_imported, enabled=extract_entities
                    )
                stats.restored_relations = await storage.insert_relations(edges) if edges else 0
        except (ImportChangedError, EmbeddingPolicyChangedError):
            raise
        except Exception:
            logger.error("Storage write failed during import; transaction rolled back")
            stats.failed_chunks = len(to_upsert)
            stats.failed_relations = len(edges)
            stats.skipped_relations = len(relations) - len(edges)
            return stats
        stats.imported_chunks = len(newly_imported)
        stats.updated_chunks = updated
    stats.skipped_relations = len(relations) - stats.restored_relations
    return stats


def _boundary(meta) -> tuple[str, str | None]:
    return meta.scope, str(meta.project_root) if meta.project_root is not None else None


def _scoped_matches(parsed, rows_by_hash, *, trust_ids, duplicate=False):
    groups = {}
    for index, item in enumerate(parsed):
        groups.setdefault(_boundary(item[0].metadata), []).append((index, item))
    result = [None] * len(parsed)
    for boundary, items in groups.items():
        scoped = {}
        for content_hash, rows in rows_by_hash.items():
            scoped[content_hash] = [r for r in rows if (r.scope, r.project_root) == boundary]
        parts = [item for _, item in items]
        keys = [_destination_key(c) for c, _ in parts]
        for key in keys:
            for row in rows_by_hash.get(key[0], []):
                if (
                    key[1:] == (row.namespace, row.source_file, row.start_line)
                    and (row.scope, row.project_root) != boundary
                ):
                    raise ValueError("Import destination conflicts with another scope or project.")
        if duplicate:
            matches = []
            occupied = {
                (h, r.namespace, r.source_file, r.start_line): r.id
                for h, rows in scoped.items()
                for r in rows
            }
            for key in keys:
                matches.append((occupied[key], False) if key in occupied else None)
        else:
            matches = _match_conflicts(parts, keys, scoped, trust_ids=trust_ids)
        for (index, _), match in zip(items, matches):
            result[index] = match
    return result


def _carry_local_metadata(chunk: Chunk, prior: Chunk, record: dict, *, legacy: bool) -> None:
    if legacy:
        carry = {
            f.name: getattr(prior.metadata, f.name)
            for f in fields(ChunkMetadata)
            if f.name not in record and f.name not in {"source_span_hash", "origin"}
        }
        chunk.metadata = replace(chunk.metadata, **carry)
        for key in ("created_at", "updated_at"):
            if key not in record:
                setattr(chunk, key, getattr(prior, key))
    stable = (
        _destination_key(chunk) == _destination_key(prior)
        and chunk.metadata.end_line == prior.metadata.end_line
        and chunk.content == prior.content
        and chunk.metadata.source_read_only == prior.metadata.source_read_only
    )
    chunk.metadata = replace(
        chunk.metadata,
        origin=prior.metadata.origin,
        source_span_hash=prior.metadata.source_span_hash if stable else None,
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
            parsed.append((str(UUID(source_id)), str(UUID(target_id)), relation_type))
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
    ns = (
        namespace_override if namespace_override is not None else record.get("namespace", "default")
    )
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
        scope=record.get("scope", "user"),
        project_root=Path(record["project_root"])
        if record.get("project_root") is not None
        else None,
        valid_from_unix=record.get("valid_from_unix"),
        valid_to_unix=record.get("valid_to_unix"),
        overlap_before=record.get("overlap_before", 0),
        overlap_after=record.get("overlap_after", 0),
        origin=origin,
    )
    # Normalize here too: UTC conversion must succeed before embedding, even
    # when this decoder is used directly rather than through bundle validation.
    created_at = (
        timestamp(record["created_at"], "record", "created_at")
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
        updated_at=timestamp(record["updated_at"], "record", "updated_at")
        if "updated_at" in record
        else created_at,
    )
    bundle_chunk_id = record.get("chunk_id")
    return chunk, str(UUID(bundle_chunk_id)) if isinstance(bundle_chunk_id, str) else None
