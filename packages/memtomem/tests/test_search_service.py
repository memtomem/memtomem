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
    InvalidRrfWeightError,
    InvalidTemporalBoundError,
    dense_degraded_hint,
    hidden_namespace_hint,
    parse_as_of_bound,
    rrf_weights_from,
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
        '(pass namespace="archive:*" to include them).'
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
        '(pass namespace="archive:*" to include them).',
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
            '(pass namespace="archive:*" to include them).'
        )

    def test_the_suggested_query_is_one_a_user_can_actually_run(self):
        """``NamespaceFilter.parse`` globs only on ``*`` and otherwise matches
        exactly, so the old ``archive:...`` spelling asked for a namespace of
        that literal name and returned nothing."""
        from memtomem.models import NamespaceFilter

        hint = hidden_namespace_hint(3, {"archive:": 3})
        quoted = hint.split('namespace="', 1)[1].split('"', 1)[0]

        assert NamespaceFilter.parse(quoted).pattern == "archive:*"

    def test_every_matching_prefix_gets_its_own_query(self):
        """A comma list cannot carry globs — ``parse`` sees the ``*`` first and
        reads the whole string as one pattern — so each group is offered as a
        separate query rather than a single combined one."""
        hint = hidden_namespace_hint(5, {"agent-runtime:": 2, "archive:": 3})

        assert hint == (
            "5 result(s) hidden in system namespaces: 2 in agent-runtime:*, 3 in archive:* "
            '(pass namespace="agent-runtime:*" or namespace="archive:*" to include each group).'
        )

    def test_prefixes_are_named_in_a_stable_order(self):
        one = hidden_namespace_hint(5, {"archive:": 3, "agent-runtime:": 2})
        other = hidden_namespace_hint(5, {"agent-runtime:": 2, "archive:": 3})

        assert one == other

    def test_a_prefix_that_cannot_be_quoted_gets_no_query(self):
        """The count escapes ``%``; the glob syntax has no way to.

        ``storage/sqlite_helpers`` maps ``*`` to ``%`` and escapes ``_``, but
        leaves an existing ``%`` as a wildcard — so quoting ``team%*`` back at
        the user would select a different set than the one just counted.
        """
        hint = hidden_namespace_hint(2, {"team%": 2})

        assert hint == (
            "2 result(s) hidden in system namespaces: 2 in team%* "
            "(pass an explicit namespace to include them)."
        )

    def test_quotable_prefixes_are_still_offered_alongside_an_unquotable_one(self):
        hint = hidden_namespace_hint(5, {"archive:": 3, "team%": 2})

        assert hint == (
            "5 result(s) hidden in system namespaces: 3 in archive:*, 2 in team%* "
            '(pass namespace="archive:*" to include the groups it names).'
        )

    @pytest.mark.parametrize(
        ("prefix", "quotable"),
        [
            ("archive:", True),
            ("no-colon", True),
            ("under_score", True),  # the SQL step escapes _, so it stays literal
            ("dot.ted", True),
            ("team%", False),
            ("a*b", False),
            ("back\\slash", False),
            ('quo"te', False),
        ],
    )
    def test_only_renderable_prefixes_are_quoted(self, prefix, quotable):
        """Both directions matter: skipping a safe prefix is also a regression.

        An assertion that only fires when a query is present would pass a
        renderer that quoted nothing at all.
        """
        hint = hidden_namespace_hint(1, {prefix: 1})

        assert ('namespace="' in hint) is quotable
        if quotable:
            assert f'namespace="{prefix}*"' in hint

    def test_a_prefix_without_a_trailing_colon_is_quoted_normally(self):
        hint = hidden_namespace_hint(1, {"legacy": 1})

        assert 'namespace="legacy*"' in hint

    def test_an_empty_breakdown_falls_back_to_unqualified_advice(self):
        """Defensive branch: the helper never names a namespace it wasn't given.

        Reached through the tool path only when nothing was quotable — a
        failed count zeroes the total and suppresses the hint entirely, so an
        empty mapping with a non-zero total does not arrive that way.
        """
        assert hidden_namespace_hint(3, {}) == (
            "3 result(s) hidden in system namespaces (pass an explicit namespace to include them)."
        )

    def test_the_noun_is_caller_supplied(self):
        hint = hidden_namespace_hint(1, {"archive:": 1}, noun="memory")

        assert hint.startswith("1 memory hidden in system namespaces:")


