"""The client's working directory belongs to the connection, not to the process.

A server that serves several clients cannot use its own working directory for
any of them. ``AppContext.client_cwd`` carries one client's directory, and
every server handler that depended on the process working directory reads it
when it is set. Nothing sets it outside these tests yet, and unset is the code
path that ran before the field existed; the tests of each tool that predate it
are the regression suite for that half.

Every test here puts the process in one directory and the client in another,
with a decoy of the same name on the process side wherever a name is resolved.
A handler that still reads the process working directory finds the decoy.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from types import SimpleNamespace

import pytest

from helpers import StubCtx
from memtomem.config import Mem2MemConfig
from memtomem.models import Chunk, ChunkMetadata
from memtomem.runtime.project_context import _resolve_project_context_root
from memtomem.server import context as server_context
from memtomem.server.client_paths import anchor_client_path, client_cwd_of, resolve_client_path
from memtomem.server.context import AppContext, _bind_active_app
from memtomem.server.resources import chunk_resource, tags_resource
from memtomem.server.tools.context import mem_context_detect, mem_context_diff
from memtomem.server.tools.dedup_decay import mem_cleanup_orphans
from memtomem.server.tools.export_import import mem_import
from memtomem.server.tools.importers import mem_import_notion, mem_import_obsidian
from memtomem.server.tools.indexing import mem_index
from memtomem.server.tools.ingest import mem_ingest
from memtomem.server.tools.memory_crud import mem_add
from memtomem.server.tools.project_bind import mem_project_bind
from memtomem.server.tools.recall import mem_recall
from memtomem.server.tools.status_config import collect_status_report
from memtomem.storage.sqlite_helpers import join_path_base, norm_path
from memtomem.tools.bundle_format import BundleValidationError, validate_root_map
from memtomem.tools.export_import import ExportBundle, _chunk_to_dict
from test_import_provenance_gate import _record


def _make_project(base: Path, name: str) -> tuple[Path, Path]:
    root = base / name
    (root / ".git").mkdir(parents=True)
    tier = root / ".memtomem" / "memories.local"
    tier.mkdir(parents=True)
    return root.resolve(), tier.resolve()


@pytest.fixture
def process_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The directory the server process runs in; no client is here."""
    cwd = tmp_path / "process"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    return cwd.resolve()


@pytest.fixture
def client(tmp_path: Path) -> Path:
    """One client's working directory, resolved as the field's contract requires."""
    cwd = tmp_path / "client"
    cwd.mkdir()
    return cwd.resolve()


@pytest.fixture
def served(bm25_only_components, process_cwd: Path, client: Path):
    """A real BM25-only context whose connection names ``client`` as its directory."""
    comp, mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    app.client_cwd = client
    return app, StubCtx(app), mem_dir.resolve()


