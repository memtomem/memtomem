"""langchain-memtomem — a LangGraph ``BaseStore`` ranked by the memtomem pipeline.

See :class:`MemtomemPipelineStore` and ``docs/protocol.md`` (ADR-0042 in the
memtomem repository records the decisions this package implements).
"""

from __future__ import annotations

__version__ = "0.1.0"

try:
    from memtomem.integrations.langgraph_store import matches_filter as _matches_filter  # noqa: F401
    from memtomem.runtime.files import CRUD_SIDECAR_LOCK_BUDGET_S as _budget  # noqa: F401
except ImportError as exc:  # pragma: no cover - exercised against an older memtomem
    raise ImportError(
        "langchain-memtomem needs the memtomem public names added for ADR-0042 "
        "(memtomem.runtime.files, matches_filter); upgrade memtomem"
    ) from exc

from langchain_memtomem.store import (
    DEFAULT_MAX_CHUNKS_PER_SEARCH,
    MemtomemPipelineStore,
    StoreIndexError,
)

__all__ = [
    "DEFAULT_MAX_CHUNKS_PER_SEARCH",
    "MemtomemPipelineStore",
    "StoreIndexError",
    "__version__",
]
