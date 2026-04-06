"""Benchmark harness — A/B comparison of direct vs STM-proxied pipeline."""

from __future__ import annotations

import time as _time
from dataclasses import dataclass, field

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import (
    Compressor,
    FieldExtractCompressor,
    HybridCompressor,
    NoopCompressor,
    SelectiveCompressor,
    TruncateCompressor,
    auto_select_strategy,
)
from memtomem_stm.proxy.config import CompressionStrategy

from .judge import RuleBasedJudge


# ═══════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════

STRATEGY_COMPRESSORS: dict[str, Compressor] = {
    "none": NoopCompressor(),
    "truncate": TruncateCompressor(),
    "extract_fields": FieldExtractCompressor(),
    "hybrid": HybridCompressor(head_chars=500),
}


@dataclass
class BenchTask:
    """A single benchmark task definition."""

    task_id: str
    description: str
    content: str
    content_type: str  # "json" | "markdown" | "code" | "text"
    max_chars: int
    expected_keywords: list[str] = field(default_factory=list)
    expect_headings: int = 0
    expect_code_blocks: int = 0
    surfacing_memories: list[str] | None = None
    # Weight for keywords in scoring (0-1, default equal)
    keyword_weights: list[float] | None = None


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
    strategy: str = "unknown"

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
    text: str
    stage_metrics: StageMetrics | None
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


@dataclass
class SelectiveResult:
    """Result of a 2-phase selective compression benchmark."""

    task_id: str
    toc_chars: int  # Phase 1: TOC size
    toc_entry_count: int  # Number of selectable sections
    selected_chars: int  # Phase 2: selected content size
    selected_sections: list[str]  # Which sections were selected
    quality_score: float  # Quality of selected content
    total_chars: int  # Original content size
    recovery_ratio: float  # selected_chars / total_chars


@dataclass
class CurvePoint:
    """A single point on the compression curve."""

    budget_ratio: float  # 0.0-1.0 (fraction of original size)
    max_chars: int
    compressed_chars: int
    quality_score: float
    strategy: str


@dataclass
class StrategyResult:
    """Result of running one strategy on one task."""

    strategy: str
    quality_score: float
    compression_ratio: float  # compressed / original (lower = more compression)
    compressed_chars: int


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _get_compressor(strategy: str) -> Compressor:
    """Get compressor instance for a strategy name."""
    if strategy in STRATEGY_COMPRESSORS:
        return STRATEGY_COMPRESSORS[strategy]
    return TruncateCompressor()


def resolve_auto_strategy(content: str) -> str:
    """Use auto_select_strategy to pick the best compression strategy for content."""
    strategy = auto_select_strategy(content)
    return strategy.value


