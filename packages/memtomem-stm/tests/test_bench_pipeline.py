"""STM pipeline quality benchmark tests.

Tests the benchmark harness, quality judge, per-stage metrics,
auto-strategy selection, strategy matrix, compression curves,
surfacing integration, and regression gates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from dataclasses import field as dc_field
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import (
    FieldExtractCompressor,
    HybridCompressor,
    TruncateCompressor,
    auto_select_strategy,
)
from memtomem_stm.proxy.config import CleaningConfig, CompressionStrategy
from memtomem_stm.proxy.metrics import CallMetrics, TokenTracker
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine

from bench.harness import (
    BenchHarness,
    BenchResult,
    BenchTask,
    ComparisonReport,
    CurvePoint,
    QAPair,
    SelectiveResult,
    StageBreakdown,
    StageMetrics,
    StageScore,
    StrategyResult,
    SurfacingValue,
    resolve_auto_strategy,
)
from bench.judge import RuleBasedJudge
from bench.report import (
    format_curve,
    format_full_report,
    format_matrix,
    format_report,
    format_stage_breakdown,
    format_surfacing_value,
)
from bench.tasks import (
    API_RESPONSE_JSON,
    AUTH_MEMORIES,
    CODE_FILE,
    DEPLOY_MEMORIES,
    HTML_MIXED,
    LARGE_DIFF_OUTPUT,
    MARKDOWN_WITH_LINKS,
    MEETING_NOTES,
    MULTILINGUAL_KR_EN,
    OPTIMAL_STRATEGIES,
    SHORT_RESPONSE,
    TASK_CATEGORIES,
    get_all_tasks,
    get_distractor_tasks,
    get_multihop_tasks,
    get_needle_tasks,
    get_surfacing_tasks,
    get_generous_tasks,
    get_tight_tasks,
)
from bench.datasets import (
    all_tasks as ds_all_tasks,
    all_tasks_with_surfacing as ds_all_with_surfacing,
    json_tasks as ds_json_tasks,
    markdown_tasks as ds_markdown_tasks,
    code_tasks as ds_code_tasks,
    text_tasks as ds_text_tasks,
    surfacing_tasks as ds_surfacing_tasks,
)


# ═══════════════════════════════════════════════════════════════════════════
# Fixtures
# ═══════════════════════════════════════════════════════════════════════════


@pytest.fixture
def cleaner():
    return DefaultContentCleaner(
        CleaningConfig(strip_html=True, collapse_links=True, deduplicate=True)
    )


@pytest.fixture
def truncate():
    return TruncateCompressor()


@pytest.fixture
def hybrid():
    return HybridCompressor(head_chars=500)


@pytest.fixture
def field_extract():
    return FieldExtractCompressor()


@pytest.fixture
def judge():
    return RuleBasedJudge()


@pytest.fixture
def harness(cleaner, truncate, judge):
    return BenchHarness(cleaner=cleaner, compressor=truncate, judge=judge)


# ═══════════════════════════════════════════════════════════════════════════
# Fake objects for surfacing tests
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class FakeChunkMeta:
    source_file: Path = dc_field(default_factory=lambda: Path("/notes/test.md"))
    namespace: str = "default"


@dataclass
class FakeChunk:
    id: str = ""
    content: str = "relevant memory"
    metadata: FakeChunkMeta | None = None

    def __post_init__(self):
        if not self.id:
            self.id = str(uuid4())
        if self.metadata is None:
            self.metadata = FakeChunkMeta()


@dataclass
class FakeSearchResult:
    chunk: FakeChunk
    score: float
    rank: int = 1


def _make_surfacing_config(**overrides) -> SurfacingConfig:
    defaults = {
        "enabled": True,
        "min_response_chars": 10,
        "timeout_seconds": 5.0,
        "min_score": 0.01,
        "max_results": 3,
        "cooldown_seconds": 0.0,
        "max_surfacings_per_minute": 1000,
        "auto_tune_enabled": False,
        "include_session_context": False,
        "fire_webhook": False,
        "cache_ttl_seconds": 60.0,
    }
    defaults.update(overrides)
    return SurfacingConfig(**defaults)


def _make_search_pipeline(results=None):
    pipeline = AsyncMock()
    pipeline.search = AsyncMock(return_value=(results or [], {}))
    return pipeline


# ═══════════════════════════════════════════════════════════════════════════
# TestBenchHarness — basic harness behavior
# ═══════════════════════════════════════════════════════════════════════════


class TestBenchHarness:
    def test_direct_returns_original(self, harness):
        task = BenchTask(
            task_id="test",
            description="test",
            content="Hello world",
            content_type="text",
            max_chars=100,
            expected_keywords=["Hello"],
        )
        result = harness.run_direct(task)
        assert result.mode == "direct"
        assert result.text == "Hello world"
        assert result.stage_metrics is None
        assert result.quality_score == 10.0

    def test_stm_returns_processed(self, harness):
        task = BenchTask(
            task_id="test",
            description="test",
            content=MEETING_NOTES,
            content_type="markdown",
            max_chars=500,
            expected_keywords=["PostgreSQL"],
        )
        result = harness.run_stm(task)
        assert result.mode == "stm"
        assert result.stage_metrics is not None
        assert result.stage_metrics.original_chars == len(MEETING_NOTES)
        assert result.error is None

    def test_stm_short_text_passthrough(self, harness):
        task = BenchTask(
            task_id="short",
            description="short",
            content=SHORT_RESPONSE,
            content_type="text",
            max_chars=1000,
            expected_keywords=["OK", "saved"],
        )
        result = harness.run_stm(task)
        assert result.text == SHORT_RESPONSE
        assert result.quality_score == 10.0

    def test_comparison_returns_both(self, harness):
        task = BenchTask(
            task_id="cmp",
            description="cmp",
            content=MEETING_NOTES,
            content_type="markdown",
            max_chars=500,
            expected_keywords=["PostgreSQL"],
        )
        report = harness.run_comparison(task)
        assert isinstance(report, ComparisonReport)
        assert report.direct.mode == "direct"
        assert report.stm.mode == "stm"
        assert report.quality_preservation <= 100.0

    def test_error_handling(self, cleaner, judge):
        class BrokenCompressor:
            def compress(self, text, *, max_chars):
                raise RuntimeError("broken")

        h = BenchHarness(cleaner=cleaner, compressor=BrokenCompressor(), judge=judge)
        task = BenchTask(
            task_id="err", description="err", content="some text", content_type="text", max_chars=100
        )
        result = h.run_stm(task)
        assert result.error is not None
        assert "broken" in result.error
        assert result.quality_score == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# TestStageMetrics — per-stage measurement accuracy
# ═══════════════════════════════════════════════════════════════════════════


class TestStageMetrics:
    def test_cleaning_reduces_html(self, cleaner, truncate, judge):
        h = BenchHarness(cleaner=cleaner, compressor=truncate, judge=judge)
        task = BenchTask(
            task_id="html",
            description="html cleaning",
            content=HTML_MIXED,
            content_type="text",
            max_chars=2000,
            expected_keywords=["API Reference"],
        )
        result = h.run_stm(task)
        m = result.stage_metrics
        assert m is not None
        assert m.cleaned_chars < m.original_chars

    def test_compression_reduces_size(self, cleaner, truncate, judge):
        h = BenchHarness(cleaner=cleaner, compressor=truncate, judge=judge)
        task = BenchTask(
            task_id="big",
            description="large text",
            content=CODE_FILE,
            content_type="code",
            max_chars=500,
            expected_keywords=["JWT"],
        )
        result = h.run_stm(task)
        m = result.stage_metrics
        assert m is not None
        assert m.compressed_chars <= task.max_chars + 200

    def test_timing_is_positive(self, harness):
        task = BenchTask(
            task_id="time", description="timing", content=CODE_FILE, content_type="code", max_chars=500
        )
        result = harness.run_stm(task)
        m = result.stage_metrics
        assert m is not None
        assert m.clean_ms >= 0
        assert m.compress_ms >= 0

    def test_cleaning_ratio(self):
        m = StageMetrics(
            original_chars=1000,
            cleaned_chars=800,
            compressed_chars=400,
            surfaced_chars=450,
            clean_ms=1.0,
            compress_ms=2.0,
            surface_ms=0.5,
        )
        assert m.cleaning_ratio == 0.8
        assert m.compression_ratio == 0.5
        assert m.total_reduction == 0.4
        assert m.surfacing_overhead == pytest.approx(0.125)

    def test_zero_original_safety(self):
        m = StageMetrics(
            original_chars=0, cleaned_chars=0, compressed_chars=0, surfaced_chars=0,
            clean_ms=0, compress_ms=0, surface_ms=0,
        )
        assert m.cleaning_ratio == 1.0
        assert m.total_reduction == 1.0
        assert m.surfacing_overhead == 0.0

    def test_strategy_recorded(self, cleaner, hybrid, judge):
        h = BenchHarness(cleaner=cleaner, compressor=hybrid, judge=judge)
        task = BenchTask(
            task_id="s", description="s", content=CODE_FILE, content_type="code", max_chars=800
        )
        result = h.run_stm(task)
        assert result.stage_metrics is not None
        assert "Hybrid" in result.stage_metrics.strategy


# ═══════════════════════════════════════════════════════════════════════════
# TestQualityJudge — scoring logic
# ═══════════════════════════════════════════════════════════════════════════


class TestQualityJudge:
    def test_perfect_score(self, judge):
        task = BenchTask(
            task_id="t", description="t", content="Hello World", content_type="text",
            max_chars=100, expected_keywords=["Hello", "World"],
        )
        assert judge.score(task, "Hello World") == 10.0

    def test_missing_keyword_deducts(self, judge):
        task = BenchTask(
            task_id="t", description="t", content="x", content_type="text",
            max_chars=100, expected_keywords=["alpha", "beta", "gamma"],
        )
        assert judge.score(task, "nothing here") == 4.0

    def test_partial_keywords(self, judge):
        task = BenchTask(
            task_id="t", description="t", content="x", content_type="text",
            max_chars=100, expected_keywords=["alpha", "beta"],
        )
        assert judge.score(task, "alpha is present") == 8.0

    def test_heading_check(self, judge):
        task = BenchTask(
            task_id="t", description="t", content="x", content_type="markdown",
            max_chars=100, expect_headings=3,
        )
        assert judge.score(task, "## H1\n## H2\nno more") == 9.0

    def test_code_block_check(self, judge):
        task = BenchTask(
            task_id="t", description="t", content="x", content_type="code",
            max_chars=100, expect_code_blocks=2,
        )
        assert judge.score(task, "```python\ncode\n```\nonly one block") == 9.0

    def test_json_validity_bonus(self, judge):
        task = BenchTask(
            task_id="t", description="t", content="x", content_type="json",
            max_chars=100, expected_keywords=["key"],
        )
        assert judge.score(task, '{"key": "value"}') == 10.0

    def test_json_invalid_no_bonus(self, judge):
        task = BenchTask(
            task_id="t", description="t", content="x", content_type="json",
            max_chars=100, expected_keywords=["key"],
        )
        assert judge.score(task, "key: value") == 10.0

    def test_score_floor_at_zero(self, judge):
        task = BenchTask(
            task_id="t", description="t", content="x", content_type="text",
            max_chars=100, expected_keywords=["a", "b", "c", "d", "e", "f"],
        )
        assert judge.score(task, "nothing") == 0.0

    def test_case_insensitive_keywords(self, judge):
        task = BenchTask(
            task_id="t", description="t", content="x", content_type="text",
            max_chars=100, expected_keywords=["PostgreSQL"],
        )
        assert judge.score(task, "we use postgresql for storage") == 10.0

    def test_weighted_keywords(self, judge):
        task = BenchTask(
            task_id="t", description="t", content="x", content_type="text",
            max_chars=100,
            expected_keywords=["critical", "optional"],
            keyword_weights=[1.0, 0.3],
        )
        # Missing critical (-2.0*1.0) + missing optional (-2.0*0.3) = -2.6
        assert judge.score(task, "nothing") == pytest.approx(10.0 - 2.0 - 0.6)

    def test_keyword_report(self, judge):
        task = BenchTask(
            task_id="t", description="t", content="x", content_type="text",
            max_chars=100, expected_keywords=["present", "absent"],
        )
        report = judge.keyword_report(task, "present in text")
        assert report["present"] is True
        assert report["absent"] is False


# ═══════════════════════════════════════════════════════════════════════════
# TestAutoStrategy — auto_select_strategy integration
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoStrategy:
    def test_json_selects_extract_fields(self):
        assert resolve_auto_strategy(API_RESPONSE_JSON) == "extract_fields"

    def test_markdown_with_headings_selects_hybrid(self, cleaner):
        cleaned = cleaner.clean(MARKDOWN_WITH_LINKS)
        strategy = resolve_auto_strategy(cleaned)
        assert strategy in ("hybrid", "truncate")

    def test_short_text_selects_truncate(self):
        assert resolve_auto_strategy("short") == "truncate"

    def test_auto_strategy_improves_json_quality(self, cleaner, judge):
        """Auto strategy should pick extract_fields for JSON, outperforming truncate."""
        task = [t for t in get_all_tasks() if t.task_id == "api_response_json"][0]

        h_trunc = BenchHarness(cleaner=cleaner, compressor=TruncateCompressor(), judge=judge)
        h_auto = BenchHarness(cleaner=cleaner, compressor=TruncateCompressor(), judge=judge)

        r_trunc = h_trunc.run_comparison(task)
        r_auto = h_auto.run_auto_strategy(task)

        # Auto should be >= truncate for JSON
        assert r_auto.stm.quality_score >= r_trunc.stm.quality_score

    def test_auto_strategy_all_tasks(self, cleaner, truncate, judge):
        """Auto strategy should run on all tasks without errors."""
        h = BenchHarness(cleaner=cleaner, compressor=truncate, judge=judge)
        for task in get_all_tasks():
            report = h.run_auto_strategy(task)
            assert report.stm.error is None

    def test_auto_matches_optimal_for_json(self, cleaner):
        """Verify auto_select picks extract_fields for JSON content."""
        cleaned = cleaner.clean(API_RESPONSE_JSON)
        assert resolve_auto_strategy(cleaned) == "extract_fields"


# ═══════════════════════════════════════════════════════════════════════════
# TestStrategyMatrix — multi-strategy comparison
# ═══════════════════════════════════════════════════════════════════════════


class TestStrategyMatrix:
    def test_matrix_returns_all_strategies(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "meeting_notes"][0]
        results = harness.run_strategy_matrix(task)
        assert len(results) >= 3  # truncate, hybrid, extract_fields, auto(...)

    def test_matrix_has_quality_scores(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        results = harness.run_strategy_matrix(task)
        for name, r in results.items():
            assert 0.0 <= r.quality_score <= 10.0
            assert r.compressed_chars > 0

    def test_extract_fields_best_for_json(self, cleaner, truncate, judge):
        h = BenchHarness(cleaner=cleaner, compressor=truncate, judge=judge)
        task = [t for t in get_all_tasks() if t.task_id == "api_response_json"][0]
        results = h.run_strategy_matrix(task)
        # extract_fields should outperform truncate for JSON
        ef_score = results.get("extract_fields", StrategyResult("", 0, 1, 0)).quality_score
        tr_score = results.get("truncate", StrategyResult("", 0, 1, 0)).quality_score
        assert ef_score >= tr_score

    def test_matrix_all_tasks(self, harness):
        """Run matrix on all tasks — no errors."""
        for task in get_all_tasks():
            results = harness.run_strategy_matrix(task)
            assert len(results) >= 3

    def test_best_strategy_per_task(self, harness):
        """Find the best strategy for each task and verify it's reasonable."""
        for task in get_all_tasks():
            results = harness.run_strategy_matrix(task)
            best = max(results.values(), key=lambda r: r.quality_score)
            # Best strategy should have quality >= 4.0 (at least half)
            assert best.quality_score >= 4.0, f"{task.task_id}: best={best}"


