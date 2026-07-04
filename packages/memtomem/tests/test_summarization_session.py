"""Direct unit tests for ``summarization/session.py`` (#1620).

The module owns only the LLM call (prompt build + a single
``llm.generate`` await). Chunk selection, gating thresholds, and the
archive-chunk persistence live at the ``mem_session_end`` call site and
are covered end-to-end in ``test_sessions.py`` /
``test_session_summary_rescue.py`` — these tests pin the helper's own
contract without spinning up storage or a server context.
"""

from pathlib import Path

import pytest

from memtomem.models import Chunk, ChunkMetadata
from memtomem.summarization.session import (
    SessionTooLargeError,
    _format_chunks_for_prompt,
    _load_system_prompt,
    summarize_session,
)


class _FakeLLM:
    """Minimal ``LLMProvider`` stand-in — records calls, fixed response."""

    def __init__(self, response: str = "SUMMARY-OUTPUT") -> None:
        self.response = response
        self.calls: list[tuple[str, str, int]] = []

    async def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        self.calls.append((prompt, system, max_tokens))
        return self.response

    async def close(self) -> None:
        return None


def _chunk(
    content: str,
    source: str = "notes/entry.md",
    headings: tuple[str, ...] = (),
) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=Path(source), heading_hierarchy=headings),
    )


class TestFormatChunksForPrompt:
    def test_numbers_chunks_and_renders_header_bits(self):
        body = _format_chunks_for_prompt(
            [
                _chunk("first body", source="notes/a.md", headings=("Top", "Sub")),
                _chunk("second body", source="notes/b.md"),
            ]
        )

        blocks = body.split("\n\n")
        assert blocks[0] == "--- chunk 1 | notes/a.md | Top > Sub ---\nfirst body"
        assert blocks[1] == "--- chunk 2 | notes/b.md ---\nsecond body"

    def test_omits_heading_segment_when_hierarchy_empty(self):
        body = _format_chunks_for_prompt([_chunk("plain", headings=())])
        header = body.split("\n")[0]
        assert header.count("|") == 1  # "chunk 1 | <source>" only, no heading segment

    def test_strips_chunk_content_whitespace(self):
        body = _format_chunks_for_prompt([_chunk("\n\n  padded content  \n\n")])
        assert body.endswith("---\npadded content")


class TestSummarizeSession:
    @pytest.mark.asyncio
    async def test_empty_chunks_returns_empty_without_llm_call(self):
        llm = _FakeLLM()

        result = await summarize_session("sess-1", [], llm=llm)

        assert result == ""
        assert llm.calls == []

    @pytest.mark.asyncio
    async def test_oversize_body_raises_before_llm_call(self):
        llm = _FakeLLM()
        chunks = [_chunk("x" * 200)]

        with pytest.raises(SessionTooLargeError, match="max_input_chars=100"):
            await summarize_session("sess-big", chunks, llm=llm, max_input_chars=100)

        assert llm.calls == []

    @pytest.mark.asyncio
    async def test_happy_path_prompt_shape_and_passthrough(self):
        llm = _FakeLLM(response="A tidy summary.")
        chunks = [
            _chunk("alpha content", source="notes/a.md"),
            _chunk("beta content", source="notes/b.md"),
        ]

        result = await summarize_session("sess-42", chunks, llm=llm, max_tokens=77)

        assert result == "A tidy summary."
        assert len(llm.calls) == 1
        prompt, system, max_tokens = llm.calls[0]
        assert prompt.startswith("Session id: sess-42\n")
        assert "(2 total, newest first)" in prompt
        assert "alpha content" in prompt and "beta content" in prompt
        assert system == _load_system_prompt()
        assert max_tokens == 77

    @pytest.mark.asyncio
    async def test_strips_code_fence_wrapper_from_response(self):
        llm = _FakeLLM(response="```markdown\nfenced summary\n```")

        result = await summarize_session("sess-f", [_chunk("body")], llm=llm)

        assert result == "fenced summary"

    @pytest.mark.asyncio
    async def test_whitespace_only_response_returns_empty(self):
        """Callers treat ``not summary`` as "skip auto-summary" — a
        whitespace-only model response must collapse to the empty string.
        """
        llm = _FakeLLM(response="   \n\t  ")

        result = await summarize_session("sess-w", [_chunk("body")], llm=llm)

        assert result == ""


def test_load_system_prompt_reads_packaged_resource():
    prompt = _load_system_prompt()
    assert prompt.strip(), "packaged summarization/prompts/session.md must be non-empty"
