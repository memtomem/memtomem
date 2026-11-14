"""The write guard reads an entry as it is written, title and tags included.

Every appender used to scan the body alone and then write ``format_entry_block``,
which also puts the caller's title into the heading and the tags into a
``> tags:`` line. A credential there passed the guard and landed in the file.
Each appender now scans ``entry_guard_text`` of the same arguments it writes.
"""

from __future__ import annotations

import json

import pytest

from helpers import StubCtx
from memtomem import privacy
from memtomem.server.context import AppContext
from memtomem.tools import memory_writer
from memtomem.tools.memory_writer import entry_guard_text, format_entry_block
from test_excluded_target_writes import _cli, _patch_cli_components, _web_client

SECRET = "sk-" + "a" * 30


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


def hits(text: str) -> int:
    return len(privacy.scan_with_escape_views(text))


# -- entry_guard_text ---------------------------------------------------------


def test_it_carries_the_title_each_tag_and_the_body():
    text = entry_guard_text("body text", "My title", ["alpha", "βeta"])

    assert "## My title" in text
    assert '> tags: ["alpha", "βeta"]' in text
    assert "body text" in text
    assert text.count("alpha") == 2 and text.count("βeta") == 2  # written form and raw


def test_a_title_the_file_does_not_get_is_not_scanned():
    # Content that starts with its own heading keeps it; the title is dropped.
    assert "Ignored" not in format_entry_block("## Own\n\nx", "Ignored")
    assert "Ignored" not in entry_guard_text("## Own\n\nx", "Ignored")


def test_it_is_the_same_on_every_call():
    assert entry_guard_text("x") == entry_guard_text("x")
    assert entry_guard_text("x", None, ["t"]) == entry_guard_text("x", None, ["t"])
    assert entry_guard_text("x", None, None) == entry_guard_text("x", None, [])


def test_a_raw_tag_is_scanned_past_the_escaping_its_json_form_adds():
    header = "-----BEGIN\nRSA\nPRIVATE\nKEY-----"
    tag = json.dumps(json.dumps(header))
    assert hits(tag) == 1
    # The written block alone no longer shows it: one escape level too many.
    assert hits(format_entry_block("note", None, [tag]).replace(tag, "")) == 0

    assert hits(entry_guard_text("note", None, [tag])) >= 1


def test_parts_that_are_apart_in_the_file_do_not_match_together():
    content, tag = "note password", "= hunter2hunter2"
    assert hits(format_entry_block(content, None, [tag])) == 0
    assert hits(content + "\n" + tag) == 1

    assert hits(entry_guard_text(content, None, [tag])) == 0


# -- every appender ---------------------------------------------------------


async def _write(
    surface: str, comp, monkeypatch, *, content: str, title, tags, template=None, force=False
):
    """Drive one appender with a title and tags; return its result."""
    ctx = StubCtx(AppContext.from_components(comp))
    if surface == "mcp_add":
        from memtomem.server.tools import memory_crud

        return await memory_crud.mem_add(
            content, title=title, tags=tags, template=template, force_unsafe=force, ctx=ctx
        )
    if surface == "mcp_batch_add":
        from memtomem.server.tools import memory_crud

        return await memory_crud.mem_batch_add(
            [{"content": content, "key": title, "tags": tags}], force_unsafe=force, ctx=ctx
        )
    if surface == "cli_add":
        from memtomem.cli.memory import _add

        _patch_cli_components(monkeypatch, comp)
        return await _cli(_add(content, title, tags or [], None, force, yes=True))
    if surface == "web_add":
        async with _web_client(comp) as client:
            return await client.post(
                "/api/add",
                json={"content": content, "title": title, "tags": tags, "force_unsafe": force},
            )
    if surface == "scratch_promote":
        await comp.storage.scratch_set("k", content)
        async with _web_client(comp) as client:
            return await client.post(
                "/api/scratch/k/promote",
                json={"title": title, "tags": tags, "force_unsafe": force},
            )
    if surface == "langgraph_add":
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        store._components = comp
        return await store.add(content, title=title, tags=tags, force_unsafe=force)
    raise AssertionError(surface)


def _refused(surface: str, result) -> bool:
    if surface in ("mcp_add", "mcp_batch_add"):
        return result.startswith("Error") and "privacy pattern" in result
    if surface == "cli_add":
        return result is not None and "privacy pattern" in result
    if surface in ("web_add", "scratch_promote"):
        return result.status_code == 403 and "redaction_blocked" in result.text
    if surface == "langgraph_add":
        return isinstance(result, dict) and result.get("error") == "redaction_blocked"
    raise AssertionError(surface)


SURFACES = ("mcp_add", "mcp_batch_add", "cli_add", "web_add", "scratch_promote", "langgraph_add")


def _files_with(mem_dir, needle: str) -> list:
    return [p for p in mem_dir.rglob("*.md") if needle in p.read_text(encoding="utf-8")]


@pytest.mark.parametrize("where", ["title", "tag"])
@pytest.mark.parametrize("surface", SURFACES)
async def test_a_credential_in_the_title_or_a_tag_is_refused(
    bm25_only_components, monkeypatch, surface, where
):
    comp, mem_dir = bm25_only_components
    title, tags = ("Token " + SECRET, ["plain"]) if where == "title" else ("Plain", [SECRET])

    result = await _write(surface, comp, monkeypatch, content="plain body", title=title, tags=tags)

    assert _refused(surface, result), result
    assert _files_with(mem_dir, SECRET) == []


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_clean_title_and_tags_still_write(bm25_only_components, monkeypatch, surface):
    comp, mem_dir = bm25_only_components

    result = await _write(
        surface, comp, monkeypatch, content="plain body", title="Plain title", tags=["alpha"]
    )

    assert not _refused(surface, result), result
    assert _files_with(mem_dir, "plain body")


