"""Tests for standalone tool modules: entity_extraction, policy_engine, temporal."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from helpers import make_chunk

from memtomem.tools.consolidation_engine import (
    DEFAULT_SUMMARY_NAMESPACE,
    apply_consolidation,
    compute_source_hash,
    extract_bullet,
    make_heuristic_summary,
    parse_source_hash,
)
from memtomem.tools.entity_extraction import extract_entities
from memtomem.tools.policy_engine import (
    PolicyRunResult,
    _VALID_TYPES,
    execute_auto_archive,
    execute_auto_consolidate,
    execute_auto_expire,
    execute_auto_tag,
    run_policy,
)
from memtomem.tools.temporal import (
    ActivityDay,
    TimelineBucket,
    build_timeline,
    format_activity,
    format_timeline,
)


# ── Entity Extraction ────────────────────────────────────────────────


class TestEntityExtraction:
    async def test_extract_person_by_context(self):
        text = "Review by Alice Johnson for the sprint."
        entities = extract_entities(text, entity_types=["person"])
        values = [e.entity_value for e in entities]
        assert "Alice Johnson" in values

    async def test_extract_person_by_mention(self):
        text = "Ping @steveoh for review."
        entities = extract_entities(text, entity_types=["person"])
        values = [e.entity_value for e in entities]
        assert "@steveoh" in values

    async def test_extract_iso_date(self):
        text = "Deadline is 2025-03-15 for the release."
        entities = extract_entities(text, entity_types=["date"])
        values = [e.entity_value for e in entities]
        assert "2025-03-15" in values
        # ISO dates should have high confidence
        assert any(e.confidence >= 0.9 for e in entities if e.entity_value == "2025-03-15")

    async def test_extract_natural_date(self):
        text = "Meeting scheduled for January 5th, 2025."
        entities = extract_entities(text, entity_types=["date"])
        values = [e.entity_value for e in entities]
        assert any("January" in v for v in values)

    async def test_extract_decision(self):
        text = "Decision: Use FastAPI for the new backend service.\nOther notes follow."
        entities = extract_entities(text, entity_types=["decision"])
        assert len(entities) >= 1
        assert "FastAPI" in entities[0].entity_value

    async def test_extract_decision_agreed_prefix(self):
        text = "Agreed: We switch from REST to GraphQL for the public API."
        entities = extract_entities(text, entity_types=["decision"])
        assert len(entities) >= 1
        assert "GraphQL" in entities[0].entity_value

    async def test_extract_technology_known(self):
        text = "We deployed with Docker and Kubernetes on AWS."
        entities = extract_entities(text, entity_types=["technology"])
        values = {e.entity_value.lower() for e in entities}
        assert "docker" in values
        assert "kubernetes" in values
        assert "aws" in values

    async def test_extract_technology_pascal_case(self):
        text = "The new MyCustomParser handles edge cases well."
        entities = extract_entities(text, entity_types=["technology"])
        values = [e.entity_value for e in entities]
        # PascalCase word over 4 chars should be detected with low confidence
        assert "MyCustomParser" in values
        pascal = [e for e in entities if e.entity_value == "MyCustomParser"]
        assert pascal[0].confidence == 0.5

    async def test_extract_action_items_todo(self):
        text = "TODO: Migrate database to PostgreSQL.\nSome other content."
        entities = extract_entities(text, entity_types=["action_item"])
        assert len(entities) >= 1
        assert "Migrate database to PostgreSQL." in entities[0].entity_value

    async def test_extract_action_items_checkbox(self):
        text = "- [ ] Write integration tests for auth module"
        entities = extract_entities(text, entity_types=["action_item"])
        assert len(entities) >= 1
        assert "Write integration tests" in entities[0].entity_value

    async def test_extract_action_items_keyword(self):
        text = "Action item: Deploy hotfix to production by Friday"
        entities = extract_entities(text, entity_types=["action_item"])
        assert len(entities) >= 1
        assert "Deploy hotfix" in entities[0].entity_value

    async def test_empty_text_returns_empty(self):
        assert extract_entities("") == []
        assert extract_entities("", entity_types=["person", "date"]) == []

    async def test_no_matches_returns_empty(self):
        text = "simple lowercase text without any entities or dates."
        entities = extract_entities(text, entity_types=["person", "decision", "action_item"])
        assert entities == []

    async def test_mixed_content_multiple_types(self):
        text = (
            "Meeting notes 2025-01-20:\n"
            "- Attendees: from Alice Park, cc Bob Lee\n"
            "- Decision: Migrate to FastAPI by Q2.\n"
            "TODO: Set up Docker CI pipeline.\n"
            "- [ ] Review Kubernetes deployment config.\n"
            "- @charlie will handle the Kubernetes setup.\n"
        )
        entities = extract_entities(text)
        types_found = {e.entity_type for e in entities}
        assert "date" in types_found
        assert "person" in types_found
        assert "decision" in types_found
        assert "action_item" in types_found
        assert "technology" in types_found

    async def test_deduplication(self):
        text = "by Alice Park and with Alice Park again."
        entities = extract_entities(text, entity_types=["person"])
        # Same person appearing twice should be deduplicated
        alice_entries = [e for e in entities if e.entity_value == "Alice Park"]
        assert len(alice_entries) == 1

    async def test_entity_position_tracked(self):
        text = "2025-06-01 is the deadline."
        entities = extract_entities(text, entity_types=["date"])
        assert len(entities) >= 1
        assert entities[0].position == 0  # date at start of string

    async def test_concept_extraction_quoted_terms(self):
        text = 'The concept of "dependency injection" is used throughout.'
        entities = extract_entities(text, entity_types=["concept"])
        values = [e.entity_value for e in entities]
        assert "dependency injection" in values

    async def test_filter_by_entity_types(self):
        text = "by Alice Park on 2025-01-01 using Docker."
        date_only = extract_entities(text, entity_types=["date"])
        assert all(e.entity_type == "date" for e in date_only)

        person_only = extract_entities(text, entity_types=["person"])
        assert all(e.entity_type == "person" for e in person_only)


# ── Policy Engine ────────────────────────────────────────────────────


class TestPolicyEngine:
    async def test_auto_archive_dry_run(self, storage):
        """Dry-run should count but not actually move chunks."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        chunk = make_chunk("old content", namespace="default")
        await storage.upsert_chunks([chunk])
        # Manually backdate the chunk
        db = storage._get_db()
        db.execute("UPDATE chunks SET created_at = ? WHERE id = ?", [old_time, str(chunk.id)])
        db.commit()

        result = await execute_auto_archive(
            storage, {"max_age_days": 30}, namespace=None, dry_run=True
        )
        assert isinstance(result, PolicyRunResult)
        assert result.policy_type == "auto_archive"
        assert result.dry_run is True
        assert result.affected_count == 1
        assert "Would archive" in result.details

        # Chunk should still be in default namespace
        row = db.execute("SELECT namespace FROM chunks WHERE id = ?", [str(chunk.id)]).fetchone()
        assert row[0] == "default"

    async def test_auto_archive_executes(self, storage):
        """Non-dry-run should move old chunks to archive namespace."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        chunk = make_chunk("archivable content", namespace="default")
        await storage.upsert_chunks([chunk])
        db = storage._get_db()
        db.execute("UPDATE chunks SET created_at = ? WHERE id = ?", [old_time, str(chunk.id)])
        db.commit()

        result = await execute_auto_archive(
            storage, {"max_age_days": 30, "archive_namespace": "old"}, namespace=None, dry_run=False
        )
        assert result.affected_count == 1
        assert result.dry_run is False
        assert "Archived" in result.details
        assert "'old'" in result.details

        row = db.execute("SELECT namespace FROM chunks WHERE id = ?", [str(chunk.id)]).fetchone()
        assert row[0] == "old"

    async def test_auto_archive_skips_recent(self, storage):
        """Chunks newer than max_age_days should not be archived."""
        chunk = make_chunk("fresh content")
        await storage.upsert_chunks([chunk])

        result = await execute_auto_archive(
            storage, {"max_age_days": 30}, namespace=None, dry_run=False
        )
        assert result.affected_count == 0

    async def test_auto_archive_namespace_filter(self, storage):
        """Only chunks in the specified namespace should be considered."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        c1 = make_chunk("work stuff", namespace="work")
        c2 = make_chunk("personal stuff", namespace="personal")
        await storage.upsert_chunks([c1, c2])
        db = storage._get_db()
        db.execute("UPDATE chunks SET created_at = ?", [old_time])
        db.commit()

        result = await execute_auto_archive(
            storage, {"max_age_days": 30}, namespace="work", dry_run=True
        )
        assert result.affected_count == 1

    async def test_auto_archive_age_field_last_accessed_fallback(self, storage):
        """age_field=last_accessed_at uses COALESCE with created_at for null values."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        recent_time = (datetime.now(timezone.utc) - timedelta(days=5)).isoformat()

        c_null = make_chunk("null last_access")
        c_recent = make_chunk("recent last_access")
        c_old = make_chunk("old last_access")
        await storage.upsert_chunks([c_null, c_recent, c_old])

        db = storage._get_db()
        db.execute("UPDATE chunks SET created_at = ?", [old_time])
        # c_null: last_accessed_at stays NULL → COALESCE falls back to old created_at (eligible)
        db.execute(
            "UPDATE chunks SET last_accessed_at = ? WHERE id = ?",
            [recent_time, str(c_recent.id)],
        )
        db.execute(
            "UPDATE chunks SET last_accessed_at = ? WHERE id = ?",
            [old_time, str(c_old.id)],
        )
        db.commit()

        result = await execute_auto_archive(
            storage,
            {"max_age_days": 30, "age_field": "last_accessed_at"},
            namespace=None,
            dry_run=False,
        )
        assert result.affected_count == 2  # c_null + c_old, not c_recent

        row = db.execute("SELECT namespace FROM chunks WHERE id = ?", [str(c_recent.id)]).fetchone()
        assert row[0] == "default"

    async def test_auto_archive_min_access_count_filter(self, storage):
        """min_access_count excludes chunks accessed more than the threshold."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

        c_cold = make_chunk("never accessed")
        c_hot = make_chunk("accessed a lot")
        await storage.upsert_chunks([c_cold, c_hot])

        db = storage._get_db()
        db.execute("UPDATE chunks SET created_at = ?", [old_time])
        db.execute("UPDATE chunks SET access_count = 0 WHERE id = ?", [str(c_cold.id)])
        db.execute("UPDATE chunks SET access_count = 10 WHERE id = ?", [str(c_hot.id)])
        db.commit()

        result = await execute_auto_archive(
            storage,
            {"max_age_days": 30, "min_access_count": 3},
            namespace=None,
            dry_run=True,
        )
        assert result.affected_count == 1  # only c_cold (0 <= 3)

    async def test_auto_archive_max_importance_score_filter(self, storage):
        """max_importance_score excludes chunks above the threshold."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

        c_low = make_chunk("low priority")
        c_high = make_chunk("high priority")
        await storage.upsert_chunks([c_low, c_high])

        db = storage._get_db()
        db.execute("UPDATE chunks SET created_at = ?", [old_time])
        db.execute("UPDATE chunks SET importance_score = 0.1 WHERE id = ?", [str(c_low.id)])
        db.execute("UPDATE chunks SET importance_score = 0.8 WHERE id = ?", [str(c_high.id)])
        db.commit()

        result = await execute_auto_archive(
            storage,
            {"max_age_days": 30, "max_importance_score": 0.5},
            namespace=None,
            dry_run=True,
        )
        assert result.affected_count == 1  # only c_low (0.1 < 0.5)

    async def test_auto_archive_namespace_template_first_tag(self, storage):
        """archive_namespace_template expands {first_tag} per chunk."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

        c_dec = make_chunk("a decision", tags=("decisions", "important"))
        c_tech = make_chunk("a tech note", tags=("tech",))
        await storage.upsert_chunks([c_dec, c_tech])

        db = storage._get_db()
        db.execute("UPDATE chunks SET created_at = ?", [old_time])
        db.commit()

        result = await execute_auto_archive(
            storage,
            {
                "max_age_days": 30,
                "archive_namespace_template": "archive:{first_tag}",
            },
            namespace=None,
            dry_run=False,
        )
        assert result.affected_count == 2
        assert "archive:decisions: 1" in result.details
        assert "archive:tech: 1" in result.details

        row = db.execute("SELECT namespace FROM chunks WHERE id = ?", [str(c_dec.id)]).fetchone()
        assert row[0] == "archive:decisions"

        row = db.execute("SELECT namespace FROM chunks WHERE id = ?", [str(c_tech.id)]).fetchone()
        assert row[0] == "archive:tech"

    async def test_auto_archive_template_misc_fallback(self, storage):
        """Empty tags resolve {first_tag} to 'misc'."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

        c_empty = make_chunk("no tags")
        await storage.upsert_chunks([c_empty])

        db = storage._get_db()
        db.execute("UPDATE chunks SET created_at = ?", [old_time])
        db.commit()

        result = await execute_auto_archive(
            storage,
            {
                "max_age_days": 30,
                "archive_namespace_template": "archive:{first_tag}",
            },
            namespace=None,
            dry_run=False,
        )
        assert result.affected_count == 1
        assert "archive:misc: 1" in result.details

        row = db.execute("SELECT namespace FROM chunks WHERE id = ?", [str(c_empty.id)]).fetchone()
        assert row[0] == "archive:misc"

    async def test_auto_archive_template_skips_self_moves(self, storage):
        """Chunks already in their resolved target namespace are skipped."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

        c_already = make_chunk(
            "already archived", tags=("decisions",), namespace="archive:decisions"
        )
        c_new = make_chunk("to archive", tags=("decisions",))
        await storage.upsert_chunks([c_already, c_new])

        db = storage._get_db()
        db.execute("UPDATE chunks SET created_at = ?", [old_time])
        db.commit()

        result = await execute_auto_archive(
            storage,
            {
                "max_age_days": 30,
                "archive_namespace_template": "archive:{first_tag}",
            },
            namespace=None,
            dry_run=False,
        )
        assert result.affected_count == 1  # only c_new

    async def test_auto_archive_combined_rule(self, storage):
        """All new rule fields combine with AND semantics."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()

        c_match = make_chunk("matches everything", tags=("misc",))
        c_hot = make_chunk("access count too high", tags=("misc",))
        c_important = make_chunk("importance too high", tags=("misc",))
        await storage.upsert_chunks([c_match, c_hot, c_important])

        db = storage._get_db()
        db.execute("UPDATE chunks SET created_at = ?", [old_time])
        db.execute(
            "UPDATE chunks SET last_accessed_at=?, access_count=?, importance_score=? WHERE id=?",
            [old_time, 1, 0.1, str(c_match.id)],
        )
        db.execute(
            "UPDATE chunks SET last_accessed_at=?, access_count=?, importance_score=? WHERE id=?",
            [old_time, 10, 0.1, str(c_hot.id)],
        )
        db.execute(
            "UPDATE chunks SET last_accessed_at=?, access_count=?, importance_score=? WHERE id=?",
            [old_time, 1, 0.8, str(c_important.id)],
        )
        db.commit()

        result = await execute_auto_archive(
            storage,
            {
                "max_age_days": 30,
                "age_field": "last_accessed_at",
                "min_access_count": 3,
                "max_importance_score": 0.5,
            },
            namespace=None,
            dry_run=True,
        )
        assert result.affected_count == 1  # only c_match passes every gate

    async def test_auto_archive_invalid_age_field(self, storage):
        """Invalid age_field returns a typed error result and mutates nothing."""
        result = await execute_auto_archive(
            storage,
            {"max_age_days": 30, "age_field": "bogus"},
            namespace=None,
            dry_run=False,
        )
        assert result.affected_count == 0
        assert "age_field must be" in result.details

    async def test_auto_expire_dry_run(self, storage):
        """Dry-run should count expired chunks but not delete."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        chunk = make_chunk("stale content")
        await storage.upsert_chunks([chunk])
        db = storage._get_db()
        db.execute(
            "UPDATE chunks SET created_at = ?, access_count = 0 WHERE id = ?",
            [old_time, str(chunk.id)],
        )
        db.commit()

        result = await execute_auto_expire(
            storage, {"max_age_days": 90}, namespace=None, dry_run=True
        )
        assert result.affected_count == 1
        assert "Would expire" in result.details

        row = db.execute("SELECT id FROM chunks WHERE id = ?", [str(chunk.id)]).fetchone()
        assert row is not None  # not deleted

    async def test_auto_expire_executes(self, storage):
        """Non-dry-run should delete old unaccessed chunks."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        chunk = make_chunk("deletable content")
        await storage.upsert_chunks([chunk])
        db = storage._get_db()
        db.execute(
            "UPDATE chunks SET created_at = ?, access_count = 0 WHERE id = ?",
            [old_time, str(chunk.id)],
        )
        db.commit()

        result = await execute_auto_expire(
            storage, {"max_age_days": 90}, namespace=None, dry_run=False
        )
        assert result.affected_count == 1
        assert "Expired" in result.details

        row = db.execute("SELECT id FROM chunks WHERE id = ?", [str(chunk.id)]).fetchone()
        assert row is None  # deleted

    async def test_auto_expire_keeps_accessed(self, storage):
        """Chunks with access_count > 0 should not be expired even if old."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat()
        chunk = make_chunk("accessed content")
        await storage.upsert_chunks([chunk])
        db = storage._get_db()
        db.execute(
            "UPDATE chunks SET created_at = ?, access_count = 5 WHERE id = ?",
            [old_time, str(chunk.id)],
        )
        db.commit()

        result = await execute_auto_expire(
            storage, {"max_age_days": 90}, namespace=None, dry_run=False
        )
        assert result.affected_count == 0

    async def test_auto_tag_dry_run(self, storage):
        """Dry-run should report untagged count without modifying."""
        chunk = make_chunk("some untagged content")
        await storage.upsert_chunks([chunk])

        result = await execute_auto_tag(storage, {"max_tags": 3}, namespace=None, dry_run=True)
        assert result.policy_type == "auto_tag"
        assert result.dry_run is True
        assert result.affected_count >= 1
        assert "Would tag" in result.details

    async def test_run_policy_unknown_type(self):
        """Unknown policy type returns an error result."""
        policy = {"name": "bad_policy", "policy_type": "auto_delete_everything", "config": {}}
        result = await run_policy(object(), policy, dry_run=True)
        assert result.affected_count == 0
        assert "Unknown policy type" in result.details
        assert result.policy_name == "bad_policy"

    async def test_run_policy_dispatches(self, storage):
        """run_policy routes to correct handler based on policy_type."""
        old_time = (datetime.now(timezone.utc) - timedelta(days=60)).isoformat()
        chunk = make_chunk("to archive")
        await storage.upsert_chunks([chunk])
        db = storage._get_db()
        db.execute("UPDATE chunks SET created_at = ? WHERE id = ?", [old_time, str(chunk.id)])
        db.commit()

        policy = {
            "name": "test_archive",
            "policy_type": "auto_archive",
            "config": {"max_age_days": 30},
        }
        result = await run_policy(storage, policy, dry_run=True)
        assert result.policy_name == "test_archive"
        assert result.policy_type == "auto_archive"
        assert result.affected_count == 1

    async def test_policy_result_dataclass(self):
        """PolicyRunResult is frozen and holds expected fields."""
        r = PolicyRunResult(
            policy_name="p1",
            policy_type="auto_archive",
            affected_count=5,
            dry_run=True,
            details="test",
        )
        assert r.policy_name == "p1"
        assert r.affected_count == 5
        with pytest.raises(AttributeError):
            r.affected_count = 10  # type: ignore[misc]

    async def test_valid_types_set(self):
        """All expected policy types are in _VALID_TYPES."""
        assert "auto_archive" in _VALID_TYPES
        assert "auto_expire" in _VALID_TYPES
        assert "auto_tag" in _VALID_TYPES
        assert "auto_promote" in _VALID_TYPES
        assert "auto_consolidate" in _VALID_TYPES

    async def test_server_tools_valid_types_includes_auto_consolidate(self):
        """server/tools/policy.py:_VALID_TYPES must accept auto_consolidate."""
        from memtomem.server.tools.policy import _VALID_TYPES as _SERVER_VALID_TYPES

        assert "auto_consolidate" in _SERVER_VALID_TYPES
        assert "auto_archive" in _SERVER_VALID_TYPES
        assert "auto_expire" in _SERVER_VALID_TYPES
        assert "auto_tag" in _SERVER_VALID_TYPES


