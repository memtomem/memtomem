"""Custom exceptions for memtomem."""


class Mem2MemError(Exception):
    """Base exception."""


class StorageError(Mem2MemError):
    """Storage backend error."""


class EmbeddingError(Mem2MemError):
    """Embedding provider error."""


class ChunkingError(Mem2MemError):
    """Chunking error."""


class IndexingError(Mem2MemError):
    """Indexing error."""


class ConfigError(Mem2MemError):
    """Configuration error."""


class RetryableError(Exception):
    """Error that can be resolved by retrying (e.g., network timeout, rate limit)."""


class PermanentError(Exception):
    """Error that will not resolve with retries (e.g., invalid API key, malformed input)."""
