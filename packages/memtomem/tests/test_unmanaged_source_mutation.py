"""A file indexed from outside every memory directory is never rewritten by chunk id.

``index_file`` accepts any path, so a chunk's ``source_file`` can name a file
memtomem was only asked to read. Edit and delete by chunk id used to write to
that path with no question about where it was: indexing an agent's settings
file and then editing its one chunk replaced the file's content.

Each surface test starts from that state: a file outside every index root,
indexed by explicit path, its chunk writable-looking in every stored respect.
The controls keep the refusal from being a blanket one: a memory directory, the
upload directory and a read-only root each still get the answer they had.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx, set_home
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.source_provenance import (
    SOURCE_READ_ONLY_DETAIL,
    UNMANAGED_SOURCE_DETAIL,
    UPLOAD_DIR,
    UnmanagedSourceError,
)
from memtomem.storage.sqlite_helpers import resolves_under_any_root
from memtomem.tools import memory_mutation, memory_writer
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured
from memtomem.web.routes import chunks as chunk_routes

ORIGINAL = "## Alpha\n\nAlpha body\n"
SURFACES = ("mcp_edit", "mcp_delete", "web_edit", "web_delete")

#: How many times each surface asks ``is_managed_source`` on a request that
#: goes through. Every one of them is a refusal point of its own; see
#: ``test_each_consult_on_the_path_refuses``.
CONSULTS = {
    # ``_locked_chunk`` before its lock, ``_mutate_file_and_reindex`` before its write.
    "mcp_edit": 2,
    "mcp_delete": 2,
    # The route before the lock, ``locked_source_chunk`` before its acquire, the
    # route again on the fresh chunk, ``mutate_source_and_reindex`` before its write.
    "web_edit": 4,
    # As the edit, without the helper: this route removes the lines itself.
    "web_delete": 3,
}


async def _index(comp, source: Path) -> None:
    stats = await comp.index_engine.index_file(source, ingress_guarded=True)
    assert not stats.errors
    assert stats.indexed_chunks == 1


def _clear_sidecars(directory: Path) -> None:
    """Indexing leaves its own ``.<name>.lock``; the observation starts without it."""
    for stale in directory.glob(".*.lock"):
        stale.unlink()


@pytest.fixture
async def unmanaged_source(bm25_only_components, tmp_path):
    """A file outside every index root, indexed by explicit path."""
    comp, _mem_dir = bm25_only_components
    host = tmp_path / "host"
    host.mkdir()
    source = host / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, source)
    _clear_sidecars(host)

    (chunk,) = await comp.storage.list_chunks_by_source(source)
    assert not chunk.metadata.source_read_only, "fixture must start from a writable chunk"
    assert chunk.metadata.source_span_hash, "fixture must start from usable provenance"
    assert not comp.index_engine.is_read_only_source(source)
    assert not comp.index_engine.is_managed_source(source)
    return comp, source, chunk


def _web_app(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    return app


async def _request(comp, surface, chunk_id):
    if surface.startswith("mcp"):
        ctx = StubCtx(AppContext.from_components(comp))
        if surface.endswith("edit"):
            return await memory_crud.mem_edit(str(chunk_id), "NEW BODY", ctx=ctx)
        return await memory_crud.mem_delete(chunk_id=str(chunk_id), ctx=ctx)
    transport = ASGITransport(app=_web_app(comp))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        if surface.endswith("edit"):
            return await client.patch(f"/api/chunks/{chunk_id}", json={"new_content": "NEW BODY"})
        return await client.delete(f"/api/chunks/{chunk_id}")


def _assert_detail(result, detail: str) -> None:
    if isinstance(result, str):
        assert result == f"Error: {detail}"
    else:
        assert result.status_code == 409, result.text
        assert result.json()["detail"] == detail


def _assert_went_through(result) -> None:
    if isinstance(result, str):
        assert not result.startswith("Error"), result
    else:
        assert result.status_code == 200, result.text


def _rows(comp):
    return comp.storage._get_db().execute("SELECT * FROM chunks").fetchall()


@pytest.mark.parametrize("surface", SURFACES)
async def test_an_unmanaged_source_is_refused_with_nothing_written(
    unmanaged_source, monkeypatch, surface
):
    """The reported sequence, on each surface that rewrites a chunk's file."""
    comp, source, chunk = unmanaged_source
    # Read back, not ``ORIGINAL.encode()``: ``write_text`` wrote the platform's newline.
    source_before = source.read_bytes()
    rows_before = _rows(comp)
    listing_before = sorted(p.name for p in source.parent.iterdir())
    assert listing_before == ["note.md"]
    index = AsyncMock(wraps=comp.index_engine.index_file)
    monkeypatch.setattr(comp.index_engine, "index_file", index)
    # MCP imports the writers inside the tool body; the web routes bind them at import.
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

    _assert_detail(await _request(comp, surface, chunk.id), UNMANAGED_SOURCE_DETAIL)

    assert source.read_bytes() == source_before
    assert _rows(comp) == rows_before
    # The lock sidecar is a write beside the file too, and it is created on
    # acquire, so its absence says the refusal came before the lock.
    assert sorted(p.name for p in source.parent.iterdir()) == listing_before
    index.assert_not_awaited()
    for spy in writes:
        spy.assert_not_called()


