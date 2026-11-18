"""#2821: an explicit file target is gated for the tier it lands in.

``mem_add(file=...)`` classifies the resolved target and runs Gate B for the
tier it finds. ``mm add --file`` and web ``POST /api/add`` with ``file`` confined
the target to the base directory but judged Gate B on the requested scope alone,
so a ``project_shared`` tier registered inside the user base took a write made
with the default user scope, without the project_shared confirmation. Both now
refuse a target in a tier other than the requested one, as the day file is
refused (#2807).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from memtomem.context._atomic import memory_lock_path
from test_excluded_target_writes import (
    _cli,
    _patch_cli_components,
    _rows,
    _symlink_or_skip,
    _web_client,
)

pytestmark = pytest.mark.asyncio

SURFACES = ("cli", "web")


def _tier(comp, root: Path, leaf: str = "memories") -> Path:
    """Register a project tier at ``root/.memtomem/<leaf>``; return the tier dir."""
    tier = root / ".memtomem" / leaf
    tier.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [
        *comp.config.indexing.project_memory_dirs,
        tier,
    ]
    return tier


def _mm_add(file: str, *, scope: str, confirm: bool):
    from memtomem.cli.memory import _add

    return _add(
        "NEW ENTRY",
        None,
        [],
        file,
        False,
        scope,
        confirm,
        True,
        namespace=None,
        as_json=False,
        allow_namespace_mix=False,
    )


async def _add_file(
    surface: str,
    comp,
    monkeypatch,
    file: str,
    *,
    scope: str = "user",
    confirm: bool = False,
):
    """Return ``None`` on success, else the refusal text."""
    if surface == "cli":
        _patch_cli_components(monkeypatch, comp)
        return await _cli(_mm_add(file, scope=scope, confirm=confirm))
    async with _web_client(comp) as client:
        resp = await client.post(
            "/api/add",
            json={
                "content": "NEW ENTRY",
                "file": file,
                "scope": scope,
                "confirm_project_shared": confirm,
            },
        )
    if resp.status_code == 200:
        return None
    assert resp.status_code == 422, resp.text
    return resp.json()["detail"]


@pytest.mark.parametrize("confirm", [False, True], ids=["unconfirmed", "confirmed"])
@pytest.mark.parametrize("surface", SURFACES)
async def test_a_file_in_a_nested_shared_tier_is_refused(
    bm25_only_components, monkeypatch, surface, confirm
):
    """Refused with or without the confirmation: the rule is the tier, not consent."""
    comp, mem_dir = bm25_only_components
    tier = _tier(comp, mem_dir / "proj")
    target = tier / "notes.md"
    rows_before = _rows(comp)

    result = await _add_file(
        surface, comp, monkeypatch, "proj/.memtomem/memories/notes.md", confirm=confirm
    )

    assert result is not None and "nothing was written" in result, result
    assert "project_shared memory tier, not the user one" in result
    assert not target.exists()
    assert not memory_lock_path(target).exists()
    assert _rows(comp) == rows_before


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_file_in_a_nested_local_tier_is_refused(bm25_only_components, monkeypatch, surface):
    comp, mem_dir = bm25_only_components
    tier = _tier(comp, mem_dir / "proj", "memories.local")

    result = await _add_file(surface, comp, monkeypatch, "proj/.memtomem/memories.local/notes.md")

    assert result is not None and "project_local memory tier, not the user one" in result
    assert not (tier / "notes.md").exists()


@pytest.mark.parametrize("surface", SURFACES)
async def test_the_refusal_names_the_surfaces_own_remedy(
    bm25_only_components, monkeypatch, surface
):
    comp, mem_dir = bm25_only_components
    _tier(comp, mem_dir / "proj")

    result = await _add_file(surface, comp, monkeypatch, "proj/.memtomem/memories/notes.md")

    expected = {
        "cli": "`mm add --scope project_shared --confirm-project-shared`",
        "web": "scope='project_shared' and confirm_project_shared=true",
    }[surface]
    assert expected in result, result


async def test_a_link_into_a_nested_tier_is_refused_on_the_cli(bm25_only_components, monkeypatch):
    """The CLI follows links, so the link's destination is what is judged."""
    comp, mem_dir = bm25_only_components
    tier = _tier(comp, mem_dir / "proj")
    note = tier / "notes.md"
    note.write_text("# Shared\n", encoding="utf-8")
    _symlink_or_skip(mem_dir / "alias.md", note)

    result = await _add_file("cli", comp, monkeypatch, "alias.md")

    assert result is not None and "project_shared memory tier" in result, result
    assert note.read_text(encoding="utf-8") == "# Shared\n"


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="ntpath.expanduser guesses ~user as a sibling of the current profile, so the root "
    "resolves and there is no unknown tier to refuse",
)
async def test_an_unresolvable_configured_root_refuses(bm25_only_components, monkeypatch):
    """A root whose ``~user`` names no account leaves the tier unknown: refuse."""
    comp, mem_dir = bm25_only_components
    comp.config.indexing.project_memory_dirs = [Path("~no_such_user_2821/.memtomem/memories")]

    result = await _add_file("cli", comp, monkeypatch, "notes/x.md")

    assert result is not None and "Could not tell which memory tier" in result, result
    assert not (mem_dir / "notes" / "x.md").exists()


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_file_elsewhere_in_the_user_base_still_writes(
    bm25_only_components, monkeypatch, surface
):
    comp, mem_dir = bm25_only_components
    _tier(comp, mem_dir / "proj")
    target = mem_dir / "notes" / "x.md"

    result = await _add_file(surface, comp, monkeypatch, "notes/x.md")

    assert result is None, result
    assert "NEW ENTRY" in target.read_text(encoding="utf-8")
    scopes = {c.metadata.scope for c in await comp.storage.list_chunks_by_source(target)}
    assert scopes == {"user"}


