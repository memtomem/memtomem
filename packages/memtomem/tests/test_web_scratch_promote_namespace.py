"""Web scratch promotion refuses before writing when the namespace lookup fails.

The lookup used to run inside ``index_file``, after ``append_entry`` had made
the entry durable: the request failed with a 500, the scratch row stayed
unpromoted, and a retry appended the entry a second time (#2731). Web
``/api/add`` asks the lookup before appending; promotion now does the same.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.web.app import create_app
from memtomem.web.deps import require_configured
from memtomem.web.routes._errors import NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL

MARKER = "promoted-scratch-marker"


def _web_client(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.state.project_root = None
    app.dependency_overrides[require_configured] = lambda: None
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )


def _markers_on_disk(mem_dir) -> int:
    return sum(p.read_text(encoding="utf-8").count(MARKER) for p in mem_dir.glob("*.md"))


async def test_lookup_failure_refuses_before_append_and_retry_writes_once(
    bm25_only_components, monkeypatch
):
    comp, mem_dir = bm25_only_components
    await comp.storage.scratch_set("note", MARKER)

    async def unavailable(_source):
        raise RuntimeError("transient read error")

    lookup = comp.storage.namespaces_for_source
    monkeypatch.setattr(comp.storage, "namespaces_for_source", unavailable)
    async with _web_client(comp) as client:
        refused = await client.post("/api/scratch/note/promote", json={})
        assert refused.status_code == 503, refused.text
        assert refused.json()["detail"] == NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL
        assert _markers_on_disk(mem_dir) == 0
        assert (await comp.storage.scratch_get("note"))["promoted"] is False

        monkeypatch.setattr(comp.storage, "namespaces_for_source", lookup)
        retried = await client.post("/api/scratch/note/promote", json={})
    assert retried.status_code == 200, retried.text
    assert _markers_on_disk(mem_dir) == 1
    assert (await comp.storage.scratch_get("note"))["promoted"] is True


# ``None`` from the resolver is the untagged carve-out, which stores the chunk
# model's default. Substituting ``namespace.default_namespace`` for it would
# stamp "" when that setting is empty, where promotion used to store "default".
@pytest.mark.parametrize(("configured", "stored"), [("", "default"), ("custom", "custom")])
async def test_promotion_stamps_the_namespace_indexing_would(
    bm25_only_components, configured, stored
):
    comp, _ = bm25_only_components
    comp.config.namespace.default_namespace = configured
    await comp.storage.scratch_set("note", MARKER)
    async with _web_client(comp) as client:
        resp = await client.post("/api/scratch/note/promote", json={})
    assert resp.status_code == 200, resp.text
    chunks = await comp.storage.list_chunks_by_source(Path(resp.json()["file"]), limit=None)
    assert [c.metadata.namespace for c in chunks] == [stored]
