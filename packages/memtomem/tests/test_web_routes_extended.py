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
import json
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
        system_namespace_prefixes: list[str] = []

    class _Indexing:
        memory_dirs = [Path("/tmp/memories")]
        supported_extensions = frozenset({".md", ".json"})
        max_chunk_tokens = 512
        min_chunk_tokens = 128
        target_chunk_tokens = 384
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
        rules: list = []

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
    storage.reset_all = AsyncMock(return_value={"chunks": 42, "chunks_fts": 42, "chunks_vec": 42})

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
            {
                "namespace": "default",
                "chunk_count": 30,
                "description": "General",
                "color": "#3b82f6",
            },
            {
                "namespace": "work",
                "chunk_count": 12,
                "description": "Work notes",
                "color": "#ef4444",
            },
        ]
    )
    storage.list_namespaces = AsyncMock(return_value=[("default", 30), ("work", 12)])
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
    application.state.project_root = Path("/tmp/test-project")

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
            resp = await client.get("/api/decay/scan", params={"max_age_days": 60.0})
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

            bundle = ExportBundle(exported_at="2026-01-01T00:00:00Z", total_chunks=42, chunks=[])
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


class TestSettingsSync:
    """Tests for the settings-sync route (record-format hooks)."""

    @staticmethod
    def _rule(matcher: str = "", command: str = "echo ok") -> dict:
        return {"matcher": matcher, "hooks": [{"type": "command", "command": command}]}

    async def test_no_source_returns_no_source(self, app, client: AsyncClient, tmp_path):
        """When .memtomem/settings.json doesn't exist, status is no_source."""
        app.state.project_root = tmp_path
        resp = await client.get("/api/settings-sync")
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_source"

    async def test_in_sync(self, app, client: AsyncClient, tmp_path):
        """When hooks are identical in both files, status is in_sync."""
        rule = self._rule("Write", "echo ok")
        hooks = {"hooks": {"PostToolUse": [rule]}}

        canonical = tmp_path / ".memtomem" / "settings.json"
        canonical.parent.mkdir()
        canonical.write_text(json.dumps(hooks))

        target = Path.home() / ".claude" / "settings.json"
        backup = target.read_text() if target.is_file() else None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(hooks))
            app.state.project_root = tmp_path

            resp = await client.get("/api/settings-sync")
            data = resp.json()
            assert data["status"] == "in_sync"
            assert len(data["hooks"]["synced"]) == 1
            assert data["hooks"]["synced"][0]["event"] == "PostToolUse"
            assert data["hooks"]["synced"][0]["matcher"] == "Write"
        finally:
            if backup is not None:
                target.write_text(backup)
            elif target.is_file():
                target.unlink()

    async def test_conflict_detected(self, app, client: AsyncClient, tmp_path):
        """Same event+matcher but different config → status is conflicts."""
        canonical_rule = self._rule("Write", "echo new")
        target_rule = self._rule("Write", "echo old")

        canonical = tmp_path / ".memtomem" / "settings.json"
        canonical.parent.mkdir()
        canonical.write_text(json.dumps({"hooks": {"PostToolUse": [canonical_rule]}}))

        target = Path.home() / ".claude" / "settings.json"
        backup = target.read_text() if target.is_file() else None
        try:
            target.write_text(json.dumps({"hooks": {"PostToolUse": [target_rule]}}))
            app.state.project_root = tmp_path

            resp = await client.get("/api/settings-sync")
            data = resp.json()
            assert data["status"] == "conflicts"
            assert len(data["hooks"]["conflicts"]) == 1
            c = data["hooks"]["conflicts"][0]
            assert c["event"] == "PostToolUse"
            assert c["matcher"] == "Write"
            assert c["existing"]["hooks"][0]["command"] == "echo old"
            assert c["proposed"]["hooks"][0]["command"] == "echo new"
        finally:
            if backup is not None:
                target.write_text(backup)
            elif target.is_file():
                target.unlink()

    async def test_pending_hooks(self, app, client: AsyncClient, tmp_path):
        """Rules in canonical but not in target are pending."""
        rule = self._rule("Write", "echo hi")

        canonical = tmp_path / ".memtomem" / "settings.json"
        canonical.parent.mkdir()
        canonical.write_text(json.dumps({"hooks": {"PostToolUse": [rule]}}))

        target = Path.home() / ".claude" / "settings.json"
        backup = target.read_text() if target.is_file() else None
        try:
            target.write_text(json.dumps({"hooks": {}}))
            app.state.project_root = tmp_path

            resp = await client.get("/api/settings-sync")
            data = resp.json()
            assert data["status"] == "out_of_sync"
            assert len(data["hooks"]["pending"]) == 1
            assert data["hooks"]["pending"][0]["event"] == "PostToolUse"
        finally:
            if backup is not None:
                target.write_text(backup)
            elif target.is_file():
                target.unlink()

    async def test_resolve_replaces_rule(self, app, client: AsyncClient, tmp_path):
        """POST /resolve replaces the target's rule with canonical version."""
        canonical_rule = self._rule("Write", "echo new")
        target_rule = self._rule("Write", "echo old")

        canonical = tmp_path / ".memtomem" / "settings.json"
        canonical.parent.mkdir()
        canonical.write_text(json.dumps({"hooks": {"PostToolUse": [canonical_rule]}}))

        target = Path.home() / ".claude" / "settings.json"
        backup = target.read_text() if target.is_file() else None
        try:
            target.write_text(json.dumps({"hooks": {"PostToolUse": [target_rule]}}))
            app.state.project_root = tmp_path

            resp = await client.post(
                "/api/settings-sync/resolve",
                json={"event": "PostToolUse", "matcher": "Write", "action": "use_proposed"},
            )
            data = resp.json()
            assert data["status"] == "ok"

            updated = json.loads(target.read_text())
            assert updated["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "echo new"
        finally:
            if backup is not None:
                target.write_text(backup)
            elif target.is_file():
                target.unlink()

    async def test_resolve_missing_canonical_returns_404(self, app, client: AsyncClient, tmp_path):
        """POST /resolve when canonical file doesn't exist returns HTTP 404."""
        # No .memtomem/settings.json created in tmp_path
        app.state.project_root = tmp_path
        resp = await client.post(
            "/api/settings-sync/resolve",
            json={"event": "PostToolUse", "matcher": "Write", "action": "use_proposed"},
        )
        assert resp.status_code == 404
        assert "Canonical source does not exist" in resp.json()["detail"]

    async def test_resolve_unknown_action_returns_400(self, app, client: AsyncClient, tmp_path):
        """POST /resolve with invalid action returns HTTP 400."""
        app.state.project_root = tmp_path
        resp = await client.post(
            "/api/settings-sync/resolve",
            json={"event": "PostToolUse", "matcher": "Write", "action": "bad_action"},
        )
        assert resp.status_code == 400
        assert "Unknown action" in resp.json()["detail"]

    async def test_resolve_missing_rule_returns_404(self, app, client: AsyncClient, tmp_path):
        """POST /resolve for non-existent rule returns HTTP 404."""
        canonical = tmp_path / ".memtomem" / "settings.json"
        canonical.parent.mkdir()
        canonical.write_text(json.dumps({"hooks": {}}))

        target = Path.home() / ".claude" / "settings.json"
        backup = target.read_text() if target.is_file() else None
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps({"hooks": {}}))
            app.state.project_root = tmp_path

            resp = await client.post(
                "/api/settings-sync/resolve",
                json={"event": "PostToolUse", "matcher": "Write", "action": "use_proposed"},
            )
            assert resp.status_code == 404
            assert "not in canonical source" in resp.json()["detail"]
        finally:
            if backup is not None:
                target.write_text(backup)
            elif target.is_file():
                target.unlink()

    # -- URL alias tests (/api/context/settings/*) ----------------------------

    async def test_alias_get_context_settings(self, app, client: AsyncClient, tmp_path):
        """GET /api/context/settings returns same result as /api/settings-sync."""
        app.state.project_root = tmp_path
        resp = await client.get("/api/context/settings")
        assert resp.status_code == 200
        assert resp.json()["status"] == "no_source"

    async def test_alias_post_context_settings_sync(self, app, client: AsyncClient, tmp_path):
        """POST /api/context/settings/sync runs the settings merge."""
        canonical = tmp_path / ".memtomem" / "settings.json"
        canonical.parent.mkdir()
        canonical.write_text(json.dumps({"hooks": {}}))
        app.state.project_root = tmp_path

        resp = await client.post(
            "/api/context/settings/sync",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 200
        assert "results" in resp.json()

    async def test_alias_post_context_settings_resolve(self, app, client: AsyncClient, tmp_path):
        """POST /api/context/settings/resolve resolves a hook conflict."""
        canonical_rule = self._rule("Edit", "echo v2")
        target_rule = self._rule("Edit", "echo v1")

        canonical = tmp_path / ".memtomem" / "settings.json"
        canonical.parent.mkdir()
        canonical.write_text(json.dumps({"hooks": {"PostToolUse": [canonical_rule]}}))

        target = Path.home() / ".claude" / "settings.json"
        backup = target.read_text() if target.is_file() else None
        try:
            target.write_text(json.dumps({"hooks": {"PostToolUse": [target_rule]}}))
            app.state.project_root = tmp_path

            resp = await client.post(
                "/api/context/settings/resolve",
                json={"event": "PostToolUse", "matcher": "Edit", "action": "use_proposed"},
            )
            data = resp.json()
            assert data["status"] == "ok"

            updated = json.loads(target.read_text())
            assert updated["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "echo v2"
        finally:
            if backup is not None:
                target.write_text(backup)
            elif target.is_file():
                target.unlink()


# ---------------------------------------------------------------------------
# PATCH /api/config  (config mutation)
# ---------------------------------------------------------------------------


class TestConfigPatch:
    async def test_patch_mutable_field(self, client: AsyncClient):
        """Patching a mutable field succeeds and returns applied change."""
        resp = await client.patch(
            "/api/config",
            json={"search": {"default_top_k": 20}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["applied"]) == 1
        assert data["applied"][0]["field"] == "search.default_top_k"
        assert data["applied"][0]["new_value"] == "20"

    async def test_patch_readonly_field_rejected(self, client: AsyncClient):
        """Patching a read-only field is rejected."""
        resp = await client.patch(
            "/api/config",
            json={"embedding": {"provider": "openai"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["applied"]) == 0
        assert any("read-only" in r for r in data["rejected"])

    async def test_patch_unknown_section_rejected(self, client: AsyncClient):
        """Unknown sections are reported in rejected list."""
        resp = await client.patch(
            "/api/config",
            json={"nonexistent": {"key": "val"}},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["applied"]) == 0
        assert any("nonexistent" in r for r in data["rejected"])

    async def test_save_config(self, client: AsyncClient):
        """POST /api/config/save persists config."""
        with patch("memtomem.config.save_config_overrides"):
            resp = await client.post("/api/config/save")
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True

    async def test_patch_namespace_rules(self, app, client: AsyncClient):
        """PATCH /api/config accepts list[NamespacePolicyRule] as JSON-compatible dicts."""
        from memtomem.config import NamespacePolicyRule

        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.patch(
                "/api/config",
                json={
                    "namespace": {
                        "rules": [
                            {"path_glob": "docs/**/*.md", "namespace": "docs"},
                            {"path_glob": "work/**/*.md", "namespace": "work"},
                        ]
                    }
                },
            )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["applied"]) == 1
        assert data["applied"][0]["field"] == "namespace.rules"
        # Runtime config actually holds validated model instances.
        rules = app.state.config.namespace.rules
        assert len(rules) == 2
        assert all(isinstance(r, NamespacePolicyRule) for r in rules)
        assert rules[0].namespace == "docs"
        assert rules[1].namespace == "work"

    async def test_patch_namespace_rules_validation_error(self, client: AsyncClient):
        """Invalid rule (empty path_glob) is reported in rejected list, not applied."""
        with patch("memtomem.web.routes.system.save_config_overrides"):
            resp = await client.patch(
                "/api/config",
                json={"namespace": {"rules": [{"path_glob": "", "namespace": "x"}]}},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["applied"]) == 0
        assert any("namespace.rules" in r for r in data["rejected"])


# ---------------------------------------------------------------------------
# Namespace CRUD
# ---------------------------------------------------------------------------


class TestNamespaceCRUD:
    async def test_get_namespace_info(self, client: AsyncClient):
        resp = await client.get("/api/namespaces/default")
        assert resp.status_code == 200
        data = resp.json()
        assert data["namespace"] == "default"
        assert data["chunk_count"] == 30
        assert data["description"] == "General"

    async def test_get_namespace_not_found(self, app, client: AsyncClient):
        app.state.storage.list_namespaces.return_value = [("default", 30), ("work", 12)]
        resp = await client.get("/api/namespaces/nonexistent")
        assert resp.status_code == 404

    async def test_update_namespace_meta(self, client: AsyncClient):
        resp = await client.patch(
            "/api/namespaces/default",
            json={"description": "Updated", "color": "#00ff00"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["namespace"] == "default"

    async def test_rename_namespace(self, app, client: AsyncClient):
        app.state.storage.rename_namespace = AsyncMock(return_value=30)
        resp = await client.post(
            "/api/namespaces/default/rename",
            json={"new_name": "general"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["namespace"] == "general"
        assert data["chunk_count"] == 30

    async def test_rename_namespace_empty_name_rejected(self, client: AsyncClient):
        """Empty new_name is rejected by Pydantic min_length=1."""
        resp = await client.post(
            "/api/namespaces/default/rename",
            json={"new_name": ""},
        )
        assert resp.status_code == 422

    async def test_delete_namespace(self, app, client: AsyncClient):
        app.state.storage.delete_by_namespace = AsyncMock(return_value=30)
        resp = await client.delete("/api/namespaces/default")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == 30


# ---------------------------------------------------------------------------
# Scratch workspace CRUD
# ---------------------------------------------------------------------------


class TestScratchCRUD:
    async def test_set_scratch(self, app, client: AsyncClient):
        app.state.storage.scratch_set = AsyncMock()
        resp = await client.post(
            "/api/scratch",
            json={"key": "task", "value": "write tests"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "task"

    async def test_set_scratch_with_ttl(self, app, client: AsyncClient):
        app.state.storage.scratch_set = AsyncMock()
        resp = await client.post(
            "/api/scratch",
            json={"key": "tmp", "value": "ephemeral", "ttl_minutes": 60},
        )
        assert resp.status_code == 200
        app.state.storage.scratch_set.assert_called_once()
        # Verify expires_at was computed (non-None)
        call_kwargs = app.state.storage.scratch_set.call_args
        assert call_kwargs.kwargs.get("expires_at") is not None

    async def test_delete_scratch(self, app, client: AsyncClient):
        app.state.storage.scratch_delete = AsyncMock(return_value=True)
        resp = await client.delete("/api/scratch/task")
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] == "task"
        assert data["deleted"] is True

    async def test_promote_scratch(self, app, client: AsyncClient):
        app.state.storage.scratch_get = AsyncMock(
            return_value={"key": "note", "value": "promote me"}
        )
        app.state.storage.scratch_promote = AsyncMock()
        with patch("memtomem.tools.memory_writer.append_entry"):
            resp = await client.post(
                "/api/scratch/note/promote",
                json={},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["promoted"] is True
            assert data["key"] == "note"

    async def test_promote_scratch_not_found(self, app, client: AsyncClient):
        app.state.storage.scratch_get = AsyncMock(return_value=None)
        resp = await client.post(
            "/api/scratch/missing/promote",
            json={},
        )
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Watchdog endpoints
# ---------------------------------------------------------------------------


class TestWatchdog:
    async def test_status_disabled(self, client: AsyncClient):
        """When no watchdog is configured, returns enabled=false."""
        resp = await client.get("/api/watchdog/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False

    async def test_status_enabled(self, app, client: AsyncClient):
        mock_wd = MagicMock()
        mock_wd.get_status.return_value = {
            "enabled": True,
            "checks": {"storage": "ok"},
        }
        app.state.health_watchdog = mock_wd
        resp = await client.get("/api/watchdog/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert "checks" in data

    async def test_history(self, app, client: AsyncClient):
        mock_wd = MagicMock()
        mock_wd.get_trends.return_value = {"check": "storage", "points": []}
        app.state.health_watchdog = mock_wd
        resp = await client.get(
            "/api/watchdog/history",
            params={"check": "storage", "hours": 24},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["check"] == "storage"

    async def test_run_disabled(self, client: AsyncClient):
        """POST /watchdog/run returns 400 when not configured."""
        resp = await client.post("/api/watchdog/run")
        assert resp.status_code == 400
        data = resp.json()
        assert data["enabled"] is False

    async def test_run_enabled(self, app, client: AsyncClient):
        mock_wd = AsyncMock()
        mock_wd.run_now = AsyncMock(return_value={"storage": "ok", "embedding": "ok"})
        app.state.health_watchdog = mock_wd
        resp = await client.post("/api/watchdog/run")
        assert resp.status_code == 200
        data = resp.json()
        assert data["storage"] == "ok"


# ---------------------------------------------------------------------------
# POST /api/export/import
# ---------------------------------------------------------------------------


class TestImport:
    async def test_import_json_bundle(self, client: AsyncClient):
        with patch("memtomem.tools.export_import.import_chunks") as mock_imp:
            from types import SimpleNamespace

            mock_imp.return_value = SimpleNamespace(
                total_chunks=5,
                imported_chunks=4,
                skipped_chunks=1,
                failed_chunks=0,
            )
            resp = await client.post(
                "/api/export/import",
                files={"file": ("export.json", b'{"chunks":[]}', "application/json")},
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["total_chunks"] == 5
            assert data["imported_chunks"] == 4

    async def test_import_rejects_non_json(self, client: AsyncClient):
        resp = await client.post(
            "/api/export/import",
            files={"file": ("data.csv", b"a,b,c", "text/csv")},
        )
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# POST /api/reset
# ---------------------------------------------------------------------------


class TestResetAll:
    async def test_reset_returns_deleted_counts(self, client: AsyncClient):
        resp = await client.post("/api/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["deleted"]["chunks"] == 42
        assert data["total_deleted"] > 0

    async def test_reset_calls_storage_reset_all(self, app, client: AsyncClient):
        resp = await client.post("/api/reset")
        assert resp.status_code == 200
        app.state.storage.reset_all.assert_awaited_once()


# ---------------------------------------------------------------------------
# Integration test — real SQLite storage
# ---------------------------------------------------------------------------


class TestNamespaceIntegration:
    """End-to-end namespace CRUD with real SQLite storage."""

    @pytest.fixture
    async def real_client(self, tmp_path):
        """App backed by a real in-memory SQLite store."""
        from memtomem.config import Mem2MemConfig
        from memtomem.server.component_factory import create_components, close_components

        db_path = str(tmp_path / "integ.db")
        mem_dir = tmp_path / "memories"
        mem_dir.mkdir()

        config = Mem2MemConfig()
        config.storage.sqlite_path = Path(db_path)
        config.indexing.memory_dirs = [mem_dir]
        config.embedding.dimension = 768

        import memtomem.config as _cfg

        _orig = _cfg.load_config_overrides
        _cfg.load_config_overrides = lambda c: None
        comp = await create_components(config)
        _cfg.load_config_overrides = _orig

        application = create_app(lifespan=None)
        application.state.storage = comp.storage
        application.state.config = config
        application.state.embedder = comp.embedder
        application.state.search_pipeline = comp.search_pipeline
        application.state.index_engine = comp.index_engine
        application.state.dedup_scanner = AsyncMock()
        application.state.project_root = tmp_path

        transport = ASGITransport(app=application)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c, comp

        await close_components(comp)

    async def test_namespace_crud_lifecycle(self, real_client):
        """Create chunk → set meta → rename ns → verify → delete ns."""
        client, comp = real_client
        storage = comp.storage

        # Insert a chunk into "alpha" namespace directly
        chunk = _make_test_chunk(namespace="alpha")
        await storage.upsert_chunks([chunk])

        # 1) List namespaces — "alpha" should appear
        resp = await client.get("/api/namespaces")
        assert resp.status_code == 200
        ns_names = [ns["namespace"] for ns in resp.json()["namespaces"]]
        assert "alpha" in ns_names

        # 2) Set metadata on "alpha"
        resp = await client.patch(
            "/api/namespaces/alpha",
            json={"description": "Test ns", "color": "#ff0000"},
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "Test ns"

        # 3) Rename "alpha" → "beta"
        resp = await client.post(
            "/api/namespaces/alpha/rename",
            json={"new_name": "beta"},
        )
        assert resp.status_code == 200
        assert resp.json()["namespace"] == "beta"
        assert resp.json()["chunk_count"] == 1

        # 4) Old name is gone
        resp = await client.get("/api/namespaces/alpha")
        assert resp.status_code == 404

        # 5) Delete "beta" — removes chunk
        resp = await client.delete("/api/namespaces/beta")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == 1

        # 6) Verify empty
        resp = await client.get("/api/namespaces")
        assert resp.status_code == 200
        ns_names = [ns["namespace"] for ns in resp.json()["namespaces"]]
        assert "beta" not in ns_names