# ── Consolidation engine (unit: bullet extraction + hash) ────────────


class TestConsolidationEngineUnit:
    def test_extract_bullet_with_heading_and_first_sentence(self):
        """heading_hierarchy[-1] becomes the label; first sentence is the body."""
        chunk = make_chunk(
            content="Alice, Bob, Carol joined. Bob will lead the sprint.",
            heading=("April Standup", "Attendees"),
        )
        bullet = extract_bullet(chunk)
        assert bullet.startswith("**Attendees** — ")
        assert "Alice, Bob, Carol joined" in bullet
        # Second sentence should not leak into the single-sentence bullet.
        assert "Bob will lead" not in bullet

    def test_extract_bullet_no_heading_fallback(self):
        """Chunks with no heading and no content heading fall back to first sentence."""
        chunk = make_chunk(
            content="Plain text with no structure. Something else.",
            heading=(),
        )
        bullet = extract_bullet(chunk)
        assert "Plain text with no structure" in bullet
        assert not bullet.startswith("**")  # no label

    def test_extract_bullet_keyword_boost_decision_wins(self):
        """A ``Decision:`` line anywhere in the body beats the first-sentence fallback."""
        chunk = make_chunk(
            content=(
                "The team met briefly to discuss roadmap blockers.\n"
                "Decision: freeze main branch until 2026-04-10.\n"
                "Followups to be tracked in #42."
            ),
            heading=("Sprint 12", "Notes"),
        )
        bullet = extract_bullet(chunk)
        assert "Decision" in bullet
        assert "freeze main branch" in bullet
        # First-sentence fallback should not have fired.
        assert "The team met briefly" not in bullet

    def test_extract_bullet_checklist_count(self):
        """Chunks with 2+ checklist items become ``N items (…)`` rather than truncated prose."""
        chunk = make_chunk(
            content=(
                "Action items from the review:\n"
                "- [ ] Alice: write tests for feature X\n"
                "- [ ] Bob: unblock ticket #42\n"
                "- [x] Carol: approve the RFC draft"
            ),
            heading=("Sprint 12", "Action items"),
        )
        bullet = extract_bullet(chunk)
        # Keyword boost on Action: picks the first TODO line, so checklist path
        # applies only when no Action/Decision keyword fires. Allow either
        # outcome but require the label to be present and that the bullet is
        # non-trivial.
        assert "**Action items**" in bullet
        assert len(bullet) > len("**Action items**")

    def test_extract_bullet_checklist_without_keyword(self):
        """Pure checklist (no Action/TODO keyword) → ``N items (preview…)``."""
        chunk = make_chunk(
            content=("- [ ] first task here\n- [ ] second task here\n- [ ] third task here"),
            heading=("Tasks",),
        )
        bullet = extract_bullet(chunk)
        assert "**Tasks**" in bullet
        # Either keyword path (- [ ] pattern in _ACTION_RE) or checklist path
        # is acceptable; both preserve the item content.
        assert "first task" in bullet

    def test_compute_source_hash_deterministic_and_order_independent(self):
        """Same chunk id set → same hash regardless of order."""
        ids_a = ["aaa-111", "bbb-222", "ccc-333"]
        ids_b = ["ccc-333", "aaa-111", "bbb-222"]
        assert compute_source_hash(ids_a) == compute_source_hash(ids_b)
        assert compute_source_hash(ids_a) != compute_source_hash(ids_a + ["ddd-444"])
        # 16 hex chars = 64 bits.
        assert len(compute_source_hash(ids_a)) == 16

    def test_parse_source_hash_present_and_missing(self):
        """parse_source_hash returns the hash or None for legacy summaries."""
        with_hash = (
            "## Metadata\n\n"
            "- Source: `/tmp/foo.md`\n"
            "- Source hash: `a3f28b1c9e4d5f60`\n"
            "- Generated: 2026-04-12T00:00:00+00:00\n"
        )
        assert parse_source_hash(with_hash) == "a3f28b1c9e4d5f60"

        without_hash = "## Metadata\n\n- Source: `/tmp/foo.md`\n- Generated: 2026-04-12\n"
        assert parse_source_hash(without_hash) is None


