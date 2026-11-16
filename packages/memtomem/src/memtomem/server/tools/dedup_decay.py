"""Tools: mem_dedup_scan, mem_dedup_merge, mem_dedup_remove, mem_decay_scan, mem_decay_expire,
mem_cleanup_orphans."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import UUID

from memtomem.errors import RetryableError, StorageError
from memtomem.indexing.engine import StoredSourceRead, read_stored_source
from memtomem.models import ORIGIN_CONSOLIDATION_POLICY
from memtomem.search.dedup import DIFFERING_FIELDS, differing_fields
from memtomem.server import mcp
from memtomem.server.client_paths import anchor_client_path, client_cwd_of
from memtomem.server.context import AppContext, CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.tools._id_access import caller_boundary, in_boundary
from memtomem.server.tools._provenance import (
    capture_session_for_untracked_write,
    flag_untracked_write,
)
from memtomem.storage.orphan_detect import RetainRule, group_orphans, scan_orphans, source_state
from memtomem.storage.sqlite_backend import SqliteBackend
from memtomem.storage.sqlite_errors import is_writer_busy

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from memtomem.models import Chunk, ChunkTopology
    from memtomem.search.dedup import DedupCandidate


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_dedup_scan(
    threshold: float = 0.92,
    limit: int = 50,
    max_scan: int = 500,
    ctx: CtxType = None,
) -> str:
    """Scan for duplicate chunk candidates (dry-run, no mutations).

    Args:
        threshold: Dense score 1/(1+L2) threshold (0-1, default 0.92)
        limit: Maximum number of candidate pairs to return
        max_scan: Maximum chunks to scan, taken in chunk-id order
    """
    if not 0.0 <= threshold <= 1.0:
        return f"Error: threshold must be between 0 and 1, got {threshold}."
    if not 1 <= limit <= 500:
        return f"Error: limit must be between 1 and 500, got {limit}."
    if max_scan < 1:
        return f"Error: max_scan must be at least 1, got {max_scan}."

    app = await _get_app_initialized(ctx)
    if app.dedup_scanner is None:
        return "DedupScanner not initialized."
    candidates, coverage = await app.dedup_scanner.scan_with_coverage(
        threshold=threshold, limit=limit, max_scan=max_scan
    )
    footer = _dedup_coverage_line(coverage)

    if candidates and app.config.maintenance.experimental_duplicate_brief:
        evidence = await app.dedup_scanner.member_evidence(candidates)
        return _duplicate_brief(candidates, evidence, threshold, footer)

    if not candidates:
        return f"No duplicate chunks found (threshold={threshold}).\n{footer}"

    parts: list[str] = [f"Duplicate candidates: {len(candidates)} pairs (threshold={threshold}):\n"]
    for i, c in enumerate(candidates, 1):
        kind = "exact duplicate" if c.exact else f"score={c.score:.4f}"
        meta_a = c.chunk_a.metadata
        meta_b = c.chunk_b.metadata
        preview_a = c.chunk_a.content[:80].replace("\n", " ")
        preview_b = c.chunk_b.content[:80].replace("\n", " ")
        parts.append(
            f"[{i}] {kind}\n"
            f'  A ({c.chunk_a.id}): {meta_a.source_file}:{meta_a.start_line} — "{preview_a}"\n'
            f'  B ({c.chunk_b.id}): {meta_b.source_file}:{meta_b.start_line} — "{preview_b}"'
        )

    parts.append(footer)
    return "\n".join(parts)


def _dedup_coverage_line(coverage) -> str:
    """Say what the scan looked at, so an empty result is not read as a clean store.

    The first clause is about pool selection only: "all" means no scannable
    chunk was left out of the pool, not that every duplicate was looked for.
    """
    if coverage.pool < coverage.eligible:
        # The rule and when it repeats, not what the subset is like: id order
        # promises neither a spread over files nor anything about age.
        line = (
            f"Scanned {coverage.pool} of {coverage.eligible} scannable chunks "
            f"(the first {coverage.pool} in chunk-id order: the same chunks on every run "
            "while the scannable chunks and max_scan stay the same; "
            "raise max_scan to cover more)"
        )
    else:
        noun = "chunk" if coverage.pool == 1 else "chunks"
        line = f"Scanned all {coverage.pool} scannable {noun}"
    if not coverage.near_search_enabled:
        return f"{line}; near-duplicate search is off (BM25-only store)."
    line += f"; near-duplicate search probed {coverage.probed}"
    if coverage.without_vector:
        line += f", {coverage.without_vector} had no usable stored vector and were not probed"
    return f"{line}."


#: Says what was compared: two members can still differ in what is not, such
#: as the body of a near pair or the read-only flag printed on the line above.
_NO_DIFFERENCE = f"none (compared: {', '.join(DIFFERING_FIELDS)})"


def _duplicate_brief(
    candidates: Sequence[DedupCandidate],
    evidence: dict[UUID, tuple[Chunk, ChunkTopology]],
    threshold: float,
    footer: str,
) -> str:
    """The scan's pairs with what the store holds about each member.

    Every member line is rendered from *evidence*, the read made after the
    scan, so a member's facts describe one moment. A pair is left out when
    that read does not return one of its members, or when an exact pair's
    members do not hash the same. The scan supplies the pairing, its order
    and a near pair's score, which is not recomputed.
    """
    pairs = []
    for candidate in candidates:
        a = evidence.get(candidate.chunk_a.id)
        b = evidence.get(candidate.chunk_b.id)
        if a is None or b is None:
            continue
        if candidate.exact and a[0].content_hash != b[0].content_hash:
            continue
        pairs.append((candidate, a, b))

    tail = [footer]
    omitted = len(candidates) - len(pairs)
    if omitted:
        tail.insert(
            0,
            # Present tense, not "since the scan": a pair can be dropped with
            # nothing having changed. The scan's near phase takes neighbours
            # under the search's row-level hold rule, and the evidence read
            # uses the listing's path-level one, which also hides an ordinary
            # chunk filed on a hidden policy summary's path.
            f"{omitted} pair{'' if omitted == 1 else 's'} omitted: a member is not stored "
            "now, its source is unavailable, or the bodies of an exact pair do not match.",
        )
    if not pairs:
        return "\n".join([f"No duplicate chunks found (threshold={threshold}).", *tail])

    parts = [f"Duplicate candidates: {len(pairs)} pairs (threshold={threshold}):\n"]
    for i, (candidate, a, b) in enumerate(pairs, 1):
        kind = "exact duplicate" if candidate.exact else f"score={candidate.score:.4f}"
        differs = differing_fields(a[0].metadata, b[0].metadata)
        parts.append(
            f"[{i}] {kind}\n"
            f"{_brief_member('A', *a)}\n"
            f"{_brief_member('B', *b)}\n"
            f"  differs: {', '.join(differs) or _NO_DIFFERENCE}"
        )
    return "\n".join([*parts, *tail])


def _brief_member(label: str, chunk: Chunk, topology: ChunkTopology) -> str:
    meta = chunk.metadata
    preview = _quoted(chunk.content[:80].replace("\n", " "))
    scope = _quoted(meta.scope)
    if meta.project_root:
        scope += f" ({_quoted(str(meta.project_root))})"
    record = [
        f"heading={' > '.join(map(_quoted, meta.heading_hierarchy)) or '(none)'}",
        f"namespace={_quoted(meta.namespace)}",
        f"scope={scope}",
        f"tags={', '.join(map(_quoted, meta.tags)) or '(none)'}",
        f"valid={_brief_validity(meta.valid_from_unix, meta.valid_to_unix)}",
    ]

    # "stored flag": what the index recorded, not what the configuration says
    # now. ``mem_delete`` asks the configuration itself.
    source = ["read-only (stored flag)" if meta.source_read_only else "writable (stored flag)"]
    if meta.redaction_count:
        source.append("masked projection")
    if topology.at_source_start is None:
        source.append("line span not recorded")
    else:
        if topology.at_source_start:
            source.append("no sibling starts on an earlier line")
        if topology.span_shared:
            source.append("line span overlaps a sibling chunk")
    if topology.only_in_source:
        source.append("only chunk of its source")

    links = []
    if meta.origin == ORIGIN_CONSOLIDATION_POLICY:
        links.append("policy summary")
    if topology.consolidated_into:
        links.append(f"covered by {_count(topology.consolidated_into, 'summary', 'summaries')}")
    if topology.consolidates:
        links.append(f"summary of {_count(topology.consolidates, 'chunk', 'chunks')}")
    if topology.other_relations:
        links.append(_count(topology.other_relations, "other relation", "other relations"))
    if topology.links_out:
        links.append(
            "link source: " + ", ".join(f"{_quoted(kind)} x{n}" for kind, n in topology.links_out)
        )
    if topology.links_in:
        links.append("link target: " + ", ".join(map(_quoted, topology.links_in)))

    return (
        f"  {label} ({chunk.id}): {_quoted(str(meta.source_file))}:{meta.start_line} — {preview}\n"
        f"     record: {' | '.join(record)}\n"
        f"     source: {' | '.join(source)}\n"
        f"     links: {' | '.join(links) or 'none'}"
    )


def _count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _quoted(text: str) -> str:
    """*text* in double quotes, on one line.

    Headings, tags, namespaces, paths and bodies are whatever a file or a
    caller stored. Quoted, with the quote, the backslash and every
    non-printable character escaped, such a value cannot end its own field,
    pass for the next one, or start a line that reads as evidence.
    ``str.isprintable`` is false for the line breaks ``splitlines`` honours
    beyond ``\\n`` (U+0085, U+2028) as well as for control characters.
    """
    escaped = (
        "\\" + ch
        if ch in '"\\'
        else ch
        if ch.isprintable()
        else ch.encode("unicode_escape").decode("ascii")
        for ch in text
    )
    return f'"{"".join(escaped)}"'


_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _brief_validity(valid_from: int | None, valid_to: int | None) -> str:
    def instant(unix: int) -> str:
        # To the second: two bounds a day apart or ten seconds apart must not
        # read the same. A stored bound is any signed 64-bit integer (a bundle
        # import accepts the full range), most of which no calendar reaches;
        # ``fromtimestamp`` would also refuse a negative one on Windows.
        try:
            return (_EPOCH + timedelta(seconds=unix)).strftime("%Y-%m-%dT%H:%M:%SZ")
        except OverflowError:
            return f"unix {unix}"

    bounds = []
    if valid_from is not None:
        bounds.append(f"from {instant(valid_from)}")
    if valid_to is not None:
        bounds.append(f"until {instant(valid_to)}")
    return " ".join(bounds) or "always"


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_dedup_merge(
    keep_id: str,
    delete_ids: list[str],
    dry_run: bool = True,
    ctx: CtxType = None,
) -> str:
    """Merge duplicate chunks: keep keep_id, delete delete_ids.

    Tags from deleted chunks are merged into the kept chunk.

    Defaults to dry_run=True for safety -- set dry_run=False to actually
    delete. Mirrors the safety-first defaults on mem_decay_expire and
    mem_cleanup_orphans so a wrong UUID in a maintenance script can't
    silently destroy data on first call.

    Args:
        keep_id: UUID of the chunk to keep
        delete_ids: UUIDs of chunks to delete
        dry_run: If True (default), preview without making any changes.
    """
    app = await _get_app_initialized(ctx)
    if app.dedup_scanner is None:
        return "DedupScanner not initialized."
    try:
        keep_uuid = UUID(keep_id)
        delete_uuids = [UUID(d) for d in delete_ids]
    except ValueError as exc:
        return f"Invalid UUID: {exc}"

    # Same reason as ``mem_cleanup_orphans``: a merged-away chunk may be
    # one an earlier provenance event still names.
    provenance_session_id = await capture_session_for_untracked_write(app)
    try:
        would_or_did = await app.dedup_scanner.merge(keep_uuid, delete_uuids, dry_run=dry_run)
    except (Exception, asyncio.CancelledError):
        # ``merge`` commits the kept chunk's merged tags and the deletes
        # together, so a failed apply normally leaves nothing written. The
        # invalidation stays anyway (#2157): it costs a cache rebuild and does
        # not depend on where the failure happened. A dry run writes nothing
        # however it ends.
        if not dry_run:
            app.search_pipeline.invalidate_cache()
        raise
    if dry_run:
        return (
            f"Merge preview (dry-run): {would_or_did} chunks would be deleted, "
            f"keep_id={keep_id}\n(no changes -- re-run with dry_run=False to apply)"
        )
    app.search_pipeline.invalidate_cache()
    if would_or_did:
        await flag_untracked_write(app, provenance_session_id)
    return f"Merge complete: {would_or_did} chunks deleted, keep_id={keep_id}"


_REINDEX_REMEDY = (
    "Run `mm index <file>` on it (plain, not `--force`, which records no receipt), "
    "then mem_dedup_scan again."
)
_JUDGE_REMEDY = "Read the file(s) and remove the copy with mem_delete."
_HELD_REMEDY = (
    "Clear the hold first: a file whose project tier is no longer registered needs that tier "
    "restored, or the file moved where it belongs; otherwise a plain `mm index <file>` checks "
    "it again. Then mem_dedup_scan again."
)
_WRITER_BUSY = "the store's writer is busy; no lines were removed. Retry."


@asynccontextmanager
async def _busy_is_retryable() -> AsyncIterator[None]:
    """A busy writer refuses retryably, worded for this tool rather than ``mem_delete``."""
    try:
        yield
    except StorageError as exc:
        if not is_writer_busy(exc):
            raise
        raise RetryableError(_WRITER_BUSY) from exc


def _refusal(role: str, key: str, problem: str, remedy: str) -> str:
    return f"Error: the {role} member's file {key} {problem}; no lines were removed. {remedy}"


def _read_members(keys: list[tuple[str, str]]) -> list[StoredSourceRead] | str:
    """Read each member's file as the indexer would, or return the first refusal."""
    from memtomem.storage.sqlite_helpers import stored_key_moved

    reads: list[StoredSourceRead] = []
    for role, key in keys:
        if stored_key_moved(key):
            return _refusal(role, key, "now resolves to another file", _JUDGE_REMEDY)
        read = read_stored_source(key)
        if isinstance(read, str):
            return _refusal(role, key, read, _REINDEX_REMEDY)
        if read["inode"] == 0:
            return _refusal(
                role,
                key,
                "is on a filesystem that reports no file identity, so it cannot be checked",
                _JUDGE_REMEDY,
            )
        if read["links"] > 1:
            return _refusal(
                role,
                key,
                "has another hard-linked name, through which it can change unchecked",
                _JUDGE_REMEDY,
            )
        reads.append(read)
    return reads