async def test_the_web_source_route_refuses_an_unmanaged_source(unmanaged_source):
    """``GET /api/chunks/{id}/source`` serves the text an edit replaces."""
    comp, _source, chunk = unmanaged_source
    transport = ASGITransport(app=_web_app(comp))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(f"/api/chunks/{chunk.id}/source")

    _assert_detail(response, UNMANAGED_SOURCE_DETAIL)


async def test_the_langgraph_delete_refuses_an_unmanaged_source(unmanaged_source):
    """It removes rows only, and takes the source's lock sidecar to do it."""
    from memtomem.integrations.langgraph import MemtomemStore

    comp, source, chunk = unmanaged_source
    rows_before = _rows(comp)
    store = MemtomemStore()
    store._components = comp

    with pytest.raises(PermissionError) as refused:
        await store.delete(str(chunk.id))

    assert str(refused.value) == UNMANAGED_SOURCE_DETAIL
    assert _rows(comp) == rows_before
    assert sorted(p.name for p in source.parent.iterdir()) == ["note.md"]


@pytest.mark.parametrize(
    ("surface", "passing"),
    [(surface, passing) for surface, asks in CONSULTS.items() for passing in range(asks)],
)
async def test_each_consult_on_the_path_refuses(
    bm25_only_components, monkeypatch, surface, passing
):
    """Each place a surface asks the question is a refusal point of its own.

    The gates overlap, so with one removed the others still refuse the
    reported sequence and the test above stays green. Here the source is a
    managed one and the predicate answers yes ``passing`` times before it
    answers no. A request that asks ``CONSULTS[surface]`` times must refuse for
    every smaller ``passing``; with a gate removed it asks one time fewer, and
    the last case writes the file.
    """
    comp, mem_dir = bm25_only_components
    source = mem_dir / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, source)
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    asked = []

    def is_managed_source(path, stored=None):
        asked.append(path)
        return len(asked) <= passing

    monkeypatch.setattr(comp.index_engine, "is_managed_source", is_managed_source)

    _assert_detail(await _request(comp, surface, chunk.id), UNMANAGED_SOURCE_DETAIL)

    assert len(asked) == passing + 1
    assert source.read_text(encoding="utf-8") == ORIGINAL


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_request_that_goes_through_asks_the_expected_number_of_times(
    bm25_only_components, monkeypatch, surface
):
    """Keeps ``CONSULTS`` true, which the test above reads its cases from."""
    comp, mem_dir = bm25_only_components
    source = mem_dir / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, source)
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    asked = Mock(wraps=comp.index_engine.is_managed_source)
    monkeypatch.setattr(comp.index_engine, "is_managed_source", asked)

    _assert_went_through(await _request(comp, surface, chunk.id))

    assert asked.call_count == CONSULTS[surface]
    assert source.read_text(encoding="utf-8") != ORIGINAL


