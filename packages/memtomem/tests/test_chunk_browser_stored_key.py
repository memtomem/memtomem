"""The web chunk browser reads a source by its stored key (#2896).

Stored ``chunks.source_file`` keys are :func:`norm_path` forms when written. If a
stored path has since become a symlink to another source, resolving the request
names that other source, and the browser would list its rows under this path.
``GET /api/chunks`` takes the stored key the way ``DELETE /api/sources`` does
(#2835) and reads and counts it as stored.

``a`` and ``b`` hold different chunk counts, so a count that resolves the key
reads the wrong total. These run against a real ``SqliteBackend``.
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


def _web_app(comp):
    """The web app wired to the real ``comp`` stack, without a lifespan."""
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    app.state.config_signature = hot_reload.current_signature()
    app.state.last_reload_error = None
    return app


def _body(word: str, sections: int) -> str:
    return "".join(f"## Part {i}\n\n{word} part {i}\n\n" for i in range(sections))


@pytest.fixture
async def relinked(bm25_only_components, tmp_path, tmp_path_factory, monkeypatch):
    """``(comp, a, b)``: ``work/a.md`` (3 chunks) relinked to ``plans/b.md`` (2 chunks)."""
    comp, _ = bm25_only_components
    isolate_config_paths(monkeypatch, tmp_path_factory.mktemp("config-home"))
    base = Path(norm_path(tmp_path))
    work, plans = base / "work", base / "plans"
    comp.config.indexing.memory_dirs = [work, plans]
    a, b = work / "a.md", plans / "b.md"
    for path, word, sections in ((a, "alphaword", 3), (b, "betaword", 2)):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(_body(word, sections), encoding="utf-8")
        stats = await comp.index_engine.index_file(path)
        assert stats.indexed_chunks == sections, stats
    a.unlink()
    a.symlink_to(b)
    return comp, a, b


async def _browse(comp, source: Path | str, **params) -> tuple[int, dict]:
    app = _web_app(comp)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get("/api/chunks", params={"source": str(source), **params})
    return resp.status_code, resp.json()


def _listed(body: dict) -> list[tuple[str, str]]:
    return [(c["source_file"], c["content"].split()[0]) for c in body["chunks"]]


@pytest.mark.requires_symlinks
async def test_a_relinked_key_lists_and_counts_its_own_chunks(relinked):
    comp, a, b = relinked

    status_a, body_a = await _browse(comp, a)
    status_b, body_b = await _browse(comp, b)

    assert status_a == status_b == 200
    assert (body_a["total"], _listed(body_a)) == (3, [(str(a), "alphaword")] * 3)
    assert (body_b["total"], _listed(body_b)) == (2, [(str(b), "betaword")] * 2)


@pytest.mark.requires_symlinks
async def test_the_total_is_the_key_own_count_when_the_page_is_smaller(relinked):
    comp, a, _ = relinked

    status, body = await _browse(comp, a, limit=1)

    assert status == 200
    assert (body["total"], _listed(body)) == (3, [(str(a), "alphaword")])


@pytest.mark.requires_symlinks
async def test_a_held_relinked_key_is_hidden_without_hiding_its_target(relinked):
    comp, a, b = relinked
    assert await comp.storage.hold_source(a, "scan_missing", as_stored=True)

    status_a, _ = await _browse(comp, a)
    status_b, body_b = await _browse(comp, b)

    assert status_a == 404
    assert (status_b, body_b["total"]) == (200, 2)


@pytest.mark.requires_symlinks
async def test_a_directory_alias_spelling_is_not_an_indexed_source(relinked, tmp_path):
    """Declared narrowing, as for the source delete (#2835): the request is
    matched as a stored spelling, not resolved, so a path through a symlinked
    directory names no indexed source. The Web UI sends stored paths."""
    comp, _, b = relinked
    alias = Path(norm_path(tmp_path)) / "plans-alias"
    alias.symlink_to(b.parent, target_is_directory=True)

    status, body = await _browse(comp, alias / b.name)

    assert (status, body["detail"]) == (403, "Path is not an indexed source file.")


@pytest.mark.requires_symlinks
async def test_count_chunks_by_source_counts_a_stored_key_as_stored(relinked):
    comp, a, b = relinked

    assert await comp.storage.count_chunks_by_source(a, as_stored=True) == 3
    assert await comp.storage.count_chunks_by_source(a) == 2  # resolves to b
    assert await comp.storage.count_chunks_by_source(b, as_stored=True) == 2
