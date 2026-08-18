"""``mem_agent_search`` delegates retrieval to the shared search service.

The tool used to call the pipeline itself and keep its own copy of the
trust-UX hint block. It now goes through
``services.search_service.run_search``, so what needs pinning is the
handoff: the namespace filter it builds, the arguments it forwards, and
the hints it lets through.

Namespace *semantics* (which agent resolves to which bucket, what shared
merging surfaces) live in ``test_multi_agent_integration.py`` against real
components; this file stubs the service to pin the wiring.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem.search.pipeline import RetrievalStats

pytestmark = pytest.mark.asyncio


def _fake_app(*, agent_id=None, current_namespace=None) -> MagicMock:
    app = MagicMock()
    app.current_agent_id = agent_id
    app.current_namespace = current_namespace
    return app


async def _call(
    monkeypatch,
    *,
    app=None,
    results=None,
    stats=None,
    hints=None,
    dim_notice=None,
    formatted="FORMATTED",
    **kwargs,
):
    """Invoke ``mem_agent_search`` with the search service stubbed."""
    from memtomem.server.tools import multi_agent as agent_mod

    monkeypatch.setattr(
        agent_mod, "_get_app_initialized", AsyncMock(return_value=app or _fake_app())
    )
    monkeypatch.setattr(
        agent_mod, "_announce_dim_mismatch_once", AsyncMock(return_value=dim_notice)
    )
    # The tool imports the text formatter at call time, so the patch has to
    # land on the defining module rather than on ``multi_agent``.
    monkeypatch.setattr(
        "memtomem.server.formatters._format_results", MagicMock(return_value=formatted)
    )
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda _app: Path("/tmp/project"),
    )
    monkeypatch.setattr(
        agent_mod,
        "run_search",
        AsyncMock(return_value=(results or [], stats or RetrievalStats(), list(hints or []))),
    )
    out = await agent_mod.mem_agent_search(query="hello", ctx=SimpleNamespace(), **kwargs)
    return out, agent_mod.run_search


class TestDelegation:
    async def test_every_argument_reaches_the_service_exactly_once(self, monkeypatch):
        # The ambient namespace is deliberately non-None: the agent bucket is
        # already resolved here, so passing it on as ``current_namespace``
        # would be a fallback this tool must never arm.
        app = _fake_app(current_namespace="legacy-ns")

        _, core = await _call(monkeypatch, app=app, agent_id="alpha", top_k=5)

        core.assert_awaited_once_with(
            app.search_pipeline,
            query="hello",
            top_k=5,
            namespace="agent-runtime:alpha,shared",
            current_namespace=None,
            project_context_root=Path("/tmp/project"),
            origin="internal",
        )

    async def test_include_shared_false_narrows_to_the_private_bucket(self, monkeypatch):
        _, core = await _call(monkeypatch, agent_id="alpha", include_shared=False)

        assert core.await_args.kwargs["namespace"] == "agent-runtime:alpha"

    async def test_a_project_shared_bucket_repoints_only_the_shared_leg(self, monkeypatch):
        _, core = await _call(monkeypatch, agent_id="alpha", shared_namespace="shared:acme")

        assert core.await_args.kwargs["namespace"] == "agent-runtime:alpha,shared:acme"

    async def test_an_unresolved_agent_searches_unpinned(self, monkeypatch):
        """No agent and no session binding means "search everything".

        ``current_namespace`` must stay None through the handoff — letting
        the service's ambient fallback engage would silently pin the search
        to the legacy namespace and disarm the archive hint.
        """
        _, core = await _call(monkeypatch, app=_fake_app(current_namespace=None))

        assert core.await_args.kwargs["namespace"] is None
        assert core.await_args.kwargs["current_namespace"] is None


class TestHints:
    async def test_service_hints_reach_the_structured_payload(self, monkeypatch):
        out, _ = await _call(
            monkeypatch,
            hints=[
                "3 result(s) hidden in system namespaces "
                '(pass namespace="archive:..." to include them).'
            ],
            output_format="structured",
        )

        assert json.loads(out)["hints"] == [
            '3 result(s) hidden in system namespaces (pass namespace="archive:..." '
            "to include them)."
        ]

    async def test_no_hints_leaves_the_key_out(self, monkeypatch):
        out, _ = await _call(monkeypatch, output_format="structured")

        assert "hints" not in json.loads(out)

    async def test_the_empty_compact_message_names_the_agent(self, monkeypatch):
        out, _ = await _call(monkeypatch, agent_id="alpha")

        assert out == "No results found for agent 'alpha'."


class TestHintsReachTextFormats:
    """#2085: compact/verbose used to drop every hint on the floor."""

    async def test_the_empty_compact_message_carries_the_hints(self, monkeypatch):
        out, _ = await _call(monkeypatch, agent_id="alpha", hints=["hint one", "hint two"])

        assert out == "No results found for agent 'alpha'.\n\n(hint one)\n(hint two)"

    async def test_compact_results_carry_the_hints(self, monkeypatch):
        out, _ = await _call(monkeypatch, results=["result"], hints=["hint one"])

        assert out == "FORMATTED\n\n(hint one)"

    async def test_verbose_results_carry_the_hints(self, monkeypatch):
        out, _ = await _call(
            monkeypatch, results=["result"], hints=["hint one"], output_format="verbose"
        )

        assert out == "FORMATTED\n\n(hint one)"

    async def test_the_one_shot_dimension_notice_reaches_the_default_format(self, monkeypatch):
        """The notice is consumed per process, so dropping it here loses it.

        ``_announce_dim_mismatch_once`` hands out its warning exactly once;
        a caller in the default format used to consume it and print nothing,
        which also robbed every later ``mem_search`` of the same warning.
        """
        out, _ = await _call(monkeypatch, results=["result"], dim_notice="embedding dim changed")

        assert out == "FORMATTED\n\n(embedding dim changed)"

    async def test_the_dimension_notice_reaches_the_empty_message(self, monkeypatch):
        out, _ = await _call(monkeypatch, dim_notice="embedding dim changed")

        assert out == "No results found for agent 'current'.\n\n(embedding dim changed)"

    async def test_no_hints_leaves_the_text_output_untouched(self, monkeypatch):
        out, _ = await _call(monkeypatch, results=["result"])

        assert out == "FORMATTED"
