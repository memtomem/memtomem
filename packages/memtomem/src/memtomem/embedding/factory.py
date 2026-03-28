"""Embedder factory: instantiates the right EmbeddingProvider from config."""

from __future__ import annotations

from memtomem.config import EmbeddingConfig
from memtomem.errors import ConfigError


def create_embedder(config: EmbeddingConfig) -> object:
    """Return the embedding provider for the configured provider name."""
    provider = config.provider.lower()

    if provider == "ollama":
        from memtomem.embedding.ollama import OllamaEmbedder

        return OllamaEmbedder(config)

    if provider == "openai":
        from memtomem.embedding.openai import OpenAIEmbedder

        return OpenAIEmbedder(config)

    raise ConfigError(f"Unknown embedding provider: {config.provider!r}. Supported: ollama, openai")
