"""Tests for ``validate_namespace`` enforcement on the ``mem_ns_*`` CRUD
tools.

Closes the kin gap issue #500 flagged on PR #499 review (Concern 1):
even after #496 / #499 gated every caller-supplied ``namespace=`` /
``target=`` override on session-start and ``mem_agent_share``, the
``mem_ns_*`` namespace CRUD tools still wrote user-supplied namespace
strings into either app state or the storage row with no validation.

The transitive bypass that drives this file:

    mem_ns_set(namespace="agent-runtime:foo:bar")     # accepted today
    mem_session_start(agent_id="default")             # falls through to
                                                      # current_namespace
    -> session row lands with the same shape PR #499 closes elsewhere

The contract this file pins:

* ``mem_ns_set`` rejects hostile-shaped ``namespace=`` BEFORE writing
  ``app.current_namespace`` — so the transitive bypass into
  ``mem_session_start``'s priority-chain step 3 (``app.current_namespace``
  fallback) cannot land a malformed session row.
* ``mem_ns_rename`` / ``mem_ns_assign`` / ``mem_ns_update`` /
  ``mem_ns_delete`` reject hostile-shaped namespace arguments BEFORE the
  storage write / lookup.
* Rejection text matches the rest of the namespace gate (``Error:
  invalid namespace ...``) so log scrapers see one shape across surfaces.
"""

from __future__ import annotations

import pytest

from memtomem.constants import INVALID_NAMESPACE_MESSAGE_PREFIX, InvalidNameError
from memtomem.server.context import AppContext
from memtomem.server.tools.namespace import (
    mem_ns_assign,
    mem_ns_delete,
    mem_ns_rename,
    mem_ns_set,
    mem_ns_update,
)
from memtomem.server.tools.session import mem_session_start

from test_validate_namespace import HOSTILE_NAMESPACES, _StubCtx

_ERROR_PREFIX = f"Error: {INVALID_NAMESPACE_MESSAGE_PREFIX}"


# ---------------------------------------------------------------------------
# AppContext.current_namespace property — defense-in-depth setter
# ---------------------------------------------------------------------------


class TestAppContextCurrentNamespaceSetter:
    """The setter on ``AppContext.current_namespace`` re-validates every
    write to app state, even ones that bypass ``mem_ns_set``. This
    catches the regression class where a future tool (or test code, or
    a Python-level adapter) mutates ``app.current_namespace`` directly
    without remembering to import ``validate_namespace`` — the same
    "forgot to gate" pattern the multi-agent series (#491 → #500) hit
    one PR at a time.

    These tests are at the Python attribute level, not the MCP boundary
    — the ``mem_ns_set`` tests above already pin the boundary contract.
    """

    def test_assigning_hostile_value_raises(self, components):
        app = AppContext.from_components(components)
        with pytest.raises(InvalidNameError, match=INVALID_NAMESPACE_MESSAGE_PREFIX):
            app.current_namespace = "agent-runtime:foo:bar"

    @pytest.mark.parametrize("hostile", HOSTILE_NAMESPACES)
    def test_setter_rejects_every_hostile_shape(self, components, hostile):
        app = AppContext.from_components(components)
        with pytest.raises(InvalidNameError, match=INVALID_NAMESPACE_MESSAGE_PREFIX):
            app.current_namespace = hostile

    def test_rejected_assignment_leaves_attribute_unchanged(self, components):
        app = AppContext.from_components(components)
        app.current_namespace = "legacy-ns"  # known-good
        assert app.current_namespace == "legacy-ns"

        with pytest.raises(InvalidNameError):
            app.current_namespace = "agent-runtime:foo:bar"

        # Backing store is untouched on the rejected path — the bad value
        # never reaches ``_current_namespace``.
        assert app.current_namespace == "legacy-ns"

    def test_assigning_none_passes_through(self, components):
        """``mem_session_end`` writes ``current_session_id = None`` (a
        sibling field), and a future caller may want to clear the
        namespace fallback the same way. ``None`` is the documented
        cleared state and must not trip the validator.
        """
        app = AppContext.from_components(components)
        app.current_namespace = "shared"
        app.current_namespace = None
        assert app.current_namespace is None

    def test_assigning_accepted_shape_succeeds(self, components):
        app = AppContext.from_components(components)
        app.current_namespace = "shared"
        assert app.current_namespace == "shared"
        app.current_namespace = "claude-memory:project-x"
        assert app.current_namespace == "claude-memory:project-x"


# ---------------------------------------------------------------------------
# mem_ns_set — the transitive-bypass closer
# ---------------------------------------------------------------------------


