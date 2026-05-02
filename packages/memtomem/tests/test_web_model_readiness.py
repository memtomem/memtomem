"""Tests for ``GET /api/system/model-readiness`` (issue #696).

The endpoint inspects observability flags on the lazy fastembed loaders
(``_model``, ``_loading``, ``_load_error``) plus a filesystem probe of
the cache directory. These tests stub the loaders directly — actually
loading fastembed would download a multi-GB model and defeats the
purpose of having a state machine you can introspect from outside.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from memtomem.web.app import create_app


def _config(
    *,
    embedder_provider: str = "onnx",
    embedder_model: str = "bge-small-en-v1.5",
    rerank_enabled: bool = False,
    rerank_provider: str = "fastembed",
    rerank_model: str = "Xenova/ms-marco-MiniLM-L-6-v2",
) -> SimpleNamespace:
    """Minimal config stand-in with just the fields the readiness endpoint reads."""
    return SimpleNamespace(
        embedding=SimpleNamespace(provider=embedder_provider, model=embedder_model),
        rerank=SimpleNamespace(
            enabled=rerank_enabled,
            provider=rerank_provider,
            model=rerank_model,
        ),
    )


@dataclass
class _StubLoader:
    """Mimics the ``_model`` / ``_loading`` / ``_load_error`` flags
    that ``OnnxEmbedder`` and ``FastEmbedReranker`` expose for the
    readiness endpoint."""

    _model: object | None = None
    _loading: bool = False
    _load_error: str | None = None
    embed_query: object = field(default_factory=MagicMock)
    embed_texts: object = field(default_factory=MagicMock)


def _make_app(config: SimpleNamespace, embedder: _StubLoader, reranker: _StubLoader | None):
    """Build a FastAPI app with the routes wired and minimal app.state."""
    from memtomem.web.deps import require_configured

    app = create_app(lifespan=None, mode="prod")
    app.state.config = config
    app.state.embedder = embedder
    # The reranker lives on ``search_pipeline._reranker`` in production.
    # Build a stub pipeline carrying just that attribute.
    pipeline_stub = SimpleNamespace(_reranker=reranker)
    app.state.search_pipeline = pipeline_stub
    # Bypass the require_configured gate (no real ``~/.memtomem/config.json``).
    app.dependency_overrides[require_configured] = lambda: None
    return app


@pytest.fixture
def fake_cache_absent():
    """Patch ``model_snapshot_present`` to always return False (cold cache).

    Patches the source module rather than the import site because
    ``_component_for`` does the import lazily inside the function body —
    no module-level reference exists in ``routes.system`` to rebind.
    """
    with patch("memtomem.embedding.readiness.model_snapshot_present", return_value=False) as m:
        yield m


@pytest.fixture
def fake_cache_present():
    """Patch ``model_snapshot_present`` to always return True (cache populated)."""
    with patch("memtomem.embedding.readiness.model_snapshot_present", return_value=True) as m:
        yield m


# ─── Embedder states ─────────────────────────────────────────────────


def test_embedder_cold_when_no_load_attempt(fake_cache_absent):
    app = _make_app(_config(), _StubLoader(), reranker=None)
    with TestClient(app) as c:
        resp = c.get("/api/system/model-readiness")
    assert resp.status_code == 200
    body = resp.json()
    assert body["embedder"]["state"] == "cold"
    assert body["embedder"]["provider"] == "onnx"
    assert body["embedder"]["model"] == "bge-small-en-v1.5"
    assert body["embedder"]["cache_present"] is False


def test_embedder_downloading_when_loading_and_cache_absent(fake_cache_absent):
    embedder = _StubLoader(_loading=True)
    app = _make_app(_config(), embedder, reranker=None)
    with TestClient(app) as c:
        resp = c.get("/api/system/model-readiness")
    assert resp.json()["embedder"]["state"] == "downloading"


def test_embedder_loading_when_loading_and_cache_present(fake_cache_present):
    embedder = _StubLoader(_loading=True)
    app = _make_app(_config(), embedder, reranker=None)
    with TestClient(app) as c:
        resp = c.get("/api/system/model-readiness")
    body = resp.json()
    assert body["embedder"]["state"] == "loading"
    assert body["embedder"]["cache_present"] is True


def test_embedder_ready_when_model_loaded(fake_cache_present):
    embedder = _StubLoader(_model=object())
    app = _make_app(_config(), embedder, reranker=None)
    with TestClient(app) as c:
        resp = c.get("/api/system/model-readiness")
    assert resp.json()["embedder"]["state"] == "ready"


def test_embedder_error_when_load_error_set(fake_cache_absent):
    embedder = _StubLoader(_load_error="boom")
    app = _make_app(_config(), embedder, reranker=None)
    with TestClient(app) as c:
        resp = c.get("/api/system/model-readiness")
    body = resp.json()
    assert body["embedder"]["state"] == "error"
    assert body["embedder"]["error"] == "boom"


def test_embedder_skipped_when_provider_not_onnx(fake_cache_absent):
    """Ollama/Cohere providers have their own connection model — skip."""
    app = _make_app(_config(embedder_provider="ollama"), _StubLoader(), reranker=None)
    with TestClient(app) as c:
        resp = c.get("/api/system/model-readiness")
    assert resp.json()["embedder"]["state"] == "skipped"


def test_embedder_approx_size_for_known_model(fake_cache_absent):
    embedder = _StubLoader(_loading=True)
    app = _make_app(_config(embedder_model="bge-m3"), embedder, reranker=None)
    with TestClient(app) as c:
        resp = c.get("/api/system/model-readiness")
    body = resp.json()
    # ``bge-m3`` aliases to ``BAAI/bge-m3`` which has a 2300 MB entry.
    assert body["embedder"]["approx_size_mb"] == 2300


# ─── Reranker states ─────────────────────────────────────────────────


def test_reranker_skipped_when_disabled(fake_cache_absent):
    app = _make_app(_config(rerank_enabled=False), _StubLoader(), reranker=None)
    with TestClient(app) as c:
        resp = c.get("/api/system/model-readiness")
    body = resp.json()
    assert body["reranker"]["state"] == "skipped"
    assert body["reranker"]["model"] is None


def test_reranker_states_when_enabled(fake_cache_present):
    reranker = _StubLoader(_loading=True)
    app = _make_app(
        _config(
            rerank_enabled=True,
            rerank_model="jinaai/jina-reranker-v2-base-multilingual",
        ),
        _StubLoader(_model=object()),
        reranker=reranker,
    )
    with TestClient(app) as c:
        resp = c.get("/api/system/model-readiness")
    body = resp.json()
    assert body["embedder"]["state"] == "ready"
    assert body["reranker"]["state"] == "loading"
    # 1110 MB matches fastembed's reported size_in_GB for jina-reranker-v2.
    assert body["reranker"]["approx_size_mb"] == 1110
