"""Comprehensive tests for memtomem server maintenance and advanced tool functions.

Covers: cross-references, policies, entities, importance scoring, analytics,
history, dedup, export/import, cleanup orphans, and auto-tag.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from helpers import make_chunk
from memtomem.models import Chunk, ChunkMetadata
from memtomem.tools.auto_tag import AutoTagStats, auto_tag_storage, extract_tags_keyword
from memtomem.tools.export_import import ExportBundle, export_chunks


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chunk(content="test content", tags=(), namespace="default", source="test.md", heading=()):
    """Shorthand wrapper around make_chunk with 1024-dim embedding."""
    return make_chunk(
        content=content,
        tags=tags,
        namespace=namespace,
        source=source,
        heading=heading,
        embedding=[0.1] * 1024,
    )


# ===================================================================
# Cross-references (RelationMixin)
# ===================================================================


class TestCrossRef:
    async def test_add_and_get_relation(self, storage):
        a = _chunk("chunk A")
        b = _chunk("chunk B")
        await storage.upsert_chunks([a, b])

        await storage.add_relation(a.id, b.id, "related")
        related = await storage.get_related(a.id)
        assert len(related) == 1
        assert related[0][0] == b.id
        assert related[0][1] == "related"

    async def test_relation_bidirectional(self, storage):
        """get_related should return links from both directions."""
        a = _chunk("chunk A")
        b = _chunk("chunk B")
        await storage.upsert_chunks([a, b])

        await storage.add_relation(a.id, b.id, "depends_on")

        # Query from target side
        from_b = await storage.get_related(b.id)
        assert len(from_b) == 1
        assert from_b[0][0] == a.id
        assert from_b[0][1] == "depends_on"

    async def test_multiple_relations(self, storage):
        a = _chunk("A")
        b = _chunk("B")
        c = _chunk("C")
        await storage.upsert_chunks([a, b, c])

        await storage.add_relation(a.id, b.id, "related")
        await storage.add_relation(a.id, c.id, "supersedes")

        related = await storage.get_related(a.id)
        assert len(related) == 2
        relation_types = {r[1] for r in related}
        assert "related" in relation_types
        assert "supersedes" in relation_types

    async def test_delete_relation(self, storage):
        a = _chunk("A")
        b = _chunk("B")
        await storage.upsert_chunks([a, b])

        await storage.add_relation(a.id, b.id, "related")
        deleted = await storage.delete_relation(a.id, b.id)
        assert deleted is True

        related = await storage.get_related(a.id)
        assert related == []

    async def test_delete_nonexistent_relation(self, storage):
        a_id = uuid4()
        b_id = uuid4()
        deleted = await storage.delete_relation(a_id, b_id)
        assert deleted is False

    async def test_get_related_empty(self, storage):
        chunk = _chunk("solo chunk")
        await storage.upsert_chunks([chunk])
        related = await storage.get_related(chunk.id)
        assert related == []

    async def test_relation_replace_on_duplicate(self, storage):
        """Adding the same relation twice should not create duplicates (INSERT OR REPLACE)."""
        a = _chunk("A")
        b = _chunk("B")
        await storage.upsert_chunks([a, b])

        await storage.add_relation(a.id, b.id, "related")
        await storage.add_relation(a.id, b.id, "related")

        related = await storage.get_related(a.id)
        assert len(related) == 1


# ===================================================================
# Policies (PolicyMixin)
# ===================================================================


class TestPolicies:
    async def test_add_and_list(self, storage):
        pid = await storage.policy_add("cleanup", "auto_expire", {"max_age_days": 90})
        assert isinstance(pid, str)
        assert len(pid) > 0

        policies = await storage.policy_list()
        assert len(policies) == 1
        assert policies[0]["name"] == "cleanup"
        assert policies[0]["policy_type"] == "auto_expire"
        assert policies[0]["config"]["max_age_days"] == 90
        assert policies[0]["enabled"] is True

    async def test_add_with_namespace_filter(self, storage):
        await storage.policy_add(
            "archive-old", "auto_archive", {"threshold": 0.5}, namespace_filter="archive"
        )
        policy = await storage.policy_get("archive-old")
        assert policy is not None
        assert policy["namespace_filter"] == "archive"
        assert policy["policy_type"] == "auto_archive"

    async def test_get_specific_policy(self, storage):
        await storage.policy_add("tagging", "auto_tag", {"max_tags": 5})
        policy = await storage.policy_get("tagging")
        assert policy is not None
        assert policy["name"] == "tagging"
        assert policy["config"]["max_tags"] == 5

    async def test_get_nonexistent_policy(self, storage):
        result = await storage.policy_get("no-such-policy")
        assert result is None

    async def test_delete_policy(self, storage):
        await storage.policy_add("temp", "auto_archive", {})
        deleted = await storage.policy_delete("temp")
        assert deleted is True

        policies = await storage.policy_list()
        assert len(policies) == 0

    async def test_delete_nonexistent_policy(self, storage):
        deleted = await storage.policy_delete("ghost")
        assert deleted is False

    async def test_multiple_policies(self, storage):
        await storage.policy_add("p1", "auto_expire", {"days": 30})
        await storage.policy_add("p2", "auto_tag", {"max_tags": 3})
        await storage.policy_add("p3", "auto_archive", {})

        policies = await storage.policy_list()
        assert len(policies) == 3
        names = [p["name"] for p in policies]
        assert "p1" in names
        assert "p2" in names
        assert "p3" in names

    async def test_policy_update_last_run(self, storage):
        await storage.policy_add("runner", "auto_expire", {})
        await storage.policy_update_last_run("runner")

        policy = await storage.policy_get("runner")
        assert policy["last_run_at"] is not None

    async def test_get_enabled_policies(self, storage):
        await storage.policy_add("active1", "auto_tag", {})
        await storage.policy_add("active2", "auto_expire", {})

        enabled = await storage.policy_get_enabled()
        assert len(enabled) == 2
        assert all(p["name"] in ("active1", "active2") for p in enabled)


# ===================================================================
# Entities (EntityMixin)
# ===================================================================


class TestEntities:
    async def test_upsert_and_search(self, storage):
        chunk = _chunk("Alice met Bob at Google headquarters in 2024")
        await storage.upsert_chunks([chunk])

        entities = [
            {"entity_type": "person", "entity_value": "Alice", "confidence": 0.95, "position": 0},
            {"entity_type": "person", "entity_value": "Bob", "confidence": 0.9, "position": 1},
            {"entity_type": "org", "entity_value": "Google", "confidence": 0.85, "position": 2},
        ]
        count = await storage.upsert_entities(str(chunk.id), entities)
        assert count == 3

        # Search by type
        results = await storage.search_entities(entity_type="person")
        assert len(results) == 2
        values = {r["entity_value"] for r in results}
        assert "Alice" in values
        assert "Bob" in values

    async def test_search_by_value_substring(self, storage):
        chunk = _chunk("Meeting with Alice Smith")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "person", "entity_value": "Alice Smith", "confidence": 0.9},
            ],
        )

        results = await storage.search_entities(value="Alice")
        assert len(results) == 1
        assert results[0]["entity_value"] == "Alice Smith"

    async def test_search_by_namespace(self, storage):
        c1 = _chunk("Entity in ns1", namespace="ns1")
        c2 = _chunk("Entity in ns2", namespace="ns2")
        await storage.upsert_chunks([c1, c2])

        await storage.upsert_entities(
            str(c1.id),
            [
                {"entity_type": "concept", "entity_value": "concept-1"},
            ],
        )
        await storage.upsert_entities(
            str(c2.id),
            [
                {"entity_type": "concept", "entity_value": "concept-2"},
            ],
        )

        results = await storage.search_entities(namespace="ns1")
        assert len(results) == 1
        assert results[0]["entity_value"] == "concept-1"

    async def test_upsert_overwrites_existing(self, storage):
        chunk = _chunk("overwrite test")
        await storage.upsert_chunks([chunk])

        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "person", "entity_value": "Old"},
            ],
        )
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "person", "entity_value": "New"},
            ],
        )

        result = await storage.get_entities_for_chunk(str(chunk.id))
        assert len(result) == 1
        assert result[0]["entity_value"] == "New"

    async def test_upsert_empty_list(self, storage):
        count = await storage.upsert_entities("nonexistent-id", [])
        assert count == 0

    async def test_delete_entities_for_chunk(self, storage):
        chunk = _chunk("deletable")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "person", "entity_value": "Gone"},
            ],
        )
        deleted = await storage.delete_entities_for_chunk(str(chunk.id))
        assert deleted == 1

        remaining = await storage.get_entities_for_chunk(str(chunk.id))
        assert remaining == []

    async def test_entity_type_counts(self, storage):
        chunk = _chunk("mixed entities")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "person", "entity_value": "A"},
                {"entity_type": "person", "entity_value": "B"},
                {"entity_type": "org", "entity_value": "C"},
            ],
        )

        counts = await storage.get_entity_type_counts()
        assert counts["person"] == 2
        assert counts["org"] == 1


# ===================================================================
# Importance scoring (AnalyticsMixin)
# ===================================================================


class TestImportance:
    async def test_get_chunk_factors_empty(self, storage):
        factors = await storage.get_chunk_factors()
        assert factors == []

    async def test_get_chunk_factors_with_data(self, storage):
        c1 = _chunk("factor test", tags=("python", "dev"))
        c2 = _chunk("second chunk")
        await storage.upsert_chunks([c1, c2])
        await storage.increment_access([c1.id])

        factors = await storage.get_chunk_factors()
        assert len(factors) == 2

        factor_by_id = {f["id"]: f for f in factors}
        c1_factor = factor_by_id[str(c1.id)]
        assert c1_factor["access_count"] == 1
        assert c1_factor["tag_count"] == 2

    async def test_get_chunk_factors_with_relations(self, storage):
        a = _chunk("A")
        b = _chunk("B")
        await storage.upsert_chunks([a, b])
        await storage.add_relation(a.id, b.id, "related")

        factors = await storage.get_chunk_factors()
        factor_by_id = {f["id"]: f for f in factors}
        assert factor_by_id[str(a.id)]["relation_count"] == 1
        assert factor_by_id[str(b.id)]["relation_count"] == 1

    async def test_update_and_get_importance_scores(self, storage):
        chunk = _chunk("importance test")
        await storage.upsert_chunks([chunk])

        scores = {str(chunk.id): 0.85}
        updated = await storage.update_importance_scores(scores)
        assert updated == 1

        result = await storage.get_importance_scores([chunk.id])
        assert result[str(chunk.id)] == pytest.approx(0.85)

    async def test_update_importance_empty(self, storage):
        result = await storage.update_importance_scores({})
        assert result == 0

    async def test_get_importance_empty(self, storage):
        result = await storage.get_importance_scores([])
        assert result == {}

    async def test_get_chunk_factors_namespace_filter(self, storage):
        c1 = _chunk("in ns", namespace="work")
        c2 = _chunk("other ns", namespace="personal")
        await storage.upsert_chunks([c1, c2])

        factors = await storage.get_chunk_factors(namespace="work")
        assert len(factors) == 1
        assert factors[0]["id"] == str(c1.id)


# ===================================================================
# Analytics (AnalyticsMixin)
# ===================================================================


class TestAnalytics:
    async def test_health_report_empty_db(self, storage):
        report = await storage.get_health_report()
        assert report["total_chunks"] == 0
        assert report["access_coverage"]["pct"] == 0
        assert report["tag_coverage"]["pct"] == 0
        assert report["dead_memories_pct"] == 0

    async def test_health_report_with_chunks(self, storage):
        c1 = _chunk("accessed chunk", tags=("test",))
        c2 = _chunk("unaccessed chunk")
        await storage.upsert_chunks([c1, c2])
        await storage.increment_access([c1.id])

        report = await storage.get_health_report()
        assert report["total_chunks"] == 2
        assert report["access_coverage"]["accessed"] == 1
        assert report["access_coverage"]["pct"] == 50.0
        assert report["tag_coverage"]["tagged"] == 1
        assert report["tag_coverage"]["pct"] == 50.0

    async def test_health_report_namespace_distribution(self, storage):
        c1 = _chunk("ns1 chunk", namespace="work")
        c2 = _chunk("ns2 chunk", namespace="personal")
        c3 = _chunk("ns1 again", namespace="work")
        await storage.upsert_chunks([c1, c2, c3])

        report = await storage.get_health_report()
        ns_dist = {d["namespace"]: d["count"] for d in report["namespace_distribution"]}
        assert ns_dist["work"] == 2
        assert ns_dist["personal"] == 1

    async def test_activity_summary_empty(self, storage):
        summary = await storage.get_activity_summary()
        assert summary == []

    async def test_activity_summary_with_data(self, storage):
        chunk = _chunk("activity test")
        await storage.upsert_chunks([chunk])

        summary = await storage.get_activity_summary()
        assert len(summary) >= 1
        assert summary[0]["created"] >= 1

    async def test_frequently_accessed(self, storage):
        chunk = _chunk("popular content")
        await storage.upsert_chunks([chunk])
        await storage.increment_access([chunk.id])
        await storage.increment_access([chunk.id])

        result = await storage.get_frequently_accessed()
        assert len(result) >= 1
        assert result[0]["total_access"] >= 2

    async def test_knowledge_gaps(self, storage):
        # Save a query with no results
        await storage.save_query_history("unanswered topic", [], [], [])
        await storage.save_query_history("unanswered topic", [], [], [])

        gaps = await storage.get_knowledge_gaps()
        assert len(gaps) >= 1
        assert gaps[0]["query"] == "unanswered topic"
        assert gaps[0]["count"] == 2

    async def test_most_connected_empty(self, storage):
        result = await storage.get_most_connected()
        assert result == []

    async def test_most_connected_with_relations(self, storage):
        a = _chunk("hub")
        b = _chunk("spoke 1")
        c = _chunk("spoke 2")
        await storage.upsert_chunks([a, b, c])
        await storage.add_relation(a.id, b.id, "related")
        await storage.add_relation(a.id, c.id, "related")

        result = await storage.get_most_connected(limit=5)
        assert len(result) >= 1
        # The hub chunk should appear with the most links
        hub = result[0]
        assert hub["chunk_id"] == str(a.id)

    async def test_consolidation_groups(self, storage):
        # Create 3+ chunks from same source to form a group
        chunks = [_chunk(f"content {i}", source="big_file.md") for i in range(4)]
        await storage.upsert_chunks(chunks)

        groups = await storage.get_consolidation_groups(min_size=3)
        assert len(groups) >= 1
        assert groups[0]["chunk_count"] >= 3


# ===================================================================
# History (HistoryMixin)
# ===================================================================


class TestHistory:
    async def test_save_and_get(self, storage):
        await storage.save_query_history(
            "search for python", [0.1] * 10, ["id-1", "id-2"], [0.9, 0.8]
        )
        history = await storage.get_query_history(limit=10)
        assert len(history) == 1
        assert history[0]["query_text"] == "search for python"
        assert history[0]["result_chunk_ids"] == ["id-1", "id-2"]
        assert history[0]["result_scores"] == [0.9, 0.8]

    async def test_empty_history(self, storage):
        history = await storage.get_query_history()
        assert history == []

    async def test_limit_respected(self, storage):
        for i in range(5):
            await storage.save_query_history(f"query {i}", [], [], [])

        history = await storage.get_query_history(limit=3)
        assert len(history) == 3

    async def test_ordering_most_recent_first(self, storage):
        await storage.save_query_history("first", [], [], [])
        await storage.save_query_history("second", [], [], [])
        await storage.save_query_history("third", [], [], [])

        history = await storage.get_query_history(limit=10)
        assert history[0]["query_text"] == "third"
        assert history[2]["query_text"] == "first"

    async def test_suggest_queries_prefix_match(self, storage):
        await storage.save_query_history("deployment strategy", [], [], [])
        await storage.save_query_history("deployment pipeline", [], [], [])
        await storage.save_query_history("testing framework", [], [], [])

        suggestions = await storage.suggest_queries("deploy")
        assert len(suggestions) == 2
        assert all("deploy" in s for s in suggestions)

    async def test_suggest_queries_no_match(self, storage):
        await storage.save_query_history("hello world", [], [], [])
        suggestions = await storage.suggest_queries("xyz")
        assert suggestions == []

    async def test_suggest_queries_limit(self, storage):
        for i in range(10):
            await storage.save_query_history(f"alpha-{i}", [], [], [])

        suggestions = await storage.suggest_queries("alpha", limit=3)
        assert len(suggestions) == 3


# ===================================================================
# Export / Import
# ===================================================================


class TestExportImport:
    async def test_export_empty(self, storage):
        bundle = await export_chunks(storage)
        assert bundle.total_chunks == 0
        assert bundle.chunks == []

    async def test_export_with_chunks(self, storage):
        chunks = [
            _chunk("export content A", tags=("python",), source="notes.md"),
            _chunk("export content B", tags=("rust",), source="notes.md"),
        ]
        await storage.upsert_chunks(chunks)

        bundle = await export_chunks(storage)
        assert bundle.total_chunks == 2
        assert len(bundle.chunks) == 2
        contents = {c["content"] for c in bundle.chunks}
        assert "export content A" in contents
        assert "export content B" in contents

    async def test_export_to_file(self, storage, tmp_path):
        chunk = _chunk("file export test", source="file_test.md")
        await storage.upsert_chunks([chunk])

        output_path = tmp_path / "export.json"
        await export_chunks(storage, output_path=output_path)

        assert output_path.exists()
        data = json.loads(output_path.read_text())
        assert data["total_chunks"] == 1
        assert data["chunks"][0]["content"] == "file export test"

    async def test_export_source_filter(self, storage):
        c1 = _chunk("match", source="important.md")
        c2 = _chunk("skip", source="other.md")
        await storage.upsert_chunks([c1, c2])

        bundle = await export_chunks(storage, source_filter="important")
        assert bundle.total_chunks == 1
        assert bundle.chunks[0]["content"] == "match"

    async def test_export_tag_filter(self, storage):
        c1 = _chunk("tagged", tags=("python",), source="mix.md")
        c2 = _chunk("untagged", source="mix.md")
        await storage.upsert_chunks([c1, c2])

        bundle = await export_chunks(storage, tag_filter="python")
        assert bundle.total_chunks == 1
        assert bundle.chunks[0]["content"] == "tagged"

    async def test_export_namespace_filter(self, storage):
        c1 = _chunk("work stuff", namespace="work", source="w.md")
        c2 = _chunk("personal stuff", namespace="personal", source="p.md")
        await storage.upsert_chunks([c1, c2])

        bundle = await export_chunks(storage, namespace_filter="work")
        assert bundle.total_chunks == 1
        assert bundle.chunks[0]["content"] == "work stuff"

    async def test_bundle_roundtrip_serialization(self):
        bundle = ExportBundle(
            exported_at="2024-01-01T00:00:00+00:00",
            total_chunks=1,
            chunks=[{"content": "test", "source_file": "/tmp/t.md", "tags": []}],
        )
        json_str = bundle.to_json()
        restored = ExportBundle.from_json(json_str)
        assert restored.total_chunks == 1
        assert restored.chunks[0]["content"] == "test"
        assert restored.version == "1"

    async def test_export_preserves_metadata(self, storage):
        chunk = _chunk(
            "metadata test",
            tags=("tag1", "tag2"),
            namespace="myns",
            source="src.md",
            heading=("Section 1", "Subsection A"),
        )
        await storage.upsert_chunks([chunk])

        bundle = await export_chunks(storage)
        assert bundle.total_chunks == 1
        record = bundle.chunks[0]
        assert record["tags"] == ["tag1", "tag2"]
        assert record["namespace"] == "myns"
        assert record["heading_hierarchy"] == ["Section 1", "Subsection A"]


# ===================================================================
# Cleanup orphans
# ===================================================================


class TestCleanupOrphans:
    async def test_identify_orphaned_sources(self, storage, memory_dir):
        """Chunks whose source_file does not exist on disk are orphans."""
        # Create a real file and a chunk pointing to it
        real_file = memory_dir / "real.md"
        real_file.write_text("# Real content")

        real_chunk = make_chunk(
            content="real content",
            source=str(real_file),
            embedding=[0.1] * 1024,
        )
        # Override source_file to use absolute path
        real_chunk = Chunk(
            content=real_chunk.content,
            metadata=ChunkMetadata(
                source_file=real_file,
                tags=real_chunk.metadata.tags,
                namespace=real_chunk.metadata.namespace,
            ),
            content_hash=real_chunk.content_hash,
            embedding=real_chunk.embedding,
        )

        # Orphaned chunk pointing to non-existent file
        orphan = Chunk(
            content="orphan content",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/nonexistent_file_abc123.md"),
                tags=(),
                namespace="default",
            ),
            content_hash=f"hash-{uuid4().hex[:8]}",
            embedding=[0.1] * 1024,
        )

        await storage.upsert_chunks([real_chunk, orphan])

        source_files = await storage.get_all_source_files()
        assert len(source_files) == 2

        # Identify orphans: source files that don't exist on disk
        orphans = [sf for sf in source_files if not sf.exists()]
        assert len(orphans) >= 1

    async def test_get_all_source_files(self, storage):
        c1 = _chunk("from src1", source="src1.md")
        c2 = _chunk("from src2", source="src2.md")
        c3 = _chunk("also src1", source="src1.md")
        await storage.upsert_chunks([c1, c2, c3])

        sources = await storage.get_all_source_files()
        # source paths are stored normalized — just check count
        assert len(sources) == 2

    async def test_delete_by_source(self, storage):
        """Orphan cleanup typically uses delete_by_source."""
        c1 = _chunk("orphan 1", source="gone.md")
        c2 = _chunk("orphan 2", source="gone.md")
        c3 = _chunk("keeper", source="keep.md")
        await storage.upsert_chunks([c1, c2, c3])

        deleted = await storage.delete_by_source(Path("/tmp/gone.md"))
        assert deleted == 2

        sources = await storage.get_all_source_files()
        remaining_paths = {str(s) for s in sources}
        assert any("keep" in p for p in remaining_paths)

    async def test_empty_source_files(self, storage):
        sources = await storage.get_all_source_files()
        assert sources == set()


# ===================================================================
# Auto-tag (keyword extraction)
# ===================================================================


class TestAutoTag:
    def test_extract_tags_basic(self):
        tags = extract_tags_keyword(
            "Python is a programming language used for machine learning and data science",
            max_tags=3,
        )
        assert len(tags) <= 3
        assert len(tags) > 0
        # Should contain meaningful words, not stop words
        for tag in tags:
            assert tag not in ("the", "and", "for", "are", "used", "using")

    def test_extract_tags_empty_text(self):
        tags = extract_tags_keyword("")
        assert tags == []

    def test_extract_tags_whitespace_only(self):
        tags = extract_tags_keyword("   \n\t  ")
        assert tags == []

    def test_extract_tags_heading_boost(self):
        """Heading words should be boosted (3x) and rank higher."""
        tags = extract_tags_keyword(
            "This document discusses various topics including deployment",
            heading_hierarchy=("deployment",),
            max_tags=3,
        )
        # "deployment" appears in heading (3x boost) + body (1x) = 4 mentions
        assert "deployment" in tags

    def test_extract_tags_max_limit(self):
        text = "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        tags = extract_tags_keyword(text, max_tags=2)
        assert len(tags) <= 2

    async def test_auto_tag_storage_basic(self, storage):
        """auto_tag_storage should tag untagged chunks."""
        chunk = _chunk(
            "Python programming language for data science applications", source="code.md"
        )
        await storage.upsert_chunks([chunk])

        stats = await auto_tag_storage(storage, max_tags=3)
        assert isinstance(stats, AutoTagStats)
        assert stats.total_chunks == 1
        assert stats.tagged_chunks == 1
        assert stats.skipped_chunks == 0

        # Verify tags were persisted
        updated = await storage.list_chunks_by_source(Path("/tmp/code.md"))
        assert len(updated) == 1
        assert len(updated[0].metadata.tags) > 0

    async def test_auto_tag_storage_skip_already_tagged(self, storage):
        """Chunks with existing tags should be skipped when overwrite=False."""
        chunk = _chunk("some content", tags=("existing-tag",), source="tagged.md")
        await storage.upsert_chunks([chunk])

        stats = await auto_tag_storage(storage, overwrite=False)
        assert stats.skipped_chunks == 1
        assert stats.tagged_chunks == 0

    async def test_auto_tag_storage_overwrite(self, storage):
        """When overwrite=True, even tagged chunks get re-tagged."""
        chunk = _chunk(
            "Python machine learning framework development",
            tags=("old-tag",),
            source="overwrite.md",
        )
        await storage.upsert_chunks([chunk])

        stats = await auto_tag_storage(storage, overwrite=True)
        assert stats.tagged_chunks == 1

    async def test_auto_tag_storage_dry_run(self, storage):
        """dry_run=True should not write to storage."""
        chunk = _chunk("dry run content for tagging test", source="dry.md")
        await storage.upsert_chunks([chunk])

        stats = await auto_tag_storage(storage, dry_run=True)
        assert stats.tagged_chunks >= 1

        # Verify no tags were actually written
        chunks = await storage.list_chunks_by_source(Path("/tmp/dry.md"))
        assert chunks[0].metadata.tags == ()

    async def test_auto_tag_storage_source_filter(self, storage):
        c1 = _chunk("Python code example function", source="python.md")
        c2 = _chunk("Rust memory safety ownership model", source="rust.md")
        await storage.upsert_chunks([c1, c2])

        stats = await auto_tag_storage(storage, source_filter="python")
        assert stats.total_chunks == 1
        assert stats.tagged_chunks == 1

    async def test_auto_tag_storage_namespace_filter(self, storage):
        c1 = _chunk("Python code example function", namespace="proj_a", source="a.md")
        c2 = _chunk("Rust memory safety ownership model", namespace="proj_b", source="b.md")
        await storage.upsert_chunks([c1, c2])

        stats = await auto_tag_storage(storage, namespace_filter="proj_a")
        assert stats.total_chunks == 1
        assert stats.tagged_chunks == 1

        # Verify only the proj_a chunk was tagged
        a_chunks = await storage.list_chunks_by_source(Path("/tmp/a.md"))
        b_chunks = await storage.list_chunks_by_source(Path("/tmp/b.md"))
        assert a_chunks[0].metadata.tags  # tagged
        assert b_chunks[0].metadata.tags == ()  # untouched


# ===================================================================
# Dedup (DedupScanner) - basic exact duplicate detection
# ===================================================================


class TestDedup:
    async def test_exact_duplicate_detection(self, storage, components):
        """Two chunks with the same content_hash are exact duplicates."""
        from memtomem.search.dedup import DedupScanner

        # Create two chunks with the same content hash
        c1 = Chunk(
            content="duplicate content",
            metadata=ChunkMetadata(source_file=Path("/tmp/a.md"), namespace="default"),
            content_hash="SAME_HASH",
            embedding=[0.1] * 1024,
        )
        c2 = Chunk(
            content="duplicate content",
            metadata=ChunkMetadata(source_file=Path("/tmp/b.md"), namespace="default"),
            content_hash="SAME_HASH",
            embedding=[0.1] * 1024,
        )
        await storage.upsert_chunks([c1, c2])

        scanner = DedupScanner(storage=storage, embedder=components.embedder)
        # Only test the _find_exact_duplicates internal method to avoid
        # needing a running embedder for dense_search
        all_chunks = await scanner._get_all_chunks(max_count=100)
        seen: set[frozenset] = set()
        exact = scanner._find_exact_duplicates(all_chunks, seen)

        assert len(exact) == 1
        assert exact[0].exact is True
        assert exact[0].score == 1.0

    async def test_no_duplicates(self, storage, components):
        """Distinct hashes should not appear as exact duplicates."""
        from memtomem.search.dedup import DedupScanner

        c1 = _chunk("unique content A", source="a.md")
        c2 = _chunk("unique content B", source="b.md")
        await storage.upsert_chunks([c1, c2])

        scanner = DedupScanner(storage=storage, embedder=components.embedder)
        all_chunks = await scanner._get_all_chunks(max_count=100)
        seen: set[frozenset] = set()
        exact = scanner._find_exact_duplicates(all_chunks, seen)
        assert len(exact) == 0
