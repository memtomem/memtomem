"""``scope`` on the secondary read surfaces (#2194).

``mem_search`` and ``mem_recall`` have taken an ADR-0011 ``scope`` since
PR-C; ``mem_ask``, ``mem_entity_search`` and ``mem_timeline`` did not, so
the only way to reach a project tier from them was to be standing in the
right directory. These pin the three things that makes true:

1. A comma/glob mix is refused **before** the app opens, so a filter that
   cannot run is reported as a filter error rather than as an empty
   result set — the same contract ``test_mem_search_wrapper`` pins for
   ``mem_search``.
2. The value reaches the layer that acts on it: the pipeline for
   ``mem_ask`` / ``mem_timeline``, a parsed ``ScopeFilter`` plus the
   project anchor for ``mem_entity_search`` (its storage query is where
   the boundary is applied).
3. ``mem_ask``'s ``as_of`` is validated on the same pre-app path.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem.search.pipeline import RetrievalStats

pytestmark = pytest.mark.asyncio


def _fake_app() -> MagicMock:
    app = MagicMock()
    app.current_namespace = None
    app.webhook_manager = None
    return app


def _modules():
    from memtomem.server.tools import ask as ask_mod
    from memtomem.server.tools import entity as entity_mod
    from memtomem.server.tools import temporal as temporal_mod

    return ask_mod, entity_mod, temporal_mod


def _call_kwargs(mod, scope: str):
    """The minimum arguments each tool needs, plus the scope under test."""
    ask_mod, entity_mod, temporal_mod = _modules()
    if mod is ask_mod:
        return {"question": "hello", "scope": scope}
    if mod is temporal_mod:
        return {"topic": "hello", "scope": scope}
    return {"value": "hello", "scope": scope}


def _tool(mod):
    ask_mod, entity_mod, temporal_mod = _modules()
    if mod is ask_mod:
        return mod.mem_ask
    if mod is temporal_mod:
        return mod.mem_timeline
    return mod.mem_entity_search


@pytest.fixture(params=["ask", "entity", "temporal"])
def tool_module(request):
    ask_mod, entity_mod, temporal_mod = _modules()
    return {"ask": ask_mod, "entity": entity_mod, "temporal": temporal_mod}[request.param]


class TestScopeValidation:
    async def test_a_comma_glob_mix_is_refused_before_the_app_opens(self, monkeypatch, tool_module):
        monkeypatch.setattr(
            tool_module,
            "_get_app_initialized",
            AsyncMock(side_effect=AssertionError("too late")),
        )

        out = await _tool(tool_module)(
            ctx=SimpleNamespace(), **_call_kwargs(tool_module, "project_*,user")
        )

        assert out.startswith("Error: ")
        assert "scope 'project_*,user'" in out

    async def test_a_bad_as_of_is_refused_before_the_app_opens(self, monkeypatch):
        from memtomem.server.tools import ask as ask_mod

        monkeypatch.setattr(
            ask_mod, "_get_app_initialized", AsyncMock(side_effect=AssertionError("too late"))
        )

        out = await ask_mod.mem_ask(question="hello", as_of="not-a-date", ctx=SimpleNamespace())

        assert out.startswith("Error: ")


class TestScopeReachesTheActingLayer:
    @pytest.mark.parametrize("scope", ["user,project_local", "project_*"])
    async def test_ask_hands_scope_and_as_of_to_the_search_core(self, monkeypatch, scope):
        from memtomem.server.tools import ask as ask_mod

        core = AsyncMock(return_value=([], RetrievalStats(), []))
        monkeypatch.setattr(ask_mod, "run_search", core)
        monkeypatch.setattr(ask_mod, "_get_app_initialized", AsyncMock(return_value=_fake_app()))
        monkeypatch.setattr(ask_mod, "_resolve_project_context_root", lambda _app: Path("/proj"))

        await ask_mod.mem_ask(
            question="hello", scope=scope, as_of="2026-01-01", ctx=SimpleNamespace()
        )

        assert core.await_count == 1
        assert core.await_args.kwargs["scope"] == scope
        assert core.await_args.kwargs["as_of"] == "2026-01-01"
        assert core.await_args.kwargs["project_context_root"] == Path("/proj")

    @pytest.mark.parametrize("scope", ["user,project_local", "project_*"])
    async def test_timeline_hands_scope_to_the_pipeline(self, monkeypatch, scope):
        from memtomem.server.tools import temporal as temporal_mod

        app = _fake_app()
        app.search_pipeline.search = AsyncMock(return_value=([], RetrievalStats()))
        monkeypatch.setattr(temporal_mod, "_get_app_initialized", AsyncMock(return_value=app))
        monkeypatch.setattr(
            temporal_mod, "_resolve_project_context_root", lambda _app: Path("/proj")
        )

        await temporal_mod.mem_timeline(topic="hello", scope=scope, ctx=SimpleNamespace())

        kwargs = app.search_pipeline.search.await_args.kwargs
        assert kwargs["scope"] == scope
        assert kwargs["project_context_root"] == Path("/proj")

    @pytest.mark.parametrize(
        ("scope", "expected_scopes", "expected_pattern"),
        [
            ("user,project_local", ("user", "project_local"), None),
            ("project_*", (), "project_*"),
        ],
    )
    async def test_entity_search_hands_a_parsed_filter_and_anchor_to_storage(
        self, monkeypatch, scope, expected_scopes, expected_pattern
    ):
        from memtomem.server.tools import entity as entity_mod

        app = _fake_app()
        app.storage.search_entities = AsyncMock(return_value=[])
        monkeypatch.setattr(entity_mod, "_get_app_initialized", AsyncMock(return_value=app))
        monkeypatch.setattr(entity_mod, "_resolve_project_context_root", lambda _app: Path("/proj"))

        await entity_mod.mem_entity_search(value="hello", scope=scope, ctx=SimpleNamespace())

        kwargs = app.storage.search_entities.await_args.kwargs
        assert kwargs["scope_filter"].scopes == expected_scopes
        assert kwargs["scope_filter"].pattern == expected_pattern
        assert kwargs["project_context_root"] == Path("/proj")

    async def test_entity_search_without_scope_still_threads_the_project_anchor(self, monkeypatch):
        """The default merge needs the anchor; without it an in-project
        entity search would silently drop its own project's rows."""
        from memtomem.server.tools import entity as entity_mod

        app = _fake_app()
        app.storage.search_entities = AsyncMock(return_value=[])
        monkeypatch.setattr(entity_mod, "_get_app_initialized", AsyncMock(return_value=app))
        monkeypatch.setattr(entity_mod, "_resolve_project_context_root", lambda _app: Path("/proj"))

        await entity_mod.mem_entity_search(value="hello", ctx=SimpleNamespace())

        kwargs = app.storage.search_entities.await_args.kwargs
        assert kwargs["scope_filter"] is None
        assert kwargs["project_context_root"] == Path("/proj")
