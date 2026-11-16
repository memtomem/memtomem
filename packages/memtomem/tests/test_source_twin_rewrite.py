"""A rewrite never removes lines from a file the store holds under two spellings (#3408).

On a case-insensitive volume ``memories/a.md`` and ``MEMORIES/a.md`` are one
file. Indexed through both, the store holds its lines under two keys, and a
delete through one used to take the lines out of the only real file while the
other key's rows kept serving them. ``IndexEngine.rewrite_refusal`` now refuses
such a rewrite; the Sources tab's index-only delete is the way out.

The second spelling is seeded by re-keying rows, not by indexing two
spellings: on Windows ``resolve()`` may already return one spelling, and how a
store came to hold two is not what this file tests. Every test runs on a real
BM25-only store.
"""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import unicodedata
from pathlib import Path
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools.memory_crud import mem_delete, mem_edit
from memtomem.storage import sqlite_helpers
from memtomem.storage.sqlite_helpers import (
    _source_identity,
    source_twins,
    twin_candidates,
)
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured

_NOTE = "## Plans\n\nheron rotation plan\n\n## Later\n\nsecond heron item\n"


def _case_insensitive(base: Path) -> bool:
    probe = base / "CaseProbe"
    probe.mkdir(exist_ok=True)
    try:
        return (base / "caseprobe").exists()
    finally:
        probe.rmdir()


@pytest.fixture
def case_insensitive(tmp_path: Path) -> None:
    if not _case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive volume (macOS, Windows)")


def _respelled(target: Path) -> str:
    """``target`` with its parent directory's name in upper case: the same file here."""
    return str(target.parent.parent / target.parent.name.upper() / target.name)


async def _twinned(comp, target: Path, second: str) -> None:
    """Index ``target``, then give the store a second set of rows under ``second``."""
    target.write_text(_NOTE, encoding="utf-8")
    stats = await comp.index_engine.index_file(target)
    assert not stats.errors, stats.errors
    db = comp.storage._get_db()
    db.execute("UPDATE chunks SET source_file=? WHERE source_file=?", (second, str(target)))
    db.commit()
    stats = await comp.index_engine.index_file(target, force=True)
    assert not stats.errors, stats.errors
    assert _counts(comp) == {str(target): 2, second: 2}


def _counts(comp) -> dict[str, int]:
    rows = (
        comp.storage._get_db()
        .execute("SELECT source_file, COUNT(*) FROM chunks GROUP BY source_file")
        .fetchall()
    )
    return {row[0]: row[1] for row in rows}


def _chunk_id(comp, key: str, needle: str = "rotation") -> str:
    row = (
        comp.storage._get_db()
        .execute(
            "SELECT id FROM chunks WHERE source_file=? AND content LIKE ?", (key, f"%{needle}%")
        )
        .fetchone()
    )
    assert row is not None, key
    return row[0]


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


def _moved_on_this_platform(key: str) -> bool:
    """Whether the key is refused as relinked before the twin check is reached.

    On Windows ``resolve()`` returns the on-disk spelling, so a key in another
    case no longer resolves to itself there; on macOS it does.
    """
    return sqlite_helpers.stored_key_moved(key)


# ---------------------------------------------------------------------------
# The twin predicate
# ---------------------------------------------------------------------------


def test_candidates_are_caseless_equal_keys_in_a_fixed_order() -> None:
    key = "/m/notes/café.md"
    stored = [
        "/m/NOTES/café.md",
        "/m/notes/" + unicodedata.normalize("NFD", "café.md"),
        "/m/Notes/CAFÉ.md",
        key,
        "/m/notes/cafe.md",
        "/m/other/café.md",
    ]
    assert twin_candidates(key, stored) == sorted(
        [
            "/m/NOTES/café.md",
            "/m/notes/" + unicodedata.normalize("NFD", "café.md"),
            "/m/Notes/CAFÉ.md",
        ]
    )
    assert twin_candidates(key, list(reversed(stored))) == twin_candidates(key, stored)


@pytest.mark.skipif(os.name != "nt", reason="separator folding is Windows-only")
def test_candidates_fold_separators_on_windows() -> None:
    assert twin_candidates("C:\\m\\a.md", ["C:/M/a.md"]) == ["C:/M/a.md"]


def test_a_case_spelling_of_one_file_is_a_twin(tmp_path: Path, case_insensitive) -> None:
    target = tmp_path / "memories" / "a.md"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    second = _respelled(target)
    assert source_twins(str(target), [second, str(target)]) == [second]
    assert source_twins(second, [str(target)]) == [str(target)]


def test_two_files_with_caseless_equal_names_are_not_twins(tmp_path: Path) -> None:
    if _case_insensitive(tmp_path):
        pytest.skip("needs a case-sensitive volume (Linux)")
    lower = tmp_path / "notes" / "a.md"
    upper = tmp_path / "NOTES" / "a.md"
    for path in (lower, upper):
        path.parent.mkdir()
        path.write_text("x", encoding="utf-8")
    assert twin_candidates(str(lower), [str(upper)]) == [str(upper)]
    assert source_twins(str(lower), [str(upper)]) == []