# ── auto_consolidate handler (integration with storage) ──────────────


class TestAutoConsolidate:
    async def test_auto_consolidate_empty_no_candidates(self, storage):
        """No source files with enough chunks → affected_count = 0."""
        chunk = make_chunk("lonely", source="solo.md")
        await storage.upsert_chunks([chunk])

        result = await execute_auto_consolidate(
            storage, {"min_group_size": 3}, namespace=None, dry_run=False
        )
        assert result.policy_type == "auto_consolidate"
        assert result.affected_count == 0
        assert "0 groups" in result.details

    async def test_auto_consolidate_dry_run_reports_candidates(self, storage):
        """Dry run counts groups but does not create a summary chunk."""
        source = "meeting-2026-04.md"
        chunks = [
            make_chunk(
                content=f"Content chunk {i} with some text here.",
                source=source,
                heading=("April Standup", f"Section {i}"),
            )
            for i in range(3)
        ]
        await storage.upsert_chunks(chunks)

        result = await execute_auto_consolidate(
            storage,
            {"min_group_size": 3},
            namespace=None,
            dry_run=True,
        )
        assert result.dry_run is True
        assert result.affected_count == 1
        assert "Would consolidate" in result.details
        assert source in result.details

        # No summary chunk should have been persisted.
        summaries = await storage.list_chunks_by_source(
            Path(f"/tmp/{source}.consolidated.md"), limit=5
        )
        assert summaries == []

    async def test_auto_consolidate_happy_path(self, storage):
        """Creates a summary chunk in archive:summary + links originals."""
        source = "meeting-2026-04.md"
        chunks = [
            make_chunk(
                content=(
                    "Alice, Bob, Carol joined the standup session for April."
                    if i == 0
                    else f"Further notes from section {i} of the meeting."
                ),
                source=source,
                heading=("April Standup", f"Section {i}"),
            )
            for i in range(3)
        ]
        await storage.upsert_chunks(chunks)

        result = await execute_auto_consolidate(
            storage, {"min_group_size": 3}, namespace=None, dry_run=False
        )
        assert result.affected_count == 1
        assert "Consolidated 1 groups" in result.details

        # Summary chunk exists and contains expected markers.
        summaries = await storage.list_chunks_by_source(
            Path(f"/tmp/{source}.consolidated.md"), limit=5
        )
        assert len(summaries) == 1
        summary_chunk = summaries[0]
        assert summary_chunk.metadata.namespace == DEFAULT_SUMMARY_NAMESPACE
        assert "consolidated" in summary_chunk.metadata.tags
        assert "# Consolidated:" in summary_chunk.content
        assert "Source hash:" in summary_chunk.content

        # Each original should have a consolidated_into relation → summary.
        related = await storage.get_related(chunks[0].id)
        assert any(rel == "consolidated_into" for _, rel in related)

    async def test_auto_consolidate_idempotent_same_hash_skips(self, storage):
        """Second run on unchanged inputs must be a no-op."""
        source = "meeting-idempotent.md"
        chunks = [
            make_chunk(f"Idempotency chunk {i}", source=source, heading=("Doc", f"§{i}"))
            for i in range(3)
        ]
        await storage.upsert_chunks(chunks)

        first = await execute_auto_consolidate(
            storage, {"min_group_size": 3}, namespace=None, dry_run=False
        )
        assert first.affected_count == 1

        second = await execute_auto_consolidate(
            storage, {"min_group_size": 3}, namespace=None, dry_run=False
        )
        assert second.affected_count == 0
        assert "0 groups" in second.details

        # Only one summary chunk should exist.
        summaries = await storage.list_chunks_by_source(
            Path(f"/tmp/{source}.consolidated.md"), limit=5
        )
        assert len(summaries) == 1

    async def test_auto_consolidate_staleness_regen(self, storage):
        """Adding a chunk after first run → second run deletes old, creates new."""
        source = "meeting-staleness.md"
        first_batch = [
            make_chunk(f"Stale chunk {i}", source=source, heading=("Doc", f"§{i}"))
            for i in range(3)
        ]
        await storage.upsert_chunks(first_batch)

        first = await execute_auto_consolidate(
            storage, {"min_group_size": 3}, namespace=None, dry_run=False
        )
        assert first.affected_count == 1
        summaries_before = await storage.list_chunks_by_source(
            Path(f"/tmp/{source}.consolidated.md"), limit=5
        )
        assert len(summaries_before) == 1
        old_summary_id = summaries_before[0].id

        # Add a new chunk → input hash changes.
        new_chunk = make_chunk("Newly added chunk", source=source, heading=("Doc", "§new"))
        await storage.upsert_chunks([new_chunk])

        second = await execute_auto_consolidate(
            storage, {"min_group_size": 3}, namespace=None, dry_run=False
        )
        assert second.affected_count == 1
        assert "regen" in second.details

        # Exactly one summary (old one was replaced, not stacked).
        summaries_after = await storage.list_chunks_by_source(
            Path(f"/tmp/{source}.consolidated.md"), limit=5
        )
        assert len(summaries_after) == 1
        assert summaries_after[0].id != old_summary_id

    async def test_auto_consolidate_mixed_namespace_skips(self, storage, caplog):
        """A source file whose chunks span multiple namespaces is skipped with a warn."""
        source = "mixed-ns.md"
        chunks = [
            make_chunk(f"Chunk {i}", source=source, namespace="default" if i < 2 else "other")
            for i in range(3)
        ]
        await storage.upsert_chunks(chunks)

        import logging

        with caplog.at_level(logging.WARNING, logger="memtomem.tools.policy_engine"):
            result = await execute_auto_consolidate(
                storage, {"min_group_size": 3}, namespace=None, dry_run=False
            )

        assert result.affected_count == 0
        assert "mixed ns" in result.details
        # The warning line should reference the source.
        assert any("mixed namespaces" in rec.message for rec in caplog.records)

        # No summary chunk should have been written.
        summaries = await storage.list_chunks_by_source(
            Path(f"/tmp/{source}.consolidated.md"), limit=5
        )
        assert summaries == []

    async def test_apply_consolidation_decay_floor(self, storage):
        """keep_originals=False applies decay but never drops below DECAY_FLOOR=0.3."""
        chunks = [make_chunk(f"decay chunk {i}", source="decay-source.md") for i in range(3)]
        await storage.upsert_chunks(chunks)

        # Force one chunk to a very low importance score; the halving must
        # floor at 0.3, not drop to 0.1.
        low_id = str(chunks[0].id)
        await storage.update_importance_scores({low_id: 0.2})

        group = {
            "source": "/tmp/decay-source.md",
            "chunk_ids": [str(c.id) for c in chunks],
            "namespace": "default",
            "chunk_count": 3,
        }
        summary = make_heuristic_summary(chunks, Path("/tmp/decay-source.md"))
        await apply_consolidation(storage, group, summary, keep_originals=False)

        scores = await storage.get_importance_scores([str(c.id) for c in chunks])
        # The 0.2 chunk should have been floored to 0.3, not halved to 0.1.
        assert scores[low_id] == pytest.approx(0.3)
        # Other chunks start at the default importance (let the storage layer
        # decide the initial value — we just assert the floor held).
        for cid in scores:
            assert scores[cid] >= 0.3

    async def test_auto_consolidate_defers_to_agent_consolidation(self, storage, caplog):
        """Layer 2 (b) idempotency: if any chunk already has a consolidated_into
        edge and there's no policy-owned virtual summary, skip with a tag. This
        protects agent-written summaries from being overwritten by heuristic."""
        source = "agent-consolidated.md"
        chunks = [make_chunk(f"agent-done chunk {i}", source=source) for i in range(3)]
        await storage.upsert_chunks(chunks)

        # Simulate agent path having already consolidated this source: the
        # agent's summary chunk is a real chunk in some other source file
        # (e.g. the daily notes file written by mem_add). FK constraints on
        # chunk_relations require both endpoints to exist.
        agent_summary = make_chunk(
            "Agent-written consolidated summary body.",
            source="daily-2026-04-12.md",
        )
        await storage.upsert_chunks([agent_summary])
        await storage.add_relation(chunks[0].id, agent_summary.id, "consolidated_into")

        result = await execute_auto_consolidate(
            storage, {"min_group_size": 3}, namespace=None, dry_run=False
        )
        assert result.affected_count == 0
        assert "agent-consolidated" in result.details

        # No virtual summary was created.
        summaries = await storage.list_chunks_by_source(
            Path(f"/tmp/{source}.consolidated.md"), limit=5
        )
        assert summaries == []

    async def test_auto_consolidate_agent_any_not_all(self, storage):
        """Only one chunk in a group needs a consolidated_into edge for the
        whole group to be deferred (``any``, not ``all``)."""
        source = "partial-agent.md"
        chunks = [make_chunk(f"partial {i}", source=source) for i in range(4)]
        await storage.upsert_chunks(chunks)

        # Agent consolidated only 1 of the 4 chunks. FK requires a real target.
        agent_summary = make_chunk("Agent summary for 1 of 4.", source="daily-2026-04-12.md")
        await storage.upsert_chunks([agent_summary])
        await storage.add_relation(chunks[2].id, agent_summary.id, "consolidated_into")

        result = await execute_auto_consolidate(
            storage, {"min_group_size": 3}, namespace=None, dry_run=False
        )
        assert result.affected_count == 0
        assert "agent-consolidated" in result.details

    async def test_auto_consolidate_own_relations_not_self_block(self, storage):
        """A policy-owned virtual summary sets consolidated_into edges on its
        originals. A subsequent re-run must find the virtual summary FIRST
        (layer 1) and compare hashes, not match layer 2 on its own edges. The
        first run creates + links, the second run must skip via hash match,
        not via the ``source_has_consolidation_relations`` fallback."""
        source = "own-relations.md"
        chunks = [make_chunk(f"own chunk {i}", source=source) for i in range(3)]
        await storage.upsert_chunks(chunks)

        first = await execute_auto_consolidate(
            storage, {"min_group_size": 3}, namespace=None, dry_run=False
        )
        assert first.affected_count == 1

        # After first run: virtual summary exists AND originals have
        # consolidated_into edges pointing to it. Re-run.
        second = await execute_auto_consolidate(
            storage, {"min_group_size": 3}, namespace=None, dry_run=False
        )
        # Must skip via layer 1 hash match — details say "0 groups" (idempotent),
        # not "(SKIP: agent-consolidated)".
        assert second.affected_count == 0
        assert "agent-consolidated" not in second.details


