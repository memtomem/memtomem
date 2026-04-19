"""Custom exceptions for memtomem."""


class Mem2MemError(Exception):
    """Base exception."""


class StorageError(Mem2MemError):
    """Storage backend error."""


class EmbeddingDimensionMismatchError(StorageError):
    """Raised when stored embedding dimension is 0 but a real provider is configured.

    This happens when a DB was initialized with ``provider=none`` (NoopEmbedder,
    BM25-only) and the config was later switched to a real provider like
    ``onnx``/``ollama`` without running ``mm embedding-reset``. In that state
    ``chunks_vec`` was never created (it only exists when dimension > 0) but
    the runtime embedder produces real vectors, so every ``upsert_chunks``
    fails with ``no such table: chunks_vec``. Fail-fast at startup with a
    remediation message instead of letting the cascade happen.
    """


class EmbeddingError(Mem2MemError):
    """Embedding provider error."""


class ChunkingError(Mem2MemError):
    """Chunking error."""


class IndexingError(Mem2MemError):
    """Indexing error."""


class LLMError(Mem2MemError):
    """LLM provider error."""


class ConfigError(Mem2MemError):
    """Configuration error."""


class RetryableError(Exception):
    """Error that can be resolved by retrying (e.g., network timeout, rate limit)."""


class PermanentError(Exception):
    """Error that will not resolve with retries (e.g., invalid API key, malformed input)."""