@pytest.mark.parametrize("surface", ("mcp_edit", "mcp_delete"))
async def test_a_chunk_moved_to_an_unmanaged_source_under_the_lock_is_refused(
    bm25_only_components, tmp_path, monkeypatch, surface
):
    """``_locked_chunk`` re-keys onto a moved chunk's new path and locks that.

    The gate is inside its retry loop, so the path the second iteration is
    about to lock is judged too. The re-fetch under the first lock reports the
    chunk as moved to a file outside every root.
    """
    comp, mem_dir = bm25_only_components
    managed = mem_dir / "note.md"
    managed.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, managed)
    (chunk,) = await comp.storage.list_chunks_by_source(managed)
    host = tmp_path / "host"
    host.mkdir()
    unmanaged = host / "note.md"
    unmanaged.write_text(ORIGINAL, encoding="utf-8")

    moved = dataclasses.replace(
        chunk, metadata=dataclasses.replace(chunk.metadata, source_file=unmanaged)
    )
    real_get_chunk = comp.storage.get_chunk
    calls = {"n": 0}

    async def get_chunk(uid, **_kwargs):
        calls["n"] += 1
        return await real_get_chunk(uid) if calls["n"] == 1 else moved

    monkeypatch.setattr(comp.storage, "get_chunk", get_chunk)

    _assert_detail(await _request(comp, surface, chunk.id), UNMANAGED_SOURCE_DETAIL)

    assert calls["n"] >= 2, "the re-fetch under the lock did not happen"
    assert sorted(p.name for p in host.iterdir()) == ["note.md"]
    assert unmanaged.read_text(encoding="utf-8") == ORIGINAL
    assert managed.read_text(encoding="utf-8") == ORIGINAL


async def test_the_shared_mutation_helper_refuses_an_unmanaged_source(unmanaged_source):
    """The helper's own gate, for a caller that reaches it without asking."""
    comp, source, _chunk = unmanaged_source
    calls = []

    with pytest.raises(UnmanagedSourceError):
        await memory_mutation.mutate_source_and_reindex(
            comp.index_engine, source, lambda pre: calls.append(pre)
        )

    assert calls == [], "the mutation callback must never run"
    assert source.read_text(encoding="utf-8") == ORIGINAL


# --- controls ----------------------------------------------------------------


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_memory_directory_source_still_mutates(unmanaged_source, surface):
    """With an unmanaged source indexed beside it, so the location decides."""
    comp, _unmanaged, _chunk = unmanaged_source
    source = Path(comp.config.indexing.memory_dirs[0]) / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, source)
    (chunk,) = await comp.storage.list_chunks_by_source(source)

    _assert_went_through(await _request(comp, surface, chunk.id))

    assert source.read_text(encoding="utf-8") != ORIGINAL


@pytest.mark.parametrize("surface", SURFACES)
async def test_an_uploaded_source_still_mutates(
    bm25_only_components, tmp_path, monkeypatch, surface
):
    """The upload directory is outside every index root and stays editable."""
    comp, _mem_dir = bm25_only_components
    set_home(monkeypatch, tmp_path / "home")
    upload_dir = UPLOAD_DIR.expanduser()
    assert upload_dir.is_relative_to(tmp_path), "the upload directory must be the sandboxed one"
    upload_dir.mkdir(parents=True)
    source = upload_dir / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, source)
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    assert not any(
        resolves_under_any_root(source, [root]) for root in comp.config.indexing.all_index_roots()
    ), "the upload directory must not be an index root here"

    _assert_went_through(await _request(comp, surface, chunk.id))

    assert source.read_text(encoding="utf-8") != ORIGINAL


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_read_only_root_still_gets_the_read_only_refusal(
    bm25_only_components, tmp_path, surface
):
    """A read-only root is an index root: managed, and refused for its own reason."""
    comp, _mem_dir = bm25_only_components
    indexing = comp.config.indexing
    vault = tmp_path / "vault"
    vault.mkdir()
    source = vault / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    indexing.memory_dirs = [*indexing.memory_dirs, vault]
    await _index(comp, source)
    indexing.memory_dirs = [d for d in indexing.memory_dirs if d != vault]
    indexing.read_only_memory_dirs = [vault]
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    assert comp.index_engine.is_managed_source(source)

    _assert_detail(await _request(comp, surface, chunk.id), SOURCE_READ_ONLY_DETAIL)

    assert source.read_text(encoding="utf-8") == ORIGINAL


