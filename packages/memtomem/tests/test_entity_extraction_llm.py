"""Tests for LLM-based entity extraction."""

from __future__ import annotations

import pytest

from memtomem.errors import LLMError
from memtomem.tools.entity_extraction import (
    ExtractedEntity,
    _parse_entity_response,
    extract_entities,
    extract_entities_llm,
    extract_entities_with_llm,
)


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


class FakeLLM:
    def __init__(self, response: str = "") -> None:
        self._response = response
        self.calls: list[dict] = []

    async def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        self.calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens})
        return self._response

    async def close(self) -> None:
        pass


class FailingLLM:
    async def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        raise LLMError("simulated LLM failure")

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# _parse_entity_response
# ---------------------------------------------------------------------------


class TestParseEntityResponse:
    def test_well_formed_response(self):
        raw = "person|John Doe|0.9\ndate|2026-01-15|0.95\ntechnology|Python|0.85"
        entities = _parse_entity_response(raw)
        assert len(entities) == 3
        assert entities[0] == ExtractedEntity("person", "John Doe", 0.9, 0)
        assert entities[1] == ExtractedEntity("date", "2026-01-15", 0.95, 0)

    def test_filters_by_type(self):
        raw = "person|Jane|0.8\ntechnology|Python|0.9"
        entities = _parse_entity_response(raw, entity_types={"person"})
        assert len(entities) == 1
        assert entities[0].entity_type == "person"

    def test_none_response(self):
        assert _parse_entity_response("NONE") == []

    def test_empty_response(self):
        assert _parse_entity_response("") == []

    def test_malformed_lines_skipped(self):
        raw = "person|John|0.8\ngarbage without pipes\ntechnology|React|0.7"
        entities = _parse_entity_response(raw)
        assert len(entities) == 2

    def test_missing_confidence_defaults(self):
        raw = "person|John Doe"
        entities = _parse_entity_response(raw)
        assert len(entities) == 1
        assert entities[0].confidence == 0.7

    def test_invalid_confidence_defaults(self):
        raw = "person|John|notanumber"
        entities = _parse_entity_response(raw)
        assert entities[0].confidence == 0.7

    def test_confidence_clamped(self):
        raw = "person|John|2.5"
        entities = _parse_entity_response(raw)
        assert entities[0].confidence == 1.0

    def test_strips_code_fence(self):
        raw = "```\nperson|John|0.8\n```"
        entities = _parse_entity_response(raw)
        assert len(entities) == 1

    def test_unknown_type_skipped(self):
        raw = "unknown_type|value|0.9\nperson|Jane|0.8"
        entities = _parse_entity_response(raw)
        assert len(entities) == 1
        assert entities[0].entity_type == "person"

    def test_value_truncated_at_200(self):
        long_value = "x" * 300
        raw = f"concept|{long_value}|0.7"
        entities = _parse_entity_response(raw)
        assert len(entities[0].entity_value) == 200


# ---------------------------------------------------------------------------
# extract_entities_llm
# ---------------------------------------------------------------------------


class TestExtractEntitiesLlm:
    @pytest.mark.anyio
    async def test_returns_parsed_entities(self):
        llm = FakeLLM(response="person|Alice|0.9\ntechnology|Python|0.85")
        entities = await extract_entities_llm("Alice uses Python", llm)
        assert len(entities) == 2
        assert entities[0].entity_value == "Alice"

    @pytest.mark.anyio
    async def test_filters_by_type(self):
        llm = FakeLLM(response="person|Alice|0.9\ntechnology|Python|0.85")
        entities = await extract_entities_llm("text", llm, entity_types=["person"])
        assert len(entities) == 1

    @pytest.mark.anyio
    async def test_none_answer_is_no_entities(self):
        llm = FakeLLM(response="NONE")
        entities = await extract_entities_llm("text", llm)
        assert entities == []

    @pytest.mark.anyio
    @pytest.mark.parametrize("answer", ["", "  \n", "```\n```"])
    async def test_empty_answer_raises(self, answer):
        # A thinking model that spends its token budget before answering
        # returns an empty string (#3382). The prompt's answer for "no
        # entities" is NONE, so an empty string is no answer.
        llm = FakeLLM(response=answer)
        with pytest.raises(LLMError, match="empty answer"):
            await extract_entities_llm("text", llm)

    @pytest.mark.anyio
    async def test_type_hint_in_prompt(self):
        llm = FakeLLM(response="NONE")
        await extract_entities_llm("text", llm, entity_types=["person", "date"])
        assert "person, date" in llm.calls[0]["prompt"]


# ---------------------------------------------------------------------------
# extract_entities_with_llm (fallback logic)
# ---------------------------------------------------------------------------


class TestExtractEntitiesWithLlm:
    @pytest.mark.anyio
    async def test_uses_llm_when_available(self):
        llm = FakeLLM(response="person|Alice|0.95")
        entities = await extract_entities_with_llm("Alice joined", llm_provider=llm)
        assert len(entities) == 1
        assert entities[0].entity_value == "Alice"

    @pytest.mark.anyio
    async def test_falls_back_no_llm(self):
        text = "Decision: use Python for the backend"
        entities = await extract_entities_with_llm(text, llm_provider=None)
        # Should get heuristic results
        expected = extract_entities(text)
        assert entities == expected

    @pytest.mark.anyio
    async def test_falls_back_on_error(self):
        text = "Decision: use Python for the backend"
        entities = await extract_entities_with_llm(text, llm_provider=FailingLLM())
        expected = extract_entities(text)
        assert entities == expected

    @pytest.mark.anyio
    async def test_falls_back_on_empty_answer(self):
        text = "Decision: use Python for the backend"
        expected = extract_entities(text)
        assert expected  # an empty heuristic result would not show the fallback ran
        llm = FakeLLM(response="")
        entities = await extract_entities_with_llm(text, llm_provider=llm)
        assert len(llm.calls) == 1
        assert entities == expected

    @pytest.mark.anyio
    async def test_none_answer_does_not_fall_back(self):
        # Passes before and after #3382: it keeps the empty-answer rule from
        # widening to "the parse produced nothing", which would overrule the
        # model's NONE with the heuristic.
        text = "Decision: use Python for the backend"
        assert extract_entities(text)
        llm = FakeLLM(response="NONE")
        entities = await extract_entities_with_llm(text, llm_provider=llm)
        assert entities == []
