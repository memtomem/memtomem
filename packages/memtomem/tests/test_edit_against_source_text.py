"""Chunk edits are written against the span's source text, not its indexed projection.

Indexed content is not always the text of its span: wikilinks are resolved,
merged sections get heading labels rebuilt from the hierarchy, and a
frontmatter preamble is merged into the next section without that section's
heading line. An edit of such content, written over the span, removed what the
content did not show (#3071). Every chunk here comes from the real chunker and
short-chunk merge through ``IndexEngine.index_file``, and is edited through both
the Web route and MCP ``mem_edit``.
"""

from __future__ import annotations

import re
from unittest.mock import AsyncMock, Mock

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.tools import memory_mutation
from memtomem.tools.memory_writer import format_entry_block
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured

SURFACES = ("web", "mcp")

FRONTMATTER = "---\ntags: [alpha]\nowner: x\n---\n\n# Cache policy\n\nRedis LRU eviction.\n"
# A heading-only section directly followed by another heading: the merged
# content drops ``## Reviewed`` and repeats ``## Fixes`` before each child.
MERGED = (
    "# Log\n\nintro text\n\n## Reviewed: 2026-02-06\n## Fixes\n\n"
    "### First\n\nfirst fix\n\n### Second\n\nsecond fix\n"
)
WIKILINK = "# Notes\n\nSee [[cache-policy]] and [[ops|the ops page]].\n"
# Merged content opens with ``## A``: it reproduces its span only through the
# ``## `` full-replacement rule, which a body-only edit would then leave.
OVERRIDE_ONLY = "# Parent\n## A\n\nalpha\n\n## B\n\nbeta\n"


async def _index(comp, directory, text, name="note.md"):
    source = directory / name
    source.write_text(text, encoding="utf-8")
    stats = await comp.index_engine.index_file(source, ingress_guarded=True)
    assert not stats.errors
    chunks = await comp.storage.list_chunks_by_source(source)
    assert chunks
    return source, sorted(chunks, key=lambda c: c.metadata.start_line)


def _web_app(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.dependency_overrides[require_configured] = lambda: None
    return app


async def _get_source(comp, chunk_id):
    async with AsyncClient(transport=ASGITransport(app=_web_app(comp)), base_url="http://t") as c:
        return await c.get(f"/api/chunks/{chunk_id}/source")


class Outcome:
    """One edit attempt's result, read the same way for both surfaces."""

    def __init__(self, surface, raw):
        self.raw = raw
        self.text = self.base_hash = None
        if surface == "mcp":
            self.ok = raw.startswith("Memory updated")
            self.mismatch = raw.startswith("Error: edit_base_mismatch")
            if self.mismatch:
                self.base_hash = re.search(r"base_hash=([0-9a-f]{64})", raw).group(1)
                self.text = raw.split("\n--- source text ---\n", 1)[1]
        else:
            self.ok = raw.status_code == 200
            detail = raw.json().get("detail") if raw.status_code == 409 else None
            self.mismatch = isinstance(detail, dict) and detail.get("detail") == (
                "edit_base_mismatch"
            )
            if self.mismatch:
                self.text, self.base_hash = detail["text"], detail["base_hash"]


async def _edit(comp, surface, chunk_id, new_content, base_hash=None):
    if surface == "mcp":
        ctx = StubCtx(AppContext.from_components(comp))
        kwargs = {"base_hash": base_hash} if base_hash is not None else {}
        raw = await memory_crud.mem_edit(str(chunk_id), new_content, ctx=ctx, **kwargs)
    else:
        body = {"new_content": new_content}
        if base_hash is not None:
            body["base_hash"] = base_hash
        app = _web_app(comp)
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            raw = await c.patch(f"/api/chunks/{chunk_id}", json=body)
    return Outcome(surface, raw)


async def _source_base(comp, surface, chunk):
    """The source text and hash a client edits, obtained the way it would be."""
    if surface == "web":
        resp = await _get_source(comp, chunk.id)
        assert resp.status_code == 200, resp.text
        return resp.json()["text"], resp.json()["base_hash"]
    # An agent learns them from the refusal of an edit of the indexed content.
    refused = await _edit(comp, surface, chunk.id, chunk.content)
    assert refused.mismatch, refused.raw
    return refused.text, refused.base_hash


@pytest.fixture
def spies(bm25_only_components, monkeypatch):
    """Witnesses that a refusal wrote, restored and re-indexed nothing."""
    comp, _ = bm25_only_components
    index = AsyncMock(wraps=comp.index_engine.index_file)
    monkeypatch.setattr(comp.index_engine, "index_file", index)
    restores = []
    for module in (memory_crud, memory_mutation):
        restore = Mock(wraps=module.restore_pre_image_quietly)
        monkeypatch.setattr(module, "restore_pre_image_quietly", restore)
        restores.append(restore)
    return index, restores


def _assert_refused_untouched(outcome, source, before, spies):
    assert outcome.mismatch, outcome.raw
    assert source.read_bytes() == before
    index, restores = spies
    index.assert_not_awaited()
    for restore in restores:
        restore.assert_not_called()


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "new_content",
    [
        pytest.param(None, id="indexed-content-with-a-word-changed"),
        pytest.param("Redis LRU eviction (body only).", id="body-only"),
    ],
)
async def test_frontmatter_chunk_edit_of_indexed_content_is_refused(
    bm25_only_components, spies, surface, new_content
):
    comp, directory = bm25_only_components
    source, (chunk,) = await _index(comp, directory, FRONTMATTER)
    # The merged preamble holds the frontmatter but not ``# Cache policy``.
    assert "---" in chunk.content and "# Cache policy" not in chunk.content
    spies[0].reset_mock()
    before = source.read_bytes()
    if new_content is None:
        new_content = chunk.content.replace("LRU", "LFU")

    outcome = await _edit(comp, surface, chunk.id, new_content)

    _assert_refused_untouched(outcome, source, before, spies)
    assert outcome.text == FRONTMATTER.rstrip("\n")


