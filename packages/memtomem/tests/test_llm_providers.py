"""Tests for memtomem LLM provider infrastructure.

Covers:
  - LLMProvider protocol conformance
  - OllamaLLM, OpenAILLM, AnthropicLLM (mocked httpx)
  - create_llm factory + provider-default model resolution
  - make_llm_summary with FakeLLM
  - Consolidation fallback paths
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from memtomem.config import LLMConfig
from memtomem.errors import ConfigError, LLMError
from memtomem.llm.anthropic import AnthropicLLM
from memtomem.llm.factory import _DEFAULT_MODELS, create_llm
from memtomem.llm.ollama import OllamaLLM
from memtomem.llm.openai import OpenAILLM


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ollama_llm_config(**overrides) -> LLMConfig:
    defaults = dict(
        enabled=True,
        provider="ollama",
        model="llama3.2",
        base_url="http://localhost:11434",
        api_key="",
        max_tokens=1024,
        timeout=60.0,
    )
    defaults.update(overrides)
    return LLMConfig(**defaults)


def _openai_llm_config(**overrides) -> LLMConfig:
    defaults = dict(
        enabled=True,
        provider="openai",
        model="gpt-4o-mini",
        base_url="https://api.openai.com",
        api_key="sk-test",
        max_tokens=1024,
        timeout=60.0,
    )
    defaults.update(overrides)
    return LLMConfig(**defaults)


def _anthropic_llm_config(**overrides) -> LLMConfig:
    defaults = dict(
        enabled=True,
        provider="anthropic",
        model="claude-sonnet-4-20250514",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-test",
        max_tokens=1024,
        timeout=60.0,
    )
    defaults.update(overrides)
    return LLMConfig(**defaults)


def _make_httpx_response(
    status_code: int = 200, json_data: dict | None = None, headers: dict | None = None
) -> httpx.Response:
    resp = httpx.Response(
        status_code=status_code,
        json=json_data,
        headers=headers or {},
        request=httpx.Request("POST", "http://test"),
    )
    return resp


class FakeLLM:
    """Minimal LLMProvider implementation for testing consumers."""

    def __init__(self, response: str = "Fake summary") -> None:
        self._response = response
        self.calls: list[dict] = []

    async def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        self.calls.append({"prompt": prompt, "system": system, "max_tokens": max_tokens})
        return self._response

    async def close(self) -> None:
        pass


class FailingLLM:
    """LLM provider that always raises LLMError."""

    async def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str:
        raise LLMError("simulated LLM failure")

    async def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 1. Protocol conformance
# ---------------------------------------------------------------------------


class TestLLMProviderProtocol:
    def test_ollama_has_required_methods(self):
        llm = OllamaLLM(_ollama_llm_config())
        assert hasattr(llm, "generate")
        assert hasattr(llm, "close")

    def test_openai_has_required_methods(self):
        llm = OpenAILLM(_openai_llm_config())
        assert hasattr(llm, "generate")
        assert hasattr(llm, "close")

    def test_anthropic_has_required_methods(self):
        llm = AnthropicLLM(_anthropic_llm_config())
        assert hasattr(llm, "generate")
        assert hasattr(llm, "close")

    def test_fake_llm_satisfies_protocol(self):
        llm = FakeLLM()
        assert hasattr(llm, "generate")
        assert hasattr(llm, "close")


# ---------------------------------------------------------------------------
# 2. OllamaLLM — mocked httpx
# ---------------------------------------------------------------------------


class TestOllamaLLM:
    @pytest.mark.anyio
    async def test_generate_returns_response_text(self):
        config = _ollama_llm_config()
        llm = OllamaLLM(config)
        fake_resp = _make_httpx_response(json_data={"response": "Hello from Ollama"})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = fake_resp
        llm._client = mock_client

        result = await llm.generate("test prompt", system="sys")
        assert result == "Hello from Ollama"
        mock_client.post.assert_called_once()
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "/api/generate"
        payload = call_args[1]["json"]
        assert payload["model"] == "llama3.2"
        assert payload["prompt"] == "test prompt"
        assert payload["system"] == "sys"

    @pytest.mark.anyio
    async def test_connection_error_raises_llm_error(self):
        config = _ollama_llm_config()
        llm = OllamaLLM(config)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.side_effect = httpx.ConnectError("refused")
        llm._client = mock_client

        with pytest.raises(LLMError, match="Cannot connect to Ollama"):
            await llm.generate("test")

    @pytest.mark.anyio
    async def test_close_clears_client(self):
        llm = OllamaLLM(_ollama_llm_config())
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        llm._client = mock_client
        await llm.close()
        mock_client.aclose.assert_awaited_once()
        assert llm._client is None


# ---------------------------------------------------------------------------
# 3. OpenAILLM — mocked httpx
# ---------------------------------------------------------------------------


class TestOpenAILLM:
    @pytest.mark.anyio
    async def test_generate_returns_message_content(self):
        config = _openai_llm_config()
        llm = OpenAILLM(config)
        fake_resp = _make_httpx_response(
            json_data={"choices": [{"message": {"content": "Hello from OpenAI"}}]}
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = fake_resp
        llm._client = mock_client

        result = await llm.generate("test prompt")
        assert result == "Hello from OpenAI"

    @pytest.mark.anyio
    async def test_401_raises_auth_error(self):
        config = _openai_llm_config()
        llm = OpenAILLM(config)
        fake_resp = _make_httpx_response(status_code=401)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = fake_resp
        llm._client = mock_client

        with pytest.raises(LLMError, match="authentication failed"):
            await llm.generate("test")

    @pytest.mark.anyio
    async def test_429_raises_rate_limit_error(self):
        config = _openai_llm_config()
        llm = OpenAILLM(config)
        fake_resp = _make_httpx_response(status_code=429, headers={"Retry-After": "5"})
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = fake_resp
        llm._client = mock_client

        with pytest.raises(LLMError, match="rate limit"):
            await llm.generate("test")


# ---------------------------------------------------------------------------
# 4. AnthropicLLM — mocked httpx
# ---------------------------------------------------------------------------


class TestAnthropicLLM:
    @pytest.mark.anyio
    async def test_generate_returns_content_text(self):
        config = _anthropic_llm_config()
        llm = AnthropicLLM(config)
        fake_resp = _make_httpx_response(
            json_data={"content": [{"type": "text", "text": "Hello from Claude"}]}
        )
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = fake_resp
        llm._client = mock_client

        result = await llm.generate("test prompt", system="sys prompt")
        assert result == "Hello from Claude"
        call_args = mock_client.post.call_args
        assert call_args[0][0] == "/v1/messages"
        payload = call_args[1]["json"]
        assert payload["system"] == "sys prompt"

    @pytest.mark.anyio
    async def test_ollama_base_url_overridden_to_anthropic_default(self):
        config = _anthropic_llm_config(base_url="http://localhost:11434")
        llm = AnthropicLLM(config)
        client = llm._get_client()
        assert str(client.base_url).rstrip("/") == "https://api.anthropic.com"
        await llm.close()

    @pytest.mark.anyio
    async def test_401_raises_auth_error(self):
        config = _anthropic_llm_config()
        llm = AnthropicLLM(config)
        fake_resp = _make_httpx_response(status_code=401)
        mock_client = AsyncMock(spec=httpx.AsyncClient)
        mock_client.post.return_value = fake_resp
        llm._client = mock_client

        with pytest.raises(LLMError, match="authentication failed"):
            await llm.generate("test")


# ---------------------------------------------------------------------------
# 5. create_llm factory
# ---------------------------------------------------------------------------


class TestCreateLLM:
    def test_disabled_returns_none(self):
        config = LLMConfig(enabled=False)
        assert create_llm(config) is None

    def test_ollama_provider(self):
        config = _ollama_llm_config()
        llm = create_llm(config)
        assert isinstance(llm, OllamaLLM)

    def test_openai_provider(self):
        config = _openai_llm_config()
        llm = create_llm(config)
        assert isinstance(llm, OpenAILLM)

    def test_anthropic_provider(self):
        config = _anthropic_llm_config()
        llm = create_llm(config)
        assert isinstance(llm, AnthropicLLM)

    def test_unknown_provider_raises(self):
        config = _ollama_llm_config(provider="unknown")
        with pytest.raises(ConfigError, match="Unknown LLM provider"):
            create_llm(config)

    def test_empty_model_resolved_to_provider_default(self):
        for provider, expected_model in _DEFAULT_MODELS.items():
            config = LLMConfig(enabled=True, provider=provider, model="")
            create_llm(config)
            assert config.model == expected_model


# ---------------------------------------------------------------------------
# 6. make_llm_summary with FakeLLM
# ---------------------------------------------------------------------------


class TestMakeLLMSummary:
    @pytest.mark.anyio
    async def test_produces_summary_with_llm_strategy(self):
        from datetime import datetime, timezone
        from uuid import uuid4

        from memtomem.models import Chunk, ChunkMetadata, ChunkType
        from memtomem.tools.consolidation_engine import make_llm_summary

        chunks = [
            Chunk(
                id=uuid4(),
                content="Decision: use BM25 as default",
                metadata=ChunkMetadata(
                    source_file=Path("/notes/design.md"),
                    chunk_type=ChunkType.MARKDOWN_SECTION,
                ),
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                embedding=[],
            ),
            Chunk(
                id=uuid4(),
                content="Action: implement NoopEmbedder",
                metadata=ChunkMetadata(
                    source_file=Path("/notes/design.md"),
                    chunk_type=ChunkType.MARKDOWN_SECTION,
                ),
                created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
                embedding=[],
            ),
        ]

        fake = FakeLLM(response="## Summary\n- BM25 default\n- NoopEmbedder")
        result = await make_llm_summary(chunks, Path("/notes/design.md"), fake)

        assert "Strategy: llm" in result
        assert "Consolidated: design.md" in result
        assert "Source hash:" in result
        assert "BM25 default" in result
        assert len(fake.calls) == 1

    @pytest.mark.anyio
    async def test_empty_chunks_raises(self):
        from memtomem.tools.consolidation_engine import make_llm_summary

        with pytest.raises(ValueError, match="cannot summarize empty"):
            await make_llm_summary([], Path("/notes/x.md"), FakeLLM())


# ---------------------------------------------------------------------------
# 7. Consolidation fallback: LLM=None → heuristic, LLM error → heuristic
# ---------------------------------------------------------------------------


class TestConsolidationFallback:
    @pytest.mark.anyio
    async def test_heuristic_summary_has_heuristic_strategy(self):
        from datetime import datetime, timezone
        from uuid import uuid4

        from memtomem.models import Chunk, ChunkMetadata, ChunkType
        from memtomem.tools.consolidation_engine import make_heuristic_summary

        chunks = [
            Chunk(
                id=uuid4(),
                content="Some content here",
                metadata=ChunkMetadata(
                    source_file=Path("/a.md"),
                    chunk_type=ChunkType.MARKDOWN_SECTION,
                ),
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                embedding=[],
            ),
        ]
        result = make_heuristic_summary(chunks, Path("/a.md"))
        assert "Strategy: heuristic" in result

    @pytest.mark.anyio
    async def test_llm_failure_produces_heuristic_fallback(self):
        """When LLM raises LLMError, make_llm_summary raises — caller handles fallback."""
        from memtomem.tools.consolidation_engine import make_llm_summary

        from datetime import datetime, timezone
        from uuid import uuid4

        from memtomem.models import Chunk, ChunkMetadata, ChunkType

        chunks = [
            Chunk(
                id=uuid4(),
                content="Test chunk",
                metadata=ChunkMetadata(
                    source_file=Path("/a.md"),
                    chunk_type=ChunkType.MARKDOWN_SECTION,
                ),
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                embedding=[],
            ),
        ]

        failing = FailingLLM()
        with pytest.raises(LLMError, match="simulated LLM failure"):
            await make_llm_summary(chunks, Path("/a.md"), failing)
