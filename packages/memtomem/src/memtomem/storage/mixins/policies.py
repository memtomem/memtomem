"""Policy storage mixin — CRUD for memory_policies table."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4


class PolicyMixin:
    """Mixin providing memory policy CRUD. Requires self._get_db()."""

    async def policy_add(
        self,
        name: str,
        policy_type: str,
        config: dict,
        namespace_filter: str | None = None,
    ) -> str:
        db = self._get_db()
        policy_id = uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "INSERT INTO memory_policies (id, name, policy_type, config, enabled, namespace_filter, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
            (policy_id, name, policy_type, json.dumps(config), namespace_filter, now, now),
        )
        db.commit()
        return policy_id

    async def policy_list(self) -> list[dict]:
        db = self._get_read_db()
        rows = db.execute(
            "SELECT id, name, policy_type, config, enabled, namespace_filter, last_run_at, created_at "
            "FROM memory_policies ORDER BY created_at"
        ).fetchall()
        return [
            {
                "id": r[0],
                "name": r[1],
                "policy_type": r[2],
                "config": json.loads(r[3]) if r[3] else {},
                "enabled": bool(r[4]),
                "namespace_filter": r[5],
                "last_run_at": r[6],
                "created_at": r[7],
            }
            for r in rows
        ]

    async def policy_get(self, name: str) -> dict | None:
        db = self._get_read_db()
        row = db.execute(
            "SELECT id, name, policy_type, config, enabled, namespace_filter, last_run_at, created_at "
            "FROM memory_policies WHERE name = ?",
            (name,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row[0],
            "name": row[1],
            "policy_type": row[2],
            "config": json.loads(row[3]) if row[3] else {},
            "enabled": bool(row[4]),
            "namespace_filter": row[5],
            "last_run_at": row[6],
            "created_at": row[7],
        }

    async def policy_delete(self, name: str) -> bool:
        db = self._get_db()
        cur = db.execute("DELETE FROM memory_policies WHERE name = ?", (name,))
        db.commit()
        return cur.rowcount > 0

    async def policy_update_last_run(self, name: str) -> None:
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "UPDATE memory_policies SET last_run_at = ?, updated_at = ? WHERE name = ?",
            (now, now, name),
        )
        db.commit()

    async def policy_get_enabled(self) -> list[dict]:
        db = self._get_read_db()
        rows = db.execute(
            "SELECT id, name, policy_type, config, namespace_filter "
            "FROM memory_policies WHERE enabled = 1 ORDER BY created_at"
        ).fetchall()
        return [
            {
                "id": r[0],
                "name": r[1],
                "policy_type": r[2],
                "config": json.loads(r[3]) if r[3] else {},
                "namespace_filter": r[4],
            }
            for r in rows
        ]
