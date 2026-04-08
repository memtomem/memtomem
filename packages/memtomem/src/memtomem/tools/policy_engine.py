"""Policy execution engine — run lifecycle policies on memories."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_VALID_TYPES = {"auto_archive", "auto_promote", "auto_expire", "auto_consolidate", "auto_tag"}


@dataclass(frozen=True)
class PolicyRunResult:
    policy_name: str
    policy_type: str
    affected_count: int
    dry_run: bool
    details: str


async def execute_auto_archive(
    storage: object,
    config: dict,
    namespace: str | None,
    dry_run: bool,
) -> PolicyRunResult:
    """Move chunks older than max_age_days to archive namespace."""
    max_age = config.get("max_age_days", 30)
    archive_ns = config.get("archive_namespace", "archive")
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max_age)).isoformat()

    db = storage._get_db()  # type: ignore[attr-defined]
    query = "SELECT id FROM chunks WHERE created_at < ? AND namespace != ?"
    params: list = [cutoff, archive_ns]
    if namespace:
        query += " AND namespace = ?"
        params.append(namespace)

    rows = db.execute(query, params).fetchall()
    count = len(rows)

    if not dry_run and count > 0:
        ids = [r[0] for r in rows]
        db.executemany(
            "UPDATE chunks SET namespace = ? WHERE id = ?",
            [(archive_ns, cid) for cid in ids],
        )
        db.commit()

    return PolicyRunResult(
        policy_name="",
        policy_type="auto_archive",
        affected_count=count,
        dry_run=dry_run,
        details=f"{'Would archive' if dry_run else 'Archived'} {count} chunks older than {max_age} days → '{archive_ns}'",
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
