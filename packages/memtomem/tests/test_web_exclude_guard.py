"""HTTP-layer regression: ``GET /api/index/stream?path=<excluded_file>``.

Engine-level coverage of the same bypass lives in
``test_indexing_engine.py`` (``test_index_path_stream_single_file_blocks_excluded``).
This file asserts the guard also fires through the real route → real engine
stack, because that's the actual entry point exposed to users via the Web UI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import Mem2MemConfig
from memtomem.server.component_factory import close_components, create_components
from memtomem.web.app import create_app


@pytest.fixture
async def real_stack_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Minimal app wired to a real IndexEngine + storage (NoopEmbedder)."""
    for k in list(os.environ):
        if k.startswith("MEMTOMEM_"):
            monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))

    cfg = Mem2MemConfig()
    cfg.embedding.provider = "none"
    cfg.storage.sqlite_path = tmp_path / "mm.db"
    cfg.storage.sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    cfg.indexing.memory_dirs = [mem_dir]
    cfg.indexing.exclude_patterns = []

    comp = await create_components(cfg)
    app = create_app(lifespan=None)
    app.state.storage = comp.storage
    app.state.embedder = comp.embedder
    app.state.search_pipeline = comp.search_pipeline
    app.state.index_engine = comp.index_engine
    app.state.config = comp.config
    app.state.dedup_scanner = getattr(comp, "dedup_scanner", None)
    from memtomem.web import hot_reload as _hot_reload

    app.state.config_signature = _hot_reload.current_signature()
    app.state.last_reload_error = None

    try:
        yield app, comp, mem_dir
    finally:
        await close_components(comp)


def _sse_events(body: str) -> list[dict]:
    return [
        json.loads(line[len("data: ") :]) for line in body.splitlines() if line.startswith("data: ")
    ]


async def test_index_stream_excluded_single_file_not_indexed(real_stack_app):
    """GET /api/index/stream?path=<memory_dir>/oauth_creds.json —
    the built-in denylist matches, so the stream completes with zero
    indexed chunks and the chunker is never invoked.
    """
    app, comp, mem_dir = real_stack_app
    creds = mem_dir / "oauth_creds.json"
    creds.write_text('{"token": "secret"}')

    chunker_calls: list[str] = []
    orig = comp.index_engine._registry.chunk_file

    def spy(fp: Path, content: str):
        chunker_calls.append(fp.name)
        return orig(fp, content)

    comp.index_engine._registry.chunk_file = spy  # type: ignore[method-assign]
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/index/stream", params={"path": str(creds)})
    finally:
        comp.index_engine._registry.chunk_file = orig  # type: ignore[method-assign]

    assert resp.status_code == 200, resp.text
    events = _sse_events(resp.text)
    complete = next(e for e in events if e.get("type") == "complete")
    assert complete["indexed_chunks"] == 0
    assert complete["total_chunks"] == 0
    assert "oauth_creds.json" not in chunker_calls
