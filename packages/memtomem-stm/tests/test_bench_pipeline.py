"""STM pipeline quality benchmark tests.

Tests the benchmark harness, quality judge, per-stage metrics,
and runs A/B comparisons across all task types.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import uuid4

import pytest

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import (
    FieldExtractCompressor,
    HybridCompressor,
    TruncateCompressor,
)
from memtomem_stm.proxy.config import CleaningConfig
from memtomem_stm.proxy.metrics import CallMetrics, TokenTracker

from bench.harness import BenchHarness, BenchResult, BenchTask, ComparisonReport, StageMetrics
from bench.judge import RuleBasedJudge
from bench.report import format_report
from bench.tasks import (
    API_RESPONSE_JSON,
    CODE_FILE,
    HTML_MIXED,
    MARKDOWN_WITH_LINKS,
    MEETING_NOTES,
    MULTILINGUAL_KR_EN,
    SHORT_RESPONSE,
    get_all_tasks,
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
        """Harness returns error result if compressor raises."""

        class BrokenCompressor:
            def compress(self, text, *, max_chars):
                raise RuntimeError("broken")

        h = BenchHarness(cleaner=cleaner, compressor=BrokenCompressor(), judge=judge)
        task = BenchTask(
            task_id="err",
            description="err",
            content="some text",
            content_type="text",
            max_chars=100,
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
        # HTML cleaning should reduce size (script/style/tags removed)
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
        assert m.compressed_chars <= task.max_chars + 200  # allow some metadata overhead

    def test_timing_is_positive(self, harness):
        task = BenchTask(
            task_id="time",
            description="timing",
            content=CODE_FILE,
            content_type="code",
            max_chars=500,
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
            original_chars=0,
            cleaned_chars=0,
            compressed_chars=0,
            surfaced_chars=0,
            clean_ms=0,
            compress_ms=0,
            surface_ms=0,
        )
        assert m.cleaning_ratio == 1.0
        assert m.compression_ratio == 1.0
        assert m.total_reduction == 1.0
        assert m.surfacing_overhead == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# TestQualityJudge — scoring logic
# ═══════════════════════════════════════════════════════════════════════════


class TestQualityJudge:
    def test_perfect_score(self, judge):
        task = BenchTask(
            task_id="t",
            description="t",
            content="Hello World",
            content_type="text",
            max_chars=100,
            expected_keywords=["Hello", "World"],
        )
        assert judge.score(task, "Hello World") == 10.0

    def test_missing_keyword_deducts(self, judge):
        task = BenchTask(
            task_id="t",
            description="t",
            content="x",
            content_type="text",
            max_chars=100,
            expected_keywords=["alpha", "beta", "gamma"],
        )
        # Missing all 3 → -6.0
        assert judge.score(task, "nothing here") == 4.0

    def test_partial_keywords(self, judge):
        task = BenchTask(
            task_id="t",
            description="t",
            content="x",
            content_type="text",
            max_chars=100,
            expected_keywords=["alpha", "beta"],
        )
        # Has alpha, missing beta → -2.0
        assert judge.score(task, "alpha is present") == 8.0

    def test_heading_check(self, judge):
        task = BenchTask(
            task_id="t",
            description="t",
            content="x",
            content_type="markdown",
            max_chars=100,
            expect_headings=3,
        )
        text = "## H1\n## H2\nno more"
        # 2 headings but expected 3 → -1.0
        assert judge.score(task, text) == 9.0

    def test_code_block_check(self, judge):
        task = BenchTask(
            task_id="t",
            description="t",
            content="x",
            content_type="code",
            max_chars=100,
            expect_code_blocks=2,
        )
        text = "```python\ncode\n```\nonly one block"
        # 1 block but expected 2 → -1.0
        assert judge.score(task, text) == 9.0

    def test_json_validity_bonus(self, judge):
        task = BenchTask(
            task_id="t",
            description="t",
            content="x",
            content_type="json",
            max_chars=100,
            expected_keywords=["key"],
        )
        # Valid JSON + keyword present → 10.0 + 0.5 = 10.0 (capped)
        assert judge.score(task, '{"key": "value"}') == 10.0

    def test_json_invalid_no_bonus(self, judge):
        task = BenchTask(
            task_id="t",
            description="t",
            content="x",
            content_type="json",
            max_chars=100,
            expected_keywords=["key"],
        )
        assert judge.score(task, "key: value") == 10.0  # keyword present, no JSON bonus

    def test_score_floor_at_zero(self, judge):
        task = BenchTask(
            task_id="t",
            description="t",
            content="x",
            content_type="text",
            max_chars=100,
            expected_keywords=["a", "b", "c", "d", "e", "f"],  # 6 missing → -12
        )
        assert judge.score(task, "nothing") == 0.0

    def test_case_insensitive_keywords(self, judge):
        task = BenchTask(
            task_id="t",
            description="t",
            content="x",
            content_type="text",
            max_chars=100,
            expected_keywords=["PostgreSQL"],
        )
        assert judge.score(task, "we use postgresql for storage") == 10.0


# ═══════════════════════════════════════════════════════════════════════════
# TestPipelineQuality — A/B comparison across all tasks
# ═══════════════════════════════════════════════════════════════════════════


class TestPipelineQuality:
    """Run all 7 benchmark tasks and verify quality preservation."""

    def test_all_tasks_run(self, harness):
        tasks = get_all_tasks()
        reports = [harness.run_comparison(t) for t in tasks]
        assert len(reports) == 7
        for r in reports:
            assert r.direct.error is None
            assert r.stm.error is None

    def test_short_response_passthrough(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "short_response"][0]
        report = harness.run_comparison(task)
        assert report.quality_preservation == 100.0
        assert report.total_reduction == 1.0  # no compression

    def test_json_compression_preserves_structure(self, cleaner, field_extract, judge):
        h = BenchHarness(cleaner=cleaner, compressor=field_extract, judge=judge)
        task = [t for t in get_all_tasks() if t.task_id == "api_response_json"][0]
        report = h.run_comparison(task)
        # FieldExtract should preserve top-level keys
        assert report.stm.quality_score >= 6.0

    def test_code_file_preserves_key_info(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        report = harness.run_comparison(task)
        assert report.quality_preservation >= 50.0  # at least half quality

    def test_meeting_notes_preserves_decisions(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "meeting_notes"][0]
        report = harness.run_comparison(task)
        # Meeting decisions are critical
        assert "PostgreSQL" in report.stm.text or "postgresql" in report.stm.text.lower()

    def test_multilingual_preserves_keywords(self, harness):
        task = [t for t in get_all_tasks() if t.task_id == "multilingual_kr_en"][0]
        report = harness.run_comparison(task)
        assert report.quality_preservation >= 50.0


# ═══════════════════════════════════════════════════════════════════════════
# TestCompressionStrategies — strategy comparison
# ═══════════════════════════════════════════════════════════════════════════


class TestCompressionStrategies:
    """Compare quality across different compression strategies."""

    def _run_with(self, cleaner, compressor, judge, task):
        h = BenchHarness(cleaner=cleaner, compressor=compressor, judge=judge)
        return h.run_comparison(task)

    def test_truncate_vs_hybrid_on_code(self, cleaner, truncate, hybrid, judge):
        task = [t for t in get_all_tasks() if t.task_id == "code_file_large"][0]
        r_trunc = self._run_with(cleaner, truncate, judge, task)
        r_hybrid = self._run_with(cleaner, hybrid, judge, task)
        # Both should produce valid results
        assert r_trunc.stm.error is None
        assert r_hybrid.stm.error is None

    def test_field_extract_on_json(self, cleaner, field_extract, judge):
        task = [t for t in get_all_tasks() if t.task_id == "api_response_json"][0]
        report = self._run_with(cleaner, field_extract, judge, task)
        # FieldExtract should show top-level keys
        assert "total" in report.stm.text.lower() or "users" in report.stm.text.lower()

    def test_truncate_on_markdown(self, cleaner, truncate, judge):
        task = [t for t in get_all_tasks() if t.task_id == "meeting_notes"][0]
        report = self._run_with(cleaner, truncate, judge, task)
        m = report.stm.stage_metrics
        assert m is not None
        # Should actually compress
        assert m.compressed_chars <= m.cleaned_chars

    def test_hybrid_preserves_head(self, cleaner, hybrid, judge):
        task = BenchTask(
            task_id="hybrid_head",
            description="hybrid head test",
            content=CODE_FILE,
            content_type="code",
            max_chars=800,
            expected_keywords=["JWT", "Overview"],
        )
        report = self._run_with(cleaner, hybrid, judge, task)
        # Head should be preserved verbatim (first ~500 chars)
        assert "Authentication Module" in report.stm.text


# ═══════════════════════════════════════════════════════════════════════════
# TestSurfacingOverhead — surfacing adds content
# ═══════════════════════════════════════════════════════════════════════════


class TestSurfacingOverhead:
    def test_no_surfacing_zero_overhead(self, harness):
        task = BenchTask(
            task_id="nosrf",
            description="no surfacing",
            content=MEETING_NOTES,
            content_type="markdown",
            max_chars=500,
        )
        result = harness.run_stm(task)
        m = result.stage_metrics
        assert m is not None
        # Without surfacing engine, surfaced == compressed
        assert m.surfaced_chars == m.compressed_chars
        assert m.surfacing_overhead == 0.0


# ═══════════════════════════════════════════════════════════════════════════
# TestReport — report formatting
# ═══════════════════════════════════════════════════════════════════════════


class TestReport:
    def test_format_empty(self):
        text = format_report([])
        assert "No tasks run" in text

    def test_format_single_task(self, harness):
        task = BenchTask(
            task_id="rpt",
            description="report test",
            content=MEETING_NOTES,
            content_type="markdown",
            max_chars=500,
            expected_keywords=["PostgreSQL"],
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
        assert "Tasks: 7" in text
        assert "Avg quality preservation" in text

    def test_warning_on_low_quality(self):
        # Construct a report with low quality preservation
        direct = BenchResult(
            task_id="low", mode="direct", text="x", stage_metrics=None, quality_score=10.0
        )
        stm = BenchResult(
            task_id="low",
            mode="stm",
            text="y",
            stage_metrics=StageMetrics(
                original_chars=100,
                cleaned_chars=90,
                compressed_chars=50,
                surfaced_chars=50,
                clean_ms=1,
                compress_ms=1,
                surface_ms=0,
            ),
            quality_score=6.0,
        )
        report = ComparisonReport(task_id="low", direct=direct, stm=stm)
        text = format_report([report])
        assert "⚠️" in text  # Quality below 80%


# ═══════════════════════════════════════════════════════════════════════════
# TestCallMetrics — new timing fields in metrics.py
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
            server="s",
            tool="t",
            original_chars=1000,
            compressed_chars=500,
            clean_ms=1.5,
            compress_ms=3.2,
            surface_ms=10.0,
            surfaced_chars=600,
        )
        assert m.clean_ms == 1.5
        assert m.surfaced_chars == 600

    def test_tracker_aggregates_timing(self):
        tracker = TokenTracker(metrics_store=None)
        tracker.record(
            CallMetrics(
                server="a",
                tool="t1",
                original_chars=1000,
                compressed_chars=500,
                clean_ms=2.0,
                compress_ms=5.0,
                surface_ms=10.0,
                surfaced_chars=600,
            )
        )
        tracker.record(
            CallMetrics(
                server="a",
                tool="t2",
                original_chars=2000,
                compressed_chars=800,
                clean_ms=4.0,
                compress_ms=7.0,
                surface_ms=20.0,
                surfaced_chars=900,
            )
        )
        summary = tracker.get_summary()
        assert summary["total_calls"] == 2
        assert summary["total_surfaced_chars"] == 1500
        assert summary["avg_clean_ms"] == 3.0
        assert summary["avg_compress_ms"] == 6.0
        assert summary["avg_surface_ms"] == 15.0

    def test_tracker_backward_compatible(self):
        """Old-style CallMetrics (no timing) still works."""
        tracker = TokenTracker(metrics_store=None)
        tracker.record(
            CallMetrics(server="s", tool="t", original_chars=100, compressed_chars=50)
        )
        summary = tracker.get_summary()
        assert summary["total_calls"] == 1
        assert summary["avg_clean_ms"] == 0.0
        assert summary["total_surfaced_chars"] == 0
