"""Tests for query expansion."""

import logging
from pathlib import Path

import pytest
from memtomem.search.expansion import expand_query_headings, expand_query_tags


class FakeStorage:
    """Mock storage for tag-based expansion tests."""

    def __init__(self, tags):
        self._tags = tags
        self.project_context_root = None
        self.scope_filter = None
        self.namespace_filter = None

    async def get_tag_counts(
        self, *, project_context_root=None, scope_filter=None, namespace_filter=None
    ):
        self.project_context_root = project_context_root
        self.scope_filter = scope_filter
        self.namespace_filter = namespace_filter
        return self._tags


class TestExpandQueryTags:
    @pytest.mark.asyncio
    async def test_no_matching_tags(self):
        storage = FakeStorage([("python", 5), ("javascript", 3)])
        result = await expand_query_tags("deployment strategy", storage)
        assert result == "deployment strategy"

    @pytest.mark.asyncio
    async def test_matching_tag_appended(self):
        storage = FakeStorage([("deploy", 5), ("testing", 3)])
        result = await expand_query_tags("deployment strategy", storage)
        assert "deploy" in result

    @pytest.mark.asyncio
    async def test_max_terms_respected(self):
        storage = FakeStorage([("deploy-a", 5), ("deploy-b", 3), ("deploy-c", 2), ("deploy-d", 1)])
        result = await expand_query_tags("deploy", storage, max_terms=2)
        # Original query + up to 2 terms
        added = result.replace("deploy", "").strip().split()
        assert len(added) <= 2

    @pytest.mark.asyncio
    async def test_short_words_ignored(self):
        """Words shorter than 3 chars should not trigger expansion."""
        storage = FakeStorage([("ai", 10), ("ml", 8)])
        result = await expand_query_tags("ai tools", storage)
        assert result == "ai tools"

    @pytest.mark.asyncio
    async def test_empty_tags(self):
        storage = FakeStorage([])
        result = await expand_query_tags("test query", storage)
        assert result == "test query"

    @pytest.mark.asyncio
    async def test_project_boundary_is_forwarded_to_tag_counts(self):
        storage = FakeStorage([("deploy", 1)])
        boundary = Path("/registered/project")

        await expand_query_tags("deployment strategy", storage, project_context_root=boundary)

        assert storage.project_context_root == boundary

    @pytest.mark.asyncio
    async def test_scope_and_namespace_filters_are_forwarded_to_tag_counts(self):
        """The outer search's boundary reaches the tag sample, so a tag outside it
        is never appended to a query that is then embedded and reranked."""
        from memtomem.models import NamespaceFilter, ScopeFilter

        storage = FakeStorage([("deployment", 1)])
        scope = ScopeFilter.parse("user")
        namespaces = NamespaceFilter.parse(None, system_prefixes=("archive:",))

        result = await expand_query_tags(
            "deploy strategy", storage, scope_filter=scope, namespace_filter=namespaces
        )

        assert result == "deploy strategy deployment"
        assert storage.scope_filter is scope
        assert storage.namespace_filter is namespaces


class _RaisingTagStorage:
    async def get_tag_counts(self, *, project_context_root=None, **filters):
        raise RuntimeError("tag store unavailable")


class _RaisingEmbedder:
    async def embed_query(self, query):
        raise RuntimeError("embedder offline")


class _RaisingDenseStorage:
    async def dense_search(self, embedding, top_k=3, **filters):
        raise RuntimeError("dense index offline")


class _OkEmbedder:
    async def embed_query(self, query):
        return [0.0, 0.0, 0.0]


class _RecordingDenseStorage:
    """Returns one chunk whose heading words expansion appends, and records the
    probe's filters."""

    def __init__(self):
        self.kwargs: dict = {}

    async def dense_search(self, embedding, top_k=3, **filters):
        from memtomem.models import Chunk, ChunkMetadata, SearchResult

        self.kwargs = {"top_k": top_k, **filters}
        chunk = Chunk(
            content="x",
            metadata=ChunkMetadata(
                source_file=Path("/notes/a.md"), heading_hierarchy=("## Rollout Window",)
            ),
        )
        return [SearchResult(chunk=chunk, score=1.0, rank=1, source="dense")]


@pytest.mark.asyncio
async def test_heading_probe_carries_the_outer_search_boundary():
    from memtomem.models import NamespaceFilter, ScopeFilter

    storage = _RecordingDenseStorage()
    scope = ScopeFilter.parse("user")
    namespaces = NamespaceFilter.parse(None, system_prefixes=("archive:",))
    root = Path("/registered/project")

    result = await expand_query_headings(
        "deploy",
        storage,
        _OkEmbedder(),
        project_context_root=root,
        scope_filter=scope,
        namespace_filter=namespaces,
    )

    assert result == "deploy rollout window"
    assert storage.kwargs["scope_filter"] is scope
    assert storage.kwargs["namespace_filter"] is namespaces
    assert storage.kwargs["project_context_root"] == root
    # The boundary filters run after the KNN pool is cut; the optional probe must
    # not escalate to a full-table pass on a sparse one.
    assert storage.kwargs["escalate"] is False


class TestExpansionFailureLogging:
    """Expansion failures must be visible (WARNING) so degraded search quality
    is observable in production. See feedback_silent_except_log_level."""

    @pytest.mark.asyncio
    async def test_tag_failure_emits_warning(self, caplog):
        with caplog.at_level(logging.WARNING, logger="memtomem.search.expansion"):
            result = await expand_query_tags("hello world", _RaisingTagStorage())

        assert result == "hello world"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Tag expansion failed" in r.getMessage() for r in warnings), (
            f"Expected a WARNING about tag expansion; got {[r.getMessage() for r in warnings]}"
        )

    @pytest.mark.asyncio
    async def test_heading_failure_emits_warning_when_embedder_fails(self, caplog):
        with caplog.at_level(logging.WARNING, logger="memtomem.search.expansion"):
            result = await expand_query_headings(
                "hello world", _RaisingDenseStorage(), _RaisingEmbedder()
            )

        assert result == "hello world"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Heading expansion failed" in r.getMessage() for r in warnings)

    @pytest.mark.asyncio
    async def test_heading_failure_emits_warning_when_storage_fails(self, caplog):
        with caplog.at_level(logging.WARNING, logger="memtomem.search.expansion"):
            result = await expand_query_headings(
                "hello world", _RaisingDenseStorage(), _OkEmbedder()
            )

        assert result == "hello world"
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Heading expansion failed" in r.getMessage() for r in warnings)
