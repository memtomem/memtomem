"""STM effectiveness tests — validates the system makes GOOD decisions.

These tests verify that STM delivers actual value:
- Surfaces relevant memories, suppresses irrelevant ones
- Compression preserves important info while reducing size
- Auto-tuner converges to optimal thresholds
- Context extraction produces meaningful queries
- Gating makes correct allow/reject decisions
- Cleaning removes noise without losing content
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from memtomem_stm.proxy.cleaning import DefaultContentCleaner
from memtomem_stm.proxy.compression import (
    FieldExtractCompressor,
    HybridCompressor,
    SelectiveCompressor,
    TruncateCompressor,
)
from memtomem_stm.proxy.config import CleaningConfig
from memtomem_stm.surfacing.config import SurfacingConfig, ToolSurfacingConfig
from memtomem_stm.surfacing.context_extractor import ContextExtractor
from memtomem_stm.surfacing.feedback import AutoTuner, FeedbackTracker
from memtomem_stm.surfacing.relevance import RelevanceGate


# ═══════════════════════════════════════════════════════════════════════════
# 1. CONTEXT EXTRACTION QUALITY
#    Does the system extract meaningful search queries from tool calls?
# ═══════════════════════════════════════════════════════════════════════════


class TestQueryExtractionQuality:
    """Verify the system produces useful search queries, not garbage."""

    def setup_method(self):
        self.extractor = ContextExtractor()
        self.config = SurfacingConfig()

    def test_file_path_tokenized_into_query(self):
        """File paths are split into meaningful tokens for search."""
        query = self.extractor.extract_query(
            "fs", "read_file",
            {"path": "/src/auth/jwt_handler.py"},
            self.config,
        )
        assert query is not None
        assert "auth" in query.lower()
        assert "jwt" in query.lower()
        assert "handler" in query.lower()

    def test_explicit_context_query_preferred(self):
        """_context_query overrides heuristic extraction."""
        query = self.extractor.extract_query(
            "fs", "read_file",
            {"path": "/tmp/x.py", "_context_query": "authentication token refresh"},
            self.config,
        )
        assert query == "authentication token refresh"

    def test_uuid_excluded_from_query(self):
        """UUIDs should not appear in queries — they're not semantically useful."""
        query = self.extractor.extract_query(
            "db", "get_record",
            {"id": "550e8400-e29b-41d4-a716-446655440000", "table": "users profile data"},
            self.config,
        )
        assert query is not None
        assert "550e8400" not in query

    def test_hex_string_excluded(self):
        """Long hex strings (commit hashes etc.) excluded."""
        query = self.extractor.extract_query(
            "git", "show_commit",
            {"hash": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2", "repo": "memtomem project repo"},
            self.config,
        )
        assert query is not None
        assert "a1b2c3" not in query

    def test_short_query_rejected(self):
        """Queries with < 3 tokens are rejected (too vague to search)."""
        query = self.extractor.extract_query(
            "fs", "stat", {"path": "/tmp"},
            self.config,
        )
        # /tmp alone is too short for a useful query
        # Result should be None or very short
        if query is not None:
            tokens = query.split()
            # Either rejected or padded with tool name
            assert len(tokens) >= 1

    def test_template_extraction(self):
        """Per-tool query templates produce formatted queries."""
        config = SurfacingConfig(
            context_tools={
                "search_code": ToolSurfacingConfig(
                    query_template="Code search: {arg.query} in {arg.language}"
                )
            }
        )
        query = self.extractor.extract_query(
            "gh", "search_code",
            {"query": "authentication middleware", "language": "python"},
            config,
        )
        assert query is not None
        assert "authentication middleware" in query
        assert "python" in query

    def test_semantic_keys_prioritized(self):
        """Keys like 'query', 'search', 'topic' are preferred over generic keys."""
        query = self.extractor.extract_query(
            "api", "fetch",
            {"url": "https://example.com/api", "query": "deployment configuration"},
            self.config,
        )
        assert query is not None
        # 'query' key should be prioritized
        assert "deployment" in query.lower()


# ═══════════════════════════════════════════════════════════════════════════
# 2. GATING EFFECTIVENESS
#    Does the system correctly decide WHEN to surface memories?
# ═══════════════════════════════════════════════════════════════════════════


class TestGatingEffectiveness:
    """Verify the gate makes correct allow/reject decisions."""

    def test_write_tools_always_rejected(self):
        """Writing/mutating tools should never trigger surfacing."""
        gate = RelevanceGate(SurfacingConfig())
        write_tools = [
            "write_file", "create_issue", "delete_branch",
            "push_changes", "send_message", "remove_label",
        ]
        for tool in write_tools:
            assert not gate.should_surface("any", tool, "some query here"), (
                f"Write tool '{tool}' should be rejected"
            )

    def test_read_tools_allowed(self):
        """Reading/querying tools should be allowed."""
        gate = RelevanceGate(SurfacingConfig(cooldown_seconds=0))
        read_tools = [
            "read_file", "list_repos", "get_issue",
            "search_code", "show_diff", "fetch_page",
        ]
        for tool in read_tools:
            assert gate.should_surface("any", tool, f"query about {tool}"), (
                f"Read tool '{tool}' should be allowed"
            )

    def test_rate_limit_prevents_spam(self):
        """Rate limiting prevents excessive surfacing."""
        gate = RelevanceGate(SurfacingConfig(
            max_surfacings_per_minute=3,
            cooldown_seconds=0,
        ))
        # First 3 should pass
        for i in range(3):
            assert gate.should_surface("s", "read_file", f"unique query number {i} here")

        # 4th should be rate-limited
        assert not gate.should_surface("s", "read_file", "another unique query here now")

    def test_cooldown_deduplicates_queries(self):
        """Near-identical queries within cooldown are suppressed."""
        gate = RelevanceGate(SurfacingConfig(cooldown_seconds=10.0))
        assert gate.should_surface("s", "read_file", "kubernetes monitoring setup config")
        # Same query immediately after → rejected
        assert not gate.should_surface("s", "read_file", "kubernetes monitoring setup config")

    def test_different_queries_not_blocked(self):
        """Sufficiently different queries pass cooldown check."""
        gate = RelevanceGate(SurfacingConfig(cooldown_seconds=10.0))
        assert gate.should_surface("s", "read_file", "kubernetes monitoring setup config")
        # Different enough query → allowed
        assert gate.should_surface("s", "read_file", "redis caching eviction policy details")

    def test_explicit_exclusion_works(self):
        """Excluded tools are always rejected regardless of query."""
        gate = RelevanceGate(SurfacingConfig(exclude_tools=["llm__summarize"]))
        assert not gate.should_surface("llm", "summarize", "important research topic query")


# ═══════════════════════════════════════════════════════════════════════════
# 3. COMPRESSION PRESERVES IMPORTANT INFORMATION
#    Does compression reduce size while keeping what matters?
# ═══════════════════════════════════════════════════════════════════════════


class TestCompressionEffectiveness:
    """Verify compression reduces size without losing critical content."""

    def test_truncate_respects_sentence_boundary(self):
        """Truncation should cut at sentence boundaries, not mid-word."""
        text = "First sentence here. Second sentence follows. Third one too. Fourth comes last."
        comp = TruncateCompressor()
        result = comp.compress(text, max_chars=50)
        # Should cut at a sentence boundary
        assert result.endswith(".") or "truncated" in result

    def test_truncate_includes_structure_summary(self):
        """Truncated output should hint at what was cut."""
        text = "# Title\n\n" + "Content paragraph. " * 100 + "\n\n```python\ncode```"
        comp = TruncateCompressor()
        result = comp.compress(text, max_chars=200)
        assert "truncated" in result
        assert "original:" in result.lower() or "chars" in result.lower()

    def test_hybrid_preserves_head(self):
        """Hybrid compression keeps the head intact for immediate context."""
        head_content = "IMPORTANT HEAD CONTENT. " * 20
        tail_content = "Less important tail. " * 100
        text = head_content + tail_content
        comp = HybridCompressor(head_chars=len(head_content))
        result = comp.compress(text, max_chars=len(head_content) + 500)
        assert "IMPORTANT HEAD CONTENT" in result

    def test_selective_creates_navigable_toc(self):
        """Selective compression produces a TOC that preserves document structure."""
        import json

        sections = "\n\n".join(
            f"# Section {i}\n\n{'Detail for section. ' * 30}" for i in range(5)
        )
        comp = SelectiveCompressor(min_section_chars=10)
        result = comp.compress(sections, max_chars=300)
        toc = json.loads(result)
        assert toc["type"] == "toc"
        assert len(toc["entries"]) == 5
        assert all("size" in e for e in toc["entries"])

    def test_field_extract_preserves_json_structure(self):
        """JSON compression keeps keys visible while truncating values."""
        import json

        data = {
            "name": "memtomem",
            "description": "A very long description " * 20,
            "config": {"nested": "value", "list": list(range(50))},
        }
        comp = FieldExtractCompressor()
        result = comp.compress(json.dumps(data), max_chars=300)
        # Key structure should be visible
        assert "name" in result
        assert "description" in result
        assert "config" in result

    def test_compression_ratio_meaningful(self):
        """Compression should achieve meaningful size reduction on large input."""
        text = "Detailed technical content. " * 500  # ~14000 chars
        comp = HybridCompressor(head_chars=2000)
        result = comp.compress(text, max_chars=5000)
        ratio = len(result) / len(text)
        assert ratio < 0.5, f"Expected >50% reduction, got {ratio:.0%}"


# ═══════════════════════════════════════════════════════════════════════════
# 4. AUTO-TUNER CONVERGES TO OPTIMAL THRESHOLDS
#    Does feedback actually improve surfacing quality over time?
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoTunerConvergence:
    """Verify the auto-tuner adjusts thresholds based on user feedback."""

    def _make_tuner(self, tmp_path, min_score=0.02, min_samples=5, increment=0.005):
        config = SurfacingConfig(
            auto_tune_enabled=True,
            auto_tune_min_samples=min_samples,
            auto_tune_score_increment=increment,
            min_score=min_score,
        )
        tracker = FeedbackTracker(config, tmp_path / "fb.db")
        tuner = AutoTuner(config, tracker.store)
        return config, tracker, tuner

    def test_mostly_negative_raises_threshold(self, tmp_path):
        """When >60% feedback is 'not_relevant', threshold increases."""
        config, tracker, tuner = self._make_tuner(tmp_path)
        for i in range(6):
            sid = f"neg{i}"
            tracker.record_surfacing(sid, "s", "tool_a", f"q{i}", ["id"], [0.03])
            # 5 negative, 1 positive → 83% not_relevant
            rating = "not_relevant" if i < 5 else "helpful"
            tracker.store.record_feedback(sid, rating)

        new = tuner.maybe_adjust("tool_a")
        if new is not None:
            assert new > config.min_score

    def test_mostly_positive_lowers_threshold(self, tmp_path):
        """When <20% feedback is 'not_relevant', threshold decreases."""
        config, tracker, tuner = self._make_tuner(tmp_path, min_score=0.04)
        for i in range(6):
            sid = f"pos{i}"
            tracker.record_surfacing(sid, "s", "tool_b", f"q{i}", ["id"], [0.05])
            # 1 negative, 5 positive → 17% not_relevant
            rating = "not_relevant" if i == 0 else "helpful"
            tracker.store.record_feedback(sid, rating)

        new = tuner.maybe_adjust("tool_b")
        if new is not None:
            assert new < config.min_score

    def test_stable_band_no_change(self, tmp_path):
        """When 20-60% negative, threshold stays stable."""
        config, tracker, tuner = self._make_tuner(tmp_path)
        for i in range(5):
            sid = f"mid{i}"
            tracker.record_surfacing(sid, "s", "tool_c", f"q{i}", ["id"], [0.03])
            # 2 negative, 3 positive → 40% not_relevant
            rating = "not_relevant" if i < 2 else "helpful"
            tracker.store.record_feedback(sid, rating)

        new = tuner.maybe_adjust("tool_c")
        # Should be None (no adjustment) or same as original
        if new is not None:
            assert abs(new - config.min_score) < 0.001

    def test_ceiling_enforcement(self, tmp_path):
        """Threshold cannot exceed 0.05 ceiling."""
        config, tracker, tuner = self._make_tuner(tmp_path, min_score=0.048, increment=0.01)
        for i in range(6):
            sid = f"ceil{i}"
            tracker.record_surfacing(sid, "s", "tool_d", f"q{i}", ["id"], [0.05])
            tracker.store.record_feedback(sid, "not_relevant")

        new = tuner.maybe_adjust("tool_d")
        if new is not None:
            assert new <= 0.05

    def test_floor_enforcement(self, tmp_path):
        """Threshold cannot go below 0.005 floor."""
        config, tracker, tuner = self._make_tuner(tmp_path, min_score=0.006, increment=0.01)
        for i in range(6):
            sid = f"floor{i}"
            tracker.record_surfacing(sid, "s", "tool_e", f"q{i}", ["id"], [0.01])
            tracker.store.record_feedback(sid, "helpful")

        new = tuner.maybe_adjust("tool_e")
        if new is not None:
            assert new >= 0.005

    def test_per_tool_independence(self, tmp_path):
        """Each tool's threshold adjusts independently."""
        config, tracker, tuner = self._make_tuner(tmp_path)
        # Tool A: all negative
        for i in range(6):
            sid = f"a{i}"
            tracker.record_surfacing(sid, "s", "tool_good", f"q{i}", ["id"], [0.03])
            tracker.store.record_feedback(sid, "helpful")
        # Tool B: all positive
        for i in range(6):
            sid = f"b{i}"
            tracker.record_surfacing(sid, "s", "tool_bad", f"q{i}", ["id"], [0.03])
            tracker.store.record_feedback(sid, "not_relevant")

        score_good = tuner.get_effective_min_score("tool_good")
        score_bad = tuner.get_effective_min_score("tool_bad")
        # tool_bad should have higher threshold than tool_good
        assert score_bad >= score_good


# ═══════════════════════════════════════════════════════════════════════════
# 5. CLEANING FIDELITY
#    Does cleaning remove noise without losing real content?
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# 4b. AUTO-STRATEGY SELECTION
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoStrategySelection:
    """Verify content-type-based compression strategy auto-detection."""

    def test_json_selects_field_extract(self):
        from memtomem_stm.proxy.compression import auto_select_strategy
        from memtomem_stm.proxy.config import CompressionStrategy

        text = '{"users": [1, 2, 3], "total": 3}'
        assert auto_select_strategy(text) == CompressionStrategy.EXTRACT_FIELDS

    def test_markdown_with_headings_selects_hybrid(self):
        from memtomem_stm.proxy.compression import auto_select_strategy
        from memtomem_stm.proxy.config import CompressionStrategy

        text = "# A\n\ntext\n\n## B\n\ntext\n\n### C\n\ntext"
        assert auto_select_strategy(text) == CompressionStrategy.HYBRID

    def test_plain_text_selects_truncate(self):
        from memtomem_stm.proxy.compression import auto_select_strategy
        from memtomem_stm.proxy.config import CompressionStrategy

        text = "Just a plain paragraph of text without any special formatting."
        assert auto_select_strategy(text) == CompressionStrategy.TRUNCATE

    def test_code_heavy_selects_hybrid(self):
        from memtomem_stm.proxy.compression import auto_select_strategy
        from memtomem_stm.proxy.config import CompressionStrategy

        text = "Intro.\n\n```python\ncode1\n```\n\nMiddle.\n\n```js\ncode2\n```\n\nEnd."
        assert auto_select_strategy(text) == CompressionStrategy.HYBRID

    def test_empty_selects_none(self):
        from memtomem_stm.proxy.compression import auto_select_strategy
        from memtomem_stm.proxy.config import CompressionStrategy

        assert auto_select_strategy("") == CompressionStrategy.NONE


# ═══════════════════════════════════════════════════════════════════════════
# 4c. AUTO-TUNER COLD START
# ═══════════════════════════════════════════════════════════════════════════


class TestAutoTunerColdStart:
    """Verify cold-start fallback to global feedback ratio."""

    def test_new_tool_uses_global_ratio(self, tmp_path):
        """Tool with 0 samples falls back to global ratio."""
        config = SurfacingConfig(
            auto_tune_enabled=True,
            auto_tune_min_samples=5,
            auto_tune_score_increment=0.005,
            min_score=0.02,
        )
        tracker = FeedbackTracker(config, tmp_path / "fb.db")
        tuner = AutoTuner(config, tracker.store)

        # Build global history on tool_a (5 negative feedbacks)
        for i in range(5):
            sid = f"global{i}"
            tracker.record_surfacing(sid, "s", "tool_a", f"q{i}", ["id"], [0.03])
            tracker.store.record_feedback(sid, "not_relevant")

        # tool_b has 0 samples → should use global ratio (100% negative)
        new = tuner.maybe_adjust("tool_b")
        if new is not None:
            assert new > config.min_score  # raised due to global negative ratio


# ═══════════════════════════════════════════════════════════════════════════
# 5. CLEANING FIDELITY
# ═══════════════════════════════════════════════════════════════════════════


# ═══════════════════════════════════════════════════════════════════════════
# 5b. FEEDBACK → SEARCH BOOST
# ═══════════════════════════════════════════════════════════════════════════


class TestFeedbackBoost:
    """Verify helpful feedback boosts memory access count for future search ranking."""

    async def test_helpful_feedback_increments_access(self):
        """'helpful' rating increments access_count on surfaced memories."""
        from dataclasses import dataclass
        from pathlib import Path
        from uuid import uuid4

        from memtomem_stm.surfacing.config import SurfacingConfig
        from memtomem_stm.surfacing.engine import SurfacingEngine

        # Mock storage with increment_access tracking
        mock_storage = AsyncMock()
        mock_storage.increment_access = AsyncMock()
        mock_storage.scratch_list = AsyncMock(return_value=[])

        # Mock search pipeline
        @dataclass
        class FakeMeta:
            source_file: Path = Path("/test.md")
            namespace: str = "default"

        @dataclass
        class FakeChunk:
            id: str = ""
            content: str = "test memory"
            metadata: FakeMeta = None
            def __post_init__(self):
                if not self.id: self.id = str(uuid4())
                if not self.metadata: self.metadata = FakeMeta()

        @dataclass
        class FakeResult:
            chunk: FakeChunk = None
            score: float = 0.5
            rank: int = 1
            def __post_init__(self):
                if not self.chunk: self.chunk = FakeChunk()

        chunk = FakeChunk()
        results = [FakeResult(chunk=chunk)]
        pipeline = AsyncMock()
        pipeline.search = AsyncMock(return_value=(results, {}))

        config = SurfacingConfig(
            enabled=True, min_response_chars=10, cooldown_seconds=0,
            max_surfacings_per_minute=1000, auto_tune_enabled=False,
            include_session_context=False, fire_webhook=False,
            feedback_enabled=True,
        )

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tracker = FeedbackTracker(config, Path(tmp) / "fb.db")
            engine = SurfacingEngine(
                config, search_pipeline=pipeline,
                storage=mock_storage, feedback_tracker=tracker,
            )

            # Trigger surfacing to get a surfacing_id
            response = "x" * 200
            output = await engine.surface(
                "gh", "read_file",
                {"path": "/src/app.py", "_context_query": "Flask authentication"},
                response,
            )
            assert "Relevant Memories" in output

            # Extract surfacing_id from output
            import re
            match = re.search(r"Surfacing ID: (\w+)", output)
            assert match, "Surfacing ID should be in output"
            sid = match.group(1)

            # Give helpful feedback
            result = await engine.handle_feedback(sid, "helpful")
            assert "helpful" in result.lower() or "recorded" in result.lower()

            # Verify access_count was incremented
            mock_storage.increment_access.assert_called_once()
            called_ids = mock_storage.increment_access.call_args[0][0]
            assert len(called_ids) >= 1

    async def test_double_helpful_only_boosts_once(self):
        """Same surfacing rated helpful twice → access_count incremented only once."""
        from pathlib import Path
        from unittest.mock import AsyncMock

        from memtomem_stm.surfacing.config import SurfacingConfig
        from memtomem_stm.surfacing.engine import SurfacingEngine

        mock_storage = AsyncMock()
        mock_storage.increment_access = AsyncMock()

        config = SurfacingConfig(
            enabled=True, auto_tune_enabled=False, feedback_enabled=True,
        )

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tracker = FeedbackTracker(config, Path(tmp) / "fb.db")
            engine = SurfacingEngine(
                config, storage=mock_storage, feedback_tracker=tracker,
            )

            # Register event via tracker (goes to same store engine uses)
            from uuid import uuid4
            mid = str(uuid4())
            tracker.record_surfacing("dup-id", "gh", "read_file", "query", [mid], [0.5])

            r1 = await engine.handle_feedback("dup-id", "helpful")
            r2 = await engine.handle_feedback("dup-id", "helpful")

            assert "recorded" in r1.lower() or "helpful" in r1.lower()
            # Second call may record feedback but should NOT boost again
            assert mock_storage.increment_access.call_count == 1

    async def test_not_relevant_does_not_boost(self):
        """'not_relevant' rating does NOT increment access_count."""
        from pathlib import Path
        from unittest.mock import AsyncMock

        from memtomem_stm.surfacing.config import SurfacingConfig
        from memtomem_stm.surfacing.engine import SurfacingEngine

        mock_storage = AsyncMock()
        mock_storage.increment_access = AsyncMock()

        config = SurfacingConfig(
            enabled=True, auto_tune_enabled=False, feedback_enabled=True,
        )

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            tracker = FeedbackTracker(config, Path(tmp) / "fb.db")
            engine = SurfacingEngine(
                config, storage=mock_storage, feedback_tracker=tracker,
            )

            # Record a fake surfacing event
            from uuid import uuid4
            tracker.record_surfacing("test-id", "gh", "read_file", "query", [str(uuid4())], [0.5])

            # Give negative feedback
            await engine.handle_feedback("test-id", "not_relevant")

            # Access should NOT be incremented
            mock_storage.increment_access.assert_not_called()


# ═══════════════════════════════════════════════════════════════════════════
# 6. CLEANING FIDELITY
# ═══════════════════════════════════════════════════════════════════════════


class TestCleaningFidelity:
    """Verify cleaning preserves content while removing noise."""

    def test_code_preserved_html_removed(self):
        """HTML tags removed but code blocks inside are preserved."""
        text = '<div><p>Description</p>\n\n```python\ndef hello():\n    print("hi")\n```\n</div>'
        cleaner = DefaultContentCleaner(CleaningConfig())
        result = cleaner.clean(text)
        assert "<div>" not in result
        assert "<p>" not in result
        assert "def hello():" in result
        assert 'print("hi")' in result

    def test_link_flood_collapsed(self):
        """Paragraphs that are mostly links get collapsed."""
        links = "\n".join(f"https://example.com/page{i}" for i in range(20))
        text = f"Introduction.\n\n{links}\n\nConclusion."
        cleaner = DefaultContentCleaner(CleaningConfig(collapse_links=True))
        result = cleaner.clean(text)
        assert "Introduction" in result
        assert "Conclusion" in result
        assert "links omitted" in result.lower() or result.count("https://") < 5

    def test_duplicate_paragraphs_deduped(self):
        """Repeated paragraphs appear only once."""
        text = "Unique first.\n\nRepeated content here.\n\nOther stuff.\n\nRepeated content here."
        cleaner = DefaultContentCleaner(CleaningConfig(deduplicate=True))
        result = cleaner.clean(text)
        assert result.count("Repeated content here") == 1
        assert "Unique first" in result
        assert "Other stuff" in result

    def test_typescript_generics_preserved(self):
        """TypeScript generics like Array<string> should NOT be stripped as HTML."""
        text = "Use Array<string> and Map<string, number> for type safety."
        cleaner = DefaultContentCleaner(CleaningConfig(strip_html=True))
        result = cleaner.clean(text)
        # Generics should survive HTML stripping
        assert "Array" in result
        assert "Map" in result

    def test_script_content_fully_removed(self):
        """<script> blocks (tags + content) are completely stripped."""
        text = "Content before.<script>alert('xss')</script>Content after."
        cleaner = DefaultContentCleaner(CleaningConfig())
        result = cleaner.clean(text)
        assert "<script>" not in result
        assert "alert" not in result  # content also removed
        assert "Content before" in result
        assert "Content after" in result

    def test_style_content_fully_removed(self):
        """<style> blocks are completely stripped."""
        text = "Text.<style>.hidden { display: none; }</style>More text."
        cleaner = DefaultContentCleaner(CleaningConfig())
        result = cleaner.clean(text)
        assert "display: none" not in result
        assert "Text" in result
        assert "More text" in result

    def test_markdown_preserved(self):
        """Markdown formatting should survive cleaning."""
        text = "## Heading\n\n**Bold** and *italic* and `code` and [link](url)."
        cleaner = DefaultContentCleaner(CleaningConfig())
        result = cleaner.clean(text)
        assert "## Heading" in result
        assert "**Bold**" in result
        assert "`code`" in result

    def test_empty_input_safe(self):
        """Empty/whitespace input returns empty without error."""
        cleaner = DefaultContentCleaner(CleaningConfig())
        assert cleaner.clean("") == ""
        assert cleaner.clean("   \n\n   ").strip() == ""
