"""Re-indexing never turns project memory into user memory (#2923).

A re-index classifies a file against the tiers registered now. Once a project
tier is unregistered, rewriting its rows would stamp them ``user``, readable
from every project. The engine holds such a source instead of re-indexing it,
and the by-id rewrite surfaces refuse before they write. Every test runs on a
real BM25-only store.
"""

from __future__ import annotations

import os
import shutil
import sys
import unicodedata
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools.memory_crud import mem_delete, mem_edit
from memtomem.storage.source_holds import SCOPE_LOST_HOLD
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured

_NOTE = "## Plans\n\nprivate falcon roadmap\n\n## Later\n\nsecond falcon item\n"


def _project(base: Path, name: str) -> tuple[Path, Path, Path]:
    root = base / name
    (root / ".git").mkdir(parents=True)
    local = root / ".memtomem" / "memories.local"
    local.mkdir(parents=True)
    shared = root / ".memtomem" / "memories"
    shared.mkdir()
    return root.resolve(), local.resolve(), shared.resolve()


async def _indexed_note(comp, tier: Path, name: str = "note.md") -> Path:
    target = tier / name
    target.write_text(_NOTE, encoding="utf-8")
    stats = await comp.index_engine.index_file(target)
    assert not stats.errors, stats.errors
    return target


async def _scopes(comp, target: Path) -> set[tuple[str, str | None]]:
    rows = await comp.storage.list_chunks_by_source(target, limit=None)
    return {
        (c.metadata.scope, str(c.metadata.project_root) if c.metadata.project_root else None)
        for c in rows
    }


def _hold_reason(comp, key: str) -> str | None:
    row = (
        comp.storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file=?", (key,))
        .fetchone()
    )
    return row[0] if row else None


def _user_rows(comp) -> int:
    return (
        comp.storage._get_db()
        .execute("SELECT COUNT(*) FROM chunks WHERE content LIKE '%falcon%' AND scope='user'")
        .fetchone()[0]
    )


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


async def _chunk_id(comp, target: Path, needle: str = "roadmap") -> str:
    rows = await comp.storage.list_chunks_by_source(target, limit=None)
    return next(str(c.id) for c in rows if needle in c.content)


