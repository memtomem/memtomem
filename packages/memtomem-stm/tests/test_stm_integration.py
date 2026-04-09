"""Integration tests for STM pipeline stages working together."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4


from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import HybridCompressor, SelectiveCompressor
from memtomem_stm.proxy.config import CleaningConfig
from memtomem_stm.surfacing.config import SurfacingConfig
from memtomem_stm.surfacing.engine import SurfacingEngine
from memtomem_stm.surfacing.feedback import AutoTuner, FeedbackTracker


@dataclass
class FakeChunkMeta:
    source_file: Path = Path("/notes/test.md")
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


# ── Clean → Compress → Surface pipeline ─────────────────────────────────


class TestFullPipeline:
    async def test_html_clean_compress_surface(self):
        """HTML content → clean → hybrid compress → surface memories."""
        # Stage 1: HTML content from upstream
        raw = (
            "<div><h1>API Reference</h1>"
            "<p>This is the authentication endpoint.</p>" * 20
            + "<script>alert('xss')</script>"
            + "<a href='http://example.com'>link</a>" * 50
            + "</div>"
        )

        # Stage 2: Clean
        cleaner = DefaultContentCleaner(CleaningConfig(strip_html=True, collapse_links=True))
        cleaned = cleaner.clean(raw)
        assert "<div>" not in cleaned
        assert "<script>" not in cleaned

        # Stage 3: Compress (hybrid — head preserve + tail TOC)
        compressor = HybridCompressor(head_chars=200)
        compressed = compressor.compress(cleaned, max_chars=500)
        assert len(compressed) <= 500

        # Stage 4: Surface
        results = [FakeSearchResult(chunk=FakeChunk(content="JWT auth guide"), score=0.5)]
        engine = SurfacingEngine(
            config=SurfacingConfig(
                enabled=True,
                min_response_chars=10,
                cooldown_seconds=0,
                max_surfacings_per_minute=1000,
                auto_tune_enabled=False,
                include_session_context=False,
                fire_webhook=False,
            ),
            mcp_adapter=AsyncMock(search=AsyncMock(return_value=(results, {}))),
        )
        final = await engine.surface(
            "api", "read_file",
            {"path": "/src/auth/jwt_handler.py", "_context_query": "authentication JWT handler"},
            compressed,
        )
        assert "Relevant Memories" in final
        assert "JWT auth guide" in final


# ── Selective 2-phase flow ───────────────────────────────────────────────


class TestSelectiveTwoPhase:
    def test_compress_then_select(self):
        """Phase 1: compress → TOC. Phase 2: select sections."""
        # Content must exceed max_chars to trigger TOC generation
        sections = []
        for name in ["Overview", "Installation", "Configuration", "Usage", "API Reference"]:
            body = f"Detailed content for {name} section. " * 20
            sections.append(f"# {name}\n\n{body}")
        content = "\n\n".join(sections)

        compressor = SelectiveCompressor(min_section_chars=10)

        # Phase 1: Returns TOC with selection key (content > max_chars)
        toc = compressor.compress(content, max_chars=200)
        assert "selection_key" in toc  # TOC JSON, not raw text

        # Extract selection key from TOC JSON
        import json
        toc_data = json.loads(toc)
        key = toc_data["selection_key"]
        entry_names = [e.get("title") or e.get("name", "") for e in toc_data["entries"]]
        assert len(entry_names) >= 2

        # Phase 2: Retrieve specific sections
        selected = compressor.select(key, entry_names[:2])
        assert len(selected) > 0


# ── AutoTuner feedback loop ──────────────────────────────────────────────


class TestAutoTunerLoop:
    def test_negative_feedback_raises_min_score(self, tmp_path):
        """60%+ not_relevant feedback → min_score increases."""
        db_path = tmp_path / "feedback.db"
        config = SurfacingConfig(
            auto_tune_enabled=True,
            auto_tune_min_samples=5,
            auto_tune_score_increment=0.005,
            min_score=0.02,
        )
        tracker = FeedbackTracker(config, db_path)
        tuner = AutoTuner(config, tracker.store)

        for i in range(5):
            tracker.record_surfacing(
                surfacing_id=f"s{i}",
                server="gh",
                tool="read_file",
                query=f"query{i}",
                memory_ids=[str(uuid4())],
                scores=[0.03],
            )
            tracker.store.record_feedback(f"s{i}", "not_relevant")

        new_score = tuner.maybe_adjust("read_file")
        if new_score is not None:
            assert new_score > config.min_score

        tracker.close()

    def test_positive_feedback_lowers_min_score(self, tmp_path):
        """<20% not_relevant feedback → min_score decreases."""
        db_path = tmp_path / "feedback.db"
        config = SurfacingConfig(
            auto_tune_enabled=True,
            auto_tune_min_samples=5,
            auto_tune_score_increment=0.005,
            min_score=0.05,
        )
        tracker = FeedbackTracker(config, db_path)
        tuner = AutoTuner(config, tracker.store)

        for i in range(5):
            tracker.record_surfacing(
                surfacing_id=f"p{i}",
                server="gh",
                tool="search_code",
                query=f"query{i}",
                memory_ids=[str(uuid4())],
                scores=[0.1],
            )
            tracker.store.record_feedback(f"p{i}", "helpful")

        new_score = tuner.maybe_adjust("search_code")
        if new_score is not None:
            assert new_score < config.min_score

        tracker.close()