@pytest.mark.parametrize("where", ["title", "tag"])
@pytest.mark.parametrize("surface", SURFACES)
async def test_force_unsafe_lets_a_flagged_title_or_tag_through_as_a_bypass(
    bm25_only_components, monkeypatch, surface, where
):
    """The same valve as for a flagged body: written, and recorded as a bypass."""
    comp, mem_dir = bm25_only_components
    title, tags = ("Token " + SECRET, ["plain"]) if where == "title" else ("Plain", [SECRET])

    result = await _write(
        surface, comp, monkeypatch, content="plain body", title=title, tags=tags, force=True
    )

    assert not _refused(surface, result), result
    assert _files_with(mem_dir, SECRET)
    assert privacy.snapshot()["outcomes"]["bypassed"] >= 1


async def test_a_template_puts_the_title_where_the_guard_now_looks(
    bm25_only_components, monkeypatch
):
    comp, mem_dir = bm25_only_components

    result = await _write(
        "mcp_add",
        comp,
        monkeypatch,
        content="## ordinary heading",
        title=SECRET,
        tags=None,
        template="meeting",
    )

    assert _refused("mcp_add", result), result
    assert _files_with(mem_dir, SECRET) == []


async def test_mm_agent_share_scans_the_heading_path_it_copies(bm25_only_components, monkeypatch):
    from memtomem.cli.agent_cmd import _run_share
    from memtomem.models import Chunk, ChunkMetadata

    comp, mem_dir = bm25_only_components
    chunk = Chunk(
        "plain body",
        ChunkMetadata(source_file=mem_dir / "src.md", heading_hierarchy=("Token " + SECRET,)),
        embedding=[0.0] * comp.config.embedding.dimension,
    )
    await comp.storage.upsert_chunks([chunk])
    _patch_cli_components(monkeypatch, comp)

    result = await _cli(_run_share(str(chunk.id), "shared", False))

    assert result is not None and "privacy pattern" in result, result
    assert _files_with(mem_dir, SECRET) == []


# -- parity: what is scanned is what is written ------------------------------


@pytest.fixture
def rendered(monkeypatch):
    """Every block rendered, split into the guard's (fixed time) and the writer's."""
    calls: dict[str, list] = {"scanned": [], "written": []}
    original = memory_writer._render_entry_block

    def record(content, title, tags, *, created, untitled_heading):
        kind = "scanned" if created.startswith("1970-") else "written"
        calls[kind].append((content, title, tuple(tags or ())))
        return original(content, title, tags, created=created, untitled_heading=untitled_heading)

    monkeypatch.setattr(memory_writer, "_render_entry_block", record)

    calls["guarded"] = []
    real_guard = privacy.enforce_write_guard

    def guard(text, *args, **kwargs):
        calls["guarded"].append(text)
        return real_guard(text, *args, **kwargs)

    monkeypatch.setattr(privacy, "enforce_write_guard", guard)
    return calls


def _scanned_as_written(rendered) -> None:
    """The renders agree, and the guard read exactly the text for what was written."""
    assert rendered["written"], "nothing was written"
    assert rendered["scanned"] == rendered["written"]
    for content, title, tags in rendered["written"]:
        assert entry_guard_text(content, title, list(tags) or None) in rendered["guarded"]


@pytest.mark.parametrize("surface", SURFACES)
async def test_each_appender_scans_exactly_what_it_writes(
    bm25_only_components, monkeypatch, rendered, surface
):
    comp, _ = bm25_only_components

    result = await _write(
        surface, comp, monkeypatch, content="plain body", title="Plain title", tags=["a", "b"]
    )

    assert not _refused(surface, result), result
    _scanned_as_written(rendered)


async def test_mcp_add_with_a_template_scans_what_it_writes(
    bm25_only_components, monkeypatch, rendered
):
    comp, _ = bm25_only_components

    await _write(
        "mcp_add",
        comp,
        monkeypatch,
        content="notes",
        title="Weekly",
        tags=["t"],
        template="meeting",
    )

    _scanned_as_written(rendered)


async def test_shell_add_scans_what_it_writes(bm25_only_components, rendered, capsys):
    from memtomem.cli.shell import _cmd_add

    comp, _ = bm25_only_components
    await _cmd_add(comp, ["plain", "body"])

    _scanned_as_written(rendered)


async def test_mm_agent_share_scans_what_it_writes(bm25_only_components, monkeypatch, rendered):
    from memtomem.cli.agent_cmd import _run_share
    from memtomem.models import Chunk, ChunkMetadata

    comp, mem_dir = bm25_only_components
    chunk = Chunk(
        "plain body",
        ChunkMetadata(
            source_file=mem_dir / "src.md", heading_hierarchy=("Plan", "Step"), tags=("x",)
        ),
        embedding=[0.0] * comp.config.embedding.dimension,
    )
    await comp.storage.upsert_chunks([chunk])
    _patch_cli_components(monkeypatch, comp)

    assert await _cli(_run_share(str(chunk.id), "shared", False)) is None

    _scanned_as_written(rendered)


async def test_mm_review_approve_scans_what_it_writes(bm25_only_components, monkeypatch, rendered):
    from memtomem.cli.review_cmd import _decide
    from memtomem.formation import propose_memory_candidate

    comp, _ = bm25_only_components
    _patch_cli_components(monkeypatch, comp)
    candidate, _ = await propose_memory_candidate(
        comp.storage,
        "Decision: a durable fact",
        source="test",
        source_ref="ref-1",
        idempotency_key="proposal-1",
    )

    await _decide(candidate["id"], "approved", "alice", "")

    _scanned_as_written(rendered)