# --- the predicate -----------------------------------------------------------


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows without the right
        pytest.skip(f"cannot create a symlink here: {exc}")


async def test_the_predicate_judges_location(bm25_only_components, tmp_path, monkeypatch):
    comp, mem_dir = bm25_only_components
    set_home(monkeypatch, tmp_path / "home")
    managed = comp.index_engine.is_managed_source
    sibling = mem_dir.parent / (mem_dir.name + "2")
    sibling.mkdir()

    assert managed(mem_dir / "note.md")
    assert managed(mem_dir / "sub" / "note.md")
    assert managed(mem_dir)
    assert managed(UPLOAD_DIR.expanduser() / "note.md")
    assert not managed(tmp_path / "host" / "note.md")
    # ``/memories`` must not claim ``/memories2``.
    assert not managed(sibling / "note.md")
    assert not managed(UPLOAD_DIR.expanduser().parent / "config.json")


async def test_a_link_inside_a_root_pointing_out_of_it_is_not_managed(
    bm25_only_components, tmp_path
):
    """A write through the link lands on the target, so the target is what is judged.

    Checked in the other direction as well: a link outside every root that
    points into one names a file a root does hold.
    """
    comp, mem_dir = bm25_only_components
    host = tmp_path / "host"
    host.mkdir()
    outside = host / "settings.md"
    outside.write_text(ORIGINAL, encoding="utf-8")
    inside = mem_dir / "real.md"
    inside.write_text(ORIGINAL, encoding="utf-8")
    link_out = mem_dir / "link.md"
    link_in = host / "link.md"
    _symlink_or_skip(link_out, outside)
    _symlink_or_skip(link_in, inside)

    assert not comp.index_engine.is_managed_source(link_out)
    assert comp.index_engine.is_managed_source(link_in)


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_file_indexed_through_a_link_in_a_memory_directory_is_refused(
    bm25_only_components, tmp_path, surface
):
    """The link is in a memory directory; the bytes an edit would replace are not."""
    comp, mem_dir = bm25_only_components
    host = tmp_path / "host"
    host.mkdir()
    outside = host / "settings.md"
    outside.write_text(ORIGINAL, encoding="utf-8")
    link = mem_dir / "link.md"
    _symlink_or_skip(link, outside)
    await _index(comp, link)
    _clear_sidecars(host)
    chunks = await comp.storage.list_chunks_by_source(outside)
    assert len(chunks) == 1, "the chunk is stored under the path the link resolves to"

    result = await _request(comp, surface, chunks[0].id)

    if isinstance(result, str):
        assert result == f"Error: {UNMANAGED_SOURCE_DETAIL}"
    else:
        # The web edit route refuses a symlinked source by its own older rule
        # only when the stored key is itself the link; here it is the target.
        assert result.status_code == 409, result.text
        assert result.json()["detail"] == UNMANAGED_SOURCE_DETAIL
    assert outside.read_text(encoding="utf-8") == ORIGINAL
    assert sorted(p.name for p in host.iterdir()) == ["settings.md"]


def _project_tier(base: Path, name: str) -> tuple[Path, Path]:
    root = base / name
    (root / ".git").mkdir(parents=True)
    tier = root / ".memtomem" / "memories.local"
    tier.mkdir(parents=True)
    return root.resolve(), tier.resolve()


