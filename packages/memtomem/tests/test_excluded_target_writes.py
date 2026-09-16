"""#2488 (rest): every writer refuses an excluded target before writing it.

``index_file`` returns zeroed stats for an excluded file without raising, so a
writer that appends or saves first reports success for content search never
sees — and an append to a file indexed before the exclusion leaves its old
chunks served. ``test_excluded_source_mutation.py`` pins the rewrite surfaces;
this module pins the adds, the new-file writers, and the symlinked destinations
the replace-writers refuse.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.source_provenance import EXCLUDED_TARGET_DETAIL
from memtomem.tools import memory_writer
from memtomem.web import upload_quarantine
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured

DAY_FILE_PATTERN = "20??-??-??*.md"
EXISTING = "## Earlier\n\nAn entry indexed before the exclusion.\n"


def _snapshot(root: Path) -> dict[str, bytes | str]:
    """Every entry under ``root``: bytes for files, the target for links.

    Empty ``.<name>.lock`` sidecars are left out: the locked writers take the
    lock before they can know the final target, exactly as they already do for
    the namespace-mix refusal, and the sidecar carries no content.
    """
    out: dict[str, bytes | str] = {}
    for p in sorted(root.rglob("*")):
        key = p.relative_to(root).as_posix()
        if p.name.startswith(".") and p.name.endswith(".lock") and p.stat().st_size == 0:
            continue
        if p.is_symlink():
            out[key] = "link->" + os.readlink(p)
        elif p.is_file():
            out[key] = p.read_bytes()
    return out


def _rows(comp) -> list:
    return comp.storage._get_db().execute("SELECT * FROM chunks ORDER BY id").fetchall()


def _web_client(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.state.project_root = None
    app.dependency_overrides[require_configured] = lambda: None
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def _patch_cli_components(monkeypatch, comp) -> None:
    @asynccontextmanager
    async def _fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _fake)
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root", lambda comp: None
    )
    monkeypatch.setattr(
        "memtomem.runtime.project_context._resolve_project_context_root", lambda comp: None
    )


@pytest.fixture
async def day_file(bm25_only_components):
    """Today's default day file, indexed, then excluded by a pattern."""
    comp, mem_dir = bm25_only_components
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    source = mem_dir / f"{date_str}.md"
    source.write_text(EXISTING, encoding="utf-8")
    stats = await comp.index_engine.index_file(source, already_scanned=True)
    assert stats.indexed_chunks == 1
    return comp, mem_dir, source


def _exclude_day_files(comp) -> None:
    comp.config.indexing.exclude_patterns = [
        *comp.config.indexing.exclude_patterns,
        DAY_FILE_PATTERN,
    ]


# ── appenders ────────────────────────────────────────────────────────────

APPENDERS = (
    "mcp_add",
    "mcp_batch_add",
    "cli_add",
    "shell_add",
    "web_add",
    "cli_agent_share",
    "langgraph_add",
)


async def _append(surface: str, comp, monkeypatch, capsys, chunk_id: str):
    """Drive one appender; return its refusal text (or response) for comparison."""
    if surface in ("mcp_add", "mcp_batch_add"):
        from memtomem.server.tools import memory_crud

        ctx = StubCtx(AppContext.from_components(comp))
        if surface == "mcp_add":
            return await memory_crud.mem_add("NEW ENTRY", idempotency_key="k-add", ctx=ctx)
        return await memory_crud.mem_batch_add(
            [{"content": "NEW ENTRY"}], idempotency_key="k-batch", ctx=ctx
        )
    if surface == "cli_add":
        _patch_cli_components(monkeypatch, comp)
        return await _cli(_mm_add(None))
    if surface == "shell_add":
        from memtomem.cli.shell import _cmd_add

        capsys.readouterr()
        await _cmd_add(comp, ["NEW", "ENTRY"])
        return capsys.readouterr().out
    if surface == "web_add":
        async with _web_client(comp) as client:
            return await client.post("/api/add", json={"content": "NEW ENTRY"})
    if surface == "cli_agent_share":
        from memtomem.cli.agent_cmd import _run_share

        _patch_cli_components(monkeypatch, comp)
        return await _cli(_run_share(chunk_id, "shared", False))
    if surface == "langgraph_add":
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()
        store._components = comp
        return await store.add("NEW ENTRY")
    raise AssertionError(surface)