@pytest.mark.parametrize("surface", SURFACES)
async def test_frontmatter_chunk_source_edit_keeps_frontmatter_and_heading(
    bm25_only_components, surface
):
    comp, directory = bm25_only_components
    source, (chunk,) = await _index(comp, directory, FRONTMATTER)
    text, base_hash = await _source_base(comp, surface, chunk)

    outcome = await _edit(comp, surface, chunk.id, text.replace("LRU", "LFU"), base_hash)

    assert outcome.ok, outcome.raw
    assert source.read_text(encoding="utf-8") == FRONTMATTER.replace("LRU", "LFU")


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "text", [MERGED, WIKILINK, OVERRIDE_ONLY], ids=["merged", "wikilink", "override-only"]
)
async def test_projection_edits_are_refused_and_source_saves_round_trip(
    bm25_only_components, spies, surface, text
):
    comp, directory = bm25_only_components
    source, chunks = await _index(comp, directory, text)
    chunk = chunks[0]
    if text is MERGED:
        assert "## Reviewed" not in chunk.content
        assert chunk.content.count("## Fixes") == 2
    spies[0].reset_mock()
    before = source.read_bytes()

    # An unchanged save of the indexed content would already rewrite the span.
    refused = await _edit(comp, surface, chunk.id, chunk.content)
    _assert_refused_untouched(refused, source, before, spies)

    # The source text saved back unchanged leaves the file as it was.
    source_text, base_hash = await _source_base(comp, surface, chunk)
    saved = await _edit(comp, surface, chunk.id, source_text, base_hash)
    assert saved.ok, saved.raw
    assert source.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("surface", SURFACES)
async def test_override_only_base_refuses_the_edits_that_would_switch_rules(
    bm25_only_components, spies, surface
):
    comp, directory = bm25_only_components
    source, chunks = await _index(comp, directory, OVERRIDE_ONLY)
    chunk = chunks[0]
    assert chunk.content.startswith("## A")
    spies[0].reset_mock()
    before = source.read_bytes()
    for edited in (f"intro\n\n{chunk.content}", chunk.content.replace("## A", "### A", 1)):
        _assert_refused_untouched(
            await _edit(comp, surface, chunk.id, edited), source, before, spies
        )


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("tags", [None, ["x"]], ids=["untagged", "tagged"])
async def test_mem_add_entries_keep_their_metadata_header(bm25_only_components, surface, tags):
    comp, directory = bm25_only_components
    entry = format_entry_block("Old body.", title="Note", tags=tags).lstrip("\n")
    source, (chunk,) = await _index(comp, directory, entry)
    header = entry.split("Old body.")[0]
    # Untagged: the chunker keeps ``> created:`` in the content.
    assert chunk.content.startswith(">") == (tags is None)

    body_only = await _edit(comp, surface, chunk.id, "New body.")
    assert body_only.ok, body_only.raw
    assert source.read_text(encoding="utf-8") == header + "New body.\n"


@pytest.mark.parametrize("surface", SURFACES)
async def test_untagged_entry_edited_as_shown_keeps_one_created_line(bm25_only_components, surface):
    comp, directory = bm25_only_components
    entry = format_entry_block("Old body.", title="Note").lstrip("\n")
    source, (chunk,) = await _index(comp, directory, entry)

    outcome = await _edit(comp, surface, chunk.id, chunk.content.replace("Old", "New"))

    assert outcome.ok, outcome.raw
    assert source.read_text(encoding="utf-8") == entry.replace("Old", "New")