@pytest.mark.parametrize("scope", ["project_shared", "project_local"])
@pytest.mark.parametrize("surface", SURFACES)
async def test_a_file_in_the_requested_project_tier_still_writes(
    bm25_only_components, tmp_path, monkeypatch, surface, scope
):
    """The control for each project scope: a file inside that scope's own tier."""
    comp, _mem_dir = bm25_only_components
    project = tmp_path / "project"
    tier = _tier(comp, project, "memories" if scope == "project_shared" else "memories.local")
    target = tier / "notes.md"

    if surface == "cli":
        _patch_cli_components(monkeypatch, comp)
        # That pins the project root to ``None``; point it at this project, as
        # ``mm`` run from inside it would find it.
        monkeypatch.setattr(
            "memtomem.runtime.project_context._resolve_project_context_root",
            lambda comp: project,
        )
        result = await _cli(_mm_add("notes.md", scope=scope, confirm=True))
    else:
        monkeypatch.setattr(
            "memtomem.runtime.project_context._resolve_project_context_from_dirs",
            lambda dirs: project,
        )
        async with _web_client(comp) as client:
            resp = await client.post(
                "/api/add",
                json={
                    "content": "NEW ENTRY",
                    "file": "notes.md",
                    "scope": scope,
                    "confirm_project_shared": True,
                },
            )
        assert resp.status_code == 200, resp.text
        result = None

    assert result is None, result
    assert "NEW ENTRY" in target.read_text(encoding="utf-8")
    scopes = {c.metadata.scope for c in await comp.storage.list_chunks_by_source(target)}
    assert scopes == {scope}


async def test_mcp_still_infers_the_tier_and_gates_on_it(bm25_only_components):
    """MCP keeps its own rule: refused without the confirmation, written with it."""
    from helpers import StubCtx
    from memtomem.server.context import AppContext
    from memtomem.server.tools import memory_crud

    comp, mem_dir = bm25_only_components
    tier = _tier(comp, mem_dir / "proj")
    ctx = StubCtx(AppContext.from_components(comp))
    file = "proj/.memtomem/memories/notes.md"

    refused = await memory_crud.mem_add("NEW ENTRY", file=file, idempotency_key="k1", ctx=ctx)
    assert refused.startswith("Error: scope='project_shared'"), refused
    assert not (tier / "notes.md").exists()

    written = await memory_crud.mem_add(
        "NEW ENTRY", file=file, confirm_project_shared=True, idempotency_key="k2", ctx=ctx
    )
    assert not written.startswith("Error"), written
    assert "NEW ENTRY" in (tier / "notes.md").read_text(encoding="utf-8")


@pytest.mark.xfail(
    strict=True,
    reason="#3268: a shared tier nested in a local one classifies as project_local",
)
async def test_a_shared_tier_nested_in_a_local_tier_is_judged_as_shared(bm25_only_components):
    from memtomem.integrations.langgraph import resolve_target_scope

    comp, mem_dir = bm25_only_components
    local = _tier(comp, mem_dir / "proj", "memories.local")
    shared = _tier(comp, local / "child")

    tier = resolve_target_scope(
        shared / "notes.md",
        comp.config.indexing.memory_dirs,
        comp.config.indexing.project_memory_dirs,
    )

    assert tier == "project_shared"
