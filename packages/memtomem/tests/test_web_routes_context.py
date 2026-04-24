"""Tests for Context Gateway web routes (overview + skills + commands + agents)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.context.skills import SKILL_MANIFEST
from memtomem.web.app import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_app(tmp_path: Path):
    """App with project_root pointing to a temp directory."""
    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = tmp_path
    # Minimal stubs for deps the app might check
    application.state.storage = AsyncMock()
    application.state.config = None
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


def _make_skill(tmp_path: Path, name: str, content: str = "# Test skill\n") -> Path:
    """Create a canonical skill directory with SKILL.md."""
    skill_dir = tmp_path / ".memtomem" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_MANIFEST).write_text(content, encoding="utf-8")
    return skill_dir


def _make_runtime_skill(
    tmp_path: Path,
    runtime_dir: str,
    name: str,
    content: str = "# Test skill\n",
) -> Path:
    """Create a runtime skill directory."""
    skill_dir = tmp_path / runtime_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_MANIFEST).write_text(content, encoding="utf-8")
    return skill_dir


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


class TestOverview:
    @pytest.mark.anyio
    async def test_empty_project(self, client: AsyncClient):
        r = await client.get("/api/context/overview")
        assert r.status_code == 200
        data = r.json()
        assert data["skills"]["total"] == 0
        # commands/agents may pick up user-scope Codex files from ~/.codex/
        assert "total" in data["commands"]
        assert "total" in data["agents"]

    @pytest.mark.anyio
    async def test_with_skills(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "code-review")
        r = await client.get("/api/context/overview")
        data = r.json()
        assert data["skills"]["total"] >= 1


# ---------------------------------------------------------------------------
# Skills — List
# ---------------------------------------------------------------------------


class TestListSkills:
    @pytest.mark.anyio
    async def test_empty(self, client: AsyncClient):
        r = await client.get("/api/context/skills")
        assert r.status_code == 200
        assert r.json()["skills"] == []

    @pytest.mark.anyio
    async def test_with_items(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "alpha")
        _make_skill(tmp_path, "beta")
        r = await client.get("/api/context/skills")
        data = r.json()
        names = [s["name"] for s in data["skills"]]
        assert "alpha" in names
        assert "beta" in names

    @pytest.mark.anyio
    async def test_includes_runtime_status(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "review")
        r = await client.get("/api/context/skills")
        skill = r.json()["skills"][0]
        assert skill["runtimes"]  # should have entries for each generator
        statuses = [rt["status"] for rt in skill["runtimes"]]
        # All should be "missing target" since we haven't synced
        assert all(s == "missing target" for s in statuses)


# ---------------------------------------------------------------------------
# Skills — Read
# ---------------------------------------------------------------------------


class TestReadSkill:
    @pytest.mark.anyio
    async def test_read(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "demo", "# Demo skill\nDetails here.\n")
        r = await client.get("/api/context/skills/demo")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "demo"
        assert "Demo skill" in data["content"]
        assert int(data["mtime_ns"]) > 0

    @pytest.mark.anyio
    async def test_not_found(self, client: AsyncClient):
        r = await client.get("/api/context/skills/nonexistent")
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_auxiliary_files(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "rich")
        scripts_dir = tmp_path / ".memtomem" / "skills" / "rich" / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.sh").write_text("#!/bin/bash\necho hello\n", encoding="utf-8")
        r = await client.get("/api/context/skills/rich")
        data = r.json()
        paths = [f["path"] for f in data["files"]]
        assert any("run.sh" in p for p in paths)


# ---------------------------------------------------------------------------
# Skills — Create
# ---------------------------------------------------------------------------


class TestCreateSkill:
    @pytest.mark.anyio
    async def test_create(self, client: AsyncClient, tmp_path: Path):
        r = await client.post(
            "/api/context/skills",
            json={"name": "new-skill", "content": "# New\n"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "new-skill"
        # Verify file exists on disk
        assert (tmp_path / ".memtomem" / "skills" / "new-skill" / SKILL_MANIFEST).is_file()

    @pytest.mark.anyio
    async def test_create_duplicate(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "existing")
        r = await client.post(
            "/api/context/skills",
            json={"name": "existing", "content": "# Dup\n"},
        )
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_create_invalid_name(self, client: AsyncClient):
        r = await client.post(
            "/api/context/skills",
            json={"name": "../escape", "content": "bad"},
        )
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Skills — Update
# ---------------------------------------------------------------------------


class TestUpdateSkill:
    @pytest.mark.anyio
    async def test_update(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "upd")
        # Read to get mtime_ns
        r = await client.get("/api/context/skills/upd")
        mtime_ns = r.json()["mtime_ns"]

        r = await client.put(
            "/api/context/skills/upd",
            json={"content": "# Updated\n", "mtime_ns": mtime_ns},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "upd"
        # Verify content changed
        content = (tmp_path / ".memtomem" / "skills" / "upd" / SKILL_MANIFEST).read_text(
            encoding="utf-8"
        )
        assert "Updated" in content

    @pytest.mark.anyio
    async def test_mtime_conflict(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "conflict")
        r = await client.put(
            "/api/context/skills/conflict",
            json={"content": "# Changed\n", "mtime_ns": "0"},  # wrong mtime_ns
        )
        assert r.status_code == 409
        data = r.json()
        assert data["status"] == "aborted"


# ---------------------------------------------------------------------------
# Skills — Delete
# ---------------------------------------------------------------------------


class TestDeleteSkill:
    @pytest.mark.anyio
    async def test_delete_canonical_only(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "del-me")
        r = await client.delete("/api/context/skills/del-me")
        assert r.status_code == 200
        assert not (tmp_path / ".memtomem" / "skills" / "del-me").exists()

    @pytest.mark.anyio
    async def test_delete_cascade(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "cascade")
        _make_runtime_skill(tmp_path, ".claude/skills", "cascade")
        r = await client.delete("/api/context/skills/cascade?cascade=true")
        assert r.status_code == 200
        assert not (tmp_path / ".memtomem" / "skills" / "cascade").exists()
        assert not (tmp_path / ".claude" / "skills" / "cascade").exists()

    @pytest.mark.anyio
    async def test_delete_missing_is_idempotent(self, client: AsyncClient):
        """DELETE of a missing skill succeeds with an empty result (idempotent)."""
        r = await client.delete("/api/context/skills/nope")
        assert r.status_code == 200
        assert r.json()["deleted"] == []


# ---------------------------------------------------------------------------
# Skills — Diff
# ---------------------------------------------------------------------------


class TestDiffSkill:
    @pytest.mark.anyio
    async def test_diff_missing_target(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "orphan")
        r = await client.get("/api/context/skills/orphan/diff")
        assert r.status_code == 200
        data = r.json()
        assert data["canonical_content"] is not None
        assert any(rt["status"] == "missing target" for rt in data["runtimes"])

    @pytest.mark.anyio
    async def test_diff_in_sync(self, client: AsyncClient, tmp_path: Path):
        content = "# Synced\n"
        _make_skill(tmp_path, "synced", content)
        _make_runtime_skill(tmp_path, ".claude/skills", "synced", content)
        r = await client.get("/api/context/skills/synced/diff")
        data = r.json()
        claude_rt = [rt for rt in data["runtimes"] if rt["runtime"] == "claude_skills"][0]
        assert claude_rt["status"] == "in sync"

    @pytest.mark.anyio
    async def test_diff_out_of_sync(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "diverged", "# V1\n")
        _make_runtime_skill(tmp_path, ".claude/skills", "diverged", "# V2\n")
        r = await client.get("/api/context/skills/diverged/diff")
        data = r.json()
        claude_rt = [rt for rt in data["runtimes"] if rt["runtime"] == "claude_skills"][0]
        assert claude_rt["status"] == "out of sync"
        assert claude_rt["runtime_content"] == "# V2\n"


# ---------------------------------------------------------------------------
# Skills — Sync
# ---------------------------------------------------------------------------


class TestSyncSkills:
    @pytest.mark.anyio
    async def test_sync(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "fan-out", "# Ready\n")
        r = await client.post("/api/context/skills/sync")
        assert r.status_code == 200
        data = r.json()
        assert len(data["generated"]) >= 3  # claude + gemini + codex
        # Verify files created
        assert (tmp_path / ".claude" / "skills" / "fan-out" / SKILL_MANIFEST).is_file()

    @pytest.mark.anyio
    async def test_sync_empty(self, client: AsyncClient):
        r = await client.post("/api/context/skills/sync")
        assert r.status_code == 200
        data = r.json()
        assert data["skipped"]


# ---------------------------------------------------------------------------
# Skills — Import
# ---------------------------------------------------------------------------


class TestImportSkills:
    @pytest.mark.anyio
    async def test_import(self, client: AsyncClient, tmp_path: Path):
        _make_runtime_skill(tmp_path, ".claude/skills", "from-claude", "# Imported\n")
        r = await client.post(
            "/api/context/skills/import",
            json={"overwrite": False},
        )
        assert r.status_code == 200
        data = r.json()
        assert any(i["name"] == "from-claude" for i in data["imported"])
        assert (tmp_path / ".memtomem" / "skills" / "from-claude" / SKILL_MANIFEST).is_file()

    @pytest.mark.anyio
    async def test_import_skips_existing(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "already")
        _make_runtime_skill(tmp_path, ".claude/skills", "already", "# Different\n")
        r = await client.post(
            "/api/context/skills/import",
            json={"overwrite": False},
        )
        data = r.json()
        assert any(s["name"] == "already" for s in data["skipped"])

    @pytest.mark.anyio
    async def test_import_empty(self, client: AsyncClient):
        r = await client.post(
            "/api/context/skills/import",
            json={},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["imported"] == []


# ---------------------------------------------------------------------------
# Path traversal guard
# ---------------------------------------------------------------------------


class TestPathSafety:
    @pytest.mark.anyio
    async def test_slash_in_name(self, client: AsyncClient):
        """Names with slashes/backslashes are rejected before touching the FS."""
        r = await client.post(
            "/api/context/skills",
            json={"name": "sub/dir", "content": "bad"},
        )
        assert r.status_code == 400

    # Note: path tokens like "." / ".." are normalised by the HTTP layer before
    # reaching the route handler, so validate_name's reserved-token check is
    # exercised from the POST body in test_web_routes_context_mutators.py
    # (see test_POST_rejects_hostile_name with hostile_name in {".", ".."}).


# ===========================================================================
# Commands
# ===========================================================================

_CMD_CONTENT = """---
description: Review code
argument-hint: "[file-path]"
allowed-tools: [Read, Grep]
model: opus
---
Review the provided file and suggest improvements.
$ARGUMENTS
"""


def _make_command(tmp_path: Path, name: str, content: str = _CMD_CONTENT) -> Path:
    cmd_dir = tmp_path / ".memtomem" / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    cmd_file = cmd_dir / f"{name}.md"
    cmd_file.write_text(content, encoding="utf-8")
    return cmd_file


def _make_runtime_command(
    tmp_path: Path, runtime_dir: str, name: str, ext: str = ".md", content: str = "# rt\n"
) -> Path:
    rt_dir = tmp_path / runtime_dir
    rt_dir.mkdir(parents=True, exist_ok=True)
    f = rt_dir / f"{name}{ext}"
    f.write_text(content, encoding="utf-8")
    return f


class TestListCommands:
    @pytest.mark.anyio
    async def test_empty(self, client: AsyncClient):
        r = await client.get("/api/context/commands")
        assert r.status_code == 200
        # May include user-scope Codex prompts from ~/.codex/prompts/
        canonicals = [c for c in r.json()["commands"] if c["canonical_path"] is not None]
        assert canonicals == []

    @pytest.mark.anyio
    async def test_with_items(self, client: AsyncClient, tmp_path: Path):
        _make_command(tmp_path, "review")
        r = await client.get("/api/context/commands")
        names = [c["name"] for c in r.json()["commands"]]
        assert "review" in names


class TestReadCommand:
    @pytest.mark.anyio
    async def test_read_with_fields(self, client: AsyncClient, tmp_path: Path):
        _make_command(tmp_path, "review")
        r = await client.get("/api/context/commands/review")
        assert r.status_code == 200
        data = r.json()
        assert data["fields"]["description"] == "Review code"
        assert data["fields"]["model"] == "opus"

    @pytest.mark.anyio
    async def test_not_found(self, client: AsyncClient):
        r = await client.get("/api/context/commands/nope")
        assert r.status_code == 404


class TestRenderedCommand:
    @pytest.mark.anyio
    async def test_rendered_shows_dropped(self, client: AsyncClient, tmp_path: Path):
        _make_command(tmp_path, "review")
        r = await client.get("/api/context/commands/review/rendered")
        assert r.status_code == 200
        data = r.json()
        assert data["canonical_content"]
        # Gemini drops argument-hint, allowed-tools, model
        gemini = [rt for rt in data["runtimes"] if rt["runtime"] == "gemini_commands"]
        if gemini:
            assert gemini[0]["dropped_fields"]
            assert gemini[0]["content"]  # rendered TOML


class TestCommandCRUD:
    @pytest.mark.anyio
    async def test_create_update_delete(self, client: AsyncClient, tmp_path: Path):
        # Create
        r = await client.post(
            "/api/context/commands",
            json={"name": "test-cmd", "content": "---\ndescription: test\n---\nBody\n"},
        )
        assert r.status_code == 200

        # Read + update
        r = await client.get("/api/context/commands/test-cmd")
        mtime_ns = r.json()["mtime_ns"]
        r = await client.put(
            "/api/context/commands/test-cmd",
            json={
                "content": "---\ndescription: updated\n---\nNew body\n",
                "mtime_ns": mtime_ns,
            },
        )
        assert r.status_code == 200

        # Delete
        r = await client.delete("/api/context/commands/test-cmd")
        assert r.status_code == 200


class TestSyncCommands:
    @pytest.mark.anyio
    async def test_sync_with_dropped(self, client: AsyncClient, tmp_path: Path):
        _make_command(tmp_path, "review")
        r = await client.post(
            "/api/context/commands/sync",
            json={"on_drop": "warn"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["generated"]
        # Some runtimes should have dropped fields (allowed-tools, model)
        assert data["dropped"]


class TestImportCommands:
    @pytest.mark.anyio
    async def test_import_from_claude(self, client: AsyncClient, tmp_path: Path):
        _make_runtime_command(tmp_path, ".claude/commands", "from-claude", ".md", _CMD_CONTENT)
        r = await client.post("/api/context/commands/import", json={})
        assert r.status_code == 200
        data = r.json()
        assert any(i["name"] == "from-claude" for i in data["imported"])


# ===========================================================================
# Agents
# ===========================================================================

_AGENT_CONTENT = """---
name: reviewer
description: Code review agent
tools: [Read, Grep, Glob]
model: opus
skills: [code-review]
isolation: repo
---
You are a code review agent. Review files thoroughly.
"""


def _make_agent(tmp_path: Path, name: str, content: str = _AGENT_CONTENT) -> Path:
    agent_dir = tmp_path / ".memtomem" / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agent_dir / f"{name}.md"
    agent_file.write_text(content, encoding="utf-8")
    return agent_file


def _make_runtime_agent(
    tmp_path: Path,
    runtime_dir: str,
    name: str,
    content: str = "---\nname: rt\ndescription: rt\n---\nBody\n",
) -> Path:
    rt_dir = tmp_path / runtime_dir
    rt_dir.mkdir(parents=True, exist_ok=True)
    f = rt_dir / f"{name}.md"
    f.write_text(content, encoding="utf-8")
    return f


class TestListAgents:
    @pytest.mark.anyio
    async def test_empty(self, client: AsyncClient):
        r = await client.get("/api/context/agents")
        assert r.status_code == 200
        # May include user-scope Codex agents from ~/.codex/agents/
        canonicals = [a for a in r.json()["agents"] if a["canonical_path"] is not None]
        assert canonicals == []

    @pytest.mark.anyio
    async def test_with_items(self, client: AsyncClient, tmp_path: Path):
        _make_agent(tmp_path, "reviewer")
        r = await client.get("/api/context/agents")
        names = [a["name"] for a in r.json()["agents"]]
        assert "reviewer" in names


class TestReadAgent:
    @pytest.mark.anyio
    async def test_read_with_fields(self, client: AsyncClient, tmp_path: Path):
        _make_agent(tmp_path, "reviewer")
        r = await client.get("/api/context/agents/reviewer")
        assert r.status_code == 200
        data = r.json()
        assert data["fields"]["description"] == "Code review agent"
        assert data["fields"]["model"] == "opus"
        assert "Read" in data["fields"]["tools"]

    @pytest.mark.anyio
    async def test_not_found(self, client: AsyncClient):
        r = await client.get("/api/context/agents/nope")
        assert r.status_code == 404


class TestRenderedAgent:
    @pytest.mark.anyio
    async def test_rendered_shows_dropped_and_field_map(self, client: AsyncClient, tmp_path: Path):
        _make_agent(tmp_path, "reviewer")
        r = await client.get("/api/context/agents/reviewer/rendered")
        assert r.status_code == 200
        data = r.json()
        assert data["field_map"]
        # Codex should drop multiple fields
        codex = [rt for rt in data["runtimes"] if rt["runtime"] == "codex_agents"]
        if codex:
            assert len(codex[0]["dropped_fields"]) >= 3
        # Field map should show tools as False for codex
        if "tools" in data["field_map"] and "codex_agents" in data["field_map"]["tools"]:
            assert data["field_map"]["tools"]["codex_agents"] is False


class TestAgentCRUD:
    @pytest.mark.anyio
    async def test_create_update_delete(self, client: AsyncClient, tmp_path: Path):
        # Create
        r = await client.post(
            "/api/context/agents",
            json={
                "name": "test-agent",
                "content": "---\nname: test-agent\ndescription: test\n---\nBody\n",
            },
        )
        assert r.status_code == 200

        # Read + update
        r = await client.get("/api/context/agents/test-agent")
        mtime_ns = r.json()["mtime_ns"]
        r = await client.put(
            "/api/context/agents/test-agent",
            json={
                "content": "---\nname: test-agent\ndescription: updated\n---\nNew\n",
                "mtime_ns": mtime_ns,
            },
        )
        assert r.status_code == 200

        # Delete
        r = await client.delete("/api/context/agents/test-agent")
        assert r.status_code == 200


class TestSyncAgents:
    @pytest.mark.anyio
    async def test_sync_with_dropped(self, client: AsyncClient, tmp_path: Path):
        _make_agent(tmp_path, "reviewer")
        r = await client.post("/api/context/agents/sync", json={"on_drop": "warn"})
        assert r.status_code == 200
        data = r.json()
        assert data["generated"]
        assert data["dropped"]  # Codex/Gemini should drop fields


class TestImportAgents:
    @pytest.mark.anyio
    async def test_import_from_claude(self, client: AsyncClient, tmp_path: Path):
        _make_runtime_agent(tmp_path, ".claude/agents", "from-claude")
        r = await client.post("/api/context/agents/import", json={})
        assert r.status_code == 200
        data = r.json()
        assert any(i["name"] == "from-claude" for i in data["imported"])
