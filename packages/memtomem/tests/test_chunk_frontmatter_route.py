"""``GET /api/chunks/{id}/frontmatter`` answers from the file, not the content (#3030).

The detail view shows a note's leading YAML frontmatter as metadata instead of
rendering it as Markdown. Indexed content cannot say whether it opens with its
file's frontmatter: a file that opens ``# Topic`` / ``---`` / a line / ``---``
stores the same shape. Every chunk here comes from ``IndexEngine.index_file``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from memtomem.chunking.markdown import leading_frontmatter
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured
from memtomem.web.routes import sources as sources_routes

BASIC = "---\ntags: [alpha]\nowner: zqxvteam\n---\n\n# Cache policy\n\nRedis LRU eviction.\n"

# Shared with ``tests-js/detail-frontmatter-render.test.mjs``, which runs the
# client's ``_leadingFrontmatter`` over the same stored contents. Each entry
# holds a file, what the indexer stores as its first chunk, the route's answer
# and the block. The first test below re-indexes every file, so an entry that
# drifts from the indexer fails here rather than passing in JS.
FIXTURE = json.loads(
    (Path(__file__).parents[1] / "tests-js/fixtures/frontmatter-indexed.json").read_text(
        encoding="utf-8"
    )
)
CASES = {entry["name"]: entry for entry in FIXTURE}


async def _index(comp, directory, data: bytes, name="note.md"):
    source = directory / name
    source.write_bytes(data)
    stats = await comp.index_engine.index_file(source, ingress_guarded=True)
    assert not stats.errors
    chunks = await comp.storage.list_chunks_by_source(source)
    assert chunks
    return source, min(chunks, key=lambda c: c.metadata.start_line)


def _web_app(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    return app


async def _get(comp, chunk_id):
    async with AsyncClient(transport=ASGITransport(app=_web_app(comp)), base_url="http://t") as c:
        return await c.get(f"/api/chunks/{chunk_id}/frontmatter")


async def _leading(comp, chunk_id):
    resp = await _get(comp, chunk_id)
    assert resp.status_code == 200, resp.text
    return resp.json()["leading"]


@pytest.mark.parametrize("name", sorted(CASES))
async def test_answer_matches_the_file(bm25_only_components, name):
    comp, directory = bm25_only_components
    entry = CASES[name]
    _, chunk = await _index(comp, directory, entry["file"].encode("utf-8"))
    assert chunk.metadata.start_line == 1
    # The shared fixture is what the indexer stores today.
    assert chunk.content == entry["content"]

    assert await _leading(comp, chunk.id) is entry["leading"]
    if entry["leading"]:
        assert leading_frontmatter(entry["file"]).rstrip("\n") == entry["block"]


async def test_crlf_file_is_read_as_the_indexer_reads_it(bm25_only_components):
    comp, directory = bm25_only_components
    _, chunk = await _index(comp, directory, BASIC.replace("\n", "\r\n").encode("utf-8"))

    assert await _leading(comp, chunk.id) is True


async def test_a_later_file_edit_is_answered_from_the_file(bm25_only_components):
    comp, directory = bm25_only_components
    source, chunk = await _index(comp, directory, BASIC.encode("utf-8"))
    source.write_text(BASIC.replace("owner: zqxvteam", "owner: other"), encoding="utf-8")

    assert await _leading(comp, chunk.id) is False


@pytest.mark.parametrize("change", ["later_line", "not_markdown", "redacted", "no_dashes"])
async def test_ineligible_chunks_never_reach_the_file(bm25_only_components, monkeypatch, change):
    comp, directory = bm25_only_components
    _, chunk = await _index(comp, directory, BASIC.encode("utf-8"))

    async def _must_not_run(*_a, **_k):
        raise AssertionError("the file was consulted")

    monkeypatch.setattr(sources_routes, "resolve_readable_source", _must_not_run)
    meta = chunk.metadata
    if change == "later_line":
        # Overlap can prepend the whole frontmatter to a chunk that starts later.
        chunk.metadata = dataclasses.replace(meta, start_line=5, end_line=8)
    elif change == "not_markdown":
        chunk.metadata = dataclasses.replace(meta, source_file=meta.source_file.with_suffix(".txt"))
    elif change == "redacted":
        chunk.metadata = dataclasses.replace(meta, redaction_count=1)
    else:
        chunk.content = "Redis LRU eviction."
    await comp.storage.upsert_chunks([chunk])

    assert await _leading(comp, chunk.id) is False


async def test_a_read_only_source_is_answered(bm25_only_components):
    comp, directory = bm25_only_components
    _, chunk = await _index(comp, directory, BASIC.encode("utf-8"))
    chunk.metadata = dataclasses.replace(chunk.metadata, source_read_only=True)
    await comp.storage.upsert_chunks([chunk])

    assert await _leading(comp, chunk.id) is True


@pytest.mark.parametrize("status", [403, 404, 409, 413])
async def test_every_gate_refusal_is_the_same_false(bm25_only_components, monkeypatch, status):
    """The raw-text gates decide, on the file the read would reach; a refusal says nothing."""
    comp, directory = bm25_only_components
    _, chunk = await _index(comp, directory, BASIC.encode("utf-8"))
    seen = []

    async def _refuse(_storage, path):
        seen.append(path)
        raise HTTPException(status_code=status, detail="refused")

    monkeypatch.setattr(sources_routes, "resolve_readable_source", _refuse)

    resp = await _get(comp, chunk.id)

    assert resp.status_code == 200
    assert resp.json() == {"leading": False}
    assert seen == [str(chunk.metadata.source_file)]


async def test_a_symlinked_or_deleted_source_is_false(bm25_only_components, tmp_path):
    comp, directory = bm25_only_components
    source, chunk = await _index(comp, directory, BASIC.encode("utf-8"))
    elsewhere = tmp_path / "elsewhere.md"
    elsewhere.write_text(BASIC, encoding="utf-8")
    source.unlink()
    try:
        source.symlink_to(elsewhere)
    except OSError:
        pytest.skip("symlinks unavailable")
    assert await _leading(comp, chunk.id) is False

    source.unlink()
    assert await _leading(comp, chunk.id) is False


async def test_a_resolver_filesystem_error_is_the_same_false(bm25_only_components, monkeypatch):
    """A source removed between the resolver's ``exists()`` and ``stat()`` raises ``OSError``."""
    comp, directory = bm25_only_components
    _, chunk = await _index(comp, directory, BASIC.encode("utf-8"))

    async def _vanished(_storage, path):
        raise FileNotFoundError(path)

    monkeypatch.setattr(sources_routes, "resolve_readable_source", _vanished)

    resp = await _get(comp, chunk.id)

    assert resp.status_code == 200
    assert resp.json() == {"leading": False}