def _bundle(path: Path, records: list[dict]) -> Path:
    path.write_text(json.dumps({"version": "3", "chunks": records}), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The two helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("app", [None, SimpleNamespace(), SimpleNamespace(client_cwd=None)])
def test_unset_is_the_expression_the_call_sites_ran(
    app: object, process_cwd: Path, tmp_path: Path
) -> None:
    for raw in ("rel/note.md", ".", "../up", "~/in-home", str(tmp_path / "absolute")):
        assert anchor_client_path(app, raw) == Path(raw).expanduser()  # type: ignore[arg-type]
        assert resolve_client_path(app, raw) == Path(raw).expanduser().resolve()  # type: ignore[arg-type]
    assert resolve_client_path(app, "rel") == process_cwd / "rel"  # type: ignore[arg-type]


def test_a_relative_argument_starts_at_the_client(
    process_cwd: Path, client: Path, tmp_path: Path
) -> None:
    app = SimpleNamespace(client_cwd=client)
    assert client_cwd_of(app) == client
    assert anchor_client_path(app, "sub/note.md") == client / "sub" / "note.md"  # type: ignore[arg-type]
    assert resolve_client_path(app, "sub/note.md") == client / "sub" / "note.md"  # type: ignore[arg-type]
    assert resolve_client_path(app, ".") == client  # type: ignore[arg-type]
    assert resolve_client_path(app, "../sibling") == client.parent / "sibling"  # type: ignore[arg-type]
    # Not resolved by the anchoring helper: its caller resolves once, itself.
    assert anchor_client_path(app, "../sibling") == client / ".." / "sibling"  # type: ignore[arg-type]


def test_an_absolute_or_home_argument_ignores_the_client(client: Path, tmp_path: Path) -> None:
    app = SimpleNamespace(client_cwd=client)
    absolute = tmp_path / "elsewhere" / "note.md"
    assert resolve_client_path(app, str(absolute)) == absolute.resolve()  # type: ignore[arg-type]
    assert resolve_client_path(app, "~/note.md") == Path("~/note.md").expanduser().resolve()  # type: ignore[arg-type]


def test_join_handles_the_windows_shapes_that_are_not_plainly_relative() -> None:
    base = PureWindowsPath(r"D:\client\sub")
    # Rooted, no drive: the root of the client's drive.
    assert join_path_base(base, PureWindowsPath(r"\foo"), r"\foo") == PureWindowsPath(r"D:\foo")
    # Drive-relative on the client's drive, whatever the letter's case.
    assert join_path_base(base, PureWindowsPath("D:foo"), "D:foo") == PureWindowsPath(
        r"D:\client\sub\foo"
    )
    assert join_path_base(base, PureWindowsPath("d:foo"), "d:foo") == PureWindowsPath(
        r"D:\client\sub\foo"
    )
    assert join_path_base(base, PureWindowsPath("foo"), "foo") == PureWindowsPath(
        r"D:\client\sub\foo"
    )


def test_join_refuses_a_drive_relative_path_on_another_drive() -> None:
    with pytest.raises(
        ValueError, match="'C:foo' is relative to the working directory of drive C:"
    ):
        join_path_base(PureWindowsPath(r"D:\client"), PureWindowsPath("C:foo"), "C:foo")


def test_join_on_posix_is_a_plain_join() -> None:
    assert join_path_base(PurePosixPath("/client"), PurePosixPath("a/b"), "a/b") == PurePosixPath(
        "/client/a/b"
    )


@pytest.mark.skipif(os.name != "nt", reason="drive-relative and rooted paths are Windows shapes")
def test_windows_shapes_through_the_helper(client: Path) -> None:
    app = SimpleNamespace(client_cwd=client)
    assert anchor_client_path(app, r"\foo") == Path(client.anchor) / "foo"  # type: ignore[arg-type]
    same_drive = f"{client.drive}foo"
    assert anchor_client_path(app, same_drive) == client / "foo"  # type: ignore[arg-type]
    other = "Z:" if client.drive.upper() != "Z:" else "Y:"
    with pytest.raises(ValueError, match="relative to the working directory of drive"):
        anchor_client_path(app, f"{other}foo")  # type: ignore[arg-type]


def test_two_connections_of_one_owner_resolve_the_same_argument_apart(tmp_path: Path) -> None:
    owner = AppContext(config=Mem2MemConfig())
    first = AppContext.from_runtime_owner(owner)
    second = AppContext.from_runtime_owner(owner)
    first.client_cwd = (tmp_path / "one").resolve()
    second.client_cwd = (tmp_path / "two").resolve()

    assert resolve_client_path(first, "notes") == first.client_cwd / "notes"
    assert resolve_client_path(second, "notes") == second.client_cwd / "notes"
    assert owner.client_cwd is None


def test_a_new_connection_does_not_inherit_the_owners_directory(tmp_path: Path) -> None:
    owner = AppContext(config=Mem2MemConfig())
    owner.client_cwd = tmp_path.resolve()
    assert AppContext.from_runtime_owner(owner).client_cwd is None


# ---------------------------------------------------------------------------
# Relative tool arguments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mem_index_path(served, process_cwd: Path) -> None:
    app, ctx, mem_dir = served
    app.client_cwd = mem_dir
    (mem_dir / "sub").mkdir()
    (mem_dir / "sub" / "note.md").write_text(
        "# Client\n\nthe client's own note\n", encoding="utf-8"
    )
    (process_cwd / "sub").mkdir()
    (process_cwd / "sub" / "note.md").write_text("# Decoy\n\ndecoy note\n", encoding="utf-8")

    out = await mem_index(path="sub", ctx=ctx)

    assert "- Indexed: 1" in out, out
    rows = await app.storage.recall_chunks(limit=10)
    assert [row.metadata.source_file for row in rows] == [mem_dir / "sub" / "note.md"]


@pytest.mark.asyncio
async def test_mem_ingest_source_without_opening_the_store(process_cwd: Path, client: Path) -> None:
    (client / "memory").mkdir()
    (client / "memory" / "client_fact.md").write_text("client fact\n", encoding="utf-8")
    (process_cwd / "memory").mkdir()
    (process_cwd / "memory" / "decoy_fact.md").write_text("decoy fact\n", encoding="utf-8")
    app = AppContext(config=Mem2MemConfig())
    app.client_cwd = client

    out = await mem_ingest(source="memory", dry_run=True, ctx=StubCtx(app))

    assert "client_fact.md" in out and "decoy_fact.md" not in out, out
    assert app._components is None

    missing = await mem_ingest(source="absent", dry_run=True, ctx=StubCtx(app))
    assert missing == f"Error: path not found: {client / 'absent'}"
    assert app._components is None


@pytest.mark.asyncio
async def test_mem_ingest_without_a_context_reads_the_process_directory(process_cwd: Path) -> None:
    (process_cwd / "memory").mkdir()
    (process_cwd / "memory" / "process_fact.md").write_text("process fact\n", encoding="utf-8")

    out = await mem_ingest(source="memory", dry_run=True)

    assert "process_fact.md" in out, out


@pytest.mark.asyncio
async def test_mem_import_notion_path(served, process_cwd: Path, client: Path) -> None:
    app, ctx, mem_dir = served
    (client / "export").mkdir()
    (client / "export" / "Client page.md").write_text("# Client page\n\nfrom the client\n")
    (process_cwd / "export").mkdir()
    (process_cwd / "export" / "Decoy page.md").write_text("# Decoy page\n\nfrom the decoy\n")

    out = await mem_import_notion(path="export", ctx=ctx)

    assert not out.startswith("Error"), out
    written = sorted(p.name for p in (mem_dir / "_imported" / "notion").rglob("*.md"))
    assert written == ["Client page.md"], (out, written)


@pytest.mark.asyncio
async def test_mem_import_obsidian_vault_path(served, process_cwd: Path, client: Path) -> None:
    app, ctx, mem_dir = served
    (client / "vault").mkdir()
    (client / "vault" / "Client note.md").write_text("# Client note\n\nfrom the client\n")
    (process_cwd / "vault").mkdir()
    (process_cwd / "vault" / "Decoy note.md").write_text("# Decoy note\n\nfrom the decoy\n")

    out = await mem_import_obsidian(vault_path="vault", ctx=ctx)

    assert not out.startswith("Error"), out
    written = sorted(p.name for p in (mem_dir / "_imported" / "obsidian").rglob("*.md"))
    assert written == ["Client note.md"], (out, written)


@pytest.mark.asyncio
async def test_mem_import_input_file(
    served, process_cwd: Path, client: Path, tmp_path: Path
) -> None:
    app, ctx, _mem_dir = served
    source = str(tmp_path / "notes.md")
    _bundle(client / "bundle.json", [_record("the client's bundle", source=source)])
    _bundle(process_cwd / "bundle.json", [_record("the decoy bundle", source=source)])

    out = await mem_import(input_file="bundle.json", ctx=ctx)

    assert "- Imported (new):   1" in out, out
    rows = await app.storage.recall_chunks(limit=10)
    assert [row.content for row in rows] == ["the client's bundle"]


@pytest.mark.asyncio
async def test_mem_import_missing_input_file_names_the_client_path(served, client: Path) -> None:
    _app, ctx, _mem_dir = served
    out = await mem_import(input_file="absent.json", ctx=ctx)
    assert out == f"File not found: {client / 'absent.json'}"


@pytest.mark.asyncio
async def test_mem_import_relative_source_file_in_a_bundle(
    served, process_cwd: Path, client: Path
) -> None:
    app, ctx, _mem_dir = served
    bundle = _bundle(client / "bundle.json", [_record("relative source", source="rel-note.md")])

    out = await mem_import(input_file=str(bundle), ctx=ctx)

    assert "- Imported (new):   1" in out, out
    rows = await app.storage.recall_chunks(limit=10)
    assert [row.metadata.source_file for row in rows] == [Path(norm_path(client / "rel-note.md"))]


@pytest.mark.asyncio
async def test_mem_import_relative_source_file_without_a_client_keeps_the_process_directory(
    served, process_cwd: Path, client: Path
) -> None:
    app, ctx, _mem_dir = served
    app.client_cwd = None
    bundle = _bundle(client / "bundle.json", [_record("relative source", source="rel-note.md")])

    out = await mem_import(input_file=str(bundle), ctx=ctx)

    assert "- Imported (new):   1" in out, out
    rows = await app.storage.recall_chunks(limit=10)
    assert [row.metadata.source_file for row in rows] == [
        Path(norm_path(process_cwd / "rel-note.md"))
    ]


@pytest.mark.asyncio
async def test_mem_import_project_root_map_value(
    served, process_cwd: Path, client: Path, tmp_path: Path
) -> None:
    """A relative destination root names the client's project, not a process-side path."""
    app, ctx, _mem_dir = served
    destination, tier = _make_project(client, "proj")
    app.config.indexing.project_memory_dirs = [tier]
    old = tmp_path / "old"
    chunk = Chunk(
        "a project note that moves",
        ChunkMetadata(
            source_file=old / ".memtomem" / "memories.local" / "note.md",
            scope="project_local",
            project_root=old,
        ),
    )
    bundle = client / "v4.json"
    bundle.write_text(ExportBundle(chunks=[_chunk_to_dict(chunk)], total_chunks=1).to_json())

    out = await mem_import(input_file=str(bundle), project_root_map={str(old): "proj"}, ctx=ctx)

    assert "- Imported (new):   1" in out, out
    rows = await app.storage.recall_chunks(project_context_root=destination)
    assert [row.metadata.source_file for row in rows] == [tier / "note.md"]
    assert rows[0].metadata.project_root == destination


_BAD_ROOT_MAPS = [
    pytest.param({"/old": "new\x00root"}, id="nul-in-value"),
    pytest.param({"relative-key": "~nouser-3381/x"}, id="bad-key-with-unknown-user"),
    pytest.param({"/old": 7}, id="non-string-value"),
    pytest.param({"/old": ""}, id="empty-value"),
    pytest.param({"relative-key": "fine", "/old": ""}, id="bad-key-then-bad-value"),
    pytest.param({"/old": "", "relative-key": "fine"}, id="bad-value-then-bad-key"),
    pytest.param([], id="not-a-mapping"),
]


@pytest.mark.parametrize("value", _BAD_ROOT_MAPS)
def test_root_map_errors_do_not_depend_on_the_base(value: object, client: Path) -> None:
    with pytest.raises(BundleValidationError) as without_base:
        validate_root_map(value)
    with pytest.raises(BundleValidationError) as with_base:
        validate_root_map(value, client)
    assert str(with_base.value) == str(without_base.value)


def test_root_map_destination_with_and_without_a_base(process_cwd: Path, client: Path) -> None:
    key = PurePosixPath("/old") if os.name != "nt" else PureWindowsPath(r"C:\old")
    raw = {str(key): "dest"}
    assert validate_root_map(raw) == {key: process_cwd / "dest"}
    assert validate_root_map(raw, client) == {key: client / "dest"}
    absolute = {str(key): str(process_cwd / "abs")}
    assert validate_root_map(absolute, client) == {key: process_cwd / "abs"}


@pytest.mark.asyncio
async def test_mem_cleanup_orphans_under(
    served, process_cwd: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, ctx, mem_dir = served
    theirs, others = mem_dir / "client-side", mem_dir / "process-side"
    for directory, word in ((theirs, "clientword"), (others, "processword")):
        directory.mkdir()
        (directory / "note.md").write_text(f"# Note\n\n{word}\n", encoding="utf-8")
    app.client_cwd = mem_dir
    assert "Error" not in await mem_index(path=".", ctx=ctx)
    (theirs / "note.md").unlink()
    (others / "note.md").unlink()
    app.client_cwd = theirs
    monkeypatch.chdir(others)

    preview = await mem_cleanup_orphans(under=".", ctx=ctx)

    assert "Orphaned files: 1 (dry-run" in preview, preview
    assert str(theirs / "note.md") in preview and str(others / "note.md") not in preview
    assert "1 more orphaned file(s) are outside the given directories." in preview

    purged = await mem_cleanup_orphans(dry_run=False, confirm_purge=True, under=".", ctx=ctx)

    assert not purged.startswith("Error"), purged
    rows = await app.storage.recall_chunks(limit=10)
    assert [row.metadata.source_file for row in rows] == [others / "note.md"]


@pytest.mark.asyncio
async def test_mem_cleanup_orphans_under_is_anchored_and_left_unresolved(
    served, client: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``group_orphans`` resolves each entry once, its own way; the tool must not first."""
    import memtomem.server.tools.dedup_decay as dedup_decay

    _app, ctx, _mem_dir = served
    seen: list[list[str]] = []
    real = dedup_decay.group_orphans

    def spy(candidates, roots, *, under):
        seen.append(list(under))
        return real(candidates, roots, under=under)

    monkeypatch.setattr(dedup_decay, "group_orphans", spy)

    await mem_cleanup_orphans(under=["../sibling", str(client / "absolute")], ctx=ctx)

    assert seen == [[str(client / ".." / "sibling"), str(client / "absolute")]]


@pytest.mark.asyncio
async def test_mem_cleanup_orphans_under_without_a_client_is_passed_as_given(
    served, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memtomem.server.tools.dedup_decay as dedup_decay

    app, ctx, _mem_dir = served
    app.client_cwd = None
    seen: list[list[str]] = []
    real = dedup_decay.group_orphans

    def spy(candidates, roots, *, under):
        seen.append(list(under))
        return real(candidates, roots, under=under)

    monkeypatch.setattr(dedup_decay, "group_orphans", spy)

    await mem_cleanup_orphans(under=["../sibling", "~/in-home"], ctx=ctx)

    assert seen == [["../sibling", "~/in-home"]]


# ---------------------------------------------------------------------------
# Project context
# ---------------------------------------------------------------------------


@pytest.fixture
def two_projects(bm25_only_components, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Registered projects A and B; the process runs in B."""
    comp, _mem_dir = bm25_only_components
    root_a, tier_a = _make_project(tmp_path, "project-a")
    root_b, tier_b = _make_project(tmp_path, "project-b")
    comp.config.indexing.project_memory_dirs = [tier_a, tier_b]
    monkeypatch.chdir(root_b)
    app = AppContext.from_components(comp)
    return app, StubCtx(app), (root_a, tier_a), (root_b, tier_b)


async def _local_contents(ctx: StubCtx) -> list[str]:
    recalled = json.loads(
        await mem_recall(scope="project_local", output_format="structured", ctx=ctx)
    )
    # A note written by ``mem_add`` carries a ``> created:`` line above its text.
    return sorted(row["content"].rsplit("\n", 1)[-1] for row in recalled["results"])


@pytest.mark.asyncio
async def test_project_scoped_reads_and_writes_follow_the_client(two_projects) -> None:
    app, ctx, (root_a, tier_a), (root_b, tier_b) = two_projects

    assert _resolve_project_context_root(app) == root_b
    added = await mem_add(content="written from project b", scope="project_local", ctx=ctx)
    assert not added.startswith("Error"), added

    app.client_cwd = root_a
    assert _resolve_project_context_root(app) == root_a
    added = await mem_add(content="written from project a", scope="project_local", ctx=ctx)
    assert not added.startswith("Error"), added

    assert [p.parent for p in tier_a.rglob("*.md")] == [tier_a]
    assert await _local_contents(ctx) == ["written from project a"]

    app.client_cwd = None
    assert await _local_contents(ctx) == ["written from project b"]


@pytest.mark.asyncio
async def test_a_client_in_a_subdirectory_is_in_its_project(two_projects) -> None:
    app, _ctx, (root_a, _tier_a), _b = two_projects
    (root_a / "src" / "pkg").mkdir(parents=True)
    app.client_cwd = root_a / "src" / "pkg"
    assert _resolve_project_context_root(app) == root_a


@pytest.mark.asyncio
async def test_a_client_outside_every_project_has_none(two_projects, tmp_path: Path) -> None:
    app, _ctx, _a, _b = two_projects
    app.client_cwd = tmp_path.resolve()
    assert _resolve_project_context_root(app) is None


@pytest.mark.asyncio
async def test_a_binding_still_wins_over_the_client_directory(two_projects) -> None:
    app, _ctx, (root_a, _tier_a), (root_b, _tier_b) = two_projects
    app.client_cwd = root_a
    app.bound_project_root = root_b
    assert _resolve_project_context_root(app) == root_b


@pytest.mark.asyncio
async def test_mem_project_bind_guard_reads_the_client_directory(
    two_projects, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app, ctx, (root_a, _tier_a), (root_b, _tier_b) = two_projects
    monkeypatch.setattr(server_context, "_server_transport", "stdio")

    # The process is in B and the client in A: binding B is refused for A.
    app.client_cwd = root_a
    refused = await mem_project_bind(project_root=str(root_b), ctx=ctx)
    assert refused == (
        f"Error: this server's working directory is already in project {root_a}; "
        f"start the server outside it to bind {root_b}."
    )
    assert app.bound_project_root is None

    # The client is outside every project: the process being in B no longer
    # refuses binding A.
    app.client_cwd = tmp_path.resolve()
    bound = await mem_project_bind(project_root=str(root_a), ctx=ctx)
    assert bound.startswith(f"Bound project root: {root_a}"), bound


@pytest.mark.asyncio
async def test_resources_take_their_boundary_from_the_client(two_projects) -> None:
    app, ctx, (root_a, _tier_a), _b = two_projects
    assert not (
        await mem_add(content="b side", scope="project_local", tags=["tag-b"], ctx=ctx)
    ).startswith("Error")
    app.client_cwd = root_a
    assert not (
        await mem_add(content="a side", scope="project_local", tags=["tag-a"], ctx=ctx)
    ).startswith("Error")
    ids = {
        row.content: str(row.id)
        for root in (root_a, _b[0])
        for row in await app.storage.recall_chunks(project_context_root=root)
        if row.metadata.scope == "project_local"
    }

    async def read() -> tuple[set[str], dict, dict]:
        with _bind_active_app(app):
            tags = {entry["tag"] for entry in json.loads(await tags_resource())}
            a_side = json.loads(await chunk_resource(ids["a side"]))
            b_side = json.loads(await chunk_resource(ids["b side"]))
        return tags, a_side, b_side

    tags, a_side, b_side = await read()
    assert "tag-a" in tags and "tag-b" not in tags
    assert a_side.get("content") == "a side" and "error" in b_side

    app.client_cwd = None
    tags, a_side, b_side = await read()
    assert "tag-b" in tags and "tag-a" not in tags
    assert b_side.get("content") == "b side" and "error" in a_side


@pytest.mark.asyncio
async def test_mem_status_reports_the_client_directory(served, client: Path) -> None:
    app, _ctx, _mem_dir = served
    assert (await collect_status_report(app))["runtime"]["cwd"] == str(client)
    app.client_cwd = None
    assert (await collect_status_report(app))["runtime"]["cwd"] == str(Path(os.getcwd()).resolve())


# ---------------------------------------------------------------------------
# The context tools: the client's project, and no store
# ---------------------------------------------------------------------------


@pytest.fixture
def client_project(process_cwd: Path, client: Path) -> tuple[AppContext, Path]:
    """A client inside a repository that has agent files; the process side has none."""
    (client / ".git").mkdir()
    (client / "CLAUDE.md").write_text("# Project memory\n", encoding="utf-8")
    (client / ".memtomem").mkdir()
    (client / ".memtomem" / "context.md").write_text("# Project memory\n", encoding="utf-8")
    (client / "src").mkdir()
    app = AppContext(config=Mem2MemConfig())
    app.client_cwd = client / "src"
    return app, client


@pytest.mark.asyncio
async def test_mem_context_detect_walks_up_from_the_client(client_project) -> None:
    app, _root = client_project

    out = await mem_context_detect(ctx=StubCtx(app))

    assert "claude: CLAUDE.md" in out, out
    assert app._components is None

    app.client_cwd = None
    assert await mem_context_detect(ctx=StubCtx(app)) == "No agent configuration files found."
    assert app._components is None


@pytest.mark.asyncio
async def test_mem_context_diff_walks_up_from_the_client(client_project) -> None:
    app, _root = client_project

    out = await mem_context_diff(ctx=StubCtx(app))

    assert "claude: CLAUDE.md [" in out, out
    assert app._components is None

    app.client_cwd = None
    assert await mem_context_diff(ctx=StubCtx(app)) == ".memtomem/context.md not found."


@pytest.mark.asyncio
async def test_the_context_tools_still_run_without_a_context(process_cwd: Path) -> None:
    assert await mem_context_detect() == "No agent configuration files found."
    assert await mem_context_diff() == ".memtomem/context.md not found."
