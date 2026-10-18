"""Web export, source preview and scratch promotion against a real store.

The route tests elsewhere stub storage, so a chunk-shaped mistake in export
filtering and a missing import inside a branch only taken for masked rows both
shipped green. These run the real stack.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.web.app import create_app
from memtomem.web.deps import require_configured


def _web_client(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.state.project_root = None
    app.dependency_overrides[require_configured] = lambda: None
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _index(comp, path: Path, text: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    stats = await comp.index_engine.index_file(path, ingress_guarded=True)
    return stats.indexed_chunks


@pytest.fixture
async def two_projects(bm25_only_components, tmp_path, monkeypatch):
    """A user note plus one note in each of two registered projects, cwd in ``a``."""
    comp, mem_dir = bm25_only_components
    home = tmp_path / "home"
    user_tier = home / ".memtomem" / "memories"
    proj_a = home / "a"
    proj_b = home / "b"
    tier_a = proj_a / ".memtomem" / "memories"
    tier_b = proj_b / ".memtomem" / "memories"
    comp.config.indexing.memory_dirs = [mem_dir, user_tier]
    comp.config.indexing.project_memory_dirs = [tier_a, tier_b]

    assert await _index(comp, user_tier / "user.md", "## User\n\nuser note\n") == 1
    assert await _index(comp, tier_a / "a.md", "## A\n\nproject a note\n") == 1
    assert await _index(comp, tier_b / "b.md", "## B\n\nproject b note\n") == 1
    monkeypatch.chdir(proj_a)
    return comp


# Export is a backup: like ``mem_export`` it is not bounded by the caller's
# project, so a download from inside one project still carries every other
# project's rows.
async def test_export_stats_counts_every_project(two_projects):
    async with _web_client(two_projects) as client:
        resp = await client.get("/api/export/stats")
    assert resp.status_code == 200, resp.text
    assert resp.json()["total_chunks"] == 3


async def test_export_bundle_carries_every_project(two_projects):
    async with _web_client(two_projects) as client:
        resp = await client.get("/api/export")
    assert resp.status_code == 200, resp.text
    contents = sorted(c["content"] for c in resp.json()["chunks"])
    assert contents == ["project a note", "project b note", "user note"]


async def _source_content(comp, source: Path):
    async with _web_client(comp) as client:
        return await client.get("/api/sources/content", params={"path": str(source)})


async def test_source_content_serves_an_unmasked_source(bm25_only_components):
    comp, mem_dir = bm25_only_components
    source = mem_dir / "plain.md"
    await _index(comp, source, "## Plain\n\nnothing masked\n")

    resp = await _source_content(comp, source)
    assert resp.status_code == 200, resp.text
    assert resp.json()["content"] == "## Plain\n\nnothing masked\n"


async def test_source_content_refuses_a_masked_projection(bm25_only_components):
    comp, mem_dir = bm25_only_components
    source = mem_dir / "masked.md"
    await _index(comp, source, "## Masked\n\nthe file still holds the raw value\n")
    db = comp.storage._get_db()
    db.execute("UPDATE chunks SET redaction_count = 1")
    db.commit()

    resp = await _source_content(comp, source)
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == "masked_projection_read_only"
    assert "raw value" not in resp.text


async def test_scratch_promote_flushes_the_search_cache(bm25_only_components, monkeypatch):
    comp, mem_dir = bm25_only_components
    await comp.storage.scratch_set("note", "promoted scratch entry")
    flushes: list[None] = []
    original = comp.search_pipeline.invalidate_cache

    def _spy() -> None:
        flushes.append(None)
        original()

    monkeypatch.setattr(comp.search_pipeline, "invalidate_cache", _spy)

    async with _web_client(comp) as client:
        resp = await client.post("/api/scratch/note/promote", json={})
    assert resp.status_code == 200, resp.text
    target = Path(resp.json()["file"])
    stored = await comp.storage.list_chunks_by_source(target, limit=None)
    assert resp.json()["indexed_chunks"] == len(stored) == 1
    assert flushes
