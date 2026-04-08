"""Tests for FastAPI web routes using httpx AsyncClient.

The web app is created by create_app() and dependencies are injected via
request.app.state.  We override app.state with mock/stub objects to avoid
full component initialization (embedding provider, SQLite, etc.).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.models import Chunk, ChunkMetadata, IndexingStats, SearchResult
from memtomem.search.pipeline import RetrievalStats
from memtomem.web.app import create_app


# ---------------------------------------------------------------------------
# Stub objects that stand in for real components
# ---------------------------------------------------------------------------

CHUNK_ID = uuid.uuid4()


def _make_test_chunk(
    chunk_id: uuid.UUID | None = None,
    content: str = "test chunk content",
    source: str = "/tmp/test.md",
) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(source),
            heading_hierarchy=("Overview",),
            tags=("tag1",),
            namespace="default",
            start_line=1,
            end_line=5,
        ),
        id=chunk_id or CHUNK_ID,
        content_hash="abc123",
        embedding=[0.1] * 768,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )


@dataclass
class FakeConfig:
    """Minimal stand-in for Mem2MemConfig with the fields the routes need."""

    class _Embedding:
        provider = "ollama"
        model = "nomic-embed-text"
        dimension = 768
        base_url = "http://localhost:11434"
        batch_size = 64
        api_key = ""

    class _Storage:
        backend = "sqlite"
        sqlite_path = Path("/tmp/test.db")
        collection_name = "memories"

    class _Search:
        default_top_k = 10
        bm25_candidates = 50
        dense_candidates = 50
        rrf_k = 60
        enable_bm25 = True
        enable_dense = True
        tokenizer = "unicode61"
        rrf_weights = [1.0, 1.0]

    class _Indexing:
        memory_dirs = [Path("/tmp/memories")]
        supported_extensions = frozenset({".md", ".json"})
        max_chunk_tokens = 512
        min_chunk_tokens = 128
        chunk_overlap_tokens = 0
        structured_chunk_mode = "original"

    class _Decay:
        enabled = False
        half_life_days = 30.0

    class _MMR:
        enabled = False
        lambda_param = 0.7

    class _Namespace:
        default_namespace = "default"
        enable_auto_ns = False

    embedding = _Embedding()
    storage = _Storage()
    search = _Search()
    indexing = _Indexing()
    decay = _Decay()
    mmr = _MMR()
    namespace = _Namespace()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app():
    """Create an app without lifespan and wire mock state."""
    application = create_app(lifespan=None)

    # -- storage mock --
    storage = AsyncMock()
    storage.get_stats = AsyncMock(return_value={"total_chunks": 42, "total_sources": 3})
    storage.get_chunk_size_distribution = AsyncMock(return_value=[])
    storage.get_chunk = AsyncMock(return_value=_make_test_chunk())
    storage.get_all_source_files = AsyncMock(return_value=[Path("/tmp/test.md")])
    storage.list_chunks_by_source = AsyncMock(return_value=[_make_test_chunk()])
    storage.delete_chunks = AsyncMock()
    storage.delete_by_source = AsyncMock(return_value=1)
    storage.get_source_files_with_counts = AsyncMock(
        return_value=[
            (
                Path("/tmp/test.md"),
                5,
                "2026-01-01T00:00:00",
                "default",
                100,
                50,
                200,
            )
        ]
    )
    storage.list_sessions = AsyncMock(return_value=[])
    storage.get_session_events = AsyncMock(return_value=[])
    storage.upsert_chunks = AsyncMock()
    storage.stored_embedding_info = None
    storage.embedding_mismatch = None

    # -- embedder mock --
    embedder = AsyncMock()
    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 768])
    embedder.embed_query = AsyncMock(return_value=[0.1] * 768)

    # -- search pipeline mock --
    search_pipeline = AsyncMock()
    test_chunk = _make_test_chunk()
    result = SearchResult(chunk=test_chunk, score=0.95, rank=1, source="fused")
    rstats = RetrievalStats(bm25_candidates=10, dense_candidates=10, fused_total=1, final_total=1)
    search_pipeline.search = AsyncMock(return_value=([result], rstats))
    search_pipeline.invalidate_cache = MagicMock()

    # -- index engine mock --
    index_engine = AsyncMock()
    index_engine.index_path = AsyncMock(
        return_value=IndexingStats(
            total_files=1,
            total_chunks=2,
            indexed_chunks=2,
            skipped_chunks=0,
            deleted_chunks=0,
            duration_ms=100.0,
        )
    )
    index_engine.index_file = AsyncMock(
        return_value=IndexingStats(
            total_files=1,
            total_chunks=1,
            indexed_chunks=1,
            skipped_chunks=0,
            deleted_chunks=0,
            duration_ms=50.0,
        )
    )

    # -- dedup scanner mock --
    dedup_scanner = AsyncMock()

    # Wire into app.state
    application.state.storage = storage
    application.state.embedder = embedder
    application.state.search_pipeline = search_pipeline
    application.state.index_engine = index_engine
    application.state.config = FakeConfig()
    application.state.dedup_scanner = dedup_scanner

    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# GET /api/health
# ---------------------------------------------------------------------------


class TestHealth:
    async def test_health_ok(self, client: AsyncClient):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert "checks" in data
        assert data["checks"]["storage"] == "ok"
        assert data["checks"]["embedding"] == "ok"

    async def test_health_degraded_when_storage_fails(self, app, client: AsyncClient):
        app.state.storage.get_stats.side_effect = RuntimeError("db down")
        resp = await client.get("/api/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["checks"]["storage"] == "RuntimeError"


# ---------------------------------------------------------------------------
# GET /api/stats
# ---------------------------------------------------------------------------


class TestStats:
    async def test_stats_returns_counts(self, client: AsyncClient):
        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_chunks"] == 42
        assert data["total_sources"] == 3
        assert "chunk_size_distribution" in data


# ---------------------------------------------------------------------------
# GET /api/config
# ---------------------------------------------------------------------------


class TestConfig:
    async def test_config_returns_sections(self, client: AsyncClient):
        resp = await client.get("/api/config")
        assert resp.status_code == 200
        data = resp.json()
        assert "embedding" in data
        assert data["embedding"]["provider"] == "ollama"
        assert "search" in data
        assert "indexing" in data
        assert "decay" in data
        assert "mmr" in data
        assert "namespace" in data


# ---------------------------------------------------------------------------
# GET /api/search
# ---------------------------------------------------------------------------


class TestSearch:
    async def test_search_returns_results(self, client: AsyncClient):
        resp = await client.get("/api/search", params={"q": "hello world"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["results"]) == 1
        result = data["results"][0]
        assert result["score"] == pytest.approx(0.95)
        assert result["chunk"]["content"] == "test chunk content"

    async def test_search_missing_query_returns_422(self, client: AsyncClient):
        resp = await client.get("/api/search")
        assert resp.status_code == 422

    async def test_search_with_filters(self, client: AsyncClient):
        resp = await client.get(
            "/api/search",
            params={"q": "test", "top_k": 5, "namespace": "work"},
        )
        assert resp.status_code == 200

    async def test_search_pipeline_error_returns_500(self, app, client: AsyncClient):
        app.state.search_pipeline.search.side_effect = RuntimeError("search failed")
        resp = await client.get("/api/search", params={"q": "test"})
        assert resp.status_code == 500


# ---------------------------------------------------------------------------
# GET /api/sources
# ---------------------------------------------------------------------------


class TestSources:
    async def test_list_sources(self, client: AsyncClient):
        resp = await client.get("/api/sources")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["sources"]) == 1
        src = data["sources"][0]
        assert src["chunk_count"] == 5
        assert "path" in src

    async def test_list_sources_pagination(self, client: AsyncClient):
        resp = await client.get("/api/sources", params={"limit": 1, "offset": 0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 1
        assert data["offset"] == 0


# ---------------------------------------------------------------------------
# GET /api/chunks
# ---------------------------------------------------------------------------


class TestChunksList:
    async def test_list_chunks_for_source(self, client: AsyncClient):
        resp = await client.get("/api/chunks", params={"source": "/tmp/test.md"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["chunks"][0]["content"] == "test chunk content"

    async def test_list_chunks_missing_source_returns_422(self, client: AsyncClient):
        resp = await client.get("/api/chunks")
        assert resp.status_code == 422

    async def test_list_chunks_non_indexed_source_returns_403(self, app, client: AsyncClient):
        app.state.storage.get_all_source_files.return_value = [Path("/tmp/other.md")]
        resp = await client.get("/api/chunks", params={"source": "/tmp/test.md"})
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/chunks/{id}
# ---------------------------------------------------------------------------


class TestGetChunk:
    async def test_get_chunk_by_id(self, client: AsyncClient):
        resp = await client.get(f"/api/chunks/{CHUNK_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(CHUNK_ID)
        assert data["content"] == "test chunk content"
        assert data["tags"] == ["tag1"]
        assert data["heading_hierarchy"] == ["Overview"]

    async def test_get_chunk_not_found(self, app, client: AsyncClient):
        app.state.storage.get_chunk.return_value = None
        fake_id = uuid.uuid4()
        resp = await client.get(f"/api/chunks/{fake_id}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# DELETE /api/chunks/{id}
# ---------------------------------------------------------------------------


class TestDeleteChunk:
    async def test_delete_chunk(self, client: AsyncClient):
        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 1

    async def test_delete_chunk_not_found(self, app, client: AsyncClient):
        app.state.storage.get_chunk.return_value = None
        fake_id = uuid.uuid4()
        resp = await client.delete(f"/api/chunks/{fake_id}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# PATCH /api/chunks/{id}
# ---------------------------------------------------------------------------


class TestEditChunk:
    async def test_edit_chunk_not_found(self, app, client: AsyncClient):
        app.state.storage.get_chunk.return_value = None
        fake_id = uuid.uuid4()
        resp = await client.patch(
            f"/api/chunks/{fake_id}",
            json={"new_content": "updated"},
        )
        assert resp.status_code == 404

    async def test_edit_chunk_rejects_symlinks(self, app, client: AsyncClient):
        chunk = _make_test_chunk()
        # Override source_file.is_symlink to return True
        with patch.object(type(chunk.metadata.source_file), "is_symlink", return_value=True):
            app.state.storage.get_chunk.return_value = chunk
            resp = await client.patch(
                f"/api/chunks/{CHUNK_ID}",
                json={"new_content": "updated"},
            )
            assert resp.status_code == 403


# ---------------------------------------------------------------------------
# GET /api/sessions
# ---------------------------------------------------------------------------


class TestSessions:
    async def test_list_sessions_empty(self, client: AsyncClient):
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sessions"] == []
        assert data["total"] == 0

    async def test_list_sessions_with_data(self, app, client: AsyncClient):
        app.state.storage.list_sessions.return_value = [
            {
                "id": "sess-1",
                "agent_id": "agent-a",
                "started_at": "2026-01-01T00:00:00Z",
                "ended_at": None,
                "summary": None,
                "namespace": "default",
            }
        ]
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["sessions"][0]["id"] == "sess-1"


# ---------------------------------------------------------------------------
# POST /api/add
# ---------------------------------------------------------------------------


class TestAddMemory:
    async def test_add_memory_success(self, client: AsyncClient):
        resp = await client.post(
            "/api/add",
            json={"content": "Remember this important fact."},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "file" in data
        assert data["indexed_chunks"] == 1

    async def test_add_memory_missing_content(self, client: AsyncClient):
        resp = await client.post("/api/add", json={})
        assert resp.status_code == 422

    async def test_add_memory_empty_content(self, client: AsyncClient):
        resp = await client.post("/api/add", json={"content": ""})
        assert resp.status_code == 422

    async def test_add_memory_rejects_absolute_file_path(self, client: AsyncClient):
        resp = await client.post(
            "/api/add",
            json={"content": "test", "file": "/etc/passwd"},
        )
        assert resp.status_code == 422

    async def test_add_memory_rejects_path_traversal(self, client: AsyncClient):
        resp = await client.post(
            "/api/add",
            json={"content": "test", "file": "../../etc/passwd"},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/index
# ---------------------------------------------------------------------------


class TestIndex:
    async def test_trigger_index(self, client: AsyncClient):
        resp = await client.post("/api/index", json={"path": "/tmp/memories"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_files"] == 1
        assert data["indexed_chunks"] == 2

    async def test_trigger_index_default_params(self, client: AsyncClient):
        resp = await client.post("/api/index")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# GET /api/embedding-status
# ---------------------------------------------------------------------------


class TestEmbeddingStatus:
    async def test_no_mismatch(self, client: AsyncClient):
        resp = await client.get("/api/embedding-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_mismatch"] is False
