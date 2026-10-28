"""Web memory-dir remove, its dialog count and the Sources tree judge a stored key (#2878).

Stored ``chunks.source_file`` keys are :func:`norm_path` forms when written. If a
stored path has since become a symlink to another source, resolving the key again
names that other source. The status counts, the Sources tree and the remove sweep
must all group a key where it was indexed, and the sweep must delete it as stored,
the rule ``mm gc orphan-sources`` uses since #2835.

These run against a real ``SqliteBackend`` so a delete that resolves the key lands
on the wrong rows, not on a mock.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import isolate_config_paths
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.web import hot_reload
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured

BODY = "## Note\n\nstoredkeyword lives here\n"


def _web_app(comp):
    """The web app wired to the real ``comp`` stack, without a lifespan."""
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    app.state.config_signature = hot_reload.current_signature()
    app.state.last_reload_error = None
    return app


async def _indexed(comp, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BODY, encoding="utf-8")
    stats = await comp.index_engine.index_file(path)
    assert stats.indexed_chunks == 1, stats
    return path


async def _rows(comp, key: Path) -> int:
    return len(await comp.storage.list_chunks_by_source(key, as_stored=True))


@pytest.fixture
async def roots(bm25_only_components, tmp_path, tmp_path_factory, monkeypatch):
    """``(comp, work, plans)`` with both roots configured and one note indexed in each."""
    comp, _ = bm25_only_components
    isolate_config_paths(monkeypatch, tmp_path_factory.mktemp("config-home"))
    base = Path(norm_path(tmp_path))
    work, plans = base / "work", base / "plans"
    work.mkdir()
    plans.mkdir()
    comp.config.indexing.memory_dirs = [work, plans]
    await _indexed(comp, work / "a.md")
    await _indexed(comp, plans / "b.md")
    return comp, work, plans


def _relink(work: Path, plans: Path) -> tuple[Path, Path]:
    """Make the stored ``work/a.md`` a link to the stored ``plans/b.md``."""
    a, b = work / "a.md", plans / "b.md"
    a.unlink()
    a.symlink_to(b)
    return a, b


async def _status(client: AsyncClient) -> dict[str, dict]:
    dirs = (await client.get("/api/memory-dirs/status")).json()["dirs"]
    return {d["path"]: d for d in dirs}


async def _tree_owner(client: AsyncClient, source: Path) -> str | None:
    sources = (await client.get("/api/sources")).json()["sources"]
    (entry,) = [s for s in sources if s["path"] == str(source)]
    return entry["memory_dir"]


async def _remove(client: AsyncClient, root: Path) -> int:
    resp = await client.post(
        "/api/memory-dirs/remove", json={"path": str(root), "delete_chunks": True}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["deleted_chunks"]


@pytest.mark.requires_symlinks
async def test_a_relinked_key_counts_and_lists_under_the_root_it_was_indexed_in(roots):
    comp, work, plans = roots
    a, _ = _relink(work, plans)

    async with AsyncClient(transport=ASGITransport(app=_web_app(comp)), base_url="http://t") as c:
        status = await _status(c)
        owner = await _tree_owner(c, a)

    assert (status[str(work)]["chunk_count"], status[str(work)]["delete_chunk_count"]) == (1, 1)
    assert (status[str(plans)]["chunk_count"], status[str(plans)]["delete_chunk_count"]) == (1, 1)
    assert owner == str(work)


@pytest.mark.requires_symlinks
async def test_removing_the_link_target_root_deletes_only_its_own_rows(roots):
    comp, work, plans = roots
    a, b = _relink(work, plans)

    async with AsyncClient(transport=ASGITransport(app=_web_app(comp)), base_url="http://t") as c:
        shown = (await _status(c))[str(plans)]["delete_chunk_count"]
        deleted = await _remove(c, plans)

    assert deleted == shown == 1
    assert (await _rows(comp, a), await _rows(comp, b)) == (1, 0)


@pytest.mark.requires_symlinks
async def test_removing_the_link_root_deletes_the_link_key_not_its_target(roots):
    comp, work, plans = roots
    a, b = _relink(work, plans)

    async with AsyncClient(transport=ASGITransport(app=_web_app(comp)), base_url="http://t") as c:
        shown = (await _status(c))[str(work)]["delete_chunk_count"]
        deleted = await _remove(c, work)

    assert deleted == shown == 1
    assert (await _rows(comp, a), await _rows(comp, b)) == (0, 1)


@pytest.mark.requires_symlinks
async def test_a_held_relinked_key_is_hidden_and_swept_with_its_own_root(roots):
    comp, work, plans = roots
    a, b = _relink(work, plans)
    assert await comp.storage.hold_source(a, "scan_missing", as_stored=True)

    async with AsyncClient(transport=ASGITransport(app=_web_app(comp)), base_url="http://t") as c:
        status = await _status(c)
        listed = {s["path"] for s in (await c.get("/api/sources")).json()["sources"]}
        shown = status[str(work)]["delete_chunk_count"]
        deleted = await _remove(c, work)

    work_status = status[str(work)]
    assert (work_status["hidden_chunk_count"], work_status["held_chunk_count"]) == (1, 1)
    assert status[str(plans)]["hidden_chunk_count"] == 0
    # Totals minus hidden are what the tree lists for each root.
    assert work_status["source_file_count"] - work_status["hidden_source_file_count"] == 0
    assert listed == {str(b)}
    assert deleted == shown == 1
    assert (await _rows(comp, a), await _rows(comp, b)) == (0, 1)


async def test_ordinary_sources_count_list_and_sweep_by_their_own_root(roots):
    comp, work, plans = roots
    a, b = work / "a.md", plans / "b.md"

    async with AsyncClient(transport=ASGITransport(app=_web_app(comp)), base_url="http://t") as c:
        status = await _status(c)
        owners = (await _tree_owner(c, a), await _tree_owner(c, b))
        deleted = await _remove(c, plans)

    assert [status[str(r)]["delete_chunk_count"] for r in (work, plans)] == [1, 1]
    assert owners == (str(work), str(plans))
    assert deleted == 1
    assert (await _rows(comp, a), await _rows(comp, b)) == (1, 0)
