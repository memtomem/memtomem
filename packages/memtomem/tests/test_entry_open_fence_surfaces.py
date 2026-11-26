"""#3618: every entry writer refuses an entry that leaves a code fence open.

The Markdown chunker reads a fence that is never closed as running to the end
of the file, so every entry appended after one is indexed as code inside it:
no chunk of its own, no heading, no tags. The writers refuse such an entry
before their privacy guard, claim and write, each in its own refusal form (a
Gate B consent line can precede it: it records the consent, not the write).
The checker itself is pinned in ``test_entry_open_fence.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import click
import pytest

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from test_excluded_target_writes import (
    _cli,
    _patch_cli_components,
    _snapshot,
    _web_client,
)

pytestmark = pytest.mark.asyncio

OPEN = "Before the code.\n\n```python\nprint(1)"
CLOSED = "Before the code.\n\n```python\nprint(1)\n```"
EARLIER = "## Earlier\n\nAn entry written before.\n"


def _ledger(comp) -> list[tuple]:
    """Every idempotency row, pending ones included (``idempotency_get`` skips those)."""
    db = comp.storage._get_db()
    return db.execute("SELECT * FROM idempotency_ledger").fetchall()


def _refusal_text() -> str:
    from memtomem.tools.memory_writer import open_fence_refusal

    return open_fence_refusal("```")


def _mm_add(content: str, file: str | None):
    from memtomem.cli.memory import _add

    return _add(
        content,
        None,
        [],
        file,
        False,
        "user",
        False,
        True,
        namespace=None,
        as_json=False,
        allow_namespace_mix=False,
    )


# ── the MCP tools ────────────────────────────────────────────────────────


async def test_mem_add_refuses_an_open_fence_and_writes_nothing(bm25_only_components):
    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    before = _snapshot(mem_dir)

    result = await memory_crud.mem_add(
        content=OPEN, title="First", tags=["first"], file="a.md", idempotency_key="k1", ctx=ctx
    )

    assert _snapshot(mem_dir) == before
    assert result == f"Error: {_refusal_text()}"
    assert _ledger(comp) == []


async def test_mem_add_refuses_before_appending_to_an_existing_file(bm25_only_components):
    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    target = mem_dir / "a.md"
    target.write_text(EARLIER, encoding="utf-8")

    result = await memory_crud.mem_add(content=OPEN, file="a.md", ctx=ctx)

    assert result.startswith("Error: ")
    assert target.read_text(encoding="utf-8") == EARLIER


async def test_mem_add_still_writes_a_closed_fence(bm25_only_components):
    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))

    for title, body in (("First", CLOSED), ("Second", "A plain note."), ("Third", "Another.")):
        result = await memory_crud.mem_add(
            content=body, title=title, tags=[title.lower()], file="a.md", ctx=ctx
        )
        assert not result.startswith("Error"), result

    by_heading = {
        c.metadata.heading_hierarchy[-1]: set(c.metadata.tags)
        for c in await comp.storage.list_chunks_by_source(mem_dir / "a.md")
    }
    assert by_heading == {"## First": {"first"}, "## Second": {"second"}, "## Third": {"third"}}


async def test_mem_add_judges_the_rendered_template(bm25_only_components):
    """The ``procedure`` template puts its JSON ``steps`` into the body as written."""
    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    before = _snapshot(mem_dir)
    content = '{"trigger": "deploy", "steps": "```bash\\nmake deploy"}'

    result = await memory_crud.mem_add(
        content=content, title="Deploy", template="procedure", file="a.md", ctx=ctx
    )

    assert _snapshot(mem_dir) == before
    assert result == f"Error: {_refusal_text()}"


@pytest.mark.parametrize("file", ["notes.py", "notes.md/unused/..", "notes.md\\"])
async def test_mem_add_judges_every_destination_whatever_its_name(bm25_only_components, file):
    """The name a writer is given is not the path it writes: no name is exempt."""
    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    before = _snapshot(mem_dir)

    result = await memory_crud.mem_add(content=OPEN, file=file, ctx=ctx)

    assert _snapshot(mem_dir) == before
    assert result == f"Error: {_refusal_text()}"


async def test_mem_batch_add_rejects_the_whole_batch(bm25_only_components):
    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    before = _snapshot(mem_dir)

    result = await memory_crud.mem_batch_add(
        entries=[
            {"key": "Clean", "value": "safe content"},
            {"key": "Open", "value": OPEN},
            # Skipped as every empty item is: its title would open a fence.
            {"key": "Empty\n```", "value": ""},
            {"key": "Tilde", "value": "~~~~\nstill open"},
        ],
        file="b.md",
        idempotency_key="k1",
        ctx=ctx,
    )

    assert _snapshot(mem_dir) == before
    assert result == (
        "Error: items at indices [1, 3] leave a code fence open, so every entry written "
        "to the file after them would be indexed inside that fence. Whole batch "
        "rejected; close the fences and retry."
    )
    assert _ledger(comp) == []


# ── the other writers ────────────────────────────────────────────────────


async def test_web_add_refuses_with_422(bm25_only_components):
    comp, mem_dir = bm25_only_components
    before = _snapshot(mem_dir)

    async with _web_client(comp) as client:
        resp = await client.post("/api/add", json={"content": OPEN, "file": "a.md"})

    assert _snapshot(mem_dir) == before
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == {
        "detail": "unclosed_code_fence",
        "fence": "```",
        "surface": "web_api_add",
        "message": _refusal_text(),
    }


async def test_web_scratch_promote_refuses_and_keeps_the_entry(bm25_only_components):
    comp, mem_dir = bm25_only_components
    await comp.storage.scratch_set("note", OPEN)
    before = _snapshot(mem_dir)

    async with _web_client(comp) as client:
        resp = await client.post("/api/scratch/note/promote", json={})

    assert _snapshot(mem_dir) == before
    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"] == {
        "detail": "unclosed_code_fence",
        "fence": "```",
        "surface": "web_api_scratch_promote",
        "message": _refusal_text(),
    }
    entry = await comp.storage.scratch_get("note")
    assert entry is not None and not entry["promoted"]


async def test_mm_add_refuses(bm25_only_components, monkeypatch):
    comp, mem_dir = bm25_only_components
    _patch_cli_components(monkeypatch, comp)
    before = _snapshot(mem_dir)

    refusal = await _cli(_mm_add(OPEN, "a.md"))

    assert _snapshot(mem_dir) == before
    assert refusal == _refusal_text()


async def test_shell_add_refuses(bm25_only_components, capsys):
    from memtomem.cli.shell import _cmd_add

    comp, mem_dir = bm25_only_components
    before = _snapshot(mem_dir)
    capsys.readouterr()

    await _cmd_add(comp, ["```python", "print(1)"])

    assert _snapshot(mem_dir) == before
    assert click.unstyle(capsys.readouterr().out) == f"{_refusal_text()}\n"


async def _open_fence_chunk(comp, mem_dir):
    source = mem_dir / "src.md"
    source.write_text(f"## Earlier\n\n{OPEN}\n", encoding="utf-8")
    assert (await comp.index_engine.index_file(source, ingress_guarded=True)).indexed_chunks
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    return chunk


async def test_cli_agent_share_refuses_a_chunk_that_leaves_a_fence_open(
    bm25_only_components, monkeypatch
):
    """A share copy has a file of its own, but a later ``mem_add(file=...)`` can
    still append to it."""
    from memtomem.cli.agent_cmd import _run_share

    comp, mem_dir = bm25_only_components
    chunk = await _open_fence_chunk(comp, mem_dir)
    _patch_cli_components(monkeypatch, comp)
    before = _snapshot(mem_dir)

    refusal = await _cli(_run_share(str(chunk.id), "shared", False))

    assert _snapshot(mem_dir) == before
    assert refusal == _refusal_text()


async def test_mcp_agent_share_refuses_and_releases_its_key(bm25_only_components):
    from memtomem.server.tools.multi_agent import mem_agent_share

    comp, mem_dir = bm25_only_components
    chunk = await _open_fence_chunk(comp, mem_dir)
    ctx = StubCtx(AppContext.from_components(comp))
    before = _snapshot(mem_dir)

    first = await mem_agent_share(chunk_id=str(chunk.id), idempotency_key="k1", ctx=ctx)
    retry = await mem_agent_share(chunk_id=str(chunk.id), idempotency_key="k1", ctx=ctx)

    assert _snapshot(mem_dir) == before
    assert first == retry == f"Error: {_refusal_text()}"
    assert _ledger(comp) == []


async def test_langgraph_add_refuses(bm25_only_components):
    from memtomem.integrations.langgraph import MemtomemStore

    comp, mem_dir = bm25_only_components
    store = MemtomemStore()
    store._components = comp
    before = _snapshot(mem_dir)

    result = await store.add(OPEN)

    assert _snapshot(mem_dir) == before
    assert result == {
        "error": "unclosed_code_fence",
        "fence": "```",
        "surface": "langgraph_add",
        "message": _refusal_text(),
    }


async def _candidate(comp, content: str, idem: str):
    from memtomem.formation import propose_memory_candidate

    candidate, _created = await propose_memory_candidate(
        comp.storage, content, source="test", source_ref=idem, idempotency_key=idem
    )
    return candidate


async def test_review_approve_refuses_a_daily_entry_before_claiming(
    bm25_only_components, monkeypatch
):
    from memtomem.cli.review_cmd import _decide

    comp, mem_dir = bm25_only_components
    _patch_cli_components(monkeypatch, comp)
    candidate = await _candidate(comp, f"Decision: {OPEN}", "p-1")
    assert candidate["destination"] != "pinned"
    claim = AsyncMock(wraps=comp.storage.claim_memory_candidate)
    monkeypatch.setattr(comp.storage, "claim_memory_candidate", claim)
    before = _snapshot(mem_dir)

    with pytest.raises(click.ClickException) as excinfo:
        await _decide(candidate["id"], "approved", "alice", "")

    claim.assert_not_awaited()
    assert _snapshot(mem_dir) == before
    assert excinfo.value.message == _refusal_text()
    assert (await comp.storage.get_memory_candidate(candidate["id"]))["status"] == "pending"