async def test_a_project_rows_own_tier_directory_is_managed_while_unregistered(
    bm25_only_components, tmp_path
):
    """So the row reaches the refusal that names how to register the tier again.

    ``test_scope_loss_reindex.py`` pins that refusal on each surface. This is
    the predicate's half: the stored tier counts, and only for a file in it.
    """
    comp, _mem_dir = bm25_only_components
    root, tier = _project_tier(tmp_path, "proj")
    other_root, other_tier = _project_tier(tmp_path, "other")
    comp.config.indexing.project_memory_dirs = [tier]
    source = tier / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, source)
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    stored = chunk.metadata
    assert (stored.scope, stored.project_root) == ("project_local", root)
    comp.config.indexing.project_memory_dirs = []
    managed = comp.index_engine.is_managed_source

    assert managed(source, stored)
    # The path alone says nothing: the tier is not registered.
    assert not managed(source)
    # Another project's tier directory is not this row's.
    assert not managed(other_tier / "note.md", stored)
    assert not managed(tmp_path / "host" / "note.md", stored)
    # A row stored as user memory has no tier of its own, wherever its file is.
    as_user = dataclasses.replace(stored, scope="user", project_root=None)
    assert not managed(source, as_user)
    assert other_root != root


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_project_row_naming_a_file_outside_its_tier_is_refused(
    bm25_only_components, tmp_path, monkeypatch, surface
):
    """A stored project scope does not open the gate for a file elsewhere."""
    comp, _mem_dir = bm25_only_components
    root, tier = _project_tier(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [tier]
    # From inside the project, so the row is within the caller's boundary and
    # the answer below is this gate's and not "no such chunk".
    monkeypatch.chdir(root)
    inside = tier / "note.md"
    inside.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, inside)
    (chunk,) = await comp.storage.list_chunks_by_source(inside)
    host = tmp_path / "host"
    host.mkdir()
    outside = host / "note.md"
    outside.write_text(ORIGINAL, encoding="utf-8")
    forged = dataclasses.replace(
        chunk, metadata=dataclasses.replace(chunk.metadata, source_file=outside)
    )
    assert forged.metadata.scope == "project_local"

    async def get_chunk(_uid, **_kwargs):
        return forged

    async def recall_chunks(**_kwargs):
        return [forged]

    monkeypatch.setattr(comp.storage, "get_chunk", get_chunk)
    monkeypatch.setattr(comp.storage, "recall_chunks", recall_chunks)

    result = await _request(comp, surface, chunk.id)

    if isinstance(result, str):
        assert result == f"Error: {UNMANAGED_SOURCE_DETAIL}"
    else:
        assert result.status_code == 409, result.text
        assert result.json()["detail"] == UNMANAGED_SOURCE_DETAIL
    assert outside.read_text(encoding="utf-8") == ORIGINAL
    assert sorted(p.name for p in host.iterdir()) == ["note.md"]


async def test_a_chunk_moved_onto_a_project_row_is_judged_with_that_rows_tier(
    bm25_only_components, tmp_path, monkeypatch
):
    """``_locked_chunk`` re-keys the row along with the path.

    The chunk starts as user memory and the re-fetch under the lock reports it
    as a project row whose tier is unregistered. Judged with the row it started
    from, the new path is outside every directory and gets this gate's refusal;
    judged with its own row, it reaches the one that names the tier.
    """
    comp, mem_dir = bm25_only_components
    root, tier = _project_tier(tmp_path, "proj")
    shared = root / ".memtomem" / "memories"
    shared.mkdir()
    comp.config.indexing.project_memory_dirs = [shared, tier]
    monkeypatch.chdir(root)
    user_source = mem_dir / "note.md"
    user_source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, user_source)
    (chunk,) = await comp.storage.list_chunks_by_source(user_source)
    project_source = tier / "note.md"
    project_source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, project_source)
    (project_chunk,) = await comp.storage.list_chunks_by_source(project_source)
    assert project_chunk.metadata.scope == "project_local"
    comp.config.indexing.project_memory_dirs = [shared]
    assert not comp.index_engine.is_managed_source(project_source, chunk.metadata)
    real_get_chunk = comp.storage.get_chunk
    calls = {"n": 0}

    async def get_chunk(uid, **_kwargs):
        calls["n"] += 1
        return await real_get_chunk(uid) if calls["n"] == 1 else project_chunk

    monkeypatch.setattr(comp.storage, "get_chunk", get_chunk)

    result = await _request(comp, "mcp_edit", chunk.id)

    assert result.startswith(f"Error: chunk {chunk.id} rewrite refused: "), result
    assert project_source.read_text(encoding="utf-8") == ORIGINAL


