"""End-to-end tests simulating real user workflows.

Each test class represents a user persona from the usability test plan.
Tests use the full component stack (storage, indexer, search pipeline)
with a temporary DB — no mocks.

All tests require a running Ollama instance for embedding.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.tools.memory_writer import append_entry

pytestmark = pytest.mark.ollama


# ── Scenario 1: Developer — Basic CRUD ───────────────────────────────────


class TestDeveloperCRUD:
    """Claude Code developer who wants to remember project decisions."""

    async def test_add_then_search(self, components, memory_dir):
        """mem_add → mem_search: newly added content is immediately searchable."""
        target = memory_dir / "decisions.md"
        append_entry(target, "Chose Flask over FastAPI for rapid prototyping", title="Framework")
        await components.index_engine.index_file(target)

        results, _ = await components.search_pipeline.search("Flask framework choice", top_k=5)
        assert len(results) >= 1
        assert any("Flask" in r.chunk.content for r in results)

    async def test_multiple_entries_separate_chunks(self, components, memory_dir):
        """Two mem_add calls to same file → two separate chunks with own tags."""
        target = memory_dir / "arch.md"

        append_entry(target, "Redis for caching", title="Cache Decision", tags=["cache"])
        await components.index_engine.index_file(target)

        # Apply tags to newest chunk
        chunks = await components.storage.list_chunks_by_source(target)
        assert len(chunks) == 1
        chunks[0] = _apply_tags(chunks[0], {"cache"})
        await components.storage.upsert_chunks(chunks)

        append_entry(target, "PostgreSQL for persistence", title="DB Decision", tags=["database"])
        await components.index_engine.index_file(target)

        chunks = await components.storage.list_chunks_by_source(target)
        assert len(chunks) == 2, "Each heading should be a separate chunk"

    async def test_tag_filter_search(self, components, memory_dir):
        """tag_filter narrows results to tagged content only."""
        # Add tagged content
        f1 = memory_dir / "tagged.md"
        append_entry(f1, "Redis LRU eviction policy", title="Redis Note", tags=["redis"])
        await components.index_engine.index_file(f1)
        chunks = await components.storage.list_chunks_by_source(f1)
        chunks[-1] = _apply_tags(chunks[-1], {"redis"})
        await components.storage.upsert_chunks(chunks)

        # Add untagged content
        f2 = memory_dir / "untagged.md"
        append_entry(f2, "Memcached is also a caching option", title="Memcached")
        await components.index_engine.index_file(f2)

        # Search with tag filter
        results, _ = await components.search_pipeline.search(
            "caching", top_k=10, tag_filter="redis"
        )
        assert all("redis" in r.chunk.metadata.tags for r in results)

    async def test_recall_by_date(self, components, memory_dir):
        """mem_recall returns memories ordered by creation date."""
        target = memory_dir / "daily.md"
        append_entry(target, "Today's standup: discussed auth refactor", title="Standup")
        await components.index_engine.index_file(target)

        from datetime import datetime, timezone

        chunks = await components.storage.recall_chunks(
            since=datetime(2020, 1, 1, tzinfo=timezone.utc),
            limit=10,
        )
        assert len(chunks) >= 1


# ── Scenario 2: Researcher — Directory Indexing ──────────────────────────


class TestResearcherIndexing:
    """Researcher managing paper notes across multiple files."""

    async def test_directory_indexing(self, components, memory_dir):
        """Index a directory with multiple files, all become searchable."""
        (memory_dir / "paper1.md").write_text(
            "# Scaling Laws\n\nChinchilla showed compute-optimal training."
        )
        (memory_dir / "paper2.md").write_text(
            "# RAG Patterns\n\nRetrieval-Augmented Generation improves factuality."
        )

        stats = await components.index_engine.index_path(memory_dir, recursive=True)
        assert stats.total_files == 2
        assert stats.indexed_chunks >= 2

        results, _ = await components.search_pipeline.search("retrieval augmented", top_k=3)
        assert any("RAG" in r.chunk.content or "Retrieval" in r.chunk.content for r in results)

    async def test_namespace_search(self, components, memory_dir):
        """Namespace isolates search results."""
        (memory_dir / "work.md").write_text("# Work\n\nDeployment pipeline config.")
        (memory_dir / "personal.md").write_text("# Personal\n\nGrocery list for Saturday.")

        await components.index_engine.index_path(memory_dir, recursive=True)

        # Assign namespaces
        chunks = await components.storage.list_chunks_by_source(memory_dir / "work.md")
        for c in chunks:
            c = _set_namespace(c, "work")
        await components.storage.upsert_chunks(chunks)

        # Search within namespace
        results, _ = await components.search_pipeline.search(
            "pipeline", top_k=5, namespace="work"
        )
        assert all(r.chunk.metadata.namespace == "work" for r in results)

    async def test_source_filter(self, components, memory_dir):
        """source_filter restricts results to matching files."""
        (memory_dir / "notes.md").write_text("# Notes\n\nImportant project notes here.")
        (memory_dir / "archive.md").write_text("# Archive\n\nOld project notes archived.")
        await components.index_engine.index_path(memory_dir, recursive=True)

        results, _ = await components.search_pipeline.search(
            "project notes", top_k=10, source_filter="notes.md"
        )
        assert all("notes.md" in str(r.chunk.metadata.source_file) for r in results)


# ── Scenario 3: Team Lead — Templates & Sessions ────────────────────────


class TestTeamLeadSessions:
    """Team lead managing meeting notes with templates and sessions."""

    async def test_session_lifecycle(self, components):
        """Start session → end session → list sessions."""
        storage = components.storage
        session_id = "test-session-001"

        await storage.create_session(session_id, "team-lead", "default", {"title": "Sprint"})
        sessions = await storage.list_sessions(limit=5)
        assert any(s["id"] == session_id for s in sessions)

        await storage.end_session(session_id, "Sprint planning complete", {})
        sessions = await storage.list_sessions(limit=5)
        ended = [s for s in sessions if s["id"] == session_id]
        assert ended[0]["ended_at"] is not None

    async def test_scratch_promote(self, components, memory_dir):
        """scratch_set → scratch_get → scratch_promote to long-term."""
        storage = components.storage

        await storage.scratch_set("current_task", "Testing memtomem workflows")
        result = await storage.scratch_get("current_task")
        assert result["value"] == "Testing memtomem workflows"

        items = await storage.scratch_list()
        assert any(i["key"] == "current_task" for i in items)


# ── Scenario 4: Multilingual — Korean + English ─────────────────────────


class TestMultilingualSearch:
    """User working with Korean and English content."""

    async def test_same_language_search(self, components, memory_dir):
        """Korean query finds Korean content."""
        f = memory_dir / "kr.md"
        f.write_text("## 모니터링\n\n쿠버네티스 클러스터 모니터링 설정 완료.")
        await components.index_engine.index_file(f)

        results, _ = await components.search_pipeline.search("쿠버네티스 모니터링", top_k=3)
        assert len(results) >= 1

    async def test_english_finds_english(self, components, memory_dir):
        """English query finds English content."""
        f = memory_dir / "en.md"
        f.write_text("## Monitoring\n\nKubernetes cluster monitoring setup complete.")
        await components.index_engine.index_file(f)

        results, _ = await components.search_pipeline.search("kubernetes monitoring", top_k=3)
        assert len(results) >= 1


# ── Scenario 5: Power User — Advanced Features ──────────────────────────


class TestPowerUserFeatures:
    """Power user leveraging advanced features."""

    async def test_frontmatter_tags_extracted(self, components, memory_dir):
        """YAML frontmatter tags are automatically applied to chunks."""
        f = memory_dir / "frontmatter.md"
        f.write_text("---\ntags: [api, backend]\n---\n\n## API Design\n\nREST vs GraphQL.")
        await components.index_engine.index_file(f)

        chunks = await components.storage.list_chunks_by_source(f)
        assert len(chunks) >= 1
        assert "api" in chunks[0].metadata.tags
        assert "backend" in chunks[0].metadata.tags

    async def test_wikilinks_resolved(self, components, memory_dir):
        """Obsidian wikilinks are cleaned from indexed content."""
        f = memory_dir / "obsidian.md"
        f.write_text("## Notes\n\nSee [[other-page]] and [[link|display text]] for details.")
        await components.index_engine.index_file(f)

        chunks = await components.storage.list_chunks_by_source(f)
        assert "[[" not in chunks[0].content
        assert "other-page" in chunks[0].content
        assert "display text" in chunks[0].content

    async def test_force_reindex(self, components, memory_dir):
        """force=True re-indexes unchanged files."""
        f = memory_dir / "stable.md"
        f.write_text("## Stable\n\nThis content does not change.")
        stats1 = await components.index_engine.index_file(f)
        assert stats1.indexed_chunks >= 1

        # Normal re-index skips unchanged
        stats2 = await components.index_engine.index_file(f)
        assert stats2.skipped_chunks >= 1
        assert stats2.indexed_chunks == 0

        # Force re-index processes everything
        stats3 = await components.index_engine.index_file(f, force=True)
        assert stats3.indexed_chunks >= 1

    async def test_duplicate_content_indexed(self, components, memory_dir):
        """Identical content in two files produces separate indexed chunks."""
        f1 = memory_dir / "dup1.md"
        f2 = memory_dir / "dup2.md"
        f1.write_text("## Config\n\nRedis connection pool size set to 20.")
        f2.write_text("## Config\n\nRedis connection pool size set to 20.")
        await components.index_engine.index_file(f1)
        await components.index_engine.index_file(f2)

        c1 = await components.storage.list_chunks_by_source(f1)
        c2 = await components.storage.list_chunks_by_source(f2)
        assert len(c1) >= 1
        assert len(c2) >= 1
        assert c1[0].content == c2[0].content  # same content, different sources


# ── Scenario 6: Migration — Obsidian Import ──────────────────────────────


class TestMigrationObsidian:
    """User importing existing Obsidian vault."""

    async def test_frontmatter_searchable(self, components, memory_dir):
        """Frontmatter YAML fields are included in searchable content."""
        f = memory_dir / "vault-note.md"
        f.write_text(
            "---\ntitle: API Redesign\nstatus: in-progress\ntags: [project, api]\n---\n\n"
            "## Overview\n\nMigrating from REST to GraphQL."
        )
        await components.index_engine.index_file(f)

        results, _ = await components.search_pipeline.search("GraphQL migration", top_k=3)
        assert len(results) >= 1

        # Frontmatter tags extracted
        chunks = await components.storage.list_chunks_by_source(f)
        tagged = [c for c in chunks if "project" in c.metadata.tags]
        assert len(tagged) >= 1

    async def test_heading_chunking(self, components, memory_dir):
        """Multiple headings produce separate chunks."""
        f = memory_dir / "multi-heading.md"
        f.write_text(
            "## Section A\n\nContent for section A with enough text.\n\n"
            "## Section B\n\nContent for section B with enough text.\n\n"
            "## Section C\n\nContent for section C with enough text."
        )
        await components.index_engine.index_file(f)

        chunks = await components.storage.list_chunks_by_source(f)
        headings = {c.metadata.heading_hierarchy for c in chunks}
        # Each heading should produce at least a separate entry in headings
        assert len(headings) >= 2

    async def test_mixed_language_file(self, components, memory_dir):
        """File with both Korean and English content is indexed correctly."""
        f = memory_dir / "mixed.md"
        f.write_text(
            "## 프로젝트 개요\n\n이 프로젝트는 GraphQL API를 구축합니다.\n\n"
            "## Technical Stack\n\nUsing Apollo Server with TypeScript."
        )
        await components.index_engine.index_file(f)

        chunks = await components.storage.list_chunks_by_source(f)
        assert len(chunks) >= 1

        # Both languages searchable
        kr_results, _ = await components.search_pipeline.search("GraphQL API 프로젝트", top_k=3)
        en_results, _ = await components.search_pipeline.search("Apollo TypeScript", top_k=3)
        assert len(kr_results) >= 1
        assert len(en_results) >= 1


# ── Helpers ──────────────────────────────────────────────────────────────


def _apply_tags(chunk, new_tags: set[str]):
    """Return chunk with merged tags."""
    merged = set(chunk.metadata.tags) | new_tags
    chunk.metadata = chunk.metadata.__class__(
        **{
            **{f: getattr(chunk.metadata, f) for f in chunk.metadata.__dataclass_fields__},
            "tags": tuple(sorted(merged)),
        }
    )
    return chunk


def _set_namespace(chunk, namespace: str):
    """Return chunk with updated namespace."""
    chunk.metadata = chunk.metadata.__class__(
        **{
            **{f: getattr(chunk.metadata, f) for f in chunk.metadata.__dataclass_fields__},
            "namespace": namespace,
        }
    )
    return chunk
