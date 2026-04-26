"""Tests for ``validate_agent_id`` and its enforcement at the CLI + MCP
boundaries that build ``agent-runtime:<agent_id>`` namespaces.

Covers the gate added in #486 — without validation, hostile-shaped values
like ``"foo:bar"`` or ``"../x"`` round-trip into storage as malformed
namespace strings (e.g. ``"agent-runtime:foo:bar"``). Both ``mm session
start`` / ``mm session wrap`` (CLI) and ``mem_session_start`` (MCP) must
reject the same set with an identical core error message so fixing one
surface doesn't leave the other open.

Charset coverage for the underlying ``validate_name`` lives in
``test_context_names.py``; this file pins the **wiring**: that the
validator is actually called at every entry point that concatenates
``AGENT_NAMESPACE_PREFIX`` with caller input, and that the storage layer
never sees a malformed namespace from either surface.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.constants import InvalidNameError, validate_agent_id
from memtomem.server.context import AppContext
from memtomem.server.tools.multi_agent import mem_agent_register, mem_agent_search
from memtomem.server.tools.session import mem_session_start

# Hostile-shaped values that must be rejected at every boundary. Each
# value would otherwise concatenate into a malformed namespace
# (``agent-runtime:foo:bar``, ``agent-runtime:../etc``, etc.) and
# round-trip into storage / search.
HOSTILE_AGENT_IDS = [
    "foo:bar",  # collides with the namespace separator
    "../x",  # path traversal
    "..",  # reserved path token
    "a/b",  # path separator
    "a\\b",  # windows-style separator
    "  spaces  ",  # surrounding whitespace
    "a b",  # internal whitespace
    "a\u200bb",  # zero-width space — escape sequence so it isn't an invisible source artifact
    "a\x00b",  # null byte
    "a\nb",  # newline
    "-leading-dash",  # collides with click flag parsing
    "",  # empty
    "   ",  # all whitespace
]


class _StubCtx:
    """Minimal stand-in for MCP ``Context`` so async tools can be invoked
    directly. Mirrors the helper in ``test_sessions``.
    """

    def __init__(self, app: AppContext) -> None:
        class _RC:
            pass

        self.request_context = _RC()
        self.request_context.lifespan_context = app


# ---------------------------------------------------------------------------
# validate_agent_id (thin wrapper around validate_name)
# ---------------------------------------------------------------------------


def test_validate_agent_id_accepts_canonical_charset() -> None:
    for value in ("planner", "agent-1", "v2.0", "claude_code", "default"):
        assert validate_agent_id(value) == value


@pytest.mark.parametrize("value", HOSTILE_AGENT_IDS)
def test_validate_agent_id_rejects_hostile_inputs(value: str) -> None:
    with pytest.raises(InvalidNameError, match="invalid agent-id"):
        validate_agent_id(value)


# ---------------------------------------------------------------------------
# MCP boundary — mem_session_start
# ---------------------------------------------------------------------------


class TestMcpBoundary:
    """``mem_session_start`` must reject malformed agent_ids before any
    namespace concatenation reaches storage. ``tool_handler`` surfaces the
    ``InvalidNameError`` (a ``ValueError`` subclass) as ``"Error: ..."``.
    """

    @pytest.mark.asyncio
    async def test_colon_in_agent_id_returns_error_string(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_session_start(agent_id="foo:bar", ctx=ctx)  # type: ignore[arg-type]

        assert out.startswith("Error: invalid agent-id")
        assert "'foo:bar'" in out

    @pytest.mark.asyncio
    async def test_malformed_namespace_never_reaches_storage(self, components):
        """Regression pin: ``agent-runtime:foo:bar`` must not appear in any
        session row even if validation regresses to a warning.
        """
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="foo:bar", ctx=ctx)  # type: ignore[arg-type]

        rows = await app.storage.list_sessions()
        assert not any("agent-runtime:foo:bar" in (r["namespace"] or "") for r in rows)
        assert app.current_session_id is None
        assert app.current_agent_id is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_id", HOSTILE_AGENT_IDS)
    async def test_hostile_agent_ids_all_rejected(self, components, agent_id):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_session_start(agent_id=agent_id, ctx=ctx)  # type: ignore[arg-type]

        assert out.startswith("Error: invalid agent-id")

    @pytest.mark.asyncio
    async def test_valid_agent_id_still_succeeds(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]

        assert "Session started" in out
        assert app.current_agent_id == "planner"


# ---------------------------------------------------------------------------
# CLI boundary — mm session start / mm session wrap
# ---------------------------------------------------------------------------


def _patched_cli_components(comp):
    @asynccontextmanager
    async def fake():
        yield comp

    return fake


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def storage_mock():
    """Mock storage that fails loudly if any session-creating call lands —
    the validation gate must short-circuit before storage is ever touched
    on the rejection path.
    """
    return SimpleNamespace(
        create_session=AsyncMock(),
        end_session=AsyncMock(),
        get_session_events=AsyncMock(return_value=[]),
    )


class TestCliBoundary:
    def test_session_start_rejects_colon(self, runner, monkeypatch, storage_mock):
        comp = SimpleNamespace(storage=storage_mock)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["session", "start", "--agent-id", "foo:bar"])

        assert result.exit_code != 0
        assert "invalid agent-id" in result.output
        assert "'foo:bar'" in result.output
        # Regression pin: storage never received a malformed namespace.
        storage_mock.create_session.assert_not_called()

    def test_session_start_rejects_path_traversal(self, runner, monkeypatch, storage_mock):
        comp = SimpleNamespace(storage=storage_mock)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["session", "start", "--agent-id", "../etc"])

        assert result.exit_code != 0
        assert "invalid agent-id" in result.output
        storage_mock.create_session.assert_not_called()

    def test_session_wrap_rejects_colon_before_subprocess(self, runner, monkeypatch, storage_mock):
        """``mm session wrap`` must fail BEFORE the wrapped command runs —
        otherwise an invalid agent_id leaves a half-set state file plus a
        rogue child process, and the user only sees a Warning.
        """
        import subprocess
        from unittest.mock import MagicMock

        comp = SimpleNamespace(storage=storage_mock)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        # Pin the contract directly: ``subprocess.run`` is not invoked
        # on the rejection path. Without this, we'd only be observing
        # that storage stayed clean — subprocess could still launch and
        # leave a rogue child plus the half-set state file the docstring
        # warns about. ``mm session wrap`` does ``import subprocess``
        # inside the function, so patching the module attribute lands
        # before the lazy import resolves it.
        run_mock = MagicMock()
        monkeypatch.setattr(subprocess, "run", run_mock)

        result = runner.invoke(
            cli, ["session", "wrap", "--agent-id", "foo:bar", "--", "echo", "hi"]
        )

        assert result.exit_code != 0
        assert "invalid agent-id" in result.output
        storage_mock.create_session.assert_not_called()
        run_mock.assert_not_called()

    def test_session_start_accepts_valid_agent_id(self, runner, monkeypatch, storage_mock):
        comp = SimpleNamespace(storage=storage_mock)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["session", "start", "--agent-id", "planner"])

        assert result.exit_code == 0, result.output
        storage_mock.create_session.assert_awaited_once()


# ---------------------------------------------------------------------------
# MCP boundary — mem_agent_register / mem_agent_search (issue #493)
# ---------------------------------------------------------------------------


class TestMcpMultiAgentBoundary:
    """``mem_agent_register`` and ``mem_agent_search`` must reject the same
    hostile shapes as ``mem_session_start`` so the read/write contract
    stays symmetric (issue #493).
    """

    @pytest.mark.asyncio
    async def test_register_colon_in_agent_id_returns_error_string(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_agent_register(agent_id="foo:bar", ctx=ctx)  # type: ignore[arg-type]

        assert out.startswith("Error: invalid agent-id")
        assert "'foo:bar'" in out

    @pytest.mark.asyncio
    async def test_register_malformed_namespace_never_reaches_storage(self, components):
        """Regression pin: ``agent-runtime:foo:bar`` must not appear as a
        namespace meta row even if validation regresses to a warning.
        """
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_agent_register(agent_id="foo:bar", ctx=ctx)  # type: ignore[arg-type]

        rows = await app.storage.list_namespace_meta()
        assert not any("agent-runtime:foo:bar" in (r.get("namespace") or "") for r in rows)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_id", HOSTILE_AGENT_IDS)
    async def test_register_hostile_agent_ids_all_rejected(self, components, agent_id):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_agent_register(agent_id=agent_id, ctx=ctx)  # type: ignore[arg-type]

        assert out.startswith("Error: invalid agent-id")

    @pytest.mark.asyncio
    async def test_register_valid_agent_id_still_succeeds(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_agent_register(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]

        assert "Agent registered: planner" in out
        assert "agent-runtime:planner" in out

    @pytest.mark.asyncio
    async def test_search_colon_in_agent_id_returns_error_string(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_agent_search(query="hello", agent_id="foo:bar", ctx=ctx)  # type: ignore[arg-type]

        assert out.startswith("Error: invalid agent-id")
        assert "'foo:bar'" in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_id", HOSTILE_AGENT_IDS)
    async def test_search_hostile_agent_ids_all_rejected(self, components, agent_id):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_agent_search(query="hello", agent_id=agent_id, ctx=ctx)  # type: ignore[arg-type]

        assert out.startswith("Error: invalid agent-id")

    @pytest.mark.asyncio
    async def test_search_omitted_agent_id_unaffected(self, components):
        """``agent_id=None`` is the documented "use session / legacy NS"
        path — must not trip the validator.
        """
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_agent_search(query="nothing-here", agent_id=None, ctx=ctx)  # type: ignore[arg-type]

        # Either "No results found" or a real result list — must NOT be a
        # validation error. The empty-DB fixture yields no hits.
        assert not out.startswith("Error:")


# ---------------------------------------------------------------------------
# CLI boundary — mm agent register (issue #493)
# ---------------------------------------------------------------------------


class TestCliAgentRegisterBoundary:
    @pytest.fixture
    def agent_storage_mock(self):
        """Mock storage that fails loudly if any namespace-creating call
        lands on the rejection path.
        """
        return SimpleNamespace(
            set_namespace_meta=AsyncMock(),
            get_namespace_meta=AsyncMock(return_value=None),
        )

    def test_register_rejects_colon(self, runner, monkeypatch, agent_storage_mock):
        comp = SimpleNamespace(storage=agent_storage_mock)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["agent", "register", "foo:bar"])

        assert result.exit_code != 0
        assert "invalid agent-id" in result.output
        assert "'foo:bar'" in result.output
        agent_storage_mock.set_namespace_meta.assert_not_called()

    def test_register_rejects_path_traversal(self, runner, monkeypatch, agent_storage_mock):
        comp = SimpleNamespace(storage=agent_storage_mock)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["agent", "register", "../etc"])

        assert result.exit_code != 0
        assert "invalid agent-id" in result.output
        agent_storage_mock.set_namespace_meta.assert_not_called()

    def test_register_accepts_valid_agent_id(self, runner, monkeypatch, agent_storage_mock):
        comp = SimpleNamespace(storage=agent_storage_mock)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["agent", "register", "planner"])

        assert result.exit_code == 0, result.output
        assert "Agent registered: planner" in result.output
        agent_storage_mock.set_namespace_meta.assert_awaited()


# ---------------------------------------------------------------------------
# Cross-surface error parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cli_and_mcp_share_core_error_text(components, monkeypatch):
    """The CLI and MCP surfaces must produce the same identifying text
    so users (and log scrapers) see one error shape regardless of how the
    request entered the system. Both wrap a common ``InvalidNameError``
    message; only the framing prefix (``Error:`` from Click vs.
    ``Error:`` from ``tool_handler``) differs.
    """
    app = AppContext.from_components(components)
    ctx = _StubCtx(app)
    mcp_out = await mem_session_start(agent_id="foo:bar", ctx=ctx)  # type: ignore[arg-type]

    storage = SimpleNamespace(create_session=AsyncMock())
    comp = SimpleNamespace(storage=storage)
    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
    cli_result = CliRunner().invoke(cli, ["session", "start", "--agent-id", "foo:bar"])

    core = "invalid agent-id 'foo:bar'"
    assert core in mcp_out
    assert core in cli_result.output


@pytest.mark.asyncio
async def test_session_and_multi_agent_surfaces_share_core_error_text(components):
    """Issue #493 parity pin: ``mem_session_start``, ``mem_agent_register``,
    and ``mem_agent_search`` all emit the same identifying error fragment
    for the same hostile id. Without this, a caller that types ``foo:bar``
    at the registration tool would have seen a sanitised success while
    the same string at session-start would loudly reject — the asymmetry
    that issue #493 was filed to close.
    """
    app = AppContext.from_components(components)
    ctx = _StubCtx(app)

    session_out = await mem_session_start(agent_id="foo:bar", ctx=ctx)  # type: ignore[arg-type]
    register_out = await mem_agent_register(agent_id="foo:bar", ctx=ctx)  # type: ignore[arg-type]
    search_out = await mem_agent_search(query="x", agent_id="foo:bar", ctx=ctx)  # type: ignore[arg-type]

    core = "invalid agent-id 'foo:bar'"
    assert core in session_out
    assert core in register_out
    assert core in search_out