@pytest.mark.parametrize("surface", SURFACES)
async def test_body_edit_keeps_the_blank_lines_ending_its_span(bm25_only_components, surface):
    comp, directory = bm25_only_components
    alpha, beta = ("alpha words " * 80).strip(), ("beta words " * 80).strip()
    text = f"## A\n\n{alpha}\n\n\n## B\n\n{beta}\n"
    source, chunks = await _index(comp, directory, text)
    assert len(chunks) == 2 and chunks[0].metadata.end_line == 5

    outcome = await _edit(comp, surface, chunks[0].id, "short alpha")

    assert outcome.ok, outcome.raw
    assert source.read_text(encoding="utf-8") == f"## A\n\nshort alpha\n\n\n## B\n\n{beta}\n"


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_base_hash_for_other_text_is_refused(bm25_only_components, spies, surface):
    comp, directory = bm25_only_components
    source, (chunk,) = await _index(comp, directory, WIKILINK)
    spies[0].reset_mock()
    before = source.read_bytes()

    outcome = await _edit(comp, surface, chunk.id, "anything", base_hash="0" * 64)

    _assert_refused_untouched(outcome, source, before, spies)
    assert outcome.text == WIKILINK.rstrip("\n")


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("bad", ["", "abc", "G" * 64, "0" * 63, "A" * 64])
async def test_a_malformed_base_hash_is_rejected_not_ignored(bm25_only_components, surface, bad):
    comp, directory = bm25_only_components
    source, (chunk,) = await _index(comp, directory, WIKILINK)
    before = source.read_bytes()

    outcome = await _edit(comp, surface, chunk.id, "anything", base_hash=bad)

    assert not outcome.ok and not outcome.mismatch
    if surface == "web":
        assert outcome.raw.status_code == 422
    else:
        assert outcome.raw.startswith("Error: base_hash")
    assert source.read_bytes() == before


async def test_source_route_serves_the_span_text_and_hash(bm25_only_components):
    comp, directory = bm25_only_components
    _, (chunk,) = await _index(comp, directory, WIKILINK)

    resp = await _get_source(comp, chunk.id)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == WIKILINK.rstrip("\n")
    assert (body["start_line"], body["end_line"]) == (1, 3)
    import hashlib

    assert body["base_hash"] == hashlib.sha256(body["text"].encode()).hexdigest()


@pytest.mark.parametrize("refusal", ["masked", "read_only", "stale", "unknown"])
async def test_source_route_refusals_carry_no_source_text(bm25_only_components, refusal):
    import dataclasses
    import uuid

    comp, directory = bm25_only_components
    source, (chunk,) = await _index(comp, directory, WIKILINK)
    chunk_id = chunk.id
    if refusal in ("masked", "read_only"):
        field = {"masked": {"redaction_count": 1}, "read_only": {"source_read_only": True}}
        chunk.metadata = dataclasses.replace(chunk.metadata, **field[refusal])
        await comp.storage.upsert_chunks([chunk])
    elif refusal == "stale":
        source.write_text(WIKILINK.replace("ops page", "ops wiki"), encoding="utf-8")
    else:
        chunk_id = uuid.uuid4()

    resp = await _get_source(comp, chunk_id)

    assert resp.status_code == (404 if refusal == "unknown" else 409), resp.text
    assert "cache-policy" not in resp.text
    assert "base_hash" not in resp.text


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_quoted_body_never_replaces_a_hidden_metadata_header(bm25_only_components, surface):
    comp, directory = bm25_only_components
    entry = format_entry_block("Old body.", title="Note", tags=["x"]).lstrip("\n")
    source, (chunk,) = await _index(comp, directory, entry)
    assert chunk.content == "Old body."  # the tags header is not shown

    outcome = await _edit(comp, surface, chunk.id, "> Quoted replacement.")

    assert outcome.ok, outcome.raw
    assert source.read_text(encoding="utf-8") == entry.replace("Old body.", "> Quoted replacement.")


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_tagged_entry_whose_body_is_a_quote_is_editable(bm25_only_components, surface):
    comp, directory = bm25_only_components
    entry = format_entry_block("> Quoted body.", title="Note", tags=["x"]).lstrip("\n")
    source, (chunk,) = await _index(comp, directory, entry)
    assert chunk.content == "> Quoted body."

    outcome = await _edit(comp, surface, chunk.id, "> Requoted body.")

    assert outcome.ok, outcome.raw
    assert source.read_text(encoding="utf-8") == entry.replace("Quoted", "Requoted")


QUOTED_NOTE = "# Note\n\n> ordinary quote\n"


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize(
    "text, edit",
    [
        pytest.param(None, lambda c: f"Intro\n\n{c}", id="untagged-entry-text-prepended"),
        pytest.param(
            None,
            lambda c: c.replace("> created: ", "> created: 1999-"),
            id="untagged-group-changed",
        ),
        pytest.param(None, lambda c: "> New quoted body.", id="untagged-body-starts-with-quote"),
        pytest.param(QUOTED_NOTE, lambda c: f"Intro\n\n{c}", id="quote-text-prepended"),
        pytest.param(QUOTED_NOTE, lambda c: "> another quote", id="quote-replaced-by-quote"),
    ],
)
async def test_an_edit_ambiguous_about_a_shown_quote_group_is_refused(
    bm25_only_components, spies, surface, text, edit
):
    comp, directory = bm25_only_components
    text = text or format_entry_block("Old body.", title="Note").lstrip("\n")
    source, (chunk,) = await _index(comp, directory, text)
    assert chunk.content.startswith(">")  # the chunker showed the group
    spies[0].reset_mock()
    before = source.read_bytes()

    outcome = await _edit(comp, surface, chunk.id, edit(chunk.content))

    _assert_refused_untouched(outcome, source, before, spies)
