"""Storage methods for review candidates and temporal assertions."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any


class FormationMixin:
    _CANDIDATE_KEYS = (
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
        "claim_started_at",
    )

    @classmethod
    def _candidate_row(cls, row: tuple[Any, ...]) -> dict[str, Any]:
        item = dict(zip(cls._CANDIDATE_KEYS, row))
        item["evidence"] = json.loads(item["evidence"])
        item["matched_existing_ids"] = json.loads(item["matched_existing_ids"])
        return item

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
            "SELECT id, session_id, kind, operation, destination, content, evidence, "
            "matched_existing_ids, confidence, sensitivity, proposed_diff, status, "
            "extractor_version, reviewer, decision_reason, created_at, expires_at, "
            "decided_at, claim_started_at "
            "FROM memory_candidates WHERE status=? ORDER BY created_at LIMIT ?",
            (status, limit),
        ).fetchall()
        return [self._candidate_row(row) for row in rows]

    async def get_memory_candidate(self, candidate_id: str) -> dict[str, Any] | None:
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        db.execute(
            "UPDATE memory_candidates SET status='expired' "
            "WHERE id=? AND status='pending' AND expires_at <= ?",
            (candidate_id, now),
        )
        db.commit()
        row = db.execute(
            "SELECT id, session_id, kind, operation, destination, content, evidence, "
            "matched_existing_ids, confidence, sensitivity, proposed_diff, status, "
            "extractor_version, reviewer, decision_reason, created_at, expires_at, "
            "decided_at, claim_started_at "
            "FROM memory_candidates WHERE id=?",
            (candidate_id,),
        ).fetchone()
        return self._candidate_row(row) if row is not None else None

    async def claim_memory_candidate(
        self, candidate_id: str, reviewer: str, reason: str = ""
    ) -> dict[str, Any] | None:
        """Atomically claim a pending candidate before any durable write."""
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cursor = db.execute(
            "UPDATE memory_candidates SET status='writing', reviewer=?, decision_reason=?, "
            "claim_started_at=? "
            "WHERE id=? AND status='pending' AND expires_at > ?",
            (
                reviewer,
                reason,
                now,
                candidate_id,
                now,
            ),
        )
        if cursor.rowcount > 0:
            self._record_candidate_transition(
                db,
                candidate_id,
                "pending",
                "writing",
                reviewer,
                reason or "approval claim",
                now,
            )
        db.commit()
        if cursor.rowcount == 0:
            return None
        return await self.get_memory_candidate(candidate_id)

    async def release_memory_candidate(
        self,
        candidate_id: str,
        *,
        actor: str = "system",
        reason: str = "durable write failed or was cancelled",
    ) -> bool:
        """Release a failed write claim so the candidate can be retried."""
        db = self._get_db()
        cursor = db.execute(
            "UPDATE memory_candidates SET status='pending', reviewer=NULL, "
            "decision_reason=NULL, claim_started_at=NULL "
            "WHERE id=? AND status='writing'",
            (candidate_id,),
        )
        if cursor.rowcount > 0:
            self._record_candidate_transition(
                db,
                candidate_id,
                "writing",
                "pending",
                actor,
                reason,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        db.commit()
        return cursor.rowcount > 0

    async def finalize_memory_candidate(self, candidate_id: str) -> bool:
        """Finalize a successfully persisted candidate claim."""
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cursor = db.execute(
            "UPDATE memory_candidates SET status='approved', decided_at=?, claim_started_at=NULL "
            "WHERE id=? AND status='writing'",
            (now, candidate_id),
        )
        if cursor.rowcount > 0:
            self._record_candidate_transition(
                db,
                candidate_id,
                "writing",
                "approved",
                "system",
                "durable write completed",
                now,
            )
        db.commit()
        return cursor.rowcount > 0

    async def recover_stale_memory_candidates(
        self,
        *,
        stale_before: str,
        actor: str = "operator",
        limit: int = 100,
    ) -> list[str]:
        """Atomically return stale ``writing`` claims to ``pending``.

        A concurrent finalize and this recovery update serialize in SQLite;
        only the operation that first matches ``status='writing'`` succeeds.
        """
        if limit < 1 or limit > 1000:
            raise ValueError("recovery limit must be between 1 and 1000")
        if not actor.strip():
            raise ValueError("recovery actor cannot be empty")
        try:
            cutoff = datetime.fromisoformat(stale_before)
        except ValueError as exc:
            raise ValueError("stale_before must be an ISO-8601 datetime") from exc
        if cutoff.tzinfo is None:
            raise ValueError("stale_before must include a timezone")
        normalized_cutoff = cutoff.astimezone(timezone.utc).isoformat(timespec="seconds")
        db = self._get_db()
        recovered: list[str] = []
        try:
            # Serialize selection and updates across processes without relying
            # on SQLite 3.35's ``UPDATE ... RETURNING`` extension.
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute(
                "SELECT id FROM memory_candidates "
                "WHERE status='writing' AND claim_started_at IS NOT NULL "
                "AND claim_started_at <= ? ORDER BY claim_started_at, id LIMIT ?",
                (normalized_cutoff, limit),
            ).fetchall()
            now = datetime.now(timezone.utc).isoformat(timespec="seconds")
            reason = f"stale approval claim recovered (claim_started_at <= {normalized_cutoff})"
            for row in rows:
                candidate_id = str(row[0])
                cursor = db.execute(
                    "UPDATE memory_candidates SET status='pending', reviewer=NULL, "
                    "decision_reason=NULL, claim_started_at=NULL "
                    "WHERE id=? AND status='writing'",
                    (candidate_id,),
                )
                if cursor.rowcount == 0:
                    continue
                recovered.append(candidate_id)
                self._record_candidate_transition(
                    db,
                    candidate_id,
                    "writing",
                    "pending",
                    actor,
                    reason,
                    now,
                )
            db.commit()
        except Exception:
            db.rollback()
            raise
        return recovered

    async def mark_memory_candidate_write_uncertain(
        self,
        candidate_id: str,
        *,
        actor: str,
        reason: str,
    ) -> bool:
        """Quarantine a recovered claim after its durable write completed."""
        db = self._get_db()
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        cursor = db.execute(
            "UPDATE memory_candidates SET status='write_uncertain', reviewer=?, "
            "decision_reason=?, decided_at=?, claim_started_at=NULL "
            "WHERE id=? AND status='pending'",
            (actor, reason, now, candidate_id),
        )
        if cursor.rowcount > 0:
            self._record_candidate_transition(
                db,
                candidate_id,
                "pending",
                "write_uncertain",
                actor,
                reason,
                now,
            )
        db.commit()
        return cursor.rowcount > 0

    async def list_memory_candidate_transitions(self, candidate_id: str) -> list[dict[str, Any]]:
        db = self._get_db()
        rows = db.execute(
            "SELECT from_status, to_status, actor, reason, created_at "
            "FROM memory_candidate_transitions WHERE candidate_id=? ORDER BY id",
            (candidate_id,),
        ).fetchall()
        keys = ("from_status", "to_status", "actor", "reason", "created_at")
        return [dict(zip(keys, row)) for row in rows]

    @staticmethod
    def _record_candidate_transition(
        db: Any,
        candidate_id: str,
        from_status: str,
        to_status: str,
        actor: str,
        reason: str,
        created_at: str,
    ) -> None:
        db.execute(
            "INSERT INTO memory_candidate_transitions "
            "(candidate_id, from_status, to_status, actor, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (candidate_id, from_status, to_status, actor, reason, created_at),
        )

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
        if cursor.rowcount > 0:
            self._record_candidate_transition(
                db,
                candidate_id,
                "pending",
                status,
                reviewer,
                reason or f"candidate {status}",
                now,
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
        row = db.execute(
            "SELECT id FROM canonical_entities WHERE canonical_name=? AND entity_type=?",
            (canonical_name, entity_type),
        ).fetchone()
        if row is None:  # defensive: INSERT OR IGNORE may only lose to an id collision
            raise ValueError("unable to resolve canonical entity")
        resolved_entity_id = str(row[0])
        db.execute(
            """
            INSERT INTO memory_assertions (
                id, subject_entity_id, predicate, object_value, source_chunk_id,
                recorded_at, valid_from, valid_to, confidence, extractor_version
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                assertion_id,
                resolved_entity_id,
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
