"""``GET /api/chunks/{id}/frontmatter`` answers from the file, not the content (#3030).

The detail view shows a note's leading YAML frontmatter as metadata instead of
rendering it as Markdown. Indexed content cannot say whether it opens with its
file's frontmatter: a file that opens ``# Topic`` / ``---`` / a line / ``---``
stores the same shape. Every chunk here comes from ``IndexEngine.index_file``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
import uuid

import pytest
from fastapi import HTTPException
from httpx import ASGITransport, AsyncClient

from memtomem.chunking.markdown import leading_frontmatter
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured
from memtomem.web.routes import sources as sources_routes

BASIC = "---\ntags: [alpha]\nowner: zqxvteam\n---\n\n# Cache policy\n\nRedis LRU eviction.\n"

# name -> (file text, leading)
CASES = {
    "basic": (BASIC, True),
    # The shapes a content-only judge cannot tell from frontmatter.
    "topic_prose": ("# Topic\n---\nThis is prose\n---\nBody.\n", False),
    "topic_kv": ("# Topic\n---\nStatus: draft\n---\nBody.\n", False),
    # YAML the chunker reads tags from, which a shape check had rejected.
    "nonascii_key": (
        "---\ntags: [alpha]\nownér: team\n소유자: 팀\n---\n\n# H\n\nBody text here.\n",
        True,
    ),
    "flow_list": ("---\ntags: [\nalpha,\nbeta\n]\n---\n\n# H\n\nBody text here.\n", True),
    "bare_dash": ("---\ntags:\n-\n  alpha\n---\n\n# H\n\nBody text here.\n", True),
    "emoji": ("---\ntags: [alpha]\nname: 😀😀😀😀😀😀\n---\n\n# H\n\nBody text.\n", True),
    # Stored with the link resolved, so the raw file block never prefixes it.
    "wikilink": ('---\ntags: [alpha]\nowner: "[[Team|Alias]]"\n---\n\n# H\n\nBody text.\n', True),
    "eof_lf": ("---\ntags: [alpha]\n---\n", True),
    "eof_nolf": ("---\ntags: [alpha]\n---", True),
    # The producer's regex stops after three hyphens: no line holds the block.
    "close_4dash": ("---\ntags: x\n----\nBody\n---\n", False),
    "close_trail": ("---\ntags: x\n--- x\nBody\n---\n", False),
    "bom": ("﻿---\ntags: [alpha]\n---\n\n# H\n\nBody.\n", False),
}


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
    text, expected = CASES[name]
    _, chunk = await _index(comp, directory, text.encode("utf-8"))
    assert chunk.metadata.start_line == 1

    assert await _leading(comp, chunk.id) is expected


async def test_crlf_file_is_read_as_the_indexer_reads_it(bm25_only_components):
    comp, directory = bm25_only_components
    _, chunk = await _index(comp, directory, BASIC.replace("\n", "\r\n").encode("utf-8"))

    assert await _leading(comp, chunk.id) is True


@pytest.mark.parametrize("name", sorted(n for n, (_, leading) in CASES.items() if leading))
async def test_the_block_is_a_prefix_of_the_stored_content(bm25_only_components, name):
    """What the client's regex finds on content is what the producer found on the file."""
    import re

    comp, directory = bm25_only_components
    text, _ = CASES[name]
    _, chunk = await _index(comp, directory, text.encode("utf-8"))
    span = leading_frontmatter(text)
    assert span is not None
    # The client runs the same regex on the content and keeps a line-bounded span.
    client = re.match(r"^---\n([\s\S]*?)\n---\n?", chunk.content)
    assert client is not None
    assert client.group(0).rstrip("\n") == span.rstrip("\n")


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
