"""Replay engine tests (#1802, Quality Lab PR-4).

All hermetic on ``bm25_only_components`` (dense off → no embedder download), so
the fixture eval set runs in CI without pulling a model. Covers the report's
determinism contract, no-side-effects guarantee, metric correctness, staleness,
stage-outcome degradation, selection/dedup, portable-filter enforcement, the
nondeterminism-flag derivation, and the artifact privacy guarantee.
"""

from __future__ import annotations

import asyncio

import pytest
from helpers import make_chunk as _make_chunk

from memtomem.errors import EvalCaseError
from memtomem.quality.replay import replay_cases, serialize_report
from memtomem.quality.state import nondeterministic_stages
from memtomem.storage.mixins.eval_cases import (
    EVAL_CASE_SET_KIND,
    EVAL_CASE_SET_SCHEMA_VERSION,
)


async def _drain_bg(pipeline):
    if pipeline._bg_tasks:
        await asyncio.gather(*list(pipeline._bg_tasks), return_exceptions=True)


def _envelope(cases: list[dict]) -> dict:
    return {
        "schema_version": EVAL_CASE_SET_SCHEMA_VERSION,
        "kind": EVAL_CASE_SET_KIND,
        "cases": cases,
    }


def _case(
    case_id: str,
    query: str,
    labels: list[tuple[str, str]],
    *,
    name: str | None = None,
    top_k: int = 5,
    status: str = "active",
    filters: dict | None = None,
) -> dict:
    return {
        "case_id": case_id,
        "name": name,
        "query_text": query,
        "top_k": top_k,
        "version": 1,
        "status": status,
        "filters": filters if filters is not None else {"namespace": None, "scope": None},
        "labels": [{"content_hash": h, "judgment": j} for h, j in labels],
    }


async def _seed(storage, texts: list[tuple[str, str]]) -> list[str]:
    """Upsert chunks ``[(body, source)]``; return their content_hashes."""
    chunks = [_make_chunk(body, source=src) for body, src in texts]
    await storage.upsert_chunks(chunks)
    return [c.content_hash for c in chunks]


async def _seed_chunks(storage, texts: list[tuple[str, str]]):
    """Upsert chunks ``[(body, source)]``; return the Chunk objects."""
    chunks = [_make_chunk(body, source=src) for body, src in texts]
    await storage.upsert_chunks(chunks)
    return chunks


