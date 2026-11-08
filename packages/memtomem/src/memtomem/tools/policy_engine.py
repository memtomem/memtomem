"""Policy execution engine — run lifecycle policies on memories."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import UUID

from memtomem.constants import InvalidNameError, validate_writable_namespace
from memtomem.storage.base import WRITER_WAIT_S
from memtomem.storage.sqlite_helpers import escape_like
from memtomem.storage.sqlite_visibility import VISIBLE_SOURCE_PATH

if TYPE_CHECKING:
    from memtomem.llm.base import LLMProvider
    from memtomem.storage.sqlite_backend import SqliteBackend

logger = logging.getLogger(__name__)

_NS_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

_VALID_TYPES = {"auto_archive", "auto_promote", "auto_expire", "auto_consolidate", "auto_tag"}


# Cap on how many chunk ids a single run records, per id list. Scheduler runs
# are already bounded by ``max_actions_per_run``; only a manual run can exceed
# this. Truncation is reported, never silent — see ``_record_ids``.
_MAX_RECORDED_IDS = 2000


def _record_ids(outcome: dict, key: str, ids: list[str]) -> None:
    """Store ``ids`` under ``key``, capped, noting how many were omitted."""
    outcome[key] = [str(i) for i in ids[:_MAX_RECORDED_IDS]]
    if len(ids) > _MAX_RECORDED_IDS:
        outcome[f"{key}_omitted"] = len(ids) - _MAX_RECORDED_IDS


@dataclass(frozen=True)
class PolicyRunResult:
    policy_name: str
    policy_type: str
    affected_count: int
    dry_run: bool
    details: str
    # Audit payload for the maintenance run log (#2132). Defaulted so the five
    # positional fields above keep their existing construction sites. ``frozen``
    # only forbids mutation of the fields themselves; the dict is never hashed.
    namespaces: tuple[str, ...] = ()
    outcome: dict = field(default_factory=dict)
    #: Handler-reported failure that did not raise (the handler still returns a
    #: result whose ``details`` explains it). Maps to a ``status=error`` row.
    error: str | None = None
    #: Set by ``run_policy`` when a run row was written.
    run_id: int | None = None
    #: #2157: the cache-invalidation signal. It is deliberately conservative:
    #: ``False`` guarantees this run wrote nothing search-visible, while
    #: ``True`` means it wrote — or may have written — and the caller must
    #: drop its search cache. Handlers arm it before a write rather than
    #: confirm one after, because the paths that need it are exactly the ones
    #: where the evidence is lost with an exception.
    #: ``affected_count`` cannot answer this — auto_tag's count is a pre-scan
    #: estimate that can be positive for a run that wrote nothing, and a
    #: handler that raises mid-write has no count to report at all. (The
    #: original #2157 example, auto_consolidate committing a stale-summary
    #: delete and then failing to regenerate, is gone since #2158 made that
    #: path atomic — the flag now over-reports there, which is the safe
    #: direction.) Long-lived callers (the policy scheduler)
    #: gate their ``SearchPipeline.invalidate_cache()`` on this flag, mirroring
    #: ``IndexingStats.mutated`` (#2141).
    #:
    #: ``None`` means the handler did not answer, and ``run_policy`` derives
    #: the value from the counter — correct for the handlers whose counter is
    #: a real write count. A handler that sets it explicitly is believed, so
    #: an explicit ``False`` is not overridden by a non-zero counter.
    #: Appended last for positional construction compatibility.
    mutated: bool | None = None


def _resolve_archive_ns(template: str, tags_json: str | None, fallback: str) -> str:
    """Expand the ``{first_tag}`` placeholder in ``archive_namespace_template``.

    Empty / non-string / invalid tags fall back to ``"misc"``. Characters that
    are not namespace-safe (alphanumerics, dot, dash, underscore) are replaced
    with ``_``. If ``template`` has no placeholder it is returned verbatim, or
    ``fallback`` when ``template`` is empty.
    """
    if not template:
        return fallback
    if "{first_tag}" not in template:
        return template

    first_tag = "misc"
    if tags_json:
        try:
            tags = json.loads(tags_json)
            if isinstance(tags, list) and tags and isinstance(tags[0], str) and tags[0].strip():
                first_tag = tags[0].strip()
        except (json.JSONDecodeError, TypeError):
            pass

    first_tag = _NS_SAFE_RE.sub("_", first_tag) or "misc"
    return template.replace("{first_tag}", first_tag)


# The config key each policy type moves or writes chunks into (#2949). The
# archive template is judged on its own: its literal text here, each resolved
# target per chunk in ``execute_auto_archive``.
_TARGET_KEYS: dict[str, str] = {
    "auto_archive": "archive_namespace",
    "auto_promote": "target_namespace",
    "auto_consolidate": "summary_namespace",
}


def _target_refusal(label: str, value: object) -> str | None:
    try:
        validate_writable_namespace(value)
    except InvalidNameError as exc:
        return f"{label}: {exc}"
    return None


_JSON_TYPE_NAMES: dict[type, str] = {
    list: "an array",
    str: "a string",
    bool: "a boolean",
    int: "a number",
    float: "a number",
    type(None): "null",
}


def config_shape_error(config: object) -> str | None:
    """Why *config* cannot be a policy config, or ``None`` when it can (#2955).

    Every handler reads its settings with ``config.get``, so a config must be
    a JSON object. ``mem_policy_add`` refuses anything else, and
    ``run_policy`` refuses to run a row stored before that check.
    """
    if isinstance(config, dict):
        return None
    kind = _JSON_TYPE_NAMES.get(type(config), type(config).__name__)
    return f"config must be a JSON object, got {kind}"


def policy_target_errors(policy_type: str, config: object) -> list[str]:
    """Why the namespaces *config* names as write targets fail the write gate.

    The gate is :func:`validate_writable_namespace`: a policy must not move
    chunks into a name a search cannot address by that exact name (#2949).
    Only keys *policy_type* reads are judged, and a key left out is not: its
    default passes. ``archive_namespace_template`` is judged with
    ``{first_tag}`` replaced by ``x``, the same ``str.replace`` the resolver
    does, so its literal text is what is judged. A non-empty template never
    selects ``archive_namespace``, so the fallback is judged only without one
    (``None``) or with an empty one. ``mem_policy_add`` refuses a policy on
    these; the handlers refuse to run a stored one.
    """
    key = _TARGET_KEYS.get(policy_type)
    if key is None or not isinstance(config, dict):
        return []
    if policy_type == "auto_archive":
        template = config.get("archive_namespace_template")
        if template is not None and not isinstance(template, str):
            return [
                "archive_namespace_template: expected a string or null, "
                f"got {type(template).__name__}"
            ]
        if template:
            sample = template.replace("{first_tag}", "x")
            refusal = _target_refusal(f"archive_namespace_template {template!r}", sample)
            return [refusal] if refusal else []
    if key in config:
        refusal = _target_refusal(key, config[key])
        if refusal:
            return [refusal]
    return []


def _refused_target_result(policy_type: str, dry_run: bool, errors: list[str]) -> PolicyRunResult:
    message = "; ".join(errors)
    return PolicyRunResult(
        policy_name="",
        policy_type=policy_type,
        affected_count=0,
        dry_run=dry_run,
        details=f"Error: {message}",
        error=message,
    )


# Columns read for every row of a file a move policy considers.
_MOVE_ROW_COLS = "c.id, c.source_file, c.namespace, c.tags, c.content_hash, c.start_line"


def _rows_of_eligible_files(
    db: sqlite3.Connection, having: str, params: list
) -> dict[str, list[tuple]]:
    """Every row of each visible source whose rows together pass ``having``.

    A move policy decides per file, not per chunk: moving part of a file
    leaves it in two namespaces, which a forced re-index then refuses (and a
    plain one resolves only for the rows it rewrites). ``having`` aggregates
    over all of a file's rows, in every namespace. The hold is judged here,
    in the statement that reads the rows; a live run reads under the write
    lock it then writes with.
    """
    rows = db.execute(
        "WITH files AS (SELECT source_file FROM chunks "  # nosec B608
        f"WHERE {VISIBLE_SOURCE_PATH} GROUP BY source_file HAVING {having}) "
        f"SELECT {_MOVE_ROW_COLS} FROM chunks c JOIN files f ON c.source_file = f.source_file "
        "ORDER BY c.source_file, c.start_line, c.rowid",
        params,
    ).fetchall()
    by_file: dict[str, list[tuple]] = {}
    for row in rows:
        by_file.setdefault(row[1], []).append(row)
    return by_file


def _colliding_rows(rows: list[tuple]) -> int:
    """How many rows of one file would break the uniqueness index in one namespace.

    ``chunks`` is ``UNIQUE(namespace, source_file, content_hash, start_line)``.
    A planned move puts every row of the file in one namespace, so it fails
    exactly when two rows share ``(content_hash, start_line)``, wherever they
    are now: one already in the target, or two moving from different
    namespaces. SQLite never treats NULLs as equal in a unique index, so a
    NULL key part cannot collide.
    """
    seen: set[tuple] = set()
    dupes = 0
    for row in rows:
        key = (row[4], row[5])
        if None in key:
            continue
        if key in seen:
            dupes += 1
        seen.add(key)
    return dupes


def _move_outcome(
    ids_by_target: dict[str, list[str]],
    conflicts: list[dict],
    split: list[str],
    refused_counts: dict[str, int],
) -> dict:
    # A list of fixed-key records, not ``{namespace: ids}``: a namespace is
    # user-supplied text and would collide with the omission bookkeeping.
    moved: list[dict] = []
    for target, ids in sorted(ids_by_target.items()):
        record: dict = {"target": target}
        _record_ids(record, "ids", ids)
        record["omitted"] = record.pop("ids_omitted", 0)
        moved.append(record)
    outcome: dict = {"moved": moved} if moved else {}
    if refused_counts:
        outcome["refused"] = [
            {"target": target, "count": n} for target, n in sorted(refused_counts.items())
        ]
    if conflicts:
        outcome["conflicts"] = conflicts
    if split:
        outcome["skipped"] = [{"source": source, "reason": "split_targets"} for source in split]
    return outcome


def _conflict_error(conflicts: list[dict]) -> str:
    return (
        f"{len(conflicts)} file(s) not moved: rows with the same content at the same line "
        "would collide in one namespace"
    )


async def execute_auto_archive(
    storage: SqliteBackend,
    config: dict,
    namespace: str | None,
    dry_run: bool,
) -> PolicyRunResult:
    """Move source files matching an aging rule to an archive namespace.

    A file moves whole, and only when every one of its chunks matches the
    rule: archiving part of a file would leave it in two namespaces. Held and
    pending sources are not candidates.

    Config fields (all but ``max_age_days`` are optional):

    - ``max_age_days`` (int, default 30): chunks older than this many days
      match the age rule.
    - ``archive_namespace`` (str, default ``"archive"``): single target
      namespace when ``archive_namespace_template`` is not set. Also acts as
      the fallback if the template is empty.
    - ``age_field`` (str, default ``"created_at"``): ``"created_at"`` or
      ``"last_accessed_at"``. For ``"last_accessed_at"``, null values fall
      back to ``created_at`` via ``COALESCE``.
    - ``min_access_count`` (int | None, default None): a chunk matches only
      when its ``access_count`` is at most this value. None disables the filter.
    - ``max_importance_score`` (float | None, default None): a chunk matches
      only when its ``importance_score`` is strictly below this value. None
      disables the filter.
    - ``archive_namespace_template`` (str | None, default None): target
      namespace template. Supports the ``{first_tag}`` placeholder, which
      expands to each chunk's first tag (or ``"misc"`` when tags are empty).
      A file whose chunks resolve to different targets is skipped and
      reported (``split_targets``). Chunks already in the target stay.

    With a policy ``namespace`` filter, a file is a candidate only when each
    of its chunks is in that namespace or already in the target. A file whose
    move would put two rows with the same content at the same line in one
    namespace is not moved and is reported as a conflict.
    """
    max_age = config.get("max_age_days", 30)
    archive_ns = config.get("archive_namespace", "archive")
    age_field = config.get("age_field", "created_at")
    min_access_count = config.get("min_access_count")
    max_importance_score = config.get("max_importance_score")
    ns_template = config.get("archive_namespace_template")

    if age_field not in ("created_at", "last_accessed_at"):
        return PolicyRunResult(
            policy_name="",
            policy_type="auto_archive",
            affected_count=0,
            dry_run=dry_run,
            details=(
                f"Error: age_field must be 'created_at' or 'last_accessed_at', got {age_field!r}"
            ),
            error=f"age_field must be 'created_at' or 'last_accessed_at', got {age_field!r}",
        )

    target_errors = policy_target_errors("auto_archive", config)
    if target_errors:
        return _refused_target_result("auto_archive", dry_run, target_errors)

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age)).isoformat()

    # The per-chunk rule; a file is eligible when all of its rows match.
    matches: list[str] = []
    params: list = []
    if age_field == "last_accessed_at":
        matches.append("COALESCE(last_accessed_at, created_at) < ?")
    else:
        matches.append("created_at < ?")
    params.append(cutoff)
    if min_access_count is not None:
        matches.append("access_count <= ?")
        params.append(min_access_count)
    if max_importance_score is not None:
        matches.append("importance_score < ?")
        params.append(max_importance_score)
    having = f"SUM(CASE WHEN {' AND '.join(matches)} THEN 1 ELSE 0 END) = COUNT(*)"
    if namespace:
        # In the filter's namespace or already archived. Template targets are
        # per chunk, so that case is judged per file below.
        if ns_template is None:
            having += " AND SUM(CASE WHEN namespace IN (?, ?) THEN 1 ELSE 0 END) = COUNT(*)"
            params.extend([namespace, archive_ns])
        having += " AND SUM(CASE WHEN namespace = ? THEN 1 ELSE 0 END) > 0"
        params.append(namespace)

    # Template targets come from tag data, so a template that passed the check
    # above can still resolve to a refused name (tag ``agent-runtime`` under
    # ``{first_tag}``). Those files stay where they are (#2949).
    refusal_by_target: dict[str, str | None] = {}

    def plan(db: sqlite3.Connection) -> tuple[dict, set, dict, list, list]:
        ids_by_target: dict[str, list[str]] = {}
        source_namespaces: set[str] = set()
        refused_counts: dict[str, int] = {}
        conflicts: list[dict] = []
        split: list[str] = []
        for source, rows in _rows_of_eligible_files(db, having, params).items():
            if ns_template is None:
                target = archive_ns
            else:
                targets = {
                    _resolve_archive_ns(ns_template, r[3], fallback=archive_ns) for r in rows
                }
                if len(targets) > 1:
                    split.append(source)
                    continue
                (target,) = targets
                if namespace and any(r[2] not in (namespace, target) for r in rows):
                    continue
            moving = [r for r in rows if r[2] != target]
            if not moving:
                continue  # already in its target
            if target not in refusal_by_target:
                refusal_by_target[target] = _target_refusal("archive target", target)
                # Once per target per run; the run row is the durable record.
                if refusal_by_target[target]:
                    logger.warning("auto_archive skipped: %s", refusal_by_target[target])
            if refusal_by_target[target]:
                refused_counts[target] = refused_counts.get(target, 0) + len(moving)
                continue
            collisions = _colliding_rows(rows)
            if collisions:
                conflicts.append({"source": source, "target": target, "count": collisions})
                continue
            ids_by_target.setdefault(target, []).extend(r[0] for r in moving)
            source_namespaces.update(r[2] for r in moving if r[2])
        return ids_by_target, source_namespaces, refused_counts, conflicts, split

    if dry_run:
        async with storage._lease_read_db() as db:
            ids_by_target, source_namespaces, refused_counts, conflicts, split = plan(db)
    else:
        async with storage.transaction(wait=WRITER_WAIT_S):
            db = storage._get_db()
            ids_by_target, source_namespaces, refused_counts, conflicts, split = plan(db)
            for target, ids in ids_by_target.items():
                db.executemany(
                    "UPDATE chunks SET namespace = ? WHERE id = ?",
                    [(target, cid) for cid in ids],
                )

    count = sum(len(ids) for ids in ids_by_target.values())
    outcome = _move_outcome(ids_by_target, conflicts, split, refused_counts)

    verb = "Would archive" if dry_run else "Archived"
    if ns_template is not None and ids_by_target:
        per_bucket = "; ".join(
            f"{target}: {len(ids)}" for target, ids in sorted(ids_by_target.items())
        )
        details = f"{verb} {count} chunks older than {max_age} days ({per_bucket})"
    else:
        details = f"{verb} {count} chunks older than {max_age} days → '{archive_ns}'"

    # Any refusal or conflict reads as an error even when other chunks moved,
    # as a partly failed auto_consolidate does: the files the policy set out
    # to archive were not all archived. ``affected_count`` still counts what
    # moved. A split-target file is reported, not an error: the template, not
    # the store, decides it has no single bucket.
    errors: list[str] = []
    if refused_counts:
        skipped = sum(refused_counts.values())
        details += f"; {skipped} skipped (target a search could not find)"
        errors.append(
            f"{skipped} chunk(s) not archived: target namespace a search could not find "
            f"({', '.join(repr(t) for t in sorted(refused_counts))})"
        )
    if conflicts:
        details += f"; {len(conflicts)} file(s) skipped (conflict in target)"
        errors.append(_conflict_error(conflicts))
    if split:
        details += f"; {len(split)} file(s) skipped (tags route to several targets)"

    return PolicyRunResult(
        policy_name="",
        policy_type="auto_archive",
        affected_count=count,
        dry_run=dry_run,
        details=details,
        namespaces=tuple(sorted(source_namespaces | set(ids_by_target))),
        outcome=outcome,
        error="; ".join(errors) or None,
    )


async def execute_auto_expire(
    storage: SqliteBackend,
    config: dict,
    namespace: str | None,
    dry_run: bool,
) -> PolicyRunResult:
    """Delete unaccessed chunks older than max_age_days.

    A held or pending source is not a candidate: its rows are retained for
    recovery, and deleting them would lose what the hold keeps.
    """
    max_age = config.get("max_age_days", 90)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age)).isoformat()

    query = (
        "SELECT id, namespace FROM chunks WHERE created_at < ? AND access_count = 0 "
        f"AND {VISIBLE_SOURCE_PATH}"  # nosec B608
    )
    params: list = [cutoff]
    if namespace:
        query += " AND namespace = ?"
        params.append(namespace)

    if dry_run:
        async with storage._lease_read_db() as db:
            rows = db.execute(query, params).fetchall()
    else:
        # The read and the delete under one write lock: a hold committed after
        # a read outside it would still lose its rows to the delete.
        async with storage.transaction(wait=WRITER_WAIT_S):
            rows = storage._get_db().execute(query, params).fetchall()
            if rows:
                await storage.delete_chunks(
                    [UUID(r[0]) for r in rows],
                    # Final, so a source it empties is cleaned up now, as it
                    # was when this delete ran outside a transaction.
                    finalize_emptied=True,
                )
    count = len(rows)

    # The only surviving record of what the rows were once the delete has run.
    ids = [r[0] for r in rows]
    outcome: dict = {}
    if ids:
        _record_ids(outcome, "deleted_ids", ids)

    return PolicyRunResult(
        policy_name="",
        policy_type="auto_expire",
        affected_count=count,
        dry_run=dry_run,
        details=f"{'Would expire' if dry_run else 'Expired'} {count} unaccessed chunks older than {max_age} days",
        namespaces=tuple(sorted({r[1] for r in rows if r[1]})),
        outcome=outcome,
    )


async def execute_auto_tag(
    storage: SqliteBackend,
    config: dict,
    namespace: str | None,
    dry_run: bool,
) -> PolicyRunResult:
    """Run auto-tagging on untagged chunks."""
    max_tags = config.get("max_tags", 5)

    # Same rows ``auto_tag_storage`` reads: it skips hidden sources whole.
    query = (
        "SELECT COUNT(*) FROM chunks WHERE (tags = '[]' OR tags = '') "
        f"AND {VISIBLE_SOURCE_PATH}"  # nosec B608
    )
    params: list = []
    if namespace:
        query += " AND namespace = ?"
        params.append(namespace)
    async with storage._lease_read_db() as db:
        count = db.execute(query, params).fetchone()[0]
    outcome: dict = {}

    if not dry_run and count > 0:
        try:
            from memtomem.tools.auto_tag import auto_tag_storage

            stats = await auto_tag_storage(
                storage,
                max_tags=max_tags,
                namespace_filter=namespace,
                overwrite=False,
                dry_run=False,
            )
        except Exception as exc:
            return PolicyRunResult(
                policy_name="",
                policy_type="auto_tag",
                affected_count=0,
                dry_run=False,
                details=f"Auto-tag failed: {exc}",
                namespaces=(namespace,) if namespace else (),
                error=str(exc),
                # ``auto_tag_storage`` upserts one chunk at a time, so a pass
                # that failed partway has already committed the tag rewrites
                # a tag-filtered search caches. The count is lost with the
                # exception — assume it wrote (#2157).
                mutated=True,
            )
        # ``count`` above is a pre-count estimate of untagged chunks; these are
        # what the run actually did.
        outcome.update(
            {
                "total_chunks": stats.total_chunks,
                "tagged_chunks": stats.tagged_chunks,
                "skipped_chunks": stats.skipped_chunks,
            }
        )

    return PolicyRunResult(
        policy_name="",
        policy_type="auto_tag",
        affected_count=count,
        dry_run=dry_run,
        details=f"{'Would tag' if dry_run else 'Tagged'} {count} untagged chunks (max_tags={max_tags})",
        namespaces=(namespace,) if namespace else (),
        outcome=outcome,
        # ``count`` is a pre-scan estimate of untagged chunks — it can be
        # positive for a run that wrote nothing (every candidate skipped by
        # the extractor). ``tagged_chunks`` is incremented only after the
        # upsert on the non-dry-run path, so it is the real write count.
        mutated=bool(outcome.get("tagged_chunks", 0)),
    )


async def execute_auto_consolidate(
    storage: SqliteBackend,
    config: dict,
    namespace: str | None,
    dry_run: bool,
    *,
    llm_provider: LLMProvider | None = None,
    extract_entities: bool = True,
) -> PolicyRunResult:
    """Group related chunks by source file and create heuristic summary chunks.

    Candidates are source files with at least ``min_group_size`` chunks. Each
    candidate produces one summary chunk in ``summary_namespace`` (default
    ``archive:summary``) with ``consolidated_into`` edges back to the
    originals. The summary chunk's content embeds a source hash so re-runs
    are idempotent: matching hash → skip, mismatched hash → delete old
    summary and regenerate. Sources with mixed namespaces are skipped with
    a warning (YAGNI — full per-namespace groupby can land in a follow-up
    if anyone hits that case).

    Config fields (all optional):

    - ``min_group_size`` (int, default 3): minimum chunks per source file
      to qualify as a consolidation candidate.
    - ``max_groups`` (int, default 10): cap on groups processed per run.
    - ``max_bullets`` (int, default 20): cap on bullets in the summary. It
      is also the coverage: the summary reads a source's first
      ``max_bullets`` chunks, and only those are linked to it and, with
      ``keep_originals: false``, demoted. Chunks past it stay uncovered, so
      agent consolidation can still group them.
    - ``keep_originals`` (bool, default True): if False, halve importance
      scores of originals (floor 0.3) so decay can evict them later; an
      original that is itself a summary keeps its score.
      Never a hard delete — see ``feedback_compression_priority.md``.
    - ``summary_namespace`` (str, default ``archive:summary``): target
      namespace for summary chunks. Keeps summaries out of default search
      unless explicitly queried.

    The ``namespace`` arg (from ``namespace_filter`` on the policy row) acts
    as a coarse gate: sources whose chunks aren't in that namespace are
    skipped. For same-source mixed-namespace rows, the whole source is
    skipped regardless of filter.
    """
    from memtomem.tools.consolidation_engine import (
        DEFAULT_SUMMARY_NAMESPACE,
        CONSOLIDATED_SUFFIX,
        SUMMARY_PATH_SCAN_LIMIT,
        CoveredElsewhere,
        apply_consolidation,
        compute_source_hash,
        covered_elsewhere,
        is_policy_summary,
        make_heuristic_summary,
        make_llm_summary,
        parse_source_hash,
    )

    min_group_size = config.get("min_group_size", 3)
    max_groups = config.get("max_groups", 10)
    max_bullets = config.get("max_bullets", 20)
    keep_originals = config.get("keep_originals", True)
    summary_namespace = config.get("summary_namespace", DEFAULT_SUMMARY_NAMESPACE)

    if min_group_size < 2:
        return PolicyRunResult(
            policy_name="",
            policy_type="auto_consolidate",
            affected_count=0,
            dry_run=dry_run,
            details="Error: min_group_size must be at least 2",
            error="min_group_size must be at least 2",
        )
    # ``max_bullets`` decides which originals a summary covers, so a summary
    # of no bullets would be linked to nothing.
    if isinstance(max_bullets, bool) or not isinstance(max_bullets, int) or max_bullets < 1:
        return PolicyRunResult(
            policy_name="",
            policy_type="auto_consolidate",
            affected_count=0,
            dry_run=dry_run,
            details="Error: max_bullets must be a positive integer",
            error="max_bullets must be a positive integer",
        )

    target_errors = policy_target_errors("auto_consolidate", config)
    if target_errors:
        return _refused_target_result("auto_consolidate", dry_run, target_errors)

    raw_groups = await storage.get_consolidation_groups(
        min_size=min_group_size,
        max_groups=max_groups,
    )

    applied = 0
    # Armed the moment a write is *attempted*, not when one is confirmed. Since
    # #2158 ``apply_consolidation`` is one transaction, so a failed group leaves
    # nothing behind and this over-reports — deliberately. The flag drives cache
    # invalidation and "did this run touch the store" reporting, where claiming a
    # write that rolled back is harmless and missing one that landed is not
    # (#2157).
    mutation_attempted = False
    llm_fallback_count = 0
    detail_parts: list[str] = []
    group_records: list[dict] = []
    deleted_summary_ids: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    seen_namespaces: set[str] = set()

    for g in raw_groups:
        source_path = Path(g["source"])
        # The hold is judged in the read; the recheck before the write below
        # covers a hold that lands while the summary is generated. The whole
        # source is read: the mixed-namespace check and the source hash must
        # see every row, or a change past a cap would never regenerate.
        chunks = await storage.list_chunks_by_source(
            source_path, limit=None, exclude_hidden=True, as_stored=True
        )
        if len(chunks) < min_group_size:
            continue
        # What the summary is made from: both summary builders read only
        # ``chunks[:max_bullets]``. These alone are linked and demoted; linking
        # the rest would mark content consolidated that no summary contains.
        summarized = chunks[:max_bullets]
        summarized_ids = {c.id for c in summarized}

        # Mixed-namespace sources → skip + warn. See plan for rationale.
        ns_set = {c.metadata.namespace for c in chunks}
        if len(ns_set) > 1:
            logger.warning(
                "auto_consolidate: skipping %s — mixed namespaces %s",
                source_path,
                sorted(ns_set),
            )
            detail_parts.append(f"{source_path.name} (SKIP: mixed ns)")
            skipped.append(f"{source_path.name} (mixed ns)")
            continue
        chunk_ns = next(iter(ns_set))

        # namespace policy filter — skip sources outside the target ns.
        if namespace and chunk_ns != namespace:
            continue

        # Idempotency layer 1 — policy-owned virtual summary + source hash.
        # This is the definitive signal for the policy's own prior work:
        # matching hash means nothing to do, mismatched hash means an input
        # changed (chunk added/removed) and we must regenerate.
        current_hash = compute_source_hash([c.id for c in chunks])
        virtual_path = source_path.parent / f"{source_path.name}{CONSOLIDATED_SUFFIX}"
        existing = await storage.list_chunks_by_source(virtual_path, limit=SUMMARY_PATH_SCAN_LIMIT)
        # Ownership before the hash (#2161). Reading the first chunk's embedded
        # ``Source hash:`` line and acting on it trusts whatever occupies the
        # path: a foreign chunk quoting a matching hash suppressed the policy
        # over that source forever, and one without a hash was reported as
        # "would consolidate" by dry-run over a group the live run then refused.
        # The check runs before the ``dry_run`` branch so both report the same
        # collision, and no LLM call is spent on a group that cannot be written.
        if len(existing) >= SUMMARY_PATH_SCAN_LIMIT or any(
            not is_policy_summary(c) for c in existing
        ):
            logger.warning(
                "auto_consolidate: refusing %s — summary path %s is occupied by "
                "chunks this policy does not own",
                source_path,
                virtual_path,
            )
            detail_parts.append(f"{source_path.name} (COLLISION: summary path occupied)")
            failed.append(f"{source_path.name} (collision)")
            continue
        # Read before the idempotency check below, which compares what the
        # existing summary covers with what a summary made now would cover.
        try:
            targets = await storage.get_consolidation_targets([c.id for c in chunks])
        except Exception:
            # Unknown coverage is not "uncovered": a heuristic summary
            # here could sit over the agent's, and decay its originals.
            logger.warning(
                "auto_consolidate: could not read consolidation coverage for %s",
                source_path,
                exc_info=True,
            )
            detail_parts.append(f"{source_path.name} (FAILED)")
            failed.append(f"{source_path.name} (coverage unreadable)")
            continue
        existing_ids = {c.id for c in existing}

        stale = False
        if existing:
            # Idempotent only when the path holds exactly the one summary the
            # last run left. Several owned rows mean a previous replacement
            # went wrong, so a matching hash on one of them is not evidence the
            # path is current — regenerate and let ``_clear_policy_summaries``
            # collapse them back to one. The summary must also cover exactly
            # the chunks it was made from: one written while the read stopped
            # at 20 rows can be linked to chunks past ``max_bullets`` under the
            # same hash, and only a regeneration removes those links.
            linked = {cid for cid, found in targets.items() if existing_ids.intersection(found)}
            if (
                len(existing) == 1
                and parse_source_hash(existing[0].content) == current_hash
                and linked == summarized_ids
            ):
                continue  # idempotent — same inputs, same output
            stale = True  # regenerate below

        # Idempotency layer 2 — agent-driven consolidation already covers this
        # source. When some original carries an outgoing ``consolidated_into``
        # edge to a summary other than the policy's own at the virtual path,
        # the agent ran ``mem_consolidate_apply`` over it. We defer to their
        # work rather than overwriting it with a heuristic summary; ``any``
        # covered chunk is enough, so even a partial agent consolidation is
        # kept. Only the outgoing direction counts: a chunk an edge points *at*
        # is a summary, and a source that holds one is not consolidated by it.
        #
        # This holds on a stale source too. An agent apply refuses originals the
        # policy summary already covers, but chunks added after the policy run
        # are uncovered, and an apply over them used to be followed by a
        # regeneration that summarized them again and, with
        # ``keep_originals: false``, decayed them. The stale policy summary is
        # left as it is: a skip writes nothing, and that summary still describes
        # the chunks it was made from. A migrated source whose old-path policy
        # summary still covers it is reported under the same label
        # (``covered_elsewhere``). Every chunk of the source counts here, not
        # only the summarized ones, so an agent summary of the tail still
        # defers the policy.
        if covered_elsewhere(targets, existing_ids):
            detail_parts.append(f"{source_path.name} (SKIP: agent-consolidated)")
            skipped.append(f"{source_path.name} (agent-consolidated)")
            continue

        if dry_run:
            applied += 1
            tag = " (stale)" if stale else ""
            detail_parts.append(f"{source_path.name}{tag}")
            continue

        try:
            # Try LLM summary first, fall back to heuristic on failure.
            summary = None
            if llm_provider is not None:
                try:
                    summary = await make_llm_summary(
                        chunks, source_path, llm_provider, max_bullets=max_bullets
                    )
                except Exception:
                    logger.warning(
                        "auto_consolidate: LLM failed for %s, using heuristic",
                        source_path,
                        exc_info=True,
                    )
                    llm_fallback_count += 1
            if summary is None:
                summary = make_heuristic_summary(chunks, source_path, max_bullets=max_bullets)
            if await storage.is_source_held(source_path):
                continue
            group_dict = {
                "source": str(source_path),
                "chunk_ids": [str(c.id) for c in summarized],
                # The recheck under the write lock defers to a summary over any
                # chunk of the source, as the check above does.
                "coverage_ids": [str(c.id) for c in chunks],
                "namespace": chunk_ns,
                "chunk_count": len(summarized),
            }
            mutation_attempted = True
            summary_id = await apply_consolidation(
                storage,
                group_dict,
                summary,
                keep_originals=keep_originals,
                extract_entities=extract_entities,
                summary_namespace=summary_namespace,
            )
        except CoveredElsewhere:
            # An agent apply linked these originals while the summary was being
            # generated; the transaction rolled back, so this is the skip above.
            detail_parts.append(f"{source_path.name} (SKIP: agent-consolidated)")
            skipped.append(f"{source_path.name} (agent-consolidated)")
            continue
        except Exception:
            logger.warning(
                "auto_consolidate: failed to consolidate %s",
                source_path,
                exc_info=True,
            )
            detail_parts.append(f"{source_path.name} (FAILED)")
            failed.append(source_path.name)
            continue

        applied += 1
        if stale:
            # Recorded only now: the replacement happens inside
            # ``apply_consolidation``'s transaction, so on failure the old
            # summary is still there and reporting it deleted would be a lie.
            # Every owned row is listed, not just the first: the clear empties
            # the whole path, so reporting one id under-reports a path that had
            # collected more than one summary.
            deleted_summary_ids.extend(str(c.id) for c in existing)
        tag = " (regen)" if stale else ""
        detail_parts.append(f"{source_path.name}{tag}")
        group_records.append(
            {
                "source": str(source_path),
                "summary_id": str(summary_id),
                "chunk_ids": group_dict["chunk_ids"],
                "regenerated": stale,
            }
        )
        seen_namespaces.add(chunk_ns)

    verb = "Would consolidate" if dry_run else "Consolidated"
    if detail_parts:
        details = f"{verb} {applied} groups: {', '.join(detail_parts)}"
    else:
        details = f"{verb} 0 groups (no candidates)"
    if llm_fallback_count > 0:
        details += f" (llm_fallback_count={llm_fallback_count})"

    outcome: dict = {}
    if group_records:
        outcome["groups"] = group_records
    if deleted_summary_ids:
        outcome["deleted_summary_ids"] = deleted_summary_ids
    if skipped:
        outcome["skipped"] = skipped
    if failed:
        outcome["failed"] = failed
    if llm_fallback_count:
        outcome["llm_fallback_count"] = llm_fallback_count

    # A failed group leaves the store as it was — since #2158 the replacement is
    # atomic, so a source that had a stale summary still has it. The status must
    # still not read ``ok``: the group the policy set out to consolidate wasn't.
    error = f"{len(failed)} group(s) failed: {', '.join(failed)}" if failed else None

    namespaces = set(seen_namespaces)
    if group_records:
        namespaces.add(summary_namespace)

    return PolicyRunResult(
        policy_name="",
        policy_type="auto_consolidate",
        affected_count=applied,
        dry_run=dry_run,
        details=details,
        namespaces=tuple(sorted(namespaces)),
        outcome=outcome,
        error=error,
        mutated=not dry_run and mutation_attempted,
    )


async def execute_auto_promote(
    storage: SqliteBackend,
    config: dict,
    namespace: str | None,
    dry_run: bool,
) -> PolicyRunResult:
    """Move archived source files back to an active namespace based on access patterns.

    This is the inverse of ``auto_archive``: files that were archived but
    continue to be accessed (high access_count, recent last_accessed_at, or
    high importance_score) are promoted back to an active namespace. A file
    moves whole as soon as one of its chunks qualifies, since promoting part
    of it would leave it in two namespaces. Held and pending sources are not
    candidates.

    To prevent ping-pong with ``auto_archive``, promotion resets
    ``last_accessed_at`` to the current time on every chunk it moves, so the
    file won't immediately re-qualify for archival on the next policy run.

    Config fields (all optional):

    - ``source_prefix`` (str, default ``"archive"``): files whose chunks are
      in a namespace starting with this prefix are candidates. Matches both
      ``"archive"`` (flat mode) and ``"archive:work"``, ``"archive:misc"`` etc.
    - ``target_namespace`` (str, default ``"default"``): destination namespace
      for promoted chunks.
    - ``min_access_count`` (int, default 3): minimum ``access_count`` for a
      chunk to qualify its file for promotion.
    - ``min_importance_score`` (float | None, default None): if set, a chunk
      qualifies only when its ``importance_score`` is at least this value.
      Combined with ``min_access_count`` via AND.
    - ``recency_days`` (int | None, default None): if set, a chunk qualifies
      only when its ``last_accessed_at`` is within this many days. Chunks
      with null ``last_accessed_at`` do not qualify. Note: this is the
      opposite of ``auto_archive``'s age-based cutoff — here, *recent*
      access qualifies a file for promotion.

    The ``namespace`` arg (from ``namespace_filter`` on the policy row)
    overrides ``source_prefix`` with an exact-match filter when set. Either
    way, a file is a candidate only when each of its chunks is in a source
    namespace or already in the target. A file whose move would put two rows
    with the same content at the same line in one namespace is not moved and
    is reported as a conflict.
    """
    source_prefix = config.get("source_prefix", "archive")
    target_ns = config.get("target_namespace", "default")
    min_access_count = config.get("min_access_count", 3)
    min_importance_score = config.get("min_importance_score")
    recency_days = config.get("recency_days")

    target_errors = policy_target_errors("auto_promote", config)
    if target_errors:
        return _refused_target_result("auto_promote", dry_run, target_errors)

    # Source filtering: namespace param (exact) overrides prefix.
    if namespace:
        in_source = "namespace = ?"
        source_params: list = [namespace]
    else:
        in_source = "namespace LIKE ? ESCAPE '\\'"
        source_params = [f"{escape_like(source_prefix)}%"]

    # The per-chunk rule. The target is excluded from it, so a qualifying
    # chunk is always one that would move.
    qualifies = [in_source, "namespace != ?", "access_count >= ?"]
    qualify_params: list = [*source_params, target_ns, min_access_count]
    if min_importance_score is not None:
        qualifies.append("importance_score >= ?")
        qualify_params.append(min_importance_score)
    # Optional recency gate — opposite of auto_archive: here, *recent*
    # access qualifies a chunk. Null last_accessed_at disqualifies.
    if recency_days is not None:
        recency_cutoff = (datetime.now(timezone.utc) - timedelta(days=recency_days)).isoformat()
        qualifies.append("last_accessed_at IS NOT NULL")
        qualifies.append("last_accessed_at >= ?")
        qualify_params.append(recency_cutoff)

    having = (
        f"SUM(CASE WHEN {' AND '.join(qualifies)} THEN 1 ELSE 0 END) > 0 "
        f"AND SUM(CASE WHEN {in_source} OR namespace = ? THEN 1 ELSE 0 END) = COUNT(*)"
    )
    params = [*qualify_params, *source_params, target_ns]

    def plan(db: sqlite3.Connection) -> tuple[list[tuple], list[dict]]:
        moving: list[tuple] = []
        conflicts: list[dict] = []
        for source, rows in _rows_of_eligible_files(db, having, params).items():
            collisions = _colliding_rows(rows)
            if collisions:
                conflicts.append({"source": source, "target": target_ns, "count": collisions})
                continue
            moving.extend(r for r in rows if r[2] != target_ns)
        return moving, conflicts

    if dry_run:
        async with storage._lease_read_db() as db:
            rows, conflicts = plan(db)
    else:
        async with storage.transaction(wait=WRITER_WAIT_S):
            db = storage._get_db()
            rows, conflicts = plan(db)
            if rows:
                now_iso = datetime.now(timezone.utc).isoformat()
                db.executemany(
                    "UPDATE chunks SET namespace = ?, last_accessed_at = ? WHERE id = ?",
                    [(target_ns, now_iso, r[0]) for r in rows],
                )
    count = len(rows)

    outcome: dict = {}
    if rows:
        _record_ids(outcome, "promoted_ids", [r[0] for r in rows])
        outcome["target_namespace"] = target_ns
    if conflicts:
        outcome["conflicts"] = conflicts

    verb = "Would promote" if dry_run else "Promoted"
    details = f"{verb} {count} chunks → '{target_ns}'"
    error = None
    if conflicts:
        details += f"; {len(conflicts)} file(s) skipped (conflict in target)"
        error = _conflict_error(conflicts)

    return PolicyRunResult(
        policy_name="",
        policy_type="auto_promote",
        affected_count=count,
        dry_run=dry_run,
        details=details,
        namespaces=tuple(sorted({r[2] for r in rows if r[2]} | {target_ns})),
        outcome=outcome,
        error=error,
    )


_HANDLERS = {
    "auto_archive": execute_auto_archive,
    "auto_consolidate": execute_auto_consolidate,
    "auto_expire": execute_auto_expire,
    "auto_promote": execute_auto_promote,
    "auto_tag": execute_auto_tag,
}


async def run_policy(
    storage: SqliteBackend,
    policy: dict,
    dry_run: bool = False,
    *,
    llm_provider: LLMProvider | None = None,
    extract_entities: bool = True,
    source: str = "mcp",
) -> PolicyRunResult:
    """Execute a single policy.

    ``llm_provider`` is forwarded to ``execute_auto_consolidate`` when set.
    ``source`` labels the resulting maintenance run row (``scheduler`` for
    unattended runs, ``mcp`` for tool calls).

    This is the single choke point for policy execution — both
    ``run_all_enabled`` and ``mem_policy_run(name=...)`` reach handlers through
    it — so it is where the maintenance run row (#2132) is written. Applied
    runs only: a dry run mutates nothing and records nothing.
    """
    ptype = policy["policy_type"]
    handler = _HANDLERS.get(ptype)
    if handler is None:
        # No row: nothing ran.
        return PolicyRunResult(
            policy_name=policy["name"],
            policy_type=ptype,
            affected_count=0,
            dry_run=dry_run,
            details=f"Unknown policy type: {ptype}",
        )

    run_id: int | None = None
    if not dry_run:
        run_id = await storage.maintenance_run_start(
            ptype, policy_name=policy["name"], source=source
        )

    config = policy.get("config", {})
    shape_error = config_shape_error(config)
    try:
        if shape_error is not None:
            # Not handed to the handler, which would raise on ``config.get``;
            # an error result lets the row and ``run_all_enabled`` go on.
            result = PolicyRunResult(
                policy_name="",
                policy_type=ptype,
                affected_count=0,
                dry_run=dry_run,
                details=f"Error: {shape_error}",
                error=shape_error,
            )
        elif ptype == "auto_consolidate":
            result = await execute_auto_consolidate(
                storage,
                config,
                policy.get("namespace_filter"),
                dry_run,
                llm_provider=llm_provider,
                extract_entities=extract_entities,
            )
        else:
            result = await handler(
                storage,
                config,
                policy.get("namespace_filter"),
                dry_run,
            )
    except Exception as exc:
        # The mutation may have partly landed; record that and let the caller
        # see the original failure.
        if run_id is not None:
            try:
                await storage.maintenance_run_finish(
                    run_id, status="error", error=f"{type(exc).__name__}: {exc}"
                )
            except Exception:
                # Best-effort: the original failure is the one worth raising.
                logger.warning("could not finalize maintenance run %s", run_id, exc_info=True)
        raise

    if run_id is not None:
        await storage.maintenance_run_finish(
            run_id,
            status="error" if result.error else "ok",
            affected_count=result.affected_count,
            namespaces=result.namespaces,
            summary=result.outcome,
            error=result.error,
        )

    return PolicyRunResult(
        policy_name=policy["name"],
        policy_type=result.policy_type,
        affected_count=result.affected_count,
        dry_run=result.dry_run,
        details=result.details,
        namespaces=result.namespaces,
        outcome=result.outcome,
        error=result.error,
        run_id=run_id,
        # Handlers whose counter is a real write count (archive / expire /
        # promote) leave ``mutated`` unset — derive it from the counter here.
        mutated=(
            result.mutated
            if result.mutated is not None
            else (not result.dry_run and result.affected_count > 0)
        ),
    )


async def run_all_enabled(
    storage: SqliteBackend,
    dry_run: bool = False,
    max_actions: int | None = None,
    *,
    llm_provider: LLMProvider | None = None,
    extract_entities: bool = True,
    source: str = "mcp",
) -> list[PolicyRunResult]:
    """Run all enabled policies.

    Args:
        max_actions: If set, stop after cumulative affected_count reaches
            this limit.  The cap is checked between policies — individual
            handlers run atomically.
        llm_provider: Optional LLM provider forwarded to consolidation.
        extract_entities: Forwarded to consolidation so
            ``indexing.extract_entities`` governs the summary chunk's entities
            the same way it governs the indexer's.
        source: Label for the maintenance run rows written per applied policy
            (``scheduler`` for unattended runs, ``mcp`` for tool calls).
    """
    policies = await storage.policy_get_enabled()
    results: list[PolicyRunResult] = []
    cumulative = 0
    for p in policies:
        result = await run_policy(
            storage,
            p,
            dry_run=dry_run,
            llm_provider=llm_provider,
            extract_entities=extract_entities,
            source=source,
        )
        if not dry_run:
            await storage.policy_update_last_run(p["name"])
        results.append(result)
        cumulative += result.affected_count
        if max_actions is not None and cumulative >= max_actions:
            logger.info(
                "max_actions reached (%d/%d) — stopping after policy '%s'",
                cumulative,
                max_actions,
                p["name"],
            )
            break
    return results