# ═══════════════════════════════════════════════════════════════════════════
# BenchHarness
# ═══════════════════════════════════════════════════════════════════════════


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

    def _run_pipeline(
        self, task: BenchTask, compressor: Compressor | None = None, max_chars: int | None = None
    ) -> BenchResult:
        """Run clean → compress pipeline with optional overrides."""
        comp = compressor or self._compressor
        budget = max_chars if max_chars is not None else task.max_chars
        original_chars = len(task.content)

        try:
            t0 = _time.monotonic()
            cleaned = self._cleaner.clean(task.content)
            clean_ms = (_time.monotonic() - t0) * 1000

            t0 = _time.monotonic()
            compressed = comp.compress(cleaned, max_chars=budget)
            compress_ms = (_time.monotonic() - t0) * 1000

            surfaced = compressed
            surface_ms = 0.0

            strategy_name = type(comp).__name__
            metrics = StageMetrics(
                original_chars=original_chars,
                cleaned_chars=len(cleaned),
                compressed_chars=len(compressed),
                surfaced_chars=len(surfaced),
                clean_ms=clean_ms,
                compress_ms=compress_ms,
                surface_ms=surface_ms,
                strategy=strategy_name,
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

    def run_stm(self, task: BenchTask) -> BenchResult:
        """Run task through clean → compress pipeline."""
        return self._run_pipeline(task)

    async def run_stm_with_surfacing(self, task: BenchTask) -> BenchResult:
        """Run task with full pipeline including async surfacing."""
        original_chars = len(task.content)
        try:
            t0 = _time.monotonic()
            cleaned = self._cleaner.clean(task.content)
            clean_ms = (_time.monotonic() - t0) * 1000

            t0 = _time.monotonic()
            compressed = self._compressor.compress(cleaned, max_chars=task.max_chars)
            compress_ms = (_time.monotonic() - t0) * 1000

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
                strategy=type(self._compressor).__name__,
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

    # ── Auto-strategy ────────────────────────────────────────────────

    def run_auto_strategy(self, task: BenchTask) -> ComparisonReport:
        """Run with auto-selected strategy based on content type."""
        direct = self.run_direct(task)
        # Clean first so auto-select sees cleaned content
        cleaned = self._cleaner.clean(task.content)
        strategy = resolve_auto_strategy(cleaned)
        compressor = _get_compressor(strategy)
        stm = self._run_pipeline(task, compressor=compressor)
        return ComparisonReport(task_id=task.task_id, direct=direct, stm=stm)

    # ── Strategy matrix ──────────────────────────────────────────────

    def run_strategy_matrix(
        self, task: BenchTask, strategies: list[str] | None = None
    ) -> dict[str, StrategyResult]:
        """Run a task with multiple strategies, return results keyed by strategy."""
        if strategies is None:
            strategies = ["truncate", "hybrid", "extract_fields", "auto"]

        cleaned = self._cleaner.clean(task.content)
        results: dict[str, StrategyResult] = {}

        for name in strategies:
            if name == "auto":
                auto_name = resolve_auto_strategy(cleaned)
                comp = _get_compressor(auto_name)
                actual_name = f"auto({auto_name})"
            else:
                comp = _get_compressor(name)
                actual_name = name

            compressed = comp.compress(cleaned, max_chars=task.max_chars)
            score = self._judge.score(task, compressed)
            ratio = len(compressed) / len(task.content) if task.content else 1.0
            results[actual_name] = StrategyResult(
                strategy=actual_name,
                quality_score=score,
                compression_ratio=ratio,
                compressed_chars=len(compressed),
            )

        return results

    # ── Compression curve ────────────────────────────────────────────

    def run_compression_curve(
        self,
        task: BenchTask,
        budget_ratios: list[float] | None = None,
        compressor: Compressor | None = None,
    ) -> list[CurvePoint]:
        """Run task at multiple budget levels, return quality vs compression curve."""
        if budget_ratios is None:
            budget_ratios = [0.3, 0.5, 0.7, 0.9]

        comp = compressor or self._compressor
        cleaned = self._cleaner.clean(task.content)
        original_chars = len(cleaned)
        points: list[CurvePoint] = []

        for ratio in sorted(budget_ratios):
            budget = max(50, int(original_chars * ratio))
            compressed = comp.compress(cleaned, max_chars=budget)
            score = self._judge.score(task, compressed)
            points.append(
                CurvePoint(
                    budget_ratio=ratio,
                    max_chars=budget,
                    compressed_chars=len(compressed),
                    quality_score=score,
                    strategy=type(comp).__name__,
                )
            )

        return points

    # ── Selective 2-phase ────────────────────────────────────────────

    def run_selective_2phase(
        self,
        task: BenchTask,
        select_top_n: int | None = None,
    ) -> SelectiveResult:
        """Run 2-phase selective compression: TOC → select top sections.

        Phase 1: compress() returns JSON TOC with section catalog
        Phase 2: select() retrieves full content of chosen sections
        """
        import json as _json

        comp = SelectiveCompressor(min_section_chars=10)
        cleaned = self._cleaner.clean(task.content)

        # Phase 1: get TOC
        toc_str = comp.compress(cleaned, max_chars=200)
        try:
            toc = _json.loads(toc_str)
        except _json.JSONDecodeError:
            # Content was short enough to return as-is
            score = self._judge.score(task, toc_str)
            return SelectiveResult(
                task_id=task.task_id,
                toc_chars=len(toc_str),
                toc_entry_count=0,
                selected_chars=len(toc_str),
                selected_sections=[],
                quality_score=score,
                total_chars=len(cleaned),
                recovery_ratio=1.0,
            )

        key = toc.get("selection_key", "")
        entries = toc.get("entries", [])

        # Phase 2: select top N sections by size (largest first)
        n = select_top_n or min(3, len(entries))
        sorted_entries = sorted(entries, key=lambda e: e.get("size", 0), reverse=True)
        section_keys = [e["key"] for e in sorted_entries[:n]]
        selected = comp.select(key, section_keys)

        score = self._judge.score(task, selected)
        return SelectiveResult(
            task_id=task.task_id,
            toc_chars=len(toc_str),
            toc_entry_count=len(entries),
            selected_chars=len(selected),
            selected_sections=section_keys,
            quality_score=score,
            total_chars=len(cleaned),
            recovery_ratio=len(selected) / len(cleaned) if cleaned else 0.0,
        )