def _mm_add(file: str | None):
    """The coroutine ``mm add`` runs (``CliRunner`` would nest ``asyncio.run``)."""
    from memtomem.cli.memory import _add

    return _add(
        "NEW ENTRY",
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


async def _cli(coro) -> str | None:
    """``None`` on success, else the ``ClickException`` message the CLI prints."""
    import click

    try:
        await coro
    except click.ClickException as exc:
        return exc.message
    return None


def _assert_refused(surface: str, result) -> None:
    if surface in ("mcp_add", "mcp_batch_add"):
        assert result == f"Error: {EXCLUDED_TARGET_DETAIL}"
    elif surface in ("cli_add", "cli_agent_share"):
        assert result == EXCLUDED_TARGET_DETAIL
    elif surface == "shell_add":
        assert click_unstyle(result) == f"{EXCLUDED_TARGET_DETAIL}\n"
    elif surface == "web_add":
        assert result.status_code == 409, result.text
        assert result.json()["detail"] == EXCLUDED_TARGET_DETAIL
    elif surface == "langgraph_add":
        assert result == {"error": "source_excluded", "detail": EXCLUDED_TARGET_DETAIL}
    else:
        raise AssertionError(surface)


def click_unstyle(text: str) -> str:
    import click

    return click.unstyle(text)


def _writer_spies(monkeypatch) -> list[Mock]:
    """Spy on every append writer in each module that binds one at import."""
    from memtomem.cli import agent_cmd
    from memtomem.integrations import langgraph
    from memtomem.web.routes import system

    spies = []
    for module, name in (
        (memory_writer, "append_entry"),
        (memory_writer, "append_blocks"),
        (system, "append_entry"),
    ):
        spy = Mock(wraps=getattr(module, name))
        monkeypatch.setattr(module, name, spy)
        spies.append(spy)
    # Modules that import inside the function body resolve through
    # ``memory_writer`` at call time, so the spies above cover them; these
    # asserts keep that assumption honest.
    for module in (agent_cmd, langgraph):
        assert not hasattr(module, "append_entry")
    return spies


@pytest.mark.parametrize("surface", APPENDERS)
async def test_an_appender_refuses_an_excluded_target_before_writing(
    day_file, monkeypatch, capsys, surface
):
    comp, mem_dir, source = day_file
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    _exclude_day_files(comp)
    assert comp.index_engine.is_excluded(source)
    files_before = _snapshot(mem_dir)
    rows_before = _rows(comp)
    index = AsyncMock(wraps=comp.index_engine.index_file)
    monkeypatch.setattr(comp.index_engine, "index_file", index)
    spies = _writer_spies(monkeypatch)

    result = await _append(surface, comp, monkeypatch, capsys, str(chunk.id))

    _assert_refused(surface, result)
    assert _snapshot(mem_dir) == files_before
    assert _rows(comp) == rows_before
    index.assert_not_awaited()
    for spy in spies:
        spy.assert_not_called()
    ledger = comp.storage._get_db().execute("SELECT tool, key FROM idempotency_ledger").fetchall()
    assert ledger == []


@pytest.mark.parametrize("surface", APPENDERS)
async def test_an_appender_still_writes_a_target_that_is_not_excluded(
    day_file, monkeypatch, capsys, surface
):
    """The control: the guard refuses exclusion, not every add."""
    comp, mem_dir, source = day_file
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    files_before = _snapshot(mem_dir)

    result = await _append(surface, comp, monkeypatch, capsys, str(chunk.id))

    if surface in ("mcp_add", "mcp_batch_add"):
        assert not result.startswith("Error"), result
    elif surface in ("cli_add", "cli_agent_share"):
        assert result is None, result
    elif surface == "web_add":
        assert result.status_code == 200, result.text
    elif surface == "langgraph_add":
        assert "error" not in result, result
    assert _snapshot(mem_dir) != files_before


async def test_review_approve_refuses_before_claiming(day_file, monkeypatch):
    """Claim and release each record a transition, so the refusal precedes both."""
    import click

    from memtomem.cli.review_cmd import _decide
    from memtomem.formation import propose_memory_candidate

    comp, mem_dir, source = day_file
    _patch_cli_components(monkeypatch, comp)
    candidate, _created = await propose_memory_candidate(
        comp.storage,
        "Decision: a durable fact",
        source="test",
        source_ref="ref-1",
        idempotency_key="proposal-1",
    )
    assert candidate["destination"] != "pinned"
    _exclude_day_files(comp)
    files_before = _snapshot(mem_dir)
    rows_before = _rows(comp)
    history_before = (
        comp.storage._get_db().execute("SELECT * FROM memory_candidate_transitions").fetchall()
    )
    claim = AsyncMock(wraps=comp.storage.claim_memory_candidate)
    monkeypatch.setattr(comp.storage, "claim_memory_candidate", claim)

    with pytest.raises(click.ClickException) as excinfo:
        await _decide(candidate["id"], "approved", "alice", "")

    assert excinfo.value.message == EXCLUDED_TARGET_DETAIL
    claim.assert_not_awaited()
    assert _snapshot(mem_dir) == files_before
    assert _rows(comp) == rows_before
    assert (
        comp.storage._get_db().execute("SELECT * FROM memory_candidate_transitions").fetchall()
        == history_before
    )
    assert (await comp.storage.get_memory_candidate(candidate["id"]))["status"] == "pending"


async def test_scratch_promote_refuses_an_excluded_target(day_file):
    """Scratch promote never indexes, so the pins are bytes and promotion state."""
    comp, mem_dir, source = day_file
    await comp.storage.scratch_set("note", "promote me")
    _exclude_day_files(comp)
    files_before = _snapshot(mem_dir)

    async with _web_client(comp) as client:
        resp = await client.post("/api/scratch/note/promote", json={})

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == EXCLUDED_TARGET_DETAIL
    assert _snapshot(mem_dir) == files_before
    assert not (await comp.storage.scratch_get("note"))["promoted"]


async def test_scratch_promote_still_appends_a_target_that_is_not_excluded(day_file):
    comp, mem_dir, source = day_file
    await comp.storage.scratch_set("note", "promote me")

    async with _web_client(comp) as client:
        resp = await client.post("/api/scratch/note/promote", json={})

    assert resp.status_code == 200, resp.text
    assert "promote me" in source.read_text(encoding="utf-8")
    assert (await comp.storage.scratch_get("note"))["promoted"]


# ── explicit files inside a nested worktree ──────────────────────────────


@pytest.mark.parametrize("surface", ["mcp_add", "cli_add", "web_add"])
async def test_an_explicit_file_in_a_nested_worktree_is_refused(
    bm25_only_components, monkeypatch, surface
):
    from test_excluded_source_mutation import _repository_with_worktree

    comp, mem_dir = bm25_only_components
    wt = _repository_with_worktree(mem_dir)
    rel = wt.relative_to(mem_dir).as_posix() + "/note.md"
    target = mem_dir / rel
    assert comp.index_engine.is_excluded(target)
    files_before = _snapshot(wt)

    if surface == "mcp_add":
        from memtomem.server.tools import memory_crud

        ctx = StubCtx(AppContext.from_components(comp))
        result = await memory_crud.mem_add("NEW ENTRY", file=str(target), ctx=ctx)
        assert result == f"Error: {EXCLUDED_TARGET_DETAIL}"
    elif surface == "cli_add":
        _patch_cli_components(monkeypatch, comp)
        assert await _cli(_mm_add(rel)) == EXCLUDED_TARGET_DETAIL
    else:
        async with _web_client(comp) as client:
            resp = await client.post("/api/add", json={"content": "NEW ENTRY", "file": rel})
        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"] == EXCLUDED_TARGET_DETAIL

    assert not target.exists()
    assert _snapshot(wt) == files_before


# ── upload ───────────────────────────────────────────────────────────────


@pytest.fixture
def upload_home(bm25_only_components, monkeypatch, tmp_path):
    comp, mem_dir = bm25_only_components
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    return comp, home / ".memtomem" / "uploads"


def _link_spy(monkeypatch) -> Mock:
    spy = Mock(wraps=os.link)
    monkeypatch.setattr(upload_quarantine.os, "link", spy)
    return spy


async def test_upload_refuses_an_excluded_file_and_keeps_the_rest(upload_home, monkeypatch):
    comp, upload_dir = upload_home
    comp.config.indexing.exclude_patterns = ["**/uploads/secret-notes.md"]
    link = _link_spy(monkeypatch)

    async with _web_client(comp) as client:
        resp = await client.post(
            "/api/upload",
            files=[
                ("files", ("secret-notes.md", b"# Hidden\n\nnot for the index\n", "text/markdown")),
                ("files", ("kept.md", b"# Kept\n\nindexed\n", "text/markdown")),
            ],
        )

    assert resp.status_code == 200, resp.text
    by_name = {f["filename"]: f for f in resp.json()["files"]}
    assert by_name["secret-notes.md"] == {
        "filename": "secret-notes.md",
        "indexed_chunks": 0,
        "path": None,
        "error": "source_excluded",
    }
    assert by_name["kept.md"]["error"] is None
    assert by_name["kept.md"]["indexed_chunks"] == 1
    assert not (upload_dir / "secret-notes.md").exists()
    linked = [Path(call.args[1]).name for call in link.call_args_list]
    assert linked == ["kept.md"]


async def test_upload_checks_each_collision_name_before_linking(upload_home, monkeypatch):
    """A renamed candidate is judged before its link, not unlinked afterwards."""
    comp, upload_dir = upload_home
    upload_dir.mkdir(parents=True)
    (upload_dir / "notes.md").write_text("# Taken\n", encoding="utf-8")
    comp.config.indexing.exclude_patterns = ["**/uploads/notes_*.md"]
    monkeypatch.setattr(upload_quarantine.secrets, "token_hex", lambda n: "a" * (2 * n))
    link = _link_spy(monkeypatch)

    async with _web_client(comp) as client:
        resp = await client.post(
            "/api/upload",
            files=[("files", ("notes.md", b"# New\n\nbody\n", "text/markdown"))],
        )

    assert resp.status_code == 200, resp.text
    (result,) = resp.json()["files"]
    assert result["error"] == "source_excluded"
    for call in link.call_args_list:
        assert not comp.index_engine.is_excluded(Path(call.args[1]))
    assert [Path(c.args[1]).name for c in link.call_args_list] == ["notes.md"]
    assert sorted(p.name for p in upload_dir.iterdir()) == ["notes.md"]


# ── fetch, importers, session summary ────────────────────────────────────


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"symlinks unavailable: {exc}")