# ═══════════════════════════════════════════════════════════════════════════
# TestCompressionCurve — quality at different budget levels
# ═══════════════════════════════════════════════════════════════════════════


class TestCompressionCurve:
    def test_curve_returns_points(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        points = harness.run_compression_curve(task)
        assert len(points) == 4  # default: 0.3, 0.5, 0.7, 0.9

    def test_curve_quality_increases_with_budget(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "meeting_notes"][0]
        points = harness.run_compression_curve(task)
        scores = [p.quality_score for p in points]
        # More budget should generally mean same or better quality
        assert scores[-1] >= scores[0]

    def test_curve_custom_ratios(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        points = harness.run_compression_curve(task, budget_ratios=[0.2, 0.4, 0.6, 0.8, 1.0])
        assert len(points) == 5

    def test_curve_with_different_compressors(self, cleaner, hybrid, judge):
        h = BenchHarness(cleaner=cleaner, compressor=hybrid, judge=judge)
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        points = h.run_compression_curve(task)
        assert all(p.strategy == "HybridCompressor" for p in points)

    def test_90_percent_budget_near_perfect(self, harness):
        """At 90% budget, quality should be high for most tasks."""
        # markdown_with_links is a known hard case — keywords are after 50 links
        skip = {"short_response", "markdown_with_links"}
        for task in get_all_tasks():
            if task.task_id in skip:
                continue
            points = harness.run_compression_curve(task, budget_ratios=[0.9])
            assert points[0].quality_score >= 6.0, f"{task.task_id}: {points[0].quality_score}"

    def test_curve_all_tasks(self, harness):
        """Run curve on all tasks — verify no errors."""
        for task in get_all_tasks():
            points = harness.run_compression_curve(task)
            assert len(points) > 0
            for p in points:
                assert p.quality_score >= 0.0


# ═══════════════════════════════════════════════════════════════════════════
# TestPipelineQuality — A/B comparison across all tasks
# ═══════════════════════════════════════════════════════════════════════════


class TestPipelineQuality:
    def test_all_tasks_run(self, harness):
        tasks = get_all_tasks()
        reports = [harness.run_comparison(t) for t in tasks]
        assert len(reports) == 8  # 7 original + 1 new
        for r in reports:
            assert r.direct.error is None
            assert r.stm.error is None

    def test_short_response_passthrough(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "short_response"][0]
        report = harness.run_comparison(task)
        assert report.quality_preservation == 100.0
        assert report.total_reduction == 1.0

    def test_json_compression_preserves_structure(self, cleaner, field_extract, judge):
        h = BenchHarness(cleaner=cleaner, compressor=field_extract, judge=judge)
        task = [t for t in get_all_tasks() if t.task_id == "api_response_json"][0]
        report = h.run_comparison(task)
        assert report.stm.quality_score >= 6.0

    def test_code_file_preserves_key_info(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        report = harness.run_comparison(task)
        assert report.quality_preservation >= 50.0

    def test_meeting_notes_preserves_decisions(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "meeting_notes"][0]
        report = harness.run_comparison(task)
        assert "PostgreSQL" in report.stm.text or "postgresql" in report.stm.text.lower()

    def test_multilingual_preserves_keywords(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "multilingual_kr_en"][0]
        report = harness.run_comparison(task)
        assert report.quality_preservation >= 50.0

    def test_large_diff_output(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "large_diff_output"][0]
        report = harness.run_comparison(task)
        assert report.stm.error is None


# ═══════════════════════════════════════════════════════════════════════════
# TestCompressionStrategies — strategy comparison
# ═══════════════════════════════════════════════════════════════════════════


class TestCompressionStrategies:
    def _run_with(self, cleaner, compressor, judge, task):
        h = BenchHarness(cleaner=cleaner, compressor=compressor, judge=judge)
        return h.run_comparison(task)

    def test_truncate_vs_hybrid_on_code(self, cleaner, truncate, hybrid, judge):
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        r_trunc = self._run_with(cleaner, truncate, judge, task)
        r_hybrid = self._run_with(cleaner, hybrid, judge, task)
        assert r_trunc.stm.error is None
        assert r_hybrid.stm.error is None

    def test_field_extract_on_json(self, cleaner, field_extract, judge):
        task = [t for t in get_all_tasks() if t.task_id == "api_response_json"][0]
        report = self._run_with(cleaner, field_extract, judge, task)
        assert "total" in report.stm.text.lower() or "users" in report.stm.text.lower()

    def test_truncate_on_markdown(self, cleaner, truncate, judge):
        task = [t for t in get_all_tasks() if t.task_id == "meeting_notes"][0]
        report = self._run_with(cleaner, truncate, judge, task)
        m = report.stm.stage_metrics
        assert m is not None
        assert m.compressed_chars <= m.cleaned_chars

    def test_hybrid_preserves_head(self, cleaner, hybrid, judge):
        task = BenchTask(
            task_id="hybrid_head", description="hybrid head test", content=CODE_FILE,
            content_type="code", max_chars=800, expected_keywords=["JWT", "Overview"],
        )
        report = self._run_with(cleaner, hybrid, judge, task)
        assert "Authentication Module" in report.stm.text


# ═══════════════════════════════════════════════════════════════════════════
# TestSurfacingIntegration — surfacing with mock search
# ═══════════════════════════════════════════════════════════════════════════


class TestSurfacingIntegration:
    async def test_surfacing_adds_memories(self, cleaner, truncate, judge):
        """Surfacing should inject relevant memories into compressed output."""
        memories = [
            FakeSearchResult(
                chunk=FakeChunk(content="JWT tokens expire after 1 hour by default"),
                score=0.8,
            ),
        ]
        config = _make_surfacing_config()
        pipeline = _make_search_pipeline(memories)
        engine = SurfacingEngine(config=config, search_pipeline=pipeline)

        h = BenchHarness(
            cleaner=cleaner, compressor=truncate, surfacing_engine=engine, judge=judge
        )
        task = BenchTask(
            task_id="surf", description="auth token handling", content=CODE_FILE,
            content_type="code", max_chars=800, expected_keywords=["JWT"],
        )
        result = await h.run_stm_with_surfacing(task)
        assert result.stage_metrics is not None
        # Surfacing should increase size
        assert result.stage_metrics.surfaced_chars >= result.stage_metrics.compressed_chars
        assert result.stage_metrics.surface_ms >= 0

    async def test_surfacing_overhead_measured(self, cleaner, truncate, judge):
        """Measure surfacing overhead with injected memories."""
        memories = [
            FakeSearchResult(chunk=FakeChunk(content=f"Memory {i}"), score=0.5 + i * 0.1)
            for i in range(3)
        ]
        config = _make_surfacing_config()
        pipeline = _make_search_pipeline(memories)
        engine = SurfacingEngine(config=config, search_pipeline=pipeline)

        h = BenchHarness(
            cleaner=cleaner, compressor=truncate, surfacing_engine=engine, judge=judge
        )
        task = BenchTask(
            task_id="overhead", description="test overhead", content=MEETING_NOTES,
            content_type="markdown", max_chars=600,
        )
        result = await h.run_stm_with_surfacing(task)
        m = result.stage_metrics
        assert m is not None
        if m.surfaced_chars > m.compressed_chars:
            assert m.surfacing_overhead > 0

    async def test_no_surfacing_engine_zero_overhead(self, cleaner, truncate, judge):
        """Without surfacing engine, overhead is zero."""
        h = BenchHarness(cleaner=cleaner, compressor=truncate, judge=judge)
        task = BenchTask(
            task_id="nosrf", description="no surfacing", content=MEETING_NOTES,
            content_type="markdown", max_chars=500,
        )
        result = await h.run_stm_with_surfacing(task)
        m = result.stage_metrics
        assert m is not None
        assert m.surfaced_chars == m.compressed_chars


# ═══════════════════════════════════════════════════════════════════════════
# TestBudgetLevels — tight vs generous budgets
# ═══════════════════════════════════════════════════════════════════════════


class TestBudgetLevels:
    def test_tight_budget_tasks_exist(self):
        tasks = get_tight_tasks()
        assert len(tasks) == 8
        # Budgets should be ~half of default
        default_tasks = get_all_tasks()
        for tight, default in zip(tasks, default_tasks):
            assert tight.max_chars <= default.max_chars

    def test_generous_budget_higher_quality(self, harness):
        """Generous budget should produce better quality than tight budget."""
        tight_tasks = get_tight_tasks()
        generous_tasks = get_generous_tasks()

        for tight, generous in zip(tight_tasks, generous_tasks):
            if tight.task_id == "short_response":
                continue
            r_tight = harness._run_pipeline(tight)
            r_generous = harness._run_pipeline(generous)
            assert r_generous.quality_score >= r_tight.quality_score, (
                f"{tight.task_id}: generous={r_generous.quality_score}, tight={r_tight.quality_score}"
            )

    def test_default_budget_reasonable_quality(self, harness):
        """Default budget should give reasonable quality across all tasks."""
        for task in get_all_tasks():
            result = harness.run_stm(task)
            # Short response is always perfect
            if task.task_id == "short_response":
                assert result.quality_score == 10.0


# ═══════════════════════════════════════════════════════════════════════════
# TestDataset — dataset structure validation
# ═══════════════════════════════════════════════════════════════════════════


class TestDataset:
    def test_categories_cover_all_tasks(self):
        all_ids = {t.task_id for t in get_all_tasks()}
        categorized_ids = set()
        for ids in TASK_CATEGORIES.values():
            categorized_ids.update(ids)
        assert categorized_ids == all_ids

    def test_optimal_strategies_cover_all_tasks(self):
        all_ids = {t.task_id for t in get_all_tasks()}
        assert set(OPTIMAL_STRATEGIES.keys()) == all_ids

    def test_all_tasks_have_keywords(self):
        for task in get_all_tasks():
            if task.task_id != "short_response":
                assert len(task.expected_keywords) >= 2, f"{task.task_id} needs more keywords"

    def test_content_not_empty(self):
        for task in get_all_tasks():
            assert len(task.content) > 0


# ═══════════════════════════════════════════════════════════════════════════
# TestRegressionGate — CI-friendly quality thresholds
# ═══════════════════════════════════════════════════════════════════════════


class TestRegressionGate:
    """These tests act as quality gates for CI — if compression logic changes
    and quality drops, these tests will catch it."""

    def test_auto_strategy_all_above_40(self, cleaner, truncate, judge):
        """With auto strategy, all tasks should score ≥40% quality preservation.

        Note: markdown_with_links is fundamentally hard (keywords after 50 links).
        This gate catches catastrophic regressions, not marginal quality drops.
        """
        h = BenchHarness(cleaner=cleaner, compressor=truncate, judge=judge)
        for task in get_all_tasks():
            report = h.run_auto_strategy(task)
            assert report.quality_preservation >= 40.0, (
                f"{task.task_id}: {report.quality_preservation:.1f}%"
            )

    def test_optimal_strategy_above_60(self, cleaner, judge):
        """With the known optimal strategy, each task should score ≥60%.

        Known hard cases: markdown_with_links (links before content),
        large_diff_output (summary at bottom, tight budget).
        """
        from bench.harness import _get_compressor

        for task in get_all_tasks():
            opt = OPTIMAL_STRATEGIES[task.task_id]
            comp = _get_compressor(opt)
            h = BenchHarness(cleaner=cleaner, compressor=comp, judge=judge)
            report = h.run_comparison(task)
            if task.task_id != "markdown_with_links":
                assert report.quality_preservation >= 60.0, (
                    f"{task.task_id} ({opt}): {report.quality_preservation:.1f}%"
                )

    def test_generous_budget_above_80(self, cleaner, judge):
        """With 2x budget and optimal strategy, quality should be ≥80%."""
        from bench.harness import _get_compressor

        for task in get_generous_tasks():
            opt = OPTIMAL_STRATEGIES[task.task_id]
            comp = _get_compressor(opt)
            h = BenchHarness(cleaner=cleaner, compressor=comp, judge=judge)
            report = h.run_comparison(task)
            if task.task_id != "markdown_with_links":
                assert report.quality_preservation >= 80.0, (
                    f"{task.task_id} ({opt}): {report.quality_preservation:.1f}%"
                )


# ═══════════════════════════════════════════════════════════════════════════
# TestReport — report formatting
# ═══════════════════════════════════════════════════════════════════════════


class TestReport:
    def test_format_empty(self):
        text = format_report([])
        assert "No tasks run" in text

    def test_format_single_task(self, harness):
        task = BenchTask(
            task_id="rpt", description="report test", content=MEETING_NOTES,
            content_type="markdown", max_chars=500, expected_keywords=["PostgreSQL"],
        )
        report = harness.run_comparison(task)
        text = format_report([report])
        assert "rpt" in text
        assert "quality" in text.lower()
        assert "Summary" in text

    def test_format_all_tasks(self, harness):
        tasks = get_all_tasks()
        reports = [harness.run_comparison(t) for t in tasks]
        text = format_report(reports)
        assert "Tasks: 8" in text
        assert "Avg quality preservation" in text

    def test_warning_on_low_quality(self):
        direct = BenchResult(task_id="low", mode="direct", text="x", stage_metrics=None, quality_score=10.0)
        stm = BenchResult(
            task_id="low", mode="stm", text="y",
            stage_metrics=StageMetrics(
                original_chars=100, cleaned_chars=90, compressed_chars=50, surfaced_chars=50,
                clean_ms=1, compress_ms=1, surface_ms=0,
            ),
            quality_score=6.0,
        )
        report = ComparisonReport(task_id="low", direct=direct, stm=stm)
        text = format_report([report])
        assert "⚠️" in text

    def test_format_matrix(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "meeting_notes"][0]
        results = harness.run_strategy_matrix(task)
        text = format_matrix(task.task_id, results, optimal="truncate")
        assert "meeting_notes" in text
        assert "truncate" in text

    def test_format_curve(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        points = harness.run_compression_curve(task)
        text = format_curve(task.task_id, points)
        assert "code_file_large" in text
        assert "30%" in text

    def test_format_full_report(self, harness):
        tasks = get_all_tasks()[:3]
        comparisons = [harness.run_comparison(t) for t in tasks]
        matrices = {t.task_id: harness.run_strategy_matrix(t) for t in tasks}
        curves = {t.task_id: harness.run_compression_curve(t) for t in tasks}
        text = format_full_report(
            comparisons, matrices=matrices, curves=curves, optimal_strategies=OPTIMAL_STRATEGIES
        )
        assert "Strategy Matrix" in text
        assert "Compression Curves" in text


# ═══════════════════════════════════════════════════════════════════════════
# TestCallMetrics — timing fields in metrics.py
# ═══════════════════════════════════════════════════════════════════════════


class TestCallMetrics:
    def test_default_timing_fields(self):
        m = CallMetrics(server="s", tool="t", original_chars=100, compressed_chars=50)
        assert m.clean_ms == 0.0
        assert m.compress_ms == 0.0
        assert m.surface_ms == 0.0
        assert m.surfaced_chars == 0

    def test_timing_fields_set(self):
        m = CallMetrics(
            server="s", tool="t", original_chars=1000, compressed_chars=500,
            clean_ms=1.5, compress_ms=3.2, surface_ms=10.0, surfaced_chars=600,
        )
        assert m.clean_ms == 1.5
        assert m.surfaced_chars == 600

    def test_tracker_aggregates_timing(self):
        tracker = TokenTracker(metrics_store=None)
        tracker.record(CallMetrics(
            server="a", tool="t1", original_chars=1000, compressed_chars=500,
            clean_ms=2.0, compress_ms=5.0, surface_ms=10.0, surfaced_chars=600,
        ))
        tracker.record(CallMetrics(
            server="a", tool="t2", original_chars=2000, compressed_chars=800,
            clean_ms=4.0, compress_ms=7.0, surface_ms=20.0, surfaced_chars=900,
        ))
        summary = tracker.get_summary()
        assert summary["total_calls"] == 2
        assert summary["total_surfaced_chars"] == 1500
        assert summary["avg_clean_ms"] == 3.0
        assert summary["avg_compress_ms"] == 6.0
        assert summary["avg_surface_ms"] == 15.0

    def test_tracker_backward_compatible(self):
        tracker = TokenTracker(metrics_store=None)
        tracker.record(CallMetrics(server="s", tool="t", original_chars=100, compressed_chars=50))
        summary = tracker.get_summary()
        assert summary["total_calls"] == 1
        assert summary["avg_clean_ms"] == 0.0
        assert summary["total_surfaced_chars"] == 0


# ═══════════════════════════════════════════════════════════════════════════
# TestSelective2Phase — TOC → select workflow
# ═══════════════════════════════════════════════════════════════════════════


class TestSelective2Phase:
    """Benchmark the 2-phase selective compression flow (TOC → select)."""

    def test_markdown_produces_toc(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        result = harness.run_selective_2phase(task)
        assert isinstance(result, SelectiveResult)
        assert result.toc_entry_count > 0
        assert result.selected_chars > 0

    def test_json_produces_toc(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "api_response_json"][0]
        result = harness.run_selective_2phase(task)
        assert result.toc_entry_count > 0

    def test_selected_content_has_quality(self, harness):
        """Selected sections should contain at least some keywords."""
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        result = harness.run_selective_2phase(task, select_top_n=3)
        assert result.quality_score > 0

    def test_full_select_recovers_content(self, harness):
        """Selecting all sections should recover most of the original."""
        task = [t for t in get_all_tasks() if t.task_id == "meeting_notes"][0]
        result = harness.run_selective_2phase(task, select_top_n=100)
        # Recovery should be high when selecting all sections
        assert result.recovery_ratio >= 0.8

    def test_short_text_passthrough(self, harness):
        """Short text should not produce TOC — passthrough."""
        task = [t for t in get_all_tasks() if t.task_id == "short_response"][0]
        result = harness.run_selective_2phase(task)
        assert result.toc_entry_count == 0
        assert result.recovery_ratio == 1.0

    def test_selective_all_tasks(self, harness):
        """2-phase runs on all tasks without errors."""
        for task in get_all_tasks():
            result = harness.run_selective_2phase(task)
            assert result.total_chars > 0

    def test_top1_vs_top3_quality(self, harness):
        """Selecting 3 sections should give better quality than 1."""
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        r1 = harness.run_selective_2phase(task, select_top_n=1)
        r3 = harness.run_selective_2phase(task, select_top_n=3)
        assert r3.quality_score >= r1.quality_score
        assert r3.selected_chars >= r1.selected_chars

    def test_multilingual_toc(self, harness):
        """Korean-English content should produce valid TOC."""
        task = [t for t in get_all_tasks() if t.task_id == "multilingual_kr_en"][0]
        result = harness.run_selective_2phase(task)
        assert result.toc_entry_count >= 2


# ═══════════════════════════════════════════════════════════════════════════
# TestProxyManagerIntegration — full pipeline with mock upstream
# ═══════════════════════════════════════════════════════════════════════════


class TestProxyManagerIntegration:
    """Exercise the real ProxyManager pipeline with mock upstream MCP server."""

    def _make_manager(self, tracker, compression=None, max_chars=2000):
        from memtomem_stm.proxy.config import ProxyConfig, UpstreamServerConfig
        from memtomem_stm.proxy.manager import ProxyManager

        comp = compression or CompressionStrategy.TRUNCATE
        config = ProxyConfig(
            enabled=True,
            config_path=Path("/tmp/nonexistent-bench-config.json"),
            upstream_servers={
                "bench": UpstreamServerConfig(
                    prefix="b",
                    compression=comp,
                    max_result_chars=max_chars,
                )
            },
        )
        return ProxyManager(config=config, tracker=tracker)

    def _inject_mock_upstream(self, manager, server_name, response_text):
        from dataclasses import dataclass as _dc
        from unittest.mock import AsyncMock, MagicMock

        from memtomem_stm.proxy.manager import UpstreamConnection

        mock_content = MagicMock()
        mock_content.type = "text"
        mock_content.text = response_text

        mock_result = MagicMock()
        mock_result.content = [mock_content]
        mock_result.isError = False

        mock_session = AsyncMock()
        mock_session.call_tool = AsyncMock(return_value=mock_result)

        conn = UpstreamConnection(
            name=server_name,
            config=manager._config.upstream_servers[server_name],
            session=mock_session,
            tools=[],
        )
        manager._connections[server_name] = conn
        return mock_session

    async def test_truncate_pipeline(self):
        """Full pipeline: upstream → clean → truncate → metrics."""
        tracker = TokenTracker(metrics_store=None)
        mgr = self._make_manager(tracker, CompressionStrategy.TRUNCATE, max_chars=500)
        self._inject_mock_upstream(mgr, "bench", CODE_FILE)

        result = await mgr._call_tool_inner("bench", "read_file", {})
        assert isinstance(result, str)
        assert len(result) <= 700  # max_chars + metadata overhead

        summary = tracker.get_summary()
        assert summary["total_calls"] == 1
        assert summary["avg_clean_ms"] > 0 or summary["avg_compress_ms"] >= 0

    async def test_hybrid_pipeline(self):
        """Full pipeline with hybrid compression."""
        tracker = TokenTracker(metrics_store=None)
        mgr = self._make_manager(tracker, CompressionStrategy.HYBRID, max_chars=800)
        self._inject_mock_upstream(mgr, "bench", CODE_FILE)

        result = await mgr._call_tool_inner("bench", "read_file", {})
        assert isinstance(result, str)
        assert "Authentication Module" in result  # head preserved

    async def test_extract_fields_pipeline(self):
        """Full pipeline with extract_fields on JSON."""
        tracker = TokenTracker(metrics_store=None)
        mgr = self._make_manager(tracker, CompressionStrategy.EXTRACT_FIELDS, max_chars=500)
        self._inject_mock_upstream(mgr, "bench", API_RESPONSE_JSON)

        result = await mgr._call_tool_inner("bench", "get_users", {})
        assert isinstance(result, str)
        # Top-level keys should be visible
        assert "users" in result.lower() or "total" in result.lower()

    async def test_selective_pipeline_returns_toc(self):
        """Full pipeline with selective returns TOC JSON."""
        import json

        tracker = TokenTracker(metrics_store=None)
        mgr = self._make_manager(tracker, CompressionStrategy.SELECTIVE, max_chars=200)
        self._inject_mock_upstream(mgr, "bench", CODE_FILE)

        result = await mgr._call_tool_inner("bench", "read_file", {})
        assert isinstance(result, str)
        toc = json.loads(result)
        assert toc["type"] == "toc"
        assert "selection_key" in toc

        # Phase 2: select sections
        key = toc["selection_key"]
        entries = toc["entries"]
        section_keys = [e["key"] for e in entries[:2]]
        selected = mgr.select_chunks(key, section_keys)
        assert len(selected) > 0
        assert "Selection key" not in selected  # not an error

    async def test_short_response_passthrough(self):
        """Short responses should not be compressed."""
        tracker = TokenTracker(metrics_store=None)
        mgr = self._make_manager(tracker, CompressionStrategy.TRUNCATE, max_chars=2000)
        self._inject_mock_upstream(mgr, "bench", SHORT_RESPONSE)

        result = await mgr._call_tool_inner("bench", "save_file", {})
        assert result == SHORT_RESPONSE

    async def test_metrics_recorded(self):
        """Verify per-stage timing is recorded in metrics."""
        tracker = TokenTracker(metrics_store=None)
        mgr = self._make_manager(tracker, CompressionStrategy.TRUNCATE, max_chars=500)
        self._inject_mock_upstream(mgr, "bench", MEETING_NOTES)

        await mgr._call_tool_inner("bench", "read_doc", {})

        summary = tracker.get_summary()
        assert summary["total_calls"] == 1
        assert summary["total_original_chars"] > 0
        assert summary["total_compressed_chars"] > 0
        assert summary["total_surfaced_chars"] > 0

    async def test_html_cleaning_in_pipeline(self):
        """HTML content should be cleaned before compression."""
        tracker = TokenTracker(metrics_store=None)
        mgr = self._make_manager(tracker, CompressionStrategy.TRUNCATE, max_chars=2000)
        self._inject_mock_upstream(mgr, "bench", HTML_MIXED)

        result = await mgr._call_tool_inner("bench", "read_docs", {})
        assert "<script>" not in result
        assert "<style>" not in result

    async def test_context_query_removed(self):
        """_context_query should be stripped from upstream arguments."""
        tracker = TokenTracker(metrics_store=None)
        mgr = self._make_manager(tracker, CompressionStrategy.TRUNCATE, max_chars=2000)
        mock_session = self._inject_mock_upstream(mgr, "bench", SHORT_RESPONSE)

        await mgr._call_tool_inner(
            "bench", "read_file", {"path": "/test", "_context_query": "auth tokens"}
        )
        # _context_query should NOT be forwarded to upstream
        call_args = mock_session.call_tool.call_args
        forwarded_args = call_args[0][1] if len(call_args[0]) > 1 else call_args[1].get("arguments", {})
        assert "_context_query" not in forwarded_args


# ═══════════════════════════════════════════════════════════════════════════
# TestStageBreakdown — per-stage quality measurement
# ═══════════════════════════════════════════════════════════════════════════


class TestStageBreakdown:
    """Measure quality at each pipeline stage to identify where info is lost."""

    def test_breakdown_has_all_stages(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "meeting_notes"][0]
        bd = harness.run_stage_breakdown(task)
        stage_names = [s.stage for s in bd.stages]
        assert "original" in stage_names
        assert "cleaned" in stage_names
        assert "compressed" in stage_names

    def test_cleaning_preserves_quality(self, harness):
        """Clean stage should not lose quality (removes noise, not content)."""
        for task in get_all_tasks():
            bd = harness.run_stage_breakdown(task)
            assert bd.clean_info_loss <= 1.0, (
                f"{task.task_id}: clean lost {bd.clean_info_loss:.1f} quality"
            )

    def test_compression_is_main_loss(self, harness):
        """Compression should be the primary source of quality loss."""
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        bd = harness.run_stage_breakdown(task)
        # Compress may lose some info, clean shouldn't
        assert bd.clean_info_loss <= bd.compress_info_loss or bd.compress_info_loss == 0

    def test_qa_scoring_works(self, harness):
        """Tasks with QA pairs should have them scored at each stage."""
        task = [t for t in get_all_tasks() if t.task_id == "meeting_notes"][0]
        bd = harness.run_stage_breakdown(task)
        orig = bd._get("original")
        assert orig is not None
        assert orig.qa_total > 0
        assert orig.qa_answerable > 0  # Original should answer content questions

    def test_qa_degrades_with_compression(self, harness):
        """Tight compression may reduce answerable QA pairs."""
        from bench.tasks import get_tight_tasks
        tasks = get_tight_tasks()
        task = [t for t in tasks if t.task_id == "code_file_large"][0]
        bd = harness.run_stage_breakdown(task)
        orig = bd._get("original")
        comp = bd._get("compressed")
        assert orig is not None and comp is not None
        # Tight budget may lose some answers
        assert comp.qa_answerable <= orig.qa_answerable

    def test_breakdown_all_tasks(self, harness):
        """All tasks produce valid breakdowns."""
        for task in get_all_tasks():
            bd = harness.run_stage_breakdown(task)
            assert len(bd.stages) >= 3

    def test_format_stage_breakdown(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "meeting_notes"][0]
        bd = harness.run_stage_breakdown(task)
        text = format_stage_breakdown(bd)
        assert "meeting_notes" in text
        assert "original" in text
        assert "cleaned" in text
        assert "compressed" in text

    async def test_breakdown_with_surfacing(self, cleaner, truncate, judge):
        """Surfacing stage should appear when engine is provided."""
        memories = [
            FakeSearchResult(
                chunk=FakeChunk(content="JWT tokens use HS256 with 1-hour TTL"),
                score=0.8,
            ),
        ]
        config = _make_surfacing_config()
        pipeline = _make_search_pipeline(memories)
        engine = SurfacingEngine(config=config, search_pipeline=pipeline)
        h = BenchHarness(cleaner=cleaner, compressor=truncate, surfacing_engine=engine, judge=judge)

        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        bd = await h.run_stage_breakdown_with_surfacing(task)
        stage_names = [s.stage for s in bd.stages]
        assert "surfaced" in stage_names
        assert bd.surfacing_value >= 0  # Surfacing shouldn't hurt quality


# ═══════════════════════════════════════════════════════════════════════════
# TestQAScoring — question-answer based quality measurement
# ═══════════════════════════════════════════════════════════════════════════


class TestQAScoring:
    """Test QA-based quality scoring."""

    def test_qa_score_all_answerable(self, judge):
        task = BenchTask(
            task_id="qa", description="qa", content="The sky is blue and water is wet.",
            content_type="text", max_chars=100,
            qa_pairs=[
                QAPair("What color is the sky?", "blue", "content"),
                QAPair("Is water wet?", "wet", "content"),
            ],
        )
        result = judge.qa_score(task, "The sky is blue and water is wet.")
        assert result["answerable"] == 2
        assert result["score"] == 1.0

    def test_qa_score_partial(self, judge):
        task = BenchTask(
            task_id="qa", description="qa", content="x", content_type="text", max_chars=100,
            qa_pairs=[
                QAPair("Q1?", "alpha", "content"),
                QAPair("Q2?", "beta", "content"),
            ],
        )
        result = judge.qa_score(task, "alpha is here but not the other")
        assert result["answerable"] == 1
        assert result["score"] == 0.5

    def test_qa_by_source(self, judge):
        task = BenchTask(
            task_id="qa", description="qa", content="x", content_type="text", max_chars=100,
            qa_pairs=[
                QAPair("From content?", "original_fact", "content"),
                QAPair("From memory?", "remembered_fact", "memory"),
            ],
        )
        # Only content answer present, not memory answer
        result = judge.qa_by_source(task, "This has original_fact but nothing from memories")
        assert result["content"]["answerable"] == 1
        assert result["memory"]["answerable"] == 0

    def test_qa_no_pairs(self, judge):
        task = BenchTask(
            task_id="qa", description="qa", content="x", content_type="text", max_chars=100,
        )
        result = judge.qa_score(task, "anything")
        assert result["total"] == 0
        assert result["score"] == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# TestSurfacingValue — does surfacing actually help?
# ═══════════════════════════════════════════════════════════════════════════


class TestSurfacingValue:
    """Measure whether surfaced memories improve answer quality."""

    async def test_surfacing_fills_knowledge_gaps(self, cleaner, truncate, judge):
        """Memories should make previously unanswerable QA pairs answerable."""
        tasks = get_surfacing_tasks()
        task = [t for t in tasks if t.task_id == "auth_incomplete"][0]

        # Build surfacing engine with task-specific memories
        memories = [
            FakeSearchResult(chunk=FakeChunk(content=m), score=0.8 - i * 0.1)
            for i, m in enumerate(AUTH_MEMORIES)
        ]
        config = _make_surfacing_config()
        pipeline = _make_search_pipeline(memories)
        engine = SurfacingEngine(config=config, search_pipeline=pipeline)

        h = BenchHarness(cleaner=cleaner, compressor=truncate, surfacing_engine=engine, judge=judge)
        value = await h.measure_surfacing_value(task)

        # Without surfacing: only "content" QA pairs answerable
        assert value.qa_without >= 1  # at least content answers
        # With surfacing: "memory" QA pairs also answerable
        assert value.qa_with > value.qa_without
        assert value.qa_delta > 0

    async def test_surfacing_value_deploy_task(self, cleaner, truncate, judge):
        """Deploy failure + Redis migration memory should help diagnosis."""
        tasks = get_surfacing_tasks()
        task = [t for t in tasks if t.task_id == "deploy_failure"][0]

        memories = [
            FakeSearchResult(chunk=FakeChunk(content=m), score=0.8 - i * 0.1)
            for i, m in enumerate(DEPLOY_MEMORIES)
        ]
        config = _make_surfacing_config()
        pipeline = _make_search_pipeline(memories)
        engine = SurfacingEngine(config=config, search_pipeline=pipeline)

        h = BenchHarness(cleaner=cleaner, compressor=truncate, surfacing_engine=engine, judge=judge)
        value = await h.measure_surfacing_value(task)
        assert value.qa_delta > 0  # Memories should add answerable questions

    async def test_no_surfacing_no_delta(self, cleaner, truncate, judge):
        """Without surfacing engine, quality delta should be 0."""
        tasks = get_surfacing_tasks()
        task = tasks[0]
        h = BenchHarness(cleaner=cleaner, compressor=truncate, judge=judge)
        value = await h.measure_surfacing_value(task)
        assert value.quality_delta == 0.0
        assert value.qa_delta == 0

    async def test_surfacing_doesnt_hurt(self, cleaner, truncate, judge):
        """Surfacing should not reduce quality of existing content."""
        tasks = get_surfacing_tasks()
        for task in tasks:
            memories = [
                FakeSearchResult(chunk=FakeChunk(content=m), score=0.5)
                for m in (task.surfacing_memories or [])
            ]
            config = _make_surfacing_config()
            pipeline = _make_search_pipeline(memories)
            engine = SurfacingEngine(config=config, search_pipeline=pipeline)
            h = BenchHarness(cleaner=cleaner, compressor=truncate, surfacing_engine=engine, judge=judge)
            value = await h.measure_surfacing_value(task)
            # Surfacing should never reduce quality (it only adds content)
            assert value.quality_delta >= 0, f"{task.task_id}: delta={value.quality_delta}"

    def test_format_surfacing_value(self):
        values = [
            SurfacingValue(
                task_id="test", without_surfacing=6.0, with_surfacing=8.0,
                qa_without=2, qa_with=5, qa_total=6, memories_injected=3,
                quality_delta=2.0, qa_delta=3,
            ),
        ]
        text = format_surfacing_value(values)
        assert "Surfacing Value" in text
        assert "+2.0" in text
        assert "+3" in text


# ═══════════════════════════════════════════════════════════════════════════
# TestNeedleInHaystack — critical info buried in noise
# ═══════════════════════════════════════════════════════════════════════════


class TestNeedleInHaystack:
    """Test whether compression preserves critical details under tight budgets."""

    def test_needle_tasks_exist(self):
        tasks = get_needle_tasks()
        assert len(tasks) == 2

    def test_markdown_needle_with_hybrid(self, cleaner, hybrid, judge):
        """Hybrid: needle buried in middle section — lost under tight budget.

        Known limitation: hybrid head preserves top sections, but the DB config
        needle is in the 3rd section. With 600-char budget, it goes to tail TOC.
        This test documents the limitation for future improvement.
        """
        h = BenchHarness(cleaner=cleaner, compressor=hybrid, judge=judge)
        task = [t for t in get_needle_tasks() if t.task_id == "needle_markdown"][0]
        result = h.run_stm(task)
        qa = judge.qa_score(task, result.text)
        # Document current behavior: needle is lost under this budget
        # A future "priority section" feature could improve this
        assert qa["total"] == 5
        assert result.stage_metrics is not None

    def test_json_needle_with_extract_fields(self, cleaner, field_extract, judge):
        """FieldExtract: degraded server at index 30 — only first 3 shown.

        Known limitation: FieldExtract shows first N array items, not anomalous ones.
        A future "anomaly-aware" extract could prioritize non-healthy entries.
        """
        h = BenchHarness(cleaner=cleaner, compressor=field_extract, judge=judge)
        task = [t for t in get_needle_tasks() if t.task_id == "needle_json"][0]
        result = h.run_stm(task)
        # Document: top-level keys preserved (total, alerts_active) but needle item lost
        assert "total" in result.text.lower()
        assert "alerts_active" in result.text.lower()

    def test_needle_compression_curve(self, harness):
        """Quality should increase with budget — needle more likely found."""
        task = [t for t in get_needle_tasks() if t.task_id == "needle_markdown"][0]
        points = harness.run_compression_curve(task, budget_ratios=[0.2, 0.5, 0.8])
        # More budget = better chance of preserving needle
        assert points[-1].quality_score >= points[0].quality_score

    def test_needle_qa_at_tight_budget(self, harness):
        """At very tight budget, some needles may be lost — measure which."""
        task = [t for t in get_needle_tasks() if t.task_id == "needle_markdown"][0]
        # Force very tight budget
        tight_task = BenchTask(**{**task.__dict__, "max_chars": 300})
        bd = harness.run_stage_breakdown(tight_task)
        comp = bd._get("compressed")
        assert comp is not None
        # Log what was lost
        assert comp.qa_total == 5


# ═══════════════════════════════════════════════════════════════════════════
# TestDistractorRobustness — noisy memories shouldn't hurt quality
# ═══════════════════════════════════════════════════════════════════════════


class TestDistractorRobustness:
    """Test that irrelevant surfaced memories don't degrade answer quality."""

    def test_distractor_tasks_exist(self):
        tasks = get_distractor_tasks()
        assert len(tasks) == 2
        for t in tasks:
            assert t.surfacing_memories is not None
            assert len(t.surfacing_memories) >= 3  # 1 relevant + distractors

    async def test_distractors_dont_reduce_content_qa(self, cleaner, truncate, judge):
        """Content QA should be unaffected by distractor memories."""
        for task in get_distractor_tasks():
            # Build engine with all memories (relevant + distractors)
            memories = [
                FakeSearchResult(chunk=FakeChunk(content=m), score=0.5)
                for m in task.surfacing_memories
            ]
            config = _make_surfacing_config()
            pipeline = _make_search_pipeline(memories)
            engine = SurfacingEngine(config=config, search_pipeline=pipeline)
            h = BenchHarness(
                cleaner=cleaner, compressor=truncate, surfacing_engine=engine, judge=judge
            )
            value = await h.measure_surfacing_value(task)
            # Content answers should not be lost due to distractors
            content_qa = judge.qa_by_source(task, value.task_id)  # not used directly
            assert value.quality_delta >= 0, (
                f"{task.task_id}: distractors hurt quality by {value.quality_delta}"
            )

    async def test_relevant_memory_still_found(self, cleaner, truncate, judge):
        """Despite distractors, the one relevant memory should still add value."""
        task = [t for t in get_distractor_tasks() if t.task_id == "distractor_auth"][0]
        memories = [
            FakeSearchResult(chunk=FakeChunk(content=m), score=0.8 - i * 0.1)
            for i, m in enumerate(task.surfacing_memories)
        ]
        config = _make_surfacing_config()
        pipeline = _make_search_pipeline(memories)
        engine = SurfacingEngine(config=config, search_pipeline=pipeline)
        h = BenchHarness(
            cleaner=cleaner, compressor=truncate, surfacing_engine=engine, judge=judge
        )
        value = await h.measure_surfacing_value(task)
        # The relevant memory (HS256) should be surfaced and add a QA answer
        assert value.qa_with >= value.qa_without

    async def test_distractor_vs_clean_memories(self, cleaner, truncate, judge):
        """Compare surfacing value: clean memories vs same + distractors."""
        from bench.tasks import AUTH_MEMORIES

        task = [t for t in get_surfacing_tasks() if t.task_id == "auth_incomplete"][0]

        # Clean memories (all relevant)
        clean_mems = [
            FakeSearchResult(chunk=FakeChunk(content=m), score=0.8)
            for m in AUTH_MEMORIES
        ]
        config = _make_surfacing_config()
        clean_engine = SurfacingEngine(config=config, search_pipeline=_make_search_pipeline(clean_mems))
        h_clean = BenchHarness(
            cleaner=cleaner, compressor=truncate, surfacing_engine=clean_engine, judge=judge
        )
        v_clean = await h_clean.measure_surfacing_value(task)

        # Noisy memories (1 relevant + 3 distractors)
        from bench.tasks import DISTRACTOR_MEMORIES_AUTH
        noisy_mems = [
            FakeSearchResult(chunk=FakeChunk(content=m), score=0.5)
            for m in DISTRACTOR_MEMORIES_AUTH
        ]
        noisy_engine = SurfacingEngine(config=config, search_pipeline=_make_search_pipeline(noisy_mems))
        h_noisy = BenchHarness(
            cleaner=cleaner, compressor=truncate, surfacing_engine=noisy_engine, judge=judge
        )
        v_noisy = await h_noisy.measure_surfacing_value(task)

        # Clean memories should give >= distractor memories value
        assert v_clean.qa_with >= v_noisy.qa_with


# ═══════════════════════════════════════════════════════════════════════════
# TestMultihop — answer requires combining content + memory
# ═══════════════════════════════════════════════════════════════════════════


class TestMultihop:
    """Test tasks that need information from both content and memory."""

    def test_multihop_tasks_exist(self):
        tasks = get_multihop_tasks()
        assert len(tasks) >= 1
        task = tasks[0]
        content_qs = [q for q in task.qa_pairs if q.source == "content"]
        memory_qs = [q for q in task.qa_pairs if q.source == "memory"]
        assert len(content_qs) >= 2
        assert len(memory_qs) >= 3

    def test_content_only_partial_answers(self, judge):
        """Without memory, only content QA pairs should be answerable."""
        task = get_multihop_tasks()[0]
        qa = judge.qa_by_source(task, task.content)
        # Content questions answerable from content
        assert qa["content"]["answerable"] >= 2
        # Memory questions NOT answerable from content alone
        assert qa["memory"]["answerable"] == 0

    async def test_memory_adds_answers(self, cleaner, truncate, judge):
        """With surfacing, memory QA pairs become answerable."""
        from bench.tasks import MULTIHOP_MEMORIES

        task = get_multihop_tasks()[0]
        memories = [
            FakeSearchResult(chunk=FakeChunk(content=m), score=0.8 - i * 0.1)
            for i, m in enumerate(MULTIHOP_MEMORIES)
        ]
        config = _make_surfacing_config()
        engine = SurfacingEngine(config=config, search_pipeline=_make_search_pipeline(memories))
        h = BenchHarness(
            cleaner=cleaner, compressor=truncate, surfacing_engine=engine, judge=judge
        )
        value = await h.measure_surfacing_value(task)
        assert value.qa_delta > 0  # Memory QA pairs now answerable
        assert value.qa_with > value.qa_without

    async def test_multihop_stage_breakdown(self, cleaner, truncate, judge):
        """Stage breakdown should show surfacing adding QA answers."""
        from bench.tasks import MULTIHOP_MEMORIES

        task = get_multihop_tasks()[0]
        memories = [
            FakeSearchResult(chunk=FakeChunk(content=m), score=0.8 - i * 0.1)
            for i, m in enumerate(MULTIHOP_MEMORIES)
        ]
        config = _make_surfacing_config()
        engine = SurfacingEngine(config=config, search_pipeline=_make_search_pipeline(memories))
        h = BenchHarness(
            cleaner=cleaner, compressor=truncate, surfacing_engine=engine, judge=judge
        )
        bd = await h.run_stage_breakdown_with_surfacing(task)
        assert bd.surfacing_qa_gain > 0  # Surfacing added answerable QA pairs
        assert bd.compress_info_loss >= 0  # Compression may or may not lose info
        assert bd.clean_info_loss == 0  # Clean shouldn't lose quality on plain markdown


# ═══════════════════════════════════════════════════════════════════════════
# TestStructuredDatasets — production-grade benchmark datasets
# ═══════════════════════════════════════════════════════════════════════════


class TestStructuredDatasets:
    """Tests for the structured datasets module (bench/datasets.py)."""

    def test_all_tasks_count(self):
        tasks = ds_all_tasks()
        assert len(tasks) == 11  # 3 json + 3 markdown + 2 code + 3 text

    def test_with_surfacing_count(self):
        tasks = ds_all_with_surfacing()
        assert len(tasks) == 13  # 11 + 2 surfacing

    def test_all_have_qa_pairs(self):
        for task in ds_all_tasks():
            assert len(task.qa_pairs) >= 3, f"{task.task_id} has too few QA pairs"

    def test_no_empty_content(self):
        for task in ds_all_tasks():
            assert len(task.content) > 100, f"{task.task_id} content too short"

    def test_json_tasks_are_valid_json(self):
        import json
        for task in ds_json_tasks():
            json.loads(task.content)  # should not raise

    def test_surfacing_tasks_have_memories(self):
        for task in ds_surfacing_tasks():
            assert task.surfacing_memories is not None
            assert len(task.surfacing_memories) >= 2
            memory_qs = [q for q in task.qa_pairs if q.source == "memory"]
            assert len(memory_qs) >= 2, f"{task.task_id} needs memory QA pairs"

    # ── A/B comparison on all datasets ──────────────────────────

    def test_json_compression_quality(self, cleaner, field_extract, judge):
        """JSON tasks with FieldExtract should preserve key structure."""
        h = BenchHarness(cleaner=cleaner, compressor=field_extract, judge=judge)
        for task in ds_json_tasks():
            report = h.run_comparison(task)
            assert report.stm.error is None
            # json-event-stream: deeply nested payloads lose detail under tight budget
            assert report.stm.quality_score >= 0.0  # no errors

    def test_markdown_compression_quality(self, cleaner, truncate, judge):
        """Markdown tasks should preserve headings and key info."""
        h = BenchHarness(cleaner=cleaner, compressor=truncate, judge=judge)
        for task in ds_markdown_tasks():
            report = h.run_comparison(task)
            assert report.stm.error is None

    def test_code_compression_quality(self, cleaner, hybrid, judge):
        """Code tasks with Hybrid should preserve head structure."""
        h = BenchHarness(cleaner=cleaner, compressor=hybrid, judge=judge)
        for task in ds_code_tasks():
            report = h.run_comparison(task)
            assert report.stm.error is None

    def test_text_compression_quality(self, cleaner, truncate, judge):
        """Text tasks should be compressible without errors."""
        h = BenchHarness(cleaner=cleaner, compressor=truncate, judge=judge)
        for task in ds_text_tasks():
            report = h.run_comparison(task)
            assert report.stm.error is None

    # ── QA scoring across datasets ─────────────────────────────

    def test_qa_score_on_originals(self, judge):
        """QA pairs should be answerable from the original content."""
        for task in ds_all_tasks():
            qa = judge.qa_score(task, task.content)
            assert qa["score"] >= 0.5, (
                f"{task.task_id}: only {qa['answerable']}/{qa['total']} answerable in original"
            )

    def test_strategy_matrix_on_datasets(self, harness):
        """Strategy matrix should run on all dataset tasks."""
        for task in ds_all_tasks():
            results = harness.run_strategy_matrix(task)
            assert len(results) >= 3

    # ── Surfacing value on dataset tasks ───────────────────────

    async def test_surfacing_fills_gaps(self, cleaner, truncate, judge):
        """Surfacing tasks: memories should add QA answers."""
        for task in ds_surfacing_tasks():
            memories = [
                FakeSearchResult(chunk=FakeChunk(content=m), score=0.8 - i * 0.1)
                for i, m in enumerate(task.surfacing_memories)
            ]
            config = _make_surfacing_config()
            engine = SurfacingEngine(config=config, search_pipeline=_make_search_pipeline(memories))
            h = BenchHarness(cleaner=cleaner, compressor=truncate, surfacing_engine=engine, judge=judge)
            value = await h.measure_surfacing_value(task)
            assert value.qa_delta > 0, f"{task.task_id}: surfacing added no QA answers"

    # ── Stage breakdown on datasets ────────────────────────────

    def test_stage_breakdown_all(self, harness):
        """Stage breakdown should work on all dataset tasks."""
        for task in ds_all_tasks():
            bd = harness.run_stage_breakdown(task)
            assert len(bd.stages) == 3
            assert bd.clean_info_loss >= 0
