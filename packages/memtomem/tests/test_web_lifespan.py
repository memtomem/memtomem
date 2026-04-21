"""Web lifespan auto-sync gating — follow-up to issue #349.

The web lifespan used to overwrite the runtime embedding config whenever the
DB-stored ``stored_embedding_info`` differed from config, then call
``storage.clear_embedding_mismatch()`` to suppress the mismatch banner. For
normal model drift (e.g. user edited ``config.json`` to a different onnx
model without running ``mm embedding-reset``) that soft-sync was benign.

For the dim=0 degraded-mode case introduced by #349, the stored "embedding"
is NoopEmbedder (``provider=none``, ``dim=0``) — auto-syncing silently
downgrades the user's configured onnx/bge-m3 to BM25-only AND swallows the
banner, so the user never sees the broken state and has no path to recover
it from the web UI. The gate below keeps the auto-sync only when the server
came up clean (``embedding_broken is None``) so the recovery banner +
``POST /api/embedding-reset`` flow stays reachable in degraded mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI

from memtomem.web.app import _lifespan


@dataclass
class _FakeEmbeddingCfg:
    provider: str = "onnx"
    model: str = "bge-m3"
    dimension: int = 1024


@dataclass
class _FakeIndexingCfg:
    memory_dirs: list = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.memory_dirs = self.memory_dirs or []


@dataclass
class _FakeConfig:
    embedding: _FakeEmbeddingCfg
    indexing: _FakeIndexingCfg


def _make_components(
    *,
    embedding_broken: dict[str, Any] | None,
    stored_info: dict[str, Any],
    cfg_provider: str = "onnx",
    cfg_model: str = "bge-m3",
    cfg_dim: int = 1024,
) -> MagicMock:
    """Build a mock ``Components`` for ``_lifespan``.

    ``storage.clear_embedding_mismatch`` and ``storage.stored_embedding_info``
    are probed by the auto-sync block; the rest is stubbed just enough to
    keep the context manager from raising before it yields.
    """
    storage = MagicMock()
    storage.stored_embedding_info = stored_info
    storage.clear_embedding_mismatch = MagicMock()

    comp = MagicMock()
    comp.config = _FakeConfig(
        embedding=_FakeEmbeddingCfg(provider=cfg_provider, model=cfg_model, dimension=cfg_dim),
        indexing=_FakeIndexingCfg(),
    )
    comp.storage = storage
    comp.embedder = MagicMock()
    comp.search_pipeline = MagicMock()
    comp.index_engine = MagicMock()
    comp.embedding_broken = embedding_broken
    return comp


async def _run_lifespan(comp: MagicMock) -> FastAPI:
    """Enter and exit ``_lifespan`` with a mocked ``create_components``."""
    app = FastAPI()
    with (
        patch("memtomem.server.component_factory.create_components", AsyncMock(return_value=comp)),
        patch("memtomem.server.component_factory.close_components", AsyncMock()),
        # The lifespan also instantiates DedupScanner — harmless to stub.
        patch("memtomem.search.dedup.DedupScanner", MagicMock()),
    ):
        async with _lifespan(app):
            pass
    return app


async def test_auto_sync_skipped_when_degraded():
    """With ``embedding_broken`` set, the lifespan must NOT overwrite config
    or clear the mismatch — the banner + reset button flow depends on those
    signals being left intact. Regression for issue #349 follow-up."""
    comp = _make_components(
        embedding_broken={
            "dimension_mismatch": True,
            "model_mismatch": True,
            "stored": {"dimension": 0, "provider": "none", "model": ""},
            "configured": {"dimension": 1024, "provider": "onnx", "model": "bge-m3"},
        },
        stored_info={"dimension": 0, "provider": "none", "model": ""},
    )

    await _run_lifespan(comp)

    # Config must still reflect what the user configured, not the legacy
    # NoopEmbedder meta row.
    assert comp.config.embedding.provider == "onnx"
    assert comp.config.embedding.model == "bge-m3"
    assert comp.config.embedding.dimension == 1024

    # The mismatch flag must survive so ``/api/embedding-status`` can surface
    # it and the UI banner can fire.
    comp.storage.clear_embedding_mismatch.assert_not_called()


async def test_auto_sync_runs_when_not_degraded():
    """Non-degraded model drift keeps the pre-#349 soft-sync behavior —
    config follows DB and the mismatch flag is cleared so the banner does
    not fire for drift that was already reconciled at startup."""
    comp = _make_components(
        embedding_broken=None,
        stored_info={"dimension": 384, "provider": "onnx", "model": "minilm-l12"},
        cfg_provider="onnx",
        cfg_model="bge-m3",
        cfg_dim=1024,
    )

    await _run_lifespan(comp)

    assert comp.config.embedding.model == "minilm-l12"
    assert comp.config.embedding.dimension == 384
    assert comp.config.embedding.provider == "onnx"
    comp.storage.clear_embedding_mismatch.assert_called_once()


@pytest.mark.parametrize(
    "stored_info",
    [
        None,
        {"dimension": 1024, "provider": "onnx", "model": "bge-m3"},  # matches config
    ],
)
async def test_auto_sync_noop_when_no_drift(stored_info):
    """When there's no stored info OR stored matches config, the sync block
    is a no-op regardless of ``embedding_broken`` — validates the gate
    doesn't accidentally enable sync on the non-drift path."""
    comp = _make_components(
        embedding_broken=None,
        stored_info=stored_info,
    )

    await _run_lifespan(comp)

    assert comp.config.embedding.provider == "onnx"
    comp.storage.clear_embedding_mismatch.assert_not_called()
