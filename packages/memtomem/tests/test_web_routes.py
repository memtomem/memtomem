"""Tests for FastAPI web routes using httpx AsyncClient.

The web app is created by create_app() and dependencies are injected via
request.app.state.  We override app.state with mock/stub objects to avoid
full component initialization (embedding provider, SQLite, etc.).
"""

from __future__ import annotations

import unicodedata
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
        target_chunk_tokens = 384
        chunk_overlap_tokens = 0
        structured_chunk_mode = "original"
        exclude_patterns: list[str] = []

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
    application = create_app(lifespan=None, mode="dev")

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
    cfg = FakeConfig()
    # _Indexing is a class-level singleton — reset mutable fields so tests that
    # mutate exclude_patterns don't leak into later tests.
    cfg.indexing.exclude_patterns = []
    application.state.config = cfg
    application.state.dedup_scanner = dedup_scanner

    # Pin the hot-reload signature to the current on-disk state so these
    # FakeConfig-based tests don't get their state.config swapped out for a
    # real Mem2MemConfig built from ``~/.memtomem``. Dedicated hot-reload
    # tests live in tests/test_web_hot_reload.py where reload behavior is
    # exercised against a real tmp HOME.
    from memtomem.web import hot_reload as _hot_reload

    application.state.config_signature = _hot_reload.current_signature()
    application.state.last_reload_error = None

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
        assert data["checks"]["storage"] == "error"
        # Exception class name must not leak to the response (see #75).
        assert "RuntimeError" not in resp.text

    async def test_health_degraded_logs_warning(self, app, client: AsyncClient, caplog):
        """Failures must be logged server-side so operators can diagnose."""
        import logging

        app.state.storage.get_stats.side_effect = RuntimeError("db down")
        with caplog.at_level(logging.WARNING, logger="memtomem.web.routes.system"):
            await client.get("/api/health")
        assert any("storage" in r.message for r in caplog.records)


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
        assert data["indexing"]["exclude_patterns"] == []

    async def test_builtin_exclude_patterns(self, client: AsyncClient):
        resp = await client.get("/api/indexing/builtin-exclude-patterns")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["secret"], list)
        assert isinstance(data["noise"], list)
        assert data["secret"], "secret list should not be empty"
        # Sample a known built-in secret pattern to detect silent removals.
        assert any(p.endswith("/id_rsa*") for p in data["secret"])

    async def test_config_defaults_returns_comparand(self, client: AsyncClient):
        """GET /api/config/defaults returns the comparand config shape.

        The endpoint must pull from ``build_comparand`` (defaults + env +
        fragments), not ``app.state.config`` — otherwise the Web UI reset
        button would "reset" to the pinned value, i.e. do nothing.
        """
        from memtomem.config import Mem2MemConfig

        # Construct a comparand with a non-default value so we can tell it
        # apart from app.state.config (which has FakeConfig mmr.enabled=False).
        fake_comparand = Mem2MemConfig()
        fake_comparand.mmr.enabled = True
        fake_comparand.search.default_top_k = 25

        with patch("memtomem.web.routes.system.build_comparand", return_value=fake_comparand):
            resp = await client.get("/api/config/defaults")

        assert resp.status_code == 200
        data = resp.json()
        # Shape matches ConfigResponse (same as GET /api/config).
        assert set(data.keys()) >= {
            "embedding",
            "storage",
            "search",
            "indexing",
            "decay",
            "mmr",
            "namespace",
        }
        # Comparand values come through, not app.state.config values.
        assert data["mmr"]["enabled"] is True
        assert data["search"]["default_top_k"] == 25

    async def test_config_defaults_independent_of_live_config(self, app, client: AsyncClient):
        """Live config mutations must not leak into /config/defaults.

        Regression guard: if the endpoint ever accidentally reads
        ``app.state.config``, this test fails because the fake comparand
        would report the mutated value.
        """
        from memtomem.config import Mem2MemConfig

        fake_comparand = Mem2MemConfig()
        fake_comparand.search.default_top_k = 7

        # Mutate live config to a distinct value.
        app.state.config.search.default_top_k = 999

        with patch("memtomem.web.routes.system.build_comparand", return_value=fake_comparand):
            resp = await client.get("/api/config/defaults")

        assert resp.status_code == 200
        assert resp.json()["search"]["default_top_k"] == 7

    async def test_patch_exclude_patterns_accepts_valid(self, app, client: AsyncClient):
        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.patch(
                "/api/config",
                json={"indexing": {"exclude_patterns": ["**/*.log", "dist/**"]}},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["rejected"] == []
        assert any(c["field"] == "indexing.exclude_patterns" for c in data["applied"])
        assert app.state.config.indexing.exclude_patterns == ["**/*.log", "dist/**"]

    async def test_patch_exclude_patterns_rejects_malformed(self, app, client: AsyncClient):
        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.patch(
                "/api/config",
                json={"indexing": {"exclude_patterns": ["!"]}},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] == []
        assert any(
            "indexing.exclude_patterns" in r and "Invalid git pattern" in r
            for r in data["rejected"]
        )
        # Bad input must not mutate the live config.
        assert app.state.config.indexing.exclude_patterns == []

    async def test_patch_exclude_patterns_rejects_duplicate(self, app, client: AsyncClient):
        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.patch(
                "/api/config",
                json={"indexing": {"exclude_patterns": ["**/*.log", "**/*.log"]}},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] == []
        assert any("duplicate pattern" in r for r in data["rejected"])
        assert app.state.config.indexing.exclude_patterns == []


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

    async def test_edit_chunk_preserves_blockquote_header(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """Body-only PATCH must keep the per-entry ``> created:`` / ``> tags:``
        blockquote and the heading. The Web UI editor surfaces ``chunk.content``
        (already header-stripped by the chunker), so without preservation a
        Save would silently erase metadata on disk.
        """
        source = tmp_path / "memory.md"
        source.write_text(
            "## Cache strategy\n"
            "\n"
            "> created: 2026-04-24T22:00:00+00:00\n"
            '> tags: ["cache", "decision"]\n'
            "\n"
            "Old body line.\n",
            encoding="utf-8",
        )
        chunk = _make_test_chunk(source=str(source))
        # Chunk range covers the entire entry on disk.
        chunk = chunk.__class__(
            content=chunk.content,
            metadata=chunk.metadata.__class__(
                source_file=source,
                heading_hierarchy=("## Cache strategy",),
                tags=chunk.metadata.tags,
                namespace=chunk.metadata.namespace,
                start_line=1,
                end_line=6,
            ),
            id=chunk.id,
            content_hash=chunk.content_hash,
            embedding=chunk.embedding,
            created_at=chunk.created_at,
            updated_at=chunk.updated_at,
        )
        app.state.storage.get_chunk.return_value = chunk

        resp = await client.patch(
            f"/api/chunks/{CHUNK_ID}",
            json={"new_content": "Replaced body."},
        )
        assert resp.status_code == 200

        on_disk = source.read_text(encoding="utf-8")
        assert "## Cache strategy" in on_disk
        assert "> created: 2026-04-24T22:00:00+00:00" in on_disk
        assert '> tags: ["cache", "decision"]' in on_disk
        assert "Replaced body." in on_disk
        assert "Old body line." not in on_disk


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
        # Default path "." is outside configured memory_dirs, should be rejected
        resp = await client.post("/api/index")
        assert resp.status_code == 403

    async def test_trigger_index_outside_memory_dirs(self, client: AsyncClient):
        resp = await client.post("/api/index", json={"path": "/etc"})
        assert resp.status_code == 403

    async def test_trigger_index_surfaces_engine_errors(self, app, client: AsyncClient):
        """#354 regression: POST /api/index must surface ``IndexingStats.errors``
        in the response body. Before the fix the engine aggregated errors
        into stats.errors (e.g. "Embedding failed: fastembed is required")
        and the route ignored them, so callers got a clean 200 OK with
        indexed_chunks=0 and no signal that anything went wrong."""
        app.state.index_engine.index_path = AsyncMock(
            return_value=IndexingStats(
                total_files=3,
                total_chunks=10,
                indexed_chunks=0,
                skipped_chunks=10,
                deleted_chunks=0,
                duration_ms=50.0,
                errors=(
                    "Embedding failed: fastembed is required for the ONNX "
                    "embedding provider. Install it with: pip install memtomem[onnx]",
                ),
            )
        )
        resp = await client.post("/api/index", json={"path": "/tmp/memories"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["indexed_chunks"] == 0
        assert len(data["errors"]) == 1
        assert "fastembed" in data["errors"][0]


# ---------------------------------------------------------------------------
# GET /api/embedding-status
# ---------------------------------------------------------------------------


class TestEmbeddingStatus:
    async def test_no_mismatch(self, client: AsyncClient):
        resp = await client.get("/api/embedding-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_mismatch"] is False


# ---------------------------------------------------------------------------
# GET /locales/*.json  (i18n files served via StaticFiles)
# ---------------------------------------------------------------------------


class TestLocaleEndpoints:
    async def test_en_locale_served(self, client: AsyncClient):
        resp = await client.get("/locales/en.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "nav.home" in data

    async def test_ko_locale_served(self, client: AsyncClient):
        resp = await client.get("/locales/ko.json")
        assert resp.status_code == 200
        data = resp.json()
        assert "nav.home" in data

    async def test_i18n_js_served(self, client: AsyncClient):
        resp = await client.get("/i18n.js")
        assert resp.status_code == 200
        assert "i18n" in resp.text.lower()


# ---------------------------------------------------------------------------
# Unicode path normalization (#235, #238)
# ---------------------------------------------------------------------------


class TestUnicodePaths:
    """Regression for #235 and #238: NFD on-disk vs NFC user-input path mismatch.

    Non-ASCII directory names (e.g. Google Drive's Korean "내 드라이브" /
    "My Drive" localization) can surface on disk in decomposed (NFD) form
    while users type the composed (NFC) form. Without Unicode normalization
    in ``norm_path``, equality checks in the web routes fail even when both
    strings refer to the same path:

    - #235 (sources/chunks routes) — raw ``.resolve()`` 403 mismatch.
    - #238 (memory-dirs routes) — ``in`` / ``!=`` dedup/remove mismatch.
    """

    @staticmethod
    def _nfd(s: str) -> str:
        return unicodedata.normalize("NFD", s)

    @staticmethod
    def _nfc(s: str) -> str:
        return unicodedata.normalize("NFC", s)

    def test_korean_nfd_nfc_byte_strings_differ(self):
        # Guard: "내 드라이브" must decompose differently under NFC/NFD,
        # otherwise the tests below don't actually exercise the bug.
        assert self._nfd("내 드라이브") != self._nfc("내 드라이브")

    async def test_delete_source_matches_nfd_indexed_with_nfc_query(
        self, app, client: AsyncClient, tmp_path
    ):
        nfd_path = tmp_path / self._nfd("내 드라이브") / "file.md"
        app.state.storage.get_all_source_files.return_value = [nfd_path]

        nfc_query = str(tmp_path / self._nfc("내 드라이브") / "file.md")
        resp = await client.delete("/api/sources", params={"path": nfc_query})
        assert resp.status_code == 200, resp.text

    async def test_source_content_matches_nfd_indexed_with_nfc_query(
        self, app, client: AsyncClient, tmp_path
    ):
        # Create the on-disk file under the NFC name so ``Path.exists()``
        # passes on Linux CI (ext4 has no normalization-insensitive lookup).
        # The storage mock still reports the file under its NFD-encoded
        # path — mirroring the macOS/APFS case where ``realpath`` hands back
        # the stored NFD form while the user typed NFC.
        nfc_dir = tmp_path / self._nfc("내 드라이브")
        nfc_dir.mkdir()
        real_file = nfc_dir / "file.md"
        real_file.write_text("hello from NFC")

        nfd_path = tmp_path / self._nfd("내 드라이브") / "file.md"
        app.state.storage.get_all_source_files.return_value = [nfd_path]

        resp = await client.get("/api/sources/content", params={"path": str(real_file)})
        assert resp.status_code == 200, resp.text
        assert resp.json()["content"] == "hello from NFC"

    async def test_list_chunks_matches_nfd_indexed_with_nfc_query(
        self, app, client: AsyncClient, tmp_path
    ):
        nfd_path = tmp_path / self._nfd("내 드라이브") / "file.md"
        app.state.storage.get_all_source_files.return_value = [nfd_path]

        nfc_query = str(tmp_path / self._nfc("내 드라이브") / "file.md")
        resp = await client.get("/api/chunks", params={"source": nfc_query})
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 1

    async def test_add_memory_dir_deduplicates_nfd_and_nfc(
        self, app, client: AsyncClient, tmp_path
    ):
        # Config already holds the directory under an NFD-encoded path
        # (representative of macOS/APFS paths returned by ``realpath`` when the
        # dirent is stored decomposed). The user POSTs the same directory in
        # NFC form; without NFC normalization the route would treat it as new
        # and append a duplicate entry (#238).
        nfd_dir = tmp_path / self._nfd("내 드라이브")
        app.state.config.indexing.memory_dirs = [nfd_dir]

        nfc_dir = tmp_path / self._nfc("내 드라이브")
        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(nfc_dir)},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["message"] == "Already in memory_dirs"
        assert len(app.state.config.indexing.memory_dirs) == 1

    async def test_remove_memory_dir_matches_nfd_and_nfc(self, app, client: AsyncClient, tmp_path):
        # Config has the target dir in NFD form plus a second entry (the
        # route refuses to remove the last remaining memory_dir). The user
        # POSTs the NFC form — without NFC normalization the filter keeps
        # the NFD entry and the route returns 404 "Directory not in
        # memory_dirs" (#238).
        nfd_dir = tmp_path / self._nfd("내 드라이브")
        other_dir = tmp_path / "other"
        app.state.config.indexing.memory_dirs = [nfd_dir, other_dir]

        nfc_dir = tmp_path / self._nfc("내 드라이브")
        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/remove",
                json={"path": str(nfc_dir)},
            )
        assert resp.status_code == 200, resp.text
        assert app.state.config.indexing.memory_dirs == [other_dir]

    async def test_index_stream_rejects_sibling_path_with_shared_prefix(
        self, app, client: AsyncClient, tmp_path
    ):
        # Regression for #238: the previous ``str.startswith`` check let a
        # sibling path with a shared string prefix slip past the memory_dir
        # gate (e.g. memory_dir ``/foo/bar`` accepted ``/foo/barbaz``).
        # ``Path.is_relative_to`` compares parts, so the sibling is rejected.
        bar_dir = tmp_path / "bar"
        bar_dir.mkdir()
        barbaz_dir = tmp_path / "barbaz"
        barbaz_dir.mkdir()
        app.state.config.indexing.memory_dirs = [bar_dir]

        resp = await client.get("/api/index/stream", params={"path": str(barbaz_dir)})
        assert resp.status_code == 403, resp.text

    async def test_index_stream_matches_nfd_memory_dir_with_nfc_query(
        self, app, client: AsyncClient, tmp_path
    ):
        # Regression for #238: ``index_stream`` now NFC-normalizes both the
        # request path and each configured memory_dir before the
        # ``is_relative_to`` check, so an NFD-stored memory_dir matches an
        # NFC-typed query (mirrors the macOS/APFS Korean Drive case).
        nfd_dir = tmp_path / self._nfd("내 드라이브")
        app.state.config.indexing.memory_dirs = [nfd_dir]

        async def _fake_stream(*args, **kwargs):
            yield {"type": "complete", "indexed": 0}

        app.state.index_engine.index_path_stream = _fake_stream

        nfc_path = tmp_path / self._nfc("내 드라이브") / "subdir"
        resp = await client.get("/api/index/stream", params={"path": str(nfc_path)})
        # Without normalization the route would 403 here; the streaming
        # response itself is short-circuited by ``_fake_stream``.
        assert resp.status_code == 200, resp.text

    async def test_trigger_index_matches_nfd_memory_dir_with_nfc_query(
        self, app, client: AsyncClient, tmp_path
    ):
        # Reproducer for #238 (4): trigger_index uses Path.is_relative_to
        # after .resolve() on both sides. .resolve() does not Unicode-
        # normalize, so an NFD config entry vs an NFC user query yields
        # differing .parts and the is_relative_to check fails.
        nfd_dir = tmp_path / self._nfd("내 드라이브")
        nfd_dir.mkdir()
        app.state.config.indexing.memory_dirs = [nfd_dir]

        nfc_path = tmp_path / self._nfc("내 드라이브") / "subdir"
        resp = await client.post("/api/index", json={"path": str(nfc_path)})
        assert resp.status_code == 200, resp.text

    async def test_promote_scratch_matches_nfd_memory_dir_with_nfc_target(
        self, app, client: AsyncClient, tmp_path
    ):
        # Reproducer for #238 (5): promote_scratch mirrors trigger_index —
        # is_relative_to between resolved NFD base and resolved NFC target
        # fails on parts comparison.
        nfd_dir = tmp_path / self._nfd("내 드라이브")
        nfd_dir.mkdir()
        app.state.config.indexing.memory_dirs = [nfd_dir]

        app.state.storage.scratch_get = AsyncMock(
            return_value={"key": "note", "value": "promote me"}
        )
        app.state.storage.scratch_promote = AsyncMock()

        nfc_target = tmp_path / self._nfc("내 드라이브") / "today.md"
        with patch("memtomem.tools.memory_writer.append_entry"):
            resp = await client.post(
                "/api/scratch/note/promote",
                json={"file": str(nfc_target)},
            )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# GET /api/memory-dirs/status
# ---------------------------------------------------------------------------


class TestMemoryDirsStatus:
    """Per-dir index status shape contract. The Web UI groups entries by
    ``provider`` and ``category``, so both fields must be present on every
    row returned by :func:`~memtomem.indexing.engine.memory_dir_stats`.
    RFC #304 Phase 1."""

    async def test_response_shape_includes_provider(
        self, app, client: AsyncClient, tmp_path: Path
    ) -> None:
        # Mix of provider-shaped and user paths so the route output exercises
        # every category→provider branch in one call.
        user = tmp_path / "notes"
        codex = tmp_path / ".codex" / "memories"
        plans = tmp_path / ".claude" / "plans"
        claude_mem = tmp_path / ".claude" / "projects" / "demo" / "memory"
        for d in (user, codex, plans, claude_mem):
            d.mkdir(parents=True)

        app.state.config.indexing.memory_dirs = [user, codex, plans, claude_mem]

        resp = await client.get("/api/memory-dirs/status")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        dirs = data["dirs"]
        assert len(dirs) == 4
        # Every entry carries provider + category — Web UI consumes both.
        for entry in dirs:
            assert "category" in entry
            assert "provider" in entry
        by_path = {r["path"]: r for r in dirs}
        assert by_path[str(user)]["provider"] == "user"
        assert by_path[str(codex)]["provider"] == "openai"
        assert by_path[str(plans)]["provider"] == "claude"
        assert by_path[str(claude_mem)]["provider"] == "claude"
