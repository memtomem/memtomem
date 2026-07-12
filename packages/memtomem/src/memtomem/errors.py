"""Custom exceptions for memtomem."""


class Mem2MemError(Exception):
    """Base exception."""


class StorageError(Mem2MemError):
    """Storage backend error."""


class StorageStartupError(StorageError):
    """Classified, path-safe storage initialization failure."""

    def __init__(
        self,
        *,
        reason_code: str,
        stage: str,
        retryable: bool = False,
        sqlite_code: int | None = None,
    ) -> None:
        self.reason_code = reason_code
        self.stage = stage
        self.retryable = retryable
        self.sqlite_code = sqlite_code
        super().__init__(
            f"Storage startup failed ({reason_code}, stage={stage}). "
            "Check the configured database directory and its SQLite WAL/SHM permissions."
        )


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


class SchemaDowngradeError(StorageError):
    """Raised when the DB's stored schema version is newer than this binary's.

    A newer memtomem release ran migrations this binary does not know about.
    Migrations are additive/idempotent, so same-or-older versions always
    pass — this fence only blocks the downgrade direction, where an old
    binary could misread structures it has never seen. Fail-fast at open
    with an upgrade remediation instead of undefined behavior later.
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
