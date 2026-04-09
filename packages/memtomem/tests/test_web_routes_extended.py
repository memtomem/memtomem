"""Extended web route tests covering tags, timeline, evaluation, scratch,
namespaces, dedup, decay, export, and procedures endpoints.

Uses the same httpx AsyncClient + ASGITransport pattern as test_web_routes.py
with mocked app.state dependencies.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.models import Chunk, ChunkMetadata
from memtomem.web.app import create_app


# ---------------------------------------------------------------------------
# Stub objects
# ---------------------------------------------------------------------------

CHUNK_ID = uuid.uuid4()


def _make_test_chunk(
    chunk_id: uuid.UUID | None = None,
    content: str = "test chunk content",
    source: str = "/tmp/test.md",
    tags: tuple[str, ...] = ("tag1",),
    namespace: str = "default",
) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(source),
            heading_hierarchy=("Overview",),
            tags=tags,
            namespace=namespace,
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
    storage.upsert_chunks = AsyncMock()
    storage.stored_embedding_info = None
    storage.embedding_mismatch = None

    # tags
    storage.get_tag_counts = AsyncMock(return_value=[("python", 10), ("memory", 5), ("docs", 2)])

    # timeline / recall
    storage.recall_chunks = AsyncMock(return_value=[_make_test_chunk()])

    # evaluation / health report
    storage.get_health_report = AsyncMock(
        return_value={
            "total_chunks": 42,
            "access_coverage": {"accessed": 10, "total": 42, "pct": 23.8},
            "tag_coverage": {"tagged": 30, "total": 42, "pct": 71.4},
            "dead_memories_pct": 76.2,
            "top_accessed": [],
            "namespace_distribution": [{"namespace": "default", "count": 42}],
            "sessions": {"total": 5, "active": 1},
            "working_memory": {"total": 3, "promoted": 1},
            "cross_references": 7,
        }
    )

    # scratch
    storage.scratch_list = AsyncMock(
        return_value=[
            {
                "key": "current_task",
                "value": "writing tests",
                "session_id": None,
                "created_at": "2026-01-01T00:00:00Z",
                "expires_at": None,
                "promoted": 0,
            }
        ]
    )

    # namespaces
    storage.list_namespace_meta = AsyncMock(
        return_value=[
            {"namespace": "default", "chunk_count": 30, "description": "General", "color": "#3b82f6"},
            {"namespace": "work", "chunk_count": 12, "description": "Work notes", "color": "#ef4444"},
        ]
    )
    storage.list_namespaces = AsyncMock(
        return_value=[("default", 30), ("work", 12)]
    )
    storage.get_namespace_meta = AsyncMock(
        return_value={"description": "General", "color": "#3b82f6"}
    )

    # procedures — _get_db() returns a mock connection
    mock_db = MagicMock()
    mock_db.execute.return_value.fetchall.return_value = []
    storage._get_db = MagicMock(return_value=mock_db)

    # -- embedder mock --
    embedder = AsyncMock()
    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 768])
    embedder.embed_query = AsyncMock(return_value=[0.1] * 768)

    # -- search pipeline mock --
    search_pipeline = AsyncMock()
    search_pipeline.invalidate_cache = MagicMock()

    # -- index engine mock --
    index_engine = AsyncMock()

    # -- dedup scanner mock --
    dedup_scanner = AsyncMock()
    dedup_scanner.scan = AsyncMock(return_value=[])

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
# GET /api/tags
# ---------------------------------------------------------------------------


class TestTags:
    async def test_list_tags_returns_all(self, client: AsyncClient):
        resp = await client.get("/api/tags")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert len(data["tags"]) == 3
        assert data["tags"][0]["tag"] == "python"
        assert data["tags"][0]["count"] == 10

    async def test_list_tags_pagination(self, client: AsyncClient):
        resp = await client.get("/api/tags", params={"limit": 1, "offset": 1})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3  # total is full count
        assert len(data["tags"]) == 1  # only 1 returned
        assert data["tags"][0]["tag"] == "memory"

    async def test_list_tags_empty(self, app, client: AsyncClient):
        app.state.storage.get_tag_counts.return_value = []
        resp = await client.get("/api/tags")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["tags"] == []


# ---------------------------------------------------------------------------
# GET /api/timeline
# ---------------------------------------------------------------------------


class TestTimeline:
    async def test_timeline_returns_chunks(self, client: AsyncClient):
        resp = await client.get("/api/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert len(data["chunks"]) == 1
        assert data["chunks"][0]["content"] == "test chunk content"

    async def test_timeline_with_days_param(self, client: AsyncClient):
        resp = await client.get("/api/timeline", params={"days": 7})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] >= 0

    async def test_timeline_empty(self, app, client: AsyncClient):
        app.state.storage.recall_chunks.return_value = []
        resp = await client.get("/api/timeline")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["chunks"] == []


# ---------------------------------------------------------------------------
# GET /api/eval  (evaluation / health report)
# ---------------------------------------------------------------------------


class TestEvaluation:
    async def test_eval_returns_report(self, client: AsyncClient):
        resp = await client.get("/api/eval")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_chunks"] == 42
        assert "access_coverage" in data
        assert "tag_coverage" in data
        assert "dead_memories_pct" in data
        assert data["sessions"]["total"] == 5

    async def test_eval_with_namespace_filter(self, client: AsyncClient):
        resp = await client.get("/api/eval", params={"namespace": "work"})
        assert resp.status_code == 200
        # The mock returns the same report regardless; just verify it passes through
        data = resp.json()
        assert "total_chunks" in data


# ---------------------------------------------------------------------------
# GET /api/scratch
# ---------------------------------------------------------------------------


class TestScratch:
    async def test_list_scratch_items(self, client: AsyncClient):
        resp = await client.get("/api/scratch")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["entries"][0]["key"] == "current_task"
        assert data["entries"][0]["value"] == "writing tests"

    async def test_list_scratch_empty(self, app, client: AsyncClient):
        app.state.storage.scratch_list.return_value = []
        resp = await client.get("/api/scratch")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["entries"] == []


# ---------------------------------------------------------------------------
# GET /api/namespaces
# ---------------------------------------------------------------------------


class TestNamespaces:
    async def test_list_namespaces(self, client: AsyncClient):
        resp = await client.get("/api/namespaces")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        ns_names = [ns["namespace"] for ns in data["namespaces"]]
        assert "default" in ns_names
        assert "work" in ns_names

    async def test_list_namespaces_includes_metadata(self, client: AsyncClient):
        resp = await client.get("/api/namespaces")
        assert resp.status_code == 200
        data = resp.json()
        default_ns = next(ns for ns in data["namespaces"] if ns["namespace"] == "default")
        assert default_ns["chunk_count"] == 30
        assert default_ns["description"] == "General"


# ---------------------------------------------------------------------------
# GET /api/dedup/candidates  (dedup scan)
# ---------------------------------------------------------------------------


class TestDedup:
    async def test_dedup_scan_empty(self, client: AsyncClient):
        resp = await client.get("/api/dedup/candidates")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["candidates"] == []

    async def test_dedup_scan_with_params(self, client: AsyncClient):
        resp = await client.get(
            "/api/dedup/candidates",
            params={"threshold": 0.85, "limit": 50, "max_scan": 200},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["scanned_chunks"] == 200


# ---------------------------------------------------------------------------
# GET /api/decay/scan
# ---------------------------------------------------------------------------


class TestDecay:
    async def test_decay_scan(self, client: AsyncClient):
        with patch("memtomem.web.routes.decay.expire_chunks") as mock_expire:
            from memtomem.search.decay import ExpireStats

            mock_expire.return_value = ExpireStats(
                total_chunks=42, expired_chunks=5, deleted_chunks=0
            )
            resp = await client.get("/api/decay/scan")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_chunks"] == 42
            assert data["expired_chunks"] == 5
            assert data["dry_run"] is True

    async def test_decay_scan_with_params(self, client: AsyncClient):
        with patch("memtomem.web.routes.decay.expire_chunks") as mock_expire:
            from memtomem.search.decay import ExpireStats

            mock_expire.return_value = ExpireStats(
                total_chunks=100, expired_chunks=20, deleted_chunks=0
            )
            resp = await client.get(
                "/api/decay/scan", params={"max_age_days": 60.0}
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["expired_chunks"] == 20


# ---------------------------------------------------------------------------
# GET /api/export  (export chunks as JSON)
# ---------------------------------------------------------------------------


class TestExport:
    async def test_export_returns_json_bundle(self, client: AsyncClient):
        with patch("memtomem.tools.export_import.export_chunks") as mock_export:
            from memtomem.tools.export_import import ExportBundle

            bundle = ExportBundle(
                exported_at="2026-01-01T00:00:00Z",
                total_chunks=2,
                chunks=[
                    {"content": "chunk1", "source_file": "/tmp/a.md"},
                    {"content": "chunk2", "source_file": "/tmp/b.md"},
                ],
            )
            mock_export.return_value = bundle

            resp = await client.get("/api/export")
            assert resp.status_code == 200
            assert resp.headers["content-type"] == "application/json"
            data = resp.json()
            assert data["total_chunks"] == 2
            assert len(data["chunks"]) == 2

    async def test_export_with_filters(self, client: AsyncClient):
        with patch("memtomem.tools.export_import.export_chunks") as mock_export:
            from memtomem.tools.export_import import ExportBundle

            bundle = ExportBundle(
                exported_at="2026-01-01T00:00:00Z",
                total_chunks=1,
                chunks=[{"content": "filtered", "source_file": "/tmp/a.md"}],
            )
            mock_export.return_value = bundle

            resp = await client.get(
                "/api/export",
                params={"source": "a.md", "tag": "python", "namespace": "work"},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_chunks"] == 1

    async def test_export_stats(self, client: AsyncClient):
        with patch("memtomem.tools.export_import.export_chunks") as mock_export:
            from memtomem.tools.export_import import ExportBundle

            bundle = ExportBundle(
                exported_at="2026-01-01T00:00:00Z", total_chunks=42, chunks=[]
            )
            mock_export.return_value = bundle

            resp = await client.get("/api/export/stats")
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_chunks"] == 42


# ---------------------------------------------------------------------------
# GET /api/procedures
# ---------------------------------------------------------------------------


class TestProcedures:
    async def test_list_procedures_empty(self, client: AsyncClient):
        resp = await client.get("/api/procedures")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["procedures"] == []