@pytest.mark.parametrize("kind", ["scan_missing", "pending"])
async def test_a_held_or_pending_source_is_false(bm25_only_components, kind):
    comp, directory = bm25_only_components
    source, chunk = await _index(comp, directory, BASIC.encode("utf-8"))
    if kind == "pending":
        assert comp.storage.queue_source_check_sync(source)
    else:
        assert await comp.storage.hold_source(source, kind)

    assert await _leading(comp, chunk.id) is False


async def test_a_redaction_held_source_is_the_screened_404(bm25_only_components):
    comp, directory = bm25_only_components
    source, chunk = await _index(comp, directory, BASIC.encode("utf-8"))
    assert await comp.storage.hold_source(source, REDACTION_BLOCKED_HOLD)

    resp = await _get(comp, chunk.id)

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Chunk not found"


async def test_a_parent_retargeted_into_a_held_source_is_false(bm25_only_components):
    """Visibility is judged on the file the read reaches, not on the stored key.

    ``visible/note.md`` is indexed; ``held/note.md`` has the same text and is
    held. Once ``visible`` points at ``held``, answering ``true`` would say what
    the held file begins with.
    """
    comp, directory = bm25_only_components
    visible, held = directory / "visible", directory / "held"
    visible.mkdir()
    held.mkdir()
    _, chunk = await _index(comp, visible, BASIC.encode("utf-8"))
    held_source, _ = await _index(comp, held, BASIC.encode("utf-8"))
    visible.rename(directory / "visible-old")
    try:
        visible.symlink_to(held, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    # Control: through the retargeted parent, an unheld target is answered.
    assert await _leading(comp, chunk.id) is True

    assert await comp.storage.hold_source(held_source, "scan_missing")

    assert await _leading(comp, chunk.id) is False


async def test_an_unknown_id_is_the_screened_404(bm25_only_components):
    comp, _ = bm25_only_components

    resp = await _get(comp, uuid.uuid4())

    assert resp.status_code == 404
    assert resp.json()["detail"] == "Chunk not found"


async def test_a_slow_read_does_not_hold_other_requests(bm25_only_components, monkeypatch):
    comp, directory = bm25_only_components
    source, chunk = await _index(comp, directory, BASIC.encode("utf-8"))
    real_read_text = type(source).read_text
    finished = {}

    def _slow(self, *args, **kwargs):
        if self == source:
            time.sleep(0.5)
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(type(source), "read_text", _slow)
    app = _web_app(comp)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:

        async def _timed(name, url):
            resp = await c.get(url)
            finished[name] = time.monotonic()
            return resp

        fm, other = await asyncio.gather(
            _timed("fm", f"/api/chunks/{chunk.id}/frontmatter"),
            _timed("other", f"/api/chunks/{chunk.id}"),
        )

    assert fm.json() == {"leading": True}
    assert other.status_code == 200
    assert finished["other"] < finished["fm"]
