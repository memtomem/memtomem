"""Tests for LLM-based auto-tagging (extract_tags_llm + fallback)."""

from __future__ import annotations

import pytest

from memtomem.errors import LLMError
from memtomem.tools.auto_tag import (
    _extract_tags_with_fallback,
    extract_tags_keyword,
    extract_tags_llm,
)


# ---------------------------------------------------------------------------
# Test helpers — replicated locally so this file is self-contained.
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
# extract_tags_llm
# ---------------------------------------------------------------------------


class TestExtractTagsLlm:
    @pytest.mark.anyio
    async def test_returns_tags(self):
        llm = FakeLLM(response="python, memory, search")
        tags = await extract_tags_llm("some text about python", llm)
        assert tags == ["python", "memory", "search"]

    @pytest.mark.anyio
    async def test_respects_max_tags(self):
        llm = FakeLLM(response="a, b, c, d, e, f")
        tags = await extract_tags_llm("text", llm, max_tags=3)
        assert len(tags) == 3

    @pytest.mark.anyio
    async def test_lowercases(self):
        llm = FakeLLM(response="Python, SEARCH, Memory")
        tags = await extract_tags_llm("text", llm)
        assert tags == ["python", "search", "memory"]

    @pytest.mark.anyio
    async def test_deduplicates(self):
        llm = FakeLLM(response="python, python, search, search")
        tags = await extract_tags_llm("text", llm)
        assert tags == ["python", "search"]

    @pytest.mark.anyio
    @pytest.mark.parametrize("answer", ["", "  \n", "```\n```"])
    async def test_empty_answer_raises(self, answer):
        # A thinking model that spends its token budget before answering
        # returns an empty string (#3382). That is no answer, not "no tags".
        llm = FakeLLM(response=answer)
        with pytest.raises(LLMError, match="empty answer"):
            await extract_tags_llm("text", llm)

    @pytest.mark.anyio
    async def test_strips_code_fence(self):
        llm = FakeLLM(response="```\npython, search\n```")
        tags = await extract_tags_llm("text", llm)
        assert tags == ["python", "search"]

    @pytest.mark.anyio
    async def test_heading_context_in_prompt(self):
        llm = FakeLLM(response="tag1")
        await extract_tags_llm("text", llm, heading_hierarchy=("H1", "H2"))
        assert "H1 > H2" in llm.calls[0]["prompt"]


# ---------------------------------------------------------------------------
# _extract_tags_with_fallback
# ---------------------------------------------------------------------------


class TestExtractTagsWithFallback:
    @pytest.mark.anyio
    async def test_uses_llm_when_available(self):
        llm = FakeLLM(response="llm-tag-1, llm-tag-2")
        tags = await _extract_tags_with_fallback("some text", llm_provider=llm)
        assert "llm-tag-1" in tags

    @pytest.mark.anyio
    async def test_falls_back_no_llm(self):
        text = "python python python memory memory search"
        tags = await _extract_tags_with_fallback(text, llm_provider=None)
        # Should match keyword heuristic
        expected = extract_tags_keyword(text)
        assert tags == expected

    @pytest.mark.anyio
    async def test_falls_back_on_error(self):
        text = "python python python memory memory search"
        tags = await _extract_tags_with_fallback(text, llm_provider=FailingLLM())
        expected = extract_tags_keyword(text)
        assert tags == expected

    @pytest.mark.anyio
    async def test_falls_back_on_empty_answer(self):
        text = "python python python memory memory search"
        expected = extract_tags_keyword(text)
        assert expected  # an empty heuristic result would not show the fallback ran
        llm = FakeLLM(response="")
        tags = await _extract_tags_with_fallback(text, llm_provider=llm)
        assert len(llm.calls) == 1
        assert tags == expected
