"""``mem_project_bind`` — a stdio server started outside the project (#2920).

The Antigravity CLI starts a plugin's MCP server in the plugin's own
directory, and the project context comes from the working directory, so
``project_local`` never resolves there. These tests run every tool from such
a directory (``plugin_cwd``) against a real BM25-only store.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from helpers import StubCtx
from memtomem._runtime_paths import _hint_quote
from memtomem.runtime.project_context import _resolve_project_context_root
from memtomem.server import context as server_context
from memtomem.server.context import AppContext
from memtomem.server.health_checks import check_full_health_report
from memtomem.server.tools.memory_crud import mem_add, mem_delete, mem_edit
from memtomem.server.tools.browse import mem_read
from memtomem.server.tools.meta import mem_do
from memtomem.server.tools.project_bind import mem_project_bind
from memtomem.server.tools.recall import mem_recall
from memtomem.server.tools.search import mem_search
from memtomem.server.tools.status_config import mem_status


_REPO = Path(__file__).resolve().parents[3]


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
async def test_a_server_inside_another_project_cannot_bind(
    bm25_only_components, tmp_path: Path, stdio, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Binding would move the boundary from the cwd's project to another one
    # under operations already running; only a move from no project is safe.
    comp, _ = bm25_only_components
    cwd_root, cwd_tier = _make_project(tmp_path, "cwd-proj")
    other_root, other_tier = _make_project(tmp_path, "other-proj")
    comp.config.indexing.project_memory_dirs = [cwd_tier, other_tier]
    monkeypatch.chdir(cwd_root)
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    out = await mem_project_bind(project_root=str(other_root), ctx=ctx)
    assert out.startswith(
        f"Error: this server's working directory is already in project {cwd_root}"
    )
    assert app.bound_project_root is None
    assert _resolve_project_context_root(app) == cwd_root

    same = await mem_project_bind(project_root=str(cwd_root), ctx=ctx)
    assert same.startswith(f"Bound project root: {cwd_root}"), same
    assert _resolve_project_context_root(app) == cwd_root


@pytest.mark.asyncio
async def test_a_bind_during_a_link_cannot_split_its_two_lookups(
    bm25_only_components, tmp_path: Path, stdio, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.server.tools import cross_ref

    comp, _ = bm25_only_components
    a_root, a_tier = _make_project(tmp_path, "a")
    b_root, b_tier = _make_project(tmp_path, "b")
    comp.config.indexing.project_memory_dirs = [a_tier, b_tier]

    async def _add_in(root: Path, content: str, word: str) -> str:
        monkeypatch.chdir(root)
        writer_ctx = StubCtx(AppContext.from_components(comp))
        await mem_add(content=content, scope="project_local", ctx=writer_ctx)
        return await _only_chunk_id(
            mem_search(
                query=word, scope="project_local", output_format="structured", ctx=writer_ctx
            )
        )

    b_id = await _add_in(b_root, "b private walrus", "walrus")
    a_id = await _add_in(a_root, "a private heron", "heron")
    app = AppContext.from_components(comp)  # cwd is in project A
    ctx = StubCtx(app)

    binds: list[str] = []
    real_resolve = cross_ref.resolve_chunk

    async def _resolve_then_bind(app_, chunk_id):
        chunk = await real_resolve(app_, chunk_id)
        if not binds:
            binds.append(await mem_project_bind(project_root=str(b_root), ctx=ctx))
        return chunk

    monkeypatch.setattr(cross_ref, "resolve_chunk", _resolve_then_bind)
    out = await cross_ref.mem_link(source_id=a_id, target_id=b_id, ctx=ctx)

    # The leak first: with the bind landing between the lookups, B's chunk
    # resolved under B and the link answered with its preview.
    assert "walrus" not in out, out
    assert "not found" in out.lower(), out
    assert binds and binds[0].startswith("Error: this server's working directory"), binds


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
# A rewrite must not re-index project memory as user memory
# ---------------------------------------------------------------------------


async def _only_chunk_id(coro) -> str:
    ids = await _structured_ids(coro)
    assert len(ids) == 1, ids
    return next(iter(ids))


@pytest.mark.asyncio
async def test_edit_and_delete_refuse_a_row_whose_bound_tier_was_unregistered(
    bm25_only_components, tmp_path: Path, stdio, plugin_cwd
) -> None:
    comp, _ = bm25_only_components
    mine_root, mine_tier = _make_project(tmp_path, "mine")
    other_root, other_tier = _make_project(tmp_path, "other")
    comp.config.indexing.project_memory_dirs = [mine_tier, other_tier]
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    await mem_project_bind(project_root=str(mine_root), ctx=ctx)
    await mem_add(content="private zephyr note", scope="project_local", ctx=ctx)
    chunk_id = await _only_chunk_id(
        mem_search(query="zephyr", scope="project_local", output_format="structured", ctx=ctx)
    )
    before = {path: path.read_bytes() for path in mine_tier.rglob("*.md")}

    comp.config.indexing.project_memory_dirs = [other_tier]
    edited = await mem_edit(chunk_id=chunk_id, new_content="private zephyr edited", ctx=ctx)
    assert edited.startswith(f"Error: chunk {chunk_id} rewrite refused: "), edited
    assert "would store it as user memory; nothing was written" in edited
    assert f"cd {_hint_quote(mine_root)} && mm mem init --scope project_local" in edited
    deleted = await mem_delete(chunk_id=chunk_id, ctx=ctx)
    assert deleted.startswith(f"Error: chunk {chunk_id} rewrite refused: "), deleted
    assert {path: path.read_bytes() for path in mine_tier.rglob("*.md")} == before

    other_app = AppContext.from_components(comp)
    other_ctx = StubCtx(other_app)
    await mem_project_bind(project_root=str(other_root), ctx=other_ctx)
    seen = json.loads(await mem_recall(output_format="structured", ctx=other_ctx))
    assert chunk_id not in {row["chunk_id"] for row in seen["results"]}


@pytest.mark.asyncio
async def test_edit_refuses_a_cwd_projects_row_whose_tier_was_unregistered(
    bm25_only_components, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No binding: the cwd's project keeps its shared tier, so the resolver still
    # finds it after the local tier is unregistered, and the local row resolves.
    comp, _ = bm25_only_components
    root, local_tier = _make_project(tmp_path, "proj")
    shared_tier = root / ".memtomem" / "memories"
    shared_tier.mkdir()
    comp.config.indexing.project_memory_dirs = [shared_tier, local_tier]
    monkeypatch.chdir(root)
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    await mem_add(content="local quokka note", scope="project_local", ctx=ctx)
    chunk_id = await _only_chunk_id(
        mem_search(query="quokka", scope="project_local", output_format="structured", ctx=ctx)
    )

    comp.config.indexing.project_memory_dirs = [shared_tier]
    assert _resolve_project_context_root(app) == root
    edited = await mem_edit(chunk_id=chunk_id, new_content="local quokka edited", ctx=ctx)
    assert edited.startswith(f"Error: chunk {chunk_id} rewrite refused: "), edited


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


# ---------------------------------------------------------------------------
# What the Antigravity workflows' bind step relies on (#2920)
# ---------------------------------------------------------------------------
#
# handoff, brief and update ship to Antigravity with a bind step that calls this
# tool through ``mem_do`` and continues only when the result's first line is one
# of two quoted forms naming exactly the live Git root.


def _bind_step_success_forms() -> tuple[str, str]:
    """The two first-line forms the workflow's bind step quotes."""
    source = (_REPO / "packages/memtomem-plugin-assets/workflows/handoff.md").read_text(
        encoding="utf-8"
    )
    block = source.split("<!-- scope:bound -->", 1)[1].split("<!-- /scope -->", 1)[0]
    forms = re.findall(r"`((?:Bound project root|Project root already bound): <root>)`", block)
    assert len(forms) == 2, forms
    return forms[0], forms[1]