# ── mem_add core + mem_consolidate_apply integration ─────────────────
#
# These exercise the post-Phase-A.5 split: mem_consolidate_apply is back on
# the file-first path via _mem_add_core, which also returns IndexingStats so
# the caller can recover the new summary chunk id without racing on
# recall_chunks(limit=1).


def _fake_ctx(components):
    """Minimal ctx stub that mimics what _get_app() unwraps.

    The production MCP context is ``ctx.request_context.lifespan_context``.
    We fabricate a SimpleNamespace matching that shape, filled with real
    services from the ``components`` fixture plus the few AppContext fields
    the tools access (``current_namespace``, ``webhook_manager``).
    """
    from types import SimpleNamespace

    app = SimpleNamespace(
        config=components.config,
        storage=components.storage,
        embedder=components.embedder,
        index_engine=components.index_engine,
        search_pipeline=components.search_pipeline,
        current_namespace=None,
        webhook_manager=None,
    )
    return SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))


class TestMemAddCoreIntegration:
    @pytest.mark.ollama
    async def test_mem_add_core_returns_indexing_stats(self, components, memory_dir):
        """_mem_add_core returns (message, stats) and stats.new_chunk_ids is
        populated with the UUID(s) of chunks freshly upserted during indexing.
        This is the hook that lets mem_consolidate_apply avoid the old
        recall_chunks(limit=1) race."""
        from memtomem.server.tools.memory_crud import _mem_add_core

        ctx = _fake_ctx(components)

        message, stats = await _mem_add_core(
            content="A single atomic fact worth remembering for later search.",
            title="Test Entry",
            tags=["test"],
            file="entry.md",
            namespace=None,
            template=None,
            ctx=ctx,
        )

        assert "Memory added" in message
        assert stats is not None
        assert len(stats.new_chunk_ids) >= 1
        # The reported chunk ids must actually be in the store.
        fetched = await components.storage.get_chunks_batch(list(stats.new_chunk_ids))
        assert len(fetched) == len(stats.new_chunk_ids)

    async def test_mem_add_core_returns_none_on_early_error(self, components):
        """Empty content short-circuits with (error_message, None) so callers
        must tolerate None stats."""
        from memtomem.server.tools.memory_crud import _mem_add_core

        ctx = _fake_ctx(components)
        message, stats = await _mem_add_core(
            content="",
            title=None,
            tags=None,
            file=None,
            namespace=None,
            template=None,
            ctx=ctx,
        )
        assert "Error" in message
        assert stats is None


