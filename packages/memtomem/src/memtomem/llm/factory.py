"""LLM factory: instantiates the right LLMProvider from config."""

from __future__ import annotations

from memtomem.config import LLMConfig
from memtomem.errors import ConfigError

# Provider-specific default models, used when config.model is empty.
_DEFAULT_MODELS: dict[str, str] = {
    "ollama": "llama3.2",
    "openai": "gpt-4o-mini",
    "anthropic": "claude-sonnet-4-20250514",
}


def create_llm(config: LLMConfig) -> object | None:
    """Return the LLM provider for the configured provider name.

    Returns ``None`` when ``config.enabled`` is ``False``.
    """
    if not config.enabled:
        return None

    provider = config.provider.lower()

    # Resolve provider-specific default model when not explicitly set.
    if not config.model:
        default_model = _DEFAULT_MODELS.get(provider)
        if default_model:
            config.model = default_model

    if provider == "ollama":
        from memtomem.llm.ollama import OllamaLLM

        return OllamaLLM(config)

    if provider == "openai":
        from memtomem.llm.openai import OpenAILLM

        return OpenAILLM(config)

    if provider == "anthropic":
        from memtomem.llm.anthropic import AnthropicLLM

        return AnthropicLLM(config)

    raise ConfigError(
        f"Unknown LLM provider: {config.provider!r}. Supported: ollama, openai, anthropic"
    )