async def _check_pair(app: AppContext, keep_uuid: UUID, keep_id: str, removed: Chunk) -> str | None:
    """Refuse unless both members' files and rows still match their index receipts.

    Runs under the removed file's locks (``_delete_chunk``'s precheck). The kept
    file is read, not locked: a change to it after this check is not seen. The
    files are read first, in a thread; the store is then read on the writer in
    one transaction, so holds, receipts and rows are one committed state. A
    change landing between the two reads makes them disagree, which refuses.
    """
    from memtomem.indexing.source_receipt import receipt_as_stored, state_as_stored
    from memtomem.storage.base import WRITER_WAIT_S

    boundary = caller_boundary(app)
    async with _busy_is_retryable():
        keeper = await app.storage.get_chunk(keep_uuid, exclude_withheld=True, read_writer=True)
    if keeper is None or not in_boundary(keeper, boundary):
        return f"Error: chunk {keep_id} not found."
    if _one_file(removed, keeper):
        return _same_file_refusal(keeper)
    keys = [
        ("removed", str(removed.metadata.source_file)),
        ("kept", str(keeper.metadata.source_file)),
    ]
    reads = await asyncio.to_thread(_read_members, keys)
    if isinstance(reads, str):
        return reads
    if (reads[0]["device"], reads[0]["inode"]) == (reads[1]["device"], reads[1]["inode"]):
        return _same_file_refusal(keeper)

    indexing = app.config.indexing
    no_receipt_remedy = (
        "Index passes with indexing.enrich_chunk_context or indexing.auto_summarize on "
        "record no receipt, so this file cannot be checked here. " + _JUDGE_REMEDY
        if indexing.enrich_chunk_context or indexing.auto_summarize
        else _REINDEX_REMEDY
    )
    storage = cast("SqliteBackend", app.storage)
    dimension = app.embedder.dimension
    async with _busy_is_retryable(), app.storage.transaction(wait=WRITER_WAIT_S):
        current = await app.storage.get_chunk(keep_uuid, exclude_withheld=True, read_writer=True)
        if current is None or not in_boundary(current, boundary):
            return f"Error: chunk {keep_id} not found."
        if str(current.metadata.source_file) != keys[1][1]:
            raise RetryableError(
                "the kept member moved while it was checked; no lines were removed. Retry."
            )
        db = storage._get_db()
        for (role, key), read in zip(keys, reads, strict=True):
            if await app.storage.is_source_held(Path(key)):
                return _refusal(role, key, "is held by the index", _HELD_REMEDY)
            receipt = receipt_as_stored(db, key)
            if receipt is None:
                return _refusal(role, key, "has no index receipt", no_receipt_remedy)
            if read["content_hash"] != receipt[0]:
                return _refusal(role, key, "changed since it was indexed", _REINDEX_REMEDY)
            rows = state_as_stored(db, key, dimension)
            if rows is None or rows[0] != receipt[1]:
                return _refusal(
                    role, key, "has stored rows that differ from its last index", _REINDEX_REMEDY
                )
    return None


