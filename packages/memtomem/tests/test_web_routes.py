"""Tests for FastAPI web routes using httpx AsyncClient.

The web app is created by create_app() and dependencies are injected via
request.app.state.  We override app.state with mock/stub objects to avoid
full component initialization (embedding provider, SQLite, etc.).
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import os
import stat
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import IndexingConfig, SearchConfig
from memtomem.context.projects import KnownProjectsStore
from memtomem.models import Chunk, ChunkMetadata, IndexingStats, SearchResult
from memtomem.search.pipeline import RetrievalStats
from memtomem.web.app import create_app
from .helpers import set_home
from .web.test_upload_quarantine import (
    TestUploadQuarantineBoundaries,  # noqa: F401
    TestUploadQuarantineLifecycle,  # noqa: F401
    test_body_limit_without_content_length_exact_and_plus_one,  # noqa: F401
    test_upload_openapi_keeps_multipart_file_array_contract,  # noqa: F401
)


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
        onnx_batch_size = 8
        max_sequence_tokens = 1024
        onnx_cpu_mem_arena = False
        api_key = ""
        threads = 4

    class _Storage:
        backend = "sqlite"
        sqlite_path = Path("/tmp/test.db")
        collection_name = "memories"

    def __init__(self) -> None:
        # ``search`` and ``indexing`` are the *real* section models, not stubs:
        # ``PATCH /api/config`` re-validates the assembled section through
        # ``assign_section_fields`` (#2110), and a duck-typed double would skip
        # the cross-field invariants the route exists to enforce — a green test
        # for a code path production never takes. Values below match what these
        # tests assumed before; ``all_index_roots`` comes from the real model.
        #
        # Built per instance, not as class attributes: every other section here
        # is a class-level singleton, so a test that mutates one leaks into the
        # next (which is why the ``app`` fixture re-resets a few fields by
        # hand). A config PATCH mutates arbitrary fields of these two, so they
        # get a fresh model per ``FakeConfig()`` instead.
        self.search = SearchConfig(
            default_top_k=10,
            bm25_candidates=50,
            dense_candidates=50,
            rrf_k=60,
            enable_bm25=True,
            enable_dense=True,
            tokenizer="unicode61",
            rrf_weights=[1.0, 1.0],
        )
        self.indexing = IndexingConfig(
            memory_dirs=[Path("/tmp/memories")],
            project_memory_dirs=[],
            supported_extensions=frozenset({".md", ".json"}),
            exclude_patterns=[],
        )

    class _Decay:
        enabled = False
        half_life_days = 30.0

    class _MMR:
        enabled = False
        lambda_param = 0.7

    class _Rerank:
        enabled = False
        provider = "fastembed"
        model = "Xenova/ms-marco-MiniLM-L-6-v2"
        oversample = 2.0
        min_pool = 20
        max_pool = 200
        timeout_s = 30.0

    class _Namespace:
        default_namespace = "default"
        enable_auto_ns = False

    embedding = _Embedding()
    storage = _Storage()
    decay = _Decay()
    mmr = _MMR()
    rerank = _Rerank()
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
    storage.recall_chunks = AsyncMock(return_value=[_make_test_chunk()])
    storage.get_all_source_files = AsyncMock(return_value=[Path("/tmp/test.md")])
    storage.search_source_files_by_content = AsyncMock(return_value=[Path("/tmp/test.md")])
    storage.list_chunks_by_source = AsyncMock(return_value=[_make_test_chunk()])
    storage.count_chunks_by_source = AsyncMock(return_value=1)
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
    # Heuristic + AI summary mocks. Default to empty so most tests don't
    # have to reason about preview population — specific tests override.
    storage.get_source_summaries = AsyncMock(return_value={})
    storage.get_all_ai_summaries = AsyncMock(return_value={})
    storage.count_language_drift = AsyncMock(return_value=0)
    storage.list_language_drift_paths = AsyncMock(return_value=[])
    storage.set_ai_summary = AsyncMock()
    storage.delete_ai_summary = AsyncMock()
    storage.get_ai_summary = AsyncMock(return_value=None)
    storage.list_sessions = AsyncMock(return_value=[])
    storage.get_session_events = AsyncMock(return_value=[])
    storage.upsert_chunks = AsyncMock()
    storage.stored_embedding_info = None
    storage.embedding_mismatch = None
    # Real ``asyncio.Lock`` so service-layer ``async with storage._tag_write_lock:``
    # works against the AsyncMock storage. AsyncMock auto-attrs return
    # plain MagicMock children, which don't implement the async-context
    # protocol the tag-management service relies on.
    storage._tag_write_lock = asyncio.Lock()

    # -- embedder mock --
    embedder = AsyncMock()
    embedder.embed_texts = AsyncMock(return_value=[[0.1] * 768])
    embedder.embed_query = AsyncMock(return_value=[0.1] * 768)

    # -- search pipeline: real instance, mocked entry points --
    # Real SearchPipeline (not AsyncMock) because the PATCH handler drives
    # the real swap_reranker/lease machinery (#1777); search and
    # invalidate_cache stay mocks so route tests keep their call assertions.
    from memtomem.config import SearchConfig as _SearchConfig
    from memtomem.search.pipeline import SearchPipeline as _SearchPipeline

    search_pipeline = _SearchPipeline(
        storage=AsyncMock(),
        embedder=AsyncMock(),
        config=_SearchConfig(),
    )
    test_chunk = _make_test_chunk()
    result = SearchResult(chunk=test_chunk, score=0.95, rank=1, source="fused")
    rstats = RetrievalStats(bm25_candidates=10, dense_candidates=10, fused_total=1, final_total=1)
    search_pipeline.search = AsyncMock(return_value=([result], rstats))  # type: ignore[method-assign]
    search_pipeline.invalidate_cache = MagicMock()  # type: ignore[method-assign]

    # -- index engine mock --
    index_engine = AsyncMock()
    # ``mutated=True`` mirrors what an engine that indexed chunks really
    # returns; the index routes gate their cache invalidation on it (#2141).
    index_engine.index_path = AsyncMock(
        return_value=IndexingStats(
            total_files=1,
            total_chunks=2,
            indexed_chunks=2,
            skipped_chunks=0,
            deleted_chunks=0,
            duration_ms=100.0,
            mutated=True,
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
            mutated=True,
        )
    )
    # Sync helpers powering the preview-namespace route. Default to a
    # 1-file walk producing a single named NS — individual tests override
    # to exercise rule-variance / truncation / untagged paths.
    index_engine.discover_indexable_files = MagicMock(return_value=[Path("/tmp/memories/note.md")])
    index_engine.resolve_namespaces_for = AsyncMock(return_value=["notes"])

    # -- dedup scanner mock --
    dedup_scanner = AsyncMock()

    # Wire into app.state
    application.state.storage = storage
    application.state.embedder = embedder
    application.state.search_pipeline = search_pipeline
    application.state.index_engine = index_engine
    # Per-source AI summary endpoints look these up — default to no LLM
    # configured / no regen job running. Tests that exercise the
    # bulk-regenerate flow override ``app.state.llm`` directly.
    application.state.llm = None
    application.state.summary_regen = None
    cfg = FakeConfig()
    # ``cfg.indexing`` is built fresh per ``FakeConfig()`` (see its ``__init__``),
    # so exclude_patterns / memory_dirs no longer leak between tests — including
    # for any test that reassigns memory_dirs to exercise a custom corpus shape
    # (symlinked / tilde / nested / orphan cases), where a leaked override made
    # an unrelated downstream test fail the path-inside-memory_dirs gate
    # (e.g. ``/api/index`` 403s).
    application.state.config = cfg
    application.state.dedup_scanner = dedup_scanner
    application.state.project_root = Path.cwd()

    # Pin the hot-reload signature to the current on-disk state so these
    # FakeConfig-based tests don't get their state.config swapped out for a
    # real Mem2MemConfig built from ``~/.memtomem``. Dedicated hot-reload
    # tests live in tests/test_web_hot_reload.py where reload behavior is
    # exercised against a real tmp HOME.
    from memtomem.web import hot_reload as _hot_reload

    application.state.config_signature = _hot_reload.current_signature()
    application.state.last_reload_error = None

    # Override the ``mm init`` gate (issue #577): these tests use
    # FakeConfig + AsyncMock components, so the real
    # ``~/.memtomem/config.json`` predicate is irrelevant. Dedicated
    # require_configured tests live further down and exercise the
    # gate against a monkeypatched HOME.
    from memtomem.web.deps import require_configured

    application.dependency_overrides[require_configured] = lambda: None

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
    async def test_lifespan_free_readiness_and_missing_component_contract(self):
        bare_app = create_app(lifespan=None, mode="prod")
        async with AsyncClient(
            transport=ASGITransport(app=bare_app), base_url="http://test"
        ) as bare_client:
            ready = await bare_client.get("/api/readiness")
            assert ready.status_code == 503
            assert ready.json() == {
                "ready": False,
                "state": "not_started",
                "reason_code": "startup_unavailable",
            }
            stats = await bare_client.get("/api/stats")
            assert stats.status_code == 503
            assert stats.json() == {
                "detail": {
                    "reason_code": "startup_unavailable",
                    "message": "Web backend startup is unavailable.",
                }
            }
            assert (await bare_client.get("/api/health")).status_code == 200
            assert (await bare_client.get("/api/session")).status_code == 200
            assert (await bare_client.get("/api/config/defaults")).status_code == 200

    async def test_health_liveness_is_local_only(self, app, client: AsyncClient):
        resp = await client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "checks": {"process": "ok"}}
        app.state.storage.get_stats.assert_not_awaited()
        app.state.embedder.embed_texts.assert_not_awaited()

    async def test_active_health_degraded_when_storage_fails(self, app, client: AsyncClient):
        app.state.storage.get_stats.side_effect = RuntimeError("db down")
        resp = await client.post("/api/health")
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
            await client.post("/api/health")
        assert any("storage" in r.message for r in caplog.records)


class TestBootstrapState:
    async def test_unconfigured_state_is_available_without_bootstrap_gate(
        self, app, client: AsyncClient, tmp_path, monkeypatch
    ):
        set_home(monkeypatch, tmp_path)
        app.state.startup_state = "ready"

        resp = await client.get("/api/bootstrap")

        assert resp.status_code == 200
        assert resp.json()["stage"] == "unconfigured"
        assert resp.json()["configured"] is False

    async def test_ready_state_identifies_store_and_sources(
        self, app, client: AsyncClient, tmp_path, monkeypatch
    ):
        set_home(monkeypatch, tmp_path)
        config_path = tmp_path / ".memtomem" / "config.json"
        config_path.parent.mkdir()
        config_path.write_text("{}", encoding="utf-8")
        app.state.startup_state = "ready"

        resp = await client.get("/api/bootstrap")

        data = resp.json()
        assert data["stage"] == "ready"
        assert data["total_chunks"] == 42
        assert data["total_sources"] == 3
        assert data["db_path"] == str(Path(app.state.config.storage.sqlite_path).resolve())
        assert data["memory_dirs"]


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

    async def test_stats_returns_home_aggregates(self, app, client: AsyncClient, tmp_path):
        source_md = tmp_path / "notes.md"
        source_txt = tmp_path / "notes.txt"
        source_md.write_text("one")
        source_txt.write_text("two")
        app.state.storage.get_source_files_with_counts.return_value = [
            (
                source_md,
                1,
                "2026-01-01T00:00:00",
                "default",
                1,
                1,
                1,
            ),
            (
                source_txt,
                2,
                "2026-01-02T00:00:00",
                "default",
                2,
                2,
                2,
            ),
        ]

        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data["home_sources"], list)
        assert len(data["home_sources"]) == 2
        assert isinstance(data["home_file_type_distribution"], list)
        assert set(item["file_type"] for item in data["home_file_type_distribution"]) == {
            "md",
            "txt",
        }
        assert data["home_total_source_size"] == 6

    async def test_stats_home_aggregates_handle_many_sources(
        self, app, client: AsyncClient, tmp_path
    ):
        source_count = 120
        source_rows = []
        base_dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        for i in range(source_count):
            folder = "notes" if i % 2 == 0 else "docs"
            suffix = ".md" if i % 2 == 0 else ".txt"
            path = tmp_path / folder / f"doc-{i}{suffix}"
            path.parent.mkdir(parents=True, exist_ok=True)
            size = 150 + i
            path.write_bytes(b"x" * size)
            source_rows.append(
                (
                    path,
                    1,
                    (base_dt + timedelta(minutes=i)).isoformat(),
                    "default",
                    1,
                    1,
                    1,
                )
            )

        # Return sources in reverse order to prove aggregation is
        # independent of storage list ordering and that recent entries
        # are explicitly sorted by timestamp.
        app.state.storage.get_source_files_with_counts.return_value = list(reversed(source_rows))

        resp = await client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["home_sources"]) == source_count
        assert len(data["home_recent_sources"]) == 8
        # Newest source should be first regardless of input row order.
        assert data["home_recent_sources"][0]["path"] == str(tmp_path / "docs" / "doc-119.txt")

        dist = {item["file_type"]: item["count"] for item in data["home_file_type_distribution"]}
        assert dist["md"] == source_count // 2
        assert dist["txt"] == source_count // 2
        md_sizes = sum(p.stat().st_size for p in (tmp_path / "notes").glob("*.md"))
        txt_sizes = sum(p.stat().st_size for p in (tmp_path / "docs").glob("*.txt"))
        assert data["home_total_source_size"] == (md_sizes + txt_sizes)


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
        # ``embedding.threads`` exposed read-only so the Config tab can
        # render the ORT intra-op cap. Pinning the field's presence here
        # so a future schema trim doesn't silently re-hide it (#640
        # discoverability follow-up). Default 4 since the #640 follow-up
        # default flip — pre-flip the assertion was ``== 0``.
        assert "threads" in data["embedding"]
        assert data["embedding"]["threads"] == 4
        assert data["embedding"]["onnx_batch_size"] == 8
        assert data["embedding"]["max_sequence_tokens"] == 1024
        assert data["embedding"]["onnx_cpu_mem_arena"] is False
        assert "search" in data
        assert "indexing" in data
        assert "decay" in data
        assert "mmr" in data
        assert data["rerank"] == {
            "enabled": False,
            "provider": "fastembed",
            "model": "Xenova/ms-marco-MiniLM-L-6-v2",
            "oversample": 2.0,
            "min_pool": 20,
            "max_pool": 200,
            "timeout_s": 30.0,
        }
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
            "rerank",
            "namespace",
        }
        # Comparand values come through, not app.state.config values.
        assert data["embedding"]["onnx_cpu_mem_arena"] is False
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

    async def test_patch_rerank_runtime_fields(self, app, client: AsyncClient):
        """The Web config card can edit rerank runtime knobs but not restart knobs."""
        with (
            patch("memtomem.web.routes.system.save_config_overrides"),
            patch(
                "memtomem.web.routes.system._validate_reranker_ready",
                new_callable=AsyncMock,
            ),
        ):
            resp = await client.patch(
                "/api/config",
                json={"rerank": {"enabled": True, "oversample": 3.0, "provider": "cohere"}},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert any(c["field"] == "rerank.enabled" for c in data["applied"])
        assert any(c["field"] == "rerank.oversample" for c in data["applied"])
        assert "rerank.provider: read-only field" in data["rejected"]
        assert app.state.config.rerank.enabled is True
        assert app.state.config.rerank.oversample == 3.0
        assert app.state.config.rerank.provider == "fastembed"
        assert app.state.search_pipeline._reranker is not None
        assert app.state.search_pipeline._rerank_config is app.state.config.rerank

    async def test_patch_rerank_preserves_custom_timeout(self, app, client: AsyncClient):
        """timeout_s is not PATCH-mutable, but it must ride along in the
        RerankConfig reconstruction — patching oversample used to silently
        reset a custom timeout back to the default."""
        app.state.config.rerank.timeout_s = 7.5
        with (
            patch("memtomem.web.routes.system.save_config_overrides"),
            patch(
                "memtomem.web.routes.system._validate_reranker_ready",
                new_callable=AsyncMock,
            ),
        ):
            resp = await client.patch(
                "/api/config",
                json={"rerank": {"enabled": True, "oversample": 3.0}},
            )

        assert resp.status_code == 200
        assert app.state.config.rerank.oversample == 3.0
        assert app.state.config.rerank.timeout_s == 7.5

    async def test_patch_rerank_rejects_lazy_load_failure(self, app, client: AsyncClient):
        """Runtime enabling must fail if the lazy reranker cannot load."""

        class BrokenReranker:
            def __init__(self):
                self.closed = False

            def _get_model(self):
                raise ImportError("fastembed is required")

            async def close(self):
                self.closed = True

        reranker = BrokenReranker()

        with (
            patch("memtomem.web.routes.system.create_reranker", return_value=reranker),
            patch("memtomem.web.routes.system.save_config_overrides"),
        ):
            resp = await client.patch("/api/config", json={"rerank": {"enabled": True}})

        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] == []
        assert "rerank.enabled: fastembed is required" in data["rejected"]
        assert app.state.config.rerank.enabled is False
        assert app.state.search_pipeline._reranker is None
        assert app.state.search_pipeline._rerank_config is None
        assert reranker.closed is True

    async def test_patch_rerank_disable_syncs_pipeline(self, app, client: AsyncClient):
        class StubReranker:
            def __init__(self):
                self.closed = False

            async def close(self):
                self.closed = True

        reranker = StubReranker()
        app.state.config.rerank.enabled = True
        app.state.search_pipeline._reranker = reranker
        app.state.search_pipeline._rerank_config = app.state.config.rerank

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.patch("/api/config", json={"rerank": {"enabled": False}})

        assert resp.status_code == 200
        assert resp.json()["rejected"] == []
        assert app.state.config.rerank.enabled is False
        assert app.state.search_pipeline._reranker is None
        assert app.state.search_pipeline._rerank_config is None
        assert reranker.closed is True

    async def test_patch_rerank_disable_defers_close_while_search_in_flight(
        self, app, client: AsyncClient
    ):
        """#1777, PATCH-path parity: disabling rerank while a search leases
        the reranker must publish the disable immediately but defer the
        close to the lease release. The lease is held exactly the way a real
        ``search()`` holds it (pinned in test_pipeline.py — the fixture's
        ``search`` is a mock, so the lease is taken directly here)."""

        class StubReranker:
            def __init__(self):
                self.close_calls = 0

            async def close(self):
                self.close_calls += 1

        pipeline = app.state.search_pipeline
        reranker = StubReranker()
        app.state.config.rerank.enabled = True
        pipeline._reranker = reranker
        pipeline._rerank_config = app.state.config.rerank

        with pipeline._lease_reranker() as (leased, _):
            assert leased is reranker
            with patch("memtomem.web.routes.system.save_config_overrides"):
                resp = await client.patch("/api/config", json={"rerank": {"enabled": False}})

            assert resp.status_code == 200
            assert resp.json()["rejected"] == []
            assert pipeline._reranker is None  # disable published immediately
            assert reranker.close_calls == 0  # leased: close deferred

        if pipeline._bg_tasks:
            await asyncio.gather(*pipeline._bg_tasks)
        assert reranker.close_calls == 1

    async def test_patch_search_rejects_rrf_weights_fusion_cannot_honor(
        self, app, client: AsyncClient
    ):
        """#2094: negative / non-finite / all-zero pairs land in ``rejected``
        and the running config keeps its value; a valid pair applies."""
        with patch("memtomem.web.routes.system.save_config_overrides"):
            for bad in ([-1.0, 1.0], [0.0, 0.0]):
                resp = await client.patch("/api/config", json={"search": {"rrf_weights": bad}})
                assert resp.status_code == 200
                data = resp.json()
                assert data["applied"] == []
                assert any("rrf_weights" in r for r in data["rejected"])
                assert app.state.config.search.rrf_weights == [1.0, 1.0]

            resp = await client.patch("/api/config", json={"search": {"rrf_weights": [0.0, 1.0]}})
            assert resp.status_code == 200
            assert resp.json()["rejected"] == []
            assert app.state.config.search.rrf_weights == [0.0, 1.0]

    async def test_patch_rerank_rejects_invalid_pool_bounds(self, app, client: AsyncClient):
        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.patch(
                "/api/config",
                json={"rerank": {"min_pool": 500, "max_pool": 20}},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] == []
        assert any("rerank.max_pool" in r for r in data["rejected"])
        assert app.state.config.rerank.min_pool == 20
        assert app.state.config.rerank.max_pool == 200
        assert app.state.search_pipeline._reranker is None
        assert app.state.search_pipeline._rerank_config is None

    async def test_patch_rejects_cross_field_invalid_section(self, app, client: AsyncClient):
        """#2110: a combination the section validator rejects never reaches disk.

        ``setattr`` skips the section's ``model_validator(mode="after")``, so
        this used to be persisted and then dropped by every later load.
        """
        with patch("memtomem.web.routes.system.save_config_overrides") as save:
            resp = await client.patch(
                "/api/config?persist=true",
                json={"indexing": {"max_chunk_tokens": 64}},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["applied"] == []
        assert any("must be <= max_chunk_tokens" in r for r in data["rejected"])
        assert app.state.config.indexing.max_chunk_tokens == 512
        # Nothing was accepted, so nothing is written at all — the rejected
        # combination never reaches config.json, and neither does a
        # rewrite-for-nothing of the operator's file.
        save.assert_not_called()

    async def test_patch_applies_a_pair_that_is_legal_only_together(self, app, client: AsyncClient):
        """The settings card sends every dirty field of a section at once, so
        the update is validated as a whole: lowering ``max_chunk_tokens`` to 128
        is invalid against the *old* ``target_chunk_tokens`` of 384 and valid
        against the new one sent alongside it (#2110)."""
        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.patch(
                "/api/config",
                json={"indexing": {"max_chunk_tokens": 128, "target_chunk_tokens": 100}},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["rejected"] == []
        assert {c["field"] for c in data["applied"]} == {
            "indexing.max_chunk_tokens",
            "indexing.target_chunk_tokens",
        }
        assert app.state.config.indexing.max_chunk_tokens == 128
        assert app.state.config.indexing.target_chunk_tokens == 100

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
# GET /api/privacy/patterns (issue #580)
# ---------------------------------------------------------------------------


class TestPrivacyPatterns:
    """The Web UI compose-mode privacy warning fetches LTM secret
    patterns from this endpoint and runs them client-side against the
    textarea before submission. The endpoint is read-only metadata —
    no ``require_configured`` gate, mirroring ``/api/config`` and
    ``/api/indexing/builtin-exclude-patterns``."""

    async def test_returns_documented_shape(self, client: AsyncClient):
        from memtomem import privacy

        resp = await client.get("/api/privacy/patterns")
        assert resp.status_code == 200
        data = resp.json()
        assert set(data.keys()) == {"patterns", "sha"}

        assert isinstance(data["sha"], str)
        assert len(data["sha"]) == 64
        assert all(c in "0123456789abcdef" for c in data["sha"])

        assert isinstance(data["patterns"], list)
        assert len(data["patterns"]) == len(privacy.DEFAULT_PATTERNS)
        # Each entry's flags is a (possibly empty) string of distinct
        # chars from the JS-compatible subset the translator emits.
        # ``g`` (global) and ``y`` (sticky) are JS-only — the lifter
        # never produces them; ``x`` (verbose) is hard-rejected.
        allowed = set("imsu")
        for entry in data["patterns"]:
            assert set(entry.keys()) == {"pattern", "flags"}
            assert isinstance(entry["pattern"], str) and entry["pattern"]
            flags = entry["flags"]
            assert isinstance(flags, str)
            assert len(flags) == len(set(flags)), (
                f"duplicate flag char in {flags!r} — JS rejects new RegExp(body, 'ii')"
            )
            assert set(flags) <= allowed, (
                f"unexpected flag in {flags!r}; allowed: {sorted(allowed)}"
            )

    async def test_patterns_match_translator_over_default_set(self, client: AsyncClient):
        """Drift guard: the wire patterns must equal what
        ``to_js_pattern`` produces for the live ``DEFAULT_PATTERNS``.
        If anyone touches the source tuple without re-deriving the JS
        view, this fails."""
        from memtomem import privacy

        resp = await client.get("/api/privacy/patterns")
        wire = resp.json()["patterns"]
        derived = [
            {"pattern": body, "flags": flags}
            for body, flags in (privacy.to_js_pattern(p) for p in privacy.DEFAULT_PATTERNS)
        ]
        assert wire == derived

    async def test_sha_locks_serialization_choice(self, client: AsyncClient):
        """SHA is computed from the live ``JS_PATTERNS`` using a
        canonical JSON encoding (sort_keys=True + tight separators).
        Locks *serialization* only — adding a 10th pattern would fail
        the parity test above, not this one."""
        import hashlib
        import json

        from memtomem import privacy

        resp = await client.get("/api/privacy/patterns")
        expected = hashlib.sha256(
            json.dumps(
                privacy.JS_PATTERNS,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assert resp.json()["sha"] == expected

    async def test_no_require_configured_gate(
        self,
        app,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Read-only metadata endpoint — must serve patterns even when
        ``~/.memtomem/config.json`` is absent. Mirrors ``/api/config``
        (also unguarded). Verified by *restoring* the real gate
        (the shared ``app`` fixture stubs it to ``lambda: None`` so
        all unrelated tests don't depend on the developer's real
        config) and pointing HOME at an empty tmpdir — if the gate
        had crept onto the route, this would 409."""
        from memtomem.web.deps import require_configured

        del app.dependency_overrides[require_configured]
        set_home(monkeypatch, tmp_path)

        resp = await client.get("/api/privacy/patterns")
        assert resp.status_code == 200, resp.text
        assert "patterns" in resp.json()


# ---------------------------------------------------------------------------
# GET /api/privacy/stats (ADR-0006 PR-B audit surface)
# ---------------------------------------------------------------------------


class TestPrivacyStats:
    """GUI view of ``privacy.snapshot()`` — the process-lifetime redaction
    counters the Settings → Redaction panel renders (same tally the MCP
    ``mem_add_redaction_stats`` tool surfaces). Read-only metadata, no
    ``require_configured`` gate, mirroring ``/api/privacy/patterns``."""

    @pytest.fixture(autouse=True)
    def _reset_privacy(self):
        from memtomem import privacy

        privacy.reset_for_tests()
        yield
        privacy.reset_for_tests()

    async def test_returns_snapshot_shape_zeroed(self, client: AsyncClient):
        resp = await client.get("/api/privacy/stats")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert set(data.keys()) == {"outcomes", "by_tool"}
        # All five outcome counters present and zero after reset; by_tool is a
        # defaultdict populated only on record(), so it starts empty.
        assert set(data["outcomes"]) == {
            "blocked",
            "pass",
            "bypassed",
            "blocked_project_shared",
            "exempted",
        }
        assert all(v == 0 for v in data["outcomes"].values())
        assert data["by_tool"] == {}

    async def test_reflects_recorded_outcomes(self, client: AsyncClient):
        from memtomem import privacy

        privacy.record("blocked", "index")
        privacy.record("bypassed", "index")
        privacy.record("pass", "mem_add")

        resp = await client.get("/api/privacy/stats")
        data = resp.json()
        assert data["outcomes"]["blocked"] == 1
        assert data["outcomes"]["bypassed"] == 1
        assert data["outcomes"]["pass"] == 1
        assert data["by_tool"]["index"] == {
            "blocked": 1,
            "pass": 0,
            "bypassed": 1,
            "blocked_project_shared": 0,
            "exempted": 0,
        }
        assert data["by_tool"]["mem_add"]["pass"] == 1


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

    async def test_search_returns_durable_query_run_id(self, app, client: AsyncClient):
        run_id = "e38ab6c7-4db4-4d68-8dca-93c1da2dcfe6"
        app.state.search_pipeline.search.return_value[1].query_run_id = run_id

        resp = await client.get("/api/search", params={"q": "hello world"})

        assert resp.status_code == 200
        assert resp.json()["query_run_id"] == run_id
        assert resp.json()["retrieval_stats"]["query_run_id"] == run_id

    async def test_search_no_axis_returns_400(self, client: AsyncClient):
        """#750: ``q`` is now optional, but at least one of
        ``q``/``tag_filter``/``source_filter`` must be present — search
        needs *something* to scope by. A missing-everything call is
        still rejected, just with a 400 + actionable detail rather than
        FastAPI's default 422 for missing required params."""
        resp = await client.get("/api/search")
        assert resp.status_code == 400
        detail = resp.json().get("detail", "")
        assert "tag_filter" in detail and "source_filter" in detail

    async def test_search_tag_only_returns_results(self, client: AsyncClient):
        """#750: ``tag_filter`` alone is a valid axis — the pipeline's
        empty-query branch enumerates by filter and returns results
        without needing a keyword."""
        resp = await client.get("/api/search", params={"tag_filter": "redis"})
        assert resp.status_code == 200

    async def test_search_with_filters(self, client: AsyncClient):
        resp = await client.get(
            "/api/search",
            params={"q": "test", "top_k": 5, "namespace": "work"},
        )
        assert resp.status_code == 200

    async def test_search_threads_repeatable_exact_metadata_filters(self, app, client: AsyncClient):
        app.state.search_pipeline.search.reset_mock()
        resp = await client.get(
            "/api/search",
            params=[
                ("source_exact", "/tmp/a,comma.md"),
                ("source_exact", "/tmp/b.md"),
                ("chunk_type", "markdown_section"),
                ("created_from", "2026-07-01T00:00:00+09:00"),
                ("created_before", "2026-07-13T00:00:00+09:00"),
            ],
        )
        assert resp.status_code == 200, resp.text
        kwargs = app.state.search_pipeline.search.await_args.kwargs
        assert kwargs["source_exact"] == ["/tmp/a,comma.md", "/tmp/b.md"]
        assert kwargs["chunk_types"] == ["markdown_section"]
        assert kwargs["created_from"].tzinfo is not None
        assert kwargs["created_before"] > kwargs["created_from"]
        assert kwargs["origin"] == "web"

    async def test_search_rejects_a_namespace_mixing_a_comma_list_with_a_glob(
        self, client: AsyncClient
    ):
        """A filter the caller spelled wrong is a request problem. The search
        call below the guard is wrapped in ``except Exception`` -> 500, so
        parsing has to happen ahead of it or a bad namespace reads as a
        server fault."""
        resp = await client.get("/api/search", params={"q": "test", "namespace": "archive:*,work"})

        assert resp.status_code == 422, resp.text
        assert "archive:*,work" in resp.json().get("detail", "")

    async def test_search_rejects_naive_or_reversed_date_bounds(self, client: AsyncClient):
        naive = await client.get("/api/search", params={"created_from": "2026-07-01T00:00:00"})
        assert naive.status_code == 422
        reversed_range = await client.get(
            "/api/search",
            params={
                "created_from": "2026-07-02T00:00:00Z",
                "created_before": "2026-07-01T00:00:00Z",
            },
        )
        assert reversed_range.status_code == 422

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
        # ``kind`` / ``memory_dir`` are always present so the Web UI's
        # Sources-mode toggle can partition without re-deriving anything.
        assert "kind" in src
        assert "memory_dir" in src

    async def test_list_sources_pagination(self, client: AsyncClient):
        resp = await client.get("/api/sources", params={"limit": 1, "offset": 0})
        assert resp.status_code == 200
        data = resp.json()
        assert data["limit"] == 1
        assert data["offset"] == 0

    async def test_source_content_matches_returns_matching_paths(self, app, client: AsyncClient):
        body_match = Path("/tmp/body-match.md")
        app.state.storage.search_source_files_by_content.return_value = [body_match]

        resp = await client.get("/api/sources/content-matches", params={"q": "needle"})

        assert resp.status_code == 200
        assert resp.json() == {"query": "needle", "paths": [str(body_match)]}
        app.state.storage.search_source_files_by_content.assert_awaited_once_with(
            "needle",
            limit=10000,
        )

    async def test_source_content_matches_includes_ai_summary_text(self, app, client: AsyncClient):
        summary_match = Path("/tmp/summary-match.md")
        app.state.storage.search_source_files_by_content.return_value = []
        app.state.storage.get_all_ai_summaries.return_value = {
            str(summary_match): {
                "summary": "감사 로그 정책을 설명하는 문서입니다.",
                "language": "ko",
            }
        }

        resp = await client.get("/api/sources/content-matches", params={"q": "감사"})

        assert resp.status_code == 200
        assert resp.json()["paths"] == [str(summary_match)]

    async def test_source_content_matches_hides_project_local_by_default(
        self, app, client: AsyncClient
    ):
        project_root = Path("/tmp/project")
        local_dir = project_root / ".memtomem" / "memories.local"
        local_path = local_dir / "draft.md"
        user_path = Path("/tmp/memories/public.md")
        app.state.config.indexing.project_memory_dirs = [local_dir]
        app.state.storage.search_source_files_by_content.return_value = [local_path, user_path]
        app.state.storage.get_all_ai_summaries.return_value = {
            str(local_path): {"summary": "needle draft", "language": "en"},
            str(user_path): {"summary": "needle public", "language": "en"},
        }

        resp = await client.get("/api/sources/content-matches", params={"q": "needle"})

        assert resp.status_code == 200
        assert resp.json()["paths"] == [str(user_path)]

    async def test_source_content_matches_can_filter_to_project_local(
        self, app, client: AsyncClient
    ):
        project_root = Path("/tmp/project")
        local_dir = project_root / ".memtomem" / "memories.local"
        local_path = local_dir / "draft.md"
        user_path = Path("/tmp/memories/public.md")
        app.state.config.indexing.project_memory_dirs = [local_dir]
        app.state.storage.search_source_files_by_content.return_value = [local_path, user_path]

        resp = await client.get(
            "/api/sources/content-matches",
            params={"q": "needle", "target_scope": "project_local"},
        )

        assert resp.status_code == 200
        assert resp.json()["paths"] == [str(local_path)]

    async def test_source_content_matches_rejects_blank_query(self, client: AsyncClient):
        resp = await client.get("/api/sources/content-matches", params={"q": "   "})

        assert resp.status_code == 400
        assert resp.json()["detail"] == "Query must not be blank."

    async def test_orphan_source_kind_is_null(self, app, client: AsyncClient):
        """Indexed sources whose owning dir is no longer in
        ``memory_dirs`` are orphans — they must surface with
        ``kind=null`` / ``memory_dir=null`` so the Web UI can show them
        in the General view rather than dropping them entirely. This
        is the most error-prone path because the natural code shape is
        to filter them out."""
        # Default fixture: source ``/tmp/test.md`` is NOT under any
        # configured memory_dir (only ``/tmp/memories`` is registered).
        resp = await client.get("/api/sources")
        assert resp.status_code == 200
        src = resp.json()["sources"][0]
        assert src["kind"] is None
        assert src["memory_dir"] is None

    async def test_kind_memory_filter_excludes_orphans(self, app, client: AsyncClient):
        """``?kind=memory`` is the strict filter — orphans (``kind=null``)
        are excluded so the Memory view only shows sources the user
        explicitly registered as memory. Pin the asymmetry against the
        General filter."""
        resp = await client.get("/api/sources", params={"kind": "memory"})
        assert resp.status_code == 200
        # Default fixture's lone source is orphan → empty under
        # ``kind=memory``.
        assert resp.json()["total"] == 0

    async def test_kind_general_filter_includes_orphans(self, app, client: AsyncClient):
        """``?kind=general`` is the catch-all that surfaces orphans.
        Without this contract, users who removed a memory_dir without
        purging chunks would lose the ability to find them in the UI
        until the underlying files were re-registered or deleted."""
        resp = await client.get("/api/sources", params={"kind": "general"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["sources"][0]["kind"] is None

    async def test_kind_set_when_source_under_memory_dir(self, app, client: AsyncClient):
        """Sources whose owning dir is registered carry a concrete
        ``kind``. Use a path under the existing ``/tmp/memories`` dir
        (which classifies as ``memory`` thanks to the ``memories``
        segment) so the kind/memory_dir wiring is end-to-end exercised."""
        app.state.storage.get_source_files_with_counts.return_value = [
            (
                Path("/tmp/memories/note.md"),
                3,
                "2026-04-29T10:00:00",
                "default",
                100,
                50,
                200,
            )
        ]
        resp = await client.get("/api/sources")
        assert resp.status_code == 200
        src = resp.json()["sources"][0]
        assert src["kind"] == "memory"
        assert src["memory_dir"] == str(Path("/tmp/memories").resolve())

        # Same source must round-trip through the kind=memory filter and
        # be excluded by kind=general.
        resp_mem = await client.get("/api/sources", params={"kind": "memory"})
        assert resp_mem.json()["total"] == 1
        resp_gen = await client.get("/api/sources", params={"kind": "general"})
        assert resp_gen.json()["total"] == 0

    async def test_memory_dir_resolves_symlink(
        self, app, client: AsyncClient, tmp_path: Path
    ) -> None:
        """``memory_dir`` in the response is resolved (not just expanded)
        — same treatment ``/api/memory-dirs/status`` got in #668. A
        wizard-written config under a symlinked prefix (macOS ``/tmp`` →
        ``/private/tmp``, Docker bind mounts) would otherwise emit the
        raw form here while the status endpoint emits the resolved form,
        breaking the frontend's ``STATE.memoryStatusByPath[source.memory_dir]``
        lookup. (#675)"""
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        app.state.config.indexing.memory_dirs = [link]
        # Storage returns source paths in their resolved form (chunks
        # table is canonicalised via ``norm_path``), so the source lives
        # under ``real`` even though the config still names ``link``.
        source_file = real / "note.md"
        app.state.storage.get_source_files_with_counts.return_value = [
            (source_file, 3, "2026-04-29T10:00:00", "default", 100, 50, 200)
        ]

        resp = await client.get("/api/sources")
        assert resp.status_code == 200
        src = resp.json()["sources"][0]
        assert src["memory_dir"] == str(real.resolve())

    async def test_memory_dir_matches_status_endpoint(
        self, app, client: AsyncClient, tmp_path: Path
    ) -> None:
        """Cross-endpoint parity guard. ``/api/sources`` ``memory_dir``
        and ``/api/memory-dirs/status`` ``path`` are both consumed by
        the same frontend render pass — a divergence here re-introduces
        #675 with the same symptoms (vendor inference falls through and
        sources land under whichever sub-tab is active). Pin the
        invariant directly so the regression doesn't have to surface
        through the UI again."""
        real = tmp_path / "x"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        app.state.config.indexing.memory_dirs = [link]
        source_file = real / "note.md"
        app.state.storage.get_source_files_with_counts.return_value = [
            (source_file, 1, "2026-04-29T10:00:00", "default", 100, 50, 200)
        ]

        sources_resp = await client.get("/api/sources")
        status_resp = await client.get("/api/memory-dirs/status")
        assert sources_resp.status_code == 200, sources_resp.text
        assert status_resp.status_code == 200, status_resp.text

        sources = sources_resp.json()["sources"]
        dirs = status_resp.json()["dirs"]
        assert len(sources) == 1
        assert len(dirs) == 1
        assert sources[0]["memory_dir"] == dirs[0]["path"]

    async def test_memory_dir_resolves_tilde(
        self,
        app,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Symmetric counterpart to ``test_response_path_resolves_tilde``
        in :class:`TestMemoryDirsStatus`. A config entry like
        ``~/memories`` must come back through ``/api/sources`` as the
        expanded absolute path, not the literal tilde form (#675)."""
        set_home(monkeypatch, tmp_path)
        target = tmp_path / "memories"
        target.mkdir()

        app.state.config.indexing.memory_dirs = ["~/memories"]
        source_file = target / "note.md"
        app.state.storage.get_source_files_with_counts.return_value = [
            (source_file, 1, "2026-04-29T10:00:00", "default", 100, 50, 200)
        ]

        resp = await client.get("/api/sources")
        assert resp.status_code == 200
        src = resp.json()["sources"][0]
        assert src["memory_dir"] == str(target.resolve())

    # ---- heuristic preview --------------------------------------------------
    #
    # The route resolves each source's preview/AI-summary via
    # ``str(p) -> dict.get(...)``, where ``p`` comes from the
    # ``get_source_files_with_counts`` mock (``Path("/tmp/test.md")``).
    # On Windows ``str(Path("/tmp/test.md")) == "\\tmp\\test.md"`` —
    # using a bare POSIX literal as the dict key here would silently
    # miss on Windows runners (the failure mode shipped in PR #888 CI).
    # Build the key from ``str(Path(...))`` so the test rides on the
    # same normalisation the route uses.

    async def test_summary_derived_from_first_chunk(self, app, client: AsyncClient):
        """Title strips the leading ``#`` from
        ``heading_hierarchy[0]``, and excerpt comes from the first
        chunk's body. Pin both so a future refactor can't silently
        regress what users see in the Source tab without flipping a test."""
        key = str(Path("/tmp/test.md"))
        app.state.storage.get_source_summaries.return_value = {
            key: (
                ["# Project Notes", "## Section"],
                "Opening lines of the document.",
            )
        }
        resp = await client.get("/api/sources")
        src = resp.json()["sources"][0]
        assert src["title"] == "Project Notes"
        assert src["excerpt"] == "Opening lines of the document."

    async def test_summary_excerpt_truncated_with_ellipsis(self, app, client: AsyncClient):
        """Excerpt caps at ~200 chars with a trailing ``…`` so a
        runaway opening paragraph can't blow out the row layout."""
        long_body = "word " * 200  # ~1000 chars
        key = str(Path("/tmp/test.md"))
        app.state.storage.get_source_summaries.return_value = {key: (["# Title"], long_body)}
        resp = await client.get("/api/sources")
        src = resp.json()["sources"][0]
        assert src["excerpt"] is not None
        assert src["excerpt"].endswith("…")
        assert len(src["excerpt"]) <= 200

    async def test_summary_absent_yields_null_fields(self, app, client: AsyncClient):
        """No first-chunk row → both heuristic fields are ``None``.
        Default fixture exercises this; pin so an "always populate"
        change can't sneak through."""
        resp = await client.get("/api/sources")
        src = resp.json()["sources"][0]
        assert src["title"] is None
        assert src["excerpt"] is None

    # ---- AI summary in response --------------------------------------------

    async def test_ai_summary_included_when_meta_present(self, app, client: AsyncClient):
        """When ``get_all_ai_summaries`` returns a record, the response
        carries both ``ai_summary`` text and ``ai_summary_language`` so
        the UI can flag drift."""
        key = str(Path("/tmp/test.md"))
        app.state.storage.get_all_ai_summaries.return_value = {
            key: {
                "summary": "AI-generated 2-sentence prose.",
                "signature": "abc123",
                "language": "en",
                "generated_at": "2026-01-01T00:00:00Z",
            }
        }
        resp = await client.get("/api/sources")
        src = resp.json()["sources"][0]
        assert src["ai_summary"] == "AI-generated 2-sentence prose."
        assert src["ai_summary_language"] == "en"

    async def test_ai_summary_absent_when_no_record(self, app, client: AsyncClient):
        resp = await client.get("/api/sources")
        src = resp.json()["sources"][0]
        assert src["ai_summary"] is None
        assert src["ai_summary_language"] is None

    # ---- language-drift banner ---------------------------------------------

    async def test_language_drift_present_when_record_language_differs(
        self, app, client: AsyncClient
    ):
        """Cached summary in ``en`` while config is ``ko`` → response
        carries ``language_drift`` with count + setting. Banner UX
        relies on this conditional being non-null only when there's
        actual drift."""
        # Drift count iterates ``ai_summaries.values()``, so the dict
        # key choice is incidental to this assertion — but we still go
        # through ``str(Path(...))`` so a future refactor that *does*
        # key off the path doesn't reintroduce the Windows failure.
        key = str(Path("/tmp/test.md"))
        app.state.config.indexing.summary_language = "ko"
        app.state.storage.get_all_ai_summaries.return_value = {
            key: {
                "summary": "x",
                "signature": "s",
                "language": "en",
                "generated_at": "t",
            }
        }
        resp = await client.get("/api/sources")
        data = resp.json()
        assert data["language_drift"] is not None
        assert data["language_drift"]["count"] == 1
        assert data["language_drift"]["current_setting"] == "ko"

    async def test_language_drift_absent_when_all_records_match(self, app, client: AsyncClient):
        key = str(Path("/tmp/test.md"))
        app.state.config.indexing.summary_language = "ko"
        app.state.storage.get_all_ai_summaries.return_value = {
            key: {
                "summary": "x",
                "signature": "s",
                "language": "ko",
                "generated_at": "t",
            }
        }
        resp = await client.get("/api/sources")
        data = resp.json()
        assert data["language_drift"] is None

    async def test_language_drift_absent_when_no_summaries_cached(self, app, client: AsyncClient):
        """Default fixture (empty ai_summaries) → no drift banner.
        Without this, the UI would render an empty count banner."""
        resp = await client.get("/api/sources")
        data = resp.json()
        assert data["language_drift"] is None

    # ---- canonical-residency tier (ADR-0016 §7 — #924) ----------------------
    #
    # Sources surface a per-row ``target_scope`` so the SPA can render a
    # tier badge without rebuilding the classification client-side. The
    # route also honors ``?target_scope=`` — omitting it hides
    # ``project_local`` rows per ADR-0015 §4a.

    async def test_user_tier_default_when_path_outside_project_dirs(self, app, client: AsyncClient):
        """Any source path that isn't under a registered
        ``project_memory_dir`` classifies as ``user`` — same fallback the
        indexer applies at config.py:1495-1507. Default fixture's
        ``/tmp/test.md`` lives outside the project memory tree, so the
        badge token must be the user-tier default."""
        resp = await client.get("/api/sources")
        src = resp.json()["sources"][0]
        assert src["target_scope"] == "user"

    async def test_project_shared_tier_classified_from_path(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """A source under a registered ``project_memory_dirs`` shared
        directory classifies as ``project_shared``. Pin the
        path-pattern resolution end-to-end so a regression in
        ``classify_scope`` (or in the route's wire-up) surfaces here
        rather than as a silently-wrong tier badge."""
        proj_root = tmp_path / "proj"
        shared_dir = proj_root / ".memtomem" / "memories"
        shared_dir.mkdir(parents=True)
        source = shared_dir / "note.md"
        source.touch()

        app.state.config.indexing.memory_dirs = []
        app.state.config.indexing.project_memory_dirs = [shared_dir]
        app.state.storage.get_source_files_with_counts.return_value = [
            (source, 2, "2026-04-29T10:00:00", "default", 100, 50, 200),
        ]

        resp = await client.get("/api/sources")
        src = resp.json()["sources"][0]
        assert src["target_scope"] == "project_shared"

    async def test_project_local_hidden_by_default(self, app, client: AsyncClient, tmp_path: Path):
        """ADR-0015 §4a — ``project_local`` sources are hidden in
        overview / list views unless explicitly requested. Pin the
        default-omit behavior; the only way to surface this tier is
        ``?target_scope=project_local``.
        """
        proj_root = tmp_path / "proj"
        local_dir = proj_root / ".memtomem" / "memories.local"
        local_dir.mkdir(parents=True)
        source = local_dir / "draft.md"
        source.touch()

        app.state.config.indexing.memory_dirs = []
        app.state.config.indexing.project_memory_dirs = [local_dir]
        app.state.storage.get_source_files_with_counts.return_value = [
            (source, 1, "2026-04-29T10:00:00", "default", 100, 50, 200),
        ]

        resp = await client.get("/api/sources")
        assert resp.json()["total"] == 0, (
            "project_local sources must be hidden when ?target_scope= is omitted"
        )

    async def test_project_local_visible_with_explicit_filter(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """Symmetric pin against the default-hidden case — passing
        ``?target_scope=project_local`` is the only way to surface
        these rows. Without this test the previous case could pass
        vacuously even if the filter never actually narrowed to the
        local tier."""
        proj_root = tmp_path / "proj"
        local_dir = proj_root / ".memtomem" / "memories.local"
        local_dir.mkdir(parents=True)
        source = local_dir / "draft.md"
        source.touch()

        app.state.config.indexing.memory_dirs = []
        app.state.config.indexing.project_memory_dirs = [local_dir]
        app.state.storage.get_source_files_with_counts.return_value = [
            (source, 1, "2026-04-29T10:00:00", "default", 100, 50, 200),
        ]

        resp = await client.get("/api/sources", params={"target_scope": "project_local"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1
        assert data["sources"][0]["target_scope"] == "project_local"

    async def test_target_scope_filter_narrows_to_one_tier(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """``?target_scope=user`` excludes a registered project_shared
        source even though it would otherwise pass the default filter.
        Pins that the filter is *narrow-to-one* (not *omit-only-local*)."""
        proj_root = tmp_path / "proj"
        shared_dir = proj_root / ".memtomem" / "memories"
        shared_dir.mkdir(parents=True)
        shared_src = shared_dir / "note.md"
        shared_src.touch()
        user_src = tmp_path / "user.md"
        user_src.touch()

        app.state.config.indexing.memory_dirs = [tmp_path]
        app.state.config.indexing.project_memory_dirs = [shared_dir]
        app.state.storage.get_source_files_with_counts.return_value = [
            (shared_src, 2, "2026-04-29T10:00:00", "default", 100, 50, 200),
            (user_src, 1, "2026-04-29T10:00:00", "default", 100, 50, 200),
        ]

        resp_user = await client.get("/api/sources", params={"target_scope": "user"})
        rows = resp_user.json()["sources"]
        assert len(rows) == 1
        assert rows[0]["target_scope"] == "user"

        # Symmetric narrow: ``project_shared`` excludes the user-tier row.
        resp_shared = await client.get("/api/sources", params={"target_scope": "project_shared"})
        rows_shared = resp_shared.json()["sources"]
        assert len(rows_shared) == 1
        assert rows_shared[0]["target_scope"] == "project_shared"

    async def test_invalid_target_scope_returns_422(self, client: AsyncClient):
        """Literal validation refuses unknown tier tokens at the query
        layer too — same guardrail as ``/api/add``."""
        resp = await client.get("/api/sources", params={"target_scope": "draft"})
        assert resp.status_code == 422

    # ---- regenerate endpoints ----------------------------------------------

    async def test_regenerate_summaries_rejected_when_disabled(self, app, client: AsyncClient):
        """``auto_summarize=False`` → 400. Defense-in-depth: the UI
        gates the button anyway, but a direct API client must get a
        clear error."""
        app.state.config.indexing.auto_summarize = False
        resp = await client.post("/api/sources/regenerate-summaries")
        assert resp.status_code == 400
        assert "auto_summarize" in resp.json()["detail"]

    async def test_regenerate_summaries_rejected_when_no_llm(self, app, client: AsyncClient):
        """``auto_summarize=True`` but ``app.state.llm is None`` → 400.
        Without this the background task would silently no-op while the
        UI shows a phantom "in progress" state."""
        app.state.config.indexing.auto_summarize = True
        app.state.llm = None
        resp = await client.post("/api/sources/regenerate-summaries")
        assert resp.status_code == 400
        assert "LLM" in resp.json()["detail"]

    async def test_regenerate_status_default_is_idle_zero(self, app, client: AsyncClient):
        """No job has run since startup → all counters zero, not running.
        Pin so the UI's polling loop has a stable terminal state to
        compare against."""
        resp = await client.get("/api/sources/regenerate-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {
            "running": False,
            "total": 0,
            "done": 0,
            "failed": 0,
            "skipped": 0,
        }

    async def test_regenerate_summaries_with_no_drift_is_immediate_done(
        self, app, client: AsyncClient
    ):
        """When ``list_language_drift_paths`` returns empty, the
        endpoint reports ``started=True`` with ``total=0`` — UI treats
        this as instant completion (no polling round trip)."""
        app.state.config.indexing.auto_summarize = True
        app.state.llm = MagicMock()  # any non-None
        app.state.storage.list_language_drift_paths.return_value = []
        resp = await client.post("/api/sources/regenerate-summaries")
        assert resp.status_code == 200
        data = resp.json()
        assert data == {"started": True, "total": 0}
        # Status reflects the "done immediately" state.
        status = await client.get("/api/sources/regenerate-status")
        assert status.json()["running"] is False
        assert status.json()["total"] == 0

    async def test_regenerate_task_is_referenced_on_app_state(
        self, app, client: AsyncClient, tmp_path
    ):
        """The job handle must survive on ``app.state`` (#2185).

        ``create_task`` keeps only a weak reference, and the lifespan needs a
        handle to cancel the job before ``close_components`` — otherwise it
        outlives storage and writes through a closed connection.
        """
        app.state.config.indexing.auto_summarize = True
        app.state.llm = MagicMock()
        app.state.storage.list_language_drift_paths.return_value = [tmp_path / "drifted.md"]

        started = asyncio.Event()
        release = asyncio.Event()

        async def _never_finishes(*_args, **_kwargs):
            started.set()
            await release.wait()

        with patch("memtomem.web.routes.sources._run_summary_regen", side_effect=_never_finishes):
            resp = await client.post("/api/sources/regenerate-summaries")
            assert resp.json() == {"started": True, "total": 1}
            await started.wait()
            task = app.state.summary_regen_task
            assert task is not None and not task.done()
            release.set()
            task.cancel()


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

    async def test_list_chunks_total_reflects_source_count(self, app, client: AsyncClient):
        app.state.storage.list_chunks_by_source.return_value = [_make_test_chunk()]
        app.state.storage.count_chunks_by_source.return_value = 5
        resp = await client.get(
            "/api/chunks",
            params={"source": "/tmp/test.md", "limit": 1},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["chunks"]) == 1
        assert data["total"] == 5

    async def test_list_chunks_missing_source_returns_422(self, client: AsyncClient):
        resp = await client.get("/api/chunks")
        assert resp.status_code == 422

    async def test_list_chunks_non_indexed_source_returns_403(self, app, client: AsyncClient):
        app.state.storage.get_all_source_files.return_value = [Path("/tmp/other.md")]
        resp = await client.get("/api/chunks", params={"source": "/tmp/test.md"})
        assert resp.status_code == 403

    async def test_chunk_out_carries_target_scope_from_meta(self, app, client: AsyncClient):
        """ADR-0016 §7 — ``ChunkOut.target_scope`` is sourced from
        ``ChunkMetadata.scope`` so the SPA's tier badge always agrees
        with the canonical-residency tier persisted in storage. Pin
        all three literal tokens; rendering relies on them verbatim
        (no display aliases — pinned by the Tiered Context Gateway v2
        memory)."""
        from memtomem.models import Chunk, ChunkMetadata

        def _chunk_with_scope(scope: str) -> Chunk:
            return Chunk(
                content=f"content for {scope}",
                metadata=ChunkMetadata(
                    source_file=Path("/tmp/test.md"),
                    heading_hierarchy=("Overview",),
                    tags=(),
                    namespace="default",
                    start_line=1,
                    end_line=2,
                    scope=scope,
                ),
                id=CHUNK_ID,
                content_hash="h",
                embedding=[0.1] * 768,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            )

        for token in ("user", "project_shared", "project_local"):
            app.state.storage.list_chunks_by_source.return_value = [_chunk_with_scope(token)]
            resp = await client.get("/api/chunks", params={"source": "/tmp/test.md"})
            assert resp.status_code == 200, resp.text
            assert resp.json()["chunks"][0]["target_scope"] == token, (
                f"target_scope on ChunkOut did not echo meta.scope={token!r}"
            )

    async def test_chunk_out_target_scope_defaults_to_user(self, app, client: AsyncClient):
        """Legacy chunks whose persisted ``scope`` is an empty string fall
        back to the user-tier badge. Pins the ``chunk_to_out`` fallback
        so a partially-migrated DB doesn't produce empty-string badges."""
        from memtomem.models import Chunk, ChunkMetadata

        legacy = Chunk(
            content="legacy",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/test.md"),
                heading_hierarchy=(),
                tags=(),
                namespace="default",
                start_line=1,
                end_line=2,
                scope="",  # legacy empty-string row
            ),
            id=CHUNK_ID,
            content_hash="h",
            embedding=[0.1] * 768,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        app.state.storage.list_chunks_by_source.return_value = [legacy]
        resp = await client.get("/api/chunks", params={"source": "/tmp/test.md"})
        assert resp.json()["chunks"][0]["target_scope"] == "user"


# ---------------------------------------------------------------------------
# GET /api/chunks/{id}
# ---------------------------------------------------------------------------


class TestGetChunk:
    async def test_get_chunk_by_id(self, app, client: AsyncClient):
        resp = await client.get(f"/api/chunks/{CHUNK_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["id"] == str(CHUNK_ID)
        assert data["content"] == "test chunk content"
        assert data["tags"] == ["tag1"]
        assert data["heading_hierarchy"] == ["Overview"]
        app.state.storage.recall_chunks.assert_awaited_once_with(
            chunk_ids=(CHUNK_ID,),
            limit=1,
            project_context_root=None,
        )
        app.state.storage.get_chunk.assert_not_awaited()

    async def test_get_chunk_threads_registered_project_context(
        self,
        app,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch,
    ):
        project_root = tmp_path / "project"
        shared_dir = project_root / ".memtomem" / "memories"
        cwd = project_root / "src"
        shared_dir.mkdir(parents=True)
        cwd.mkdir()
        app.state.config.indexing.project_memory_dirs = [shared_dir]
        monkeypatch.chdir(cwd)

        resp = await client.get(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code == 200
        app.state.storage.recall_chunks.assert_awaited_once_with(
            chunk_ids=(CHUNK_ID,),
            limit=1,
            project_context_root=project_root.resolve(),
        )

    async def test_get_chunk_not_found(self, app, client: AsyncClient):
        app.state.storage.recall_chunks.return_value = []
        fake_id = uuid.uuid4()
        resp = await client.get(f"/api/chunks/{fake_id}")
        assert resp.status_code == 404
        assert resp.json() == {"detail": "Chunk not found"}
        app.state.storage.get_chunk.assert_not_awaited()


# ---------------------------------------------------------------------------
# DELETE /api/chunks/{id}
# ---------------------------------------------------------------------------


class TestIndexNamespaceLookupFailure:
    """Issue #2005 follow-up: the engine refuses to re-resolve a namespace it
    could not read the stored value for. Nothing is written, so the index
    routes answer 503 like the chunk-delete path — not the generic 500 a
    genuine bug produces."""

    async def test_index_route_maps_the_failure_to_503(self, app, client: AsyncClient):
        from memtomem.errors import NamespaceResolutionError

        app.state.index_engine.index_path = AsyncMock(
            side_effect=NamespaceResolutionError("store down")
        )

        resp = await client.post("/api/index", json={"path": "/tmp/memories"})

        assert resp.status_code == 503, resp.text
        detail = resp.json()["detail"]
        assert "Retry" in detail
        # Caller-supplied and not the response's business to reflect back.
        assert "/tmp/memories" not in detail

    async def test_index_route_passes_the_retryable_marker_through(self, app, client: AsyncClient):
        """A mid-run lookup failure reaches the client as data, not just an
        opaque error string: ``retryable_errors`` rides the 200 body so the
        caller can tell "retry this" from "this file is broken" (#2018)."""
        from memtomem.models import IndexingStats

        app.state.index_engine.index_path = AsyncMock(
            return_value=IndexingStats(
                total_files=1,
                total_chunks=0,
                indexed_chunks=0,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=1.0,
                errors=("a.md: store down",),
                retryable_errors=("a.md: store down",),
            )
        )

        resp = await client.post("/api/index", json={"path": "/tmp/memories"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["retryable_errors"] == ["a.md: store down"]

    async def test_preview_route_maps_the_failure_to_503(self, app, client: AsyncClient):
        from memtomem.errors import NamespaceResolutionError

        app.state.index_engine.discover_indexable_files = MagicMock(
            return_value=[Path("/tmp/memories/a.md")]
        )
        app.state.index_engine.resolve_namespaces_for = AsyncMock(
            side_effect=NamespaceResolutionError("store down")
        )

        resp = await client.post(
            "/api/index/preview-namespace", json={"path": "/tmp/memories", "recursive": True}
        )

        assert resp.status_code == 503, resp.text
        # Same assertions as the index route: "one string so the two cannot
        # drift" is only pinned if both ends are actually read.
        detail = resp.json()["detail"]
        assert "Retry" in detail
        assert "/tmp/memories" not in detail

    async def test_the_two_index_routes_answer_with_one_string(self, app, client: AsyncClient):
        """The drift pin. Two routes describing one condition two ways is the
        defect the shared constant exists to prevent, and only a comparison
        catches a copy that was edited on one side."""
        from memtomem.errors import NamespaceResolutionError

        app.state.index_engine.index_path = AsyncMock(
            side_effect=NamespaceResolutionError("store down")
        )
        app.state.index_engine.resolve_namespaces_for = AsyncMock(
            side_effect=NamespaceResolutionError("store down")
        )

        index = await client.post("/api/index", json={"path": "/tmp/memories"})
        preview = await client.post(
            "/api/index/preview-namespace", json={"path": "/tmp/memories", "recursive": True}
        )

        assert index.json()["detail"] == preview.json()["detail"]

    async def test_reindex_keeps_the_roots_it_already_finished(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """``/api/reindex`` walks every registered root, so this one is caught
        per root rather than per request: a blanket 503 would discard the
        results of the roots indexed before the failure and claim nothing was
        changed, which for this route would be untrue."""
        from memtomem.errors import NamespaceResolutionError

        first, second = tmp_path / "one", tmp_path / "two"
        first.mkdir()
        second.mkdir()
        app.state.config.indexing.memory_dirs = [first, second]
        app.state.index_engine.index_path = AsyncMock(
            side_effect=[
                IndexingStats(
                    total_files=1,
                    total_chunks=2,
                    indexed_chunks=2,
                    skipped_chunks=0,
                    deleted_chunks=0,
                    duration_ms=10.0,
                ),
                NamespaceResolutionError("store down"),
            ]
        )

        resp = await client.post("/api/reindex")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["results"][0]["indexed_chunks"] == 2
        assert "Retry" in body["results"][1]["errors"][0]
        assert body["errors"] == body["results"][1]["errors"]
        assert body["results"][1]["retryable_errors"] == body["results"][1]["errors"]
        assert body["retryable_errors"] == body["errors"]
        # This route may not claim "nothing was changed" — the first root's
        # chunks above are the counterexample, in the same response body.
        from memtomem.web.routes._errors import NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL

        assert body["results"][1]["errors"][0] != NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL
        assert "nothing was changed" not in body["results"][1]["errors"][0]

    async def test_reindex_aggregates_retryable_as_a_strict_subset(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """Top-level ``retryable_errors`` aggregates per-root subsets rather
        than aliasing ``errors``. The sibling test above cannot catch that:
        there every error is retryable, so an accidental
        ``retryable_errors = all_errors`` would still pass. Here a permanent
        failure and a retryable one land in the same response, so the two
        aggregates must differ — and the healthy root must still contribute an
        empty list rather than omitting the key."""
        healthy, permanent_root, retryable_root = (
            tmp_path / "ok",
            tmp_path / "broken",
            tmp_path / "transient",
        )
        for d in (healthy, permanent_root, retryable_root):
            d.mkdir()
        app.state.config.indexing.memory_dirs = [healthy, permanent_root, retryable_root]
        permanent = "broken.md: malformed frontmatter"
        retryable = "transient.md: chunk store unavailable"
        app.state.index_engine.index_path = AsyncMock(
            side_effect=[
                IndexingStats(
                    total_files=1,
                    total_chunks=2,
                    indexed_chunks=2,
                    skipped_chunks=0,
                    deleted_chunks=0,
                    duration_ms=10.0,
                ),
                IndexingStats(
                    total_files=1,
                    total_chunks=0,
                    indexed_chunks=0,
                    skipped_chunks=0,
                    deleted_chunks=0,
                    duration_ms=1.0,
                    errors=(permanent,),
                ),
                IndexingStats(
                    total_files=1,
                    total_chunks=0,
                    indexed_chunks=0,
                    skipped_chunks=0,
                    deleted_chunks=0,
                    duration_ms=1.0,
                    errors=(retryable,),
                    retryable_errors=(retryable,),
                ),
            ]
        )

        resp = await client.post("/api/reindex")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["errors"] == [permanent, retryable]
        assert body["retryable_errors"] == [retryable]
        # A healthy root still carries both keys, so a client can tell "no
        # retryable failures here" from "this server predates the field".
        assert body["results"][0]["errors"] == []
        assert body["results"][0]["retryable_errors"] == []
        assert body["results"][1]["retryable_errors"] == []

    async def test_reindex_missing_root_is_not_reported_as_success(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """A registered root that was deleted or renamed used to emit only the
        singular ``error`` key, which the aggregates skip — so the response
        said ``ok: true`` with an empty top-level ``errors`` and every
        first-party client rendered "reindex complete" over a root that was
        never indexed. It is not retryable: retrying cannot conjure the
        directory back, so it must not land in ``retryable_errors``."""
        missing = tmp_path / "gone"
        app.state.config.indexing.memory_dirs = [missing]
        app.state.index_engine.index_path = AsyncMock()

        resp = await client.post("/api/reindex")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["ok"] is False
        assert body["retryable_errors"] == []
        assert len(body["errors"]) == 1
        assert "not a directory" in body["errors"][0]
        # The singular key stays for existing clients.
        assert body["results"][0]["error"] == "not a directory"
        app.state.index_engine.index_path.assert_not_awaited()

    async def test_the_stream_route_says_transient_in_the_only_field_it_has(
        self, app, client: AsyncClient
    ):
        """SSE carries no status code, so the message is the only place this
        surface can classify the failure. Since #2018 the stream resolves
        every namespace before its first write, so an escaping lookup failure
        means the run changed nothing — the stream states the same
        post-condition, with the same string, as the single-shot routes."""
        from memtomem.errors import NamespaceResolutionError
        from memtomem.web.routes._errors import NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL

        async def _boom(*args, **kwargs):
            raise NamespaceResolutionError("could not read the stored namespace for /tmp/secret.md")
            yield  # pragma: no cover — generator marker

        app.state.index_engine.index_path_stream = _boom

        resp = await client.post("/api/index/stream", json={"path": "/tmp/memories"})

        assert resp.status_code == 200, resp.text
        event = json.loads(resp.text.split("data: ", 1)[1].strip())
        assert event["retryable"] is True
        assert event["message"] == NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL
        # The engine embeds the absolute path in ``str(exc)``.
        assert "/tmp/secret.md" not in event["message"]


class TestEditChunkNamespaceLookupFailure:
    """The web twin of the MCP ``mem_edit`` re-raise: a re-index that cannot
    read the file's stored namespace is transient and rolled back, so the
    route answers 503 rather than the 500 its generic handler would give."""

    async def test_edit_route_maps_the_failure_to_503(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        from memtomem.errors import NamespaceResolutionError

        source = tmp_path / "memory.md"
        source.write_text("## Cache strategy\n\nOld body line.\n", encoding="utf-8")
        chunk = _make_test_chunk(source=str(source))
        chunk = chunk.__class__(
            content=chunk.content,
            metadata=chunk.metadata.__class__(
                source_file=source,
                heading_hierarchy=("## Cache strategy",),
                tags=chunk.metadata.tags,
                namespace=chunk.metadata.namespace,
                start_line=1,
                end_line=3,
            ),
            id=chunk.id,
            content_hash=chunk.content_hash,
            embedding=chunk.embedding,
            created_at=chunk.created_at,
            updated_at=chunk.updated_at,
        )
        app.state.storage.get_chunk.return_value = chunk
        app.state.index_engine.index_file = AsyncMock(
            side_effect=NamespaceResolutionError("store down")
        )
        before = source.read_text(encoding="utf-8")

        resp = await client.patch(f"/api/chunks/{CHUNK_ID}", json={"new_content": "Replaced."})

        assert resp.status_code == 503, resp.text
        assert "Retry" in resp.json()["detail"]
        # 503 says "nothing was changed" — the rollback is what makes that true.
        assert source.read_text(encoding="utf-8") == before

    async def test_a_permanent_edit_failure_is_still_500(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """The discriminating half: without it, mapping *every* edit failure to
        503 would pass the test above and invite endless retries of a genuine
        bug."""
        source = tmp_path / "memory.md"
        source.write_text("## Cache strategy\n\nOld body line.\n", encoding="utf-8")
        chunk = _make_test_chunk(source=str(source))
        chunk = chunk.__class__(
            content=chunk.content,
            metadata=chunk.metadata.__class__(
                source_file=source,
                heading_hierarchy=("## Cache strategy",),
                tags=chunk.metadata.tags,
                namespace=chunk.metadata.namespace,
                start_line=1,
                end_line=3,
            ),
            id=chunk.id,
            content_hash=chunk.content_hash,
            embedding=chunk.embedding,
            created_at=chunk.created_at,
            updated_at=chunk.updated_at,
        )
        app.state.storage.get_chunk.return_value = chunk
        app.state.index_engine.index_file = AsyncMock(side_effect=RuntimeError("boom"))

        resp = await client.patch(f"/api/chunks/{CHUNK_ID}", json={"new_content": "Replaced."})

        assert resp.status_code == 500, resp.text


class TestDeleteChunk:
    async def test_delete_chunk(self, app, client: AsyncClient, tmp_path: Path):
        chunk = _make_test_chunk(source=str(tmp_path / "missing.md"))
        app.state.storage.get_chunk = AsyncMock(side_effect=[chunk, chunk, chunk, chunk, None])

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 1
        app.state.storage.delete_chunks.assert_awaited_once_with([chunk.id])

    @staticmethod
    def _real_source_chunk(app, tmp_path: Path):
        """A chunk whose source file exists with real line numbers, so the
        route takes its remove-lines + re-index branch instead of the
        index-only fallback."""
        import dataclasses

        source = tmp_path / "multi.md"
        source.write_text("## a\n\nfirst\n\n## b\n\nsecond\n", encoding="utf-8")
        base = _make_test_chunk(source=str(source))
        chunk = dataclasses.replace(
            base,
            metadata=dataclasses.replace(
                base.metadata, source_file=source, start_line=1, end_line=3
            ),
        )
        # Route lookup, unlocked + fresh lookups in ``locked_source_chunk``,
        # then the post-condition probe after the forced re-index.
        app.state.storage.get_chunk = AsyncMock(side_effect=[chunk, chunk, chunk, None])
        return source, chunk

    async def test_delete_preflights_the_namespace_without_pinning_it(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """Issue #2005: the re-index uses ``force=True`` for the re-embed.
        Back when force also re-resolved namespaces, the survivors of a delete
        moved to whatever the rules said that day, and the route pinned the
        file's namespace to stop it. Since #2061 the engine preserves in-lock,
        so the route pre-flights (to refuse a mixed source before editing the
        file) but sends no namespace — a pin would freeze a value read outside
        the write's critical section, and would override the refusal."""
        from memtomem.indexing.engine import NamespaceDecision

        source, _chunk = self._real_source_chunk(app, tmp_path)
        app.state.index_engine.namespace_decision_for = AsyncMock(
            return_value=NamespaceDecision(target="aaa", stored=("aaa",), reason="preserved")
        )

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code == 200, resp.text
        app.state.index_engine.namespace_decision_for.assert_awaited_once_with(source, force=True)
        kwargs = app.state.index_engine.index_file.await_args.kwargs
        assert kwargs["force"] is True
        # No pin: the engine preserves in-lock, which is both correct and
        # fresher than anything the pre-flight read (#2061 review 5). Pinning
        # would also override the multi-namespace refusal.
        assert "namespace" not in kwargs

    async def test_delete_refuses_on_a_mixed_namespace_source(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """#2061: pinning the namespace is what lets a delete bypass the
        forced re-index's multi-namespace refusal. On a legacy source whose
        rows span several namespaces the pin would be the rule-resolved
        target, rewriting every survivor into it — so the delete is refused
        before the file is touched."""
        from memtomem.indexing.engine import NamespaceDecision

        source, _ = self._real_source_chunk(app, tmp_path)
        before = source.read_text(encoding="utf-8")
        app.state.index_engine.namespace_decision_for = AsyncMock(
            return_value=NamespaceDecision(
                target=None, stored=("aaa", "agent-runtime:planner"), reason="mixed_force_refused"
            )
        )

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code == 409, resp.text
        assert "span several namespaces" in resp.json()["detail"]
        # The pre-flight must ask the same question the write would, or its
        # answer describes a different operation.
        app.state.index_engine.namespace_decision_for.assert_awaited_once_with(source, force=True)
        # Nothing touched: not the file, not the index, not the chunk rows.
        assert source.read_text(encoding="utf-8") == before
        app.state.index_engine.index_file.assert_not_awaited()
        app.state.storage.delete_chunks.assert_not_awaited()

    async def test_delete_refuses_when_the_namespace_lookup_fails(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """The route's fallback is an index-only delete, which is right for a
        failed *file* edit and wrong here: the row would go while the source
        kept the entry, so the chunk returns on the next re-index and the
        caller was told the delete succeeded."""
        from memtomem.errors import NamespaceResolutionError

        source, _chunk = self._real_source_chunk(app, tmp_path)
        before = source.read_text(encoding="utf-8")
        app.state.index_engine.namespace_decision_for = AsyncMock(
            side_effect=NamespaceResolutionError("store down")
        )

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code == 503, resp.text
        app.state.storage.delete_chunks.assert_not_awaited()
        assert source.read_text(encoding="utf-8") == before

    @pytest.mark.parametrize(
        ("start_line", "end_line"),
        [(0, 0), (1, 999)],
        ids=["missing-provenance", "stale-provenance"],
    )
    async def test_delete_refuses_unusable_source_provenance_without_index_fallback(
        self,
        app,
        client: AsyncClient,
        tmp_path: Path,
        start_line: int,
        end_line: int,
    ):
        import dataclasses

        source, chunk = self._real_source_chunk(app, tmp_path)
        chunk = dataclasses.replace(
            chunk,
            metadata=dataclasses.replace(chunk.metadata, start_line=start_line, end_line=end_line),
        )
        app.state.storage.get_chunk = AsyncMock(side_effect=[chunk, chunk, chunk])
        before = source.read_text(encoding="utf-8")

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code == 409, resp.text
        app.state.storage.delete_chunks.assert_not_awaited()
        assert source.read_text(encoding="utf-8") == before

    async def test_delete_refuses_source_stat_error_without_index_fallback(
        self, app, client: AsyncClient, tmp_path: Path, monkeypatch
    ):
        source, chunk = self._real_source_chunk(app, tmp_path)
        app.state.storage.get_chunk = AsyncMock(side_effect=[chunk, chunk, chunk])
        before = source.read_text(encoding="utf-8")
        real_stat = Path.stat

        def denied(path: Path, *args, **kwargs):
            if path == source:
                raise PermissionError("denied")
            return real_stat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "stat", denied)

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code == 503, resp.text
        app.state.storage.delete_chunks.assert_not_awaited()
        assert source.read_text(encoding="utf-8") == before

    async def test_delete_refuses_source_write_error_without_index_fallback(
        self, app, client: AsyncClient, tmp_path: Path, monkeypatch
    ):
        source, _chunk = self._real_source_chunk(app, tmp_path)
        before = source.read_text(encoding="utf-8")

        def denied(*_args, **_kwargs):
            raise PermissionError("denied")

        monkeypatch.setattr("memtomem.web.routes.chunks.remove_lines", denied)

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code == 503, resp.text
        app.state.storage.delete_chunks.assert_not_awaited()
        assert source.read_text(encoding="utf-8") == before

    @pytest.mark.parametrize("reindex_outcome", ["raises", "reports-errors"])
    async def test_delete_verifies_and_cleans_the_row_after_reindex_failure(
        self,
        app,
        client: AsyncClient,
        tmp_path: Path,
        reindex_outcome: str,
    ):
        source, chunk = self._real_source_chunk(app, tmp_path)
        app.state.storage.get_chunk = AsyncMock(side_effect=[chunk, chunk, chunk, chunk, None])
        if reindex_outcome == "raises":
            app.state.index_engine.index_file = AsyncMock(side_effect=RuntimeError("boom"))
        else:
            app.state.index_engine.index_file = AsyncMock(
                return_value=IndexingStats(0, 0, 0, 0, 0, 0.0, errors=("boom",))
            )

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code == 200, resp.text
        app.state.storage.delete_chunks.assert_awaited_once_with([chunk.id])
        assert "first" not in source.read_text(encoding="utf-8")

    async def test_delete_reports_partial_failure_when_index_cleanup_does_not_finish(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        source, chunk = self._real_source_chunk(app, tmp_path)
        app.state.storage.get_chunk = AsyncMock(side_effect=[chunk, chunk, chunk, chunk])
        app.state.index_engine.index_file = AsyncMock(side_effect=RuntimeError("reindex failed"))
        app.state.storage.delete_chunks = AsyncMock(side_effect=RuntimeError("delete failed"))

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code == 500, resp.text
        assert "source entry was removed" in resp.json()["detail"]
        assert "first" not in source.read_text(encoding="utf-8")

    async def test_delete_chunk_not_found(self, app, client: AsyncClient):
        app.state.storage.get_chunk.return_value = None
        fake_id = uuid.uuid4()
        resp = await client.delete(f"/api/chunks/{fake_id}")
        assert resp.status_code == 404

    async def test_delete_project_shared_chunk_requires_confirm(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """ADR-0011 PR-D review round 7 pin: web DELETE on a project_shared
        chunk MUST refuse without ``confirm_project_shared=true`` query
        parameter. Mirrors the MCP ``mem_delete`` round-3 fix (8407d73).
        Without this guard a single ``DELETE /api/chunks/{id}`` would
        rewrite git-tracked memory without any explicit opt-in.
        """
        proj = tmp_path / "proj"
        (proj / ".memtomem" / "memories").mkdir(parents=True)
        source = proj / ".memtomem" / "memories" / "rule.md"
        source.write_text("## hi\n\nproject content\n", encoding="utf-8")
        chunk = _make_test_chunk(source=str(source))
        chunk = chunk.__class__(
            content=chunk.content,
            metadata=chunk.metadata.__class__(
                source_file=source,
                heading_hierarchy=chunk.metadata.heading_hierarchy,
                tags=chunk.metadata.tags,
                namespace=chunk.metadata.namespace,
                start_line=1,
                end_line=3,
                scope="project_shared",
                project_root=proj,
            ),
            id=chunk.id,
            content_hash=chunk.content_hash,
            embedding=chunk.embedding,
            created_at=chunk.created_at,
            updated_at=chunk.updated_at,
        )
        app.state.storage.get_chunk.return_value = chunk

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")
        assert resp.status_code == 403, resp.text
        body = resp.json()
        # FastAPI wraps the route's ``detail`` dict under the
        # top-level ``"detail"`` key.
        detail = body.get("detail", {})
        assert detail.get("detail") == "blocked_project_shared"
        assert detail.get("surface") == "web_api_chunk_delete"

    async def test_delete_project_shared_chunk_with_confirm_proceeds(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """``confirm_project_shared=true`` lets the delete succeed."""
        proj = tmp_path / "proj"
        (proj / ".memtomem" / "memories").mkdir(parents=True)
        source = proj / ".memtomem" / "memories" / "rule.md"
        source.write_text("## hi\n\nproject content\n", encoding="utf-8")
        chunk = _make_test_chunk(source=str(source))
        chunk = chunk.__class__(
            content=chunk.content,
            metadata=chunk.metadata.__class__(
                source_file=source,
                heading_hierarchy=chunk.metadata.heading_hierarchy,
                tags=chunk.metadata.tags,
                namespace=chunk.metadata.namespace,
                start_line=1,
                end_line=3,
                scope="project_shared",
                project_root=proj,
            ),
            id=chunk.id,
            content_hash=chunk.content_hash,
            embedding=chunk.embedding,
            created_at=chunk.created_at,
            updated_at=chunk.updated_at,
        )
        app.state.storage.get_chunk = AsyncMock(side_effect=[chunk, chunk, chunk, None])

        resp = await client.delete(
            f"/api/chunks/{CHUNK_ID}",
            params={"confirm_project_shared": "true"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] == 1


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


class TestEditChunkRedaction:
    @pytest.fixture(autouse=True)
    def _reset_counters(self):
        from memtomem import privacy

        privacy.reset_for_tests()
        yield
        privacy.reset_for_tests()

    async def test_secret_in_new_content_returns_403(self, app, client: AsyncClient):
        from memtomem import privacy

        chunk = _make_test_chunk()
        app.state.storage.get_chunk.return_value = chunk
        resp = await client.patch(
            f"/api/chunks/{CHUNK_ID}",
            json={"new_content": "token=sk-" + "a" * 30},
        )
        assert resp.status_code == 403, resp.text
        snap = privacy.snapshot()["by_tool"]["web_api_chunk_edit"]
        assert snap["blocked"] == 1

    async def test_force_unsafe_passes_guard(self, app, client: AsyncClient, tmp_path: Path):
        from memtomem import privacy

        source = tmp_path / "memory.md"
        source.write_text("## H\n\nbody\n", encoding="utf-8")
        chunk = _make_test_chunk(source=str(source))
        chunk = chunk.__class__(
            content=chunk.content,
            metadata=chunk.metadata.__class__(
                source_file=source,
                heading_hierarchy=("## H",),
                tags=chunk.metadata.tags,
                namespace=chunk.metadata.namespace,
                start_line=1,
                end_line=3,
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
            json={
                "new_content": "secret token=sk-" + "a" * 30,
                "force_unsafe": True,
            },
        )
        assert resp.status_code == 200, resp.text
        snap = privacy.snapshot()["by_tool"]["web_api_chunk_edit"]
        assert snap["bypassed"] == 1

    async def test_force_unsafe_on_project_shared_chunk_blocks(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """ADR-0011 PR-D review round 7 pin: PATCH on a project_shared
        chunk must infer scope from the loaded metadata so Gate A's
        ``force_unsafe`` hard-refusal applies. Without
        ``scope=meta.scope`` on ``enforce_write_guard``, a force_unsafe
        edit with a secret hit returns ``bypassed`` (status 200) and
        the secret lands in git-tracked memory. Mirrors the MCP
        ``mem_edit`` contract at memory_crud.py:406-413.
        """
        from memtomem import privacy

        proj = tmp_path / "proj"
        (proj / ".memtomem" / "memories").mkdir(parents=True)
        source = proj / ".memtomem" / "memories" / "rule.md"
        source.write_text("## H\n\nbody\n", encoding="utf-8")
        chunk = _make_test_chunk(source=str(source))
        chunk = chunk.__class__(
            content=chunk.content,
            metadata=chunk.metadata.__class__(
                source_file=source,
                heading_hierarchy=("## H",),
                tags=chunk.metadata.tags,
                namespace=chunk.metadata.namespace,
                start_line=1,
                end_line=3,
                scope="project_shared",
                project_root=proj,
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
            json={
                "new_content": "secret token=sk-" + "a" * 30,
                "force_unsafe": True,
            },
        )
        assert resp.status_code == 403, resp.text
        body = resp.json()
        detail = body.get("detail", {})
        assert detail.get("detail") == "blocked_project_shared"
        snap = privacy.snapshot()["by_tool"].get("web_api_chunk_edit", {})
        assert snap.get("blocked_project_shared", 0) == 1
        # The bypass counter must NOT have ticked — that was the bug.
        assert snap.get("bypassed", 0) == 0


# ---------------------------------------------------------------------------
# Temporal-validity exposure on ChunkOut (RFC §Goal 7 — Web UI badge)
# ---------------------------------------------------------------------------


class TestChunkValidityFields:
    """``ChunkOut`` surfaces ``valid_from_unix`` / ``valid_to_unix`` so the
    Web UI can render the temporal-validity badge. The frontend reads these
    fields directly (see ``_renderValidityBadge`` / ``_validityBadgeHtml``
    in ``app.js``), so the API contract is what this test pins.

    Also verifies the regression fix in ``update_chunk_tags`` — the route
    used to reconstruct ``ChunkMetadata`` with an explicit field list,
    silently dropping any field not enumerated. The Goal 7 PR switches to
    a copy-with-override (dict spread) so future ``ChunkMetadata``
    extensions don't have to chase that call site.
    """

    async def test_chunkout_includes_validity_when_set(self, app, client: AsyncClient):
        from memtomem.models import Chunk, ChunkMetadata

        chunk = Chunk(
            content="windowed",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/test.md"),
                tags=("policy",),
                namespace="default",
                start_line=1,
                end_line=3,
                valid_from_unix=1_734_220_800,  # 2024-12-15 00:00 UTC
                valid_to_unix=1_743_465_599,  # 2025-Q1 end (2025-03-31 23:59:59 UTC)
            ),
            id=CHUNK_ID,
            content_hash="abc123",
            embedding=[0.1] * 768,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        app.state.storage.recall_chunks.return_value = [chunk]

        resp = await client.get(f"/api/chunks/{CHUNK_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid_from_unix"] == 1_734_220_800
        assert data["valid_to_unix"] == 1_743_465_599

    async def test_chunkout_validity_null_when_unset(self, client: AsyncClient):
        """``_make_test_chunk`` produces a chunk without validity frontmatter
        — both fields must serialize as ``null`` so the frontend's
        always-valid branch (hidden badge) fires.
        """
        resp = await client.get(f"/api/chunks/{CHUNK_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid_from_unix"] is None
        assert data["valid_to_unix"] is None

    async def test_tag_update_preserves_validity(self, app, client: AsyncClient):
        """Regression: PATCH /chunks/{id}/tags must not silently drop the
        temporal-validity columns. Before Goal 7 the route reconstructed
        ``ChunkMetadata`` with an explicit field list; with the
        dict-spread fix every field — including ``valid_from_unix`` /
        ``valid_to_unix`` and the long-broken ``overlap_*`` /
        ``parent_context`` / ``file_context`` — round-trips intact.
        """
        from memtomem.models import Chunk, ChunkMetadata

        chunk_with_validity = Chunk(
            content="windowed",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/test.md"),
                tags=("old-tag",),
                namespace="default",
                start_line=1,
                end_line=3,
                valid_from_unix=1_734_220_800,
                valid_to_unix=1_743_465_599,
                parent_context="Section A",
                overlap_before=42,
            ),
            id=CHUNK_ID,
            content_hash="abc123",
            embedding=[0.1] * 768,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        app.state.storage.get_chunk.return_value = chunk_with_validity

        resp = await client.patch(
            f"/api/chunks/{CHUNK_ID}/tags",
            json={"tags": ["new-tag", "another"]},
        )
        assert resp.status_code == 200

        # Inspect the actual upsert call — that is what touches the DB and
        # therefore what would silently drop fields on the way back.
        upsert_call = app.state.storage.upsert_chunks.await_args
        assert upsert_call is not None, "tag PATCH must call upsert_chunks"
        upserted_chunks = upsert_call.args[0]
        assert len(upserted_chunks) == 1
        new_meta = upserted_chunks[0].metadata
        assert new_meta.valid_from_unix == 1_734_220_800
        assert new_meta.valid_to_unix == 1_743_465_599
        # Sister-fields the old explicit-list shape would also have wiped
        # — pinning them prevents the same bug returning if someone re-flattens.
        assert new_meta.parent_context == "Section A"
        assert new_meta.overlap_before == 42
        assert tuple(new_meta.tags) == ("new-tag", "another")

    async def test_tag_update_invalidates_search_cache(self, app, client: AsyncClient):
        """Routing PATCH /chunks/{id}/tags through services.tag_management
        means a successful tag rewrite must flush the search-result TTL
        cache — otherwise tag-filter queries can return stale hits until
        the cache expires. The previous direct ``upsert_chunks`` shape
        had no hook into ``SearchPipeline``."""
        from memtomem.models import Chunk, ChunkMetadata

        chunk = Chunk(
            content="alpha",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/test.md"),
                tags=("old",),
                namespace="default",
            ),
            id=CHUNK_ID,
            content_hash="abc",
            embedding=[0.1] * 768,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        app.state.storage.get_chunk.return_value = chunk
        app.state.search_pipeline.invalidate_cache.reset_mock()

        resp = await client.patch(
            f"/api/chunks/{CHUNK_ID}/tags",
            json={"tags": ["new"]},
        )
        assert resp.status_code == 200
        assert app.state.search_pipeline.invalidate_cache.call_count == 1

    async def test_tag_update_no_op_skips_upsert_and_invalidate(self, app, client: AsyncClient):
        """Idempotent guard: PATCH-ing the same tag list a chunk already
        carries must not call ``upsert_chunks`` (no ``updated_at`` bump,
        no decay-timer reset) and must not flush the cache."""
        from memtomem.models import Chunk, ChunkMetadata

        chunk = Chunk(
            content="alpha",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/test.md"),
                tags=("a", "b"),
                namespace="default",
            ),
            id=CHUNK_ID,
            content_hash="abc",
            embedding=[0.1] * 768,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
        app.state.storage.get_chunk.return_value = chunk
        app.state.storage.upsert_chunks.reset_mock()
        app.state.search_pipeline.invalidate_cache.reset_mock()

        resp = await client.patch(
            f"/api/chunks/{CHUNK_ID}/tags",
            json={"tags": ["a", "b"]},
        )
        assert resp.status_code == 200
        assert app.state.storage.upsert_chunks.await_count == 0
        assert app.state.search_pipeline.invalidate_cache.call_count == 0

    async def test_tag_update_404_when_chunk_missing(self, app, client: AsyncClient):
        app.state.storage.get_chunk.return_value = None
        app.state.storage.upsert_chunks.reset_mock()
        app.state.search_pipeline.invalidate_cache.reset_mock()

        resp = await client.patch(
            f"/api/chunks/{CHUNK_ID}/tags",
            json={"tags": ["new"]},
        )
        assert resp.status_code == 404
        assert app.state.storage.upsert_chunks.await_count == 0
        assert app.state.search_pipeline.invalidate_cache.call_count == 0

    async def test_tag_update_delegates_to_service(self, app, client: AsyncClient, monkeypatch):
        """Pin the single-service-path invariant for ``PATCH
        /chunks/{id}/tags``. The side-effect tests above (invalidate /
        no-op / 404) would still pass if a future refactor reintroduced
        route-local read-modify-upsert that happened to mimic the same
        side effects — losing the contract that
        ``services.tag_management.replace_chunk_tags`` is the only place
        per-chunk tag edits go through.

        Stub the service and assert the route forwards storage,
        chunk_id, body.tags, and search_pipeline verbatim.
        """
        from memtomem.models import Chunk, ChunkMetadata
        from memtomem.web.routes import chunks as chunks_route

        chunk = Chunk(
            content="alpha",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/test.md"),
                tags=("new",),
                namespace="default",
            ),
            id=CHUNK_ID,
            content_hash="abc",
            embedding=[0.1] * 768,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        captured: dict = {}

        async def fake_replace(storage, chunk_id, tags, *, search_pipeline=None):
            captured["storage"] = storage
            captured["chunk_id"] = chunk_id
            captured["tags"] = list(tags)
            captured["search_pipeline"] = search_pipeline
            return chunk

        monkeypatch.setattr(chunks_route.tag_svc, "replace_chunk_tags", fake_replace)

        resp = await client.patch(
            f"/api/chunks/{CHUNK_ID}/tags",
            json={"tags": ["new"]},
        )
        assert resp.status_code == 200

        assert captured["storage"] is app.state.storage
        assert captured["chunk_id"] == CHUNK_ID
        assert captured["tags"] == ["new"]
        assert captured["search_pipeline"] is app.state.search_pipeline


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
        metadata = {
            "title": "Sprint",
            "provenance": "write-v1",
            "provenance_incomplete": True,
        }
        app.state.storage.list_sessions.return_value = [
            {
                "id": "sess-1",
                "agent_id": "agent-a",
                "started_at": "2026-01-01T00:00:00Z",
                "ended_at": None,
                "summary": None,
                "namespace": "default",
                "metadata": metadata,
            }
        ]
        resp = await client.get("/api/sessions")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["sessions"][0]["id"] == "sess-1"
        assert data["sessions"][0]["metadata"] == metadata

    async def test_list_sessions_defaults_missing_metadata_to_object(
        self, app, client: AsyncClient
    ):
        app.state.storage.list_sessions.return_value = [
            {
                "id": "sess-legacy",
                "agent_id": "agent-a",
                "started_at": "2026-01-01T00:00:00Z",
                "ended_at": None,
                "summary": None,
                "namespace": "default",
            }
        ]

        resp = await client.get("/api/sessions")

        assert resp.status_code == 200
        assert resp.json()["sessions"][0]["metadata"] == {}

    async def test_get_events_routes_a_slash_bearing_session_id(self, app, client: AsyncClient):
        """An external-proposal id embeds a caller-supplied ``source`` that can
        contain ``/`` (``formation.py``). The ``:path`` converter must route it
        to the handler intact rather than 404 on the slash."""
        from urllib.parse import quote

        session_id = "external:proj/sub:0123456789abcdef01234567"
        app.state.storage.get_session_events.return_value = [
            {
                "event_type": "add",
                "content": "x",
                "chunk_ids": [],
                "created_at": "2026-01-01T00:00:00Z",
                "metadata": {},
            }
        ]

        resp = await client.get(f"/api/sessions/{quote(session_id, safe='/')}/events")

        assert resp.status_code == 200
        assert resp.json()["session_id"] == session_id
        app.state.storage.get_session_events.assert_awaited_with(session_id)


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

    @staticmethod
    def _seed_mixed_target(app, tmp_path) -> Path:
        """A real file with content: the guard skips a missing or empty
        target on purpose (nothing to protect), so a mock-only setup would
        never reach the namespace comparison.

        ``config.indexing.memory_dirs`` is the attribute the route resolves
        the base from — assigning ``config.memory_dirs`` instead silently
        leaves the route pointing at the fixture default ``/tmp/memories``,
        where a stray file from an earlier run makes this pass for the wrong
        reason on a developer's machine and fail on a clean runner.
        """
        base = tmp_path / "memories"
        base.mkdir(exist_ok=True)
        target = base / "shared.md"
        target.write_text("previously written text\n")
        app.state.config.indexing.memory_dirs = [base]
        app.state.storage.namespaces_for_source = AsyncMock(return_value=["aaa"])
        app.state.index_engine.effective_namespace_for = AsyncMock(return_value="bbb")
        return target

    async def test_add_memory_refuses_a_mixed_namespace_file(
        self, client: AsyncClient, app, tmp_path
    ):
        """Issue #2005: appending into a file that already holds another
        namespace would let re-chunking restamp its entries."""
        target = self._seed_mixed_target(app, tmp_path)

        resp = await client.post(
            "/api/add",
            json={"content": "test", "file": "shared.md", "namespace": "bbb"},
        )

        # The guard only engages on a target with content. Assert the setup
        # reached the route's own base, or a path mismatch would read as
        # "guard did not fire" instead of "test pointed somewhere else".
        assert target.exists() and target.stat().st_size > 0
        assert Path(app.state.config.indexing.memory_dirs[0]) == target.parent
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "'aaa'" in detail and "allow_namespace_mix=true" in detail
        app.state.index_engine.index_file.assert_not_awaited()

    async def test_add_memory_refuses_before_appending_when_the_lookup_fails(
        self, client: AsyncClient, app, tmp_path
    ):
        """#2005 follow-up. The re-index resolves the namespace itself when
        the caller named none, and that resolution can fail *after* the append
        is durable. This route has no idempotency key, so a caller told to
        retry a half-completed write appends the entry twice — the refusal has
        to land before anything is written, not after."""
        from memtomem.errors import NamespaceResolutionError

        base = tmp_path / "memories"
        base.mkdir(exist_ok=True)
        app.state.config.indexing.memory_dirs = [base]
        app.state.storage.namespaces_for_source = AsyncMock(return_value=[])
        app.state.index_engine.effective_namespace_for = AsyncMock(
            side_effect=NamespaceResolutionError("store down")
        )

        resp = await client.post("/api/add", json={"content": "test", "file": "note.md"})

        assert resp.status_code == 503, resp.text
        assert "Retry" in resp.json()["detail"]
        # The whole point: the retry this response invites must not duplicate
        # an entry that already landed.
        assert not (base / "note.md").exists()
        app.state.index_engine.index_file.assert_not_awaited()

    async def test_add_memory_leaves_no_second_lookup_after_the_append(
        self, client: AsyncClient, app, tmp_path
    ):
        """Pre-flighting alone does not close the window — it moves it. If the
        answer is discarded and ``index_file`` resolves again, that second
        lookup sits *after* the durable append, where 503 would be a lie and
        500 still invites the retry that duplicates the entry. Passing the
        resolved namespace through means there is no second lookup to fail."""
        base = tmp_path / "memories"
        base.mkdir(exist_ok=True)
        app.state.config.indexing.memory_dirs = [base]
        app.state.storage.namespaces_for_source = AsyncMock(return_value=[])
        app.state.index_engine.effective_namespace_for = AsyncMock(return_value="notes")

        resp = await client.post("/api/add", json={"content": "test", "file": "note.md"})

        assert resp.status_code == 200, resp.text
        # One lookup for the whole request, and its answer reaches the write.
        app.state.index_engine.effective_namespace_for.assert_awaited_once()
        assert app.state.index_engine.index_file.await_args.kwargs["namespace"] == "notes"

    async def test_add_memory_normalises_the_untagged_carve_out(
        self, client: AsyncClient, app, tmp_path
    ):
        """``None`` from the resolver means "untagged", not "no answer" —
        forwarding it would re-enter rule resolution in ``index_file`` and put
        the second lookup back. The configured default stores the same state.
        """
        base = tmp_path / "memories"
        base.mkdir(exist_ok=True)
        app.state.config.indexing.memory_dirs = [base]
        app.state.storage.namespaces_for_source = AsyncMock(return_value=[])
        app.state.index_engine.effective_namespace_for = AsyncMock(return_value=None)

        resp = await client.post("/api/add", json={"content": "test", "file": "note.md"})

        assert resp.status_code == 200, resp.text
        passed = app.state.index_engine.index_file.await_args.kwargs["namespace"]
        assert passed == app.state.config.namespace.default_namespace
        assert passed is not None

    async def test_add_memory_with_an_explicit_namespace_skips_the_preflight(
        self, client: AsyncClient, app, tmp_path
    ):
        """The discriminating half: an explicit namespace short-circuits the
        resolver, so there is nothing to pre-flight and a store that cannot
        answer must not block the write."""
        from memtomem.errors import NamespaceResolutionError

        base = tmp_path / "memories"
        base.mkdir(exist_ok=True)
        app.state.config.indexing.memory_dirs = [base]
        app.state.storage.namespaces_for_source = AsyncMock(return_value=[])
        app.state.index_engine.effective_namespace_for = AsyncMock(
            side_effect=NamespaceResolutionError("store down")
        )

        resp = await client.post(
            "/api/add", json={"content": "test", "file": "note.md", "namespace": "bbb"}
        )

        assert resp.status_code == 200, resp.text

    async def test_add_memory_honours_the_namespace_mix_override(
        self, client: AsyncClient, app, tmp_path
    ):
        self._seed_mixed_target(app, tmp_path)

        resp = await client.post(
            "/api/add",
            json={
                "content": "test",
                "file": "shared.md",
                "namespace": "bbb",
                "allow_namespace_mix": True,
            },
        )

        assert resp.status_code == 200
        app.state.index_engine.index_file.assert_awaited()

    async def test_add_memory_default_target_is_per_namespace(
        self, client: AsyncClient, app, tmp_path
    ):
        """A namespaced write must not land in the shared day file."""
        from memtomem.memory_scope import day_file_name

        base = tmp_path / "memories"
        base.mkdir(exist_ok=True)
        # Into tmp_path rather than the fixture's shared ``/tmp/memories``:
        # this test writes a real file, and a shared path accumulates them
        # across runs.
        app.state.config.indexing.memory_dirs = [base]
        app.state.storage.namespaces_for_source = AsyncMock(return_value=[])

        resp = await client.post("/api/add", json={"content": "test", "namespace": "aaa"})

        assert resp.status_code == 200
        written = Path(resp.json()["file"]).name
        assert written == day_file_name(
            "aaa", app.state.config.namespace.default_namespace, date_str=written[:10]
        )
        assert not written.endswith(f"{written[:10]}.md")

    async def test_add_memory_rejects_path_traversal(self, client: AsyncClient):
        resp = await client.post(
            "/api/add",
            json={"content": "test", "file": "../../etc/passwd"},
        )
        assert resp.status_code == 422

    async def test_add_memory_writes_under_configured_memory_dirs_default(
        self, app, client: AsyncClient, tmp_path
    ):
        # /api/add must honor ``config.indexing.memory_dirs[0]`` for the
        # default-dated file, matching MCP ``mem_add``. Before the
        # write-surface parity fix the route hardcoded
        # ``~/.memtomem/memories`` and silently ignored configured dirs,
        # which meant prod users (and this test suite under a real HOME)
        # had their entries leak outside the configured corpus.
        app.state.config.indexing.memory_dirs = [tmp_path]
        resp = await client.post(
            "/api/add",
            json={"content": "Parity check."},
        )
        assert resp.status_code == 200, resp.text
        path = Path(resp.json()["file"]).resolve()
        assert tmp_path.resolve() in path.parents, (
            f"daily file {path} did not land under configured memory_dirs[0] {tmp_path}"
        )
        legacy = Path("~/.memtomem/memories").expanduser().resolve()
        assert legacy not in path.parents, (
            f"daily file regressed to hardcoded {legacy} (write-surface divergence)"
        )

    async def test_add_memory_writes_under_configured_memory_dirs_explicit_file(
        self, app, client: AsyncClient, tmp_path
    ):
        # Explicit ``file=`` (relative) must also resolve under
        # ``memory_dirs[0]``, not the legacy ``~/.memtomem/memories``.
        app.state.config.indexing.memory_dirs = [tmp_path]
        resp = await client.post(
            "/api/add",
            json={"content": "Parity check.", "file": "notes/topic.md"},
        )
        assert resp.status_code == 200, resp.text
        path = Path(resp.json()["file"]).resolve()
        assert tmp_path.resolve() in path.parents, (
            f"explicit-file write {path} did not land under {tmp_path}"
        )


# ---------------------------------------------------------------------------
# Redaction guard wire-in for the web write surfaces. The helper-level
# contract lives in ``test_privacy.py``; these cases pin that each
# surface actually invokes the guard with the right ``surface=`` label
# and that the response shape lets the SPA distinguish a redaction
# block from other 4xx outcomes (path validation, missing config, etc).
# ---------------------------------------------------------------------------


class TestAddMemoryRedaction:
    @pytest.fixture(autouse=True)
    def _reset_counters(self):
        from memtomem import privacy

        privacy.reset_for_tests()
        yield
        privacy.reset_for_tests()

    async def test_secret_returns_403_with_hits_metadata(self, client: AsyncClient):
        from memtomem import privacy

        resp = await client.post(
            "/api/add",
            json={"content": "token=sk-" + "a" * 30},
        )
        assert resp.status_code == 403, resp.text
        body = resp.json()
        # FastAPI wraps the raised ``detail`` dict under ``detail`` again.
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
        assert detail["detail"] == "redaction_blocked"
        assert detail["hits"] >= 1
        assert detail["surface"] == "web_api_add"

        snap = privacy.snapshot()["by_tool"].get("web_api_add", {})
        assert snap.get("blocked", 0) == 1

    async def test_force_unsafe_records_bypassed(self, client: AsyncClient):
        from memtomem import privacy

        resp = await client.post(
            "/api/add",
            json={
                "content": "token=sk-" + "a" * 30,
                "force_unsafe": True,
            },
        )
        assert resp.status_code == 200, resp.text
        snap = privacy.snapshot()["by_tool"]["web_api_add"]
        assert snap["bypassed"] == 1
        assert snap["blocked"] == 0

    async def test_clean_content_records_pass(self, client: AsyncClient):
        from memtomem import privacy

        resp = await client.post(
            "/api/add",
            json={"content": "Plain prose, nothing sensitive."},
        )
        assert resp.status_code == 200, resp.text
        snap = privacy.snapshot()["by_tool"]["web_api_add"]
        assert snap["pass"] == 1
        assert snap["blocked"] == 0


# ---------------------------------------------------------------------------
# POST /api/add — project-tier (ADR-0011 §5 Gate B / ADR-0016 §7) — #924
#
# Mirror the MCP ``mem_add`` Gate B at ``memory_crud.py:204`` and the Web
# parallel on the chunks DELETE path at ``chunks.py:157``. project_shared
# writes via ``/api/add`` require an explicit ``confirm_project_shared=true``;
# the 4xx payload carries the CLI hint + docs URL so the SPA renders
# "rejected, here's the equivalent invocation" without rewriting the prose.
# ---------------------------------------------------------------------------


class TestAddMemoryProjectTier:
    async def test_invalid_scope_returns_422(self, client: AsyncClient):
        """Pydantic Literal validation rejects unknown tier tokens.

        Cheap guardrail: the API only accepts the three canonical tokens —
        any typo (e.g. ``user_local``) lands a 422 before the route runs,
        so a misspelled tier can't silently fall back to user-tier writes.
        """
        resp = await client.post(
            "/api/add",
            json={"content": "x", "scope": "user_local"},
        )
        assert resp.status_code == 422, resp.text

    async def test_project_shared_without_confirm_returns_403_with_hint(self, client: AsyncClient):
        """Gate B fires before the redaction guard runs. The 4xx body must
        carry the literal CLI hint + docs URL so the SPA can render the
        rejection without rebuilding the prose client-side.
        """
        resp = await client.post(
            "/api/add",
            json={"content": "Plain prose.", "scope": "project_shared"},
        )
        assert resp.status_code == 403, resp.text
        body = resp.json()
        # FastAPI wraps the raised ``detail`` dict under ``detail`` again
        # (mirrors the redaction-error nesting at line 1685 above).
        detail = body.get("detail") if isinstance(body.get("detail"), dict) else body
        assert detail["detail"] == "blocked_project_shared"
        assert detail["surface"] == "web_api_add"
        assert detail["scope"] == "project_shared"
        assert "confirm_project_shared" in detail["message"]
        assert detail["cli_hint"] == "mm mem add --scope project_shared"
        # Docs URL must point at the canonical-residency ADR — pin the
        # filename so a re-org of the ADR tree gets caught here rather
        # than producing a silently dead link in production toasts.
        assert "0011-canonical-artifact-scope-hierarchy" in detail["docs_url"]

    async def test_project_shared_confirm_required_even_with_clean_content(
        self, client: AsyncClient
    ):
        """The redaction guard would normally let plain prose through;
        Gate B must still refuse without confirm. Pins that the gates
        compose in the right order (Gate B → redaction, not the other
        way around) so a clean payload can't sneak past Gate B.
        """
        resp = await client.post(
            "/api/add",
            json={"content": "Plain prose, no secrets.", "scope": "project_shared"},
        )
        assert resp.status_code == 403
        # Critically, the 4xx must be the project-tier shape, not
        # the redaction shape — the latter would mean Gate B never ran.
        detail = resp.json().get("detail", {})
        assert detail.get("detail") == "blocked_project_shared", (
            f"expected Gate B 4xx shape, got: {detail!r}"
        )

    async def test_project_local_bypasses_gate_b(self, app, client: AsyncClient, tmp_path):
        """ADR-0011 §3: project_local does NOT require confirm_project_shared
        — it's the draft tier (zero-to-one fan-out for non-memory artifacts;
        memory still fans out per its own contract). Only project_shared
        is git-tracked and gate-B-confirmed.
        """
        # Register the project_local tier so the route's
        # ``is_project_tier_registered`` check passes. Mirrors the
        # ``mm context memory-migrate`` registration guard the MCP add
        # path enforces at memory_crud.py:296-300.
        proj_root = tmp_path / "proj"
        local_dir = proj_root / ".memtomem" / "memories.local"
        local_dir.mkdir(parents=True)
        app.state.project_root = proj_root
        app.state.config.indexing.memory_dirs = [tmp_path / "user_mem"]
        (tmp_path / "user_mem").mkdir()
        app.state.config.indexing.project_memory_dirs = [local_dir]

        resp = await client.post(
            "/api/add",
            json={"content": "Draft note.", "scope": "project_local"},
        )
        assert resp.status_code == 200, resp.text
        path = Path(resp.json()["file"]).resolve()
        assert local_dir.resolve() in path.parents, (
            f"project_local write landed at {path}, expected under {local_dir}"
        )

    async def test_project_shared_with_confirm_routes_to_shared_dir(
        self, app, client: AsyncClient, tmp_path
    ):
        """With ``confirm_project_shared=true`` the write proceeds and
        the resolved path lands under ``<proj>/.memtomem/memories``.
        Pins write-surface parity with the MCP ``mem_add`` shared-tier
        routing at memory_crud.py:286-289.
        """
        proj_root = tmp_path / "proj"
        shared_dir = proj_root / ".memtomem" / "memories"
        shared_dir.mkdir(parents=True)
        app.state.project_root = proj_root
        app.state.config.indexing.memory_dirs = [tmp_path / "user_mem"]
        (tmp_path / "user_mem").mkdir()
        app.state.config.indexing.project_memory_dirs = [shared_dir]

        resp = await client.post(
            "/api/add",
            json={
                "content": "Team note.",
                "scope": "project_shared",
                "confirm_project_shared": True,
            },
        )
        assert resp.status_code == 200, resp.text
        path = Path(resp.json()["file"]).resolve()
        assert shared_dir.resolve() in path.parents, (
            f"project_shared write landed at {path}, expected under {shared_dir}"
        )

    async def test_project_shared_resolves_via_registered_root_not_raw_cwd(
        self, app, client: AsyncClient, tmp_path, monkeypatch
    ):
        """ADR-0011 PR-F parity with MCP ``mem_add`` (memory_crud.py:285
        via search.py:73-96): when the server runs from a subdirectory
        of a registered project, ``scope=project_shared`` must resolve
        against the registered project root, not the raw cwd. Without
        ``_resolve_project_context_from_dirs`` wiring, the route would
        land on ``<cwd>/.memtomem/memories`` (which doesn't exist /
        isn't registered) and 422 the operator — while MCP correctly
        writes under ``<project_root>/.memtomem/memories``. Codex
        review #924 Major finding.
        """
        proj_root = tmp_path / "proj"
        subdir = proj_root / "src" / "deep"
        subdir.mkdir(parents=True)
        shared_dir = proj_root / ".memtomem" / "memories"
        shared_dir.mkdir(parents=True)

        # Simulate "server launched from a subdirectory of a project".
        # The MCP path resolves project_root from cwd via
        # ``_resolve_project_context_from_dirs`` — pin that the Web
        # path now uses the same resolver, so a cwd inside the project
        # finds its way to the registered root regardless of where on
        # the tree the process was started.
        monkeypatch.chdir(subdir)
        # ``app.state.project_root`` stays at the (now-wrong) subdir
        # value the lifespan would have captured if it ran here. The
        # fix is that the route now ignores it in favor of the
        # registered-root resolver; if a regression reverts to using
        # ``app.state.project_root`` raw, this test fails because
        # ``<subdir>/.memtomem/memories`` is unregistered.
        app.state.project_root = subdir
        app.state.config.indexing.memory_dirs = [tmp_path / "user_mem"]
        (tmp_path / "user_mem").mkdir()
        app.state.config.indexing.project_memory_dirs = [shared_dir]

        resp = await client.post(
            "/api/add",
            json={
                "content": "Team note from a subdirectory.",
                "scope": "project_shared",
                "confirm_project_shared": True,
            },
        )
        assert resp.status_code == 200, resp.text
        path = Path(resp.json()["file"]).resolve()
        assert shared_dir.resolve() in path.parents, (
            f"project_shared write from subdir landed at {path}, "
            f"expected under registered root {shared_dir}"
        )

    async def test_project_tier_unregistered_returns_422(self, app, client: AsyncClient, tmp_path):
        """Refuse if the resolved project-tier dir isn't in
        ``project_memory_dirs`` — otherwise the row's persisted scope
        would flip to ``project_shared`` but the read surface / watcher
        couldn't see it. Mirrors the MCP gate at
        ``memory_crud.py:296-300``.
        """
        proj_root = tmp_path / "proj"
        proj_root.mkdir()
        app.state.project_root = proj_root
        app.state.config.indexing.memory_dirs = [tmp_path / "user_mem"]
        (tmp_path / "user_mem").mkdir()
        # Intentionally leave project_memory_dirs empty so the registration
        # check refuses the write.
        app.state.config.indexing.project_memory_dirs = []

        resp = await client.post(
            "/api/add",
            json={
                "content": "x",
                "scope": "project_shared",
                "confirm_project_shared": True,
            },
        )
        assert resp.status_code == 422, resp.text


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
        # Folder Index is an explicit one-shot scan and need not register cwd.
        resp = await client.post("/api/index")
        assert resp.status_code == 200

    async def test_trigger_index_outside_memory_dirs(self, client: AsyncClient):
        resp = await client.post("/api/index", json={"path": "/etc"})
        assert resp.status_code == 200

    async def test_trigger_index_returns_namespace_provenance(self, app, client: AsyncClient):
        """The response preserves the hybrid namespace union and identifies
        its authoritative applied subset without collapsing multi-NS runs."""
        app.state.index_engine.index_path = AsyncMock(
            return_value=IndexingStats(
                total_files=2,
                total_chunks=4,
                indexed_chunks=4,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=80.0,
                resolved_namespaces=("ns-alpha", "ns-beta"),
                applied_namespaces=("ns-alpha",),
            )
        )
        resp = await client.post("/api/index", json={"path": "/tmp/memories"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved_namespaces"] == ["ns-alpha", "ns-beta"]
        assert data["applied_namespaces"] == ["ns-alpha"]

    async def test_preview_namespace_leaf_file(self, app, client: AsyncClient):
        """Single-file path → single-element list (here: ``notes``)."""
        resp = await client.post(
            "/api/index/preview-namespace", json={"path": "/tmp/memories/note.md"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved_namespaces"] == ["notes"]
        assert data["truncated"] is False
        assert data["scanned_files"] == 1

    async def test_preview_namespace_directory_uniform(self, app, client: AsyncClient):
        """Directory where all files share one NS → 1-element list."""
        app.state.index_engine.discover_indexable_files = MagicMock(
            return_value=[
                Path("/tmp/memories/a.md"),
                Path("/tmp/memories/b.md"),
            ]
        )
        app.state.index_engine.resolve_namespaces_for = AsyncMock(return_value=["personal"])
        resp = await client.post("/api/index/preview-namespace", json={"path": "/tmp/memories"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved_namespaces"] == ["personal"]
        assert data["scanned_files"] == 2

    async def test_preview_namespace_directory_with_rule_variance(self, app, client: AsyncClient):
        """Directory with rule-divergent files → multi-element list. This
        is the test that justifies the list shape; without it the regression
        slips in silently if someone collapses to a scalar."""
        app.state.index_engine.discover_indexable_files = MagicMock(
            return_value=[
                Path("/tmp/memories/alpha/a.md"),
                Path("/tmp/memories/beta/b.md"),
            ]
        )
        app.state.index_engine.resolve_namespaces_for = AsyncMock(
            return_value=["ns-alpha", "ns-beta"]
        )
        resp = await client.post("/api/index/preview-namespace", json={"path": "/tmp/memories"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved_namespaces"] == ["ns-alpha", "ns-beta"]

    async def test_preview_namespace_directory_truncated(self, app, client: AsyncClient):
        """File walk capped at 200; truncated flag surfaces the limit so the
        UI can render ``scanned 200+`` instead of pretending exhaustiveness."""
        app.state.index_engine.discover_indexable_files = MagicMock(
            return_value=[Path(f"/tmp/memories/f{i}.md") for i in range(250)]
        )
        app.state.index_engine.resolve_namespaces_for = AsyncMock(return_value=["notes"])
        resp = await client.post("/api/index/preview-namespace", json={"path": "/tmp/memories"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["truncated"] is True
        assert data["scanned_files"] == 200
        # The mock should have been called with exactly 200 files (the cap),
        # not the full 250 — confirms the route applied the cap before
        # invoking the resolver.
        called_with = app.state.index_engine.resolve_namespaces_for.call_args.args[0]
        assert len(called_with) == 200

    async def test_preview_namespace_outside_memory_dirs(self, app, client: AsyncClient):
        """Preview mirrors the explicitly selected one-shot scan."""
        resp = await client.post("/api/index/preview-namespace", json={"path": "/etc/passwd"})
        assert resp.status_code == 200

    async def test_preview_namespace_missing_path(self, app, client: AsyncClient):
        """422 — request body requires an explicit path."""
        resp = await client.post("/api/index/preview-namespace", json={})
        assert resp.status_code == 422

    async def test_preview_namespace_get_is_not_exposed(self, client: AsyncClient):
        """The path-reading preview must remain behind unsafe-method CSRF."""
        resp = await client.get("/api/index/preview-namespace?path=/etc/passwd")
        assert resp.status_code == 404

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
# POST /api/reindex — "Reindex all" bulk route
# ---------------------------------------------------------------------------


class TestReindexAll:
    """End-to-end pin for ``POST /api/reindex``.

    The single-dir ``trigger_index`` route wraps ``Path(req.path)``
    defensively, so it survives a ``memory_dirs`` field that holds raw
    ``str`` (the shape ``load_config_overrides`` actually produces on
    disk-config load). ``reindex_all`` iterates ``all_index_roots()``
    and calls ``.expanduser()`` directly — without the helper-side
    coercion this PR adds, the route 500s on the first entry. Lock
    that contract here so a future "simplify" of ``all_index_roots()``
    can't silently re-introduce the regression.
    """

    async def test_reindex_all_with_path_dirs_returns_200(self, app, client: AsyncClient, tmp_path):
        target = tmp_path / "memdir"
        target.mkdir()
        app.state.config.indexing.memory_dirs = [target]
        app.state.config.indexing.project_memory_dirs = []

        resp = await client.post("/api/reindex")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["errors"] == []
        assert data["retryable_errors"] == []
        assert len(data["results"]) == 1
        assert data["results"][0]["path"] == str(target)
        assert data["results"][0]["errors"] == []
        assert data["results"][0]["retryable_errors"] == []

    async def test_reindex_all_with_str_dirs_returns_200(self, app, client: AsyncClient, tmp_path):
        """Regression: ``memory_dirs`` loaded from ``~/.memtomem/config.json``
        comes in as ``list[str]`` because ``load_config_overrides`` writes
        the JSON-decoded value via ``setattr`` without Pydantic validation.
        ``all_index_roots()`` MUST coerce these to ``Path`` — otherwise the
        bulk route 500s on ``str.expanduser()`` (single-dir ``/api/index``
        is unaffected because it wraps ``Path(req.path)`` at the boundary).
        """
        target = tmp_path / "memdir"
        target.mkdir()
        # Deliberately store raw strings — mirrors what
        # ``load_config_overrides`` produces.
        app.state.config.indexing.memory_dirs = [str(target)]
        app.state.config.indexing.project_memory_dirs = []

        resp = await client.post("/api/reindex")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["ok"] is True
        assert data["errors"] == []
        assert data["retryable_errors"] == []
        assert len(data["results"]) == 1
        assert data["results"][0]["path"] == str(target)

    async def test_reindex_all_preserves_retryable_subset_and_root_order(
        self, app, client: AsyncClient, tmp_path
    ):
        first, second = tmp_path / "first", tmp_path / "second"
        first.mkdir()
        second.mkdir()
        app.state.config.indexing.memory_dirs = [first, second]
        app.state.config.indexing.project_memory_dirs = []
        shared = "shared.md: chunk store unavailable"
        first_permanent = "broken.md: malformed frontmatter"
        second_retryable = "other.md: chunk store unavailable"
        app.state.index_engine.index_path = AsyncMock(
            side_effect=[
                IndexingStats(
                    total_files=2,
                    total_chunks=0,
                    indexed_chunks=0,
                    skipped_chunks=0,
                    deleted_chunks=0,
                    duration_ms=1.0,
                    errors=(shared, first_permanent),
                    retryable_errors=(shared,),
                ),
                IndexingStats(
                    total_files=2,
                    total_chunks=0,
                    indexed_chunks=0,
                    skipped_chunks=0,
                    deleted_chunks=0,
                    duration_ms=2.0,
                    errors=(shared, second_retryable),
                    retryable_errors=(shared, second_retryable),
                ),
            ]
        )

        response = await client.post("/api/reindex")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is False
        assert body["results"][0]["retryable_errors"] == [shared]
        assert body["results"][1]["retryable_errors"] == [shared, second_retryable]
        assert body["errors"] == [shared, first_permanent, shared, second_retryable]
        assert body["retryable_errors"] == [shared, shared, second_retryable]

    async def test_reindex_all_reports_the_namespace_advisory_per_root(
        self, app, client: AsyncClient, tmp_path
    ):
        """#2061: each root entry is hand-built, so the advisory reaches the
        client only by being listed there. Unconditional, like
        ``retryable_errors`` — a zero must be distinguishable from a server
        that predates the field."""
        first, second = tmp_path / "first", tmp_path / "second"
        first.mkdir()
        second.mkdir()
        app.state.config.indexing.memory_dirs = [first, second]
        app.state.config.indexing.project_memory_dirs = []
        app.state.index_engine.index_path = AsyncMock(
            side_effect=[
                IndexingStats(
                    total_files=1,
                    total_chunks=1,
                    indexed_chunks=1,
                    skipped_chunks=0,
                    deleted_chunks=0,
                    duration_ms=1.0,
                    namespaces_preserved_against_rules=2,
                ),
                IndexingStats(
                    total_files=1,
                    total_chunks=1,
                    indexed_chunks=1,
                    skipped_chunks=0,
                    deleted_chunks=0,
                    duration_ms=1.0,
                ),
            ]
        )

        response = await client.post("/api/reindex")

        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert results[0]["namespaces_preserved_against_rules"] == 2
        assert results[1]["namespaces_preserved_against_rules"] == 0
        assert results[0]["namespaces_reassigned"] == 0
        assert results[0]["namespace_moves"] == []
        assert results[1]["namespace_moves"] == []


# ---------------------------------------------------------------------------
# GET /api/indexing/active  (#582 item 4.11 follow-up — server-bound indicator)
# ---------------------------------------------------------------------------


class TestIndexingActive:
    """Tests for ``GET /api/indexing/active``.

    The endpoint reports ``IndexEngine.is_active`` so the web UI's header
    indicator (introduced in #602) survives page reloads and reaches
    second tabs. Response shape is intentionally minimal —
    ``{"active": bool}`` only — to match the client's single-bool model.
    """

    async def test_active_idle(self, app, client: AsyncClient):
        app.state.index_engine.is_active = False
        resp = await client.get("/api/indexing/active")
        assert resp.status_code == 200
        assert resp.json() == {"active": False}

    async def test_active_running(self, app, client: AsyncClient):
        app.state.index_engine.is_active = True
        resp = await client.get("/api/indexing/active")
        assert resp.status_code == 200
        assert resp.json() == {"active": True}

    async def test_no_store_cache_header(self, app, client: AsyncClient):
        """``Cache-Control: no-store`` keeps a polling client from being
        served a stale ``active=false`` by an intermediary while a run
        starts up. Mirrors ``/index/stream``'s no-cache hygiene.
        """
        app.state.index_engine.is_active = False
        resp = await client.get("/api/indexing/active")
        assert resp.headers.get("cache-control") == "no-store"


# ---------------------------------------------------------------------------
# GET /api/embedding-status
# ---------------------------------------------------------------------------


class TestEmbeddingStatus:
    async def test_no_mismatch(self, client: AsyncClient):
        resp = await client.get("/api/embedding-status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["has_mismatch"] is False

    async def test_coverage_reports_full(self, app, client: AsyncClient):
        app.state.storage.get_dense_coverage = AsyncMock(
            return_value={"total": 100, "with_dense": 100}
        )
        resp = await client.get("/api/embedding-status")
        assert resp.status_code == 200
        cov = resp.json()["coverage"]
        assert cov == {"total": 100, "with_dense": 100, "percent": 100.0}

    async def test_coverage_reports_bm25_only(self, app, client: AsyncClient):
        # The motivating failure mode: chunks indexed but ``chunks_vec``
        # never populated (embedder init crashed, NoopEmbedder fallback,
        # etc.). The UI uses this 0% signal to flag a BM25-only run.
        app.state.storage.get_dense_coverage = AsyncMock(
            return_value={"total": 100, "with_dense": 0}
        )
        resp = await client.get("/api/embedding-status")
        cov = resp.json()["coverage"]
        assert cov["total"] == 100
        assert cov["with_dense"] == 0
        assert cov["percent"] == 0.0

    async def test_coverage_partial_rounds_to_one_decimal(self, app, client: AsyncClient):
        # 1/3 -> 33.3333… ; the schema commits to one decimal so a
        # partial-coverage banner reads consistently.
        app.state.storage.get_dense_coverage = AsyncMock(return_value={"total": 3, "with_dense": 1})
        resp = await client.get("/api/embedding-status")
        assert resp.json()["coverage"]["percent"] == 33.3

    async def test_coverage_handles_empty_db(self, app, client: AsyncClient):
        app.state.storage.get_dense_coverage = AsyncMock(return_value={"total": 0, "with_dense": 0})
        cov = (await client.get("/api/embedding-status")).json()["coverage"]
        assert cov == {"total": 0, "with_dense": 0, "percent": 0.0}


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

    async def test_add_memory_dir_returns_kind(self, app, client: AsyncClient, tmp_path):
        """The add response carries ``kind`` for the resolved dir so the
        Web UI can show "Added to {kind} view — Switch?" toast when the
        user adds a path that lands in the opposite Sources sub-toggle.
        Cover both branches: newly added + already-in dedupe."""
        general_dir = tmp_path / "work" / "docs"
        general_dir.mkdir(parents=True)
        memory_dir = tmp_path / "memories"
        memory_dir.mkdir()
        app.state.config.indexing.memory_dirs = [general_dir]

        with patch("memtomem.web.routes.system.save_config_overrides"):
            # ``general_dir`` is already in ``memory_dirs`` → exercise
            # the dedupe branch and confirm ``kind`` rides on it.
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(general_dir)},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["message"] == "Already in memory_dirs"
            assert body["kind"] == "general"

            # Newly added dir with a ``memories`` segment → exercise the
            # add branch and confirm ``kind=memory``.
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(memory_dir)},
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["kind"] == "memory"
            assert body["message"].startswith("Added ")

    async def test_add_memory_dir_returns_kind_when_config_empty(
        self, app, client: AsyncClient, tmp_path
    ):
        """Pin the empty-config first-add path: a fresh install has
        ``memory_dirs=[]``, so the dedupe branch never fires and the
        kind must come back from the add branch alone. Otherwise the
        UI's "Switch view" toast would lose its trigger on the very
        first dir a new user registers."""
        app.state.config.indexing.memory_dirs = []
        target = tmp_path / "memories"
        target.mkdir()

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(target)},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["kind"] == "memory"
        assert body["message"].startswith("Added ")

    async def test_add_memory_dir_auto_index_triggers_index_path(
        self, app, client: AsyncClient, tmp_path
    ):
        """``auto_index=true`` collapses register + index into one call.
        After a successful add, ``index_path`` runs on the registered dir
        and the response carries the ``indexed`` stats block. The watcher
        invariant (path inside ``memory_dirs``) is satisfied because the
        register block ran first inside the same handler."""
        memory_dir = tmp_path / "memories"
        memory_dir.mkdir()
        app.state.config.indexing.memory_dirs = []
        # The shared fixture mocks ``index_path`` to return the stub stats
        # block; reset the call list so we can assert on it.
        app.state.index_engine.index_path.reset_mock()

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(memory_dir), "auto_index": True},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["message"].startswith("Added ")
        assert body["indexed"] is not None
        assert body["indexed"]["indexed_chunks"] == 2
        assert body["indexed"]["total_files"] == 1
        assert body["indexed"]["retryable_errors"] == []
        assert body["index_status"] == "success"
        # ``index_path`` was called with the resolved path of the dir we
        # just added — watcher invariant naturally satisfied.
        called_args, _ = app.state.index_engine.index_path.call_args
        assert Path(str(called_args[0])).resolve() == memory_dir.resolve()

    async def test_add_memory_dir_auto_index_surfaces_retryable_error_subset(
        self, app, client: AsyncClient, tmp_path
    ):
        memory_dir = tmp_path / "memories"
        memory_dir.mkdir()
        app.state.config.indexing.memory_dirs = []
        permanent = "broken.md: malformed frontmatter"
        retryable = "transient.md: chunk store unavailable"
        app.state.index_engine.index_path = AsyncMock(
            return_value=IndexingStats(
                total_files=2,
                total_chunks=1,
                indexed_chunks=1,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=1.0,
                errors=(permanent, retryable),
                retryable_errors=(retryable,),
            )
        )

        with patch("memtomem.web.routes.system.save_config_overrides"):
            response = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(memory_dir), "auto_index": True},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["index_status"] == "partial"
        assert body["indexed"]["errors"] == [permanent, retryable]
        assert body["indexed"]["retryable_errors"] == [retryable]

    async def test_add_memory_dir_retryable_raise_keeps_its_classification(
        self, app, client: AsyncClient, tmp_path
    ):
        """The pre-write namespace prepass raises instead of returning stats,
        and the generic handler flattened it to ``{"error": "Initial indexing
        failed"}`` — dropping the retryability for the one failure class the
        split exists to describe. The message must stay path-free: this
        response is not path-safe (see the sibling ``/private/secret`` pin)."""
        from memtomem.errors import NamespaceResolutionError

        memory_dir = tmp_path / "memories"
        memory_dir.mkdir()
        app.state.config.indexing.memory_dirs = []
        app.state.index_engine.index_path = AsyncMock(
            side_effect=NamespaceResolutionError(f"lookup failed for {memory_dir}")
        )

        with patch("memtomem.web.routes.system.save_config_overrides"):
            response = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(memory_dir), "auto_index": True},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["index_status"] == "failed"
        assert body["indexed"]["retryable_errors"] == body["indexed"]["errors"]
        assert len(body["indexed"]["retryable_errors"]) == 1
        assert "Retry once it is reachable" in body["indexed"]["error"]
        assert str(memory_dir) not in json.dumps(body["indexed"])

    async def test_add_memory_dir_permanent_raise_carries_empty_retryable(
        self, app, client: AsyncClient, tmp_path
    ):
        """Counterpart to the above: a non-retryable failure must still carry
        both keys, so a client can tell "not retryable" from "old server"."""
        memory_dir = tmp_path / "memories"
        memory_dir.mkdir()
        app.state.config.indexing.memory_dirs = []
        app.state.index_engine.index_path = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("memtomem.web.routes.system.save_config_overrides"):
            response = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(memory_dir), "auto_index": True},
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["index_status"] == "failed"
        assert body["indexed"]["errors"] == []
        assert body["indexed"]["retryable_errors"] == []

    async def test_add_memory_dir_default_omitted_indexes(self, app, client: AsyncClient, tmp_path):
        """**The ``auto_index`` default is ``True``** (flipped in
        PR #576) — omitting the field triggers indexing. Locks the
        new default semantics: without this test, a future regression
        flip back to ``False`` would only fail the explicit-false
        test (which doesn't actually exercise the omit-path default).

        Naming intentionally describes the *input shape* (``omitted``)
        rather than the behavior (``auto_indexes``) so the test name
        doesn't lie if the default ever moves again — only the
        assertions need updating."""
        memory_dir = tmp_path / "memories"
        memory_dir.mkdir()
        app.state.config.indexing.memory_dirs = []
        app.state.index_engine.index_path.reset_mock()

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(memory_dir)},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["indexed"] is not None
        assert app.state.index_engine.index_path.call_count == 1

    async def test_add_memory_dir_explicit_false_skips_index(
        self, app, client: AsyncClient, tmp_path
    ):
        """Opt-out: explicit ``auto_index=false`` preserves
        register-only behavior for direct-API callers that want the
        historic two-step (register, then ``/api/index``)."""
        memory_dir = tmp_path / "memories"
        memory_dir.mkdir()
        app.state.config.indexing.memory_dirs = []
        app.state.index_engine.index_path.reset_mock()

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(memory_dir), "auto_index": False},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["indexed"] is None
        assert body["index_status"] == "not_requested"
        assert app.state.index_engine.index_path.call_count == 0

    async def test_add_memory_dir_explicit_null_skips_index(
        self, app, client: AsyncClient, tmp_path
    ):
        """JSON ``null`` is treated as opt-out (``bool(None) == False``),
        distinct from field omission. This lock is **intentional, not
        incidental** — locks the contract for clients that send all
        fields with ``null`` placeholders. If a future PR wants
        ``null`` to mean 'use default', that's a contract change:
        update this test, the ``add_memory_dir`` handler docstring in
        ``packages/memtomem/src/memtomem/web/routes/system.py``, and
        add a CHANGELOG entry."""
        memory_dir = tmp_path / "memories"
        memory_dir.mkdir()
        app.state.config.indexing.memory_dirs = []
        app.state.index_engine.index_path.reset_mock()

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(memory_dir), "auto_index": None},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["indexed"] is None
        assert body["index_status"] == "not_requested"
        assert app.state.index_engine.index_path.call_count == 0

    async def test_add_memory_dir_reports_failed_index_without_lying_about_registration(
        self, app, client: AsyncClient, tmp_path
    ):
        memory_dir = tmp_path / "memories"
        memory_dir.mkdir()
        app.state.config.indexing.memory_dirs = []
        app.state.index_engine.index_path.side_effect = RuntimeError("/private/secret/path failed")

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(memory_dir), "auto_index": True},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["index_status"] == "failed"
        assert "/private/secret" not in body["indexed"]["error"]

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

    async def test_index_stream_accepts_explicit_sibling_without_registering_it(
        self, app, client: AsyncClient, tmp_path
    ):
        # Explicit indexing is a one-shot operation and may target an external
        # path. It must not mutate the configured watcher roots.
        bar_dir = tmp_path / "bar"
        bar_dir.mkdir()
        barbaz_dir = tmp_path / "barbaz"
        barbaz_dir.mkdir()
        app.state.config.indexing.memory_dirs = [bar_dir]

        async def _fake_stream(*args, **kwargs):
            assert kwargs["path_scope"] == "explicit"
            yield {"type": "complete", "indexed": 0}

        app.state.index_engine.index_path_stream = _fake_stream
        resp = await client.post("/api/index/stream", json={"path": str(barbaz_dir)})
        assert resp.status_code == 200, resp.text
        assert app.state.config.indexing.memory_dirs == [bar_dir]

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
        resp = await client.post("/api/index/stream", json={"path": str(nfc_path)})
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
# ADR-0006 PR-B — bulk-index force_unsafe route threading
# ---------------------------------------------------------------------------


class TestBulkIndexForceUnsafe:
    """PR-B threads the Web UI's ``force_unsafe`` override into PR-A's engine
    gate. The bypass is a security downgrade, so it rides only CSRF-protected
    POSTs — the Index folder mode via ``POST /api/index`` (``trigger_index``)
    and Sources "+ Add path" via ``POST /api/memory-dirs/add``. It is
    deliberately **not** reachable through the token-exempt ``GET
    /api/index/stream`` SSE surface. These pin the forwarding, the safe default,
    the GET non-bypass, and strict boolean parsing on the raw-body POST."""

    async def test_trigger_index_forwards_force_unsafe(self, app, client: AsyncClient, tmp_path):
        app.state.config.indexing.memory_dirs = [tmp_path]
        app.state.index_engine.index_path.reset_mock()

        resp = await client.post("/api/index", json={"path": str(tmp_path), "force_unsafe": True})
        assert resp.status_code == 200, resp.text
        app.state.index_engine.index_path.assert_awaited()
        assert app.state.index_engine.index_path.await_args.kwargs["force_unsafe"] is True

    async def test_trigger_index_defaults_force_unsafe_false(
        self, app, client: AsyncClient, tmp_path
    ):
        app.state.config.indexing.memory_dirs = [tmp_path]
        app.state.index_engine.index_path.reset_mock()

        resp = await client.post("/api/index", json={"path": str(tmp_path)})
        assert resp.status_code == 200, resp.text
        app.state.index_engine.index_path.assert_awaited()
        assert app.state.index_engine.index_path.await_args.kwargs["force_unsafe"] is False

    @pytest.mark.parametrize("value", ["false", "true", "yes", 1, 0])
    async def test_trigger_index_non_literal_true_stays_false(
        self, app, client: AsyncClient, tmp_path, value
    ):
        """Only a JSON literal ``true`` enables the bypass. Pydantic's default
        ``bool`` would coerce ``"true"`` / ``"yes"`` / ``1`` to True; the strict
        validator makes every non-literal value fail closed to False so an
        ambiguous payload can't flip the redaction override on."""
        app.state.config.indexing.memory_dirs = [tmp_path]
        app.state.index_engine.index_path.reset_mock()

        resp = await client.post("/api/index", json={"path": str(tmp_path), "force_unsafe": value})
        assert resp.status_code == 200, resp.text
        assert app.state.index_engine.index_path.await_args.kwargs["force_unsafe"] is False

    async def test_index_stream_forwards_force_unsafe_bypass(
        self, app, client: AsyncClient, tmp_path
    ):
        """The POST SSE stream carries the reviewed bypass in its JSON body."""
        app.state.config.indexing.memory_dirs = [tmp_path]
        calls: list[dict] = []

        async def _fake_stream(resolved, **kwargs):
            calls.append(kwargs)
            yield {"type": "complete", "blocked_files": 0}

        app.state.index_engine.index_path_stream = MagicMock(side_effect=_fake_stream)

        resp = await client.post(
            "/api/index/stream", json={"path": str(tmp_path), "force_unsafe": True}
        )
        assert resp.status_code == 200, resp.text
        assert calls, "index_path_stream was never called"
        assert calls[0]["force_unsafe"] is True

    async def test_legacy_get_stream_is_405_without_engine_work(
        self, app, client: AsyncClient, tmp_path
    ):
        app.state.index_engine.index_path_stream = MagicMock()
        resp = await client.get("/api/index/stream", params={"path": str(tmp_path)})
        assert resp.status_code == 405
        app.state.index_engine.index_path_stream.assert_not_called()

    async def test_add_memory_dir_string_false_stays_false(
        self, app, client: AsyncClient, tmp_path
    ):
        """Raw-body POST parses ``force_unsafe`` strictly: the JSON string
        ``"false"`` must stay False, not become truthy via ``bool("false")``."""
        target = tmp_path / "notes"
        target.mkdir()
        app.state.config.indexing.memory_dirs = []
        app.state.index_engine.index_path.reset_mock()

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(target), "auto_index": True, "force_unsafe": "false"},
            )
        assert resp.status_code == 200, resp.text
        assert app.state.index_engine.index_path.await_args.kwargs["force_unsafe"] is False

    async def test_add_memory_dir_forwards_force_unsafe(self, app, client: AsyncClient, tmp_path):
        target = tmp_path / "notes"
        target.mkdir()
        app.state.config.indexing.memory_dirs = []
        app.state.index_engine.index_path.reset_mock()

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(target), "auto_index": True, "force_unsafe": True},
            )
        assert resp.status_code == 200, resp.text
        app.state.index_engine.index_path.assert_awaited()
        assert app.state.index_engine.index_path.await_args.kwargs["force_unsafe"] is True

    async def test_add_memory_dir_defaults_force_unsafe_false(
        self, app, client: AsyncClient, tmp_path
    ):
        target = tmp_path / "notes"
        target.mkdir()
        app.state.config.indexing.memory_dirs = []
        app.state.index_engine.index_path.reset_mock()

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(target), "auto_index": True},
            )
        assert resp.status_code == 200, resp.text
        app.state.index_engine.index_path.assert_awaited()
        assert app.state.index_engine.index_path.await_args.kwargs["force_unsafe"] is False


# ---------------------------------------------------------------------------
# GET /api/index/stream — SSE error-event redaction
# ---------------------------------------------------------------------------


class TestIndexStreamErrorRedaction:
    """Engine-level exceptions that escape to the SSE generator must pass
    through ``_redact_message`` before serialization. Per-file read/permission
    errors never reach this handler — the engine catches them internally and
    reports basenames in the ``complete`` event — but an *engine-level*
    failure (e.g. a storage error) propagates to the route's ``except`` and a
    raw ``str(exc)`` embedding an absolute path would leak ``$HOME`` (and the
    OS username) to the client. Every other error surface at this trust
    boundary routes through ``_redact_message`` or a fixed string; the SSE
    error event must not be the exception."""

    async def test_index_stream_error_event_redacts_home_path(
        self, app, client: AsyncClient, tmp_path, monkeypatch
    ):
        app.state.config.indexing.memory_dirs = [tmp_path]
        # ``_errors._HOME`` is captured at import time from ``Path.home()``,
        # so pin it to a fake home rather than patching the env.
        fake_home = "/home/leaky-user"
        monkeypatch.setattr("memtomem.web.routes._errors._HOME", fake_home)

        async def _boom(*args, **kwargs):
            raise RuntimeError(f"database is locked: {fake_home}/.memtomem/index.db")
            yield  # pragma: no cover — makes this an async generator function

        app.state.index_engine.index_path_stream = _boom

        resp = await client.post("/api/index/stream", json={"path": str(tmp_path)})
        assert resp.status_code == 200, resp.text
        events = [
            json.loads(line.removeprefix("data: "))
            for line in resp.text.splitlines()
            if line.startswith("data: ")
        ]
        error_events = [e for e in events if e.get("type") == "error"]
        assert error_events, f"no error event in stream: {resp.text!r}"
        message = error_events[0]["message"]
        assert fake_home not in message, f"raw $HOME path leaked to the client: {message!r}"
        assert "~/.memtomem/index.db" in message, f"expected redacted form, got: {message!r}"


# ---------------------------------------------------------------------------
# POST /api/index/stream — consumer abandonment
# ---------------------------------------------------------------------------


class TestIndexStreamAbandonment:
    """#2200: a client disconnect must release the indexing run it started.

    ``index_path_stream`` releases ``_active_runs`` and the #2180 generation
    lease in its own ``finally``, which runs only when that generator is
    closed — otherwise ``GET /api/indexing/active`` keeps reporting a run that
    has stopped and a retired ONNX session stays pinned until asyncio's
    async-generator finalizer (a *scheduled* ``aclose``) gets around to it.

    Two layers have to hold for that to work, and each has a test below:

    1. The route's ``_generate()`` wrapper drives the engine stream through
       ``contextlib.aclosing``, so closing the wrapper closes the engine's
       generator in the same unwind.
    2. The response closes ``_generate()`` at all. Starlette does not — it
       unwinds ``stream_response``, and a cancellation landing on ``send``
       (backpressure: the SSE client stopped reading) leaves the body
       generator suspended at its ``yield``. ``_ClosingStreamingResponse``
       closes it on every exit from ``__call__``.

    Layer 1 alone passes a test that closes the body iterator by hand, which
    is exactly the step layer 2 exists to guarantee — so the second test
    drives the real ASGI disconnect, against the real engine.
    """

    async def test_closing_the_response_closes_the_engine_stream(self, app, tmp_path):
        from memtomem.web.routes.system import index_stream
        from memtomem.web.schemas.memory import IndexRequest

        released: list[str] = []

        async def _stream(*args, **kwargs):
            # Stands in for the engine's ``_active_run`` context manager: the
            # release is in a ``finally``, so it is observable only if
            # something closes this generator.
            try:
                yield {"type": "discovery", "files_total": 2}
                yield {"type": "progress", "file": "a.md", "files_total": 2}
            finally:
                released.append("released")

        app.state.index_engine.index_path_stream = _stream

        response = await index_stream(
            IndexRequest(path=str(tmp_path)),
            index_engine=app.state.index_engine,
            search_pipeline=app.state.search_pipeline,
        )
        body = response.body_iterator
        first = await body.__anext__()
        assert "discovery" in first
        assert released == [], "engine stream released before the consumer left"

        # The disconnect: the outer generator is closed, the inner one is
        # never touched directly.
        await body.aclose()

        assert released == ["released"], (
            "engine stream still open after the response was closed — the run "
            "and its generation lease leak until GC"
        )

    async def test_disconnect_while_send_blocks_releases_the_real_run(self, components, memory_dir):
        """The end-to-end contract, with nothing stubbed between the client
        and the engine: a disconnect arriving while the response is blocked
        in ``send`` must drop ``_active_runs`` and release the generation
        lease before ``__call__`` returns.

        This is the window Starlette leaves open — the cancellation lands on
        ``send``, not on the body generator's ``__anext__``, so nothing throws
        into ``_generate()`` and its ``aclosing`` never runs on its own.
        """
        from memtomem.web.routes.system import index_stream
        from memtomem.web.schemas.memory import IndexRequest

        for i in range(3):
            (memory_dir / f"note{i}.md").write_text(f"# Note {i}\n\nBody.")
        engine = components.index_engine
        generation = components.generation

        response = await index_stream(
            IndexRequest(path=str(memory_dir)),
            index_engine=engine,
            search_pipeline=SimpleNamespace(invalidate_cache=lambda: None),
        )

        sending = asyncio.Event()
        held: list[tuple[int, int]] = []

        async def send(message):
            if message["type"] == "http.response.body":
                # Backpressure: the client is no longer reading. The response
                # parks here, holding the run, until the disconnect arrives.
                # Sample the counters here — asserting only that they end at
                # zero would pass against a response that never started a run
                # at all, since zero is also their initial value.
                held.append((engine._active_runs, generation.leases))
                sending.set()
                await asyncio.Event().wait()

        async def receive():
            await sending.wait()
            return {"type": "http.disconnect"}

        scope = {
            "type": "http",
            # uvicorn's current value; it selects Starlette's task-group path,
            # where the disconnect cancels the streaming task rather than
            # raising out of ``send``.
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "method": "POST",
            "path": "/api/index/stream",
            "headers": [],
        }

        await asyncio.wait_for(response(scope, receive, send), timeout=10)

        assert held == [(1, 1)], (
            "the run and its generation lease were not both held while the "
            f"response was parked in send — sampled {held}; without this the "
            "assertions below pass on a response that never indexed anything"
        )
        assert engine._active_runs == 0, (
            "the disconnected run is still counted — /api/indexing/active "
            "reports indexing that has stopped"
        )
        assert generation.leases == 0, (
            "the disconnected run still pins its component generation — a "
            "retired ONNX session cannot be closed"
        )

    @pytest.mark.parametrize(
        "cleanup_error",
        [RuntimeError("cleanup exploded"), asyncio.CancelledError()],
        ids=["exception", "cancelled"],
    )
    async def test_failing_cleanup_does_not_replace_the_original_error(
        self, app, tmp_path, cleanup_error
    ):
        """Closing the body is cleanup, so it must not become the error the
        server sees. The reason the response is unwinding — here a send
        failure surfacing as ``ClientDisconnect`` — is what classifies the
        request; a body whose own cleanup fails must not take its place.

        The ``cancelled`` case is the one a plain ``except Exception`` misses:
        ``CancelledError`` is a ``BaseException``, so cleanup interrupted by a
        second cancellation would escape and replace the original.
        """
        from starlette.responses import ClientDisconnect

        from memtomem.web.routes.system import index_stream
        from memtomem.web.schemas.memory import IndexRequest

        cleaned_up: list[str] = []

        async def _stream(*args, **kwargs):
            try:
                yield {"type": "discovery", "files_total": 1}
            finally:
                # Records that cleanup ran at all: without this the test still
                # passes against a response that never closes its body on the
                # exceptional path, since the disconnect propagates either way.
                cleaned_up.append("ran")
                raise cleanup_error

        app.state.index_engine.index_path_stream = _stream

        response = await index_stream(
            IndexRequest(path=str(tmp_path)),
            index_engine=app.state.index_engine,
            search_pipeline=app.state.search_pipeline,
        )

        async def send(message):
            if message["type"] == "http.response.body":
                # Starlette's spec-2.4 branch translates an ``OSError`` out of
                # ``send`` into ``ClientDisconnect``. (Uvicorn 0.49 advertises
                # spec 2.3 and takes the task-group branch instead; this test
                # drives the 2.4 shape because it is the one that unwinds
                # through ``__call__`` with an exception to preserve.)
                raise OSError("connection reset")

        async def receive():
            return {"type": "http.disconnect"}

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "method": "POST",
            "path": "/api/index/stream",
            "headers": [],
        }

        with pytest.raises(ClientDisconnect):
            await response(scope, receive, send)

        assert cleaned_up == ["ran"], "the body was never closed on the exceptional path"


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

    async def test_response_path_resolves_symlink(
        self, app, client: AsyncClient, tmp_path: Path
    ) -> None:
        # Wizard-written config never goes through ``/api/memory-dirs/add``,
        # so a symlinked prefix (e.g. macOS ``/tmp`` → ``/private/tmp``)
        # lands in ``config.indexing.memory_dirs`` unresolved. Frontend
        # ``STATE.memoryDirs`` keys come from ``/api/config`` (resolved),
        # so the status response must also return the resolved form or
        # the per-row badge lookup misses (#666).
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        app.state.config.indexing.memory_dirs = [link]

        resp = await client.get("/api/memory-dirs/status")
        assert resp.status_code == 200, resp.text
        dirs = resp.json()["dirs"]
        assert len(dirs) == 1
        assert dirs[0]["path"] == str(real.resolve())

    async def test_path_matches_config_endpoint(
        self, app, client: AsyncClient, tmp_path: Path
    ) -> None:
        # Cross-endpoint parity guard. ``/api/config`` and
        # ``/api/memory-dirs/status`` are read by the same frontend
        # render pass (``STATE.memoryDirs`` keyed against
        # ``STATE.memoryStatusByPath``); any future divergence in their
        # path canonicalization re-introduces #666 with the same
        # symptoms (per-row badge missing). Pin the parity invariant
        # directly so the regression doesn't have to surface through
        # the UI again.
        real = tmp_path / "x"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)

        app.state.config.indexing.memory_dirs = [link]

        cfg_resp = await client.get("/api/config")
        sts_resp = await client.get("/api/memory-dirs/status")
        assert cfg_resp.status_code == 200, cfg_resp.text
        assert sts_resp.status_code == 200, sts_resp.text

        cfg_dirs = cfg_resp.json()["indexing"]["memory_dirs"]
        sts_dirs = sts_resp.json()["dirs"]
        assert len(cfg_dirs) == 1
        assert len(sts_dirs) == 1
        assert sts_dirs[0]["path"] == cfg_dirs[0]

    async def test_response_path_resolves_tilde(
        self,
        app,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Pins the invariant the docstring originally guarded — a config
        # entry like ``~/memories`` must come back as the expanded
        # absolute path, not the literal tilde form (#666). ``HOME`` is
        # the POSIX home var; Windows ``Path.expanduser()`` reads
        # ``USERPROFILE`` first and ignores ``HOME``, so monkeypatch
        # both for cross-platform coverage.
        set_home(monkeypatch, tmp_path)
        target = tmp_path / "memories"
        target.mkdir()

        app.state.config.indexing.memory_dirs = ["~/memories"]

        resp = await client.get("/api/memory-dirs/status")
        assert resp.status_code == 200, resp.text
        dirs = resp.json()["dirs"]
        assert len(dirs) == 1
        assert dirs[0]["path"] == str(target.resolve())


class TestOpenMemoryDir:
    """``POST /api/memory-dirs/open`` reveals a registered dir in the OS
    file manager. Whitelist-gated against ``memory_dirs`` so the route
    can't be coerced into spawning a file manager pointed at arbitrary
    filesystem paths even if ``mm web`` were ever bound to a non-loopback
    interface."""

    async def test_rejects_path_not_in_memory_dirs(
        self, app, client: AsyncClient, tmp_path: Path
    ) -> None:
        registered = tmp_path / "registered"
        registered.mkdir()
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        app.state.config.indexing.memory_dirs = [registered]

        with patch("memtomem.web.routes.system._open_in_file_manager") as opener:
            resp = await client.post(
                "/api/memory-dirs/open",
                json={"path": str(elsewhere)},
            )
        assert resp.status_code == 404, resp.text
        opener.assert_not_called()

    async def test_rejects_missing_dir_on_disk(
        self, app, client: AsyncClient, tmp_path: Path
    ) -> None:
        # Path is registered but the directory has been removed from disk
        # — opening would either fail at the OS level or pop a confusing
        # "location not available" dialog. 404 short-circuits cleanly.
        ghost = tmp_path / "ghost"
        app.state.config.indexing.memory_dirs = [ghost]

        with patch("memtomem.web.routes.system._open_in_file_manager") as opener:
            resp = await client.post(
                "/api/memory-dirs/open",
                json={"path": str(ghost)},
            )
        assert resp.status_code == 404, resp.text
        opener.assert_not_called()

    async def test_opens_registered_dir(self, app, client: AsyncClient, tmp_path: Path) -> None:
        target = tmp_path / "target"
        target.mkdir()
        app.state.config.indexing.memory_dirs = [target]

        with patch("memtomem.web.routes.system._open_in_file_manager") as opener:
            resp = await client.post(
                "/api/memory-dirs/open",
                json={"path": str(target)},
            )
        assert resp.status_code == 200, resp.text
        opener.assert_called_once()
        # The path passed to the helper should be the resolved target.
        called_with = opener.call_args.args[0]
        assert called_with == target.resolve()


class TestRemoveMemoryDirChunkCleanup:
    """``POST /api/memory-dirs/remove`` with ``delete_chunks=true`` must
    drop every chunk under the resolved dir prefix; the default keeps
    chunks searchable so the Web UI's checkbox-opt-in stays the safe
    path. Mirrors the dir-level UX: removing a watch entry is reversible
    until the user explicitly elects chunk cleanup."""

    async def test_default_does_not_delete_chunks(
        self, app, client: AsyncClient, tmp_path: Path
    ) -> None:
        target = tmp_path / "going-away"
        keep = tmp_path / "keep-this"
        target.mkdir()
        keep.mkdir()
        app.state.config.indexing.memory_dirs = [target, keep]

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/remove",
                json={"path": str(target)},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted_chunks"] == 0
        app.state.storage.delete_by_source.assert_not_called()

    async def test_delete_chunks_true_removes_matching_source_files(
        self, app, client: AsyncClient, tmp_path: Path
    ) -> None:
        target = tmp_path / "going-away"
        keep = tmp_path / "keep-this"
        target.mkdir()
        keep.mkdir()
        app.state.config.indexing.memory_dirs = [target, keep]

        # Two source files under ``target`` (should be deleted) plus one
        # under ``keep`` (must be left alone). ``delete_by_source`` is
        # mocked to return 2 chunks per file, so the route should report
        # 4 deleted total.
        under_target_a = target / "a.md"
        under_target_b = target / "sub" / "b.md"
        under_keep = keep / "k.md"
        app.state.storage.get_source_files_with_counts.return_value = [
            (under_target_a, 2, "2026-04-29T00:00:00", "default", 100, 50, 200),
            (under_target_b, 2, "2026-04-29T00:00:00", "default", 100, 50, 200),
            (under_keep, 5, "2026-04-29T00:00:00", "default", 100, 50, 200),
        ]
        app.state.storage.delete_by_source = AsyncMock(return_value=2)

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/remove",
                json={"path": str(target), "delete_chunks": True},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["deleted_chunks"] == 4
        # Two calls — one per matching source file. The ``keep`` file
        # must NOT trigger a delete.
        assert app.state.storage.delete_by_source.call_count == 2
        deleted_paths = [call.args[0] for call in app.state.storage.delete_by_source.call_args_list]
        assert under_target_a in deleted_paths
        assert under_target_b in deleted_paths
        assert under_keep not in deleted_paths

    # ---------------------------------------------------------------------------
    # POST /api/upload — redaction guard wire-in
    # ---------------------------------------------------------------------------

    async def test_partial_sweep_failure_still_invalidates_the_committed_deletes(
        self, app, client: AsyncClient, tmp_path: Path
    ) -> None:
        """#2141 class: the first source's chunks are gone from the store even
        though a later source blows the request up, so a warmed query must not
        keep returning them."""
        target = tmp_path / "going-away"
        keep = tmp_path / "keep-this"
        target.mkdir()
        keep.mkdir()
        app.state.config.indexing.memory_dirs = [target, keep]
        app.state.storage.get_source_files_with_counts.return_value = [
            (target / "a.md", 2, "2026-04-29T00:00:00", "default", 100, 50, 200),
            (target / "b.md", 2, "2026-04-29T00:00:00", "default", 100, 50, 200),
        ]
        app.state.storage.delete_by_source = AsyncMock(side_effect=[2, RuntimeError("store blip")])
        app.state.search_pipeline.invalidate_cache.reset_mock()

        with (
            patch("memtomem.web.routes.system.save_config_overrides"),
            pytest.raises(RuntimeError, match="store blip"),
        ):
            await client.post(
                "/api/memory-dirs/remove",
                json={"path": str(target), "delete_chunks": True},
            )

        assert app.state.storage.delete_by_source.await_count == 2
        assert app.state.search_pipeline.invalidate_cache.call_count == 1


class TestUploadRedaction:
    @pytest.fixture(autouse=True)
    def _reset_counters(self):
        from memtomem import privacy

        privacy.reset_for_tests()
        yield
        privacy.reset_for_tests()

    async def test_secret_file_rejected_per_file(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from memtomem import privacy

        set_home(monkeypatch, tmp_path)
        files = [
            (
                "files",
                ("clean.md", b"Just regular notes.", "text/markdown"),
            ),
            (
                "files",
                ("secret.md", b"token=sk-" + b"a" * 30, "text/markdown"),
            ),
        ]
        resp = await client.post("/api/upload", files=files)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        per_file = {r["filename"]: r for r in body["files"]}
        assert per_file["secret.md"]["error"].startswith("redaction_blocked")
        assert per_file["secret.md"]["indexed_chunks"] == 0
        assert per_file["clean.md"].get("error") in (None, "")

        snap = privacy.snapshot()["by_tool"]["web_api_upload"]
        assert snap["blocked"] == 1
        assert snap["pass"] == 1
        # The blocked file must not have been written.
        assert not (tmp_path / ".memtomem" / "uploads" / "secret.md").exists()

    async def test_force_unsafe_query_param_bypasses_for_batch(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from memtomem import privacy

        set_home(monkeypatch, tmp_path)
        files = [
            (
                "files",
                ("secret.md", b"token=sk-" + b"a" * 30, "text/markdown"),
            ),
        ]
        resp = await client.post(
            "/api/upload?force_unsafe=true",
            files=files,
        )
        assert resp.status_code == 200, resp.text
        snap = privacy.snapshot()["by_tool"]["web_api_upload"]
        assert snap["bypassed"] == 1

    async def test_file_count_overflow_is_413_before_any_write(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        set_home(monkeypatch, tmp_path)
        files = [("files", (f"{idx}.md", b"safe", "text/markdown")) for idx in range(33)]
        resp = await client.post("/api/upload", files=files)
        assert resp.status_code == 413
        assert not (tmp_path / ".memtomem" / "uploads").exists()

    async def test_request_content_length_cap_is_413_before_parsing(self, client: AsyncClient):
        resp = await client.post(
            "/api/upload",
            content=b"x",
            headers={"content-length": str(202 * 1024 * 1024)},
        )
        assert resp.status_code == 413
        assert resp.json() == {"detail": "Upload request too large"}

    @pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
    async def test_saved_file_and_parent_are_owner_only(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        set_home(monkeypatch, tmp_path)
        resp = await client.post(
            "/api/upload",
            files=[("files", ("safe.md", b"safe", "text/markdown"))],
        )
        assert resp.status_code == 200, resp.text
        parent = tmp_path / ".memtomem" / "uploads"
        assert stat.S_IMODE(parent.stat().st_mode) == 0o700
        assert stat.S_IMODE((parent / "safe.md").stat().st_mode) == 0o600


# ---------------------------------------------------------------------------
# POST /api/scratch/{key}/promote — redaction guard wire-in
# ---------------------------------------------------------------------------


class TestScratchPromoteRedaction:
    @pytest.fixture(autouse=True)
    def _reset_counters(self):
        from memtomem import privacy

        privacy.reset_for_tests()
        yield
        privacy.reset_for_tests()

    async def test_secret_in_promoted_value_returns_403(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        from memtomem import privacy

        # Promote pulls the value from storage; wire a secret through the mock.
        app.state.storage.scratch_get = AsyncMock(
            return_value={"key": "k", "value": "token=sk-" + "a" * 30},
        )
        app.state.storage.scratch_promote = AsyncMock()

        target = tmp_path / "today.md"
        app.state.config.indexing.memory_dirs = [tmp_path]

        resp = await client.post(
            "/api/scratch/k/promote",
            json={"file": str(target)},
        )
        assert resp.status_code == 403, resp.text
        snap = privacy.snapshot()["by_tool"]["web_api_scratch_promote"]
        assert snap["blocked"] == 1
        # The blocked promotion must NOT mark the entry promoted in storage.
        app.state.storage.scratch_promote.assert_not_called()

    async def test_clean_value_records_pass(self, app, client: AsyncClient, tmp_path: Path):
        from memtomem import privacy

        app.state.storage.scratch_get = AsyncMock(
            return_value={"key": "k", "value": "Plain prose, nothing sensitive."},
        )
        app.state.storage.scratch_promote = AsyncMock()
        app.state.config.indexing.memory_dirs = [tmp_path]
        target = tmp_path / "today.md"

        with patch("memtomem.tools.memory_writer.append_entry"):
            resp = await client.post(
                "/api/scratch/k/promote",
                json={"file": str(target)},
            )
        assert resp.status_code == 200, resp.text
        snap = privacy.snapshot()["by_tool"]["web_api_scratch_promote"]
        assert snap["pass"] == 1


# ---------------------------------------------------------------------------
# GET /api/uploads/usage  (issue #583)
# ---------------------------------------------------------------------------


class TestUploadsUsage:
    """Cumulative-footprint endpoint for ``~/.memtomem/uploads/``.

    Read-only directory stat, no ``require_configured`` gate — it must
    return a zero-state response on a fresh install (no ``~/.memtomem/``
    yet) so the UI panel can decide to hide vs. surface from a single
    fetch. ``Path.expanduser()`` reads ``$HOME`` per call on POSIX, so
    ``monkeypatch.setenv('HOME', tmp_path)`` cleanly isolates each case.
    """

    async def test_home_memtomem_missing(
        self,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Fresh install — ``~/.memtomem`` itself does not exist."""
        set_home(monkeypatch, tmp_path)
        resp = await client.get("/api/uploads/usage")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"file_count": 0, "total_bytes": 0, "oldest_mtime": None}

    async def test_uploads_subdir_missing(
        self,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Config wizard ran but no upload yet — ``.memtomem/`` exists,
        ``uploads/`` does not. Same code path as the missing-HOME case
        but a distinct user state worth pinning."""
        set_home(monkeypatch, tmp_path)
        (tmp_path / ".memtomem").mkdir()
        resp = await client.get("/api/uploads/usage")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"file_count": 0, "total_bytes": 0, "oldest_mtime": None}

    async def test_populated(
        self,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import os

        set_home(monkeypatch, tmp_path)
        upload_dir = tmp_path / ".memtomem" / "uploads"
        upload_dir.mkdir(parents=True)
        a = upload_dir / "a.md"
        a.write_bytes(b"x" * 10)
        b = upload_dir / "b.md"
        b.write_bytes(b"y" * 25)
        # Pin mtimes — older first so ``oldest_mtime`` is deterministic.
        os.utime(a, (1_700_000_000, 1_700_000_000))
        os.utime(b, (1_700_005_000, 1_700_005_000))

        resp = await client.get("/api/uploads/usage")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["file_count"] == 2
        assert body["total_bytes"] == 35
        assert body["oldest_mtime"] == pytest.approx(1_700_000_000)

    async def test_subdirectories_ignored(
        self,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """``is_file()`` filter must skip nested dirs so a stray
        directory doesn't inflate ``file_count``."""
        set_home(monkeypatch, tmp_path)
        upload_dir = tmp_path / ".memtomem" / "uploads"
        upload_dir.mkdir(parents=True)
        (upload_dir / "real.md").write_bytes(b"hello")
        (upload_dir / "stray-subdir").mkdir()

        resp = await client.get("/api/uploads/usage")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["file_count"] == 1
        assert body["total_bytes"] == 5


# ---------------------------------------------------------------------------
# require_configured gate (issue #577)
# ---------------------------------------------------------------------------


class TestRequireConfigured:
    """Mutating index routes refuse with HTTP 409 when ``mm init`` has
    not run, mirroring the CLI bootstrap gate at
    ``cli/_bootstrap.py``. Without this gate ``mm web`` accepts
    ``+ 경로 추가`` clicks against a fresh HOME and returns
    ``indexed: {total_files: 0, ...}`` silently — confusing dead-end
    for the user (issue #577).

    These tests *restore* the gate (the shared ``app`` fixture
    overrides it to ``lambda: None`` so all the unrelated FakeConfig
    tests don't depend on the developer's real
    ``~/.memtomem/config.json``) and monkeypatch ``HOME`` to control
    the predicate."""

    @pytest.fixture
    def restore_gate(self, app):
        from memtomem.web.deps import require_configured

        del app.dependency_overrides[require_configured]
        # No teardown: ``app`` is function-scoped per pytest's default,
        # so the next test gets a freshly-built app with the override
        # already re-installed by the shared ``app`` fixture.
        yield

    async def test_memory_dirs_add_409_when_no_config(
        self,
        app,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_gate,
    ):
        """Fresh HOME with no ``~/.memtomem/config.json`` → 409 with
        the same message ``mm index`` prints. ``index_path`` must
        not be invoked (gate runs *before* indexing, so a regression
        that moves the gate after ``index_path`` would catch the
        artifact-only assertion but fail this one)."""
        set_home(monkeypatch, tmp_path)
        app.state.index_engine.index_path.reset_mock()

        target = tmp_path / "target"
        target.mkdir()
        resp = await client.post(
            "/api/memory-dirs/add",
            json={"path": str(target)},
        )
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == ("memtomem is not configured. Run 'mm init' to set up.")
        assert app.state.index_engine.index_path.call_count == 0

    async def test_index_409_when_no_config(
        self,
        app,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_gate,
    ):
        """``POST /api/index`` is the second path the issue calls out
        (the manual reindex trigger). Same gate, same message."""
        set_home(monkeypatch, tmp_path)
        app.state.index_engine.index_path.reset_mock()

        target = tmp_path / "target"
        target.mkdir()
        resp = await client.post("/api/index", json={"path": str(target)})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == ("memtomem is not configured. Run 'mm init' to set up.")
        assert app.state.index_engine.index_path.call_count == 0

    async def test_indexing_active_409_when_no_config(
        self,
        app,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_gate,
    ):
        """``GET /api/indexing/active`` shares the same gate as the rest
        of the indexing surface (``/index``, ``/index/stream``,
        ``/reindex``) — uniform 409 on a not-yet-configured server.
        """
        set_home(monkeypatch, tmp_path)
        resp = await client.get("/api/indexing/active")
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == ("memtomem is not configured. Run 'mm init' to set up.")

    async def test_memory_dirs_add_passes_when_config_exists(
        self,
        app,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_gate,
    ):
        """Same gate, configured HOME (``~/.memtomem/config.json``
        exists) → request proceeds normally."""
        set_home(monkeypatch, tmp_path)
        cfg_dir = tmp_path / ".memtomem"
        cfg_dir.mkdir()
        (cfg_dir / "config.json").write_text("{}")

        target = tmp_path / "target"
        target.mkdir()
        app.state.config.indexing.memory_dirs = []
        app.state.index_engine.index_path.reset_mock()

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(target), "auto_index": False},
            )
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize(
        "method,path,kwargs",
        [
            ("post", "/api/index/stream", {"json": {"path": "/tmp/x"}}),
            ("post", "/api/reindex", {}),
            (
                "post",
                "/api/upload",
                {"files": [("files", ("x.md", b"content", "text/markdown"))]},
            ),
            ("post", "/api/add", {"json": {"text": "hello", "source": "/tmp/x"}}),
        ],
        ids=["index/stream", "reindex", "upload", "add"],
    )
    async def test_other_gated_routes_return_409_when_no_config(
        self,
        app,
        client: AsyncClient,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        restore_gate,
        method,
        path,
        kwargs,
    ):
        """Per-route 409 coverage for the 4 remaining gated routes.
        ``dependencies=[]`` is per-route, so a regression that drops
        the dep on ``/reindex`` (say) without dropping it on
        ``/memory-dirs/add`` would still pass the deep tests above —
        these parametrized cases lock the perimeter."""
        set_home(monkeypatch, tmp_path)
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == ("memtomem is not configured. Run 'mm init' to set up.")


# ---------------------------------------------------------------------------
# GET /api/fs/list — Index-tab folder picker (issue #582 4.12)
# ---------------------------------------------------------------------------


@pytest.mark.requires_symlinks
class TestFsList:
    """Exercise the picker endpoint's allow-list, symlink, and i18n
    boundary handling. The endpoint isn't a security gate — ``mm web`` is
    localhost-bound and the user can still type any path into the Index
    input. These tests pin the *picker scope* contract: only allow-listed
    descendants navigate, symlinks pointing out are excluded so users never
    click an entry and hit a 422, broken symlinks don't sink the whole
    listing, and macOS NFD vs NFC for non-ASCII directory names compares
    equal.
    """

    @pytest.fixture
    def fs_tree(self, tmp_path: Path):
        """Build a small allow-listed tree with edge-case entries.

        Layout (``home`` and ``outside`` are siblings so the picker's HOME
        root genuinely doesn't cover ``outside``)::

            tmp_path/
              home/               (HOME for these tests)
                memdir/           (registered as memory_dir)
                  alpha/
                  beta/
                  .hidden/
                  empty/
                  한글노트/       (Korean dirname — NFD form on disk if macOS)
                  ln_inside  -> alpha
                  ln_outside -> /etc
                  ln_broken  -> nowhere
                  a_file.md
              outside/            (NOT in allow-list)
                target/
        """
        home = tmp_path / "home"
        home.mkdir()
        memdir = home / "memdir"
        outside = tmp_path / "outside"
        (memdir / "alpha").mkdir(parents=True)
        (memdir / "beta").mkdir()
        (memdir / ".hidden").mkdir()
        (memdir / "empty").mkdir()
        korean_nfc = unicodedata.normalize("NFC", "한글노트")
        (memdir / korean_nfc).mkdir()
        (memdir / "a_file.md").write_text("hello")
        (outside / "target").mkdir(parents=True)
        (memdir / "ln_inside").symlink_to(memdir / "alpha", target_is_directory=True)
        (memdir / "ln_outside").symlink_to(Path("/etc"), target_is_directory=True)
        (memdir / "ln_broken").symlink_to(memdir / "no_such_target")
        return {"home": home, "memdir": memdir, "outside": outside}

    def _wire_memory_dirs(self, app, dirs: list[Path], monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setattr(app.state.config.indexing, "memory_dirs", dirs)

    async def test_roots_no_path_or_empty(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        # HOME goes first, then memory_dirs in config order, deduped.
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        # Same dir twice + Home (= tmp_path) — the dedup must collapse to two.
        self._wire_memory_dirs(app, [memdir, memdir], monkeypatch)

        resp_none = await client.get("/api/fs/list")
        resp_empty = await client.get("/api/fs/list?path=")
        assert resp_none.status_code == 200
        assert resp_empty.status_code == 200
        assert resp_none.json() == resp_empty.json()

        body = resp_none.json()
        assert body["is_root"] is True
        assert body["path"] is None
        assert body["parent"] is None
        # Order: Home first, then memdir; duplicate collapsed.
        norm_paths = [e["path"] for e in body["entries"]]
        assert len(norm_paths) == 2
        assert Path(norm_paths[0]).name == fs_tree["home"].name
        assert Path(norm_paths[1]) == Path(unicodedata.normalize("NFC", str(memdir.resolve())))

    async def test_subdirs_inside_allow_list(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        resp = await client.get(f"/api/fs/list?path={memdir}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["is_root"] is False
        names = [e["name"] for e in body["entries"]]
        # Sorted case-insensitively. ln_outside excluded (target outside).
        # ln_broken excluded (OSError on resolve / is_dir).
        # a_file.md excluded (not a dir).
        assert "alpha" in names
        assert "beta" in names
        assert ".hidden" in names  # hidden visible by default
        assert "empty" in names
        assert "ln_inside" in names  # symlink → alpha (inside) kept
        assert "ln_outside" not in names
        assert "ln_broken" not in names
        assert "a_file.md" not in names

    async def test_path_param_tilde_expansion(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        # Pretend the memory_dir lives under a fake HOME.
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        rel = memdir.relative_to(fs_tree["home"])
        resp = await client.get(f"/api/fs/list?path=~/{rel}")
        assert resp.status_code == 200, resp.text
        names = [e["name"] for e in resp.json()["entries"]]
        assert "alpha" in names

    async def test_path_param_dotdot_resolved_inside(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        # /…/memdir/alpha/../beta resolves to /…/memdir/beta — inside.
        path = f"{memdir}/alpha/../beta"
        resp = await client.get(f"/api/fs/list?path={path}")
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_root"] is False

    async def test_outside_allow_list_422(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        outside = fs_tree["outside"] / "target"
        resp = await client.get(f"/api/fs/list?path={outside}")
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"] == "outside_picker_scope"

    async def test_project_purpose_adds_project_root_parent_without_changing_default(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Add Project can discover sibling project folders outside memory_dirs.

        The default picker still rejects the same path, preserving the Index
        tab's memory-dir scope. ``purpose=project`` adds the server project
        root's parent, which is enough for the user to choose sibling checkouts
        without browsing from filesystem root.
        """
        memdir = fs_tree["memdir"]
        outside = fs_tree["outside"]
        project_root = outside / "server-cwd"
        project_root.mkdir()
        app.state.project_root = project_root
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        default = await client.get(f"/api/fs/list?path={outside / 'target'}")
        assert default.status_code == 422, default.text

        roots = await client.get("/api/fs/list?purpose=project")
        assert roots.status_code == 200, roots.text
        root_paths = [Path(e["path"]) for e in roots.json()["entries"]]
        assert Path(unicodedata.normalize("NFC", str(outside.resolve()))) in root_paths

        scoped = await client.get(f"/api/fs/list?path={outside / 'target'}&purpose=project")
        assert scoped.status_code == 200, scoped.text
        assert scoped.json()["path"] == unicodedata.normalize("NFC", str(outside / "target"))

    async def test_project_purpose_adds_known_project_parents(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        known_parent = tmp_path / "known-family"
        known_project = known_parent / "project-a"
        known_project.mkdir(parents=True)
        known_projects_path = tmp_path / "known_projects.json"
        KnownProjectsStore(known_projects_path).add(known_project)
        app.state.config.context_gateway = SimpleNamespace(known_projects_path=known_projects_path)
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        default = await client.get(f"/api/fs/list?path={known_project}")
        assert default.status_code == 422, default.text

        scoped = await client.get(f"/api/fs/list?path={known_project}&purpose=project")
        assert scoped.status_code == 200, scoped.text

    async def test_project_purpose_drops_known_project_at_filesystem_root(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """A known project registered at a top-level dir must not widen to ``/``.

        ``_project_allow_list_roots`` guards the server cwd's parent against
        collapsing to the filesystem anchor; ``_known_project_parent_roots``
        needs the same guard so a stale ``/foo`` entry can't sidestep it.
        """
        memdir = fs_tree["memdir"]
        known_projects_path = tmp_path / "known_projects.json"

        anchor = Path(Path(memdir).anchor)
        top_level = anchor / "memtomem-test-top-level"
        # Bypass KnownProjectsStore.add so the test never has to create or
        # touch a real top-level directory.
        known_projects_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "projects": [{"root": str(top_level), "added_at": "2026-01-01T00:00:00Z"}],
                }
            ),
            encoding="utf-8",
        )
        app.state.config.context_gateway = SimpleNamespace(known_projects_path=known_projects_path)
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        roots = await client.get("/api/fs/list?purpose=project")
        assert roots.status_code == 200, roots.text
        root_paths = {Path(e["path"]) for e in roots.json()["entries"]}
        assert anchor not in root_paths

    async def test_project_purpose_still_excludes_symlink_out(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        resp = await client.get(f"/api/fs/list?path={memdir}&purpose=project")
        assert resp.status_code == 200, resp.text
        names = [e["name"] for e in resp.json()["entries"]]
        assert "ln_outside" not in names

    async def test_invalid_picker_purpose_400(
        self,
        client: AsyncClient,
    ):
        resp = await client.get("/api/fs/list?purpose=everything")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "invalid_picker_purpose"

    async def test_nonexistent_404(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        resp = await client.get(f"/api/fs/list?path={memdir}/no_such_subdir")
        assert resp.status_code == 404
        assert resp.json()["detail"] == "not_found"

    async def test_file_not_dir_400(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        resp = await client.get(f"/api/fs/list?path={memdir}/a_file.md")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "not_a_directory"

    async def test_hidden_dirs_visible(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        resp = await client.get(f"/api/fs/list?path={memdir}")
        names = [e["name"] for e in resp.json()["entries"]]
        assert ".hidden" in names

    async def test_permission_error_skipped(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        import os
        import sys

        if sys.platform == "win32":
            pytest.skip("chmod 000 not meaningful on Windows")
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        guarded = memdir / "guarded"
        guarded.mkdir()
        os.chmod(guarded, 0o000)
        try:
            resp = await client.get(f"/api/fs/list?path={memdir}")
            assert resp.status_code == 200
            names = [e["name"] for e in resp.json()["entries"]]
            # The directory itself is still a dir (chmod doesn't hide it),
            # but iterdir on it would fail. The endpoint listing the parent
            # still returns the rest.
            assert "alpha" in names
        finally:
            os.chmod(guarded, 0o755)

    async def test_broken_symlink_skipped(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        resp = await client.get(f"/api/fs/list?path={memdir}")
        assert resp.status_code == 200
        names = [e["name"] for e in resp.json()["entries"]]
        assert "ln_broken" not in names
        # Listing still completed despite the broken symlink.
        assert "alpha" in names

    async def test_symlink_inside_allow_list_kept(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        resp = await client.get(f"/api/fs/list?path={memdir}")
        entries = {e["name"]: e["path"] for e in resp.json()["entries"]}
        assert "ln_inside" in entries
        # The response carries the symlink path itself (NFC-normalised),
        # not the resolve target. Without this, ln_inside (-> alpha) would
        # surface as alpha's absolute path and clicking the row would
        # write the target into #index-path instead of the symlink the
        # user actually saw in the tree.
        expected_symlink_path = unicodedata.normalize("NFC", str(memdir / "ln_inside"))
        assert entries["ln_inside"] == expected_symlink_path

    async def test_navigate_symlink_keeps_symbolic_prefix(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        """Navigating into ``ln_inside`` (-> alpha) returns a listing
        whose ``path`` is the symlink path, not the resolve target. The
        breadcrumb on the frontend stays anchored to what the user
        clicked, and ``Up`` returns them to the symlink's parent rather
        than teleporting them to wherever the target lives.
        """
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        ln = memdir / "ln_inside"
        resp = await client.get(f"/api/fs/list?path={ln}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        expected_path = unicodedata.normalize("NFC", str(ln))
        expected_parent = unicodedata.normalize("NFC", str(memdir))
        assert body["path"] == expected_path
        assert body["parent"] == expected_parent

    async def test_symlink_outside_allow_list_excluded(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        resp = await client.get(f"/api/fs/list?path={memdir}")
        names = [e["name"] for e in resp.json()["entries"]]
        assert "ln_outside" not in names

    async def test_subdirs_with_korean_dirname(
        self,
        app,
        client: AsyncClient,
        fs_tree,
        monkeypatch: pytest.MonkeyPatch,
    ):
        memdir = fs_tree["memdir"]
        set_home(monkeypatch, fs_tree["home"])
        self._wire_memory_dirs(app, [memdir], monkeypatch)

        # Listing the memdir surfaces the Korean entry.
        resp = await client.get(f"/api/fs/list?path={memdir}")
        names = [e["name"] for e in resp.json()["entries"]]
        korean_names = [n for n in names if "한" in unicodedata.normalize("NFC", n)]
        assert korean_names, names

        # Querying with the NFC form of the path navigates into it even when
        # the on-disk form may be NFD (macOS APFS). norm_path normalises both
        # sides so the boundary check matches regardless of input form.
        nfc_path = unicodedata.normalize("NFC", str(memdir / "한글노트"))
        resp2 = await client.get(f"/api/fs/list?path={nfc_path}")
        assert resp2.status_code == 200, resp2.text


class TestChunkCrudCrossProcessLock:
    """#1587: web chunk edit/delete and add hold the source file's cross-process
    sidecar (L2) across the read → rewrite → reindex span. These pin the new
    failure surfaces — timeout → 503, concurrent migration → 409, and the edit
    rollback the web path previously lacked.
    """

    def _chunk_on(self, source: Path, cid: uuid.uuid4 | None = None) -> Chunk:
        c = _make_test_chunk(chunk_id=cid, source=str(source))
        return c.__class__(
            content=c.content,
            metadata=c.metadata.__class__(
                source_file=source,
                heading_hierarchy=("## H",),
                tags=c.metadata.tags,
                namespace=c.metadata.namespace,
                start_line=1,
                end_line=3,
            ),
            id=c.id,
            content_hash=c.content_hash,
            embedding=c.embedding,
            created_at=c.created_at,
            updated_at=c.updated_at,
        )

    async def test_edit_chunk_returns_503_when_sidecar_held(
        self, app, client: AsyncClient, tmp_path: Path, monkeypatch
    ):
        from memtomem.context._atomic import _lock_path_for, async_file_lock

        src = tmp_path / "note.md"
        src.write_text("## H\n\nbody\n", encoding="utf-8")
        chunk = self._chunk_on(src)
        app.state.storage.get_chunk = AsyncMock(return_value=chunk)
        monkeypatch.setattr("memtomem.context._atomic._CRUD_SIDECAR_LOCK_BUDGET_S", 0.2)

        async with async_file_lock(_lock_path_for(src.resolve()), timeout=5.0):
            resp = await client.patch(f"/api/chunks/{chunk.id}", json={"new_content": "updated"})
        assert resp.status_code == 503
        # File untouched — the edit never ran.
        assert src.read_text(encoding="utf-8") == "## H\n\nbody\n"

    @pytest.mark.requires_symlinks
    async def test_edit_chunk_rechecks_symlink_on_fresh_chunk(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """The symlink refusal is re-evaluated on the chunk re-fetched under the
        lock: a concurrent re-point to a symlink (resolving to the same target,
        so not reported as "moved") must still be refused with 403."""
        real = tmp_path / "real.md"
        real.write_text("## H\n\nbody\n", encoding="utf-8")
        link = tmp_path / "link.md"
        link.symlink_to(real)
        cid = uuid.uuid4()
        # Pre-check + unlocked fetch see the real (non-symlink) path; the
        # under-lock re-fetch sees the symlink (resolves to the same target).
        app.state.storage.get_chunk = AsyncMock(
            side_effect=[
                self._chunk_on(real, cid),
                self._chunk_on(real, cid),
                self._chunk_on(link, cid),
            ]
        )
        resp = await client.patch(f"/api/chunks/{cid}", json={"new_content": "updated"})
        assert resp.status_code == 403
        # The real file was not edited.
        assert real.read_text(encoding="utf-8") == "## H\n\nbody\n"

    async def test_edit_chunk_returns_409_when_file_moved(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        srca = tmp_path / "a.md"
        srcb = tmp_path / "b.md"
        srca.write_text("## H\n\nbody\n", encoding="utf-8")
        srcb.write_text("## H\n\nbody\n", encoding="utf-8")
        cid = uuid.uuid4()
        # Three fetches: the route's symlink/404 pre-check and locked_source_chunk's
        # unlocked fetch both see a.md; the re-fetch under the lock sees b.md
        # (a concurrent migrate moved it) → the route reports 409 retry.
        app.state.storage.get_chunk = AsyncMock(
            side_effect=[
                self._chunk_on(srca, cid),
                self._chunk_on(srca, cid),
                self._chunk_on(srcb, cid),
            ]
        )
        resp = await client.patch(f"/api/chunks/{cid}", json={"new_content": "updated"})
        assert resp.status_code == 409

    async def test_edit_chunk_rolls_back_file_on_reindex_failure(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        src = tmp_path / "note.md"
        original = "## H\n\nold body\n"
        src.write_text(original, encoding="utf-8")
        chunk = self._chunk_on(src)
        app.state.storage.get_chunk = AsyncMock(return_value=chunk)
        # Forward reindex raises; the rollback reindex (2nd call) succeeds.
        app.state.index_engine.index_file = AsyncMock(
            side_effect=[
                RuntimeError("boom"),
                IndexingStats(
                    total_files=1,
                    total_chunks=1,
                    indexed_chunks=1,
                    skipped_chunks=0,
                    deleted_chunks=0,
                    duration_ms=1.0,
                ),
            ]
        )

        resp = await client.patch(f"/api/chunks/{chunk.id}", json={"new_content": "new body"})
        assert resp.status_code == 500
        # The failed edit was rolled back — original bytes restored.
        assert src.read_text(encoding="utf-8") == original

    async def test_delete_chunk_returns_503_when_sidecar_held(
        self, app, client: AsyncClient, tmp_path: Path, monkeypatch
    ):
        from memtomem.context._atomic import _lock_path_for, async_file_lock

        src = tmp_path / "note.md"
        src.write_text("## H\n\nbody\n", encoding="utf-8")
        chunk = self._chunk_on(src)
        app.state.storage.get_chunk = AsyncMock(return_value=chunk)
        monkeypatch.setattr("memtomem.context._atomic._CRUD_SIDECAR_LOCK_BUDGET_S", 0.2)

        async with async_file_lock(_lock_path_for(src.resolve()), timeout=5.0):
            resp = await client.delete(f"/api/chunks/{chunk.id}")
        assert resp.status_code == 503

    async def test_add_memory_returns_503_when_sidecar_held(
        self, app, client: AsyncClient, tmp_path: Path, monkeypatch
    ):
        from memtomem.context._atomic import _lock_path_for, async_file_lock

        app.state.config.indexing.memory_dirs = [tmp_path]
        monkeypatch.setattr("memtomem.context._atomic._CRUD_SIDECAR_LOCK_BUDGET_S", 0.2)
        target = (tmp_path / "pinned.md").resolve()

        async with async_file_lock(_lock_path_for(target), timeout=5.0):
            resp = await client.post("/api/add", json={"content": "hello", "file": "pinned.md"})
        assert resp.status_code == 503


class TestConfigErrorHandler:
    """#1768 — a loadable-but-unusable configuration surfaces as 409 with a
    field-naming detail, not the opaque generic 500."""

    async def test_config_error_surfaces_as_409_with_field_name(self, app, client: AsyncClient):
        from fastapi.routing import APIRoute

        from memtomem.errors import ConfigError
        from memtomem.memory_scope import EMPTY_MEMORY_DIRS_ERROR

        async def _boom():
            raise ConfigError(EMPTY_MEMORY_DIRS_ERROR)

        # Insert ahead of the dev-mode SPA catch-all, which would
        # otherwise shadow any route appended after app creation.
        app.router.routes.insert(0, APIRoute("/api/__test-config-error", _boom, methods=["GET"]))

        resp = await client.get("/api/__test-config-error")
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert "indexing.memory_dirs is empty" in detail

    async def test_api_add_empty_memory_dirs_409_without_legacy_fallback_write(
        self, app, client: AsyncClient
    ):
        """Pre-fix ``/api/add`` substituted ``~/.memtomem/memories`` when
        ``memory_dirs`` was empty and wrote there (#1768)."""
        app.state.config.indexing.memory_dirs = []
        resp = await client.post("/api/add", json={"content": "hello"})
        assert resp.status_code == 409
        assert "indexing.memory_dirs is empty" in resp.json()["detail"]

    async def test_scratch_promote_default_target_empty_memory_dirs_409(
        self, app, client: AsyncClient
    ):
        """Pre-fix the default (no ``file``) promotion crashed on ``bases[0]``
        → generic 500; must be 409 with no write and no promote mark."""
        app.state.config.indexing.memory_dirs = []
        app.state.storage.scratch_get = AsyncMock(
            return_value={"key": "note", "value": "promote me"}
        )
        app.state.storage.scratch_promote = AsyncMock()

        with patch("memtomem.tools.memory_writer.append_entry") as appender:
            resp = await client.post("/api/scratch/note/promote", json={})
        assert resp.status_code == 409
        assert "indexing.memory_dirs is empty" in resp.json()["detail"]
        appender.assert_not_called()
        app.state.storage.scratch_promote.assert_not_called()


class TestDeleteSourceParity:
    """#2081: ``DELETE /api/sources`` is the web twin of
    ``mem_delete(source_file=...)`` and had drifted from it on two points —
    it never invalidated the search cache (deleted chunks stayed served from
    a cached page until the entry aged out) and it applied no ADR-0011 Gate-B
    confirmation before taking ``project_shared`` chunks with it.
    """

    SOURCE = Path("/tmp/memories/shared-note.md")

    def _indexed(self, app) -> None:
        app.state.storage.get_all_source_files.return_value = [self.SOURCE]

    async def test_delete_invalidates_search_cache(self, app, client: AsyncClient):
        """Assert on the pipeline itself: an empty result set would also be
        produced by a cache that is merely stale, so absence proves nothing.
        """
        self._indexed(app)
        app.state.storage.list_scopes_by_source = AsyncMock(return_value={"user"})
        app.state.search_pipeline.invalidate_cache = MagicMock()

        resp = await client.delete("/api/sources", params={"path": str(self.SOURCE)})

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"deleted": 1}
        app.state.search_pipeline.invalidate_cache.assert_called_once_with()

    async def test_user_scope_source_deletes_without_confirmation(self, app, client: AsyncClient):
        """The common path must not grow a confirmation round-trip."""
        self._indexed(app)
        app.state.storage.list_scopes_by_source = AsyncMock(return_value={"user"})

        resp = await client.delete("/api/sources", params={"path": str(self.SOURCE)})

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"deleted": 1}
        app.state.storage.delete_by_source.assert_awaited_once()

    async def test_project_shared_without_flag_performs_no_write(self, app, client: AsyncClient):
        """Gate-B refusal is an application state (200 + envelope), and the
        refusal must be proven by the absent delete, not by the body alone.
        """
        self._indexed(app)
        app.state.storage.list_scopes_by_source = AsyncMock(return_value={"user", "project_shared"})
        app.state.search_pipeline.invalidate_cache = MagicMock()

        resp = await client.delete("/api/sources", params={"path": str(self.SOURCE)})

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "needs_confirmation"
        # The client must learn the flag name from the envelope, never by
        # parsing the prose.
        assert body["confirm"] == "confirm_project_shared"
        assert body["scopes"] == ["project_shared", "user"]
        app.state.storage.delete_by_source.assert_not_awaited()
        app.state.search_pipeline.invalidate_cache.assert_not_called()

    async def test_project_shared_with_flag_deletes(self, app, client: AsyncClient):
        self._indexed(app)
        app.state.storage.list_scopes_by_source = AsyncMock(return_value={"project_shared"})
        app.state.search_pipeline.invalidate_cache = MagicMock()

        resp = await client.delete(
            "/api/sources",
            params={"path": str(self.SOURCE), "confirm_project_shared": "true"},
        )

        assert resp.status_code == 200, resp.text
        assert resp.json() == {"deleted": 1}
        app.state.storage.delete_by_source.assert_awaited_once()
        app.state.search_pipeline.invalidate_cache.assert_called_once_with()

    @pytest.mark.parametrize("flag", ["false", "0", ""])
    async def test_project_shared_falsy_flag_still_gated(self, app, client: AsyncClient, flag):
        """Presence of the parameter is not consent — only a truthy value is."""
        self._indexed(app)
        app.state.storage.list_scopes_by_source = AsyncMock(return_value={"project_shared"})

        resp = await client.delete(
            "/api/sources",
            params={"path": str(self.SOURCE), "confirm_project_shared": flag},
        )

        assert resp.status_code in (200, 422), resp.text
        if resp.status_code == 200:
            assert resp.json()["status"] == "needs_confirmation"
        app.state.storage.delete_by_source.assert_not_awaited()

    async def test_scope_probe_runs_before_the_delete(self, app, client: AsyncClient):
        """The gate must consult storage, not a caller-supplied hint: a flag
        alone must never be able to skip the probe."""
        self._indexed(app)
        probe = AsyncMock(return_value={"project_shared"})
        app.state.storage.list_scopes_by_source = probe

        await client.delete(
            "/api/sources",
            params={"path": str(self.SOURCE), "confirm_project_shared": "true"},
        )

        probe.assert_awaited_once()


class TestIndexRoutesInvalidateSearchCache:
    """#2141: every long-lived index surface must drop the search result TTL
    cache. The web process holds one pipeline for its lifetime, so a query
    warmed before an index run would otherwise keep answering from the
    pre-index cache for up to ``search.cache_ttl``."""

    @staticmethod
    def _unmutated(**over):
        base = dict(
            total_files=1,
            total_chunks=2,
            indexed_chunks=0,
            skipped_chunks=2,
            deleted_chunks=0,
            duration_ms=1.0,
        )
        base.update(over)
        return IndexingStats(**base)

    async def test_trigger_index_invalidates(self, app, client: AsyncClient):
        app.state.search_pipeline.invalidate_cache.reset_mock()

        resp = await client.post("/api/index", json={"path": "/tmp/memories"})

        assert resp.status_code == 200
        assert app.state.search_pipeline.invalidate_cache.call_count == 1

    async def test_trigger_index_steady_state_does_not_invalidate(self, app, client: AsyncClient):
        app.state.index_engine.index_path = AsyncMock(return_value=self._unmutated())
        app.state.search_pipeline.invalidate_cache.reset_mock()

        resp = await client.post("/api/index", json={"path": "/tmp/memories"})

        assert resp.status_code == 200
        assert app.state.search_pipeline.invalidate_cache.call_count == 0

    async def test_reindex_all_invalidates_per_root(self, app, client: AsyncClient, tmp_path):
        app.state.config.indexing.memory_dirs = [str(tmp_path)]
        app.state.search_pipeline.invalidate_cache.reset_mock()

        resp = await client.post("/api/reindex")

        assert resp.status_code == 200
        assert app.state.search_pipeline.invalidate_cache.call_count >= 1

    async def test_index_stream_invalidates_even_without_a_complete_event(
        self, app, client: AsyncClient
    ):
        """Unconditional and in ``finally``: a client can disconnect after a
        file's chunk transaction commits but before its progress event is
        produced, so the flag never reaches the route. Dropping a still-valid
        cache costs one cold search; keeping a stale one hides a write."""

        async def _one_event(*args, **kwargs):
            yield {"type": "discovery", "files_total": 1}

        app.state.index_engine.index_path_stream = _one_event
        app.state.search_pipeline.invalidate_cache.reset_mock()

        resp = await client.post("/api/index/stream", json={"path": "/tmp/memories"})

        assert resp.status_code == 200
        assert app.state.search_pipeline.invalidate_cache.call_count == 1

    async def test_add_memory_dir_auto_index_invalidates(self, app, client: AsyncClient, tmp_path):
        memory_dir = tmp_path / "memories"
        memory_dir.mkdir()
        app.state.config.indexing.memory_dirs = []
        app.state.search_pipeline.invalidate_cache.reset_mock()

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.post(
                "/api/memory-dirs/add",
                json={"path": str(memory_dir), "auto_index": True},
            )

        assert resp.status_code == 200, resp.text
        assert app.state.search_pipeline.invalidate_cache.call_count == 1

    async def test_edit_chunk_invalidates_on_the_namespace_503_branch(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """That branch's "nothing was changed" is a claim about the *file*:
        restoring the pre-image runs a rollback re-index, which is itself a
        write. The MCP twin invalidates on its rollback path; so must this."""
        from memtomem.errors import NamespaceResolutionError

        source = tmp_path / "edit.md"
        source.write_text("# Heading\n\nBody one.\nBody two.\nBody three.\n", encoding="utf-8")
        chunk = _make_test_chunk(source=str(source))
        app.state.storage.get_chunk = AsyncMock(return_value=chunk)
        app.state.index_engine.index_file = AsyncMock(
            side_effect=NamespaceResolutionError("store down")
        )
        app.state.search_pipeline.invalidate_cache.reset_mock()

        resp = await client.patch(f"/api/chunks/{CHUNK_ID}", json={"new_content": "rewritten body"})

        assert resp.status_code == 503, resp.text
        assert app.state.search_pipeline.invalidate_cache.call_count == 1

    async def test_delete_chunk_invalidates(self, app, client: AsyncClient, tmp_path: Path):
        chunk = _make_test_chunk(source=str(tmp_path / "missing.md"))
        app.state.storage.get_chunk = AsyncMock(side_effect=[chunk, chunk, chunk, chunk, None])
        app.state.search_pipeline.invalidate_cache.reset_mock()

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code == 200
        assert app.state.search_pipeline.invalidate_cache.call_count == 1

    async def test_delete_chunk_invalidates_when_verification_fails(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """The commit-then-500 shape: the rows are gone but the verification
        read fails, so the caller sees an error. A warmed query must not keep
        returning the deleted chunk."""
        chunk = _make_test_chunk(source=str(tmp_path / "missing.md"))
        # The row is still present on the read before the delete, so the
        # route deletes it; the *post-delete* verification read is what blows
        # up — rows gone, caller gets a 500.
        app.state.storage.get_chunk = AsyncMock(
            side_effect=[chunk, chunk, chunk, chunk, RuntimeError("store blip")]
        )
        app.state.search_pipeline.invalidate_cache.reset_mock()

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code >= 500
        app.state.storage.delete_chunks.assert_awaited_once_with([chunk.id])
        assert app.state.search_pipeline.invalidate_cache.call_count == 1

    async def test_delete_chunk_refusal_inside_the_guarded_region_does_not_invalidate(
        self, app, client: AsyncClient, tmp_path: Path
    ):
        """A refusal raised *after* the ``try`` opens but before the first
        write — here unusable source-line provenance — must leave the flag
        disarmed. ``invalidate_cache`` also drops the LLM query-expansion
        cache, so flushing on a pure refusal is pointless churn."""
        source = tmp_path / "present.md"
        source.write_text("# Heading\n\nBody.\n", encoding="utf-8")
        chunk = _make_test_chunk(source=str(source))
        # Unusable provenance -> 409 from inside the guarded region, no write.
        chunk = dataclasses.replace(
            chunk, metadata=dataclasses.replace(chunk.metadata, start_line=0)
        )
        app.state.storage.get_chunk = AsyncMock(return_value=chunk)
        app.state.search_pipeline.invalidate_cache.reset_mock()

        resp = await client.delete(f"/api/chunks/{CHUNK_ID}")

        assert resp.status_code == 409, resp.text
        app.state.storage.delete_chunks.assert_not_called()
        assert app.state.search_pipeline.invalidate_cache.call_count == 0

    async def test_reindex_all_keeps_earlier_roots_invalidation_when_a_later_root_raises(
        self, app, client: AsyncClient, tmp_path
    ):
        """Per root, not once after the loop: the first root's committed write
        must be reflected even though the second root blows the request up."""
        first = tmp_path / "one"
        second = tmp_path / "two"
        first.mkdir()
        second.mkdir()
        app.state.config.indexing.memory_dirs = [str(first), str(second)]
        mutated = IndexingStats(
            total_files=1,
            total_chunks=1,
            indexed_chunks=1,
            skipped_chunks=0,
            deleted_chunks=0,
            duration_ms=1.0,
            mutated=True,
        )
        app.state.index_engine.index_path = AsyncMock(
            side_effect=[mutated, RuntimeError("engine blew up")]
        )
        app.state.search_pipeline.invalidate_cache.reset_mock()

        with pytest.raises(RuntimeError, match="engine blew up"):
            await client.post("/api/reindex")

        # Both roots were attempted — the second is what failed, so the
        # single invalidation belongs to the first.
        assert app.state.index_engine.index_path.await_count == 2
        assert app.state.search_pipeline.invalidate_cache.call_count == 1

    async def test_index_stream_invalidates_when_the_generator_is_closed_mid_run(self, app):
        """The case the ``mutated`` flag can never survive, and the reason the
        ``finally`` is unconditional: the consumer goes away after a file's
        chunk transaction commits but before the run reports anything about
        it. Driven at the route level — an in-process ASGI transport drains
        the generator instead of cancelling it, so going through the HTTP
        client would pass for the wrong reason."""
        from memtomem.web.routes.system import index_stream
        from memtomem.web.schemas import IndexRequest

        committed = {"files": 0}

        async def _hangs_after_a_committed_file(*args, **kwargs):
            # The engine committed a file's chunk transaction, but the event
            # that would have carried ``mutated`` never reaches the route —
            # deliberately omitted here, because on a real cancellation it
            # never would. An implementation that gated the ``finally`` on an
            # observed flag would wrongly pass with ``mutated: True`` present.
            committed["files"] += 1
            yield {"type": "progress", "file": "a.md"}
            await asyncio.sleep(30)  # pragma: no cover — closed before this returns

        engine = SimpleNamespace(index_path_stream=_hangs_after_a_committed_file)
        pipeline = app.state.search_pipeline
        pipeline.invalidate_cache.reset_mock()

        response = await index_stream(
            IndexRequest(path="/tmp/memories"),
            index_engine=engine,
            search_pipeline=pipeline,
        )
        body = response.body_iterator
        first = await body.__anext__()
        assert "progress" in first
        await body.aclose()

        assert committed["files"] == 1
        assert pipeline.invalidate_cache.call_count == 1
