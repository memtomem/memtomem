"""Tests for FastAPI web routes using httpx AsyncClient.

The web app is created by create_app() and dependencies are injected via
request.app.state.  We override app.state with mock/stub objects to avoid
full component initialization (embedding provider, SQLite, etc.).
"""

from __future__ import annotations

import asyncio
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
from .helpers import set_home


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
        threads = 4

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
        project_memory_dirs: list[Path] = []
        supported_extensions = frozenset({".md", ".json"})
        max_chunk_tokens = 512
        min_chunk_tokens = 128
        target_chunk_tokens = 384
        chunk_overlap_tokens = 0
        structured_chunk_mode = "original"
        exclude_patterns: list[str] = []
        # Per-source AI summary knobs; default off to match production.
        auto_summarize = False
        summary_language = "en"
        summary_max_input_chars = 3000
        summary_max_tokens = 256

        def all_index_roots(self):
            return list(self.memory_dirs) + list(self.project_memory_dirs)

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
    # Sync helpers powering the preview-namespace route. Default to a
    # 1-file walk producing a single named NS — individual tests override
    # to exercise rule-variance / truncation / untagged paths.
    index_engine.discover_indexable_files = MagicMock(return_value=[Path("/tmp/memories/note.md")])
    index_engine.resolve_namespaces_for = MagicMock(return_value=["notes"])

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
    # _Indexing is a class-level singleton — reset mutable fields so tests that
    # mutate exclude_patterns or memory_dirs don't leak into later tests.
    # The memory_dirs reset matters for any test that reassigns the list to
    # exercise a custom corpus shape (symlinked / tilde / nested / orphan
    # cases): without it, the override persists across the fixture boundary
    # and an unrelated test downstream sees the wrong default and fails the
    # path-inside-memory_dirs gate (e.g. ``/api/index`` 403s).
    cfg.indexing.exclude_patterns = []
    cfg.indexing.memory_dirs = [Path("/tmp/memories")]
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
        # ``embedding.threads`` exposed read-only so the Config tab can
        # render the ORT intra-op cap. Pinning the field's presence here
        # so a future schema trim doesn't silently re-hide it (#640
        # discoverability follow-up). Default 4 since the #640 follow-up
        # default flip — pre-flip the assertion was ``== 0``.
        assert "threads" in data["embedding"]
        assert data["embedding"]["threads"] == 4
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

        resp_shared_tier = await client.get(
            "/api/sources", params={"target_tier": "project_shared"}
        )
        rows_shared_tier = resp_shared_tier.json()["sources"]
        assert len(rows_shared_tier) == 1
        assert rows_shared_tier[0]["target_tier"] == "project_shared"
        assert rows_shared_tier[0]["target_scope"] == "project_shared"

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
        app.state.storage.get_chunk.return_value = chunk

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
        app.state.storage.get_chunk.return_value = chunk

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
        # Default path "." is outside configured memory_dirs, should be rejected
        resp = await client.post("/api/index")
        assert resp.status_code == 403

    async def test_trigger_index_outside_memory_dirs(self, client: AsyncClient):
        resp = await client.post("/api/index", json={"path": "/etc"})
        assert resp.status_code == 403

    async def test_trigger_index_returns_resolved_namespaces(self, app, client: AsyncClient):
        """``IndexResponse.resolved_namespaces`` must echo what the engine
        actually applied across the file set — including the rule-variance
        case where a folder splits into multiple namespaces. The list
        shape is deliberate; collapsing to a single value would silently
        misrepresent multi-NS folders."""
        app.state.index_engine.index_path = AsyncMock(
            return_value=IndexingStats(
                total_files=2,
                total_chunks=4,
                indexed_chunks=4,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=80.0,
                resolved_namespaces=("ns-alpha", "ns-beta"),
            )
        )
        resp = await client.post("/api/index", json={"path": "/tmp/memories"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved_namespaces"] == ["ns-alpha", "ns-beta"]

    async def test_preview_namespace_leaf_file(self, app, client: AsyncClient):
        """Single-file path → single-element list (here: ``notes``)."""
        resp = await client.get("/api/index/preview-namespace?path=/tmp/memories/note.md")
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
        app.state.index_engine.resolve_namespaces_for = MagicMock(return_value=["personal"])
        resp = await client.get("/api/index/preview-namespace?path=/tmp/memories")
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
        app.state.index_engine.resolve_namespaces_for = MagicMock(
            return_value=["ns-alpha", "ns-beta"]
        )
        resp = await client.get("/api/index/preview-namespace?path=/tmp/memories")
        assert resp.status_code == 200
        data = resp.json()
        assert data["resolved_namespaces"] == ["ns-alpha", "ns-beta"]

    async def test_preview_namespace_directory_truncated(self, app, client: AsyncClient):
        """File walk capped at 200; truncated flag surfaces the limit so the
        UI can render ``scanned 200+`` instead of pretending exhaustiveness."""
        app.state.index_engine.discover_indexable_files = MagicMock(
            return_value=[Path(f"/tmp/memories/f{i}.md") for i in range(250)]
        )
        app.state.index_engine.resolve_namespaces_for = MagicMock(return_value=["notes"])
        resp = await client.get("/api/index/preview-namespace?path=/tmp/memories")
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
        """403, not 422: out-of-memory_dirs is a security boundary, same
        trust gate as POST /index."""
        resp = await client.get("/api/index/preview-namespace?path=/etc/passwd")
        assert resp.status_code == 403

    async def test_preview_namespace_missing_path(self, app, client: AsyncClient):
        """422 — FastAPI query-param validation."""
        resp = await client.get("/api/index/preview-namespace")
        assert resp.status_code == 422

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
        # ``index_path`` was called with the resolved path of the dir we
        # just added — watcher invariant naturally satisfied.
        called_args, _ = app.state.index_engine.index_path.call_args
        assert Path(str(called_args[0])).resolve() == memory_dir.resolve()

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
        assert app.state.index_engine.index_path.call_count == 0

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
            ("get", "/api/index/stream", {"params": {"path": "/tmp/x"}}),
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
