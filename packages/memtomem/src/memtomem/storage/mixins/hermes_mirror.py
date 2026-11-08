"""Hermes built-in memory mirror storage — a last-writer-wins element set.

Hermes notifies a memory provider after each committed write to its built-in
``MEMORY.md`` / ``USER.md`` store: add, replace or remove of one entry. The
memtomem provider forwards each notification as one operation; this mixin
applies it to ``hermes_mirror_entries``, one row per entry keyed by the sha256
of the entry's exact text.

The rule is last-writer-wins per key. A write lands only when its ``ts`` is
newer than the stored row's; on a tie a live row beats a tombstone. Every
operation is therefore idempotent and order-independent for a fixed key: a
retry changes nothing, and two processes' operations converge whatever order
they arrive in. A tombstone stores no text.

``hermes_mirror_targets`` carries the per-target bookkeeping the derived file
needs: ``revision`` moves on every state change, ``render_pending`` is set by
that change and cleared only by a render of that same revision, and
``purged_at`` fences out operations older than the last purge.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, NamedTuple


class MirrorWrite(NamedTuple):
    """One row write of an operation: ``tombstone`` writes keep no text."""

    entry_key: str
    content: str
    ts: int
    tombstone: bool


class MirrorEntry(NamedTuple):
    """A live entry as the renderer reads it."""

    entry_key: str
    content: str
    ts: int


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


_UPSERT = (
    "INSERT INTO hermes_mirror_entries "
    "(owner, target, entry_key, content, ts, tombstone) VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (owner, target, entry_key) DO UPDATE SET "
    "content = excluded.content, ts = excluded.ts, tombstone = excluded.tombstone "
    "WHERE excluded.ts > hermes_mirror_entries.ts "
    "OR (excluded.ts = hermes_mirror_entries.ts "
    "AND excluded.tombstone < hermes_mirror_entries.tombstone)"
)


class HermesMirrorMixin:
    """Mixin for the Hermes memory mirror tables. Requires ``self._get_db()``."""

    async def hermes_mirror_apply(
        self,
        owner: str,
        target: str,
        writes: list[MirrorWrite],
        *,
        previous_key: str | None = None,
    ) -> dict[str, Any]:
        """Apply one operation's row writes in one statement group.

        Returns ``fenced`` (an operation not newer than the last purge, nothing
        written), ``changed`` (some row moved), ``previous_matched`` (whether
        ``previous_key`` named a live row before the writes; ``None`` without
        one) and the target's ``revision`` after the call. ``revision`` moves
        only when a row changed, and that change also sets ``render_pending``.
        """
        db = self._get_db()  # type: ignore[attr-defined]
        with self._rolls_back_if_standalone(db):  # type: ignore[attr-defined]
            row = db.execute(
                "SELECT revision, purged_at FROM hermes_mirror_targets "
                "WHERE owner = ? AND target = ?",
                (owner, target),
            ).fetchone()
            revision = int(row[0]) if row else 0
            purged_at = row[1] if row else None
            fenced = purged_at is not None and any(w.ts <= purged_at for w in writes)
            previous_matched: bool | None = None
            if previous_key is not None and not fenced:
                hit = db.execute(
                    "SELECT 1 FROM hermes_mirror_entries "
                    "WHERE owner = ? AND target = ? AND entry_key = ? AND tombstone = 0",
                    (owner, target, previous_key),
                ).fetchone()
                previous_matched = hit is not None
            before = db.total_changes
            for w in [] if fenced else writes:
                db.execute(
                    _UPSERT,
                    (
                        owner,
                        target,
                        w.entry_key,
                        "" if w.tombstone else w.content,
                        w.ts,
                        1 if w.tombstone else 0,
                    ),
                )
            changed = db.total_changes != before
            if changed:
                revision += 1
                db.execute(
                    "INSERT INTO hermes_mirror_targets "
                    "(owner, target, revision, render_pending, updated_at) "
                    "VALUES (?, ?, ?, 1, ?) "
                    "ON CONFLICT (owner, target) DO UPDATE SET "
                    "revision = excluded.revision, render_pending = 1, "
                    "updated_at = excluded.updated_at",
                    (owner, target, revision, _utcnow_iso()),
                )
            self._commit_if_standalone(db)  # type: ignore[attr-defined]
        return {
            "fenced": fenced,
            "changed": changed,
            "previous_matched": previous_matched,
            "revision": revision,
        }

    async def hermes_mirror_purge(self, owner: str, target: str, ts: int) -> dict[str, Any]:
        """Delete every row of the target and fence out operations at or before ``ts``.

        A purge older than the current fence changes nothing (``fenced``). The
        purge always moves ``revision`` and sets ``render_pending`` otherwise,
        so the renderer writes the now-empty document and the index drops the
        old chunks through its ordinary diff.
        """
        db = self._get_db()  # type: ignore[attr-defined]
        with self._rolls_back_if_standalone(db):  # type: ignore[attr-defined]
            row = db.execute(
                "SELECT revision, purged_at FROM hermes_mirror_targets "
                "WHERE owner = ? AND target = ?",
                (owner, target),
            ).fetchone()
            revision = int(row[0]) if row else 0
            purged_at = row[1] if row else None
            fenced = purged_at is not None and ts <= purged_at
            removed = 0
            if not fenced:
                cur = db.execute(
                    "DELETE FROM hermes_mirror_entries WHERE owner = ? AND target = ?",
                    (owner, target),
                )
                removed = cur.rowcount
                revision += 1
                db.execute(
                    "INSERT INTO hermes_mirror_targets "
                    "(owner, target, revision, render_pending, purged_at, updated_at) "
                    "VALUES (?, ?, ?, 1, ?, ?) "
                    "ON CONFLICT (owner, target) DO UPDATE SET "
                    "revision = excluded.revision, render_pending = 1, "
                    "purged_at = excluded.purged_at, updated_at = excluded.updated_at",
                    (owner, target, revision, ts, _utcnow_iso()),
                )
            self._commit_if_standalone(db)  # type: ignore[attr-defined]
        return {"fenced": fenced, "changed": removed > 0, "revision": revision}

    async def hermes_mirror_snapshot(
        self, owner: str, target: str
    ) -> tuple[int, list[MirrorEntry]]:
        """The target's revision and its live entries, read in one statement.

        One statement is one snapshot, so the entries are exactly those of the
        returned revision. Order is ``(ts, entry_key)``: total even when two
        processes produced the same ``ts``.
        """
        db = self._get_txn_read_db()  # type: ignore[attr-defined]
        rows = db.execute(
            "SELECT t.revision, e.entry_key, e.content, e.ts "
            "FROM (SELECT ? AS owner, ? AS target) k "
            "LEFT JOIN hermes_mirror_targets t "
            "ON t.owner = k.owner AND t.target = k.target "
            "LEFT JOIN hermes_mirror_entries e "
            "ON e.owner = k.owner AND e.target = k.target AND e.tombstone = 0 "
            "ORDER BY e.ts, e.entry_key",
            (owner, target),
        ).fetchall()
        revision = int(rows[0][0]) if rows and rows[0][0] is not None else 0
        entries = [MirrorEntry(r[1], r[2], int(r[3])) for r in rows if r[1] is not None]
        return revision, entries

    async def hermes_mirror_mark_pending(self, owner: str, target: str) -> bool:
        """Set ``render_pending`` on an existing target so the next render rewrites the file.

        For an explicit reconcile: the file may have been edited or deleted after its last
        render, which no flag records. Returns whether the target exists.
        """
        db = self._get_db()  # type: ignore[attr-defined]
        with self._rolls_back_if_standalone(db):  # type: ignore[attr-defined]
            cur = db.execute(
                "UPDATE hermes_mirror_targets SET render_pending = 1, updated_at = ? "
                "WHERE owner = ? AND target = ?",
                (_utcnow_iso(), owner, target),
            )
            exists = cur.rowcount > 0
            self._commit_if_standalone(db)  # type: ignore[attr-defined]
        return exists

    async def hermes_mirror_mark_rendered(self, owner: str, target: str, revision: int) -> bool:
        """Clear ``render_pending`` only if ``revision`` is still the target's revision.

        A newer operation that committed while this render ran keeps its own
        pending flag. Returns whether the flag was cleared.
        """
        db = self._get_db()  # type: ignore[attr-defined]
        with self._rolls_back_if_standalone(db):  # type: ignore[attr-defined]
            cur = db.execute(
                "UPDATE hermes_mirror_targets SET render_pending = 0, rendered_revision = ? "
                "WHERE owner = ? AND target = ? AND revision = ?",
                (revision, owner, target, revision),
            )
            cleared = cur.rowcount > 0
            self._commit_if_standalone(db)  # type: ignore[attr-defined]
        return cleared

    async def hermes_mirror_target_state(self, owner: str, target: str) -> dict[str, Any] | None:
        """The target's bookkeeping row, or ``None`` before its first change."""
        db = self._get_txn_read_db()  # type: ignore[attr-defined]
        row = db.execute(
            "SELECT revision, rendered_revision, render_pending, purged_at "
            "FROM hermes_mirror_targets WHERE owner = ? AND target = ?",
            (owner, target),
        ).fetchone()
        if row is None:
            return None
        return {
            "revision": int(row[0]),
            "rendered_revision": int(row[1]),
            "render_pending": bool(row[2]),
            "purged_at": row[3],
        }
