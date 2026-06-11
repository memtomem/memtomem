"""Web sync routes — ``on_drop`` vocabulary + ``StrictDropError`` surface (#1247 id 47).

Pre-fix, ``SyncRequest.on_drop`` accepted any string: out-of-vocabulary values
slipped through to the engine where they silently behaved as ``"ignore"``
(neither the ``"error"`` nor the ``"warn"`` branch matched), and an explicit
``on_drop="error"`` raised ``StrictDropError`` mid-Phase-2 — earlier runtime
writes persisted (the #908 partial-write boundary) — which neither route
caught, surfacing as an opaque 500 with zero partial-write info.

These tests pin:

* unknown ``on_drop`` → FastAPI-native 422 (field_validator against the
  engine's ``ON_DROP_LEVELS`` — one vocabulary owner, mirroring CLI
  ``click.Choice`` and MCP ``_validate_on_drop``);
* ``on_drop="error"`` with a dropping canonical → 422 with a structured
  ``{reason_code: "strict_drop", message, generated}`` detail naming the
  writes that landed before the abort, which remain on disk;
* ``on_drop="warn"`` still syncs and reports ``dropped``.

App shape mirrors ``test_sync_privacy_block_surfaces.py`` — minimal FastAPI
app + ``get_project_root`` dependency override; the routes run the real
engine against a real tmp project.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memtomem.context.agents import CANONICAL_AGENT_ROOT
from memtomem.context.commands import CANONICAL_COMMAND_ROOT

from .helpers import set_home

# Carries Gemini-only AND Claude-only fields so some field drops for every
# runtime (same shape as test_server_tools_context_on_drop.py).
_FULL_AGENT = """---
name: beta-full
description: Reviews staged code for quality
tools: [Read, Grep, Glob]
model: sonnet
skills: [code-review]
isolation: worktree
kind: reviewer
temperature: 0.2
---

You are a meticulous code reviewer.
"""

_MINIMAL_AGENT = """---
name: alpha-minimal
description: Generic helper
---

Help with things.
"""

# gemini_commands drops argument-hint/allowed-tools; claude_commands supports
# the full schema and never drops (see test_context_commands.py strict tests).
_FULL_COMMAND = """---
description: Review a file for issues
argument-hint: [file-path]
allowed-tools: [Read, Grep]
model: sonnet
---

Review the file at $ARGUMENTS for issues.
"""

_MINIMAL_COMMAND = """---
description: Simple prompt
---

Say hi to $ARGUMENTS.
"""


def _client_for(router, project_root: Path) -> TestClient:
    from memtomem.web.deps import get_project_root

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_project_root] = lambda: project_root
    return TestClient(app)


def _seed(project_root: Path, canonical_root: str, files: dict[str, str]) -> None:
    root = project_root / canonical_root
    root.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (root / name).write_text(body, encoding="utf-8")


@pytest.fixture
def agents_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from memtomem.web.routes import context_agents

    set_home(monkeypatch, tmp_path / "home")
    return _client_for(context_agents.router, tmp_path)


@pytest.fixture
def commands_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    from memtomem.web.routes import context_commands

    set_home(monkeypatch, tmp_path / "home")
    return _client_for(context_commands.router, tmp_path)


class TestOnDropVocabulary:
    def test_agents_sync_rejects_unknown_on_drop(self, agents_client: TestClient) -> None:
        res = agents_client.post("/context/agents/sync", json={"on_drop": "bogus"})
        assert res.status_code == 422, res.text
        assert "on_drop" in res.text

    def test_commands_sync_rejects_unknown_on_drop(self, commands_client: TestClient) -> None:
        res = commands_client.post("/context/commands/sync", json={"on_drop": "bogus"})
        assert res.status_code == 422, res.text
        assert "on_drop" in res.text

    def test_agents_sync_accepts_full_vocabulary(
        self, agents_client: TestClient, tmp_path: Path
    ) -> None:
        _seed(tmp_path, CANONICAL_AGENT_ROOT, {"alpha-minimal.md": _MINIMAL_AGENT})
        for level in ("ignore", "warn", "error"):
            res = agents_client.post("/context/agents/sync", json={"on_drop": level})
            assert res.status_code == 200, f"{level}: {res.text}"


class TestStrictDropSurfaces:
    def test_agents_sync_error_returns_422_with_partial_writes(
        self, agents_client: TestClient, tmp_path: Path
    ) -> None:
        _seed(
            tmp_path,
            CANONICAL_AGENT_ROOT,
            {"alpha-minimal.md": _MINIMAL_AGENT, "beta-full.md": _FULL_AGENT},
        )

        res = agents_client.post("/context/agents/sync", json={"on_drop": "error"})

        assert res.status_code == 422, res.text
        detail = res.json()["detail"]
        assert detail["reason_code"] == "strict_drop"
        assert "beta-full" in detail["message"]
        # alpha-minimal sorted first → written before the abort; the response
        # must name it AND it must still exist on disk (#908 boundary).
        generated_paths = [g["path"] for g in detail["generated"]]
        assert any("alpha-minimal" in p for p in generated_paths)
        assert (tmp_path / ".claude/agents/alpha-minimal.md").is_file()
        assert not (tmp_path / ".claude/agents/beta-full.md").exists()

    def test_commands_sync_error_returns_422_with_partial_writes(
        self, commands_client: TestClient, tmp_path: Path
    ) -> None:
        _seed(
            tmp_path,
            CANONICAL_COMMAND_ROOT,
            {"alpha-minimal.md": _MINIMAL_COMMAND, "beta-full.md": _FULL_COMMAND},
        )

        res = commands_client.post("/context/commands/sync", json={"on_drop": "error"})

        assert res.status_code == 422, res.text
        detail = res.json()["detail"]
        assert detail["reason_code"] == "strict_drop"
        assert detail["generated"], "expected at least one pre-abort write reported"
        # claude_commands never drops — gemini's beta-full render aborts.
        assert (tmp_path / ".claude/commands/alpha-minimal.md").is_file()
        assert not (tmp_path / ".gemini/commands/beta-full.toml").exists()

    def test_agents_sync_warn_still_writes_and_reports_dropped(
        self, agents_client: TestClient, tmp_path: Path
    ) -> None:
        _seed(
            tmp_path,
            CANONICAL_AGENT_ROOT,
            {"alpha-minimal.md": _MINIMAL_AGENT, "beta-full.md": _FULL_AGENT},
        )

        res = agents_client.post("/context/agents/sync", json={"on_drop": "warn"})

        assert res.status_code == 200, res.text
        body = res.json()
        assert body["dropped"], "full agent must report dropped fields under warn"
        assert (tmp_path / ".claude/agents/beta-full.md").is_file()
