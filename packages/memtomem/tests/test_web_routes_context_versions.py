"""Tests for the ADR-0022 version/label web routes (``context_versions``).

Covers create (freeze) / promote (== rollback) / delete-label / list across the
two eligible types (agents + commands), plus the scope-boundary rejections:
skills + unknown types 404, flat-layout 409 ("migrate first"), non-shared tier
400, reserved/version-shaped label names 400.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.context import versioning
from memtomem.context.skills import SKILL_MANIFEST
from memtomem.web.app import create_app


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_app(tmp_path: Path):
    from memtomem.config import Mem2MemConfig

    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = tmp_path
    application.state.storage = AsyncMock()
    application.state.config = Mem2MemConfig()
    application.state.search_pipeline = None
    application.state.index_engine = None
    application.state.embedder = None
    application.state.dedup_scanner = None
    return application


@pytest.fixture
async def client(ctx_app):
    transport = ASGITransport(app=ctx_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_dir_agent(tmp_path: Path, name: str, body: str = "agent body\n") -> Path:
    """Directory-layout agent: ``.memtomem/agents/<name>/agent.md`` (ADR-0008)."""
    d = tmp_path / ".memtomem" / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    content = f"---\ndescription: {name}\n---\n{body}"
    (d / "agent.md").write_text(content, encoding="utf-8")
    return d


def _make_flat_agent(tmp_path: Path, name: str) -> Path:
    """Legacy flat-layout agent: ``.memtomem/agents/<name>.md`` (no version store)."""
    root = tmp_path / ".memtomem" / "agents"
    root.mkdir(parents=True, exist_ok=True)
    p = root / f"{name}.md"
    p.write_text(f"---\ndescription: {name}\n---\nbody\n", encoding="utf-8")
    return p


def _make_dir_command(tmp_path: Path, name: str) -> Path:
    """Directory-layout command: ``.memtomem/commands/<name>/command.md``."""
    d = tmp_path / ".memtomem" / "commands" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "command.md").write_text(f"---\ndescription: {name}\n---\nrun\n", encoding="utf-8")
    return d


def _make_dir_skill(tmp_path: Path, name: str) -> Path:
    """Directory-layout skill: ``.memtomem/skills/<name>/<SKILL_MANIFEST>``.

    Skills are out of versioning (ADR-0022 inv 7); used only to give the skills
    LIST a non-empty payload for the ``?include=versions`` no-op assertion.
    """
    d = tmp_path / ".memtomem" / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / SKILL_MANIFEST).write_text(
        f"---\nname: {name}\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    return d


def _by_name(items: list[dict], name: str) -> dict:
    for it in items:
        if it["name"] == name:
            return it
    raise AssertionError(f"{name!r} not in list: {[i['name'] for i in items]}")


# ---------------------------------------------------------------------------
# List
# ---------------------------------------------------------------------------


class TestListVersions:
    @pytest.mark.anyio
    async def test_empty_dir_agent_lists_no_versions(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        r = await client.get("/api/context/agents/reviewer/versions")
        assert r.status_code == 200
        body = r.json()
        assert body["layout"] == "dir"
        assert body["versions"] == []
        assert body["labels"] == {}
        assert body["has_versions"] is False
        assert body["migrate_required"] is False

    @pytest.mark.anyio
    async def test_flat_agent_reports_migrate_required_not_error(self, client, tmp_path):
        _make_flat_agent(tmp_path, "legacy")
        r = await client.get("/api/context/agents/legacy/versions")
        assert r.status_code == 200
        body = r.json()
        assert body["layout"] == "flat"
        assert body["migrate_required"] is True
        assert body["has_versions"] is False

    @pytest.mark.anyio
    async def test_unsupported_type_skills_404(self, client, tmp_path):
        r = await client.get("/api/context/skills/anything/versions")
        assert r.status_code == 404
        assert "agents and commands only" in r.json()["detail"]

    @pytest.mark.anyio
    async def test_unsupported_type_mcp_servers_404(self, client, tmp_path):
        r = await client.get("/api/context/mcp-servers/anything/versions")
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_missing_artifact_404(self, client, tmp_path):
        r = await client.get("/api/context/agents/ghost/versions")
        assert r.status_code == 404


# ---------------------------------------------------------------------------
# include_has — comma-separated ?include= parsing (pure)
# ---------------------------------------------------------------------------


class TestIncludeHas:
    @pytest.mark.parametrize(
        "include,expected",
        [
            (None, False),
            ("", False),
            ("versions", True),
            ("foo,versions", True),
            ("foo, versions ", True),  # whitespace-tolerant
            ("versions,bar", True),
            ("versionsx", False),  # substring is not a member
            ("ver", False),
            ("foo,bar", False),
        ],
    )
    def test_membership(self, include, expected):
        from memtomem.web.routes.context_versions import include_has

        assert include_has(include, "versions") is expected


# ---------------------------------------------------------------------------
# List-card enrichment (?include=versions) — ADR-0022 PR4
# ---------------------------------------------------------------------------


class TestListIncludeVersions:
    @pytest.mark.anyio
    async def test_no_include_omits_versions_key(self, client, tmp_path):
        """Default list path is unchanged — no per-item ``versions`` key, so
        existing callers and the wire shape stay byte-compatible."""
        _make_dir_agent(tmp_path, "reviewer")
        r = await client.get("/api/context/agents")
        assert r.status_code == 200
        items = r.json()["agents"]
        assert items and all("versions" not in it for it in items)

    @pytest.mark.anyio
    async def test_include_versions_dir_agent_empty_store(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        r = await client.get("/api/context/agents?include=versions")
        assert r.status_code == 200
        item = _by_name(r.json()["agents"], "reviewer")
        assert item["versions"] == {
            "labels": {},
            "count": 0,
            "versionable": True,
            "migrate_required": False,
        }

    @pytest.mark.anyio
    async def test_include_versions_reflects_labels_and_count(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        await client.post("/api/context/agents/reviewer/versions", json={})  # v1
        await client.post("/api/context/agents/reviewer/versions", json={})  # v2
        await client.put("/api/context/agents/reviewer/labels/production", json={"version": "v2"})
        await client.put("/api/context/agents/reviewer/labels/staging", json={"version": "v1"})
        r = await client.get("/api/context/agents?include=versions")
        item = _by_name(r.json()["agents"], "reviewer")
        assert item["versions"]["labels"] == {"production": "v2", "staging": "v1"}
        assert item["versions"]["count"] == 2
        assert item["versions"]["versionable"] is True
        assert item["versions"]["migrate_required"] is False

    @pytest.mark.anyio
    async def test_include_versions_flat_agent_migrate_required(self, client, tmp_path):
        _make_flat_agent(tmp_path, "legacy")
        r = await client.get("/api/context/agents?include=versions")
        item = _by_name(r.json()["agents"], "legacy")
        assert item["versions"]["versionable"] is False
        assert item["versions"]["migrate_required"] is True
        assert item["versions"]["labels"] == {}
        assert item["versions"]["count"] == 0

    @pytest.mark.anyio
    async def test_corrupt_manifest_isolated_not_500(self, client, tmp_path):
        """One unreadable ``versions.json`` must not 500 the list nor hide a
        healthy sibling's labels (per-artifact isolation, like a sync skip)."""
        _make_dir_agent(tmp_path, "good")
        bad = _make_dir_agent(tmp_path, "bad")
        await client.post("/api/context/agents/good/versions", json={})  # v1
        await client.put("/api/context/agents/good/labels/production", json={"version": "v1"})
        # Wrong JSON shape → VersionError on load_manifest.
        (bad / "versions.json").write_text("[]", encoding="utf-8")

        r = await client.get("/api/context/agents?include=versions")
        assert r.status_code == 200
        good_item = _by_name(r.json()["agents"], "good")
        bad_item = _by_name(r.json()["agents"], "bad")
        assert good_item["versions"]["labels"] == {"production": "v1"}
        assert bad_item["versions"]["labels"] == {}
        assert bad_item["versions"]["error"] is True
        assert bad_item["versions"]["count"] == 0

    @pytest.mark.anyio
    async def test_include_versions_commands_too(self, client, tmp_path):
        _make_dir_command(tmp_path, "deploy")
        await client.post("/api/context/commands/deploy/versions", json={})  # v1
        await client.put("/api/context/commands/deploy/labels/production", json={"version": "v1"})
        r = await client.get("/api/context/commands?include=versions")
        assert r.status_code == 200
        item = _by_name(r.json()["commands"], "deploy")
        assert item["versions"]["labels"] == {"production": "v1"}
        assert item["versions"]["count"] == 1
        assert item["versions"]["versionable"] is True

    @pytest.mark.anyio
    async def test_no_include_omits_versions_key_commands(self, client, tmp_path):
        """Mirror of the agents default-path pin for commands — the byte-identical
        per-route enrichment guard must keep the no-include wire shape (the two
        routes write the guard independently, so the agents pin alone doesn't
        protect a regression that drops the commands guard)."""
        _make_dir_command(tmp_path, "deploy")
        r = await client.get("/api/context/commands")
        assert r.status_code == 200
        items = r.json()["commands"]
        assert items and all("versions" not in it for it in items)

    @pytest.mark.anyio
    async def test_skills_list_ignores_include_versions(self, client, tmp_path):
        """The skills LIST route declares no ``include`` param (skills are out of
        versioning, inv 7). FastAPI silently ignores an undeclared query param →
        200, never 422, and no per-item ``versions`` key. Pins the frontend
        gate's load-bearing assumption that sending the param to skills is a
        harmless no-op."""
        _make_dir_skill(tmp_path, "demo")
        r = await client.get("/api/context/skills?include=versions")
        assert r.status_code == 200
        items = r.json()["skills"]
        assert items and all("versions" not in it for it in items)


