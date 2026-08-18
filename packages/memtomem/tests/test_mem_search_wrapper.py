"""Boundary tests for what ``mem_search`` still owns after the core split.

``services.search_service.run_search`` holds retrieval and the
result-derived hints; the tool wrapper keeps the empty-result prose, the
formatter dispatch, and the webhook. Those are pinned here with the core
stubbed, so a change to either side fails on its own terms.

``test_trust_ux.py`` covers the same surface through real components;
these are the exact-string counterparts.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem.search.pipeline import RetrievalStats

pytestmark = pytest.mark.asyncio


def _fake_app(*, webhooks: bool = False) -> MagicMock:
    app = MagicMock()
    app.current_namespace = None
    if webhooks:
        app.webhook_manager.fire = AsyncMock(return_value=None)
    else:
        app.webhook_manager = None
    return app


async def _call(
    monkeypatch,
    *,
    app,
    results,
    stats,
    hints=None,
    dim_notice=None,
    project_root=None,
    **kwargs,
):
    """Invoke ``mem_search`` with the core and app helpers stubbed.

    The stubbed core stays reachable as ``search_mod.run_search`` for the
    duration of the test, so callers can assert what the wrapper handed it.
    """
    from memtomem.server.tools import search as search_mod

    monkeypatch.setattr(search_mod, "_get_app_initialized", AsyncMock(return_value=app))
    monkeypatch.setattr(
        search_mod, "_announce_dim_mismatch_once", AsyncMock(return_value=dim_notice)
    )
    monkeypatch.setattr(search_mod, "_resolve_project_context_root", lambda _app: project_root)
    monkeypatch.setattr(
        search_mod,
        "run_search",
        AsyncMock(return_value=(results, stats, list(hints or []))),
    )
    return await search_mod.mem_search(query="hello", ctx=SimpleNamespace(), **kwargs)


class TestCoreDelegation:
    async def test_every_argument_reaches_the_core_exactly_once(self, monkeypatch):
        """Whole-call pin for the wrapper→service hop this refactor created.

        The service's own whole-call test starts one step downstream, so
        without this a dropped or rewritten argument here would reach no
        assertion at all.
        """
        from memtomem.server.tools import search as search_mod

        monkeypatch.setattr(search_mod, "_format_results", MagicMock(return_value="FORMATTED"))
        app = _fake_app()
        app.current_namespace = "ambient"
        project_root = Path("/tmp/project")

        await _call(
            monkeypatch,
            app=app,
            results=["result"],
            stats=RetrievalStats(final_total=1),
            project_root=project_root,
            top_k=7,
            source_filter="notes.md",
            tag_filter="redis",
            namespace="work",
            as_of="2026-01-01",
            bm25_weight=2.0,
            dense_weight=3.0,
            context_window=2,
            scope="user",
            rerank=False,
        )

        search_mod.run_search.assert_awaited_once_with(
            app.search_pipeline,
            query="hello",
            top_k=7,
            source_filter="notes.md",
            tag_filter="redis",
            namespace="work",
            current_namespace="ambient",
            as_of="2026-01-01",
            bm25_weight=2.0,
            dense_weight=3.0,
            context_window=2,
            scope="user",
            rerank=False,
            project_context_root=project_root,
            origin="mcp",
        )

    async def test_defaults_reach_the_core_unmodified(self, monkeypatch):
        from memtomem.server.tools import search as search_mod

        app = _fake_app()

        await _call(monkeypatch, app=app, results=[], stats=RetrievalStats())

        kwargs = search_mod.run_search.await_args.kwargs
        assert (kwargs["top_k"], kwargs["context_window"], kwargs["origin"]) == (10, 0, "mcp")
        assert kwargs["rerank"] is None
        assert kwargs["namespace"] is None

    async def test_the_verbose_alias_does_not_leak_into_the_core(self, monkeypatch):
        """``verbose`` selects a text format; it is not a retrieval input."""
        from memtomem.server.tools import search as search_mod

        monkeypatch.setattr(search_mod, "_format_results", MagicMock(return_value="FORMATTED"))

        await _call(
            monkeypatch,
            app=_fake_app(),
            results=["result"],
            stats=RetrievalStats(final_total=1),
            verbose=True,
        )

        awaited = search_mod.run_search.await_args
        assert "verbose" not in awaited.kwargs
        assert "output_format" not in awaited.kwargs


class TestEmptyResults:
    async def test_filters_excluded_everything(self, monkeypatch):
        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=[],
            stats=RetrievalStats(fused_total=5),
            tag_filter="redis",
        )

        assert out == (
            "No results match your filters (5 results found before filtering). "
            "Try broader filters or remove source_filter/tag_filter."
        )

    async def test_both_retrievers_failed(self, monkeypatch):
        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=[],
            stats=RetrievalStats(bm25_error="fts down", dense_error="no embedder"),
        )

        assert out == (
            "Search unavailable: both keyword and semantic search failed.\n"
            "- BM25: fts down\n"
            "- Dense: no embedder"
        )

    async def test_keyword_retriever_failed(self, monkeypatch):
        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=[],
            stats=RetrievalStats(bm25_error="fts down"),
        )

        assert out == "No results found. (Note: keyword search unavailable: fts down)"

    async def test_semantic_retriever_failed(self, monkeypatch):
        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=[],
            stats=RetrievalStats(dense_error="no embedder"),
        )

        assert out == "No results found. (Note: semantic search unavailable: no embedder)"

    async def test_plain_empty_result(self, monkeypatch):
        out = await _call(monkeypatch, app=_fake_app(), results=[], stats=RetrievalStats())

        assert out == "No results found."

    async def test_core_hints_are_appended_to_the_empty_message(self, monkeypatch):
        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=[],
            stats=RetrievalStats(),
            hints=["hint one", "hint two"],
        )

        assert out == "No results found.\n\n(hint one)\n(hint two)"

    async def test_the_filter_message_still_carries_the_hints(self, monkeypatch):
        """#2085 (sibling): the special empty branches used to return early.

        Returning before the hint tail throws away the one-shot dimension
        notice this call already consumed, so the warning is not deferred —
        it is gone for the life of the process.
        """
        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=[],
            stats=RetrievalStats(fused_total=5),
            hints=["hint one"],
            dim_notice="embedding dim changed",
            tag_filter="redis",
        )

        assert out == (
            "No results match your filters (5 results found before filtering). "
            "Try broader filters or remove source_filter/tag_filter."
            "\n\n(hint one)\n(embedding dim changed)"
        )

    async def test_the_both_failed_message_still_carries_the_hints(self, monkeypatch):
        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=[],
            stats=RetrievalStats(bm25_error="fts down", dense_error="no embedder"),
            hints=["hint one"],
        )

        assert out == (
            "Search unavailable: both keyword and semantic search failed.\n"
            "- BM25: fts down\n"
            "- Dense: no embedder"
            "\n\n(hint one)"
        )

    async def test_the_keyword_failure_message_still_carries_the_hints(self, monkeypatch):
        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=[],
            stats=RetrievalStats(bm25_error="fts down"),
            hints=["hint one"],
        )

        assert out == (
            "No results found. (Note: keyword search unavailable: fts down)\n\n(hint one)"
        )

    async def test_the_semantic_failure_message_still_carries_the_hints(self, monkeypatch):
        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=[],
            stats=RetrievalStats(dense_error="no embedder"),
            hints=["hint one"],
        )

        assert out == (
            "No results found. (Note: semantic search unavailable: no embedder)\n\n(hint one)"
        )

    async def test_a_filter_message_wins_over_a_retriever_error(self, monkeypatch):
        """Message precedence, pinned because the branch chain can reorder it.

        The old code expressed this as return-order; the current code is an
        if/elif chain. Both must pick the filter message when a filter
        excluded everything *and* a retriever failed.
        """
        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=[],
            stats=RetrievalStats(fused_total=5, bm25_error="fts down"),
            tag_filter="redis",
        )

        assert out == (
            "No results match your filters (5 results found before filtering). "
            "Try broader filters or remove source_filter/tag_filter."
        )

    async def test_a_retriever_failure_still_carries_the_one_shot_notice(self, monkeypatch):
        """The combination this fix exists for.

        Two independent degradations that coexist on a store people
        actually run: a BM25 exception sets ``bm25_error``, while an
        embedding-dimension mismatch (which suppresses the *dense* leg)
        raises the one-shot notice. The branch reporting the first used to
        return before the hint tail and swallow the second, permanently.
        """
        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=[],
            stats=RetrievalStats(bm25_error="fts down"),
            dim_notice="embedding dim changed",
        )

        assert out == (
            "No results found. (Note: keyword search unavailable: fts down)"
            "\n\n(embedding dim changed)"
        )

    async def test_structured_empty_carries_core_hints_before_filter_hints(self, monkeypatch):
        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=[],
            stats=RetrievalStats(fused_total=5, bm25_error="fts down"),
            hints=["core hint"],
            output_format="structured",
            tag_filter="redis",
        )

        assert json.loads(out)["hints"] == [
            "core hint",
            "No results match your filters (5 results found before filtering). "
            "Try broader filters or remove source_filter/tag_filter.",
            "keyword search unavailable: fts down",
        ]


class TestFormatterDispatch:
    async def test_compact_asks_the_text_formatter_for_non_verbose_output(self, monkeypatch):
        from memtomem.server.tools import search as search_mod

        formatter = MagicMock(return_value="FORMATTED")
        monkeypatch.setattr(search_mod, "_format_results", formatter)
        results = ["result"]

        out = await _call(
            monkeypatch, app=_fake_app(), results=results, stats=RetrievalStats(final_total=1)
        )

        formatter.assert_called_once_with(results, verbose=False)
        assert out == "FORMATTED"

    async def test_verbose_flag_selects_the_verbose_text_format(self, monkeypatch):
        from memtomem.server.tools import search as search_mod

        formatter = MagicMock(return_value="FORMATTED")
        monkeypatch.setattr(search_mod, "_format_results", formatter)

        await _call(
            monkeypatch,
            app=_fake_app(),
            results=["result"],
            stats=RetrievalStats(final_total=1),
            verbose=True,
        )

        formatter.assert_called_once_with(["result"], verbose=True)

    async def test_verbose_appends_the_pipeline_stat_line(self, monkeypatch):
        from memtomem.server.tools import search as search_mod

        monkeypatch.setattr(search_mod, "_format_results", MagicMock(return_value="FORMATTED"))

        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=["result"],
            stats=RetrievalStats(
                bm25_candidates=4, dense_candidates=3, fused_total=6, final_total=1
            ),
            output_format="verbose",
        )

        assert out == "FORMATTED\n\n---\npipeline: BM25:4 → Dense:3 → RRF:6 → Final:1"

    async def test_compact_notes_an_unavailable_keyword_index(self, monkeypatch):
        from memtomem.server.tools import search as search_mod

        monkeypatch.setattr(search_mod, "_format_results", MagicMock(return_value="FORMATTED"))

        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=["result"],
            stats=RetrievalStats(final_total=1, bm25_error="fts down"),
        )

        assert out == (
            "FORMATTED\n\n(Note: keyword index unavailable — results from semantic search only)"
        )

    async def test_structured_forwards_the_scale_metadata(self, monkeypatch):
        from memtomem.server.tools import search as search_mod

        formatter = MagicMock(return_value="{}")
        monkeypatch.setattr(search_mod, "_format_structured_results", formatter)
        results = ["result"]

        await _call(
            monkeypatch,
            app=_fake_app(),
            results=results,
            stats=RetrievalStats(
                final_total=1,
                score_scale="rerank",
                reranker_model="bge-reranker",
                query_run_id="run-1",
            ),
            hints=["core hint"],
            output_format="structured",
        )

        formatter.assert_called_once_with(
            results,
            hints=["core hint"],
            score_scale="rerank",
            reranker="bge-reranker",
            query_run_id="run-1",
        )

    async def test_text_output_carries_the_hints_as_a_suffix(self, monkeypatch):
        from memtomem.server.tools import search as search_mod

        monkeypatch.setattr(search_mod, "_format_results", MagicMock(return_value="FORMATTED"))

        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=["result"],
            stats=RetrievalStats(final_total=1),
            hints=["hint one"],
        )

        assert out == "FORMATTED\n\n(hint one)"

    async def test_the_dimension_notice_joins_the_core_hints(self, monkeypatch):
        from memtomem.server.tools import search as search_mod

        monkeypatch.setattr(search_mod, "_format_results", MagicMock(return_value="FORMATTED"))

        out = await _call(
            monkeypatch,
            app=_fake_app(),
            results=["result"],
            stats=RetrievalStats(final_total=1),
            hints=["core hint"],
            dim_notice="embedding dim changed",
        )

        assert out == "FORMATTED\n\n(core hint)\n\n(embedding dim changed)"


class TestWebhook:
    async def test_a_search_event_carries_the_query_and_result_count(self, monkeypatch):
        from memtomem.server.tools import search as search_mod

        monkeypatch.setattr(search_mod, "_format_results", MagicMock(return_value="FORMATTED"))
        app = _fake_app(webhooks=True)

        await _call(
            monkeypatch,
            app=app,
            results=["a", "b"],
            stats=RetrievalStats(final_total=2),
        )
        # The wrapper fires and forgets, so let the task run before asserting.
        await asyncio.sleep(0)

        app.webhook_manager.fire.assert_awaited_once_with(
            "search", {"query": "hello", "result_count": 2}
        )

    async def test_no_webhook_manager_is_not_an_error(self, monkeypatch):
        out = await _call(
            monkeypatch,
            app=_fake_app(webhooks=False),
            results=[],
            stats=RetrievalStats(),
        )

        assert out == "No results found."

    async def test_empty_results_fire_no_webhook(self, monkeypatch):
        app = _fake_app(webhooks=True)

        await _call(monkeypatch, app=app, results=[], stats=RetrievalStats())
        await asyncio.sleep(0)

        app.webhook_manager.fire.assert_not_awaited()