class TestRrfWeights:
    """#2087: 0.0 is a value, not an absence.

    Defaulting a zero to 1.0 gave the caller the opposite of what they asked
    for, silently. Since #2092 a zero weight disables its leg outright, so a
    pair with no weighted leg is meaningless and refused.
    """

    def test_a_zero_keyword_weight_survives(self):
        assert rrf_weights_from(0.0, None) == [0.0, 1.0]

    def test_a_zero_meaning_weight_survives(self):
        assert rrf_weights_from(None, 0.0) == [1.0, 0.0]

    def test_both_zero_is_refused(self):
        with pytest.raises(InvalidRrfWeightError, match="cannot both be zero"):
            rrf_weights_from(0.0, 0.0)

    def test_absent_weights_defer_to_server_config(self):
        assert rrf_weights_from(None, None) is None

    def test_one_sided_weight_defaults_its_partner(self):
        assert rrf_weights_from(3.0, None) == [3.0, 1.0]

    def test_both_weights_are_forwarded_verbatim(self):
        assert rrf_weights_from(2.0, 3.0) == [2.0, 3.0]


@pytest.mark.asyncio
async def test_a_zero_weight_reaches_the_pipeline_unchanged():
    pipeline = StubPipeline()

    await _run(pipeline, bm25_weight=0.0)

    assert pipeline.calls[0]["rrf_weights"] == [0.0, 1.0]


class TestRrfWeightValidation:
    """A negative weight inverts a leg rather than de-emphasising it.

    ``w / (k + rank)`` rises toward zero as rank grows, so with ``w = -1``
    rank 50 scores ``-1/110`` and rank 1 scores ``-1/61`` — ``nlargest``
    promotes the worst matches. This guards the request boundary;
    ``search.rrf_weights`` from config is #2094's territory.
    """

    @pytest.mark.parametrize("weight", [-1.0, -0.001, float("nan"), float("inf")])
    def test_a_refused_keyword_weight_names_itself(self, weight):
        with pytest.raises(InvalidRrfWeightError, match="bm25_weight"):
            rrf_weights_from(weight, None)

    @pytest.mark.parametrize("weight", [-1.0, float("-inf")])
    def test_a_refused_meaning_weight_names_itself(self, weight):
        with pytest.raises(InvalidRrfWeightError, match="dense_weight"):
            rrf_weights_from(None, weight)

    @pytest.mark.parametrize("weight", [True, 10**400, "1.0"])
    def test_untyped_garbage_is_refused_not_crashed(self, weight):
        """``mem_do`` raw params are not type-checked: booleans, huge ints
        (where ``math.isfinite`` overflows), and strings must all raise the
        tool-shaped error, never TypeError/OverflowError."""
        with pytest.raises(InvalidRrfWeightError, match="bm25_weight"):
            rrf_weights_from(weight, None)

    def test_a_one_sided_zero_is_not_refused(self):
        assert rrf_weights_from(0.0, None) == [0.0, 1.0]
        assert rrf_weights_from(None, 0.0) == [1.0, 0.0]

    def test_the_inversion_the_guard_prevents(self):
        """Documents why negative is refused rather than merely discouraged."""
        k = 60
        assert (-1.0 / (k + 50)) > (-1.0 / (k + 1))