def test_a_caseless_match_alone_is_not_a_twin(tmp_path: Path, monkeypatch) -> None:
    """Two distinct files are never twins, whatever the string prefilter says.

    Runs on every platform: the prefilter is widened so two unrelated files are
    candidates, and only the on-disk identity can tell them apart.
    """
    first = tmp_path / "a.md"
    other = tmp_path / "b.md"
    for path in (first, other):
        path.write_text("x", encoding="utf-8")
    monkeypatch.setattr(sqlite_helpers, "_caseless", lambda key: "")
    assert twin_candidates(str(first), [str(other)]) == [str(other)]
    assert source_twins(str(first), [str(other)]) == []


def test_identity_does_not_follow_a_leaf_symlink(tmp_path: Path) -> None:
    target = tmp_path / "a.md"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlinks not permitted here")
    assert _source_identity(str(link)) is not None
    assert _source_identity(str(link)) != _source_identity(str(target))


def test_a_path_that_cannot_be_stat_ed_has_no_twin(tmp_path: Path, monkeypatch) -> None:
    present = tmp_path / "a.md"
    present.write_text("x", encoding="utf-8")
    candidate = str(tmp_path / "A.MD")
    assert twin_candidates(str(present), [candidate]) == [candidate]
    real_lstat = os.lstat

    def lstat(path):
        if str(path) == candidate:
            raise FileNotFoundError(path)
        return real_lstat(path)

    monkeypatch.setattr(sqlite_helpers.os, "lstat", lstat)
    assert source_twins(str(present), [candidate]) == []
    # The key's own side: a key whose file is gone has no identity to share.
    gone = str(tmp_path / "gone.md")
    assert twin_candidates(gone, [str(tmp_path / "GONE.md")]) == [str(tmp_path / "GONE.md")]
    assert source_twins(gone, [str(tmp_path / "GONE.md")]) == []


def test_a_zero_inode_is_no_identity(tmp_path: Path, monkeypatch, case_insensitive) -> None:
    target = tmp_path / "memories" / "a.md"
    target.parent.mkdir()
    target.write_text("x", encoding="utf-8")
    real_lstat = os.lstat
    monkeypatch.setattr(
        sqlite_helpers.os,
        "lstat",
        lambda p: SimpleNamespace(st_dev=real_lstat(p).st_dev, st_ino=0),
    )
    assert source_twins(str(target), [_respelled(target)]) == []


# ---------------------------------------------------------------------------
# The rewrite surfaces
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mcp_delete_and_edit_refuse_through_either_spelling(
    bm25_only_components, case_insensitive
) -> None:
    comp, mem_dir = bm25_only_components
    target = mem_dir / "a.md"
    second = _respelled(target)
    await _twinned(comp, target, second)
    before = target.read_bytes()
    ctx = StubCtx(AppContext.from_components(comp))

    for key in (str(target), second):
        chunk_id = _chunk_id(comp, key)
        deleted = await mem_delete(chunk_id=chunk_id, ctx=ctx)
        edited = await mem_edit(chunk_id=chunk_id, new_content="revised", ctx=ctx)
        for result in (deleted, edited):
            assert result.startswith("Error:"), result
            if _moved_on_this_platform(key):
                assert "now resolves to another file" in result
            else:
                assert "under two spellings of its path" in result
                assert str(target) in result and second in result
                assert "Sources tab of the web UI" in result

    assert target.read_bytes() == before
    assert _counts(comp) == {str(target): 2, second: 2}


@pytest.mark.asyncio
async def test_web_delete_and_edit_refuse_without_naming_host_paths(
    bm25_only_components, case_insensitive
) -> None:
    comp, mem_dir = bm25_only_components
    target = mem_dir / "a.md"
    second = _respelled(target)
    await _twinned(comp, target, second)
    before = target.read_bytes()
    chunk_id = _chunk_id(comp, str(target))

    async with _web_client(comp) as client:
        edited = await client.patch(f"/api/chunks/{chunk_id}", json={"new_content": "revised"})
        deleted = await client.delete(f"/api/chunks/{chunk_id}")

    for resp in (edited, deleted):
        assert resp.status_code == 409, resp.text
        message = resp.json()["detail"]["message"]
        assert "a.md is indexed under two spellings of its path" in message
        assert "Sources tab" in message
        assert str(mem_dir) not in resp.text and str(mem_dir.parent) not in resp.text
    assert target.read_bytes() == before
    assert _counts(comp) == {str(target): 2, second: 2}