def _one_file(a: Chunk, b: Chunk) -> bool:
    return AppContext.memory_file_lock_key(a.metadata.source_file) == (
        AppContext.memory_file_lock_key(b.metadata.source_file)
    )


def _same_file_refusal(keeper: Chunk) -> str:
    return (
        f"Error: both members are in one file ({keeper.metadata.source_file}); no lines "
        f"were removed. {_JUDGE_REMEDY}"
    )


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_dedup_remove(
    remove_id: str,
    keep_id: str,
    confirm_project_shared: bool = False,
    ctx: CtxType = None,
) -> str:
    """Remove one member of a duplicate pair from its file, after checking both.

    Removes remove_id's lines from its file and re-indexes it, as mem_delete does,
    only when both members' files and rows still match what was last indexed, the
    two are in different files, and neither file has another hard-linked name.
    It does not judge whether the two say the same thing: read both first. A change
    to the kept file after the check is not seen.

    Args:
        remove_id: UUID of the chunk whose lines are removed
        keep_id: UUID of the duplicate that stays
        confirm_project_shared: Required when remove_id is a project_shared chunk
    """
    from memtomem.server.tools.memory_crud import _delete_chunk

    app = await _get_app_initialized(ctx)
    try:
        remove_uuid = UUID(remove_id)
        keep_uuid = UUID(keep_id)
    except ValueError as exc:
        return f"Error: invalid chunk ID format: {exc}"
    if remove_uuid == keep_uuid:
        return "Error: remove_id and keep_id are the same chunk; no lines were removed."
    if not isinstance(app.storage, SqliteBackend):
        return "Error: mem_dedup_remove needs the SQLite storage backend."
    boundary = caller_boundary(app)
    keeper = await app.storage.get_chunk(keep_uuid, exclude_withheld=True)
    if keeper is None or not in_boundary(keeper, boundary):
        return f"Error: chunk {keep_id} not found."
    removed = await app.storage.get_chunk(remove_uuid, exclude_withheld=True)
    if removed is not None and in_boundary(removed, boundary) and _one_file(removed, keeper):
        return _same_file_refusal(keeper)

    async def precheck(chunk: Chunk) -> str | None:
        return await _check_pair(app, keep_uuid, keep_id, chunk)

    return await _delete_chunk(
        app,
        remove_uuid,
        remove_id,
        confirm_project_shared,
        surface="mem_dedup_remove",
        precheck=precheck,
    )


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_decay_scan(
    max_age_days: float = 90,
    source_filter: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Preview chunks that would be expired by TTL (dry-run, no deletions).

    Args:
        max_age_days: Chunks older than this many days are listed as candidates.
        source_filter: Only scan chunks from sources containing this substring.
    """
    if max_age_days <= 0:
        return f"Error: max_age_days must be positive, got {max_age_days}."

    from memtomem.search.decay import expire_chunks

    app = await _get_app_initialized(ctx)
    stats = await expire_chunks(
        app.storage, max_age_days=max_age_days, dry_run=True, source_filter=source_filter
    )
    return (
        f"Expiry scan (max_age_days={max_age_days}, dry-run):\n"
        f"- Total chunks:   {stats.total_chunks}\n"
        f"- Expired:        {stats.expired_chunks}\n"
        f"(no deletions -- use mem_decay_expire to delete)"
    )


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_decay_expire(
    max_age_days: float = 90,
    source_filter: str | None = None,
    dry_run: bool = True,
    ctx: CtxType = None,
) -> str:
    """Delete chunks older than max_age_days from the index.

    Defaults to dry_run=True for safety -- set dry_run=False to actually delete.

    Args:
        max_age_days: Chunks older than this many days are expired.
        source_filter: Only expire chunks from sources containing this substring.
        dry_run: If True (default), preview without making any changes.
    """
    if max_age_days <= 0:
        return f"Error: max_age_days must be positive, got {max_age_days}."

    from memtomem.search.decay import expire_chunks

    app = await _get_app_initialized(ctx)
    provenance_session_id = await capture_session_for_untracked_write(app)
    stats = await expire_chunks(
        app.storage, max_age_days=max_age_days, dry_run=dry_run, source_filter=source_filter
    )
    if not dry_run and stats.deleted_chunks > 0:
        app.search_pipeline.invalidate_cache()
        await flag_untracked_write(app, provenance_session_id)
    mode = " (dry-run)" if dry_run else ""
    return (
        f"Memory expiry{mode}:\n"
        f"- Total chunks:   {stats.total_chunks}\n"
        f"- Expired:        {stats.expired_chunks}\n"
        f"- Deleted:        {stats.deleted_chunks}"
    )


def _under_dirs(under: object) -> list[str]:
    """Validate ``under`` as passed through ``mem_do``, which does not type-check it.

    An empty value is refused rather than read as "no filter": a filter that
    went missing on the way would otherwise purge every orphan.
    """
    if under is None:
        return []
    dirs = [under] if isinstance(under, str) else under
    if (
        not isinstance(dirs, list)
        or not dirs
        or not all(isinstance(d, str) and d.strip() for d in dirs)
    ):
        raise ValueError("under must be a directory path or a non-empty list of them")
    return dirs


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_cleanup_orphans(
    dry_run: bool = True,
    ctx: CtxType = None,
    *,
    confirm_purge: bool = False,
    under: str | list[str] | None = None,
) -> str:
    """Find and remove orphaned chunks whose source files no longer exist.

    Deletion requires both dry_run=False and confirm_purge=True. Orphans are
    listed by the memory dir that owns them; some dirs belong to tools that
    delete their own files, leaving the indexed chunks as the only copy.

    Args:
        dry_run: If True (default), only list orphaned files without deleting.
        confirm_purge: Explicit opt-in to erase confirmed-missing sources when dry_run is False.
        under: Only consider sources under these directories (a path or a list).
    """
    under_dirs = _under_dirs(under)
    app = await _get_app_initialized(ctx)
    if client_cwd_of(app) is not None:
        # Anchored, not resolved: ``group_orphans`` resolves each entry once,
        # in its own way, and resolving twice is not always resolving once.
        under_dirs = [str(anchor_client_path(app, d)) for d in under_dirs]
    # Two-pass scan keeps transient absence out of the explicit purge list.
    # A mass ratio cannot prove deletion or availability (#2498).
    result = await scan_orphans(app.storage)
    rule = await asyncio.to_thread(RetainRule.prepare, app.config.indexing)
    archived, candidates = rule.split(result.confirmed_orphans)
    groups, excluded = await asyncio.to_thread(
        group_orphans,
        candidates,
        app.config.indexing.all_index_roots(),
        under=under_dirs,
    )
    archived_note = (
        f"{len(archived)} archived source(s) under retain_missing_dirs are kept."
        if archived
        else ""
    )
    orphaned = [source for group in groups for source in group.sources]

    if not orphaned:
        if excluded:
            message = (
                f"No orphaned chunks under the given directories ({excluded} orphaned elsewhere)."
            )
        else:
            message = f"No orphaned chunks found ({result.total_sources} source files checked)."
        if archived_note:
            message += f" {archived_note}"
        if result.unavailable_sources:
            message += f" {len(result.unavailable_sources)} source(s) unavailable and not eligible for purge."
        return message

    # ``mem_do`` passes params through at runtime. Only actual JSON booleans
    # may opt into deletion; the string "false" must stay a preview.
    if dry_run is not False or confirm_purge is not True:
        lines = [f"Orphaned files: {len(orphaned)} (dry-run, no deletions)"]
        for group in groups:
            lines.append(f"\n{group.label} ({len(group.sources)}):")
            lines.extend(f"  {sf}" for sf in group.sources)
        if excluded:
            lines.append(f"\n{excluded} more orphaned file(s) are outside the given directories.")
        if archived_note:
            lines.append(f"\n{archived_note}")
        if result.unavailable_sources:
            lines.append(
                f"\n{len(result.unavailable_sources)} additional source(s) could not be checked."
            )
        lines.append("\nSet dry_run=False and confirm_purge=True to delete these chunks.")
        return "\n".join(lines)

    # Same reason as ``mem_ns_delete``: a reaped chunk may be one an
    # earlier provenance event named.
    provenance_session_id = await capture_session_for_untracked_write(app)
    total_deleted = 0
    skipped = 0
    for sf in orphaned:
        if await asyncio.to_thread(source_state, sf) != "missing":
            skipped += 1
            continue
        deleted = await app.storage.delete_by_source(sf, as_stored=True)
        total_deleted += deleted

    if total_deleted > 0:
        app.search_pipeline.invalidate_cache()
        await flag_untracked_write(app, provenance_session_id)

    message = (
        f"Cleanup complete:\n- Orphaned files: {len(orphaned)}"
        f"\n- Recovered or unavailable: {skipped}\n- Chunks deleted: {total_deleted}"
    )
    return f"{message}\n{archived_note}" if archived_note else message
