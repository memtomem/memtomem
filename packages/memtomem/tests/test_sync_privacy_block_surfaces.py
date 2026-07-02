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
# the MCP tool surface deliberately still round-trips the full message — not
# because MCP is a weaker boundary (#1539 redacts its incidental error/reason
# paths), but because this message IS the remediation: the caller must know
# exactly which file to fix.
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
        # branch (``McpServerParseError`` → ``error_kind="parse"``, no
        # reason_code) embedded the full canonical path until #1412, which routes
        # the parse detail through ``exc.safe_message`` (basename only) at every
        # web catch site — see ``TestMcpServersParseBranchPathFree`` below, which
        # exercises the REAL parse path (a genuinely malformed canonical file).
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


class TestMcpServersParseBranchPathFree:
    """#1412 — the mcp-servers parse-error 422 must be path-free at every web
    (loopback) catch site, the #1385 finding-1 invariant extended from the
    privacy branch to the parse branch.

    Unlike the privacy tests above (which monkeypatch the generator to raise a
    crafted error), these drive the REAL parse path: a genuinely malformed
    canonical ``.mcp.json`` reaches ``parse_mcp_server_text``, whose JSON-decode
    raise embeds the resolved canonical ``Path``. The fix renders
    ``exc.safe_message`` (basename + the JSON problem) instead of ``str(exc)``,
    so the resolved host path never reaches the dashboard while the CLI / MCP
    surfaces keep the full path.
    """

    # A trailing comma → ``json.JSONDecodeError`` ("Expecting property name
    # enclosed in double quotes"), the issue's reproduction. No secret shape, so
    # the sync route's Gate-A scan (which runs BEFORE the parser) passes and the
    # parse branch is actually reached.
    _MALFORMED = '{\n  "command": "echo",\n}\n'

    @staticmethod
    def _build_app(project_root: Path) -> TestClient:
        from memtomem.web.routes import context_mcp_servers
        from memtomem.web.routes._sync_phase import register_sync_phase_error_handler
        from memtomem.web.routes.context_projects import (
            resolve_scope_root,
            resolve_writable_scope_root,
        )

        app = FastAPI()
        app.include_router(context_mcp_servers.router)
        register_sync_phase_error_handler(app)
        # Override BOTH resolvers (reads use ``resolve_scope_root``, sync uses
        # the writable variant) so the routes run against a real project root
        # without the eligibility / selector machinery.
        app.dependency_overrides[resolve_scope_root] = lambda: project_root
        app.dependency_overrides[resolve_writable_scope_root] = lambda: project_root
        return TestClient(app)

    @staticmethod
    def _assert_path_free(body: dict, project_root: Path, *, basename: str) -> None:
        """Whole-body path-free assertions (no assumption about the body shape).

        Asserts the resolved canonical absolute path (and its parent dir) and
        the resolved project root never appear ANYWHERE in the body — not just
        in the message field, so a future shape change that echoes the path
        under a new key is caught (mirrors the privacy-branch whole-body check).
        The basename alone is not an absolute path, so it may (and should)
        survive; each test asserts its own actionable field separately.
        """
        from memtomem.context.mcp_servers import canonical_mcp_server_root

        resolved = canonical_mcp_server_root(project_root) / basename
        blob = json.dumps(body)
        assert str(resolved) not in blob, body  # full canonical path never leaks
        assert str(resolved.parent) not in blob, body  # nor its directory
        assert str(project_root.resolve()) not in blob, body  # nor the project root
        assert str(Path.home()) not in blob, body  # #1385 literal: no $HOME / username

    def _seed(self, tmp_path: Path) -> Path:
        from memtomem.context.mcp_servers import CANONICAL_MCP_SERVER_ROOT

        project_root = tmp_path / "proj"
        canon = project_root / CANONICAL_MCP_SERVER_ROOT
        canon.mkdir(parents=True)
        (canon / "broken.json").write_text(self._MALFORMED, encoding="utf-8")
        return project_root

    def test_sync_real_malformed_canonical_is_path_free(self, tmp_path: Path) -> None:
        # The route the issue found: SyncPhaseError(error_kind="parse") → string
        # ``detail``. One fix point covers both this standalone route and the
        # Sync-All aggregation (``_phase_error_envelope`` does ``str(detail)``).
        project_root = self._seed(tmp_path)
        res = self._build_app(project_root).post("/context/mcp-servers/sync")

        assert res.status_code == 422, res.text
        body = res.json()
        assert isinstance(body["detail"], str), body  # parse 422 keeps a string detail
        self._assert_path_free(body, project_root, basename="broken.json")
        message = body["detail"]
        assert "broken.json" in message, message  # names the artifact
        assert "invalid JSON" in message, message  # names the problem

    def test_create_real_malformed_content_is_path_free(self, tmp_path: Path) -> None:
        # create renders the object envelope (``_error`` → ``detail`` is a dict);
        # the path-free message lives at ``detail.message``.
        project_root = self._seed(tmp_path)
        res = self._build_app(project_root).post(
            "/context/mcp-servers",
            json={"name": "broken", "content": self._MALFORMED},
        )

        assert res.status_code == 422, res.text
        body = res.json()
        assert body["detail"]["error_kind"] == "parse", body
        self._assert_path_free(body, project_root, basename="broken.json")
        message = body["detail"]["message"]
        assert "broken.json" in message, message
        assert "invalid JSON" in message, message

    def test_update_real_malformed_content_is_path_free(self, tmp_path: Path) -> None:
        # update renders the object envelope like create. The seeded canonical
        # already exists (``is_file`` passes); the malformed *body* content hits
        # the parser before the mtime/lock dance, so any integer ``mtime_ns``
        # reaches the parse 422.
        project_root = self._seed(tmp_path)
        res = self._build_app(project_root).put(
            "/context/mcp-servers/broken",
            json={"content": self._MALFORMED, "mtime_ns": "0", "force": False},
        )

        assert res.status_code == 422, res.text
        body = res.json()
        assert body["detail"]["error_kind"] == "parse", body
        self._assert_path_free(body, project_root, basename="broken.json")
        message = body["detail"]["message"]
        assert "broken.json" in message, message
        assert "invalid JSON" in message, message

    def test_diff_real_malformed_canonical_is_path_free(self, tmp_path: Path) -> None:
        # The diff route routes the reason through ``sanitize_diff_reason``, but
        # that only strips a prefix-matching project root; ``safe_message`` is
        # path-free at the source regardless (macOS ``/tmp``→``/private/tmp``).
        project_root = self._seed(tmp_path)
        res = self._build_app(project_root).get("/context/mcp-servers/broken/diff")

        assert res.status_code == 200, res.text  # diff diagnoses, never 422s
        body = res.json()
        reason = body["runtimes"][0].get("reason", "")
        assert body["runtimes"][0]["status"] == "parse error", body
        self._assert_path_free(body, project_root, basename="broken.json")
        assert "broken.json" in reason, reason

    def test_list_symlinked_root_is_path_free(self, tmp_path: Path) -> None:
        # Codex #1412-review repro: under a symlinked / case-variant project
        # root the canonical path is ``.resolve()``'d but the route's
        # ``project_root`` is not, so the single-form strips left the absolute
        # resolved path in BOTH ``canonical_path`` (``_safe_rel`` fallback) and
        # the diff-row ``reason`` (``sanitize_diff_reason``). Pin both path-free.
        from memtomem.context.mcp_servers import CANONICAL_MCP_SERVER_ROOT

        real = (tmp_path / "real").resolve()
        canon = real / CANONICAL_MCP_SERVER_ROOT
        canon.mkdir(parents=True)
        (canon / "broken.json").write_text(self._MALFORMED, encoding="utf-8")
        link = tmp_path / "link"
        link.symlink_to(real)

        # The route receives the UNRESOLVED symlink as project_root.
        res = self._build_app(link).get("/context/mcp-servers")

        assert res.status_code == 200, res.text
        body = res.json()
        self._assert_path_free(body, link, basename="broken.json")  # resolves link→real
        server = body["mcp-servers"][0]
        assert server["canonical_path"] == ".memtomem/mcp-servers/broken.json", server
        assert server["runtimes"][0]["status"] == "parse error", server
        assert "broken.json" in server["runtimes"][0]["reason"], server

    def test_safe_message_is_basename_only(self) -> None:
        # The core contract every web catch site relies on: ``safe_message``
        # names only the basename while ``str(exc)`` keeps the full path for
        # the local-operator CLI (the MCP tool renders ``safe_message`` too
        # since #1539). Driven through the real parser (no hand-built
        # exception) so the raise site and the twin stay in lockstep.
        from memtomem.context.mcp_servers import McpServerParseError, parse_mcp_server_text

        src = Path("/abs/home-username/proj/.memtomem/mcp-servers/x.json")
        with pytest.raises(McpServerParseError) as ei:
            parse_mcp_server_text(self._MALFORMED, name="x", source=src)
        exc = ei.value
        assert str(src) in str(exc)  # the CLI keeps the full path
        assert str(src) not in exc.safe_message  # web boundary stays path-free
        assert str(src.parent) not in exc.safe_message
        assert exc.safe_message.startswith("invalid JSON in x.json:")  # names the artifact


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