async def _fetch(comp, output_dir: Path, monkeypatch):
    import socket

    from memtomem.indexing.url_fetcher import fetch_url

    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *a, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="# Page\n\nbody\n", headers={"content-type": "text/plain"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await fetch_url(
            "https://example.com/page",
            output_dir,
            client=client,
            is_excluded=comp.index_engine.is_excluded,
        )


async def test_fetch_refuses_an_excluded_destination(bm25_only_components, monkeypatch):
    from memtomem.indexing.url_fetcher import FetchTargetRefusedError

    comp, mem_dir = bm25_only_components
    output_dir = mem_dir / "_fetched"
    output_dir.mkdir()
    comp.config.indexing.exclude_patterns = ["**/_fetched/**"]

    with pytest.raises(FetchTargetRefusedError) as excinfo:
        await _fetch(comp, output_dir, monkeypatch)

    assert excinfo.value.reason == "excluded"
    assert list(output_dir.iterdir()) == []


async def test_fetch_still_writes_a_destination_that_is_not_excluded(
    bm25_only_components, monkeypatch
):
    comp, mem_dir = bm25_only_components
    output_dir = mem_dir / "_fetched"
    output_dir.mkdir()

    path = await _fetch(comp, output_dir, monkeypatch)

    assert path.read_text(encoding="utf-8").endswith("# Page\n\nbody\n")


def _link_cases(root: Path) -> dict[str, tuple[str, Path]]:
    """Name → (the link's file name, its target), for the three link shapes."""
    allowed = root / "allowed.md"
    excluded = root / "excluded-target.md"
    return {
        "excluded_link_to_allowed_file": ("excluded", allowed),
        "allowed_link_to_excluded_file": ("allowed", excluded),
        "dangling_link": ("allowed", root / "missing.md"),
    }


@pytest.mark.parametrize(
    "case",
    ["excluded_link_to_allowed_file", "allowed_link_to_excluded_file", "dangling_link"],
)
async def test_fetch_refuses_a_symlinked_destination(bm25_only_components, monkeypatch, case):
    from memtomem.indexing.url_fetcher import FetchTargetRefusedError

    comp, mem_dir = bm25_only_components
    output_dir = mem_dir / "_fetched"
    output_dir.mkdir()
    store = mem_dir / "store"
    store.mkdir()
    (store / "allowed.md").write_text("allowed\n", encoding="utf-8")
    (store / "excluded-target.md").write_text("excluded\n", encoding="utf-8")
    kind, target = _link_cases(store)[case]
    comp.config.indexing.exclude_patterns = ["**/excluded-target.md"]
    if kind == "excluded":
        comp.config.indexing.exclude_patterns.append("**/_fetched/example-com-page.md")
    link = output_dir / "example-com-page.md"
    _symlink_or_skip(link, target)
    before = _snapshot(mem_dir)

    with pytest.raises(FetchTargetRefusedError) as excinfo:
        await _fetch(comp, output_dir, monkeypatch)

    assert excinfo.value.reason == "symlink"
    assert _snapshot(mem_dir) == before


def _vault(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "kept.md").write_text("# Kept\n\nkept body\n", encoding="utf-8")
    (vault / "hidden.md").write_text("# Hidden\n\nhidden body\n", encoding="utf-8")
    return vault


@pytest.mark.parametrize("kind", ["obsidian", "notion"])
async def test_import_skips_an_excluded_target(bm25_only_components, tmp_path, kind):
    from memtomem.server.tools import importers

    comp, mem_dir = bm25_only_components
    comp.config.indexing.exclude_patterns = [f"**/_imported/{kind}/hidden.md"]
    ctx = StubCtx(AppContext.from_components(comp))
    tool = getattr(importers, f"mem_import_{kind}")

    result = await tool(str(_vault(tmp_path)), ctx=ctx)

    out = mem_dir / "_imported" / kind
    assert not (out / "hidden.md").exists()
    assert (out / "kept.md").is_file()
    lines = result.splitlines()
    assert "- Files imported: 1" in lines
    assert "- Skipped (target excluded from indexing): 1" in lines
    assert "- Skipped (target is a symlink): 0" in lines


async def test_obsidian_import_that_writes_nothing_says_why(bm25_only_components, tmp_path):
    from memtomem.server.tools.importers import mem_import_obsidian

    comp, mem_dir = bm25_only_components
    comp.config.indexing.exclude_patterns = ["**/_imported/obsidian/**"]
    ctx = StubCtx(AppContext.from_components(comp))

    result = await mem_import_obsidian(str(_vault(tmp_path)), ctx=ctx)

    assert result == (
        "Obsidian import wrote nothing: 2 target(s) excluded from indexing, "
        "0 target(s) are symbolic links, 0 file(s) blocked by the redaction guard."
    )
    assert not (mem_dir / "_imported" / "obsidian" / "kept.md").exists()


@pytest.mark.parametrize(
    "case",
    ["excluded_link_to_allowed_file", "allowed_link_to_excluded_file", "dangling_link"],
)
async def test_importer_skips_a_symlinked_target(bm25_only_components, tmp_path, case):
    from memtomem.indexing.importers import import_obsidian

    comp, mem_dir = bm25_only_components
    out = mem_dir / "_imported" / "obsidian"
    out.mkdir(parents=True)
    store = mem_dir / "store"
    store.mkdir()
    (store / "allowed.md").write_text("allowed\n", encoding="utf-8")
    (store / "excluded-target.md").write_text("excluded\n", encoding="utf-8")
    kind, target = _link_cases(store)[case]
    comp.config.indexing.exclude_patterns = ["**/excluded-target.md"]
    if kind == "excluded":
        comp.config.indexing.exclude_patterns.append("**/_imported/obsidian/hidden.md")
    _symlink_or_skip(out / "hidden.md", target)
    before = _snapshot(mem_dir)
    excluded: list[str] = []
    symlinks: list[str] = []

    imported = await import_obsidian(
        _vault(tmp_path),
        out,
        is_excluded=comp.index_engine.is_excluded,
        excluded_paths=excluded,
        symlink_paths=symlinks,
    )

    assert imported == [out / "kept.md"]
    assert (excluded, symlinks) == ([], ["hidden.md"])
    after = _snapshot(mem_dir)
    after.pop("_imported/obsidian/kept.md")
    assert after == before


@pytest.mark.parametrize("case", ["excluded", "symlink"])
async def test_session_summary_is_not_written_to_a_refused_target(bm25_only_components, case):
    from memtomem.server.tools.session import _SummaryArchive, _write_summary_archive

    comp, mem_dir = bm25_only_components
    archive = mem_dir / "archive"
    archive.mkdir()
    target = archive / "session-1.md"
    if case == "excluded":
        comp.config.indexing.exclude_patterns = ["**/archive/**"]
    else:
        (mem_dir / "elsewhere.md").write_text("elsewhere\n", encoding="utf-8")
        _symlink_or_skip(target, mem_dir / "elsewhere.md")
    before = _snapshot(mem_dir)
    app = AppContext.from_components(comp)

    result = await _write_summary_archive(
        app, _SummaryArchive(content="# Summary\n\nbody\n", target=target, namespace="archive:x")
    )

    assert result == (None, None)
    assert _snapshot(mem_dir) == before


# ── callers of the MCP add core ──────────────────────────────────────────


@pytest.mark.parametrize("idempotency_key", [None, "share-key"])
async def test_mcp_agent_share_returns_the_refusal_not_a_share(day_file, idempotency_key):
    """``mem_agent_share`` wraps ``_mem_add_core``; its refusal is not a share."""
    from memtomem.server.tools.multi_agent import mem_agent_share

    comp, mem_dir, source = day_file
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    _exclude_day_files(comp)
    files_before = _snapshot(mem_dir)
    rows_before = _rows(comp)
    ctx = StubCtx(AppContext.from_components(comp))

    result = await mem_agent_share(str(chunk.id), idempotency_key=idempotency_key, ctx=ctx)

    assert result == f"Error: {EXCLUDED_TARGET_DETAIL}"
    assert _snapshot(mem_dir) == files_before
    assert _rows(comp) == rows_before
    ledger = comp.storage._get_db().execute("SELECT tool, key FROM idempotency_ledger").fetchall()
    assert ledger == []


async def test_mcp_scratch_promote_leaves_the_entry_unpromoted_on_refusal(day_file):
    from memtomem.server.tools.scratch import mem_scratch_promote

    comp, mem_dir, source = day_file
    await comp.storage.scratch_set("note", "promote me")
    _exclude_day_files(comp)
    files_before = _snapshot(mem_dir)
    ctx = StubCtx(AppContext.from_components(comp))

    result = await mem_scratch_promote("note", ctx=ctx)

    assert result == f"Error: {EXCLUDED_TARGET_DETAIL}"
    assert _snapshot(mem_dir) == files_before
    assert not (await comp.storage.scratch_get("note"))["promoted"]


async def test_mcp_reflect_save_links_nothing_on_refusal(day_file):
    """A refused save has no insight chunk; the newest chunk must not stand in for it."""
    from memtomem.server.tools.reflection import mem_reflect_save

    comp, mem_dir, source = day_file
    (chunk,) = await comp.storage.list_chunks_by_source(source)
    comp.config.indexing.exclude_patterns = ["**/reflections.md"]
    files_before = _snapshot(mem_dir)
    rows_before = _rows(comp)
    db = comp.storage._get_db()
    relations_before = db.execute("SELECT * FROM chunk_relations").fetchall()
    ctx = StubCtx(AppContext.from_components(comp))

    result = await mem_reflect_save("an insight", related_chunks=[str(chunk.id)], ctx=ctx)

    assert result == f"Error: {EXCLUDED_TARGET_DETAIL}"
    assert _snapshot(mem_dir) == files_before
    assert _rows(comp) == rows_before
    assert db.execute("SELECT * FROM chunk_relations").fetchall() == relations_before
