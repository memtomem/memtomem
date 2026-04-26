"""Tests for ``validate_namespace`` and its enforcement at every public
surface that accepts a caller-supplied ``namespace=`` / ``target=``
override on session-start and ``mem_agent_share``.

Closes the bypass issue #496 flagged on PR #495 review (Concern 3): even
after #491 / #494 / #498 gated every ``agent_id`` concatenation, the
explicit ``namespace=`` argument on session-start (and ``target=`` on
``mem_agent_share``) still landed verbatim in storage. A Python / MCP /
CLI caller could write ``"agent-runtime:foo:bar"`` even though
``agent_id`` itself was clean.

The contract this file pins:

* Every entry point (LangGraph ``MemtomemStore.start_agent_session`` /
  ``start_session``, MCP ``mem_session_start`` / ``mem_agent_share``,
  CLI ``mm session start --namespace``) rejects hostile shapes with an
  ``InvalidNameError`` (or its public alias) before storage is touched.
* Storage / search never see ``"agent-runtime:foo:bar"`` from any
  surface — pinned with a "would-not-have-been-called" assertion.
* Rejection is loud, mirroring the ``agent_id`` gate's UX (``Error:
  invalid namespace ...`` on the MCP path, ``ClickException`` on CLI).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.constants import InvalidNameError, validate_namespace
from memtomem.server.context import AppContext
from memtomem.server.tools.session import mem_session_start

# Hostile-shaped values that must be rejected at every namespace boundary.
# Includes the canonical issue-#496 shape (``agent-runtime:foo:bar`` —
# the agent-runtime: prefix bypass) plus the broader path / control / regex
# violations a namespace string must never carry into storage.
HOSTILE_NAMESPACES = [
    "agent-runtime:foo:bar",  # issue #496 canonical bypass shape
    "agent-runtime:foo:bar:baz",  # more depth, same bypass
    "agent-runtime:",  # trailing colon = empty agent segment
    "agent-runtime:foo bar",  # whitespace inside the agent segment
    "agent-runtime:..",  # reserved path token in agent segment
    "agent-runtime:../etc",  # path traversal in agent segment
    "agent-runtime:-leading-dash",  # leading-dash in agent segment
    ":foo",  # leading colon = empty first segment
    "foo:",  # trailing colon = empty trailing segment
    "foo::bar",  # consecutive colons = empty middle segment
    "foo bar",  # whitespace
    "foo/bar",  # slash
    "foo\\bar",  # windows-style separator
    "..",  # reserved path token
    "../etc",  # path traversal
    "-leading-dash",  # leading dash
    "foo,bar",  # comma — search-time list shape, not a storable NS
    "",  # empty
    "   ",  # all whitespace
    "foo\x00bar",  # null byte
    "foo\nbar",  # newline
    "foo​bar",  # zero-width space — escape sequence so it isn't an invisible source artifact
]

# Shapes that must continue to round-trip through the validator so the
# gate doesn't break any existing in-tree namespace.
ACCEPTED_NAMESPACES = [
    "default",
    "shared",
    "archive:summary",
    "archive:auto-consolidate",
    "claude-memory:project-x",
    "gemini-memory:project-y",
    "codex-memory:project-z",
    "agent-runtime:planner",
    "agent-runtime:claude_code",
    "custom:scope",
    "legacy:ns",
    "a.b.c",
    "v2.0",
]


# ---------------------------------------------------------------------------
# validate_namespace (unit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ACCEPTED_NAMESPACES)
def test_validate_namespace_accepts_in_tree_shapes(value: str) -> None:
    assert validate_namespace(value) == value


@pytest.mark.parametrize("value", HOSTILE_NAMESPACES)
def test_validate_namespace_rejects_hostile_inputs(value: str) -> None:
    with pytest.raises(InvalidNameError, match="invalid namespace"):
        validate_namespace(value)


def test_validate_namespace_rejects_non_string() -> None:
    with pytest.raises(InvalidNameError, match="expected str"):
        validate_namespace(123)  # type: ignore[arg-type]


def test_validate_namespace_rejects_overlong() -> None:
    # 257 chars — one past the cap. Not in HOSTILE_NAMESPACES because
    # it's a structural, not character-class, violation.
    with pytest.raises(InvalidNameError, match="exceeds 256"):
        validate_namespace("a" * 257)


def test_agent_runtime_prefix_routes_through_validate_agent_id() -> None:
    """Pin the structural rule: ``agent-runtime:<seg>`` is the only
    prefix where the trailing segment is re-validated through the
    stricter agent_id gate. Without this, the override path would widen
    the contract that the direct ``agent_id=`` path enforces — exactly
    the shape issue #496 was filed to close.

    The outer error fragment must say ``"invalid namespace"`` so log
    scrapers grepping for that string at the namespace boundary catch
    every rejection path, including the agent_id-derived one. The
    underlying agent_id contract violation is preserved on
    ``__cause__`` for anyone debugging the wrapped error.
    """

    # Length cap: ``agent-runtime:`` segments inherit agent_id's 64-char
    # cap (``validate_agent_id``); other namespace shapes get the broader
    # 256-char total budget. Pinned via the agent_id path so a future
    # relaxation of the agent_id cap doesn't silently widen the
    # agent-runtime contract.
    overlong_id = "a" * 65
    with pytest.raises(InvalidNameError) as excinfo:
        validate_namespace(f"agent-runtime:{overlong_id}")

    # Outer message — what user / log scraper sees.
    assert "invalid namespace" in str(excinfo.value)
    assert "agent-runtime" in str(excinfo.value)
    # __cause__ — preserved agent_id detail.
    assert isinstance(excinfo.value.__cause__, InvalidNameError)
    assert "invalid agent-id" in str(excinfo.value.__cause__)


# ---------------------------------------------------------------------------
# MCP boundary — mem_session_start(namespace=...)
# ---------------------------------------------------------------------------


class _StubCtx:
    """Minimal stand-in for MCP ``Context`` so async tools can be invoked
    directly. Mirrors ``test_validate_agent_id``.
    """

    def __init__(self, app: AppContext) -> None:
        class _RC:
            pass

        self.request_context = _RC()
        self.request_context.lifespan_context = app


class TestMcpSessionStartNamespaceOverride:
    @pytest.mark.asyncio
    async def test_agent_runtime_foo_bar_returns_error_string(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_session_start(
            agent_id="planner",
            namespace="agent-runtime:foo:bar",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith("Error: invalid namespace")
        assert "'agent-runtime:foo:bar'" in out

    @pytest.mark.asyncio
    async def test_malformed_namespace_never_reaches_storage(self, components):
        """Regression pin for issue #496: ``store.start_agent_session
        (agent_id="planner", namespace="agent-runtime:foo:bar")`` cannot
        land an ``"agent-runtime:foo:bar"`` row even though ``agent_id``
        itself is clean. Same shape pinned at the MCP boundary.
        """
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        await mem_session_start(
            agent_id="planner",
            namespace="agent-runtime:foo:bar",
            ctx=ctx,  # type: ignore[arg-type]
        )

        rows = await app.storage.list_sessions()
        assert not any("agent-runtime:foo:bar" in (r["namespace"] or "") for r in rows)
        assert app.current_session_id is None
        assert app.current_agent_id is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("namespace", HOSTILE_NAMESPACES)
    async def test_hostile_namespaces_all_rejected(self, components, namespace):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_session_start(
            agent_id="planner",
            namespace=namespace,
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith("Error: invalid namespace")

    @pytest.mark.asyncio
    async def test_explicit_shared_namespace_still_succeeds(self, components):
        """Sanity: the override IS still an escape hatch — a planner-bound
        session can publish into ``shared`` even though
        ``agent-runtime:planner`` is the default derivation.
        """
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_session_start(
            agent_id="planner",
            namespace="shared",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert "Session started" in out
        assert "Namespace: shared" in out


# ---------------------------------------------------------------------------
# CLI boundary — mm session start --namespace
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


# CLI-safe subset of ``HOSTILE_NAMESPACES``: drops the leading-dash
# variants because Click parses ``--namespace -leading-dash`` as a
# missing-option-value error before our validator sees the input. The
# dash-rejection contract is still pinned at the LangGraph / MCP /
# unit-validator boundaries, so dropping it here is defense-in-depth
# without losing coverage. ``CliRunner.invoke`` passes the args list
# straight through (no shell), so control chars / whitespace / null
# bytes survive untouched.
CLI_HOSTILE_NAMESPACES = [v for v in HOSTILE_NAMESPACES if not v.startswith("-")]


class TestCliSessionStartNamespaceOverride:
    @pytest.mark.parametrize("namespace", CLI_HOSTILE_NAMESPACES)
    def test_hostile_namespaces_all_rejected(self, runner, monkeypatch, storage_mock, namespace):
        """Defense-in-depth: every hostile shape pinned at the validator
        must produce a CLI-side ``ClickException`` before storage is
        touched. Without the parametrize, a future regression that lets
        only one shape (say, ``foo,bar``) through the CLI gate while
        the validator still rejects it would slip past — the CLI is the
        thinnest layer over the validator and the easiest place for a
        ``try/except`` swallow to be reintroduced.
        """
        comp = SimpleNamespace(storage=storage_mock)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(
            cli,
            ["session", "start", "--agent-id", "planner", "--namespace", namespace],
        )

        assert result.exit_code != 0
        assert "invalid namespace" in result.output
        # Regression pin: storage never received a malformed namespace.
        storage_mock.create_session.assert_not_called()

    def test_rejects_agent_runtime_foo_bar_with_quoted_value(
        self, runner, monkeypatch, storage_mock
    ):
        """Pin the canonical issue-#496 shape with the exact error
        fragment a log scraper would grep for. Kept as a separate test
        from the parametrize so failures here surface the bypass
        directly rather than under one of N parametrised IDs.
        """
        comp = SimpleNamespace(storage=storage_mock)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(
            cli,
            [
                "session",
                "start",
                "--agent-id",
                "planner",
                "--namespace",
                "agent-runtime:foo:bar",
            ],
        )

        assert result.exit_code != 0
        assert "invalid namespace" in result.output
        assert "'agent-runtime:foo:bar'" in result.output
        storage_mock.create_session.assert_not_called()

    def test_accepts_explicit_shared(self, runner, monkeypatch, storage_mock):
        comp = SimpleNamespace(storage=storage_mock)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(
            cli,
            ["session", "start", "--agent-id", "planner", "--namespace", "shared"],
        )

        assert result.exit_code == 0, result.output
        storage_mock.create_session.assert_awaited_once()


# ---------------------------------------------------------------------------
# LangGraph adapter boundary — MemtomemStore.start_agent_session(namespace=)
# and MemtomemStore.start_session(namespace=)
# ---------------------------------------------------------------------------


def _stub_components():
    comp = MagicMock()
    comp.storage.create_session = AsyncMock(return_value=None)
    return comp


class TestLangGraphStartAgentSessionNamespaceOverride:
    @pytest.mark.asyncio
    async def test_agent_runtime_foo_bar_blocked(self):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = _stub_components()
        store._components = comp

        with pytest.raises(InvalidNameError, match="invalid namespace"):
            await store.start_agent_session("planner", namespace="agent-runtime:foo:bar")

        comp.storage.create_session.assert_not_awaited()
        assert store._current_session_id is None
        assert store._current_agent_id is None

    @pytest.mark.asyncio
    @pytest.mark.parametrize("namespace", HOSTILE_NAMESPACES)
    async def test_hostile_namespaces_all_rejected(self, namespace):
        """Pin the contract per-shape so a future regression in any one
        category (path-traversal, embedded whitespace, comma, etc.)
        surfaces as a single failing parametrise rather than silently
        widening through one unguarded shape.
        """
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = _stub_components()
        store._components = comp

        with pytest.raises(InvalidNameError, match="invalid namespace"):
            await store.start_agent_session("planner", namespace=namespace)

        comp.storage.create_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_existing_custom_scope_still_succeeds(self):
        """The pre-#496 ``custom:scope`` override fixture stays valid —
        the gate must not regress the documented escape-hatch behaviour
        (see ``test_explicit_namespace_overrides_default`` in
        ``test_langgraph.py``).
        """
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = _stub_components()
        store._components = comp

        await store.start_agent_session("planner", namespace="custom:scope")

        args, _ = comp.storage.create_session.call_args
        assert args[2] == "custom:scope"


class TestLangGraphStartSessionNamespaceOverride:
    """``start_session`` is the low-level escape hatch that does NOT
    validate ``agent_id`` (it never concatenates it into a namespace).
    The ``namespace=`` override IS validated though, because the value
    lands verbatim in the session row — without the gate this method
    would re-open the bypass that ``start_agent_session`` now closes.
    """

    @pytest.mark.asyncio
    async def test_agent_runtime_foo_bar_blocked(self):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = _stub_components()
        store._components = comp

        with pytest.raises(InvalidNameError, match="invalid namespace"):
            await store.start_session("default", namespace="agent-runtime:foo:bar")

        comp.storage.create_session.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_omitted_namespace_unaffected(self):
        """``namespace=None`` is the documented default-to-"default" path —
        must not trip the validator.
        """
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        comp = _stub_components()
        store._components = comp

        sid = await store.start_session("default")

        assert sid is not None
        args, _ = comp.storage.create_session.call_args
        assert args[2] == "default"


# ---------------------------------------------------------------------------
# MCP boundary — mem_agent_share(target=...)
# ---------------------------------------------------------------------------


class TestMemAgentShareTargetOverride:
    """``mem_agent_share(target=...)`` is the kin gap flagged on PR #494
    review: an MCP caller could ask to "share" a chunk into
    ``target="agent-runtime:foo:bar"`` and the new copy would land in a
    malformed namespace even though the equivalent ``mem_session_start``
    / ``mem_agent_register`` paths refuse the same shape.
    """

    @pytest.mark.asyncio
    async def test_agent_runtime_foo_bar_returns_error_before_copy(self, components):
        from memtomem.server.tools.multi_agent import mem_agent_share

        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        # ``chunk_id`` is irrelevant — the namespace gate must run before
        # the chunk lookup happens, so we don't even need a real chunk to
        # pin the contract.
        out = await mem_agent_share(
            chunk_id="00000000-0000-0000-0000-000000000000",
            target="agent-runtime:foo:bar",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith("Error: invalid namespace")
        assert "'agent-runtime:foo:bar'" in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize("target", HOSTILE_NAMESPACES)
    async def test_hostile_targets_all_rejected(self, components, target):
        from memtomem.server.tools.multi_agent import mem_agent_share

        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_agent_share(
            chunk_id="00000000-0000-0000-0000-000000000000",
            target=target,
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith("Error: invalid namespace")


# ---------------------------------------------------------------------------
# Cross-surface error parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_and_cli_share_core_namespace_error_text(components, monkeypatch):
    """The CLI and MCP surfaces must produce the same identifying text
    for a malformed namespace so users (and log scrapers) see one error
    shape regardless of how the request entered the system. Mirrors
    ``test_cli_and_mcp_share_core_error_text`` for the agent_id gate.
    """
    app = AppContext.from_components(components)
    ctx = _StubCtx(app)
    mcp_out = await mem_session_start(
        agent_id="planner",
        namespace="agent-runtime:foo:bar",
        ctx=ctx,  # type: ignore[arg-type]
    )

    storage = SimpleNamespace(create_session=AsyncMock())
    comp = SimpleNamespace(storage=storage)
    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
    cli_result = CliRunner().invoke(
        cli,
        [
            "session",
            "start",
            "--agent-id",
            "planner",
            "--namespace",
            "agent-runtime:foo:bar",
        ],
    )

    core = "invalid namespace 'agent-runtime:foo:bar'"
    assert core in mcp_out
    assert core in cli_result.output