# ---------------------------------------------------------------------------
# Create version
# ---------------------------------------------------------------------------


class TestCreateVersion:
    @pytest.mark.anyio
    async def test_create_increments_and_persists_files(self, client, tmp_path):
        adir = _make_dir_agent(tmp_path, "reviewer")

        r1 = await client.post("/api/context/agents/reviewer/versions", json={})
        assert r1.status_code == 200
        assert r1.json()["version"]["tag"] == "v1"

        r2 = await client.post("/api/context/agents/reviewer/versions", json={"note": "stable"})
        assert r2.status_code == 200
        assert r2.json()["version"]["tag"] == "v2"
        assert r2.json()["version"]["note"] == "stable"

        # On-disk: immutable version files + manifest sidecar.
        assert (adir / "versions" / "v1.md").is_file()
        assert (adir / "versions" / "v2.md").is_file()
        manifest = json.loads((adir / "versions.json").read_text(encoding="utf-8"))
        assert set(manifest["versions"]) == {"v1", "v2"}
        assert manifest["versions"]["v2"]["note"] == "stable"

        # The frozen bytes equal the working file at freeze time.
        assert (adir / "versions" / "v1.md").read_text() == (adir / "agent.md").read_text()

    @pytest.mark.anyio
    async def test_list_reflects_created_versions_newest_first(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        await client.post("/api/context/agents/reviewer/versions", json={})
        await client.post("/api/context/agents/reviewer/versions", json={})
        r = await client.get("/api/context/agents/reviewer/versions")
        body = r.json()
        assert [v["tag"] for v in body["versions"]] == ["v2", "v1"]
        assert body["has_versions"] is True

    @pytest.mark.anyio
    async def test_create_on_flat_layout_409_migrate(self, client, tmp_path):
        _make_flat_agent(tmp_path, "legacy")
        r = await client.post("/api/context/agents/legacy/versions", json={})
        assert r.status_code == 409
        assert "migrate" in r.json()["detail"].lower()

    @pytest.mark.anyio
    async def test_create_on_skills_type_404(self, client, tmp_path):
        r = await client.post("/api/context/skills/x/versions", json={})
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_create_non_shared_tier_400(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        r = await client.post(
            "/api/context/agents/reviewer/versions?target_scope=project_local", json={}
        )
        assert r.status_code == 400
        assert "project_shared" in r.json()["detail"]

    @pytest.mark.anyio
    async def test_create_missing_artifact_404(self, client, tmp_path):
        r = await client.post("/api/context/agents/ghost/versions", json={})
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_commands_are_versionable_too(self, client, tmp_path):
        cdir = _make_dir_command(tmp_path, "deploy")
        r = await client.post("/api/context/commands/deploy/versions", json={})
        assert r.status_code == 200
        assert r.json()["version"]["tag"] == "v1"
        assert (cdir / "versions" / "v1.md").is_file()


# ---------------------------------------------------------------------------
# Promote / rollback
# ---------------------------------------------------------------------------


class TestPromoteLabel:
    @pytest.mark.anyio
    async def test_promote_then_rollback_moves_pointer(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        await client.post("/api/context/agents/reviewer/versions", json={})
        await client.post("/api/context/agents/reviewer/versions", json={})

        # Promote production → v2.
        r = await client.put(
            "/api/context/agents/reviewer/labels/production", json={"version": "v2"}
        )
        assert r.status_code == 200
        assert r.json()["labels"] == {"production": "v2"}

        # Rollback is the same op: move the pointer back to v1.
        r = await client.put(
            "/api/context/agents/reviewer/labels/production", json={"version": "v1"}
        )
        assert r.status_code == 200
        assert r.json()["labels"] == {"production": "v1"}

        # GET reflects the moved pointer.
        listing = await client.get("/api/context/agents/reviewer/versions")
        assert listing.json()["labels"] == {"production": "v1"}

    @pytest.mark.anyio
    async def test_promote_to_nonexistent_version_404(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        await client.post("/api/context/agents/reviewer/versions", json={})
        r = await client.put(
            "/api/context/agents/reviewer/labels/production", json={"version": "v9"}
        )
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_promote_reserved_label_latest_400(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        await client.post("/api/context/agents/reviewer/versions", json={})
        r = await client.put("/api/context/agents/reviewer/labels/latest", json={"version": "v1"})
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_promote_version_shaped_label_name_400(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        await client.post("/api/context/agents/reviewer/versions", json={})
        r = await client.put("/api/context/agents/reviewer/labels/v1", json={"version": "v1"})
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_promote_on_flat_layout_409(self, client, tmp_path):
        _make_flat_agent(tmp_path, "legacy")
        r = await client.put("/api/context/agents/legacy/labels/production", json={"version": "v1"})
        assert r.status_code == 409

    @pytest.mark.anyio
    async def test_promote_non_shared_tier_400(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        r = await client.put(
            "/api/context/agents/reviewer/labels/production?target_scope=project_local",
            json={"version": "v1"},
        )
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_promote_works_on_commands(self, client, tmp_path):
        _make_dir_command(tmp_path, "deploy")
        await client.post("/api/context/commands/deploy/versions", json={})
        r = await client.put(
            "/api/context/commands/deploy/labels/production", json={"version": "v1"}
        )
        assert r.status_code == 200
        assert r.json()["labels"] == {"production": "v1"}
        listing = await client.get("/api/context/commands/deploy/versions")
        assert listing.json()["labels"] == {"production": "v1"}


# ---------------------------------------------------------------------------
# Delete label
# ---------------------------------------------------------------------------


class TestDeleteLabel:
    @pytest.mark.anyio
    async def test_delete_removes_pointer(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        await client.post("/api/context/agents/reviewer/versions", json={})
        await client.put("/api/context/agents/reviewer/labels/staging", json={"version": "v1"})
        r = await client.delete("/api/context/agents/reviewer/labels/staging")
        assert r.status_code == 200
        assert r.json()["labels"] == {}
        listing = await client.get("/api/context/agents/reviewer/versions")
        assert listing.json()["labels"] == {}

    @pytest.mark.anyio
    async def test_delete_absent_label_is_noop_200(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        await client.post("/api/context/agents/reviewer/versions", json={})
        r = await client.delete("/api/context/agents/reviewer/labels/ghost")
        assert r.status_code == 200
        assert r.json()["labels"] == {}

    @pytest.mark.anyio
    async def test_delete_reserved_label_latest_400(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        r = await client.delete("/api/context/agents/reviewer/labels/latest")
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_delete_non_shared_tier_400(self, client, tmp_path):
        _make_dir_agent(tmp_path, "reviewer")
        r = await client.delete(
            "/api/context/agents/reviewer/labels/staging?target_scope=project_local"
        )
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_delete_works_on_commands(self, client, tmp_path):
        _make_dir_command(tmp_path, "deploy")
        await client.post("/api/context/commands/deploy/versions", json={})
        await client.put("/api/context/commands/deploy/labels/staging", json={"version": "v1"})
        r = await client.delete("/api/context/commands/deploy/labels/staging")
        assert r.status_code == 200
        assert r.json()["labels"] == {}


# ---------------------------------------------------------------------------
# Privacy boundary (path redaction) + timeout branch
# ---------------------------------------------------------------------------


class TestPrivacyAndTimeout:
    @pytest.mark.anyio
    async def test_corrupt_manifest_error_redacts_filesystem_path(self, client, tmp_path):
        """A malformed versions.json surfaces a VersionError carrying the sidecar
        path; the route must redact it to ``<path>`` (privacy boundary)."""
        adir = _make_dir_agent(tmp_path, "reviewer")
        # ``[]`` is a valid JSON value but the wrong shape — load_manifest raises
        # a VersionError whose message embeds the absolute sidecar path.
        (adir / "versions.json").write_text("[]", encoding="utf-8")

        r = await client.get("/api/context/agents/reviewer/versions")
        assert r.status_code == 400
        detail = r.json()["detail"]
        assert "<path>" in detail
        assert str(tmp_path) not in detail
        assert ".memtomem" not in detail

    @pytest.mark.anyio
    async def test_timeout_during_create_returns_503(self, client, tmp_path, monkeypatch):
        """A TimeoutError from the (locked) versioning call maps to 503, not 500."""
        _make_dir_agent(tmp_path, "reviewer")

        def _raise_timeout(*args, **kwargs):
            raise TimeoutError

        monkeypatch.setattr(versioning, "create_version", _raise_timeout)
        r = await client.post("/api/context/agents/reviewer/versions", json={})
        assert r.status_code == 503
        assert "timed out" in r.json()["detail"].lower()
