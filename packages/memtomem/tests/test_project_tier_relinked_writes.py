"""#3623: a write into a project tier refuses a tier reached through a link.

A project-tier write derives its directory from the project root and resolves
it, so project A's ``.memtomem/memories.local`` linked to project B's
registered local tier used to carry A's note into B: registered, indexed as
B's, invisible from A. The indexer already leaves such a tier out
(``project_tier_is_relinked``); every surface that derives a tier from the
project root to write into it now asks the same question first.

Each refusal case compares B's whole ``.memtomem`` tree before and after, so a
write that appends to an existing file counts as reaching B, not only one
that creates a file. Nothing here imports the new resolver (its own tests are
``test_project_tier_write_resolver.py``), so this file also runs against a
revision without the fix and shows each write reaching B.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import StubCtx, isolate_memtomem_env, set_home
from memtomem.memory_scope import MemoryScopeError
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from test_excluded_target_writes import _cli, _patch_cli_components, _web_client

REFUSAL = "is a symbolic link or junction, or could not be inspected; nothing was written"
SEED = "## Earlier\n\nAn entry B wrote itself.\n"


def _snapshot(root: Path) -> dict[str, bytes | str]:
    """Every entry under ``root``: directories, links, and each file's bytes.

    Nothing is left out — not an empty lock sidecar, not an empty directory —
    because a refused write is refused before it takes a lock or makes one.
    """
    out: dict[str, bytes | str] = {}
    for p in sorted(root.rglob("*")):
        key = p.relative_to(root).as_posix()
        if p.is_symlink():
            out[key] = "link->" + str(p.readlink())
        elif p.is_dir():
            out[key] = "dir"
        else:
            out[key] = p.read_bytes()
    return out


def _link_dir(link: Path, target: Path) -> None:
    link.symlink_to(target, target_is_directory=True)


@pytest.fixture
def projects(tmp_path):
    """Project B with both real tiers; project A with a real shared tier only.

    Each tier of B holds a seeded ``note.md`` so an append into it shows up in
    the snapshot. A is marked with ``.git`` so the CLI commands that look for a
    project root from the working directory find it.
    """
    a = tmp_path / "project-a"
    b = tmp_path / "project-b"
    for root in (a, b):
        (root / ".git").mkdir(parents=True)
    b_shared = b / ".memtomem" / "memories"
    b_local = b / ".memtomem" / "memories.local"
    for tier in (b_shared, b_local):
        tier.mkdir(parents=True)
        (tier / "note.md").write_text(SEED, encoding="utf-8")
    return SimpleNamespace(
        root=tmp_path, a=a, b=b, b_shared=b_shared, b_local=b_local, b_dot=b / ".memtomem"
    )


def _link_local_tier(p) -> None:
    """A's ``.memtomem/memories`` is real; its ``memories.local`` is B's tier."""
    (p.a / ".memtomem" / "memories").mkdir(parents=True)
    _link_dir(p.a / ".memtomem" / "memories.local", p.b_local)


def _link_dot_memtomem(p) -> None:
    """A's whole ``.memtomem`` is B's."""
    _link_dir(p.a / ".memtomem", p.b_dot)


def _register(comp, p) -> None:
    dirs = [p.b_shared, p.b_local]
    a_shared = p.a / ".memtomem" / "memories"
    if a_shared.is_dir() and not (p.a / ".memtomem").is_symlink():
        dirs.append(a_shared)
    comp.config.indexing.project_memory_dirs = dirs


def _assert_refused(text: str, p, entry: str) -> None:
    assert f"{entry}, or the .memtomem directory holding it, {REFUSAL}" in text, text
    # Neither project's path, nor where the link points, is named.
    assert str(p.root) not in text, text
    assert p.root.as_posix() not in text, text


def _mcp(comp, monkeypatch, p) -> StubCtx:
    _register(comp, p)
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root", lambda app: p.a
    )
    return StubCtx(AppContext.from_components(comp))


# ---------------------------------------------------------------------------
# MCP: mem_add, mem_batch_add, relative file=
# ---------------------------------------------------------------------------


@pytest.mark.requires_symlinks
@pytest.mark.asyncio
async def test_mem_add_project_local_through_a_linked_tier_is_refused(
    bm25_only_components, monkeypatch, projects
):
    comp, _ = bm25_only_components
    _link_local_tier(projects)
    ctx = _mcp(comp, monkeypatch, projects)
    before = _snapshot(projects.b_dot)

    out = await memory_crud.mem_add(
        content="a note written from project a", scope="project_local", ctx=ctx
    )

    _assert_refused(out, projects, ".memtomem/memories.local")
    assert out.startswith("Error: "), out
    assert _snapshot(projects.b_dot) == before


@pytest.mark.requires_symlinks
@pytest.mark.asyncio
async def test_mem_batch_add_project_local_through_a_linked_tier_is_refused(
    bm25_only_components, monkeypatch, projects
):
    comp, _ = bm25_only_components
    _link_local_tier(projects)
    ctx = _mcp(comp, monkeypatch, projects)
    before = _snapshot(projects.b_dot)

    out = await memory_crud.mem_batch_add(
        entries=[{"key": "k", "value": "a note written from project a"}],
        scope="project_local",
        ctx=ctx,
    )

    _assert_refused(out, projects, ".memtomem/memories.local")
    assert _snapshot(projects.b_dot) == before


@pytest.mark.requires_symlinks
@pytest.mark.asyncio
async def test_mem_add_relative_file_through_a_linked_tier_is_refused(
    bm25_only_components, monkeypatch, projects
):
    """``file="note.md"`` names B's seeded file through the link: an append."""
    comp, _ = bm25_only_components
    _link_local_tier(projects)
    ctx = _mcp(comp, monkeypatch, projects)
    before = _snapshot(projects.b_dot)

    out = await memory_crud.mem_add(
        content="a note written from project a",
        file="note.md",
        scope="project_local",
        ctx=ctx,
    )

    _assert_refused(out, projects, ".memtomem/memories.local")
    assert _snapshot(projects.b_dot) == before


