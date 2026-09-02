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


def _fake_app(current_namespace: str | None = None) -> MagicMock:
    app = MagicMock()
    app.current_namespace = current_namespace
    app.webhook_manager = None
    # No dimension mismatch: the surfaces ask the one-shot announcer for a
    # notice, and its ``getattr`` chain would otherwise see a MagicMock here
    # and try to take a MagicMock lock.
    app.storage.embedding_mismatch = None
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

        opener = AsyncMock(side_effect=AssertionError("too late"))
        monkeypatch.setattr(ask_mod, "_get_app_initialized", opener)

        out = await ask_mod.mem_ask(question="hello", as_of="not-a-date", ctx=SimpleNamespace())

        # Naming the bound is what separates this from the handler's generic
        # rendering of any later exception: without the pre-app parse the
        # mocked opener raises and ``@tool_handler`` also answers "Error: ...",
        # so a prefix-only assertion passes with the fix reverted.
        assert "as_of" in out and "not-a-date" in out
        opener.assert_not_awaited()


class TestScopeReachesTheActingLayer:
    @pytest.mark.parametrize("scope", ["user,project_local", "project_*"])
    async def test_ask_hands_scope_and_as_of_to_the_search_core(self, monkeypatch, scope):
        from memtomem.server.tools import ask as ask_mod

        core = AsyncMock(return_value=([], RetrievalStats(), []))
        monkeypatch.setattr(ask_mod, "run_search", core)
        monkeypatch.setattr(
            ask_mod, "_get_app_initialized", AsyncMock(return_value=_fake_app("ambient-ns"))
        )
        monkeypatch.setattr(ask_mod, "_resolve_project_context_root", lambda _app: Path("/proj"))

        await ask_mod.mem_ask(
            question="hello", scope=scope, as_of="2026-01-01", ctx=SimpleNamespace()
        )

        assert core.await_count == 1
        assert core.await_args.kwargs["scope"] == scope
        assert core.await_args.kwargs["as_of"] == "2026-01-01"
        assert core.await_args.kwargs["project_context_root"] == Path("/proj")
        # The ambient namespace is the core's half of the
        # ``namespace or current_namespace`` fallback this tool handed over.
        assert core.await_args.kwargs["namespace"] is None
        assert core.await_args.kwargs["current_namespace"] == "ambient-ns"

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


_HINT = "sentinel: semantic search degraded"
_ANNOUNCED = "sentinel: embedding dimension changed"


def _ask_result():
    chunk = SimpleNamespace(
        content="Berlin is the capital.",
        metadata=SimpleNamespace(
            source_file=Path("/notes/geo.md"),
            heading_hierarchy=("Geography",),
            tags=("geo",),
        ),
    )
    return SimpleNamespace(chunk=chunk, rank=1, score=0.9)


async def _run_ask(monkeypatch, results, *, stats=None, announcer=None):
    """``mem_ask`` over a stubbed core that always returns ``_HINT``."""
    from memtomem.server.tools import ask as ask_mod

    monkeypatch.setattr(
        ask_mod,
        "run_search",
        AsyncMock(return_value=(results, stats or RetrievalStats(), [_HINT])),
    )
    monkeypatch.setattr(ask_mod, "_get_app_initialized", AsyncMock(return_value=_fake_app()))
    monkeypatch.setattr(ask_mod, "_resolve_project_context_root", lambda _app: None)
    if announcer is not None:
        monkeypatch.setattr(ask_mod, "_announce_dim_mismatch_once", announcer)

    return await ask_mod.mem_ask(question="what is the capital?", ctx=SimpleNamespace())


class TestAskRendersItsSearchHints:
    """A grounded prompt built from a degraded pool has to say so.

    ``run_search`` derives the notices (semantic leg suppressed, archived
    rows hidden); ``mem_ask`` used to drop them, so an answer synthesized
    from a keyword-only pool read exactly like one from a healthy hybrid
    pool. Both return paths render them.
    """

    async def test_hints_reach_the_caller_on_the_empty_path(self, monkeypatch):
        out = await _run_ask(monkeypatch, [])

        assert "No relevant memories found" in out
        assert f"({_HINT})" in out

    async def test_hints_reach_the_caller_on_the_answered_path(self, monkeypatch):
        out = await _run_ask(monkeypatch, [_ask_result()])

        assert "## Question: what is the capital?" in out
        assert f"({_HINT})" in out


class TestAskAnnouncesTheDimensionMismatchOnce:
    """The per-process announcement, on the same terms as ``mem_search``.

    The notice is one-shot session state, so a surface that consumes it
    and then returns without rendering does not merely skip a line — it
    destroys the only announcement the process was going to make. And
    when the query already carries ``run_search``'s degradation hint, the
    announcer must not be consulted at all: both notices name the same
    dimensions and the same fix.
    """

    async def test_the_announced_notice_is_rendered_once_on_the_empty_path(self, monkeypatch):
        announcer = AsyncMock(return_value=_ANNOUNCED)

        out = await _run_ask(monkeypatch, [], announcer=announcer)

        assert announcer.await_count == 1
        assert out.count(f"({_ANNOUNCED})") == 1

    async def test_the_announced_notice_is_rendered_once_on_the_answered_path(self, monkeypatch):
        announcer = AsyncMock(return_value=_ANNOUNCED)

        out = await _run_ask(monkeypatch, [_ask_result()], announcer=announcer)

        assert announcer.await_count == 1
        assert out.count(f"({_ANNOUNCED})") == 1

    async def test_a_per_search_degradation_hint_leaves_the_one_shot_unconsumed(self, monkeypatch):
        """``dense_suppressed_mismatch`` already said it — don't say it twice.

        The flag stays unconsumed so the write surfaces still get their
        one-shot; the search-derived hint is rendered exactly once.
        """
        announcer = AsyncMock(return_value=_ANNOUNCED)

        out = await _run_ask(
            monkeypatch,
            [_ask_result()],
            stats=RetrievalStats(dense_suppressed_mismatch=True),
            announcer=announcer,
        )

        announcer.assert_not_awaited()
        assert _ANNOUNCED not in out
        assert out.count(f"({_HINT})") == 1