@pytest.mark.parametrize("linked", ("tier", "dot_memtomem"))
@pytest.mark.parametrize("surface", SURFACES)
async def test_a_registered_tier_that_is_a_link_manages_nothing(
    bm25_only_components, tmp_path, surface, linked
):
    """A checkout can deliver the tier directory, or ``.memtomem``, as a link.

    Roots are resolved before they are matched, so ``memories -> ..`` would
    make every file of the project a file under a registered root.
    """
    comp, _mem_dir = bm25_only_components
    root = (tmp_path / "proj").resolve()
    (root / ".git").mkdir(parents=True)
    if linked == "tier":
        (root / ".memtomem").mkdir()
        _symlink_or_skip(root / ".memtomem" / "memories", Path(".."))
    else:
        _symlink_or_skip(root / ".memtomem", Path("."))
        (root / "memories").mkdir()
    tier = root / ".memtomem" / "memories"
    comp.config.indexing.project_memory_dirs = [tier]
    target = (root if linked == "tier" else root / "memories") / "RULES.md"
    target.write_text(ORIGINAL, encoding="utf-8")
    assert resolves_under_any_root(target, [tier]), "the link must lead to the file's directory"
    await _index(comp, target)
    _clear_sidecars(target.parent)
    listing = sorted(p.name for p in target.parent.iterdir())
    (chunk,) = await comp.storage.list_chunks_by_source(target)
    assert not comp.index_engine.is_managed_source(target, chunk.metadata)

    _assert_detail(await _request(comp, surface, chunk.id), UNMANAGED_SOURCE_DETAIL)

    assert target.read_text(encoding="utf-8") == ORIGINAL
    assert sorted(p.name for p in target.parent.iterdir()) == listing


async def test_a_stored_tier_that_is_a_link_manages_nothing(bm25_only_components, tmp_path):
    """The stored tier is resolved like a registered one, so the same rule holds."""
    comp, _mem_dir = bm25_only_components
    root, tier = _project_tier(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [tier]
    source = tier / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, source)
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    comp.config.indexing.project_memory_dirs = []
    assert comp.index_engine.is_managed_source(source, chunk.metadata)
    source.unlink()
    for stale in tier.iterdir():
        stale.unlink()
    tier.rmdir()
    _symlink_or_skip(tier, Path(".."))
    elsewhere = root / "RULES.md"
    elsewhere.write_text(ORIGINAL, encoding="utf-8")
    assert resolves_under_any_root(elsewhere, [tier]), "the link must lead to the file's directory"

    assert not comp.index_engine.is_managed_source(elsewhere, chunk.metadata)


async def test_a_tier_that_appears_later_as_a_link_manages_nothing(bm25_only_components, tmp_path):
    """Asked on each call: a registered tier may not exist until a checkout makes it."""
    comp, _mem_dir = bm25_only_components
    root = (tmp_path / "proj").resolve()
    (root / ".memtomem").mkdir(parents=True)
    tier = root / ".memtomem" / "memories"
    comp.config.indexing.project_memory_dirs = [tier]
    assert comp.index_engine.is_managed_source(tier / "note.md")

    _symlink_or_skip(tier, Path(".."))

    assert not comp.index_engine.is_managed_source(tier / "note.md")
    assert not comp.index_engine.is_managed_source(root / "RULES.md")


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_user_memory_directory_that_is_a_link_still_mutates(
    bm25_only_components, tmp_path, surface
):
    """``memory_dirs`` is the user's own configuration, and a link there is theirs."""
    comp, _mem_dir = bm25_only_components
    real = tmp_path / "elsewhere" / "notes"
    real.mkdir(parents=True)
    link = tmp_path / "notes"
    _symlink_or_skip(link, real)
    comp.config.indexing.memory_dirs = [*comp.config.indexing.memory_dirs, link]
    source = link / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, source)
    (chunk,) = await comp.storage.list_chunks_by_source(real / "note.md")

    _assert_went_through(await _request(comp, surface, chunk.id))

    assert (real / "note.md").read_text(encoding="utf-8") != ORIGINAL