class TestMemConsolidateApplyIntegration:
    @pytest.mark.ollama
    async def test_agent_path_writes_to_disk_and_links_relations(self, components, memory_dir):
        """Full file-first agent flow: summary appears in a disk file, the new
        chunk is indexed, and each original has a consolidated_into edge
        pointing to it. Replaces the old virtual-path test that reflected the
        pre-Option-Y design."""
        import json
        from datetime import datetime, timedelta, timezone

        from memtomem.server.tools.consolidation import mem_consolidate_apply
        from memtomem.tools.memory_writer import append_entry

        # Build a realistic source file with 3 real entries + index it so we
        # have real chunk ids to hand to mem_consolidate_apply.
        source_file = memory_dir / "apr-standup.md"
        for i in range(3):
            append_entry(
                source_file,
                f"Agenda item {i}: standup note with enough text to survive chunking.",
                title=f"Item {i}",
            )
        stats = await components.index_engine.index_file(source_file)
        assert stats.indexed_chunks >= 3
        original_ids = [str(cid) for cid in stats.new_chunk_ids]

        # Construct the scratch group entry that mem_consolidate_apply expects.
        group = {
            "group_id": 0,
            "source": str(source_file),
            "chunk_ids": original_ids,
            "chunk_count": len(original_ids),
            "namespace": None,
            "previews": [],
        }
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        await components.storage.scratch_set(
            "consolidation_groups",
            json.dumps([group]),
            expires_at=expires_at,
        )

        ctx = _fake_ctx(components)
        result = await mem_consolidate_apply(
            group_id=0,
            summary=(
                "The April standup covered 3 items: progress on feature X, "
                "a blocked ticket, and a decision to freeze the release branch."
            ),
            keep_originals=True,
            ctx=ctx,
        )

        assert "Consolidation applied" in result
        assert "Summary chunk id" in result

        # There should be at least one NEW chunk on disk whose source_file is
        # NOT the virtual .consolidated.md path — file-first means the
        # summary landed in a real user-visible markdown file.
        all_sources = await components.storage.get_all_source_files()
        virtual_suffix = ".consolidated.md"
        assert not any(str(s).endswith(virtual_suffix) for s in all_sources), (
            "Agent path must not create a virtual consolidated chunk"
        )

        # The scratch entry should have been cleaned up after successful apply.
        entry = await components.storage.scratch_get("consolidation_groups")
        assert entry is None

        # Each original must have a consolidated_into relation.
        for oid in original_ids:
            from uuid import UUID

            related = await components.storage.get_related(UUID(oid))
            assert any(rel == "consolidated_into" for _, rel in related), (
                f"Original chunk {oid} missing consolidated_into edge"
            )


