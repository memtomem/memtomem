"""The web write routes refuse a file whose content may not be indexed.

Same rule as the MCP tools (``test_mutation_reindex_gate.py``), answered with a
409 ``index_blocked`` rather than the 403 ``redaction_blocked`` the SPA meets
with a force-unsafe retry: the refused content is not the caller's, or the
write already landed, and a resend would append it again.
"""

from __future__ import annotations

from httpx import ASGITransport, AsyncClient

from memtomem.web.app import create_app
from memtomem.web.deps import require_configured

_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
_CLEAN = "## A\n\nalpha entry about deploys\n\n## B\n\nbeta entry about reviews\n"
_SECRET_SECTION = f"\n## K\n\naws {_KEY}\n"


def _web_client(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.state.project_root = None
    app.state.csrf_enforce = False
    app.dependency_overrides[require_configured] = lambda: None
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )


async def _tampered(comp, mem_dir):
    target = mem_dir / "notes.md"
    target.write_text(_CLEAN, encoding="utf-8")
    await comp.index_engine.index_file(target)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(_SECRET_SECTION)
    return target


async def _stored(comp, target) -> str:
    chunks = await comp.storage.list_chunks_by_source(target, limit=None)
    return "\n".join(c.content for c in chunks)


def _assert_index_blocked(resp, *, written: bool):
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["detail"] == "index_blocked"
    assert detail["written"] is written
    assert "refused" in detail["message"]
    assert "/" not in (detail["file"] or "/"), "a bare name, never a host path"
    assert _KEY not in resp.text
    # The SPA shows ``message`` as is: the remedy names web controls, not a
    # command a browser user cannot run (#2797).
    for foreign in ("mm ", "mem_", "`"):
        assert foreign not in detail["message"], foreign
    assert "Index tab" in detail["message"]


async def test_add_refuses_before_appending(bm25_only_components):
    comp, mem_dir = bm25_only_components
    target = await _tampered(comp, mem_dir)
    before = target.read_bytes()

    async with _web_client(comp) as client:
        resp = await client.post("/api/add", json={"content": "gamma entry", "file": "notes.md"})

    _assert_index_blocked(resp, written=False)
    assert target.read_bytes() == before
    assert _KEY not in await _stored(comp, target)
    assert await comp.storage.is_source_held(target)


async def test_chunk_edit_refuses_untouched(bm25_only_components):
    comp, mem_dir = bm25_only_components
    target = await _tampered(comp, mem_dir)
    before = target.read_bytes()
    chunks = await comp.storage.list_chunks_by_source(target, limit=None)
    chunk_id = next(str(c.id) for c in chunks if "alpha" in c.content)

    async with _web_client(comp) as client:
        edited = await client.patch(f"/api/chunks/{chunk_id}", json={"new_content": "revised"})
        _assert_index_blocked(edited, written=False)
        assert target.read_bytes() == before
    assert _KEY not in await _stored(comp, target)
    assert await comp.storage.is_source_held(target)


async def test_chunk_delete_refuses_untouched(bm25_only_components):
    comp, mem_dir = bm25_only_components
    target = await _tampered(comp, mem_dir)
    before = target.read_bytes()
    chunks = await comp.storage.list_chunks_by_source(target, limit=None)
    chunk_id = next(str(c.id) for c in chunks if "alpha" in c.content)

    async with _web_client(comp) as client:
        resp = await client.delete(f"/api/chunks/{chunk_id}")

    _assert_index_blocked(resp, written=False)
    assert target.read_bytes() == before
    assert _KEY not in await _stored(comp, target)
    assert await comp.storage.is_source_held(target)


async def test_scratch_promote_after_append_is_marked_promoted(bm25_only_components):
    """A title the ingress never scanned: refused after the append, marked promoted."""
    comp, mem_dir = bm25_only_components
    await comp.storage.scratch_set("note", "scratch-body-marker")

    async with _web_client(comp) as client:
        resp = await client.post(
            "/api/scratch/note/promote", json={"title": "password: hunter2-title-value"}
        )

    _assert_index_blocked(resp, written=True)
    assert (await comp.storage.scratch_get("note"))["promoted"] is True
    written = [p for p in mem_dir.glob("*.md") if "scratch-body-marker" in p.read_text()]
    assert len(written) == 1
    assert "hunter2-title-value" not in await _stored(comp, written[0])


async def test_a_refused_promotion_cannot_be_promoted_again(bm25_only_components):
    """Its body is already in the file: a retry must not append it a second time."""
    comp, mem_dir = bm25_only_components
    await comp.storage.scratch_set("note", "scratch-body-marker")

    async with _web_client(comp) as client:
        first = await client.post(
            "/api/scratch/note/promote", json={"title": "password: hunter2-title-value"}
        )
        _assert_index_blocked(first, written=True)
        retried = await client.post("/api/scratch/note/promote", json={"title": "clean"})

    assert retried.status_code == 409, retried.text
    assert "already promoted" in retried.json()["detail"]
    on_disk = sum(p.read_text().count("scratch-body-marker") for p in mem_dir.glob("*.md"))
    assert on_disk == 1


async def test_a_value_replaced_while_waiting_is_not_promoted(bm25_only_components, monkeypatch):
    """The value the request guarded is gone: write nothing, leave the new one promotable."""
    comp, mem_dir = bm25_only_components
    await comp.storage.scratch_set("note", "original-value-marker")
    real_lookup = comp.index_engine.effective_namespace_for

    async def lookup_then_replace(*args, **kwargs):
        # Runs under the target file's lock, before the route re-reads the entry.
        await comp.storage.scratch_set("note", "replacement-value-marker")
        return await real_lookup(*args, **kwargs)

    monkeypatch.setattr(comp.index_engine, "effective_namespace_for", lookup_then_replace)
    async with _web_client(comp) as client:
        resp = await client.post("/api/scratch/note/promote", json={})

    assert resp.status_code == 409, resp.text
    assert "changed while it was being promoted" in resp.json()["detail"]
    on_disk = "".join(p.read_text() for p in mem_dir.glob("*.md"))
    assert "original-value-marker" not in on_disk
    entry = await comp.storage.scratch_get("note")
    assert entry["value"] == "replacement-value-marker"
    assert entry["promoted"] is False
