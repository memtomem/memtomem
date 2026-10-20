"""#2807: a writer's default day file is judged where it resolves.

Every surface that appends to the day file without a caller-named target ran
its gates for the scope that chose the base directory and then appended to
``<base>/<day file>`` as spelled. A link at that path carried the write into
another directory — a registered ``project_shared`` tier outside the base, or
one nested inside it — and that tier's gates never ran. The day file is now
resolved and refused when it leaves the base or classifies to another scope.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from helpers import StubCtx
from memtomem.context._atomic import memory_lock_path
from memtomem.memory_scope import day_file_name
from memtomem.server.context import AppContext
from test_excluded_target_writes import (
    APPENDERS,
    _append,
    _patch_cli_components,
    _rows,
    _snapshot,
    _symlink_or_skip,
    _web_client,
    _writer_spies,
)

pytestmark = pytest.mark.asyncio

REFUSAL = "nothing was written"
TIER_TEXT = "# Shared\n\nA note in the git-tracked tier.\n"
# The namespaces the appenders in ``APPENDERS`` write under: the default for
# every add, and the target namespace ``_append`` gives ``mm agent share``.
NAMESPACES = (None, "shared")


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _day_file(comp, namespace: str | None) -> str:
    return day_file_name(namespace, comp.config.namespace.default_namespace, date_str=_today())


def _tier(comp, mem_dir: Path, tmp_path: Path, where: str) -> Path:
    """A registered ``project_shared`` tier holding one note; returns the note."""
    root = {"outside": tmp_path / "proj", "nested": mem_dir / "proj"}[where]
    tier = root / ".memtomem" / "memories"
    tier.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [tier]
    note = tier / "notes.md"
    note.write_text(TIER_TEXT, encoding="utf-8")
    return note


@pytest.fixture
async def seeded(bm25_only_components):
    """A store with one indexed chunk outside the day file, for ``mm agent share``."""
    comp, mem_dir = bm25_only_components
    seed = mem_dir / "seed.md"
    seed.write_text("## Seed\n\nA note to share.\n", encoding="utf-8")
    await comp.index_engine.index_file(seed, ingress_guarded=True)
    (chunk,) = await comp.storage.list_chunks_by_source(seed)
    return comp, mem_dir, str(chunk.id)


def _assert_refused(surface: str, result) -> None:
    if surface in ("mcp_add", "mcp_batch_add"):
        assert result.startswith("Error: ") and REFUSAL in result, result
    elif surface in ("cli_add", "cli_agent_share"):
        assert result is not None and REFUSAL in result, result
    elif surface == "shell_add":
        assert REFUSAL in result, result
    elif surface == "web_add":
        assert result.status_code == 422, result.text
        assert REFUSAL in result.json()["detail"]
    else:
        raise AssertionError(surface)


@pytest.mark.parametrize("where", ["outside", "nested"])
@pytest.mark.parametrize("surface", [s for s in APPENDERS if s != "langgraph_add"])
async def test_a_linked_day_file_is_refused_before_writing(
    seeded, tmp_path, monkeypatch, capsys, surface, where
):
    comp, mem_dir, chunk_id = seeded
    note = _tier(comp, mem_dir, tmp_path, where)
    for ns in NAMESPACES:
        _symlink_or_skip(mem_dir / _day_file(comp, ns), note)
    files_before = _snapshot(tmp_path)
    rows_before = _rows(comp)
    index = AsyncMock(wraps=comp.index_engine.index_file)
    monkeypatch.setattr(comp.index_engine, "index_file", index)
    spies = _writer_spies(monkeypatch)

    result = await _append(surface, comp, monkeypatch, capsys, chunk_id)

    _assert_refused(surface, result)
    assert note.read_text(encoding="utf-8") == TIER_TEXT
    assert _snapshot(tmp_path) == files_before
    assert _rows(comp) == rows_before
    index.assert_not_awaited()
    for spy in spies:
        spy.assert_not_called()
    assert not memory_lock_path(note).exists()


@pytest.mark.parametrize("surface", [s for s in APPENDERS if s != "langgraph_add"])
async def test_a_day_file_linked_inside_its_own_tier_still_writes(
    seeded, monkeypatch, capsys, surface
):
    """The control: a link is refused for where it leads, not for being a link."""
    comp, mem_dir, chunk_id = seeded
    real = mem_dir / "real.md"
    real.write_text("## Earlier\n\nAn entry.\n", encoding="utf-8")
    for ns in NAMESPACES:
        _symlink_or_skip(mem_dir / _day_file(comp, ns), real)

    result = await _append(surface, comp, monkeypatch, capsys, chunk_id)

    if surface in ("mcp_add", "mcp_batch_add"):
        assert not result.startswith("Error"), result
    elif surface in ("cli_add", "cli_agent_share"):
        assert result is None, result
    elif surface == "web_add":
        assert result.status_code == 200, result.text
    text = real.read_text(encoding="utf-8")
    assert "NEW ENTRY" in text or "A note to share." in text


# ── the in-lock re-target ────────────────────────────────────────────────
#
# The day file's name follows the namespace, which is only authoritative
# inside the lock (#1991), so the writers re-target there. A link at the day
# file of the in-lock namespace must be refused too, after the pre-lock target
# passed: each test picks a safe "aaa" file before the lock and a linked "bbb"
# file inside it.


@pytest.mark.parametrize("where", ["outside", "nested"])
@pytest.mark.parametrize("surface", ["mcp_add", "mcp_batch_add", "cli_add"])
async def test_a_linked_day_file_found_by_the_in_lock_retarget_is_refused(
    seeded, tmp_path, monkeypatch, capsys, surface, where
):
    from memtomem.server.tools import memory_crud

    comp, mem_dir, chunk_id = seeded
    note = _tier(comp, mem_dir, tmp_path, where)
    _symlink_or_skip(mem_dir / _day_file(comp, "bbb"), note)
    files_before = _snapshot(tmp_path)
    index = AsyncMock(wraps=comp.index_engine.index_file)
    monkeypatch.setattr(comp.index_engine, "index_file", index)
    if surface == "cli_add":
        answers = iter(["aaa", "bbb", "bbb", "bbb"])
        monkeypatch.setattr(
            "memtomem.cli._session_state.resolve_session_write_namespace",
            AsyncMock(side_effect=lambda storage: next(answers)),
        )
    else:
        pairs = iter([(None, "aaa"), (None, "bbb"), (None, "bbb"), (None, "bbb")])

        async def _capture(app, namespace):
            return next(pairs)

        monkeypatch.setattr(memory_crud, "capture_session_and_namespace", _capture)

    result = await _append(surface, comp, monkeypatch, capsys, chunk_id)

    _assert_refused(surface, result)
    assert note.read_text(encoding="utf-8") == TIER_TEXT
    assert _snapshot(tmp_path) == files_before
    index.assert_not_awaited()
    assert not memory_lock_path(note).exists()


# ── LangGraph: containment here, the tier gated by the adapter ───────────


async def _langgraph_add(comp, **kwargs):
    from memtomem.integrations.langgraph import MemtomemStore

    store = MemtomemStore()
    store._components = comp
    return await store.add("NEW ENTRY", **kwargs)


def _link_langgraph_day_file(comp, mem_dir: Path, note: Path) -> None:
    from memtomem.integrations.langgraph import MemtomemStore

    store = MemtomemStore()
    store._components = comp
    _symlink_or_skip(mem_dir / _day_file(comp, store._resolve_add_namespace(None)), note)


async def test_langgraph_refuses_a_day_file_linked_out_of_the_base(bm25_only_components, tmp_path):
    comp, mem_dir = bm25_only_components
    note = _tier(comp, mem_dir, tmp_path, "outside")
    _link_langgraph_day_file(comp, mem_dir, note)

    result = await _langgraph_add(comp, confirm_project_shared=True)

    assert result["error"] == "target_outside_memory_dir", result
    assert REFUSAL in result["detail"]
    assert note.read_text(encoding="utf-8") == TIER_TEXT
    assert await comp.storage.list_chunks_by_source(note.resolve()) == []


async def test_langgraph_gates_a_day_file_linked_into_a_nested_tier(bm25_only_components, tmp_path):
    """Inside the base, the adapter classifies where the link lands and asks for consent."""
    comp, mem_dir = bm25_only_components
    note = _tier(comp, mem_dir, tmp_path, "nested")
    _link_langgraph_day_file(comp, mem_dir, note)

    refused = await _langgraph_add(comp)

    assert refused["error"] == "project_shared_confirmation_required", refused
    assert note.read_text(encoding="utf-8") == TIER_TEXT

    consented = await _langgraph_add(comp, confirm_project_shared=True)

    assert "error" not in consented, consented
    assert "NEW ENTRY" in note.read_text(encoding="utf-8")
    rows = await comp.storage.list_chunks_by_source(note.resolve())
    assert rows and {r.metadata.scope for r in rows} == {"project_shared"}


# ── review approve and scratch promote ───────────────────────────────────


@pytest.mark.parametrize("where", ["outside", "nested"])
async def test_review_approve_refuses_a_linked_day_file_before_claiming(
    bm25_only_components, tmp_path, monkeypatch, where
):
    import click

    from memtomem.cli.review_cmd import _decide
    from memtomem.formation import propose_memory_candidate

    comp, mem_dir = bm25_only_components
    _patch_cli_components(monkeypatch, comp)
    note = _tier(comp, mem_dir, tmp_path, where)
    _symlink_or_skip(mem_dir / f"{_today()}.md", note)
    candidate, _created = await propose_memory_candidate(
        comp.storage,
        "Decision: a durable fact",
        source="test",
        source_ref="ref-1",
        idempotency_key="proposal-1",
    )
    assert candidate["destination"] != "pinned"
    claim = AsyncMock(wraps=comp.storage.claim_memory_candidate)
    monkeypatch.setattr(comp.storage, "claim_memory_candidate", claim)

    with pytest.raises(click.ClickException) as excinfo:
        await _decide(candidate["id"], "approved", "alice", "")

    assert REFUSAL in excinfo.value.message
    claim.assert_not_awaited()
    assert note.read_text(encoding="utf-8") == TIER_TEXT
    assert (await comp.storage.get_memory_candidate(candidate["id"]))["status"] == "pending"


@pytest.mark.parametrize("where", ["outside", "nested"])
async def test_scratch_promote_refuses_a_linked_day_file(bm25_only_components, tmp_path, where):
    comp, mem_dir = bm25_only_components
    note = _tier(comp, mem_dir, tmp_path, where)
    _symlink_or_skip(mem_dir / f"{_today()}.md", note)
    await comp.storage.scratch_set("note", "promote me")

    async with _web_client(comp) as client:
        resp = await client.post("/api/scratch/note/promote", json={})

    assert resp.status_code == 403, resp.text
    assert REFUSAL in resp.json()["detail"]
    assert note.read_text(encoding="utf-8") == TIER_TEXT
    assert not (await comp.storage.scratch_get("note"))["promoted"]


# ── the helper ───────────────────────────────────────────────────────────


async def test_a_dangling_link_is_judged_by_where_it_would_create_the_file(tmp_path):
    from memtomem.memory_scope import MemoryScopeError, resolve_scope_target

    base = tmp_path / "memories"
    base.mkdir()
    _symlink_or_skip(base / "day.md", tmp_path / "elsewhere" / "day.md")

    with pytest.raises(MemoryScopeError, match=REFUSAL):
        resolve_scope_target(base, "day.md", scope="user", project_memory_dirs=[])

    assert not (tmp_path / "elsewhere").exists()


async def test_mcp_add_reports_the_resolved_day_file(bm25_only_components):
    """The target is now the resolved path, the one the CLI and web already used."""
    from memtomem.server.tools.memory_crud import mem_add

    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))

    out = await mem_add(content="NEW ENTRY", ctx=ctx)  # type: ignore[arg-type]

    assert f"Memory added to {(mem_dir / _day_file(comp, None)).resolve()}" in out, out
