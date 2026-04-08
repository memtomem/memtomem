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
    SchemaPruningCompressor,
    SelectiveCompressor,
    SkeletonCompressor,
    TruncateCompressor,
    auto_select_strategy,
)

from .judge import RuleBasedJudge, _fuzzy_contains


# ═══════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════

STRATEGY_COMPRESSORS: dict[str, Compressor] = {
    "none": NoopCompressor(),
    "truncate": TruncateCompressor(),
    "extract_fields": FieldExtractCompressor(),
    "schema_pruning": SchemaPruningCompressor(),
    "skeleton": SkeletonCompressor(),
    "hybrid": HybridCompressor(head_chars=500),
}


@dataclass
class QAPair:
    """A question-answer pair for answer-based quality scoring."""

    question: str
    answer: str  # Ground truth — must be findable in the output
    source: str = "content"  # "content" = from original, "memory" = from surfaced memory


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
    # QA pairs for answer-based scoring
    qa_pairs: list[QAPair] = field(default_factory=list)
    # Agent query context for query-aware compression
    context_query: str = ""


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
class StageScore:
    """Quality score at a single pipeline stage."""

    stage: str  # "original" | "cleaned" | "compressed" | "surfaced"
    text: str
    chars: int
    quality_score: float
    qa_score: float  # QA-based score (0-1)
    qa_answerable: int  # number of answerable QA pairs
    qa_total: int  # total QA pairs


@dataclass
class StageBreakdown:
    """Per-stage quality breakdown showing where info is lost or gained."""

    task_id: str
    stages: list[StageScore]

    @property
    def clean_info_loss(self) -> float:
        """Quality drop from cleaning (should be ~0 for good cleaning)."""
        orig = self._get("original")
        cleaned = self._get("cleaned")
        if not orig or not cleaned or orig.quality_score == 0:
            return 0.0
        return orig.quality_score - cleaned.quality_score

    @property
    def compress_info_loss(self) -> float:
        """Quality drop from compression."""
        cleaned = self._get("cleaned")
        compressed = self._get("compressed")
        if not cleaned or not compressed or cleaned.quality_score == 0:
            return 0.0
        return cleaned.quality_score - compressed.quality_score

    @property
    def surfacing_value(self) -> float:
        """Quality gain from surfacing (positive = memories helped)."""
        compressed = self._get("compressed")
        surfaced = self._get("surfaced")
        if not compressed or not surfaced:
            return 0.0
        return surfaced.quality_score - compressed.quality_score

    @property
    def surfacing_qa_gain(self) -> int:
        """Number of QA pairs answerable ONLY because of surfacing."""
        compressed = self._get("compressed")
        surfaced = self._get("surfaced")
        if not compressed or not surfaced:
            return 0
        return surfaced.qa_answerable - compressed.qa_answerable

    def _get(self, stage: str) -> StageScore | None:
        for s in self.stages:
            if s.stage == stage:
                return s
        return None


@dataclass
class SurfacingValue:
    """Measures whether surfaced memories actually help answer questions."""

    task_id: str
    without_surfacing: float  # quality score (compress only)
    with_surfacing: float  # quality score (compress + surface)
    qa_without: int  # answerable QA pairs without surfacing
    qa_with: int  # answerable QA pairs with surfacing
    qa_total: int
    memories_injected: int
    quality_delta: float  # with - without (positive = surfacing helped)
    qa_delta: int  # qa_with - qa_without


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


