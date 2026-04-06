"""Benchmark harness — A/B comparison of direct vs STM-proxied pipeline."""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import Compressor, auto_select_strategy

from .judge import RuleBasedJudge


@dataclass
class BenchTask:
    """A single benchmark task definition."""

    task_id: str
    description: str
    content: str  # Original response text
    content_type: str  # "json" | "markdown" | "code" | "text"
    max_chars: int  # Compression budget
    expected_keywords: list[str] = field(default_factory=list)
    expect_headings: int = 0
    expect_code_blocks: int = 0
    surfacing_memories: list[str] | None = None  # Memories to inject (None=skip surfacing)


@dataclass
class StageMetrics:
    """Per-stage size and timing measurements."""

    original_chars: int
    cleaned_chars: int
    compressed_chars: int
    surfaced_chars: int
    clean_ms: float
    compress_ms: float
    surface_ms: float

    @property
    def cleaning_ratio(self) -> float:
        return self.cleaned_chars / self.original_chars if self.original_chars else 1.0

    @property
    def compression_ratio(self) -> float:
        return self.compressed_chars / self.cleaned_chars if self.cleaned_chars else 1.0

    @property
    def total_reduction(self) -> float:
        return self.compressed_chars / self.original_chars if self.original_chars else 1.0

    @property
    def surfacing_overhead(self) -> float:
        if self.compressed_chars == 0:
            return 0.0
        return (self.surfaced_chars - self.compressed_chars) / self.compressed_chars


@dataclass
class BenchResult:
    """Result of a single benchmark run."""

    task_id: str
    mode: str  # "direct" | "stm"
    text: str  # Final output text
    stage_metrics: StageMetrics | None  # None for direct mode
    quality_score: float  # 0-10
    error: str | None = None


@dataclass
class ComparisonReport:
    """A/B comparison between direct and STM results."""

    task_id: str
    direct: BenchResult
    stm: BenchResult

    @property
    def quality_preservation(self) -> float:
        if self.direct.quality_score == 0:
            return 100.0
        return (self.stm.quality_score / self.direct.quality_score) * 100

    @property
    def cleaning_ratio(self) -> float:
        m = self.stm.stage_metrics
        return m.cleaning_ratio if m else 1.0

    @property
    def compression_ratio(self) -> float:
        m = self.stm.stage_metrics
        return m.compression_ratio if m else 1.0

    @property
    def total_reduction(self) -> float:
        m = self.stm.stage_metrics
        return m.total_reduction if m else 1.0

    @property
    def surfacing_overhead(self) -> float:
        m = self.stm.stage_metrics
        return m.surfacing_overhead if m else 0.0


class BenchHarness:
    """Runs benchmark tasks through direct passthrough and STM pipeline."""

    def __init__(
        self,
        cleaner: DefaultContentCleaner,
        compressor: Compressor,
        surfacing_engine: object | None = None,
        judge: RuleBasedJudge | None = None,
    ) -> None:
        self._cleaner = cleaner
        self._compressor = compressor
        self._surfacing = surfacing_engine
        self._judge = judge or RuleBasedJudge()

    def run_direct(self, task: BenchTask) -> BenchResult:
        """Run task in direct mode — original text, baseline quality."""
        score = self._judge.score(task, task.content)
        return BenchResult(
            task_id=task.task_id,
            mode="direct",
            text=task.content,
            stage_metrics=None,
            quality_score=score,
        )

    def run_stm(self, task: BenchTask) -> BenchResult:
        """Run task through clean → compress pipeline, measure each stage."""
        original_chars = len(task.content)

        try:
            # Stage 1: CLEAN
            t0 = _time.monotonic()
            cleaned = self._cleaner.clean(task.content)
            clean_ms = (_time.monotonic() - t0) * 1000

            # Stage 2: COMPRESS
            t0 = _time.monotonic()
            compressed = self._compressor.compress(cleaned, max_chars=task.max_chars)
            compress_ms = (_time.monotonic() - t0) * 1000

            # Stage 3: SURFACE (sync placeholder — no actual surfacing in unit bench)
            surfaced = compressed
            surface_ms = 0.0

            metrics = StageMetrics(
                original_chars=original_chars,
                cleaned_chars=len(cleaned),
                compressed_chars=len(compressed),
                surfaced_chars=len(surfaced),
                clean_ms=clean_ms,
                compress_ms=compress_ms,
                surface_ms=surface_ms,
            )

            score = self._judge.score(task, surfaced)
            return BenchResult(
                task_id=task.task_id,
                mode="stm",
                text=surfaced,
                stage_metrics=metrics,
                quality_score=score,
            )
        except Exception as exc:
            return BenchResult(
                task_id=task.task_id,
                mode="stm",
                text="",
                stage_metrics=None,
                quality_score=0.0,
                error=str(exc),
            )

    async def run_stm_with_surfacing(self, task: BenchTask) -> BenchResult:
        """Run task with full pipeline including async surfacing."""
        original_chars = len(task.content)

        try:
            # Stage 1: CLEAN
            t0 = _time.monotonic()
            cleaned = self._cleaner.clean(task.content)
            clean_ms = (_time.monotonic() - t0) * 1000

            # Stage 2: COMPRESS
            t0 = _time.monotonic()
            compressed = self._compressor.compress(cleaned, max_chars=task.max_chars)
            compress_ms = (_time.monotonic() - t0) * 1000

            # Stage 3: SURFACE
            surfaced = compressed
            surface_ms = 0.0
            if self._surfacing is not None:
                t0 = _time.monotonic()
                surfaced = await self._surfacing.surface(
                    server="bench",
                    tool="bench_task",
                    arguments={"_context_query": task.description},
                    response_text=compressed,
                )
                surface_ms = (_time.monotonic() - t0) * 1000

            metrics = StageMetrics(
                original_chars=original_chars,
                cleaned_chars=len(cleaned),
                compressed_chars=len(compressed),
                surfaced_chars=len(surfaced),
                clean_ms=clean_ms,
                compress_ms=compress_ms,
                surface_ms=surface_ms,
            )

            score = self._judge.score(task, surfaced)
            return BenchResult(
                task_id=task.task_id,
                mode="stm",
                text=surfaced,
                stage_metrics=metrics,
                quality_score=score,
            )
        except Exception as exc:
            return BenchResult(
                task_id=task.task_id,
                mode="stm",
                text="",
                stage_metrics=None,
                quality_score=0.0,
                error=str(exc),
            )

    def run_comparison(self, task: BenchTask) -> ComparisonReport:
        """Run both direct and STM, return comparison."""
        direct = self.run_direct(task)
        stm = self.run_stm(task)
        return ComparisonReport(task_id=task.task_id, direct=direct, stm=stm)