class TestDeterminism:
    async def test_double_run_byte_identical(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        hashes = await _seed(storage, [("alpha beta gamma", "a.md"), ("delta epsilon", "b.md")])
        await storage.import_eval_cases(
            _envelope([_case("c-aaaa", "alpha", [(hashes[0], "relevant")])])
        )

        r1 = await replay_cases(storage, pipeline, comp.config, as_of_unix=1_784_500_000)
        r2 = await replay_cases(storage, pipeline, comp.config, as_of_unix=1_784_500_000)
        assert serialize_report(r1) == serialize_report(r2)
        assert r1["deterministic"] is True
        assert r1["nondeterministic_stages"] == []

    async def test_report_has_no_absolute_paths_or_content(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        # make_chunk sources live under /tmp; body carries a sentinel token.
        hashes = await _seed(storage, [("SENTINEL-CONTENT alpha beta", "SENTINEL-PATH.md")])
        await storage.import_eval_cases(
            _envelope([_case("c-priv", "alpha", [(hashes[0], "relevant")])])
        )
        report = await replay_cases(storage, pipeline, comp.config, as_of_unix=1)
        blob = serialize_report(report)
        assert "SENTINEL-CONTENT" not in blob
        assert "SENTINEL-PATH" not in blob
        assert "/tmp/" not in blob


class TestNoSideEffects:
    async def test_replay_mutates_nothing(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        chunks = await _seed_chunks(storage, [("alpha beta", f"n{i}.md") for i in range(3)])
        ids = [c.id for c in chunks]
        await storage.import_eval_cases(
            _envelope([_case("c-side", "alpha", [(chunks[0].content_hash, "relevant")])])
        )
        before = await storage.get_access_counts(ids)

        await replay_cases(storage, pipeline, comp.config, as_of_unix=1)
        await _drain_bg(pipeline)

        assert await storage.get_search_runs() == []
        assert await storage.get_access_counts(ids) == before
        assert pipeline._search_cache == {}
        assert pipeline._expansion_cache == {}


class TestMetrics:
    async def test_known_labels_score(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        # Two chunks, both labeled → precision computable.
        hashes = await _seed(
            storage, [("alpha unique token", "a.md"), ("alpha other token", "b.md")]
        )
        await storage.import_eval_cases(
            _envelope(
                [
                    _case(
                        "c-metrics",
                        "alpha",
                        [(hashes[0], "relevant"), (hashes[1], "not_relevant")],
                    )
                ]
            )
        )
        report = await replay_cases(storage, pipeline, comp.config, as_of_unix=1)
        case = report["cases"][0]
        assert case["metrics"]["hit_rate"] == 1.0
        assert case["metrics"]["precision"] is not None  # both retrieved items labeled
        assert "incomplete_labels" not in case["flags"]
        assert case["included_in_aggregate"] is True

    async def test_incomplete_precision_excluded_from_precision_aggregate(
        self, bm25_only_components
    ):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        # An unlabeled retrieved chunk makes the top-k window incomplete.
        hashes = await _seed(storage, [("alpha labeled", "a.md"), ("alpha unlabeled body", "b.md")])
        await storage.import_eval_cases(
            _envelope([_case("c-inc", "alpha", [(hashes[0], "relevant")])])
        )
        report = await replay_cases(storage, pipeline, comp.config, as_of_unix=1)
        case = report["cases"][0]
        assert case["metrics"]["precision"] is None
        assert "incomplete_labels" in case["flags"]
        # Still in the overall aggregate, just not the precision cohort.
        assert case["included_in_aggregate"] is True
        assert report["aggregate"]["mean_precision"]["evaluated"] == 0
        assert report["aggregate"]["mean_precision"]["incomplete"] == 1


class TestStaleness:
    async def test_imported_case_axes_null(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        hashes = await _seed(storage, [("alpha", "a.md")])
        await storage.import_eval_cases(
            _envelope([_case("c-stale", "alpha", [(hashes[0], "relevant")])])
        )
        report = await replay_cases(storage, pipeline, comp.config, as_of_unix=1)
        stale = report["cases"][0]["stale"]
        # Imported cases carry empty promoted fingerprints → unknowable, not drift.
        assert stale == {"profile": None, "corpus": None, "index": None}
        assert not any(f.startswith("stale_") for f in report["cases"][0]["flags"])

    async def test_promoted_case_detects_corpus_drift(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        from memtomem.quality.state import current_fingerprints

        hashes = await _seed(storage, [("alpha beta gamma", "a.md")])
        # Promote a real run so promoted fingerprints are the live ones.
        snapshot = [{"chunk_id": "c1", "rank": 1, "score": 0.9, "content_hash": hashes[0]}]
        await storage.save_search_observation(
            "alpha",
            [0.1],
            ["c1"],
            [0.9],
            run_id="99999999-9999-4999-8999-999999999999",
            observation={"origin": "cli", "top_k": 5, "filters": {}},
            result_snapshot=snapshot,
        )
        await storage.save_search_feedback("99999999-9999-4999-8999-999999999999", "c1", "relevant")
        fps, _ = current_fingerprints(storage, comp.config)
        await storage.promote_search_run(
            "99999999-9999-4999-8999-999999999999", name="promoted", fingerprints=fps
        )

        # Mutate the corpus so corpus/index fingerprints drift.
        await _seed(storage, [("newly added chunk body", "z.md")])
        report = await replay_cases(storage, pipeline, comp.config, as_of_unix=1)
        case = report["cases"][0]
        assert case["stale"]["profile"] is False
        assert case["stale"]["corpus"] is True
        assert case["stale"]["index"] is True
        assert "stale_corpus" in case["flags"]


class TestSelection:
    async def test_archived_skipped_by_default_but_explicit_replays(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        hashes = await _seed(storage, [("alpha", "a.md")])
        await storage.import_eval_cases(
            _envelope(
                [
                    _case("c-active", "alpha", [(hashes[0], "relevant")]),
                    _case(
                        "c-arch",
                        "alpha",
                        [(hashes[0], "relevant")],
                        name="arch",
                        status="archived",
                    ),
                ]
            )
        )
        default = await replay_cases(storage, pipeline, comp.config, as_of_unix=1)
        assert [c["case_id"] for c in default["cases"]] == ["c-active"]
        assert default["counts"]["archived_skipped"] == 1

        explicit = await replay_cases(
            storage, pipeline, comp.config, case_ids=["arch"], as_of_unix=1
        )
        assert [c["case_id"] for c in explicit["cases"]] == ["c-arch"]
        assert "archived" in explicit["cases"][0]["flags"]

    async def test_id_and_name_alias_dedup(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        hashes = await _seed(storage, [("alpha", "a.md")])
        await storage.import_eval_cases(
            _envelope([_case("c-dup", "alpha", [(hashes[0], "relevant")], name="dupname")])
        )
        report = await replay_cases(
            storage, pipeline, comp.config, case_ids=["c-dup", "dupname"], as_of_unix=1
        )
        assert [c["case_id"] for c in report["cases"]] == ["c-dup"]

    async def test_unknown_selector_raises(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        with pytest.raises(EvalCaseError):
            await replay_cases(storage, pipeline, comp.config, case_ids=["nope"], as_of_unix=1)


class TestDegradation:
    async def test_expansion_failure_marks_degraded_and_excludes(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        hashes = await _seed(storage, [("alpha", "a.md")])
        await storage.import_eval_cases(
            _envelope([_case("c-deg", "alpha", [(hashes[0], "relevant")])])
        )

        # Force a tag-expansion failure: enable tag expansion, break get_tag_counts.
        from memtomem.config import QueryExpansionConfig

        pipeline._expansion_config = QueryExpansionConfig(enabled=True, strategy="tags")

        async def _boom():
            raise RuntimeError("tag store offline")

        storage.get_tag_counts = _boom  # type: ignore[method-assign]

        report = await replay_cases(storage, pipeline, comp.config, as_of_unix=1)
        case = report["cases"][0]
        assert case["stage_outcomes"]["expansion_failed"] is True
        assert "degraded" in case["flags"]
        assert case["included_in_aggregate"] is False
        # No raw error text leaks into the artifact.
        assert "offline" not in serialize_report(report)

    async def test_rerank_fallback_with_empty_results_marks_degraded(self, bm25_only_components):
        # A reranker that fell back (score_scale stayed fused) and then had every
        # result filtered out must still read as degraded — the fallback is
        # detected from the pre-filter pool (fused_total>0), not bool(results).
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        hashes = await _seed(storage, [("alpha", "a.md")])
        await storage.import_eval_cases(
            _envelope([_case("c-rrfb", "alpha", [(hashes[0], "relevant")])])
        )

        from memtomem.search.pipeline import RetrievalStats

        async def _fake_search(query, **kwargs):
            stats = RetrievalStats(
                fused_total=5, rerank_applied=True, score_scale="rrf", final_total=0
            )
            return [], stats

        pipeline.search = _fake_search  # type: ignore[method-assign]
        report = await replay_cases(storage, pipeline, comp.config, as_of_unix=1)
        case = report["cases"][0]
        assert case["stage_outcomes"]["rerank_fallback"] is True
        assert "degraded" in case["flags"]
        assert case["included_in_aggregate"] is False

    async def test_dense_suppressed_mismatch_marks_degraded(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        hashes = await _seed(storage, [("alpha", "a.md")])
        await storage.import_eval_cases(
            _envelope([_case("c-mism", "alpha", [(hashes[0], "relevant")])])
        )
        # Simulate a stored embedding-policy mismatch with dense configured.
        # ``embedding_mismatch`` is a read-only property derived from these; set
        # the backing field so it returns a dict.
        pipeline._config.enable_dense = True
        storage._dim_mismatch = (768, 1024)
        try:
            report = await replay_cases(storage, pipeline, comp.config, as_of_unix=1)
        finally:
            pipeline._config.enable_dense = False
            storage._dim_mismatch = None
        case = report["cases"][0]
        assert case["stage_outcomes"]["dense_suppressed_mismatch"] is True
        assert case["included_in_aggregate"] is False


class TestPortableFilters:
    async def test_import_rejects_unsupported_filter_key(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage = comp.storage
        case = _case("c-bad", "alpha", [("hash-1", "relevant")])
        case["filters"] = {"source_exact": ["/etc/passwd"]}
        with pytest.raises(EvalCaseError):
            await storage.import_eval_cases(_envelope([case]))

    async def test_import_rejects_path_shaped_namespace(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage = comp.storage
        case = _case("c-path", "alpha", [("hash-1", "relevant")])
        case["filters"] = {"namespace": "/Users/secret/notes"}
        with pytest.raises(EvalCaseError):
            await storage.import_eval_cases(_envelope([case]))

    async def test_import_rejects_ill_typed_namespace(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage = comp.storage
        case = _case("c-typ", "alpha", [("hash-1", "relevant")])
        case["filters"] = {"namespace": 7}
        with pytest.raises(EvalCaseError):
            await storage.import_eval_cases(_envelope([case]))

    async def test_import_rejects_project_scope(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage = comp.storage
        case = _case("c-proj", "alpha", [("hash-1", "relevant")])
        case["filters"] = {"scope": "project_shared"}
        with pytest.raises(EvalCaseError):
            await storage.import_eval_cases(_envelope([case]))

    async def test_unreplayable_filter_case_excluded(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        hashes = await _seed(storage, [("alpha", "a.md")])
        case = _case("c-unrep", "alpha", [(hashes[0], "relevant")])
        case["filters"] = {"namespace": None, "scope": None, "unreplayable": ["has_tag_filter"]}
        await storage.import_eval_cases(_envelope([case]))
        report = await replay_cases(storage, pipeline, comp.config, as_of_unix=1)
        c = report["cases"][0]
        assert "unreplayable_filters" in c["flags"]
        assert c["included_in_aggregate"] is False


class TestResultDedup:
    async def test_duplicate_content_hash_collapsed(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage, pipeline = comp.storage, comp.search_pipeline
        # Two distinct chunks (different sources/ids) that share one content_hash.
        dup = "hash-shared-dup"
        chunks = [
            _make_chunk("alpha shared body one", source="a.md"),
            _make_chunk("alpha shared body two", source="b.md"),
        ]
        for c in chunks:
            c.content_hash = dup
        await storage.upsert_chunks(chunks)
        await storage.import_eval_cases(
            _envelope([_case("c-dup-hash", "alpha", [(dup, "relevant")])])
        )
        report = await replay_cases(storage, pipeline, comp.config, as_of_unix=1)
        retrieved = report["cases"][0]["retrieved"]
        hashes = [item["content_hash"] for item in retrieved]
        # The shared hash appears once; ranks stay 1-based positions.
        assert hashes.count(dup) == 1
        assert [item["rank"] for item in retrieved] == list(range(1, len(retrieved) + 1))


class TestNondeterminismDerivation:
    async def test_llm_strategy_without_provider_is_deterministic(self, bm25_only_components):
        comp, _ = bm25_only_components
        pipeline = comp.search_pipeline
        from memtomem.config import QueryExpansionConfig

        comp.config.query_expansion = QueryExpansionConfig(enabled=True, strategy="llm")
        pipeline._llm_provider = None
        assert nondeterministic_stages(comp.config, pipeline) == []

    async def test_llm_strategy_with_provider_flagged(self, bm25_only_components):
        comp, _ = bm25_only_components
        pipeline = comp.search_pipeline
        from memtomem.config import QueryExpansionConfig

        comp.config.query_expansion = QueryExpansionConfig(enabled=True, strategy="llm")
        pipeline._llm_provider = object()
        assert "query_expansion_llm" in nondeterministic_stages(comp.config, pipeline)

    async def test_remote_embedding_via_heading_expansion(self, bm25_only_components):
        comp, _ = bm25_only_components
        pipeline = comp.search_pipeline
        from memtomem.config import QueryExpansionConfig

        # Dense off, but heading expansion embeds independently with a remote provider.
        comp.config.embedding.provider = "openai"
        comp.config.search.enable_dense = False
        comp.config.query_expansion = QueryExpansionConfig(enabled=True, strategy="headings")
        assert "embedding_remote" in nondeterministic_stages(comp.config, pipeline)


class TestTagExpansionDeterminism:
    async def test_tied_counts_are_order_stable(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage = comp.storage
        # Two tags with identical counts must return in a stable order.
        chunks = [
            _make_chunk("body one", source="a.md", tags=("zzz", "aaa")),
            _make_chunk("body two", source="b.md", tags=("zzz", "aaa")),
        ]
        await storage.upsert_chunks(chunks)
        first = await storage.get_tag_counts()
        second = await storage.get_tag_counts()
        assert first == second
        # value ASC tie-break: 'aaa' before 'zzz' among equal counts.
        names = [t for t, _ in first]
        assert names.index("aaa") < names.index("zzz")