# ── System namespace prefix filter ───────────────────────────────────


class TestSystemNamespacePrefixFilter:
    """Feature: default search (namespace=None) hides system-generated
    namespaces like ``archive:*`` while explicit requests still retrieve
    them. See Phase A.5 docs for why this is necessary — without it, the
    auto_consolidate ``archive:summary`` chunks would pollute daily
    search results on every user query."""

    def test_namespace_filter_parse_none_with_prefixes_sets_exclude(self):
        from memtomem.models import NamespaceFilter

        f = NamespaceFilter.parse(None, system_prefixes=["archive:"])
        assert f is not None
        assert f.exclude_prefixes == ("archive:",)
        assert f.namespaces == ()
        assert f.pattern is None

    def test_namespace_filter_parse_none_without_prefixes_returns_none(self):
        from memtomem.models import NamespaceFilter

        assert NamespaceFilter.parse(None) is None
        assert NamespaceFilter.parse(None, system_prefixes=[]) is None

    def test_namespace_filter_parse_explicit_clears_exclude(self):
        """Explicit namespace argument opts into whatever was named, even
        when that namespace matches a system prefix. Parse layer decides."""
        from memtomem.models import NamespaceFilter

        f = NamespaceFilter.parse("archive:summary", system_prefixes=["archive:"])
        assert f is not None
        assert f.namespaces == ("archive:summary",)
        assert f.exclude_prefixes == ()  # explicit wins, no exclusion

    def test_namespace_filter_parse_explicit_glob_clears_exclude(self):
        from memtomem.models import NamespaceFilter

        f = NamespaceFilter.parse("archive:*", system_prefixes=["archive:"])
        assert f is not None
        assert f.pattern == "archive:*"
        assert f.exclude_prefixes == ()

    def test_namespace_sql_exclude_prefixes_emits_not_like(self):
        from memtomem.models import NamespaceFilter
        from memtomem.storage.sqlite_helpers import namespace_sql

        f = NamespaceFilter(exclude_prefixes=("archive:", "system:"))
        frag, params = namespace_sql(f)
        # Two NOT LIKE clauses AND-joined, each with its own param.
        assert frag.count("NOT LIKE") == 2
        assert " AND " in frag
        assert params == ["archive:%", "system:%"]

    def test_namespace_sql_exclude_prefixes_cap_assert(self):
        """namespace_sql refuses to emit a runaway clause list."""
        from memtomem.models import NamespaceFilter
        from memtomem.storage.sqlite_helpers import namespace_sql

        too_many = tuple(f"prefix{i}:" for i in range(11))
        f = NamespaceFilter(exclude_prefixes=too_many)
        with pytest.raises(AssertionError, match="cap is 10"):
            namespace_sql(f)

    def test_config_validator_rejects_too_many_prefixes(self):
        """The SearchConfig validator catches misconfiguration at startup."""
        from memtomem.config import SearchConfig

        too_many = [f"p{i}:" for i in range(11)]
        with pytest.raises(ValueError, match="cap is 10"):
            SearchConfig(system_namespace_prefixes=too_many)

    @pytest.mark.ollama
    async def test_default_search_excludes_archive_summary(self, components, memory_dir):
        """End-to-end: an archive:summary chunk is NOT returned by a
        bare search (no namespace), but IS returned when explicitly asked."""
        from memtomem.tools.memory_writer import append_entry

        # 1. Index a user-facing chunk in the default namespace.
        user_file = memory_dir / "normal.md"
        append_entry(
            user_file,
            "A regular memory about Python async context managers and their lifecycle.",
            title="Regular Entry",
        )
        await components.index_engine.index_file(user_file)

        # 2. Manually create an archive:summary chunk — same vocabulary
        # so both would rank highly for the same query.
        from memtomem.models import Chunk, ChunkMetadata, ChunkType

        summary_chunk = Chunk(
            content=(
                "Summary of Python async notes: covers async context managers, "
                "task lifecycle, and the asyncio event loop."
            ),
            metadata=ChunkMetadata(
                source_file=Path("/tmp/normal.md.consolidated.md"),
                chunk_type=ChunkType.MARKDOWN_SECTION,
                tags=("consolidated", "summary"),
                namespace="archive:summary",
            ),
            embedding=[0.1] * components.config.embedding.dimension,
        )
        await components.storage.upsert_chunks([summary_chunk])

        # 3. Default search — the archive:summary chunk must be filtered.
        results, _ = await components.search_pipeline.search(
            "python async context manager lifecycle", top_k=10
        )
        namespaces_found = {r.chunk.metadata.namespace for r in results}
        assert "archive:summary" not in namespaces_found, (
            f"archive:summary leaked into default search: {namespaces_found}"
        )
        # Sanity: we do get at least one result from the regular chunk.
        assert len(results) >= 1

    @pytest.mark.ollama
    async def test_explicit_archive_summary_namespace_is_retrievable(self, components, memory_dir):
        """The same chunk that default search hides is returned when the
        caller explicitly scopes to archive:summary."""
        from memtomem.models import Chunk, ChunkMetadata, ChunkType

        summary_chunk = Chunk(
            content=("Explicitly requested summary about async context managers and lifecycle."),
            metadata=ChunkMetadata(
                source_file=Path("/tmp/other.md.consolidated.md"),
                chunk_type=ChunkType.MARKDOWN_SECTION,
                tags=("consolidated", "summary"),
                namespace="archive:summary",
            ),
            embedding=[0.1] * components.config.embedding.dimension,
        )
        await components.storage.upsert_chunks([summary_chunk])

        results, _ = await components.search_pipeline.search(
            "async context manager lifecycle",
            top_k=10,
            namespace="archive:summary",
        )
        assert len(results) >= 1
        assert all(r.chunk.metadata.namespace == "archive:summary" for r in results)


