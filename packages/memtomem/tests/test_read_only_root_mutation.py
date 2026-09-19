"""A source under ``indexing.read_only_memory_dirs`` is never rewritten by memtomem.

The hard case, and the one every surface test here uses: the file was indexed
while its directory was still writable, so its stored chunk carries
``source_read_only=0``. Only a gate that asks the *configuration* refuses it —
one reading the stored flag would let the write through until a re-index
happened to stamp it, which is precisely the window in which the user has
already said "stop writing here".

The control tests are what keep this from being a blanket refusal: a writable
root still mutates, and neither a case-distinct sibling nor a directory merely
sharing the root's prefix is protected by it.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.source_provenance import SOURCE_READ_ONLY_DETAIL, ReadOnlySourceError
from memtomem.tools import memory_mutation, memory_writer
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured
from memtomem.web.routes import chunks as chunk_routes

ORIGINAL = "## Alpha\n\nAlpha body\n"
SURFACES = ("mcp_edit", "mcp_delete", "web_edit", "web_delete")


async def _index(comp, source: Path) -> None:
    stats = await comp.index_engine.index_file(source, already_scanned=True)
    assert not stats.errors
    assert stats.indexed_chunks == 1


async def _index_then_declare_read_only(comp, vault: Path, name: str = "note.md"):
    """Index ``vault/<name>`` as a writable root, then move that root to read-only.

    Returns the source and its stored chunk. The asserts are the fixture's own
    contract: the chunk must be writable-looking (flag unset, provenance intact)
    or a later refusal would prove nothing about *this* gate.
    """
    indexing = comp.config.indexing
    vault.mkdir(exist_ok=True)
    source = vault / name
    source.write_text(ORIGINAL, encoding="utf-8")
    indexing.memory_dirs = [*indexing.memory_dirs, vault]
    await _index(comp, source)
    indexing.memory_dirs = [d for d in indexing.memory_dirs if d != vault]
    indexing.read_only_memory_dirs = [vault]

    (chunk,) = await comp.storage.list_chunks_by_source(source)
    assert not chunk.metadata.source_read_only, "fixture must start from a writable chunk"
    assert chunk.metadata.source_span_hash, "fixture must start from usable provenance"
    assert comp.index_engine.is_read_only_source(source)
    return source, chunk


@pytest.fixture
async def read_only_source(bm25_only_components, tmp_path):
    comp, _mem_dir = bm25_only_components
    source, chunk = await _index_then_declare_read_only(comp, tmp_path / "vault")
    return comp, source, chunk


async def _request(comp, surface, chunk_id):
    if surface.startswith("mcp"):
        ctx = StubCtx(AppContext.from_components(comp))
        if surface.endswith("edit"):
            return await memory_crud.mem_edit(str(chunk_id), "NEW BODY", ctx=ctx)
        return await memory_crud.mem_delete(chunk_id=str(chunk_id), ctx=ctx)
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        if surface.endswith("edit"):
            return await client.patch(f"/api/chunks/{chunk_id}", json={"new_content": "NEW BODY"})
        return await client.delete(f"/api/chunks/{chunk_id}")


def _assert_read_only(result):
    if isinstance(result, str):
        assert result == f"Error: {SOURCE_READ_ONLY_DETAIL}"
    else:
        assert result.status_code == 409, result.text
        assert result.json()["detail"] == SOURCE_READ_ONLY_DETAIL


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_read_only_source_is_refused_before_any_write(
    read_only_source, monkeypatch, surface
):
    comp, source, chunk = read_only_source
    source_before = source.read_bytes()
    rows_before = comp.storage._get_db().execute("SELECT * FROM chunks").fetchall()
    index = AsyncMock(wraps=comp.index_engine.index_file)
    monkeypatch.setattr(comp.index_engine, "index_file", index)
    # MCP imports the writers inside the tool body (from ``memory_writer``); the
    # web routes bind them at import. Spy on both homes.
    writes = []
    for module, name in (
        (memory_writer, "replace_chunk_body"),
        (memory_writer, "remove_lines"),
        (chunk_routes, "replace_chunk_body"),
        (chunk_routes, "remove_lines"),
    ):
        spy = Mock(wraps=getattr(module, name))
        monkeypatch.setattr(module, name, spy)
        writes.append(spy)
    restores = []
    for module in (memory_crud, memory_mutation):
        restore = Mock(wraps=module.restore_pre_image_quietly)
        monkeypatch.setattr(module, "restore_pre_image_quietly", restore)
        restores.append(restore)

    _assert_read_only(await _request(comp, surface, chunk.id))

    assert source.read_bytes() == source_before
    assert comp.storage._get_db().execute("SELECT * FROM chunks").fetchall() == rows_before
    index.assert_not_awaited()
    for spy in writes:
        spy.assert_not_called()
    for restore in restores:
        restore.assert_not_called()


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_refusal_leaves_no_bytes_in_the_protected_directory(read_only_source, surface):
    """A refused mutation must not create the L2 sidecar next to the source.

    ``async_memory_file_lock`` opens ``.<name>.lock`` with ``O_RDWR | O_CREAT``
    and does not remove it on release (measured), so the lock is itself a write
    into the directory the root exists to protect. Both MCP surfaces did exactly
    that until the gate moved ahead of ``_locked_chunk``'s acquire; the web
    routes already refused earlier, and are pinned here so they cannot drift
    into the same order.

    Asserted on the directory's contents rather than on the error, because the
    error was already correct while the sidecar was still being created.
    """
    comp, source, chunk = read_only_source
    # The fixture indexed this file while the root was still writable, which left
    # its own sidecar. Clear it so what follows measures only what the refusal
    # does — the setup's lock is not the observation.
    for stale in source.parent.glob(".*.lock"):
        stale.unlink()
    before = sorted(p.name for p in source.parent.iterdir())
    assert before == ["note.md"], f"the observation must start clean, got {before}"

    _assert_read_only(await _request(comp, surface, chunk.id))

    after = sorted(p.name for p in source.parent.iterdir())
    assert after == before, f"refusal left {set(after) - set(before)} in the protected directory"


@pytest.mark.parametrize("surface", ("mcp_edit", "web_edit"))
async def test_a_chunk_re_scoped_into_a_protected_root_under_the_lock_is_refused(
    bm25_only_components, tmp_path, monkeypatch, surface
):
    """What the *in-lock* gate is for, and the only thing the pre-lock one cannot do.

    Both gates now exist on each surface, so removing either leaves the other
    covering the ordinary case — which makes the in-lock copy unfalsifiable
    unless something exercises its distinct job. That job is this race:
    ``memory-migrate`` re-scopes the chunk onto a *protected* source while we
    wait for L2, so the pre-lock gate judged the old (writable) path and only
    the re-fetch under the lock sees where the write would actually land.

    Simulated by making the second ``get_chunk`` — the one the lock helpers
    re-fetch with — answer with the moved chunk, which is exactly the window the
    helpers' own move-retry logic exists for.
    """
    import dataclasses

    comp, mem_dir = bm25_only_components
    vault = tmp_path / "vault"
    vault.mkdir()
    writable_source = mem_dir / "note.md"
    writable_source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, writable_source)
    (chunk,) = await comp.storage.list_chunks_by_source(writable_source)

    protected_source = vault / "note.md"
    protected_source.write_text(ORIGINAL, encoding="utf-8")
    comp.config.indexing.read_only_memory_dirs = [vault]
    assert not comp.index_engine.is_read_only_source(writable_source)
    assert comp.index_engine.is_read_only_source(protected_source)

    moved = dataclasses.replace(
        chunk, metadata=dataclasses.replace(chunk.metadata, source_file=protected_source)
    )
    real_get_chunk = comp.storage.get_chunk
    calls = {"n": 0}

    async def get_chunk(uid):
        calls["n"] += 1
        # First call is the pre-lock probe and must look writable; every later
        # one is a re-fetch under the lock and reports the move.
        return await real_get_chunk(uid) if calls["n"] == 1 else moved

    monkeypatch.setattr(comp.storage, "get_chunk", get_chunk)

    result = await _request(comp, surface, chunk.id)

    assert calls["n"] >= 2, "the re-fetch under the lock did not happen; the race was not exercised"
    assert sorted(p.name for p in vault.iterdir()) == ["note.md"], (
        "the move left a lock sidecar in the protected directory: "
        f"{sorted(p.name for p in vault.iterdir())}"
    )
    if isinstance(result, str):
        assert "read_only" in result or "not found" in result, result
    else:
        assert result.status_code in (404, 409), result.text
    assert protected_source.read_text(encoding="utf-8") == ORIGINAL


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_writable_source_still_mutates(bm25_only_components, tmp_path, surface):
    """The control: the gate refuses read-only roots, not every edit.

    A read-only root is configured and non-empty throughout, so what the gate
    discriminates on is the source's location rather than the mere presence of
    the setting.
    """
    comp, mem_dir = bm25_only_components
    (tmp_path / "vault").mkdir(exist_ok=True)
    comp.config.indexing.read_only_memory_dirs = [tmp_path / "vault"]
    source = mem_dir / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, source)
    (chunk,) = await comp.storage.list_chunks_by_source(source)

    result = await _request(comp, surface, chunk.id)

    if isinstance(result, str):
        assert not result.startswith("Error"), result
    else:
        assert result.status_code == 200, result.text
    assert source.read_text(encoding="utf-8") != ORIGINAL


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_sibling_sharing_the_roots_prefix_still_mutates(
    bm25_only_components, tmp_path, surface
):
    """``/vault`` must not claim ``/vault2`` — the trailing-separator rule (#647).

    Without it this gate would silently freeze directories the user never
    named, and the failure would look like "the edit button is broken".
    """
    comp, _mem_dir = bm25_only_components
    indexing = comp.config.indexing
    vault, sibling = tmp_path / "vault", tmp_path / "vault2"
    vault.mkdir()
    sibling.mkdir()
    source = sibling / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    indexing.memory_dirs = [*indexing.memory_dirs, sibling]
    await _index(comp, source)
    indexing.read_only_memory_dirs = [vault]
    assert not comp.index_engine.is_read_only_source(source)
    (chunk,) = await comp.storage.list_chunks_by_source(source)

    result = await _request(comp, surface, chunk.id)

    if isinstance(result, str):
        assert not result.startswith("Error"), result
    else:
        assert result.status_code == 200, result.text
    assert source.read_text(encoding="utf-8") != ORIGINAL


async def test_the_web_edit_route_refuses_before_calling_the_mutation_helper(
    read_only_source, monkeypatch
):
    """Witness for the route's *own* gate, which the helper's gate would mask.

    Both refuse a web edit with the same 409, so removing either one alone
    leaves the surface test above green and neither is actually pinned. The
    distinguishing observation is whether the helper is reached at all: the
    route must refuse before delegating, so the work of taking a pre-image and
    re-indexing is never started for a file we may not write.
    """
    comp, _source, chunk = read_only_source
    delegate = AsyncMock(wraps=memory_mutation.mutate_source_and_reindex)
    monkeypatch.setattr(memory_mutation, "mutate_source_and_reindex", delegate)

    _assert_read_only(await _request(comp, "web_edit", chunk.id))

    delegate.assert_not_awaited()


async def test_the_shared_mutation_helper_refuses_a_read_only_source(read_only_source):
    """Witness for the helper's gate, independent of any route.

    The helper is the single write path the web edit route delegates to, and
    the guard belongs there too: a future caller that forgets the check must
    still not write, and the route's gate cannot speak for callers it does not
    own. Asserted directly so the route's gate cannot stand in for this one.
    """
    comp, source, _chunk = read_only_source
    calls = []

    with pytest.raises(ReadOnlySourceError):
        await memory_mutation.mutate_source_and_reindex(
            comp.index_engine, source, lambda pre: calls.append(pre)
        )

    assert calls == [], "the mutation callback must never run"
    assert source.read_text(encoding="utf-8") == ORIGINAL


async def test_mem_add_refuses_without_leaving_a_lock_sidecar(bm25_only_components, tmp_path):
    """The refusal must come *before* the cross-process sidecar is acquired.

    Acquiring it creates ``.<name>.lock`` next to the target, so a gate that ran
    only inside the lock would have written a file into the directory memtomem
    promised not to write — and then refused. The assertion is on the directory
    contents rather than on the error alone, because the error was already
    correct while the sidecar was still being created.
    """
    comp, _mem_dir = bm25_only_components
    vault = tmp_path / "vault"
    vault.mkdir()
    comp.config.indexing.read_only_memory_dirs = [vault]
    comp.config.indexing.memory_dirs = [*comp.config.indexing.memory_dirs, vault]
    ctx = StubCtx(AppContext.from_components(comp))

    result = await memory_crud.mem_add("some content", file=str(vault / "note.md"), ctx=ctx)

    assert "read_only_target" in result, result
    assert list(vault.iterdir()) == [], f"refusal left files behind: {list(vault.iterdir())}"


async def test_batch_add_refuses_without_leaving_a_lock_sidecar(bm25_only_components, tmp_path):
    """Same rule as ``mem_add``, and it needs its own test: batch add is a
    separate code path with its own copy of both gates.

    ``.lock`` files are not removed on release (measured), so "the directory is
    still empty" is a faithful witness that no lock was ever taken there.
    """
    comp, _mem_dir = bm25_only_components
    vault = tmp_path / "vault"
    vault.mkdir()
    comp.config.indexing.read_only_memory_dirs = [vault]
    comp.config.indexing.memory_dirs = [*comp.config.indexing.memory_dirs, vault]
    ctx = StubCtx(AppContext.from_components(comp))

    result = await memory_crud.mem_batch_add(
        [{"key": "first", "value": "one"}, {"key": "second", "value": "two"}],
        file=str(vault / "batch.md"),
        ctx=ctx,
    )

    assert "read_only_target" in result, result
    assert list(vault.iterdir()) == [], f"refusal left files behind: {list(vault.iterdir())}"


async def test_the_langgraph_adapter_refuses_without_leaving_a_lock_sidecar(
    bm25_only_components, tmp_path
):
    """The LangGraph integration takes its own lock and had its own gap.

    It reports through a dict rather than a string, so the refusal vocabulary is
    asserted on that shape.
    """
    from memtomem.integrations.langgraph import MemtomemStore

    comp, _mem_dir = bm25_only_components
    vault = tmp_path / "vault"
    vault.mkdir()
    comp.config.indexing.read_only_memory_dirs = [vault]
    comp.config.indexing.memory_dirs = [*comp.config.indexing.memory_dirs, vault]

    store = MemtomemStore()
    # Hand it the fixture's components rather than letting it build its own, so
    # the test exercises the adapter's gate and not component construction.
    store._components = comp

    result = await store.add("content", file=str(vault / "note.md"))

    assert result.get("error") == "read_only_target", result
    assert list(vault.iterdir()) == [], f"refusal left files behind: {list(vault.iterdir())}"


async def test_new_chunks_under_a_read_only_root_are_stamped(bm25_only_components, tmp_path):
    """Indexing *into* a read-only root marks the chunks, so the per-chunk gates
    also cover them without consulting the configuration again."""
    comp, _mem_dir = bm25_only_components
    indexing = comp.config.indexing
    vault = tmp_path / "vault"
    vault.mkdir()
    indexing.read_only_memory_dirs = [vault]
    source = vault / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")

    await _index(comp, source)

    (chunk,) = await comp.storage.list_chunks_by_source(source)
    assert chunk.metadata.source_read_only
    # A read-only root is indexed like any other: the promise is about writes.
    assert chunk.content.strip()


async def test_a_read_only_root_is_still_searchable(bm25_only_components, tmp_path):
    """Read-only withholds the right to rewrite, not visibility.

    Pinned because "protect the vault" has an obvious wrong implementation —
    excluding it — and that one also makes every mutation test above pass.
    """
    comp, _mem_dir = bm25_only_components
    vault = tmp_path / "vault"
    vault.mkdir()
    comp.config.indexing.read_only_memory_dirs = [vault]
    source = vault / "note.md"
    source.write_text("## Zebra\n\nZebra body about xylophones\n", encoding="utf-8")

    await _index(comp, source)

    assert not comp.index_engine.is_excluded(source)
    results, _ = await comp.search_pipeline.search("xylophones", top_k=5)
    assert [r.chunk.metadata.source_file for r in results] == [source]
