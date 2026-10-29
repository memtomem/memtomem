"""The web per-source reads take a source by its stored key (#2896, #2898).

Stored ``chunks.source_file`` keys are :func:`norm_path` forms when written. If a
stored path has since become a symlink to another source, resolving the request
names that other source, and the route would read its rows under this path.
``GET /api/chunks`` and ``GET /api/sources/chunk-sizes`` take the stored key the
way ``DELETE /api/sources`` does (#2835) and read it as stored.
``GET /api/sources/content`` takes the key the same way and still reads the
file on disk.

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


async def _get(comp, url: str, **params) -> tuple[int, object]:
    app = _web_app(comp)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        resp = await client.get(url, params=params)
    return resp.status_code, resp.json()


_EMPTY_BUCKETS = [
    {"bucket": b, "count": 0}
    for b in ("32-64", "64-128", "128-256", "256-512", "512-1024", "1024+")
]


def _histogram(dist) -> dict[str, int]:
    return {bucket["bucket"]: bucket["count"] for bucket in dist if bucket["count"]}


@pytest.mark.requires_symlinks
async def test_chunk_sizes_of_a_relinked_key_are_its_own(relinked):
    comp, a, b = relinked

    status_a, dist_a = await _get(comp, "/api/sources/chunk-sizes", path=str(a))
    status_b, dist_b = await _get(comp, "/api/sources/chunk-sizes", path=str(b))

    assert status_a == status_b == 200
    # Each chunk is a heading and one short line, well under 32 tokens.
    assert dist_a == [{"bucket": "0-32", "count": 3}] + _EMPTY_BUCKETS
    assert dist_b == [{"bucket": "0-32", "count": 2}] + _EMPTY_BUCKETS


@pytest.mark.requires_symlinks
async def test_chunk_sizes_refuse_an_unindexed_path_and_a_directory_alias(relinked, tmp_path):
    comp, _, b = relinked
    alias = Path(norm_path(tmp_path)) / "plans-alias"
    alias.symlink_to(b.parent, target_is_directory=True)

    for spelling in (Path(norm_path(tmp_path)) / "nope.md", alias / b.name):
        status, body = await _get(comp, "/api/sources/chunk-sizes", path=str(spelling))
        assert (status, body["detail"]) == (403, "Path is not an indexed source file.")


@pytest.mark.requires_symlinks
async def test_chunk_sizes_of_a_held_relinked_key_are_hidden_without_hiding_its_target(
    relinked,
):
    comp, a, b = relinked
    assert await comp.storage.hold_source(a, "scan_missing", as_stored=True)

    status_a, _ = await _get(comp, "/api/sources/chunk-sizes", path=str(a))
    status_b, dist_b = await _get(comp, "/api/sources/chunk-sizes", path=str(b))

    assert status_a == 404
    assert (status_b, sum(_histogram(dist_b).values())) == (200, 2)


@pytest.mark.requires_symlinks
async def test_get_chunk_size_distribution_reads_a_stored_key_as_stored(relinked):
    comp, a, b = relinked

    def total(dist) -> int:
        return sum(bucket["count"] for bucket in dist)

    assert total(await comp.storage.get_chunk_size_distribution(a, as_stored=True)) == 3
    assert total(await comp.storage.get_chunk_size_distribution(a)) == 2  # resolves to b
    assert await comp.storage.hold_source(a, "scan_missing", as_stored=True)
    assert (
        total(
            await comp.storage.get_chunk_size_distribution(a, as_stored=True, exclude_hidden=True)
        )
        == 0
    )
    assert total(await comp.storage.get_chunk_size_distribution(b, exclude_hidden=True)) == 2


@pytest.mark.requires_symlinks
async def test_content_refuses_a_directory_alias_and_serves_the_stored_key(relinked, tmp_path):
    comp, _, b = relinked
    alias = Path(norm_path(tmp_path)) / "plans-alias"
    alias.symlink_to(b.parent, target_is_directory=True)

    status_alias, body_alias = await _get(comp, "/api/sources/content", path=str(alias / b.name))
    status_b, body_b = await _get(comp, "/api/sources/content", path=str(b))

    assert (status_alias, body_alias["detail"]) == (403, "Path is not an indexed source file.")
    assert status_b == 200
    assert (body_b["path"], body_b["content"]) == (str(b), b.read_text(encoding="utf-8"))


@pytest.mark.requires_symlinks
async def test_content_still_refuses_a_relinked_key(relinked):
    comp, a, _ = relinked

    status, body = await _get(comp, "/api/sources/content", path=str(a))

    assert (status, body["detail"]) == (403, "Symlinked files are not served.")