class TestDenseDegradedHint:
    """Per-search notice for a dense leg suppressed by an embedding mismatch.

    The one-shot server announcement is deliberately not involved here: the
    point of #2063 is that the notice must survive past the first call.
    """

    _MISMATCH = {
        "dimension_mismatch": True,
        "stored": {"provider": "none", "model": "", "dimension": 0},
        "configured": {"provider": "onnx", "model": "bge-small-en-v1.5", "dimension": 384},
    }

    def test_renders_both_sides_and_omits_an_empty_model(self):
        hint = dense_degraded_hint(self._MISMATCH)

        # ``none`` has no model name — ``none/ (0d)`` would be the naive join.
        assert "DB stored none (0d)" in hint
        assert "config uses onnx/bge-small-en-v1.5 (384d)" in hint
        assert "mm embedding-reset" in hint
        assert "configuration.md#reset-flow" in hint

    def test_never_claims_the_results_are_keyword_only(self):
        """BM25 can be disabled or zero-weighted independently of the dense
        leg, so a query can be suppressed *and* have no keyword leg either."""
        assert "keyword-only" not in dense_degraded_hint(self._MISMATCH)

    def test_names_no_destructive_command(self):
        """``apply-current`` deletes every vector. Bare ``mm embedding-reset``
        prints it alongside the non-destructive ``revert-to-stored``, so the
        hint routes through that instead of naming the destructive mode."""
        hint = dense_degraded_hint(self._MISMATCH)

        assert "apply-current" not in hint
        assert "index --force" not in hint

    def test_falls_back_to_a_generic_sentence_without_detail(self):
        hint = dense_degraded_hint(None)

        assert "dense retrieval did not contribute to this query" in hint
        assert "DB stored" not in hint
        assert "mm embedding-reset" in hint

    @pytest.mark.asyncio
    async def test_suppressed_dense_yields_the_hint(self):
        pipeline = StubPipeline(
            stats=RetrievalStats(dense_suppressed_mismatch=True, mismatch_detail=self._MISMATCH)
        )

        _, _, hints = await _run(pipeline)

        assert hints == [dense_degraded_hint(self._MISMATCH)]

    @pytest.mark.asyncio
    async def test_hint_repeats_on_every_degraded_search(self):
        """The #2063 regression shape: the server's one-shot announcement went
        quiet after the first call while the store stayed degraded."""
        pipeline = StubPipeline(
            stats=RetrievalStats(dense_suppressed_mismatch=True, mismatch_detail=self._MISMATCH)
        )

        _, _, first = await _run(pipeline)
        _, _, second = await _run(pipeline)

        assert first == second
        assert any("embedding-reset" in h for h in second)

    @pytest.mark.asyncio
    async def test_no_hint_when_dense_was_not_suppressed(self):
        """A caller who zero-weighted dense got the search they asked for —
        the pipeline reports ``dense_suppressed_mismatch=False`` for it."""
        pipeline = StubPipeline(stats=RetrievalStats(dense_suppressed_mismatch=False))

        _, _, hints = await _run(pipeline)

        assert hints == []

    @pytest.mark.asyncio
    async def test_degradation_precedes_the_discovery_hint(self):
        pipeline = StubPipeline(
            stats=RetrievalStats(
                rerank_applied=False,
                dense_suppressed_mismatch=True,
                mismatch_detail=self._MISMATCH,
                hidden_system_ns=2,
                hidden_by_prefix={"archive:": 2},
            )
        )

        _, _, hints = await _run(pipeline, rerank=True, namespace=None, current_namespace=None)

        assert len(hints) == 3
        assert hints[0].startswith("rerank=true requested")
        assert hints[1] == dense_degraded_hint(self._MISMATCH)
        assert "hidden in system namespaces" in hints[2]


@pytest.mark.asyncio
async def test_the_hint_reads_the_stats_snapshot_not_live_storage():
    """A reset landing between the search and the render must not rewrite what
    this query reports. ``run_search`` reads the detail the pipeline captured,
    so a pipeline whose live storage disagrees still describes the real query.
    """
    captured = {
        "stored": {"provider": "none", "model": "", "dimension": 0},
        "configured": {"provider": "onnx", "model": "bge-small-en-v1.5", "dimension": 384},
    }
    pipeline = StubPipeline(
        stats=RetrievalStats(dense_suppressed_mismatch=True, mismatch_detail=captured)
    )
    # What a caller reaching past the stats would find instead.
    pipeline.storage = type("S", (), {"embedding_mismatch": None})()

    _, _, hints = await _run(pipeline)

    assert hints == [dense_degraded_hint(captured)]
    assert "DB stored none (0d)" in hints[0]
