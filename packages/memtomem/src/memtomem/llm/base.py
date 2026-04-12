"""LLM provider protocol."""

from __future__ import annotations

from typing import Protocol


class LLMProvider(Protocol):
    async def generate(self, prompt: str, *, system: str = "", max_tokens: int = 1024) -> str: ...

    async def close(self) -> None: ...
