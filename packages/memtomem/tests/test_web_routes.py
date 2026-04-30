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
        monkeypatch.setenv("HOME", str(tmp_path))

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
        assert src["memory_dir"] == str(Path("/tmp/memories"))

        # Same source must round-trip through the kind=memory filter and
        # be excluded by kind=general.
        resp_mem = await client.get("/api/sources", params={"kind": "memory"})
        assert resp_mem.json()["total"] == 1
        resp_gen = await client.get("/api/sources", params={"kind": "general"})
        assert resp_gen.json()["total"] == 0


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
        monkeypatch.setenv("HOME", str(tmp_path))
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
        monkeypatch.setenv("HOME", str(tmp_path))
        app.state.index_engine.index_path.reset_mock()

        target = tmp_path / "target"
        target.mkdir()
        resp = await client.post("/api/index", json={"path": str(target)})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == ("memtomem is not configured. Run 'mm init' to set up.")
        assert app.state.index_engine.index_path.call_count == 0

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
        monkeypatch.setenv("HOME", str(tmp_path))
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
        monkeypatch.setenv("HOME", str(tmp_path))
        resp = await getattr(client, method)(path, **kwargs)
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == ("memtomem is not configured. Run 'mm init' to set up.")