async def test_a_linked_directory_registered_as_both_kinds_stays_managed(
    bm25_only_components, tmp_path
):
    """The user entry is the user's, whatever the project entry beside it is."""
    comp, _mem_dir = bm25_only_components
    indexing = comp.config.indexing
    real = tmp_path / "real"
    real.mkdir()
    root = (tmp_path / "proj").resolve()
    (root / ".memtomem").mkdir(parents=True)
    tier = root / ".memtomem" / "memories"
    _symlink_or_skip(tier, real)
    note = tier / "note.md"
    indexing.project_memory_dirs = [tier]
    assert not comp.index_engine.is_managed_source(note)

    indexing.memory_dirs = [*indexing.memory_dirs, tier]

    assert comp.index_engine.is_managed_source(note)


async def test_a_stored_tier_that_is_also_a_linked_user_directory_stays_managed(
    bm25_only_components, tmp_path
):
    """The stored tier is a project entry too, and collides the same way."""
    comp, _mem_dir = bm25_only_components
    indexing = comp.config.indexing
    root, tier = _project_tier(tmp_path, "proj")
    indexing.project_memory_dirs = [tier]
    source = tier / "note.md"
    source.write_text(ORIGINAL, encoding="utf-8")
    await _index(comp, source)
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    indexing.project_memory_dirs = []
    real = tmp_path / "real"
    for entry in tier.iterdir():
        entry.unlink()
    tier.rmdir()
    real.mkdir()
    _symlink_or_skip(tier, real)
    assert not comp.index_engine.is_managed_source(tier / "note.md", chunk.metadata)

    indexing.memory_dirs = [*indexing.memory_dirs, tier]

    assert comp.index_engine.is_managed_source(tier / "note.md", chunk.metadata)


async def test_the_managed_roots_cover_every_kind_of_index_root(bm25_only_components, tmp_path):
    """``is_managed_source`` lists the kinds apart, so a new kind has to be placed.

    It cannot take ``all_index_roots()`` whole: the project tiers in it are
    judged differently from the user's roots. So each kind is asked about with
    a file only it holds, and the list of kinds is compared with the config's.
    """
    comp, _mem_dir = bm25_only_components
    indexing = comp.config.indexing
    user, vault = tmp_path / "user", tmp_path / "vault"
    _root, tier = _project_tier(tmp_path, "proj")
    indexing.memory_dirs = [user]
    indexing.project_memory_dirs = [tier]
    indexing.read_only_memory_dirs = [vault]

    for root in (user, tier, vault):
        assert comp.index_engine.is_managed_source(root / "note.md"), root
    assert sorted(indexing.all_index_roots()) == sorted([user, tier, vault])
    assert sorted(indexing.all_index_roots()) == sorted(
        [*indexing.memory_dirs, *indexing.project_memory_dirs, *indexing.read_only_memory_dirs]
    )


