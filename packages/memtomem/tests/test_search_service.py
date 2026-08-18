"""Unit tests for ``services.search_service.run_search``.

The core runs without a server app, so these drive it with a stub
pipeline and assert on the kwargs it forwards plus the hints it derives
from stats — the parts every surface inherits.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.search.pipeline import RetrievalStats
from memtomem.services.search_service import (
    InvalidTemporalBoundError,
    hidden_namespace_hint,
    parse_as_of_bound,
    run_search,
)

# 2026-01-01T00:00:00Z, the lower bound ``as_of="2026-01-01"`` maps to.
_JAN_1_2026_UTC = 1767225600


class StubPipeline:
    """Records the kwargs of the last ``search`` call."""

    def __init__(self, results=None, stats=None):
        self._results = results if results is not None else []
        self._stats = stats if stats is not None else RetrievalStats()
        self.calls: list[dict] = []

    async def search(self, **kwargs):
        self.calls.append(kwargs)
        return self._results, self._stats


async def _run(pipeline, **overrides):
    kwargs = {"query": "hello", "origin": "test"}
    kwargs.update(overrides)
    return await run_search(pipeline, **kwargs)


def test_parse_as_of_bound_returns_none_for_an_absent_bound():
    assert parse_as_of_bound(None) is None


def test_parse_as_of_bound_rejects_a_malformed_bound():
    with pytest.raises(InvalidTemporalBoundError):
        parse_as_of_bound("2025-Q5")


def test_parse_as_of_bound_accepts_a_quarter():
    assert parse_as_of_bound("2025-Q1") is not None


@pytest.mark.asyncio
async def test_invalid_as_of_raises_with_the_documented_message():
    with pytest.raises(InvalidTemporalBoundError) as excinfo:
        await _run(StubPipeline(), as_of="not-a-date")

    assert str(excinfo.value) == (
        "invalid as_of value 'not-a-date'. "
        "Accepted formats: 'YYYY-MM-DD' (date) or 'YYYY-QN' (quarter, N in 1-4)."
    )


@pytest.mark.asyncio
async def test_every_argument_reaches_the_pipeline_exactly_once():
    """Whole-call pin: a dropped or renamed kwarg fails here.

    Per-argument tests below cover the translations (fallbacks, defaults);
    this one covers the set, so a forwarding gap cannot slip through the
    gaps between them.
    """
    pipeline = StubPipeline()
    root = Path("/tmp/project")

    await run_search(
        pipeline,
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
        project_context_root=root,
        origin="cli",
    )

    assert pipeline.calls == [
        {
            "query": "hello",
            "top_k": 7,
            "source_filter": "notes.md",
            "tag_filter": "redis",
            "namespace": "work",
            "rrf_weights": [2.0, 3.0],
            "context_window": 2,
            "as_of_unix": _JAN_1_2026_UTC,
            "scope": "user",
            "project_context_root": root,
            "rerank": False,
            "origin": "cli",
        }
    ]


@pytest.mark.asyncio
async def test_valid_as_of_is_parsed_into_as_of_unix():
    pipeline = StubPipeline()

    await _run(pipeline, as_of="2026-01-01")

    assert pipeline.calls[0]["as_of_unix"] == _JAN_1_2026_UTC


@pytest.mark.asyncio
async def test_absent_as_of_leaves_as_of_unix_unset():
    pipeline = StubPipeline()

    await _run(pipeline)

    assert pipeline.calls[0]["as_of_unix"] is None


@pytest.mark.asyncio
async def test_namespace_falls_back_to_the_ambient_namespace():
    pipeline = StubPipeline()

    await _run(pipeline, namespace=None, current_namespace="ambient")

    assert pipeline.calls[0]["namespace"] == "ambient"


@pytest.mark.asyncio
async def test_explicit_namespace_overrides_the_ambient_one():
    pipeline = StubPipeline()

    await _run(pipeline, namespace="explicit", current_namespace="ambient")

    assert pipeline.calls[0]["namespace"] == "explicit"


@pytest.mark.asyncio
async def test_one_sided_weight_defaults_its_partner_to_one():
    pipeline = StubPipeline()

    await _run(pipeline, bm25_weight=3.0)

    assert pipeline.calls[0]["rrf_weights"] == [3.0, 1.0]


@pytest.mark.asyncio
async def test_no_weights_leaves_rrf_weights_unset():
    pipeline = StubPipeline()

    await _run(pipeline)

    assert pipeline.calls[0]["rrf_weights"] is None


@pytest.mark.asyncio
async def test_zero_context_window_is_forwarded_as_none():
    pipeline = StubPipeline()

    await _run(pipeline, context_window=0)

    assert pipeline.calls[0]["context_window"] is None


@pytest.mark.asyncio
async def test_positive_context_window_is_forwarded_verbatim():
    pipeline = StubPipeline()

    await _run(pipeline, context_window=2)

    assert pipeline.calls[0]["context_window"] == 2


@pytest.mark.asyncio
async def test_origin_is_forwarded_verbatim():
    pipeline = StubPipeline()

    await _run(pipeline, origin="cli")

    assert pipeline.calls[0]["origin"] == "cli"


@pytest.mark.asyncio
async def test_project_context_root_is_forwarded_verbatim():
    pipeline = StubPipeline()
    root = Path("/tmp/project")

    await _run(pipeline, project_context_root=root)

    assert pipeline.calls[0]["project_context_root"] == root


@pytest.mark.asyncio
async def test_requested_rerank_that_did_not_apply_yields_a_hint():
    pipeline = StubPipeline(stats=RetrievalStats(rerank_applied=False))

    _, _, hints = await _run(pipeline, rerank=True)

    assert hints == [
        "rerank=true requested but server reranking is disabled "
        "(rerank.enabled=false); results are un-reranked."
    ]


@pytest.mark.asyncio
async def test_applied_rerank_yields_no_hint():
    pipeline = StubPipeline(stats=RetrievalStats(rerank_applied=True))

    _, _, hints = await _run(pipeline, rerank=True)

    assert hints == []


@pytest.mark.asyncio
async def test_hidden_system_namespaces_yield_a_hint_when_no_namespace_is_pinned():
    pipeline = StubPipeline(
        stats=RetrievalStats(hidden_system_ns=3, hidden_by_prefix={"archive:": 3})
    )

    _, _, hints = await _run(pipeline, namespace=None, current_namespace=None)

    assert hints == [
        "3 result(s) hidden in system namespaces: 3 in archive:* "
        '(pass namespace="archive:..." to include them).'
    ]


@pytest.mark.asyncio
async def test_both_hints_are_emitted_rerank_first():
    pipeline = StubPipeline(
        stats=RetrievalStats(
            rerank_applied=False, hidden_system_ns=2, hidden_by_prefix={"archive:": 2}
        ),
    )

    _, _, hints = await _run(pipeline, rerank=True, namespace=None, current_namespace=None)

    assert hints == [
        "rerank=true requested but server reranking is disabled "
        "(rerank.enabled=false); results are un-reranked.",
        "2 result(s) hidden in system namespaces: 2 in archive:* "
        '(pass namespace="archive:..." to include them).',
    ]


@pytest.mark.asyncio
async def test_pinned_namespace_suppresses_the_hidden_namespace_hint():
    pipeline = StubPipeline(stats=RetrievalStats(hidden_system_ns=3))

    _, _, hints = await _run(pipeline, namespace="work", current_namespace=None)

    assert hints == []


@pytest.mark.asyncio
async def test_results_and_stats_pass_through_untouched():
    stats = RetrievalStats(final_total=1)
    sentinel = ["result-object"]
    pipeline = StubPipeline(results=sentinel, stats=stats)

    results, returned_stats, _ = await _run(pipeline)

    assert results is sentinel
    assert returned_stats is stats


class TestHiddenNamespaceHint:
    """#2088: the hint used to name ``archive:`` whatever actually hid the rows.

    ``system_namespace_prefixes`` defaults to two entries, so telling an
    unbound agent search to look in ``archive:`` sends it somewhere that
    holds none of the rows just counted.
    """

    def test_a_single_prefix_is_named_with_its_count(self):
        assert hidden_namespace_hint(3, {"archive:": 3}) == (
            "3 result(s) hidden in system namespaces: 3 in archive:* "
            '(pass namespace="archive:..." to include them).'
        )

    def test_every_matching_prefix_is_named(self):
        hint = hidden_namespace_hint(5, {"agent-runtime:": 2, "archive:": 3})

        assert hint == (
            "5 result(s) hidden in system namespaces: 2 in agent-runtime:*, 3 in archive:* "
            '(pass namespace="agent-runtime:..." to include them).'
        )

    def test_prefixes_are_named_in_a_stable_order(self):
        one = hidden_namespace_hint(5, {"archive:": 3, "agent-runtime:": 2})
        other = hidden_namespace_hint(5, {"agent-runtime:": 2, "archive:": 3})

        assert one == other

    def test_an_unavailable_breakdown_falls_back_to_unqualified_advice(self):
        """A failed count must not put a namespace name in the user's hands."""
        assert hidden_namespace_hint(3, {}) == (
            "3 result(s) hidden in system namespaces (pass an explicit namespace to include them)."
        )

    def test_the_noun_is_caller_supplied(self):
        hint = hidden_namespace_hint(1, {"archive:": 1}, noun="memory")

        assert hint.startswith("1 memory hidden in system namespaces:")
