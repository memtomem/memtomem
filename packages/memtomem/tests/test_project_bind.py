"""``mem_project_bind`` — a stdio server started outside the project (#2920).

The Antigravity CLI starts a plugin's MCP server in the plugin's own
directory, and the project context comes from the working directory, so
``project_local`` never resolves there. These tests run every tool from such
a directory (``plugin_cwd``) against a real BM25-only store.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from helpers import StubCtx
from memtomem.runtime.project_context import _resolve_project_context_root
from memtomem.server import context as server_context
from memtomem.server.context import AppContext
from memtomem.server.health_checks import check_full_health_report
from memtomem.server.tools.memory_crud import mem_add
from memtomem.server.tools.browse import mem_read
from memtomem.server.tools.meta import mem_do
from memtomem.server.tools.project_bind import mem_project_bind
from memtomem.server.tools.recall import mem_recall
from memtomem.server.tools.search import mem_search
from memtomem.server.tools.status_config import mem_status


def _make_project(base: Path, name: str) -> tuple[Path, Path]:
    root = base / name
    (root / ".git").mkdir(parents=True)
    tier = root / ".memtomem" / "memories.local"
    tier.mkdir(parents=True)
    return root.resolve(), tier


@pytest.fixture
def stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(server_context, "_server_transport", "stdio")


@pytest.fixture
def plugin_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cwd = tmp_path / "plugins" / "memtomem"
    cwd.mkdir(parents=True)
    monkeypatch.chdir(cwd)
    return cwd


@pytest.fixture
def bound_setup(bm25_only_components, tmp_path: Path, stdio, plugin_cwd):
    comp, _mem_dir = bm25_only_components
    root, tier = _make_project(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [tier]
    app = AppContext.from_components(comp)
    return app, StubCtx(app), root, tier


# ---------------------------------------------------------------------------
# Binding resolves project-scoped memory from outside the project
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unbound_server_outside_the_project_has_no_project(bound_setup) -> None:
    app, ctx, _root, _tier = bound_setup
    assert _resolve_project_context_root(app) is None
    out = await mem_add(content="before bind", scope="project_local", ctx=ctx)
    assert out.startswith("Error"), out


@pytest.mark.asyncio
async def test_bound_server_reads_and_writes_the_project(bound_setup) -> None:
    app, ctx, root, tier = bound_setup

    out = await mem_project_bind(project_root=str(root), ctx=ctx)
    assert out.startswith(f"Bound project root: {root}"), out
    assert app.bound_project_root == root

    status = await mem_status(ctx=ctx)
    project_lines = [line for line in status.splitlines() if line.startswith("Project root:")]
    assert len(project_lines) == 1, status
    assert project_lines[0].endswith(f"{root.name} (bound)"), project_lines
    assert "bound_project_unregistered" not in status

    added = await mem_add(
        content="the bound project records this decision",
        title="Bound decision",
        scope="project_local",
        ctx=ctx,
    )
    assert not added.startswith("Error"), added
    written = list(tier.rglob("*.md"))
    assert written, "project_local write did not land in the bound project's tier"

    found = json.loads(
        await mem_search(
            query="bound project records decision",
            scope="project_local",
            output_format="structured",
            ctx=ctx,
        )
    )
    assert found["results"], found
    recalled = json.loads(
        await mem_recall(scope="project_local", output_format="structured", ctx=ctx)
    )
    assert recalled["results"], recalled


@pytest.mark.asyncio
async def test_a_path_inside_the_project_binds_its_root(bound_setup) -> None:
    app, ctx, root, _tier = bound_setup
    inner = root / "src" / "pkg"
    inner.mkdir(parents=True)
    out = await mem_project_bind(project_root=str(inner), ctx=ctx)
    assert out.startswith(f"Bound project root: {root}"), out
    assert app.bound_project_root == root


@pytest.mark.asyncio
async def test_binding_beats_a_registered_cwd(
    bm25_only_components, tmp_path: Path, stdio, monkeypatch: pytest.MonkeyPatch
) -> None:
    comp, _ = bm25_only_components
    cwd_root, cwd_tier = _make_project(tmp_path, "cwd-proj")
    other_root, other_tier = _make_project(tmp_path, "other-proj")
    comp.config.indexing.project_memory_dirs = [cwd_tier, other_tier]
    monkeypatch.chdir(cwd_root)
    app = AppContext.from_components(comp)
    assert _resolve_project_context_root(app) == cwd_root

    out = await mem_project_bind(project_root=str(other_root), ctx=StubCtx(app))
    assert out.startswith("Bound project root"), out
    assert _resolve_project_context_root(app) == other_root


# ---------------------------------------------------------------------------
# Set once
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_root_again_is_idempotent(bound_setup) -> None:
    app, ctx, root, _tier = bound_setup
    await mem_project_bind(project_root=str(root), ctx=ctx)
    again = await mem_project_bind(project_root=str(root), ctx=ctx)
    assert again == f"Project root already bound: {root}"
    assert app.bound_project_root == root


@pytest.mark.asyncio
async def test_another_root_is_refused_and_the_first_binding_stays(
    bound_setup, tmp_path: Path
) -> None:
    app, ctx, root, tier = bound_setup
    other_root, other_tier = _make_project(tmp_path, "other")
    app.config.indexing.project_memory_dirs = [tier, other_tier]
    await mem_project_bind(project_root=str(root), ctx=ctx)

    out = await mem_project_bind(project_root=str(other_root), ctx=ctx)
    assert out.startswith("Error: this server is already bound to"), out
    assert "restart the server" in out
    assert app.bound_project_root == root


# ---------------------------------------------------------------------------
# Refusals change nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", [None, "sse", "streamable-http"])
async def test_only_a_stdio_server_can_bind(
    bound_setup, monkeypatch: pytest.MonkeyPatch, transport
) -> None:
    app, ctx, root, _tier = bound_setup
    monkeypatch.setattr(server_context, "_server_transport", transport)
    out = await mem_project_bind(project_root=str(root), ctx=ctx)
    assert out.startswith("Error: project_bind works only on a stdio server"), out
    assert app.bound_project_root is None


@pytest.mark.asyncio
async def test_a_process_not_started_by_main_cannot_bind(
    bm25_only_components, tmp_path: Path, plugin_cwd, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The state every embedder and ``from_components`` context is in: nothing
    # ran ``main()``. Set explicitly, since server tests in this process do.
    monkeypatch.setattr(server_context, "_server_transport", None)
    comp, _ = bm25_only_components
    root, tier = _make_project(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [tier]
    app = AppContext.from_components(comp)
    out = await mem_project_bind(project_root=str(root), ctx=StubCtx(app))
    assert "this server's transport: unknown" in out, out
    assert app.bound_project_root is None


@pytest.mark.asyncio
async def test_unregistered_path_is_refused_with_the_init_command(
    bound_setup, tmp_path: Path
) -> None:
    app, ctx, _root, _tier = bound_setup
    bare = tmp_path / "bare"
    (bare / ".git").mkdir(parents=True)
    out = await mem_project_bind(project_root=str(bare), ctx=ctx)
    assert out.startswith("Error: no registered project tier covers"), out
    assert "mm mem init --scope project_local" in out
    assert app.bound_project_root is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "value, message",
    [
        ("", "must be a non-empty absolute path"),
        ("relative/path", "must be an absolute path"),
        ("~/proj", "must be an absolute path"),
    ],
)
async def test_malformed_paths_are_refused(bound_setup, value, message) -> None:
    app, ctx, _root, _tier = bound_setup
    out = await mem_project_bind(project_root=value, ctx=ctx)
    assert out.startswith("Error") and message in out, out
    assert app.bound_project_root is None


@pytest.mark.asyncio
async def test_missing_path_and_file_are_refused(bound_setup, tmp_path: Path) -> None:
    app, ctx, root, _tier = bound_setup
    missing = await mem_project_bind(project_root=str(root / "nope"), ctx=ctx)
    assert missing.startswith("Error: project_root does not exist"), missing
    afile = root / "README.md"
    afile.write_text("x", encoding="utf-8")
    not_dir = await mem_project_bind(project_root=str(afile), ctx=ctx)
    assert not_dir.startswith("Error: project_root is not a directory"), not_dir
    assert app.bound_project_root is None


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"project_root": 5}, {"project_root": None}, {}])
async def test_mem_do_raw_params_are_refused(bound_setup, params) -> None:
    app, ctx, _root, _tier = bound_setup
    out = await mem_do(action="project_bind", params=params, ctx=ctx)
    assert out.startswith("Error"), out
    assert app.bound_project_root is None


@pytest.mark.asyncio
async def test_mem_do_routes_a_valid_bind(bound_setup) -> None:
    app, ctx, root, _tier = bound_setup
    out = await mem_do(action="project_bind", params={"project_root": str(root)}, ctx=ctx)
    assert out.startswith("Bound project root"), out
    assert app.bound_project_root == root


# ---------------------------------------------------------------------------
# Unregistration after binding: reads stay pinned, writes are refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unregistering_keeps_reads_on_the_bound_root(bound_setup) -> None:
    app, ctx, root, _tier = bound_setup
    await mem_project_bind(project_root=str(root), ctx=ctx)
    app.config.indexing.project_memory_dirs = []

    # Not None: under an explicit scope filter that reads every project's rows.
    assert _resolve_project_context_root(app) == root
    out = await mem_add(content="after unregistration", scope="project_local", ctx=ctx)
    assert out.startswith("Error"), out

    status = await mem_status(ctx=ctx)
    assert "bound_project_unregistered" in status, status
    assert f"bound project root {root} has no registered project tier" in status, status


@pytest.mark.asyncio
async def test_unregistering_a_nested_tier_does_not_fall_back_to_its_parent(
    bm25_only_components, tmp_path: Path, stdio, plugin_cwd
) -> None:
    comp, _ = bm25_only_components
    parent_root, parent_tier = _make_project(tmp_path, "parent")
    child_root, child_tier = _make_project(parent_root, "child")
    comp.config.indexing.project_memory_dirs = [parent_tier, child_tier]
    app = AppContext.from_components(comp)
    out = await mem_project_bind(project_root=str(child_root), ctx=StubCtx(app))
    assert out.startswith(f"Bound project root: {child_root}"), out

    comp.config.indexing.project_memory_dirs = [parent_tier]
    assert _resolve_project_context_root(app) == child_root


@pytest.mark.asyncio
async def test_rebinding_after_unregistration_repeats_the_binding(bound_setup) -> None:
    app, ctx, root, _tier = bound_setup
    await mem_project_bind(project_root=str(root), ctx=ctx)
    app.config.indexing.project_memory_dirs = []
    # A workflow binds before every run; an unregistered tier must not turn
    # that into "no registered tier" while the binding is still in force.
    again = await mem_project_bind(project_root=str(root), ctx=ctx)
    assert again == f"Project root already bound: {root}"


@pytest.mark.asyncio
async def test_rebinding_a_nested_root_after_unregistration_does_not_name_the_parent(
    bm25_only_components, tmp_path: Path, stdio, plugin_cwd
) -> None:
    comp, _ = bm25_only_components
    parent_root, parent_tier = _make_project(tmp_path, "parent")
    child_root, child_tier = _make_project(parent_root, "child")
    comp.config.indexing.project_memory_dirs = [parent_tier, child_tier]
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    await mem_project_bind(project_root=str(child_root), ctx=ctx)
    comp.config.indexing.project_memory_dirs = [parent_tier]

    again = await mem_project_bind(project_root=str(child_root), ctx=ctx)
    assert again == f"Project root already bound: {child_root}"
    parent = await mem_project_bind(project_root=str(parent_root), ctx=ctx)
    assert parent.startswith(f"Error: this server is already bound to {child_root}"), parent
    assert app.bound_project_root == child_root


@pytest.mark.asyncio
async def test_a_deeper_registered_project_inside_the_bound_one_is_refused(
    bm25_only_components, tmp_path: Path, stdio, plugin_cwd
) -> None:
    comp, _ = bm25_only_components
    outer_root, outer_tier = _make_project(tmp_path, "outer")
    inner_root, inner_tier = _make_project(outer_root, "inner")
    comp.config.indexing.project_memory_dirs = [outer_tier, inner_tier]
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    await mem_project_bind(project_root=str(outer_root), ctx=ctx)

    out = await mem_project_bind(project_root=str(inner_root), ctx=ctx)
    assert out.startswith(f"Error: this server is already bound to {outer_root}"), out
    assert app.bound_project_root == outer_root


# ---------------------------------------------------------------------------
# Isolation: a bound server sees its project's rows and no other project's
# ---------------------------------------------------------------------------


async def _structured_ids(coro) -> set[str]:
    return {row["chunk_id"] for row in json.loads(await coro)["results"]}


@pytest.mark.asyncio
async def test_bound_reads_exclude_another_projects_rows_before_and_after_unregistration(
    bm25_only_components, tmp_path: Path, stdio, plugin_cwd
) -> None:
    comp, _ = bm25_only_components
    mine_root, mine_tier = _make_project(tmp_path, "mine")
    other_root, other_tier = _make_project(tmp_path, "other")
    comp.config.indexing.project_memory_dirs = [mine_tier, other_tier]

    # Two server contexts over one store, each bound to its own project.
    other_app = AppContext.from_components(comp)
    other_ctx = StubCtx(other_app)
    await mem_project_bind(project_root=str(other_root), ctx=other_ctx)
    added = await mem_add(
        content="shared keyword quartz from the other project",
        scope="project_local",
        ctx=other_ctx,
    )
    assert not added.startswith("Error"), added
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    await mem_project_bind(project_root=str(mine_root), ctx=ctx)
    added = await mem_add(
        content="shared keyword quartz from my project", scope="project_local", ctx=ctx
    )
    assert not added.startswith("Error"), added

    other_ids = await _structured_ids(
        mem_search(query="quartz", scope="project_local", output_format="structured", ctx=other_ctx)
    )
    assert other_ids

    async def _visible() -> tuple[set[str], set[str]]:
        found = await _structured_ids(
            mem_search(query="quartz", scope="project_local", output_format="structured", ctx=ctx)
        )
        recalled = await _structured_ids(
            mem_recall(scope="project_local", output_format="structured", ctx=ctx)
        )
        return found, recalled

    found, recalled = await _visible()
    assert found and recalled
    assert not found & other_ids
    assert not recalled & other_ids
    foreign = next(iter(other_ids))
    assert "not found" in (await mem_read(chunk_id=foreign, ctx=ctx)).lower()

    # Unregistering my tier must not widen reads: an unbound resolver would now
    # return no project, and an explicit scope filter would read every project.
    comp.config.indexing.project_memory_dirs = [other_tier]
    found, recalled = await _visible()
    assert not found & other_ids
    assert not recalled & other_ids
    assert "not found" in (await mem_read(chunk_id=foreign, ctx=ctx)).lower()


# ---------------------------------------------------------------------------
# Background services stay on the working directory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_watchdog_report_ignores_the_binding(tmp_path: Path, plugin_cwd) -> None:
    root, tier = _make_project(tmp_path, "proj")
    report = {
        "total_chunks": 0,
        "dead_memories_pct": 0,
        "access_coverage": {"pct": 0},
        "tag_coverage": {"pct": 0},
        "cross_references": 0,
    }
    storage = SimpleNamespace(get_health_report=AsyncMock(return_value=report))
    config = SimpleNamespace(indexing=SimpleNamespace(project_memory_dirs=[tier]))
    app = SimpleNamespace(storage=storage, config=config, bound_project_root=root)

    await check_full_health_report(app)  # type: ignore[arg-type]
    assert storage.get_health_report.await_args.kwargs == {"project_context_root": None}