class TestMemNsSetValidatesNamespace:
    """``mem_ns_set`` writes ``app.current_namespace`` directly. Without
    a gate, a hostile string lands in app state and is later picked up
    by ``mem_session_start(agent_id="default")`` via the
    ``current_namespace`` fallback (priority chain step 3) — re-opening
    the session-row bypass that PR #499 closed at the explicit
    ``namespace=`` surface.
    """

    @pytest.mark.asyncio
    async def test_agent_runtime_foo_bar_returns_error_string(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_ns_set(
            namespace="agent-runtime:foo:bar",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith(_ERROR_PREFIX)
        assert "'agent-runtime:foo:bar'" in out

    @pytest.mark.asyncio
    @pytest.mark.parametrize("namespace", HOSTILE_NAMESPACES)
    async def test_hostile_namespaces_all_rejected(self, components, namespace):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_ns_set(
            namespace=namespace,
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith(_ERROR_PREFIX)

    @pytest.mark.asyncio
    async def test_rejection_does_not_mutate_current_namespace(self, components):
        """Belt-and-suspenders: even on a rejected call, ``mem_ns_set``
        must not have touched ``app.current_namespace``. The validator is
        called before the lock + write, so the value cannot land in app
        state through any code path on the rejection branch.
        """
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)
        before = app.current_namespace

        out = await mem_ns_set(
            namespace="agent-runtime:foo:bar",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith(_ERROR_PREFIX)
        assert app.current_namespace == before

    @pytest.mark.asyncio
    async def test_explicit_shared_namespace_still_succeeds(self, components):
        """Sanity: the gate must not regress the documented happy path.
        ``mem_ns_set(namespace="shared")`` is one of the in-tree shapes
        ``ACCEPTED_NAMESPACES`` pins on the unit validator.
        """
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_ns_set(
            namespace="shared",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert "Session namespace set to 'shared'" in out
        assert app.current_namespace == "shared"


# ---------------------------------------------------------------------------
# mem_ns_set → mem_session_start: the transitive bypass regression
# ---------------------------------------------------------------------------


class TestTransitiveBypassClosed:
    @pytest.mark.asyncio
    async def test_set_then_session_start_cannot_land_malformed_row(self, components):
        """Issue #500 canonical regression:

            mem_ns_set(namespace="agent-runtime:foo:bar")     # rejected
            mem_session_start(agent_id="default")             # falls
                                                              # through
            -> sessions.namespace MUST NOT contain the bypass shape

        This pins the *full* attack chain end-to-end: even if a future
        regression weakens any one of the three checks (the ns-set gate,
        the ``current_namespace`` write guard, or the session-start
        fallback), the storage row outcome stays clean.
        """
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        rejected = await mem_ns_set(
            namespace="agent-runtime:foo:bar",
            ctx=ctx,  # type: ignore[arg-type]
        )
        assert rejected.startswith(_ERROR_PREFIX)

        out = await mem_session_start(
            agent_id="default",
            ctx=ctx,  # type: ignore[arg-type]
        )
        assert "Session started" in out

        rows = await app.storage.list_sessions()
        assert not any("agent-runtime:foo:bar" in (r.get("namespace") or "") for r in rows)


# ---------------------------------------------------------------------------
# mem_ns_delete
# ---------------------------------------------------------------------------


class TestMemNsDeleteValidatesNamespace:
    @pytest.mark.asyncio
    async def test_agent_runtime_foo_bar_returns_error_string(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_ns_delete(
            namespace="agent-runtime:foo:bar",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith(_ERROR_PREFIX)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("namespace", HOSTILE_NAMESPACES)
    async def test_hostile_namespaces_all_rejected(self, components, namespace):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_ns_delete(
            namespace=namespace,
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith(_ERROR_PREFIX)


# ---------------------------------------------------------------------------
# mem_ns_rename — both old and new are user-supplied
# ---------------------------------------------------------------------------


class TestMemNsRenameValidatesNamespace:
    @pytest.mark.asyncio
    async def test_hostile_old_returns_error(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_ns_rename(
            old="agent-runtime:foo:bar",
            new="cleanup",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith(_ERROR_PREFIX)

    @pytest.mark.asyncio
    async def test_hostile_new_returns_error(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_ns_rename(
            old="cleanup",
            new="agent-runtime:foo:bar",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith(_ERROR_PREFIX)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hostile", HOSTILE_NAMESPACES)
    async def test_hostile_either_side_rejected(self, components, hostile):
        """Both arms are user-supplied, both arms must be gated."""
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out_old = await mem_ns_rename(
            old=hostile,
            new="cleanup",
            ctx=ctx,  # type: ignore[arg-type]
        )
        out_new = await mem_ns_rename(
            old="cleanup",
            new=hostile,
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out_old.startswith(_ERROR_PREFIX)
        assert out_new.startswith(_ERROR_PREFIX)


# ---------------------------------------------------------------------------
# mem_ns_assign — namespace + old_namespace are both user-supplied
# ---------------------------------------------------------------------------


class TestMemNsAssignValidatesNamespace:
    @pytest.mark.asyncio
    async def test_hostile_namespace_returns_error(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_ns_assign(
            namespace="agent-runtime:foo:bar",
            source_filter="docs/",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith(_ERROR_PREFIX)

    @pytest.mark.asyncio
    async def test_hostile_old_namespace_returns_error(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_ns_assign(
            namespace="cleanup",
            old_namespace="agent-runtime:foo:bar",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith(_ERROR_PREFIX)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hostile", HOSTILE_NAMESPACES)
    async def test_hostile_either_side_rejected(self, components, hostile):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out_target = await mem_ns_assign(
            namespace=hostile,
            source_filter="docs/",
            ctx=ctx,  # type: ignore[arg-type]
        )
        out_old = await mem_ns_assign(
            namespace="cleanup",
            old_namespace=hostile,
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out_target.startswith(_ERROR_PREFIX)
        assert out_old.startswith(_ERROR_PREFIX)


# ---------------------------------------------------------------------------
# mem_ns_update
# ---------------------------------------------------------------------------


class TestMemNsUpdateValidatesNamespace:
    @pytest.mark.asyncio
    async def test_agent_runtime_foo_bar_returns_error_string(self, components):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_ns_update(
            namespace="agent-runtime:foo:bar",
            description="x",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith(_ERROR_PREFIX)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("namespace", HOSTILE_NAMESPACES)
    async def test_hostile_namespaces_all_rejected(self, components, namespace):
        app = AppContext.from_components(components)
        ctx = _StubCtx(app)

        out = await mem_ns_update(
            namespace=namespace,
            description="x",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith(_ERROR_PREFIX)
