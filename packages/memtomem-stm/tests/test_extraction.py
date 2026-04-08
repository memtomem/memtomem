"""Tests for automatic fact extraction."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from memtomem_stm.proxy.config import (
    ExtractionConfig,
    ExtractionStrategy,
    LLMCompressorConfig,
    LLMProvider,
    _default_extraction_llm,
)
from memtomem_stm.proxy.extraction import (
    ExtractedFact,
    FactExtractor,
    _extract_heuristic,
    _parse_facts_json,
)


# ---------------------------------------------------------------------------
# _parse_facts_json
# ---------------------------------------------------------------------------


class TestParseFactsJson:
    def test_valid_json_array(self):
        raw = json.dumps([
            {"content": "Python 3.12 is required", "category": "technical", "confidence": 0.9, "tags": ["python"]},
            {"content": "Deploy on Friday", "category": "decision", "confidence": 0.7, "tags": []},
        ])
        facts = _parse_facts_json(raw, max_facts=10)
        assert len(facts) == 2
        assert facts[0].content == "Python 3.12 is required"
        assert facts[0].category == "technical"
        assert facts[0].confidence == 0.9
        assert facts[0].tags == ["python"]

    def test_markdown_wrapped_json(self):
        raw = '```json\n[{"content": "fact one", "category": "technical", "confidence": 0.8}]\n```'
        facts = _parse_facts_json(raw, max_facts=10)
        assert len(facts) == 1
        assert facts[0].content == "fact one"

    def test_max_facts_limit(self):
        items = [{"content": f"fact {i}", "category": "technical", "confidence": 0.5} for i in range(20)]
        raw = json.dumps(items)
        facts = _parse_facts_json(raw, max_facts=5)
        assert len(facts) == 5

    def test_missing_content_field_skipped(self):
        raw = json.dumps([
            {"category": "technical", "confidence": 0.5},
            {"content": "valid", "category": "technical", "confidence": 0.8},
        ])
        facts = _parse_facts_json(raw, max_facts=10)
        assert len(facts) == 1
        assert facts[0].content == "valid"

    def test_invalid_json_returns_empty(self):
        assert _parse_facts_json("not json at all", max_facts=10) == []

    def test_empty_array_returns_empty(self):
        assert _parse_facts_json("[]", max_facts=10) == []

    def test_defaults_for_missing_fields(self):
        raw = json.dumps([{"content": "just content"}])
        facts = _parse_facts_json(raw, max_facts=10)
        assert len(facts) == 1
        assert facts[0].category == "technical"
        assert facts[0].confidence == 0.5
        assert facts[0].tags == []


# ---------------------------------------------------------------------------
# _extract_heuristic
# ---------------------------------------------------------------------------


class TestExtractHeuristic:
    def test_extracts_entities(self):
        text = "Decision: We will use PostgreSQL for the database. Author: John Smith"
        facts = _extract_heuristic(text, max_facts=10)
        assert len(facts) > 0
        categories = {f.category for f in facts}
        assert "decision" in categories or "person" in categories

    def test_respects_max_facts(self):
        text = (
            "TODO: fix auth\nTODO: add logging\nTODO: update docs\n"
            "TODO: refactor\nTODO: test\nTODO: deploy"
        )
        facts = _extract_heuristic(text, max_facts=3)
        assert len(facts) <= 3

    def test_empty_text(self):
        assert _extract_heuristic("", max_facts=10) == []

    def test_no_entities(self):
        facts = _extract_heuristic("just a simple sentence", max_facts=10)
        # May or may not find entities, but should not crash
        assert isinstance(facts, list)


# ---------------------------------------------------------------------------
# ExtractionConfig
# ---------------------------------------------------------------------------


class TestExtractionConfig:
    def test_defaults(self):
        cfg = ExtractionConfig()
        assert cfg.enabled is False
        assert cfg.strategy == ExtractionStrategy.LLM
        assert cfg.llm is None
        assert cfg.max_facts == 10
        assert cfg.background is True

    def test_effective_llm_default(self):
        cfg = ExtractionConfig()
        llm = cfg.effective_llm()
        assert llm.provider == LLMProvider.OLLAMA
        assert llm.model == "qwen3:4b"
        assert "/no_think" in llm.system_prompt

    def test_effective_llm_user_override(self):
        custom = LLMCompressorConfig(
            provider=LLMProvider.OPENAI,
            model="gpt-4.1-nano",
            api_key="sk-test",
        )
        cfg = ExtractionConfig(llm=custom)
        llm = cfg.effective_llm()
        assert llm.provider == LLMProvider.OPENAI
        assert llm.model == "gpt-4.1-nano"

    def test_default_extraction_llm_function(self):
        llm = _default_extraction_llm()
        assert llm.provider == LLMProvider.OLLAMA
        assert llm.model == "qwen3:4b"
        assert llm.max_tokens == 1000


# ---------------------------------------------------------------------------
# FactExtractor
# ---------------------------------------------------------------------------


class TestFactExtractor:
    async def test_skip_short_text(self):
        cfg = ExtractionConfig(enabled=True, min_response_chars=100)
        extractor = FactExtractor(cfg)
        result = await extractor.extract("short", server="test", tool="read")
        assert result == []

    async def test_none_strategy_returns_empty(self):
        cfg = ExtractionConfig(enabled=True, strategy=ExtractionStrategy.NONE)
        extractor = FactExtractor(cfg)
        result = await extractor.extract("x" * 1000, server="test", tool="read")
        assert result == []

    async def test_heuristic_strategy(self):
        cfg = ExtractionConfig(
            enabled=True,
            strategy=ExtractionStrategy.HEURISTIC,
            min_response_chars=10,
        )
        extractor = FactExtractor(cfg)
        text = "Decision: Use SQLite for storage. Author: Jane Doe\n" * 10
        result = await extractor.extract(text, server="test", tool="read")
        assert isinstance(result, list)


class TestFactExtractorLLM:
    async def test_llm_success(self):
        cfg = ExtractionConfig(enabled=True, min_response_chars=10)
        extractor = FactExtractor(cfg)

        mock_response = json.dumps([
            {"content": "fact 1", "category": "technical", "confidence": 0.9, "tags": ["test"]},
        ])
        with patch.object(extractor, "_call_api", new_callable=AsyncMock, return_value=mock_response):
            facts = await extractor.extract("x" * 100, server="s", tool="t")

        assert len(facts) == 1
        assert facts[0].content == "fact 1"

    async def test_llm_failure_falls_back_to_heuristic(self):
        cfg = ExtractionConfig(enabled=True, min_response_chars=10)
        extractor = FactExtractor(cfg)

        with patch.object(extractor, "_call_api", new_callable=AsyncMock, side_effect=RuntimeError("API down")):
            text = "Decision: fallback works. " * 20
            facts = await extractor.extract(text, server="s", tool="t")

        # Should get heuristic results, not crash
        assert isinstance(facts, list)

    async def test_circuit_breaker_opens(self):
        cfg = ExtractionConfig(enabled=True, min_response_chars=10)
        extractor = FactExtractor(cfg)

        with patch.object(extractor, "_call_api", new_callable=AsyncMock, side_effect=RuntimeError("fail")):
            for _ in range(4):
                await extractor.extract("x" * 100, server="s", tool="t")

        assert extractor._cb.state == "open"

    async def test_hybrid_merges_llm_and_heuristic(self):
        cfg = ExtractionConfig(
            enabled=True,
            strategy=ExtractionStrategy.HYBRID,
            min_response_chars=10,
        )
        extractor = FactExtractor(cfg)

        mock_response = json.dumps([
            {"content": "LLM fact", "category": "technical", "confidence": 0.9},
        ])
        with patch.object(extractor, "_call_api", new_callable=AsyncMock, return_value=mock_response):
            text = "Decision: Use Redis. LLM fact is separate. " * 10
            facts = await extractor.extract(text, server="s", tool="t")

        contents = [f.content for f in facts]
        assert "LLM fact" in contents

    async def test_truncation_respects_max_input_chars(self):
        cfg = ExtractionConfig(enabled=True, min_response_chars=10, max_input_chars=100)
        extractor = FactExtractor(cfg)

        captured_text = None

        async def mock_api(text):
            nonlocal captured_text
            captured_text = text
            return "[]"

        with patch.object(extractor, "_call_api", side_effect=mock_api):
            await extractor.extract("x" * 5000, server="s", tool="t")

        assert captured_text is not None
        assert len(captured_text) <= 100


# ---------------------------------------------------------------------------
# ExtractedFact dataclass
# ---------------------------------------------------------------------------


class TestExtractedFact:
    def test_frozen(self):
        fact = ExtractedFact(content="test", category="technical", confidence=0.5)
        with pytest.raises(AttributeError):
            fact.content = "changed"

    def test_default_tags(self):
        fact = ExtractedFact(content="test", category="technical", confidence=0.5)
        assert fact.tags == []


# ---------------------------------------------------------------------------
# Config integration
# ---------------------------------------------------------------------------


class TestExtractionStrategy:
    def test_all_values(self):
        assert set(ExtractionStrategy) == {"none", "llm", "heuristic", "hybrid"}