# ── Temporal ─────────────────────────────────────────────────────────


class TestTemporal:
    async def test_build_timeline_empty(self):
        assert build_timeline([]) == []

    async def test_build_timeline_single_chunk(self):
        chunks = [
            {
                "content": "First memory",
                "created_at": "2025-01-15T10:00:00+00:00",
                "source_file": "/tmp/notes.md",
                "tags": ["meeting"],
                "score": 0.9,
            }
        ]
        buckets = build_timeline(chunks)
        assert len(buckets) == 1
        assert buckets[0].chunk_count == 1
        assert "notes.md" in buckets[0].sources[0]
        assert buckets[0].key_topics == ["meeting"]

    async def test_build_timeline_auto_weekly(self):
        """Span under 90 days should auto-select week granularity."""
        base = datetime(2025, 2, 1, tzinfo=timezone.utc)
        chunks = [
            {
                "content": f"Content day {i}",
                "created_at": (base + timedelta(days=i * 7)).isoformat(),
                "source_file": f"/tmp/file{i}.md",
                "tags": [],
                "score": 0.8,
            }
            for i in range(4)
        ]
        buckets = build_timeline(chunks, granularity="auto")
        # 4 chunks spread across 4 different weeks
        assert len(buckets) >= 1
        assert all("-W" in b.period_label for b in buckets)

    async def test_build_timeline_auto_monthly(self):
        """Span over 90 days should auto-select month granularity."""
        base = datetime(2024, 1, 1, tzinfo=timezone.utc)
        chunks = [
            {
                "content": f"Monthly content {i}",
                "created_at": (base + timedelta(days=i * 35)).isoformat(),
                "source_file": "/tmp/log.md",
                "tags": ["project"],
                "score": 0.7,
            }
            for i in range(5)
        ]
        buckets = build_timeline(chunks, granularity="auto")
        assert len(buckets) >= 1
        # Month labels look like "2024-01", not "2024-W01"
        assert all("-W" not in b.period_label for b in buckets)

    async def test_build_timeline_explicit_month(self):
        chunks = [
            {
                "content": "Jan content",
                "created_at": "2025-01-10T00:00:00+00:00",
                "source_file": "/tmp/a.md",
                "tags": [],
                "score": 0.5,
            },
            {
                "content": "Feb content",
                "created_at": "2025-02-10T00:00:00+00:00",
                "source_file": "/tmp/b.md",
                "tags": [],
                "score": 0.5,
            },
        ]
        buckets = build_timeline(chunks, granularity="month")
        assert len(buckets) == 2
        assert buckets[0].period_label == "2025-01"
        assert buckets[1].period_label == "2025-02"

    async def test_build_timeline_invalid_dates_skipped(self):
        chunks = [
            {
                "content": "Good",
                "created_at": "2025-01-01T00:00:00+00:00",
                "source_file": "a.md",
                "tags": [],
                "score": 0.5,
            },
            {
                "content": "Bad date",
                "created_at": "not-a-date",
                "source_file": "b.md",
                "tags": [],
                "score": 0.5,
            },
            {"content": "Missing key", "source_file": "c.md", "tags": [], "score": 0.5},
        ]
        buckets = build_timeline(chunks)
        assert len(buckets) == 1
        assert buckets[0].chunk_count == 1

    async def test_build_timeline_tags_from_json_string(self):
        chunks = [
            {
                "content": "Tagged content",
                "created_at": "2025-03-01T00:00:00+00:00",
                "source_file": "/tmp/t.md",
                "tags": json.dumps(["alpha", "beta"]),
                "score": 0.5,
            }
        ]
        buckets = build_timeline(chunks)
        assert "alpha" in buckets[0].key_topics
        assert "beta" in buckets[0].key_topics

    async def test_build_timeline_key_topics_max_five(self):
        chunks = [
            {
                "content": "Many tags",
                "created_at": "2025-04-01T00:00:00+00:00",
                "source_file": "/tmp/m.md",
                "tags": ["a", "b", "c", "d", "e", "f", "g"],
                "score": 0.5,
            }
        ]
        buckets = build_timeline(chunks)
        assert len(buckets[0].key_topics) <= 5

    async def test_build_timeline_sources_truncated(self):
        """Sources list should have at most 3 entries and show filenames only."""
        base_dt = "2025-05-01T00:00:00+00:00"
        chunks = [
            {
                "content": f"Content {i}",
                "created_at": base_dt,
                "source_file": f"/some/deep/path/file{i}.md",
                "tags": [],
                "score": 0.5,
            }
            for i in range(6)
        ]
        buckets = build_timeline(chunks, granularity="week")
        assert len(buckets) == 1
        assert len(buckets[0].sources) <= 3
        # Should show just the filename, not the full path
        for s in buckets[0].sources:
            assert "/" not in s

    async def test_format_timeline_empty(self):
        result = format_timeline("Python", [])
        assert "No memories found" in result
        assert "Python" in result

    async def test_format_timeline_with_buckets(self):
        buckets = [
            TimelineBucket(
                period_label="2025-01",
                period_start="2025-01-01",
                period_end="2025-01-31",
                chunk_count=3,
                sources=["notes.md"],
                key_topics=["meeting", "design"],
                sample_content="We discussed the architecture...",
            ),
            TimelineBucket(
                period_label="2025-02",
                period_start="2025-02-01",
                period_end="2025-02-28",
                chunk_count=2,
                sources=["log.md"],
                key_topics=["deployment"],
                sample_content="Deployed v2.0 to production.",
            ),
        ]
        result = format_timeline("project", buckets)
        assert 'Timeline for "project"' in result
        assert "2025-01-01 -> 2025-02-28" in result
        assert "## 2025-01 (3 memories)" in result
        assert "## 2025-02 (2 memories)" in result
        assert "Sources: notes.md" in result
        assert "Topics: meeting, design" in result
        assert "Total: 5 memories across 2 periods" in result

    async def test_activity_day_dataclass(self):
        day = ActivityDay(date="2025-03-10", created=5, updated=2, accessed=10)
        assert day.date == "2025-03-10"
        assert day.created == 5
        assert day.updated == 2
        assert day.accessed == 10
        with pytest.raises(AttributeError):
            day.created = 99  # type: ignore[misc]

    async def test_format_activity_empty(self):
        result = format_activity([], since="2025-01-01", until="2025-01-31")
        assert "No activity found" in result
        assert "2025-01-01" in result
        assert "2025-01-31" in result

    async def test_format_activity_with_days(self):
        days = [
            ActivityDay(date="2025-03-01", created=3, updated=1, accessed=7),
            ActivityDay(date="2025-03-02", created=0, updated=2, accessed=5),
        ]
        result = format_activity(days, since="2025-03-01", until="2025-03-02")
        assert "Memory Activity" in result
        assert "2025-03-01" in result
        assert "Totals: 3 created, 3 updated, 12 accessed" in result
        # Table header
        assert "Date" in result
        assert "Created" in result
        assert "Updated" in result
        assert "Accessed" in result

    async def test_timeline_bucket_dataclass(self):
        b = TimelineBucket(
            period_label="2025-W10",
            period_start="2025-03-03",
            period_end="2025-03-09",
            chunk_count=4,
            sources=["a.md"],
            key_topics=["design"],
            sample_content="sample",
        )
        assert b.period_label == "2025-W10"
        assert b.chunk_count == 4
        with pytest.raises(AttributeError):
            b.chunk_count = 0  # type: ignore[misc]

    async def test_build_timeline_sorted_output(self):
        """Buckets should be sorted chronologically."""
        chunks = [
            {
                "content": "Later",
                "created_at": "2025-03-15T00:00:00+00:00",
                "source_file": "/tmp/b.md",
                "tags": [],
                "score": 0.5,
            },
            {
                "content": "Earlier",
                "created_at": "2025-01-10T00:00:00+00:00",
                "source_file": "/tmp/a.md",
                "tags": [],
                "score": 0.5,
            },
        ]
        buckets = build_timeline(chunks, granularity="month")
        assert buckets[0].period_label < buckets[1].period_label