async def test_a_tier_that_cannot_be_inspected_is_unused_and_breaks_nothing_else(
    bm25_only_components, tmp_path, monkeypatch
):
    """``lstat`` fails for an entry in a directory the process may not search.

    One such registered tier must not fail the question for every other file.
    Injected here so it runs on every platform; the test below removes the
    permission for real where that is possible.
    """
    comp, mem_dir = bm25_only_components
    _root, tier = _project_tier(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [tier]
    assert comp.index_engine.is_managed_source(tier / "note.md")
    real_lstat = Path.lstat

    def lstat(self):
        if self == tier:
            raise PermissionError(13, "Permission denied", str(self))
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", lstat)

    assert comp.index_engine.is_managed_source(mem_dir / "note.md")
    assert not comp.index_engine.is_managed_source(tier / "note.md")


@pytest.mark.skipif(
    os.name == "nt" or getattr(os, "geteuid", lambda: 1)() == 0,
    reason="needs a directory whose search permission can be taken from this user",
)
async def test_a_tier_in_an_unsearchable_directory_is_unused(bm25_only_components, tmp_path):
    """The same, with the permission removed for real."""
    comp, mem_dir = bm25_only_components
    root, tier = _project_tier(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [tier]
    assert comp.index_engine.is_managed_source(tier / "note.md")
    dot_memtomem = root / ".memtomem"
    dot_memtomem.chmod(0)
    try:
        assert comp.index_engine.is_managed_source(mem_dir / "note.md")
        assert not comp.index_engine.is_managed_source(tier / "note.md")
    finally:
        dot_memtomem.chmod(0o700)


async def test_a_tier_that_is_a_junction_manages_nothing(
    bm25_only_components, tmp_path, monkeypatch
):
    """A junction redirects a directory as a symlink does. Injected off Windows."""
    comp, _mem_dir = bm25_only_components
    root, tier = _project_tier(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [tier]
    assert comp.index_engine.is_managed_source(tier / "note.md")

    monkeypatch.setattr(Path, "is_junction", lambda self: self == root / ".memtomem")

    assert not comp.index_engine.is_managed_source(tier / "note.md")


async def test_a_symlink_loop_outside_every_root_is_not_managed(bm25_only_components, tmp_path):
    """A loop resolves to a path that cannot be opened, and to one outside the roots."""
    comp, _mem_dir = bm25_only_components
    host = tmp_path / "host"
    host.mkdir()
    _symlink_or_skip(host / "a.md", host / "b.md")
    _symlink_or_skip(host / "b.md", host / "a.md")

    assert not comp.index_engine.is_managed_source(host / "a.md")


@pytest.mark.parametrize("scope", ("project_shared", "project_local"))
def test_the_spelled_tier_directory_leads_where_the_resolved_one_is(tmp_path, scope):
    """``project_tier_dir`` and ``resolve_memory_scope_dir`` name one directory."""
    from memtomem.memory_scope import project_tier_dir, resolve_memory_scope_dir

    root = tmp_path.resolve()
    assert project_tier_dir(scope, root) == resolve_memory_scope_dir(scope, root)


def test_a_path_whose_resolution_raises_is_under_nothing(tmp_path, monkeypatch):
    """``norm_path`` falls back to the spelling as typed; a permission must not."""
    from memtomem.storage import sqlite_helpers

    target = tmp_path / "note.md"
    assert resolves_under_any_root(target, [tmp_path])

    def unresolvable(_path):
        raise OSError("cannot resolve")

    monkeypatch.setattr(sqlite_helpers, "resolve_path", unresolvable)

    assert not resolves_under_any_root(target, [tmp_path])


class _ZeroInode:
    """A ``stat`` result from a filesystem that does not report inode numbers."""

    def __init__(self, result):
        self._result = result

    def __getattr__(self, name):
        return 0 if name == "st_ino" else getattr(self._result, name)


def test_a_filesystem_without_inode_numbers_grants_nothing_by_identity(tmp_path, monkeypatch):
    """Every directory on such a filesystem has the root's ``(st_dev, 0)``.

    The refusing rule matches on it and over-refuses. The permitting rule must
    not: it keeps the literal prefix and drops identity there.
    """
    from memtomem.storage.sqlite_helpers import is_under_any_root

    root, sibling = tmp_path / "notes", tmp_path / "other"
    root.mkdir()
    sibling.mkdir()
    real_stat = Path.stat
    monkeypatch.setattr(Path, "stat", lambda self, **kw: _ZeroInode(real_stat(self, **kw)))

    assert not resolves_under_any_root(sibling / "note.md", [root])
    assert resolves_under_any_root(root / "note.md", [root])
    assert is_under_any_root(sibling / "note.md", [root]), "the refusing rule is unchanged"


def test_a_differently_cased_spelling_of_a_root_is_still_under_it(tmp_path):
    """What identity is for, where the filesystem folds case and reports inodes."""
    root = tmp_path / "Notes"
    root.mkdir()
    other_case = tmp_path / "notes"
    if not other_case.exists():
        pytest.skip("this filesystem is case-sensitive")

    assert resolves_under_any_root(other_case / "note.md", [root])


def test_no_roots_manage_nothing(tmp_path):
    assert not resolves_under_any_root(tmp_path / "note.md", [])