@pytest.mark.requires_symlinks
@pytest.mark.asyncio
async def test_mem_add_project_shared_through_a_linked_dot_memtomem_is_refused(
    bm25_only_components, monkeypatch, projects
):
    comp, _ = bm25_only_components
    _link_dot_memtomem(projects)
    ctx = _mcp(comp, monkeypatch, projects)
    before = _snapshot(projects.b_dot)

    out = await memory_crud.mem_add(
        content="a note written from project a",
        scope="project_shared",
        confirm_project_shared=True,
        ctx=ctx,
    )

    _assert_refused(out, projects, ".memtomem/memories")
    assert _snapshot(projects.b_dot) == before


@pytest.mark.asyncio
async def test_mem_add_project_local_into_a_regular_tier_still_writes(
    bm25_only_components, monkeypatch, projects
):
    """The success twin: A's own, regular local tier takes the note."""
    comp, _ = bm25_only_components
    a_local = projects.a / ".memtomem" / "memories.local"
    a_local.mkdir(parents=True)
    _register(comp, projects)
    comp.config.indexing.project_memory_dirs = [
        *comp.config.indexing.project_memory_dirs,
        a_local,
    ]
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root", lambda app: projects.a
    )
    ctx = StubCtx(AppContext.from_components(comp))
    before = _snapshot(projects.b_dot)

    out = await memory_crud.mem_add(
        content="a note written from project a", scope="project_local", ctx=ctx
    )

    assert out.startswith("Memory added to"), out
    assert [p.name for p in a_local.glob("*.md")], out
    assert _snapshot(projects.b_dot) == before


