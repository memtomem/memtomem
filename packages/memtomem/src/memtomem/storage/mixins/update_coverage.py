"""Update review coverage mixin — the commits an ``update`` run has reviewed (#2918).

One row per ``(project_root, sha)``. The ``update`` workflow reads the latest
commits with ``git log`` and asks which of them an earlier run already
reviewed; after its last memory write it records the ones it read in full.
A set of SHAs, not a cursor: nothing is paged by time, a first run marks only
what it saw, a rewritten or unreachable commit is simply absent or inert, and
two runs that overlap record the union of what each actually read.

Rows older than the retention window are pruned lazily on the next record
call, *before* the insert: pruning afterwards would let a re-review of an
expired commit hit ``INSERT OR IGNORE`` and then delete that very row, leaving a
completed review uncovered.

The recorded SHAs are provenance supplied by the caller; the server never runs
``git`` and never checks that a commit exists.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

# Rows older than this are dropped lazily on the next record call. Matches
# MaintenanceRunMixin's retention window.
_UPDATE_COVERAGE_MAX_AGE_DAYS = 90


class UpdateCoverageMixin:
    """Mixin providing the update review coverage set. Requires self._get_db()."""

    async def update_coverage_unreviewed(self, project_root: str, shas: list[str]) -> list[str]:
        """Return the SHAs in ``shas`` with no coverage row for ``project_root``, in order."""
        if not shas:
            return []
        # Bounded by the tool's cap on the list (a ``git log -n 10`` window), so
        # one IN clause stays far below SQLite's variable limit.
        placeholders = ",".join("?" for _ in shas)
        # The writer when a caller's ``transaction()`` is open: a pooled reader
        # would see the snapshot from before that caller's own record call.
        async with self._lease_txn_read_db() as db:
            rows = db.execute(
                "SELECT sha FROM update_review_coverage "
                f"WHERE project_root = ? AND sha IN ({placeholders})",
                (project_root, *shas),
            ).fetchall()
        reviewed = {r[0] for r in rows}
        return [sha for sha in shas if sha not in reviewed]

    async def update_coverage_record(
        self, project_root: str, shas: list[str], run_id: str
    ) -> tuple[int, int]:
        """Record ``shas`` as reviewed; return ``(recorded, already_recorded)``."""
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        recorded = 0
        with self._rolls_back_if_standalone(db):
            # Prune first (see the module docstring); the DELETE sits inside the
            # protected region so a failing INSERT cannot strand it.
            self._prune_update_coverage(db)
            for sha in shas:
                cur = db.execute(
                    "INSERT OR IGNORE INTO update_review_coverage "
                    "(project_root, sha, run_id, reviewed_at) VALUES (?, ?, ?, ?)",
                    (project_root, sha, run_id, now),
                )
                recorded += cur.rowcount
            self._commit_if_standalone(db)
        return recorded, len(shas) - recorded

    def _prune_update_coverage(self, db: sqlite3.Connection) -> None:
        """Drop rows older than the retention window (lazy, on write)."""
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=_UPDATE_COVERAGE_MAX_AGE_DAYS)
        ).isoformat(timespec="seconds")
        db.execute("DELETE FROM update_review_coverage WHERE reviewed_at < ?", (cutoff,))