# ---------------------------------------------------------------------------
# The indexing chokepoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forced_reindex_after_unregistration_holds_instead_of_publishing(
    bm25_only_components, tmp_path: Path
) -> None:
    comp, _ = bm25_only_components
    root, local, _shared = _project(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [local]
    target = await _indexed_note(comp, local)
    assert await _scopes(comp, target) == {("project_local", str(root))}

    comp.config.indexing.project_memory_dirs = []
    stats = await comp.index_engine.index_file(target, force=True)

    assert stats.scope_held
    assert stats.errors and "no longer registered" in stats.errors[0]
    assert "/" not in stats.errors[0], "bulk errors reach the web: bare names only"
    assert await _scopes(comp, target) == {("project_local", str(root))}
    assert _user_rows(comp) == 0
    assert _hold_reason(comp, str(target)) == SCOPE_LOST_HOLD

    comp.config.indexing.project_memory_dirs = [local]
    stats = await comp.index_engine.index_file(target, force=True)
    assert not stats.errors and not stats.scope_held
    assert _hold_reason(comp, str(target)) is None
    assert await _scopes(comp, target) == {("project_local", str(root))}


@pytest.mark.asyncio
async def test_a_changed_file_is_held_on_a_plain_reindex(
    bm25_only_components, tmp_path: Path
) -> None:
    comp, _ = bm25_only_components
    root, local, _shared = _project(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [local]
    target = await _indexed_note(comp, local)

    comp.config.indexing.project_memory_dirs = []
    with target.open("a", encoding="utf-8") as fh:
        fh.write("\n## Added\n\nthird falcon item\n")
    stats = await comp.index_engine.index_file(target)

    assert stats.scope_held
    assert _user_rows(comp) == 0
    assert _hold_reason(comp, str(target)) == SCOPE_LOST_HOLD


@pytest.mark.asyncio
async def test_an_unchanged_file_is_left_alone_and_a_hold_outlives_unchanged_passes(
    bm25_only_components, tmp_path: Path
) -> None:
    comp, _ = bm25_only_components
    root, local, _shared = _project(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [local]
    target = await _indexed_note(comp, local)

    # Unregistering invalidates the receipt, but nothing would be written.
    comp.config.indexing.project_memory_dirs = []
    stats = await comp.index_engine.index_file(target)
    assert not stats.errors and not stats.scope_held
    assert _hold_reason(comp, str(target)) is None
    assert await _scopes(comp, target) == {("project_local", str(root))}

    # Once held, an unchanged pass must not count as the clean pass that lifts it.
    await comp.index_engine.index_file(target, force=True)
    assert _hold_reason(comp, str(target)) == SCOPE_LOST_HOLD
    stats = await comp.index_engine.index_file(target)
    assert stats.scope_held
    assert _hold_reason(comp, str(target)) == SCOPE_LOST_HOLD

    comp.config.indexing.project_memory_dirs = [local]
    stats = await comp.index_engine.index_file(target)
    assert not stats.errors
    assert _hold_reason(comp, str(target)) is None


@pytest.mark.asyncio
async def test_bulk_indexing_reports_the_hold_without_raising(
    bm25_only_components, tmp_path: Path
) -> None:
    comp, _ = bm25_only_components
    root, local, _shared = _project(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [local]
    target = await _indexed_note(comp, local)

    # The project is also under a configured memory dir, so a bulk walk still
    # reaches the tier once it is unregistered — and classifies it as user.
    comp.config.indexing.memory_dirs = [*comp.config.indexing.memory_dirs, root]
    comp.config.indexing.project_memory_dirs = []
    stats = await comp.index_engine.index_path(root, force=True)

    assert stats.scope_held
    assert any("no longer registered" in e for e in stats.errors), stats.errors
    assert _user_rows(comp) == 0
    assert _hold_reason(comp, str(target)) == SCOPE_LOST_HOLD

    # The streaming path carries the flag per file and on completion.
    events = [e async for e in comp.index_engine.index_path_stream(root, force=True)]
    progress = [e for e in events if e["type"] == "progress"]
    complete = next(e for e in events if e["type"] == "complete")
    assert any(e["scope_held"] for e in progress)
    assert complete["scope_held"]
    assert _user_rows(comp) == 0


@pytest.mark.asyncio
async def test_a_root_stamped_by_another_project_is_not_rewritten(
    bm25_only_components, tmp_path: Path
) -> None:
    # The state ``memory-migrate`` leaves when A's tier directory is a symlink
    # into B's: the canonical key is under B, the stored root is A. The tier
    # string matches; the project does not.
    comp, _ = bm25_only_components
    a_root, _a_local, _ = _project(tmp_path, "a")
    _b_root, b_local, _ = _project(tmp_path, "b")
    comp.config.indexing.project_memory_dirs = [b_local]
    target = await _indexed_note(comp, b_local)
    db = comp.storage._get_db()
    db.execute("UPDATE chunks SET project_root=? WHERE source_file=?", (str(a_root), str(target)))
    db.commit()

    stats = await comp.index_engine.index_file(target, force=True)

    assert stats.scope_held
    assert await _scopes(comp, target) == {("project_local", str(a_root))}


@pytest.mark.asyncio
async def test_an_emptied_file_still_deletes_and_escalation_is_not_judged(
    bm25_only_components, tmp_path: Path
) -> None:
    comp, _ = bm25_only_components
    root, local, _shared = _project(tmp_path, "proj")
    target = await _indexed_note(comp, local)  # tier unregistered: user rows
    assert await _scopes(comp, target) == {("user", None)}

    comp.config.indexing.project_memory_dirs = [local]
    stats = await comp.index_engine.index_file(target, force=True)
    assert not stats.errors and not stats.scope_held
    assert await _scopes(comp, target) == {("project_local", str(root))}

    comp.config.indexing.project_memory_dirs = []
    target.write_text("", encoding="utf-8")
    stats = await comp.index_engine.index_file(target)
    assert not stats.scope_held
    assert await _scopes(comp, target) == set()


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="NFC/NFD fold only on macOS")
async def test_a_legacy_nfd_key_is_judged_and_held(bm25_only_components, tmp_path: Path) -> None:
    comp, _ = bm25_only_components
    root, local, _shared = _project(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [local]
    target = await _indexed_note(comp, local, unicodedata.normalize("NFC", "café.md"))
    nfd_key = unicodedata.normalize("NFD", str(target))
    assert nfd_key != str(target)
    db = comp.storage._get_db()
    db.execute("UPDATE chunks SET source_file=? WHERE source_file=?", (nfd_key, str(target)))
    db.commit()

    comp.config.indexing.project_memory_dirs = []
    stats = await comp.index_engine.index_file(target, force=True)

    # Judged and refused, but not held under the legacy key: no clean pass
    # would ever release it. ``scope_held`` says nothing was hidden. The rows
    # keep their project scope (they also stay searchable with their indexed
    # content, a limit of the pre-existing NFC/NFD key split, #2926).
    assert stats.errors and "no longer registered" in stats.errors[0]
    assert not stats.scope_held
    assert _user_rows(comp) == 0
    assert _hold_reason(comp, nfd_key) is None
    rows = (
        comp.storage._get_db()
        .execute("SELECT DISTINCT scope FROM chunks WHERE source_file=?", (nfd_key,))
        .fetchall()
    )
    assert rows == [("project_local",)]

    comp.config.indexing.project_memory_dirs = [local]
    stats = await comp.index_engine.index_file(target, force=True)
    assert not stats.errors and not stats.scope_held
    assert _hold_reason(comp, nfd_key) is None
    assert _hold_reason(comp, str(target)) is None


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "darwin", reason="NFC/NFD fold only on macOS")
async def test_a_partly_legacy_source_does_not_claim_its_memory_is_hidden(
    bm25_only_components, tmp_path: Path
) -> None:
    # Rows under both spellings: the folded key's rows are held, the legacy
    # NFD rows cannot be, so the result must not report the source as hidden.
    comp, _ = bm25_only_components
    _root, local, _shared = _project(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [local]
    target = await _indexed_note(comp, local, unicodedata.normalize("NFC", "café.md"))
    nfd_key = unicodedata.normalize("NFD", str(target))
    db = comp.storage._get_db()
    db.execute(
        "UPDATE chunks SET source_file=? WHERE source_file=? AND content LIKE '%second%'",
        (nfd_key, str(target)),
    )
    db.commit()

    comp.config.indexing.project_memory_dirs = []
    stats = await comp.index_engine.index_file(target, force=True)

    assert stats.errors and not stats.scope_held
    assert _user_rows(comp) == 0
    assert _hold_reason(comp, str(target)) == SCOPE_LOST_HOLD


# ---------------------------------------------------------------------------
# By-id rewrites refuse before writing
# ---------------------------------------------------------------------------


@pytest.fixture
def local_unregistered(bm25_only_components, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A cwd project whose local tier was unregistered while its shared tier stayed."""

    async def _make():
        comp, _ = bm25_only_components
        root, local, shared = _project(tmp_path, "proj")
        comp.config.indexing.project_memory_dirs = [shared, local]
        monkeypatch.chdir(root)
        target = await _indexed_note(comp, local)
        comp.config.indexing.project_memory_dirs = [shared]
        return comp, root, target

    return _make


@pytest.mark.asyncio
async def test_web_edit_and_delete_refuse_after_the_tier_is_unregistered(
    local_unregistered,
) -> None:
    comp, _root, target = await local_unregistered()
    before = target.read_bytes()
    chunk_id = await _chunk_id(comp, target)

    async with _web_client(comp) as client:
        edited = await client.patch(f"/api/chunks/{chunk_id}", json={"new_content": "revised"})
        deleted = await client.delete(f"/api/chunks/{chunk_id}")

    for resp in (edited, deleted):
        assert resp.status_code == 409, resp.text
        message = resp.json()["detail"]["message"]
        assert "no longer registered" in message and "nothing was written" in message
        for foreign in ("mm ", "mem_", "`", "/"):
            assert foreign not in message, (foreign, message)
    assert target.read_bytes() == before
    assert _user_rows(comp) == 0


@pytest.mark.asyncio
async def test_mcp_edit_names_the_commands(local_unregistered) -> None:
    comp, root, target = await local_unregistered()
    ctx = StubCtx(AppContext.from_components(comp))
    chunk_id = await _chunk_id(comp, target)

    edited = await mem_edit(chunk_id=chunk_id, new_content="revised", ctx=ctx)

    assert edited.startswith(f"Error: chunk {chunk_id} rewrite refused: "), edited
    assert f"cd {root} && mm mem init --scope project_local" in edited
    assert f"mm context memory-migrate {target} --from project_local --to user" in edited


@pytest.mark.asyncio
async def test_a_tier_unregistered_mid_edit_is_held_and_reported(
    local_unregistered, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Issue path 1: the early check passes, then the tier goes before the re-index.
    comp, root, target = await local_unregistered()
    shared = comp.config.indexing.project_memory_dirs[0]
    local = target.parent
    engine = comp.index_engine
    real = engine.rewrite_refusal

    async def _passes_then_unregisters(*args, **kwargs):
        comp.config.indexing.project_memory_dirs = [shared, local]
        answer = await real(*args, **kwargs)
        comp.config.indexing.project_memory_dirs = [shared]
        return answer

    monkeypatch.setattr(engine, "rewrite_refusal", _passes_then_unregisters)
    ctx = StubCtx(AppContext.from_components(comp))
    chunk_id = await _chunk_id(comp, target)

    edited = await mem_edit(chunk_id=chunk_id, new_content="revised falcon plan", ctx=ctx)

    assert edited.startswith("Memory updated in"), edited
    assert "is on disk" in edited and "held" in edited
    assert "revised falcon plan" in target.read_text(encoding="utf-8")
    assert _user_rows(comp) == 0
    assert await _scopes(comp, target) == {("project_local", str(root))}
    assert _hold_reason(comp, str(target)) == SCOPE_LOST_HOLD


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["edit", "delete"])
async def test_web_race_carries_the_warning(
    local_unregistered, monkeypatch: pytest.MonkeyPatch, op: str
) -> None:
    comp, _root, target = await local_unregistered()
    shared = comp.config.indexing.project_memory_dirs[0]
    local = target.parent
    engine = comp.index_engine
    real = engine.rewrite_refusal

    async def _passes_then_unregisters(*args, **kwargs):
        comp.config.indexing.project_memory_dirs = [shared, local]
        answer = await real(*args, **kwargs)
        comp.config.indexing.project_memory_dirs = [shared]
        return answer

    monkeypatch.setattr(engine, "rewrite_refusal", _passes_then_unregisters)
    chunk_id = await _chunk_id(comp, target)
    async with _web_client(comp) as client:
        if op == "edit":
            resp = await client.patch(f"/api/chunks/{chunk_id}", json={"new_content": "revised"})
        else:
            resp = await client.delete(f"/api/chunks/{chunk_id}")

    assert resp.status_code == 200, resp.text
    assert "is on disk" in resp.json()["warning"]
    assert _user_rows(comp) == 0
    assert _hold_reason(comp, str(target)) == SCOPE_LOST_HOLD


# ---------------------------------------------------------------------------
# A stored key that became a symlink into another project (issue path 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
@pytest.mark.parametrize("b_indexed", [False, True])
async def test_a_relinked_key_is_refused_before_writing_into_the_other_project(
    bm25_only_components, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, b_indexed: bool
) -> None:
    comp, _ = bm25_only_components
    a_root, a_local, a_shared = _project(tmp_path, "a")
    _b_root, b_local, _b_shared = _project(tmp_path, "b")
    comp.config.indexing.project_memory_dirs = [a_shared, a_local, b_local]
    monkeypatch.chdir(a_root)
    target = await _indexed_note(comp, a_local)
    chunk_id = await _chunk_id(comp, target)
    moved = b_local / target.name
    shutil.move(target, moved)
    target.symlink_to(moved)
    if b_indexed:
        await comp.index_engine.index_file(moved)
    before = moved.read_bytes()

    ctx = StubCtx(AppContext.from_components(comp))
    edited = await mem_edit(chunk_id=chunk_id, new_content="revised", ctx=ctx)
    deleted = await mem_delete(chunk_id=chunk_id, ctx=ctx)
    async with _web_client(comp) as client:
        web_deleted = await client.delete(f"/api/chunks/{chunk_id}")

    assert "now resolves to another file" in edited, edited
    assert "now resolves to another file" in deleted, deleted
    assert web_deleted.status_code == 409, web_deleted.text
    assert "now resolves to another file" in web_deleted.json()["detail"]["message"]
    assert moved.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
async def test_web_edit_refuses_a_key_whose_directory_became_a_symlink(
    bm25_only_components, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The leaf is not a symlink, so the route's own ``is_symlink`` check passes;
    # the helper's relink check is what refuses.
    comp, _ = bm25_only_components
    a_root, a_local, a_shared = _project(tmp_path, "a")
    _b_root, b_local, _b_shared = _project(tmp_path, "b")
    sub = a_local / "topic"
    sub.mkdir()
    comp.config.indexing.project_memory_dirs = [a_shared, a_local, b_local]
    monkeypatch.chdir(a_root)
    target = await _indexed_note(comp, sub)
    chunk_id = await _chunk_id(comp, target)
    moved_dir = b_local / "topic"
    shutil.move(sub, moved_dir)
    sub.symlink_to(moved_dir, target_is_directory=True)
    before = (moved_dir / target.name).read_bytes()

    async with _web_client(comp) as client:
        edited = await client.patch(f"/api/chunks/{chunk_id}", json={"new_content": "revised"})

    assert edited.status_code == 409, edited.text
    assert "now resolves to another file" in edited.json()["detail"]["message"]
    assert (moved_dir / target.name).read_bytes() == before