@pytest.mark.asyncio
async def test_mem_add_into_a_tier_that_fails_to_resolve_is_refused_without_its_path(
    bm25_only_components, monkeypatch, projects
):
    """#3646: the tier passed the link check, then ``resolve()`` raised.

    That is a tier swapped for a loop in between on Python 3.12, whose
    ``RuntimeError`` names the absolute path. It is injected here, on A's own
    registered tier, so the refusal is the only thing that stops the write.
    """
    comp, _ = bm25_only_components
    a_local = projects.a / ".memtomem" / "memories.local"
    a_local.mkdir(parents=True)
    # ``_mcp`` registers B's tiers; A's local tier is added after it.
    ctx = _mcp(comp, monkeypatch, projects)
    comp.config.indexing.project_memory_dirs = [
        *comp.config.indexing.project_memory_dirs,
        a_local,
    ]
    real_resolve = Path.resolve

    def _resolve(self, *args, **kwargs):
        if self == a_local:
            raise RuntimeError(f"Symlink loop from '{self}'")
        return real_resolve(self, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", _resolve)
    before = _snapshot(projects.b_dot)

    out = await memory_crud.mem_add(
        content="a note written from project a", scope="project_local", ctx=ctx
    )

    _assert_refused(out, projects, ".memtomem/memories.local")
    assert out.startswith("Error: "), out
    assert not list(a_local.rglob("*")), out
    assert _snapshot(projects.b_dot) == before


# ---------------------------------------------------------------------------
# CLI mm mem add, web POST /api/add
# ---------------------------------------------------------------------------


@pytest.mark.requires_symlinks
@pytest.mark.asyncio
async def test_mm_add_project_local_through_a_linked_tier_is_refused(
    bm25_only_components, monkeypatch, projects
):
    from memtomem.cli.memory import _add

    comp, _ = bm25_only_components
    _link_local_tier(projects)
    _register(comp, projects)
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.setattr(
        "memtomem.runtime.project_context._resolve_project_context_root",
        lambda comp: projects.a,
    )
    before = _snapshot(projects.b_dot)

    result = await _cli(
        _add(
            "a note written from project a",
            None,
            [],
            None,
            False,
            "project_local",
            False,
            True,
            namespace=None,
            as_json=False,
            allow_namespace_mix=False,
        )
    )

    assert result is not None
    _assert_refused(result, projects, ".memtomem/memories.local")
    assert _snapshot(projects.b_dot) == before


@pytest.mark.requires_symlinks
@pytest.mark.asyncio
async def test_web_add_project_local_through_a_linked_tier_is_refused(
    bm25_only_components, monkeypatch, projects
):
    comp, _ = bm25_only_components
    _link_local_tier(projects)
    _register(comp, projects)
    monkeypatch.setattr(
        "memtomem.runtime.project_context._resolve_project_context_from_dirs",
        lambda dirs: projects.a,
    )
    before = _snapshot(projects.b_dot)

    async with _web_client(comp) as client:
        resp = await client.post(
            "/api/add",
            json={"content": "a note written from project a", "scope": "project_local"},
        )

    assert resp.status_code == 422, resp.text
    _assert_refused(resp.json()["detail"], projects, ".memtomem/memories.local")
    assert _snapshot(projects.b_dot) == before


# ---------------------------------------------------------------------------
# mm context memory-migrate, mm mem init, LangGraph store
# ---------------------------------------------------------------------------


@pytest.mark.requires_symlinks
@pytest.mark.asyncio
async def test_memory_migrate_into_a_linked_tier_is_refused(
    bm25_only_components, monkeypatch, projects
):
    import click

    from memtomem.cli.context_cmd import _memory_migrate_run

    comp, mem_dir = bm25_only_components
    _link_local_tier(projects)
    _register(comp, projects)
    src = mem_dir / "rule.md"
    src.write_text("## Rule\n\nharmless body.\n", encoding="utf-8")
    await comp.index_engine.index_file(src, ingress_guarded=True)
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(projects.a)
    before = _snapshot(projects.b_dot)

    with pytest.raises(click.ClickException) as exc_info:
        await _memory_migrate_run(
            [src.resolve()],
            from_scope="user",
            to_scope="project_local",
            apply_=True,
            yes=True,
            confirm_project_shared=False,
        )

    _assert_refused(exc_info.value.message, projects, ".memtomem/memories.local")
    assert src.read_text(encoding="utf-8") == "## Rule\n\nharmless body.\n"
    assert _snapshot(projects.b_dot) == before


@pytest.mark.requires_symlinks
@pytest.mark.asyncio
async def test_memory_migrate_between_project_tiers_into_a_linked_tier_is_refused(
    bm25_only_components, monkeypatch, projects
):
    """From A's regular shared tier into its linked local tier: the other resolver call."""
    import click

    from memtomem.cli.context_cmd import _memory_migrate_run

    comp, _ = bm25_only_components
    _link_local_tier(projects)
    _register(comp, projects)
    a_shared = projects.a / ".memtomem" / "memories"
    src = a_shared / "rule.md"
    src.write_text("## Rule\n\nharmless body.\n", encoding="utf-8")
    await comp.index_engine.index_file(src, ingress_guarded=True)
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(projects.a)
    before_a = _snapshot(a_shared)
    before_b = _snapshot(projects.b_dot)

    with pytest.raises(click.ClickException) as exc_info:
        await _memory_migrate_run(
            [src.resolve()],
            from_scope="project_shared",
            to_scope="project_local",
            apply_=True,
            yes=True,
            confirm_project_shared=False,
        )

    _assert_refused(exc_info.value.message, projects, ".memtomem/memories.local")
    assert _snapshot(a_shared) == before_a
    assert _snapshot(projects.b_dot) == before_b


@pytest.mark.requires_symlinks
def test_mm_mem_init_through_a_linked_tier_is_refused(tmp_path, monkeypatch, projects):
    from click.testing import CliRunner

    from memtomem import config as _cfg
    from memtomem.cli.mem_cmd import mem

    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    override = home / ".memtomem" / "config.json"
    monkeypatch.setattr(_cfg, "_override_path", lambda: override)
    _link_local_tier(projects)
    monkeypatch.chdir(projects.a)
    before = _snapshot(projects.b_dot)

    result = CliRunner().invoke(mem, ["init", "--scope", "project_local"])

    assert result.exit_code != 0, result.output
    _assert_refused(result.output, projects, ".memtomem/memories.local")
    # Refused before the .gitignore guard block and before registration.
    assert not (projects.a / ".gitignore").exists()
    assert not override.exists()
    assert _snapshot(projects.b_dot) == before


@pytest.mark.requires_symlinks
def test_langgraph_store_with_a_derived_linked_tier_is_refused(tmp_path, monkeypatch, projects):
    from memtomem.integrations.langgraph_store import MemtomemBaseStore

    import memtomem.config as _cfg

    isolate_memtomem_env(monkeypatch)
    set_home(monkeypatch, tmp_path / "home")
    # The derived-root branch loads the configuration with migration on, which
    # can rewrite ~/.memtomem/config.json: a refused store must not get there.
    loads: list[object] = []
    monkeypatch.setattr(_cfg, "load_config_overrides", lambda *a, **kw: loads.append(a))
    _link_local_tier(projects)
    before = _snapshot(projects.b_dot)

    with pytest.raises(MemoryScopeError) as exc_info:
        MemtomemBaseStore(scope="project_local", project_root=projects.a)

    _assert_refused(str(exc_info.value), projects, ".memtomem/memories.local")
    assert loads == []
    assert not (projects.b_local / "langgraph-store").exists()
    assert _snapshot(projects.b_dot) == before
