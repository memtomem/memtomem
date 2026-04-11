"""Policy execution engine — run lifecycle policies on memories."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_NS_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")

_VALID_TYPES = {"auto_archive", "auto_promote", "auto_expire", "auto_consolidate", "auto_tag"}


@dataclass(frozen=True)
class PolicyRunResult:
    policy_name: str
    policy_type: str
    affected_count: int
    dry_run: bool
    details: str


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


async def execute_auto_archive(
    storage: object,
    config: dict,
    namespace: str | None,
    dry_run: bool,
) -> PolicyRunResult:
    """Move chunks matching an aging rule to an archive namespace.

    Config fields (all but ``max_age_days`` are optional):

    - ``max_age_days`` (int, default 30): chunks older than this many days
      are candidates for archival.
    - ``archive_namespace`` (str, default ``"archive"``): single target
      namespace when ``archive_namespace_template`` is not set. Also acts as
      the fallback if the template is empty.
    - ``age_field`` (str, default ``"created_at"``): ``"created_at"`` or
      ``"last_accessed_at"``. For ``"last_accessed_at"``, null values fall
      back to ``created_at`` via ``COALESCE``.
    - ``min_access_count`` (int | None, default None): only archive chunks
      whose ``access_count`` is at most this value. None disables the filter.
    - ``max_importance_score`` (float | None, default None): only archive
      chunks whose ``importance_score`` is strictly below this value. None
      disables the filter.
    - ``archive_namespace_template`` (str | None, default None): per-chunk
      target namespace template. Supports the ``{first_tag}`` placeholder,
      which expands to the chunk's first tag (or ``"misc"`` when tags are
      empty). Chunks already in their resolved target namespace are skipped.
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
        )

    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age)).isoformat()

    db = storage._get_db()  # type: ignore[attr-defined]

    # Template mode needs current namespace + tags per chunk to route and
    # skip self-moves. Flat mode can fetch only ids.
    select_cols = "id, namespace, tags" if ns_template is not None else "id"

    where_parts: list[str] = []
    params: list = []

    if age_field == "last_accessed_at":
        where_parts.append("COALESCE(last_accessed_at, created_at) < ?")
    else:
        where_parts.append("created_at < ?")
    params.append(cutoff)

    # Flat mode: exclude chunks already in the single target namespace. Template
    # mode handles self-move exclusion per-chunk after resolution, because the
    # target depends on each chunk's tag.
    if ns_template is None:
        where_parts.append("namespace != ?")
        params.append(archive_ns)

    if min_access_count is not None:
        where_parts.append("access_count <= ?")
        params.append(min_access_count)

    if max_importance_score is not None:
        where_parts.append("importance_score < ?")
        params.append(max_importance_score)

    if namespace:
        where_parts.append("namespace = ?")
        params.append(namespace)

    query = f"SELECT {select_cols} FROM chunks WHERE " + " AND ".join(where_parts)
    rows = db.execute(query, params).fetchall()

    ids_by_target: dict[str, list[str]] = {}
    if ns_template is None:
        if rows:
            ids_by_target[archive_ns] = [r[0] for r in rows]
    else:
        for chunk_id, current_ns, tags_json in rows:
            target = _resolve_archive_ns(ns_template, tags_json, fallback=archive_ns)
            if target == current_ns:
                continue  # already in target bucket
            ids_by_target.setdefault(target, []).append(chunk_id)

    count = sum(len(ids) for ids in ids_by_target.values())

    if not dry_run and count > 0:
        for target, ids in ids_by_target.items():
            db.executemany(
                "UPDATE chunks SET namespace = ? WHERE id = ?",
                [(target, cid) for cid in ids],
            )
        db.commit()

    verb = "Would archive" if dry_run else "Archived"
    if ns_template is not None and ids_by_target:
        per_bucket = "; ".join(
            f"{target}: {len(ids)}" for target, ids in sorted(ids_by_target.items())
        )
        details = f"{verb} {count} chunks older than {max_age} days ({per_bucket})"
    else:
        details = f"{verb} {count} chunks older than {max_age} days → '{archive_ns}'"

    return PolicyRunResult(
        policy_name="",
        policy_type="auto_archive",
        affected_count=count,
        dry_run=dry_run,
        details=details,
    )


async def execute_auto_expire(
    storage: object,
    config: dict,
    namespace: str | None,
    dry_run: bool,
) -> PolicyRunResult:
    """Delete chunks older than max_age_days."""
    max_age = config.get("max_age_days", 90)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age)).isoformat()

    db = storage._get_db()  # type: ignore[attr-defined]
    query = "SELECT id FROM chunks WHERE created_at < ? AND access_count = 0"
    params: list = [cutoff]
    if namespace:
        query += " AND namespace = ?"
        params.append(namespace)

    rows = db.execute(query, params).fetchall()
    count = len(rows)

    if not dry_run and count > 0:
        ids = [r[0] for r in rows]
        ph = ",".join("?" for _ in ids)
        db.execute(f"DELETE FROM chunks WHERE id IN ({ph})", ids)
        db.commit()

    return PolicyRunResult(
        policy_name="",
        policy_type="auto_expire",
        affected_count=count,
        dry_run=dry_run,
        details=f"{'Would expire' if dry_run else 'Expired'} {count} unaccessed chunks older than {max_age} days",
    )


async def execute_auto_tag(
    storage: object,
    config: dict,
    namespace: str | None,
    dry_run: bool,
) -> PolicyRunResult:
    """Run auto-tagging on untagged chunks."""
    max_tags = config.get("max_tags", 5)

    db = storage._get_read_db()  # type: ignore[attr-defined]
    query = "SELECT COUNT(*) FROM chunks WHERE tags = '[]' OR tags = ''"
    if namespace:
        query += f" AND namespace = '{namespace}'"
    count = db.execute(query).fetchone()[0]

    if not dry_run and count > 0:
        try:
            from memtomem.tools.auto_tag import auto_tag_storage

            await auto_tag_storage(
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
            )

    return PolicyRunResult(
        policy_name="",
        policy_type="auto_tag",
        affected_count=count,
        dry_run=dry_run,
        details=f"{'Would tag' if dry_run else 'Tagged'} {count} untagged chunks (max_tags={max_tags})",
    )


_HANDLERS = {
    "auto_archive": execute_auto_archive,
    "auto_expire": execute_auto_expire,
    "auto_tag": execute_auto_tag,
}


async def run_policy(
    storage: object,
    policy: dict,
    dry_run: bool = False,
) -> PolicyRunResult:
    """Execute a single policy."""
    ptype = policy["policy_type"]
    handler = _HANDLERS.get(ptype)
    if handler is None:
        return PolicyRunResult(
            policy_name=policy["name"],
            policy_type=ptype,
            affected_count=0,
            dry_run=dry_run,
            details=f"Unknown policy type: {ptype}",
        )

    result = await handler(
        storage, policy.get("config", {}), policy.get("namespace_filter"), dry_run
    )
    return PolicyRunResult(
        policy_name=policy["name"],
        policy_type=result.policy_type,
        affected_count=result.affected_count,
        dry_run=result.dry_run,
        details=result.details,
    )


async def run_all_enabled(
    storage: object,
    dry_run: bool = False,
) -> list[PolicyRunResult]:
    """Run all enabled policies."""
    policies = await storage.policy_get_enabled()  # type: ignore[attr-defined]
    results = []
    for p in policies:
        result = await run_policy(storage, p, dry_run=dry_run)
        if not dry_run:
            await storage.policy_update_last_run(p["name"])  # type: ignore[attr-defined]
        results.append(result)
    return results