def resolve_auto_strategy(content: str, max_chars: int = 0) -> str:
    """Use auto_select_strategy to pick the best compression strategy for content."""
    strategy = auto_select_strategy(content, max_chars=max_chars)
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

    @staticmethod
    def _apply_retention(cleaned_len: int, budget: int) -> int:
        """Enforce dynamic minimum retention — single source of truth.

        Mirrors ProxyManager logic so bench results match production behavior.
        """
        if cleaned_len < 1000:
            min_r = 0.9
        elif cleaned_len < 3000:
            min_r = 0.75
        elif cleaned_len < 10000:
            min_r = 0.65
        else:
            min_r = 0.5
        return max(budget, int(cleaned_len * min_r))

    def _run_pipeline(
        self,
        task: BenchTask,
        compressor: Compressor | None = None,
        max_chars: int | None = None,
        *,
        context_query: str | None = None,
    ) -> BenchResult:
        """Run clean → compress pipeline with optional overrides."""
        comp = compressor or self._compressor
        budget = max_chars if max_chars is not None else task.max_chars
        original_chars = len(task.content)

        try:
            t0 = _time.monotonic()
            cleaned = self._cleaner.clean(task.content)
            clean_ms = (_time.monotonic() - t0) * 1000

            # Enforce retention at harness level (matches ProxyManager behavior)
            budget = self._apply_retention(len(cleaned), budget)

            t0 = _time.monotonic()
            # Pass context_query to TruncateCompressor for query-aware allocation
            if context_query and isinstance(comp, TruncateCompressor):
                compressed = comp.compress(cleaned, max_chars=budget, context_query=context_query)
            else:
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

            effective_budget = self._apply_retention(len(cleaned), task.max_chars)

            t0 = _time.monotonic()
            compressed = self._compressor.compress(cleaned, max_chars=effective_budget)
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

    def run_query_aware_comparison(self, task: BenchTask) -> ComparisonReport:
        """Compare: truncate (no query) vs truncate (with context_query).

        Uses the task's context_query field to evaluate whether query-aware
        budget allocation improves quality over baseline truncation.
        """
        comp = TruncateCompressor()
        baseline = self._run_pipeline(task, compressor=comp)
        query = task.context_query or None
        query_aware = self._run_pipeline(task, compressor=comp, context_query=query)
        return ComparisonReport(task_id=task.task_id, direct=baseline, stm=query_aware)

    # ── Auto-strategy ────────────────────────────────────────────────

    def run_auto_strategy(self, task: BenchTask) -> ComparisonReport:
        """Run with auto-selected strategy based on content type."""
        direct = self.run_direct(task)
        # Clean first so auto-select sees cleaned content
        cleaned = self._cleaner.clean(task.content)
        strategy = resolve_auto_strategy(cleaned, max_chars=task.max_chars)
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
                auto_name = resolve_auto_strategy(cleaned, max_chars=task.max_chars)
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

    # ── Per-stage breakdown ──────────────────────────────────────────

    def _score_stage(self, task: BenchTask, stage: str, text: str) -> StageScore:
        """Score text at a given pipeline stage."""
        quality = self._judge.score(task, text)
        qa_answerable = 0
        qa_total = len(task.qa_pairs)
        for qa in task.qa_pairs:
            if _fuzzy_contains(qa.answer, text):
                qa_answerable += 1
        qa_score = qa_answerable / qa_total if qa_total else 1.0
        return StageScore(
            stage=stage,
            text=text,
            chars=len(text),
            quality_score=quality,
            qa_score=qa_score,
            qa_answerable=qa_answerable,
            qa_total=qa_total,
        )

    def run_stage_breakdown(self, task: BenchTask) -> StageBreakdown:
        """Run pipeline and measure quality at EACH stage individually.

        Returns scores for: original → cleaned → compressed → surfaced
        This shows exactly where information is lost (or gained).
        """
        stages: list[StageScore] = []

        # Stage 0: Original
        stages.append(self._score_stage(task, "original", task.content))

        # Stage 1: Cleaned
        cleaned = self._cleaner.clean(task.content)
        stages.append(self._score_stage(task, "cleaned", cleaned))

        # Stage 2: Compressed
        compressed = self._compressor.compress(cleaned, max_chars=task.max_chars)
        stages.append(self._score_stage(task, "compressed", compressed))

        return StageBreakdown(task_id=task.task_id, stages=stages)

    async def run_stage_breakdown_with_surfacing(self, task: BenchTask) -> StageBreakdown:
        """Run full pipeline including surfacing, measure quality at each stage."""
        stages: list[StageScore] = []

        stages.append(self._score_stage(task, "original", task.content))

        cleaned = self._cleaner.clean(task.content)
        stages.append(self._score_stage(task, "cleaned", cleaned))

        compressed = self._compressor.compress(cleaned, max_chars=task.max_chars)
        stages.append(self._score_stage(task, "compressed", compressed))

        if self._surfacing is not None:
            surfaced = await self._surfacing.surface(
                server="bench",
                tool="bench_task",
                arguments={"_context_query": task.description},
                response_text=compressed,
            )
            stages.append(self._score_stage(task, "surfaced", surfaced))

        return StageBreakdown(task_id=task.task_id, stages=stages)

    # ── Surfacing value measurement ──────────────────────────────────

    async def measure_surfacing_value(self, task: BenchTask) -> SurfacingValue:
        """Compare quality with vs without surfacing to measure memory value.

        Requires surfacing_engine to be set. Uses QA pairs to detect
        whether memories fill knowledge gaps.
        """
        cleaned = self._cleaner.clean(task.content)
        compressed = self._compressor.compress(cleaned, max_chars=task.max_chars)

        # Without surfacing
        score_without = self._judge.score(task, compressed)
        qa_without = sum(1 for qa in task.qa_pairs if _fuzzy_contains(qa.answer, compressed))

        # With surfacing
        if self._surfacing is not None:
            surfaced = await self._surfacing.surface(
                server="bench",
                tool="bench_task",
                arguments={"_context_query": task.description},
                response_text=compressed,
            )
        else:
            surfaced = compressed

        score_with = self._judge.score(task, surfaced)
        qa_with = sum(1 for qa in task.qa_pairs if _fuzzy_contains(qa.answer, surfaced))

        # Count injected memories (check for surfacing marker)
        memories_injected = surfaced.count("score=0.") if surfaced != compressed else 0

        return SurfacingValue(
            task_id=task.task_id,
            without_surfacing=score_without,
            with_surfacing=score_with,
            qa_without=qa_without,
            qa_with=qa_with,
            qa_total=len(task.qa_pairs),
            memories_injected=memories_injected,
            quality_delta=score_with - score_without,
            qa_delta=qa_with - qa_without,
        )
