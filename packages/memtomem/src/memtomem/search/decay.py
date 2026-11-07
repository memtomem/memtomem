"""Time-based memory decay: score attenuation and TTL expiry."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.models import SearchResult
    from memtomem.storage.sqlite_backend import SqliteBackend


def decay_factor(age_days: float, half_life_days: float) -> float:
    """Exponential decay factor: 1.0 at age 0, 0.5 at age == half_life_days.

    Args:
        age_days: Age of the chunk in days. Clamped to >= 0.
        half_life_days: Days after which the factor halves (must be > 0).

    Returns:
        Decay factor in (0, 1].
    """
    if age_days <= 0:
        return 1.0
    if half_life_days <= 0:
        return 1.0
    return math.exp(-math.log(2) * age_days / half_life_days)


def apply_score_decay(
    results: list[SearchResult],
    half_life_days: float = 30.0,
    now: datetime | None = None,
) -> list[SearchResult]:
    """Re-score search results by multiplying scores with a time-decay factor.

    Results are re-sorted and re-ranked after decay is applied.

    Args:
        results: Search results to decay.
        half_life_days: Decay half-life in days (score halves every N days).
        now: Reference time (defaults to UTC now).

    Returns:
        Re-scored, re-ranked list of SearchResult objects.
    """
    from memtomem.models import SearchResult as SR

    if not results or half_life_days <= 0:
        return results

    if now is None:
        now = datetime.now(timezone.utc)

    decayed: list[tuple[float, SR]] = []
    for r in results:
        updated_at = r.chunk.updated_at
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - updated_at).total_seconds() / 86400)

        effective_half_life = half_life_days

        new_score = r.score * decay_factor(age_days, effective_half_life)
        decayed.append((new_score, r))

    decayed.sort(key=lambda t: t[0], reverse=True)
    return [
        SR(chunk=r.chunk, score=score, rank=i + 1, source=r.source)
        for i, (score, r) in enumerate(decayed)
    ]


@dataclass(frozen=True)
class ExpireStats:
    """Statistics returned by expire_chunks."""

    total_chunks: int
    expired_chunks: int
    deleted_chunks: int


async def expire_chunks(
    storage: SqliteBackend,
    max_age_days: float,
    dry_run: bool = False,
    source_filter: str | None = None,
) -> ExpireStats:
    """Delete (or preview) chunks older than *max_age_days*.

    Age is measured from ``chunk.updated_at``. If *dry_run* is True no
    deletions are performed; the counts still reflect what would be expired.
    A held or pending source is neither counted nor expired: its rows are
    retained for recovery, and expiring them would lose what the hold keeps.

    Args:
        storage: StorageBackend instance.
        max_age_days: Chunks older than this many days are expired.
        dry_run: If True, compute but do not delete.
        source_filter: Only consider sources whose path contains this substring.

    Returns:
        ExpireStats with total_chunks, expired_chunks, deleted_chunks.
    """
    from uuid import UUID

    from memtomem.storage.base import WRITER_WAIT_S
    from memtomem.storage.sqlite_visibility import VISIBLE_SOURCE_PATH

    now = datetime.now(timezone.utc)
    cutoff_seconds = max_age_days * 86400
    # The hold is judged in the statement, and on a live run under the write
    # lock that also covers the delete, so a hold committed after the scan
    # cannot lose its rows.
    query = f"SELECT id, source_file, updated_at FROM chunks WHERE {VISIBLE_SOURCE_PATH}"  # nosec B608

    def scan(rows: list[tuple]) -> tuple[int, list[UUID]]:
        if source_filter:
            from memtomem.search.pipeline import match_source_filter_substring

            # Substring-only contract — see ``match_source_filter_substring``
            # for the separator-fold rule (#720) and rationale for not
            # sharing the glob-aware ``match_source_filter``.
            rows = [
                r for r in rows if match_source_filter_substring(source_filter, str(Path(r[1])))
            ]
        expired: list[UUID] = []
        for chunk_id, _source, updated_raw in rows:
            try:
                updated_at = datetime.fromisoformat(updated_raw)
            except (ValueError, TypeError):
                # ``_row_to_chunk`` reads a corrupted value as "now", so it
                # was never old enough to expire.
                continue
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=timezone.utc)
            if (now - updated_at).total_seconds() >= cutoff_seconds:
                expired.append(UUID(chunk_id))
        return len(rows), expired

    deleted = 0
    if dry_run:
        async with storage._lease_read_db() as db:
            total, to_delete = scan(db.execute(query).fetchall())
    else:
        async with storage.transaction(wait=WRITER_WAIT_S):
            total, to_delete = scan(storage._get_db().execute(query).fetchall())
            if to_delete:
                deleted = await storage.delete_chunks(to_delete, finalize_emptied=True)

    return ExpireStats(
        total_chunks=total,
        expired_chunks=len(to_delete),
        deleted_chunks=deleted,
    )