@pytest.mark.asyncio
async def test_the_sources_tab_removes_one_spelling_and_then_a_delete_works(
    bm25_only_components, case_insensitive
) -> None:
    comp, mem_dir = bm25_only_components
    target = mem_dir / "a.md"
    second = _respelled(target)
    await _twinned(comp, target, second)
    before = target.read_bytes()

    async with _web_client(comp) as client:
        listed = await client.get("/api/sources", params={"limit": 10000})
        assert listed.status_code == 200, listed.text
        assert second in {s["path"] for s in listed.json()["sources"]}
        removed = await client.delete("/api/sources", params={"path": second})
        assert removed.status_code == 200, removed.text

    assert target.read_bytes() == before
    assert _counts(comp) == {str(target): 2}

    ctx = StubCtx(AppContext.from_components(comp))
    result = await mem_delete(chunk_id=_chunk_id(comp, str(target)), ctx=ctx)
    assert not result.startswith("Error:"), result
    assert "heron rotation plan" not in target.read_text(encoding="utf-8")
    assert "second heron item" in target.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_a_project_local_twin_is_refused_and_told_the_way_out(
    bm25_only_components, tmp_path: Path, monkeypatch, case_insensitive
) -> None:
    """The tab hides a project's local tier, so the text says what to do then too."""
    comp, _ = bm25_only_components
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    local = (root / ".memtomem" / "memories.local").resolve()
    local.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [local]
    target = local / "a.md"
    second = str(local / target.name.upper())
    await _twinned(comp, target, second)
    before = target.read_bytes()
    # A project_local row is visible by id only inside its project.
    monkeypatch.chdir(root)
    ctx = StubCtx(AppContext.from_components(comp))

    result = await mem_delete(chunk_id=_chunk_id(comp, str(target)), ctx=ctx)

    assert "under two spellings of its path" in result
    assert "If the Sources tab of the web UI (mm web) lists either spelling" in result
    assert "if it lists neither, edit the file directly instead" in result
    assert target.read_bytes() == before
    assert _counts(comp) == {str(target): 2, second: 2}


@pytest.mark.asyncio
async def test_a_key_whose_own_rows_just_went_is_still_refused(
    bm25_only_components, case_insensitive
) -> None:
    comp, mem_dir = bm25_only_components
    target = mem_dir / "a.md"
    second = _respelled(target)
    await _twinned(comp, target, second)
    # An index-only delete takes no sidecar lock, so a rewrite that fetched its
    # chunk before can reach the gate with no rows of its own left.
    await comp.storage.delete_by_source(target, as_stored=True)
    assert _counts(comp) == {second: 2}

    detail = await comp.index_engine.rewrite_refusal(str(target), op="delete")

    assert detail is not None and "under two spellings of its path" in detail


@pytest.mark.skipif(sys.platform != "darwin", reason="NFC/NFD keys name one file on macOS only")
@pytest.mark.asyncio
async def test_a_legacy_nfd_key_is_a_twin_and_the_tab_removes_only_it(
    bm25_only_components,
) -> None:
    comp, mem_dir = bm25_only_components
    target = mem_dir / unicodedata.normalize("NFC", "café.md")
    nfd = unicodedata.normalize("NFD", str(target))
    assert nfd != str(target)
    await _twinned(comp, target, nfd)
    before = target.read_bytes()
    ctx = StubCtx(AppContext.from_components(comp))

    result = await mem_delete(chunk_id=_chunk_id(comp, str(target)), ctx=ctx)
    assert "under two spellings of its path" in result

    async with _web_client(comp) as client:
        removed = await client.delete("/api/sources", params={"path": nfd})
    assert removed.status_code == 200, removed.text
    assert _counts(comp) == {str(target): 2}
    assert target.read_bytes() == before


@pytest.mark.asyncio
async def test_the_key_list_is_read_before_the_gate_suspends(
    bm25_only_components, monkeypatch, case_insensitive
) -> None:
    """A twin written in another task's open transaction is not seen (#3408).

    ``rewrite_refusal`` reads on the idle writer, which is valid only until its
    next ``await``. While it awaits ``stored_key_moved``, another task opens a
    transaction and re-keys one row to the second spelling without committing.
    """
    comp, mem_dir = bm25_only_components
    target = mem_dir / "a.md"
    target.write_text(_NOTE, encoding="utf-8")
    stats = await comp.index_engine.index_file(target)
    assert not stats.errors, stats.errors
    second = _respelled(target)
    one_row = _chunk_id(comp, str(target), "second")

    entered = threading.Event()
    release = threading.Event()
    real_moved = sqlite_helpers.stored_key_moved

    def paused_moved(key: str) -> bool:
        entered.set()
        assert release.wait(10)
        return real_moved(key)

    monkeypatch.setattr(sqlite_helpers, "stored_key_moved", paused_moved)
    gate = asyncio.create_task(comp.index_engine.rewrite_refusal(str(target), op="delete"))
    await asyncio.to_thread(entered.wait, 10)

    async with comp.storage.transaction():
        comp.storage._get_db().execute(
            "UPDATE chunks SET source_file=? WHERE id=?", (second, one_row)
        )
        release.set()
        detail = await gate
        assert detail is None, detail

    monkeypatch.setattr(sqlite_helpers, "stored_key_moved", real_moved)
    assert _counts(comp) == {str(target): 1, second: 1}
    detail = await comp.index_engine.rewrite_refusal(str(target), op="delete")
    assert detail is not None and "under two spellings of its path" in detail
