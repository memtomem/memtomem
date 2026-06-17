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

import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memtomem.context.privacy_scan import FileScan, PrivacyBlockedError


_LEAK_PATH = "/tmp/p/.memtomem/agents/leak.md"
_FAKE_BLOCKED = FileScan(Path(_LEAK_PATH), "blocked", 1)
# Mirror the real engine remediation, which ends with the absolute canonical
# path (``privacy_scan.py``). The web 422 must NOT echo it (#1385 finding 1);
# the MCP tool surface (a different trust boundary — the result goes to the
# calling agent, not a loopback browser) still round-trips the full message.
_FAKE_MSG = (
    "Gate A: leak.md contains 1 privacy pattern hit(s); fan-out rejected. "
    f"Or remove the secret from {_LEAK_PATH} before re-running sync."
)


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

    Registers the REAL ``SyncPhaseError`` handler the app ships
    (#1409) via the shared registrar — not a hand-rolled double — so
    the ``reason_code`` hoisting these tests pin can't drift from
    production. FastAPI's built-in ``HTTPException`` handler (which a
    bare app would use) drops ``reason_code``; the registrar installs
    the handler that surfaces it alongside the path-free string detail.
    """
    from memtomem.web.deps import get_project_root
    from memtomem.web.routes._sync_phase import register_sync_phase_error_handler

    app = FastAPI()
    app.include_router(router)
    register_sync_phase_error_handler(app)
    app.dependency_overrides[get_project_root] = lambda: Path("/tmp/p")
    return app


def _assert_path_free_privacy_422(res, *, detail_contains: str) -> None:
    """Shared assertions for a per-type sync privacy block (#1385/#1409).

    The wire shape is the path-free *string* ``detail`` (#1385 finding 1 —
    the engine message embeds the absolute canonical path, which must never
    reach the loopback dashboard) PLUS the top-level ``reason_code`` sibling
    (#1409) the client maps to a localized hint. ``reason_code`` itself is a
    fixed token, so it cannot leak — but assert the WHOLE body is path-free
    so a future shape change (e.g. echoing the path under a new key) is caught.
    """
    assert res.status_code == 422, res.text
    body = res.json()
    detail = body["detail"]
    assert isinstance(detail, str), body  # detail stays a STRING, not an envelope
    assert _LEAK_PATH not in detail  # #1385 finding 1: host path never leaks
    assert detail_contains in detail
    assert body.get("reason_code") == "privacy_blocked", body  # #1409 wire field
    assert _LEAK_PATH not in json.dumps(body)  # whole body path-free (reason_code too)


class TestWebSyncTranslatesTo422:
    def test_agents_sync_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from memtomem.web.routes import context_agents

        monkeypatch.setattr(context_agents, "generate_all_agents", _raise_privacy_block)
        client = TestClient(_build_app_with(context_agents.router))

        res = client.post("/context/agents/sync", json={})

        # The detail is a fixed, path-free string (the engine message embeds the
        # absolute canonical path, which must not reach the loopback dashboard);
        # ``reason_code`` rides alongside it as a top-level sibling (#1409).
        _assert_path_free_privacy_422(res, detail_contains="secret was detected")

    def test_commands_sync_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from memtomem.web.routes import context_commands

        monkeypatch.setattr(context_commands, "generate_all_commands", _raise_privacy_block)
        client = TestClient(_build_app_with(context_commands.router))

        res = client.post("/context/commands/sync", json={})

        _assert_path_free_privacy_422(res, detail_contains="secret was detected")

    def test_skills_sync_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from memtomem.web.routes import context_skills

        monkeypatch.setattr(context_skills, "generate_all_skills", _raise_privacy_block)
        client = TestClient(_build_app_with(context_skills.router))

        res = client.post("/context/skills/sync")

        _assert_path_free_privacy_422(res, detail_contains="secret was detected")

    def test_mcp_servers_sync_returns_422(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # MCP servers differ from skills/agents/commands: the PRIVACY 422
        # surfaces the engine message verbatim (``str(exc)``) rather than the
        # fixed ``PRIVACY_BLOCK_DETAIL`` constant. For the privacy error that
        # message is path-free by the ENGINE's construction —
        # ``McpServerPrivacyError`` names only ``source_path.name`` (the
        # basename), never the resolved host path (``mcp_servers.py``). This test
        # pins both halves of the #1409 contract for the PRIVACY branch —
        # ``reason_code`` on the wire AND a path-free body.
        #
        # Scope note: this covers the privacy branch only. The route's OTHER 422
        # branch (``McpServerParseError`` → ``str(exc)`` with ``error_kind=
        # "parse"``, no reason_code) embeds the full canonical path and is a
        # SEPARATE, pre-existing $HOME-leak concern (#1385-class, on the parse
        # path) that predates and is untouched by #1409 — tracked on its own.
        from memtomem.context.mcp_servers import McpServerPrivacyError
        from memtomem.web.routes import context_mcp_servers

        def _raise_mcp_block(*args, **kwargs):
            raise McpServerPrivacyError(
                "Gate A: leak.mcp.json contains 1 privacy pattern hit(s); "
                "MCP server fan-out to project .mcp.json rejected."
            )

        monkeypatch.setattr(context_mcp_servers, "generate_all_mcp_servers", _raise_mcp_block)
        client = TestClient(_build_app_with(context_mcp_servers.router))

        res = client.post("/context/mcp-servers/sync")

        _assert_path_free_privacy_422(res, detail_contains="MCP server fan-out")


class TestSyncPhaseErrorHandlerContract:
    """The #1409 handler hoists ``reason_code`` to a top-level sibling ONLY for
    *string* details. A string is the one detail shape that cannot carry its own
    ``reason_code``, so it is the only one that needs the hoist; everything
    structured keeps its bare ``{"detail": …}`` wire shape. These pin all four
    branches so the boundary can't silently widen (Codex review)."""

    @staticmethod
    def _client(detail, reason_code) -> TestClient:
        from memtomem.web.routes._sync_phase import (
            SyncPhaseError,
            register_sync_phase_error_handler,
        )

        app = FastAPI()

        @app.post("/boom")
        async def _boom() -> None:
            raise SyncPhaseError(422, detail, error_kind="validation", reason_code=reason_code)

        register_sync_phase_error_handler(app)
        return TestClient(app)

    def test_string_detail_hoists_reason_code(self) -> None:
        res = self._client("blocked", "privacy_blocked").post("/boom")
        assert res.status_code == 422, res.text
        assert res.json() == {"detail": "blocked", "reason_code": "privacy_blocked"}

    def test_dict_detail_keeps_reason_code_inside(self) -> None:
        # strict_drop shape: ``reason_code`` lives INSIDE the detail dict; the
        # wire stays a bare ``{"detail": {...}}`` with NO top-level sibling so
        # the strict-drop contract is byte-identical.
        detail = {"reason_code": "strict_drop", "message": "partial", "generated": []}
        res = self._client(detail, "strict_drop").post("/boom")
        assert res.json() == {"detail": detail}

    def test_non_string_non_dict_detail_is_not_hoisted(self) -> None:
        # A future structured (non-string, non-dict) detail must NOT gain a
        # top-level reason_code — only string details, which can't carry their
        # own, are hoisted. ``isinstance(str)`` (not ``not dict``) enforces this.
        res = self._client(["a", "b"], "privacy_blocked").post("/boom")
        assert res.json() == {"detail": ["a", "b"]}

    def test_no_reason_code_stays_bare(self) -> None:
        # A string-detail SyncPhaseError with ``reason_code=None`` (e.g. an mcp
        # parse error) keeps the historical bare ``{"detail": …}`` shape.
        res = self._client("parse failed", None).post("/boom")
        assert res.json() == {"detail": "parse failed"}


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