@pytest.mark.asyncio
async def test_the_bind_step_success_forms_are_what_the_tool_prints(bound_setup) -> None:
    _app, ctx, root, _tier = bound_setup
    first_form, again_form = _bind_step_success_forms()

    first = await mem_do(action="project_bind", params={"project_root": str(root)}, ctx=ctx)
    again = await mem_do(action="project_bind", params={"project_root": str(root)}, ctx=ctx)

    assert first.splitlines()[0] == first_form.replace("<root>", str(root)), first
    assert again.splitlines()[0] == again_form.replace("<root>", str(root)), again


@pytest.mark.asyncio
async def test_a_repository_under_a_registered_parent_binds_the_parent(
    bm25_only_components, tmp_path: Path, stdio, plugin_cwd
) -> None:
    """The case the bind step's root-equality check exists for.

    With only a parent's tier registered, binding the repository succeeds and
    names the parent. A workflow that accepted any success would then write the
    repository's handoffs into the parent's tier, so it must compare the root.
    """
    comp, _mem_dir = bm25_only_components
    parent_root, parent_tier = _make_project(tmp_path, "workspace")
    repo = parent_root / "repo"
    (repo / ".git").mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [parent_tier]
    ctx = StubCtx(AppContext.from_components(comp))

    out = await mem_do(action="project_bind", params={"project_root": str(repo)}, ctx=ctx)

    assert out.splitlines()[0] == f"Bound project root: {parent_root}", out
    assert parent_root != repo.resolve()


@pytest.mark.asyncio
async def test_a_symlinked_repository_binds_the_path_git_reports(
    bm25_only_components, tmp_path: Path, stdio, plugin_cwd
) -> None:
    """The tool returns the resolved root, which is what Git prints as the top level."""
    if shutil.which("git") is None:
        pytest.skip("git is not installed")
    comp, _mem_dir = bm25_only_components
    root, tier = _make_project(tmp_path, "real")
    shutil.rmtree(root / ".git")
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(root)], env=env, check=True, timeout=60)
    link = tmp_path / "link"
    try:
        link.symlink_to(root, target_is_directory=True)
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink: {exc}")
    comp.config.indexing.project_memory_dirs = [tier]
    ctx = StubCtx(AppContext.from_components(comp))
    top = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        cwd=link,
        env=env,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()

    out = await mem_do(action="project_bind", params={"project_root": top}, ctx=ctx)

    assert out.splitlines()[0] == f"Bound project root: {root}", out
    assert Path(top) == root
