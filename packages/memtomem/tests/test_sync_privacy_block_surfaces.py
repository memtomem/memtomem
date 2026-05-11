"""Regression pins for #895 P2 review fold — surface-level translation of
``PrivacyBlockedError`` in non-CLI sync paths.

Pre-fold, ``raise_or_collect`` raised ``click.ClickException`` directly
from generic generator code; the CLI auto-handled the message but the
web routes and MCP context tool both fell through to their generic
exception handlers and turned a user-actionable privacy block into a
500 "Internal server error".

These tests pin:

* Web ``/api/context/{agents,commands,skills}/sync`` translates the
  block into HTTP 422 with the formatted user message in the body.
* MCP ``mem_context_generate`` / ``mem_context_sync`` returns a
  ``"privacy block: ..."`` string instead of letting the exception
  propagate to the tool harness.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memtomem.context.privacy_scan import FileScan, PrivacyBlockedError


_FAKE_BLOCKED = FileScan(Path("/tmp/p/.memtomem/agents/leak.md"), "blocked", 1)
_FAKE_MSG = "Gate A: leak.md contains 1 privacy pattern hit(s); fan-out rejected."


def _raise_privacy_block(*args, **kwargs):
    raise PrivacyBlockedError(
        _FAKE_MSG,
        blocked=_FAKE_BLOCKED,
        scope="project_shared",
        kind="agent",
        artifact_name="leak",
    )


def _build_app_with(router) -> FastAPI:
    """Minimal FastAPI app + ``get_project_root`` override.

    Mirrors ``test_web_csrf_middleware.py`` — boot the production
    factory pulls in storage/embedder which the sync route does not
    actually need. The dependency override is enough to satisfy
    ``project_root`` injection without touching the file system.
    """
    from memtomem.web.deps import get_project_root

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_project_root] = lambda: Path("/tmp/p")
    return app


class TestWebSyncTranslatesTo422:
    def test_agents_sync_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from memtomem.web.routes import context_agents

        monkeypatch.setattr(context_agents, "generate_all_agents", _raise_privacy_block)
        client = TestClient(_build_app_with(context_agents.router))

        res = client.post("/context/agents/sync", json={})

        assert res.status_code == 422, res.text
        # FastAPI's default HTTPException renders as ``{"detail": "..."}``;
        # the formatted message must round-trip so the UI can surface it.
        assert _FAKE_MSG in res.json()["detail"]

    def test_commands_sync_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from memtomem.web.routes import context_commands

        monkeypatch.setattr(context_commands, "generate_all_commands", _raise_privacy_block)
        client = TestClient(_build_app_with(context_commands.router))

        res = client.post("/context/commands/sync", json={})

        assert res.status_code == 422, res.text
        assert _FAKE_MSG in res.json()["detail"]

    def test_skills_sync_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from memtomem.web.routes import context_skills

        monkeypatch.setattr(context_skills, "generate_all_skills", _raise_privacy_block)
        client = TestClient(_build_app_with(context_skills.router))

        res = client.post("/context/skills/sync")

        assert res.status_code == 422, res.text
        assert _FAKE_MSG in res.json()["detail"]


class TestMcpContextToolTranslates:
    """The MCP context tool returns a string body; on privacy block it must
    return a ``"privacy block: ..."`` prefix instead of raising into the
    tool harness. Lazy imports inside the tool body pick up the
    monkeypatched generator at call time.
    """

    def _setup_project(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        # ``_find_project_root`` walks up for a ``.git`` parent — give it one.
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".git").mkdir()
        monkeypatch.chdir(project)
        return project

    @pytest.mark.anyio
    async def test_generate_tool_skills_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memtomem.context import skills as ctx_skills
        from memtomem.server.tools.context import mem_context_generate

        self._setup_project(tmp_path, monkeypatch)
        monkeypatch.setattr(ctx_skills, "generate_all_skills", _raise_privacy_block)

        result = await mem_context_generate(include="skills")

        assert isinstance(result, str)
        assert result.startswith("privacy block:"), result
        assert _FAKE_MSG in result

    @pytest.mark.anyio
    async def test_sync_tool_agents_block(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memtomem.context import agents as ctx_agents
        from memtomem.server.tools.context import mem_context_sync

        self._setup_project(tmp_path, monkeypatch)
        monkeypatch.setattr(ctx_agents, "generate_all_agents", _raise_privacy_block)

        result = await mem_context_sync(include="agents")

        assert isinstance(result, str)
        assert result.startswith("privacy block:"), result
        assert _FAKE_MSG in result
