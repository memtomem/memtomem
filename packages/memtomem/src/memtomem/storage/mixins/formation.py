"""Storage methods for review candidates and temporal assertions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


class FormationMixin:
    async def add_memory_candidate(self, candidate: dict[str, Any]) -> bool:
        db = self._get_db()
        cursor = db.execute(
            """
            INSERT OR IGNORE INTO memory_candidates (
                id, session_id, kind, operation, destination, content, evidence,
                matched_existing_ids, confidence, sensitivity, proposed_diff,
                status, extractor_version, fingerprint, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
            """,
            (
                candidate["id"],
                candidate["session_id"],
                candidate["kind"],
                candidate["operation"],
                candidate["destination"],
                candidate["content"],
                json.dumps(candidate.get("evidence", [])),
                json.dumps(candidate.get("matched_existing_ids", [])),
                candidate["confidence"],
                candidate.get("sensitivity", "normal"),
                candidate.get("proposed_diff", ""),
                candidate["extractor_version"],
                candidate["fingerprint"],
                candidate["created_at"],
                candidate["expires_at"],
            ),
        )
        db.commit()
        return cursor.rowcount > 0

    async def list_memory_candidates(
        self, status: str = "pending", limit: int = 100
    ) -> list[dict[str, Any]]:
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "UPDATE memory_candidates SET status='expired' "
            "WHERE status='pending' AND expires_at <= ?",
            (now,),
        )
        db.commit()
        rows = db.execute(
            """
            SELECT id, session_id, kind, operation, destination, content, evidence,
                   matched_existing_ids, confidence, sensitivity, proposed_diff,
                   status, extractor_version, reviewer, decision_reason,
                   created_at, expires_at, decided_at
            FROM memory_candidates WHERE status=? ORDER BY created_at LIMIT ?
            """,
            (status, limit),
        ).fetchall()
        keys = (
            "id",
            "session_id",
            "kind",
            "operation",
            "destination",
            "content",
            "evidence",
            "matched_existing_ids",
            "confidence",
            "sensitivity",
            "proposed_diff",
            "status",
            "extractor_version",
            "reviewer",
            "decision_reason",
            "created_at",
            "expires_at",
            "decided_at",
        )
        result = []
        for row in rows:
            item = dict(zip(keys, row))
            item["evidence"] = json.loads(item["evidence"])
            item["matched_existing_ids"] = json.loads(item["matched_existing_ids"])
            result.append(item)
        return result

    async def get_memory_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        for status in ("pending", "approved", "rejected", "expired"):
            rows = await self.list_memory_candidates(status=status, limit=10_000)
            for row in rows:
                if row["id"] == candidate_id:
                    return row
        return None

    async def decide_memory_candidate(
        self, candidate_id: str, status: str, reviewer: str, reason: str = ""
    ) -> bool:
        if status not in {"approved", "rejected"}:
            raise ValueError("candidate decision must be approved or rejected")
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cursor = db.execute(
            "UPDATE memory_candidates SET status=?, reviewer=?, decision_reason=?, decided_at=? "
            "WHERE id=? AND status='pending'",
            (status, reviewer, reason, now, candidate_id),
        )
        db.commit()
        return cursor.rowcount > 0

    async def add_assertion(
        self,
        *,
        assertion_id: str,
        entity_id: str,
        canonical_name: str,
        entity_type: str,
        predicate: str,
        object_value: str,
        source_chunk_id: str | None,
        recorded_at: str,
        valid_from: str | None = None,
        valid_to: str | None = None,
        confidence: float = 1.0,
        extractor_version: str = "manual-v1",
    ) -> None:
        db = self._get_db()
        db.execute(
            "INSERT OR IGNORE INTO canonical_entities "
            "(id, canonical_name, entity_type, aliases, created_at) VALUES (?, ?, ?, '[]', ?)",
            (entity_id, canonical_name, entity_type, recorded_at),
        )
        db.execute(
            """
            INSERT INTO memory_assertions (
                id, subject_entity_id, predicate, object_value, source_chunk_id,
                recorded_at, valid_from, valid_to, confidence, extractor_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                assertion_id,
                entity_id,
                predicate,
                object_value,
                source_chunk_id,
                recorded_at,
                valid_from,
                valid_to,
                confidence,
                extractor_version,
            ),
        )
        db.commit()

    async def link_assertions(self, source_id: str, target_id: str, edge_type: str) -> None:
        if edge_type not in {"supersedes", "contradicts", "supports"}:
            raise ValueError("invalid assertion edge type")
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "INSERT OR IGNORE INTO assertion_edges VALUES (?, ?, ?, ?)",
            (source_id, target_id, edge_type, now),
        )
        if edge_type == "supersedes":
            db.execute("UPDATE memory_assertions SET status='superseded' WHERE id=?", (target_id,))
        db.commit()

    async def query_assertions(
        self,
        canonical_name: str,
        predicate: str | None = None,
        as_of: str | None = None,
    ) -> list[dict[str, Any]]:
        db = self._get_db()
        rows = db.execute(
            "SELECT a.id, e.canonical_name, e.entity_type, a.predicate, a.object_value, "
            "a.source_chunk_id, a.recorded_at, a.valid_from, a.valid_to, a.confidence "
            "FROM memory_assertions a JOIN canonical_entities e "
            "ON e.id=a.subject_entity_id "
            "WHERE e.canonical_name=? AND a.status='active' "
            "AND (? IS NULL OR a.predicate=?) "
            "AND (? IS NULL OR a.valid_from IS NULL OR a.valid_from <= ?) "
            "AND (? IS NULL OR a.valid_to IS NULL OR a.valid_to > ?) "
            "AND (? IS NULL OR a.recorded_at <= ?) "
            "ORDER BY a.recorded_at DESC",
            (
                canonical_name,
                predicate,
                predicate,
                as_of,
                as_of,
                as_of,
                as_of,
                as_of,
                as_of,
            ),
        ).fetchall()
        keys = (
            "id",
            "subject",
            "entity_type",
            "predicate",
            "object",
            "source_chunk_id",
            "recorded_at",
            "valid_from",
            "valid_to",
            "confidence",
        )
        return [dict(zip(keys, row)) for row in rows]
