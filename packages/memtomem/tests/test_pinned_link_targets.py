"""A project-scope pinned write or delete is not carried through a link.

A project tier's directories live in the project's checkout, and a checkout
can deliver any of them as a symbolic link, for example
``pinned/general -> ~/.claude/commands``. Followed, such a link would have
``PinnedContextStore.set`` create or rewrite a file in the user's home and
``.delete`` remove one.

For a project scope, no entry from ``.memtomem`` down to the block file's
parent directory may be a link, a junction, or an entry that cannot be
inspected. The block file itself is not judged, the user tier is left as it
is, reads are unchanged, and an invalid id is reported before anything about
the scope.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import click
import pytest

from memtomem.config import Mem2MemConfig
from memtomem.pinned import PinnedContextStore

_TIER_DIR = {"project_shared": "memories", "project_local": "memories.local"}
_SCOPES = tuple(_TIER_DIR)
_REFUSAL = r"is a link \(or cannot be inspected\).*nothing was changed"

#: Where the link is planted, as (entry under ``<project>/.memtomem``'s parent,
#: what it points at under the outside directory or the project). ``{tier}`` is
#: the scope's directory name.
_LINK_ROWS = {
    "general": (".memtomem/{tier}/pinned/general", "outside:commands"),
    "agents": (".memtomem/{tier}/pinned/agents", "outside:skills"),
    "agent_dir": (".memtomem/{tier}/pinned/agents/planner", "outside:skills/planner"),
    "pinned": (".memtomem/{tier}/pinned", "outside:pinned"),
    "tier": (".memtomem/{tier}", "outside:tier"),
    "dot_memtomem": (".memtomem", "outside:dot"),
    "inside_tier": (".memtomem/{tier}/pinned/general", "project:.memtomem/{tier}/other"),
    "inside_project": (".memtomem/{tier}/pinned/general", "project:src"),
}
_AGENT_ROWS = {"agents", "agent_dir"}


def _symlink_or_skip(link: Path, target: Path, *, directory: bool = True) -> None:
    """Link ``link`` to ``target``. Windows needs to be told which kind of link it is."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows without the right
        pytest.skip(f"cannot create a symlink here: {exc}")


def _files(*roots: Path) -> dict[str, bytes]:
    """Every regular file under ``roots``, by absolute path, links not followed."""
    found: dict[str, bytes] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not path.is_symlink():
                found[str(path)] = path.read_bytes()
    return found


def _store(tmp_path: Path) -> tuple[PinnedContextStore, Path, Path]:
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [tmp_path / "user-memory"]
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    return PinnedContextStore(config, project_root=project), project, outside


def _plant(row: str, scope: str, project: Path, outside: Path) -> Path:
    """Build the link for ``row`` and a file a followed link would hit. Returns that file."""
    entry, to = (part.format(tier=_TIER_DIR[scope]) for part in _LINK_ROWS[row])
    where, _, rel = to.partition(":")
    target = (outside if where == "outside" else project) / rel
    # Where the block would land if the link were followed, given the rest of
    # the path below the link.
    below = {
        "general": "",
        "agents": "planner",
        "agent_dir": "",
        "pinned": "general",
        "tier": "pinned/general",
        "dot_memtomem": f"{_TIER_DIR[scope]}/pinned/general",
        "inside_tier": "",
        "inside_project": "",
    }[row]
    victim_dir = target / below
    victim_dir.mkdir(parents=True)
    victim = victim_dir / "victim.md"
    victim.write_bytes(b"THE USER'S OWN FILE\n")
    _symlink_or_skip(project / entry, target)
    return victim


def _kwargs(row: str, scope: str) -> dict[str, object]:
    kwargs: dict[str, object] = {"scope": scope}
    if scope == "project_shared":
        kwargs["confirm_project_shared"] = True
    if row in _AGENT_ROWS:
        kwargs["agent_id"] = "planner"
    return kwargs


@pytest.mark.parametrize("scope", _SCOPES)
@pytest.mark.parametrize("row", sorted(_LINK_ROWS))
@pytest.mark.parametrize("op", ["set", "delete"])
def test_a_linked_directory_refuses_the_mutation(tmp_path, op, row, scope):
    store, project, outside = _store(tmp_path)
    victim = _plant(row, scope, project, outside)
    before = _files(project, outside)
    assert str(victim) in before

    with pytest.raises(ValueError, match=_REFUSAL):
        if op == "set":
            store.set("victim", "PROBE", description="probe", **_kwargs(row, scope))
        else:
            store.delete("victim", **_kwargs(row, scope))

    assert _files(project, outside) == before


def _call(store: PinnedContextStore, op: str, scope: str) -> object:
    kwargs = _kwargs("general", scope)
    if op == "set":
        return store.set("rules", "PROBE", description="probe", **kwargs)
    return store.delete("rules", **kwargs)


@pytest.mark.parametrize("scope", _SCOPES)
@pytest.mark.parametrize(
    "entry", [".memtomem", ".memtomem/{tier}", ".memtomem/{tier}/pinned"], ids=str
)
@pytest.mark.parametrize("op", ["set", "delete"])
def test_a_link_that_loops_gets_the_same_refusal(tmp_path, op, entry, scope):
    """Asked before anything resolves the path, so a loop is not left to ``resolve()``.

    Up to Python 3.12 that raises ``RuntimeError`` with the absolute path.
    """
    store, project, _outside = _store(tmp_path)
    loop = project / entry.format(tier=_TIER_DIR[scope])
    loop.parent.mkdir(parents=True, exist_ok=True)
    try:
        loop.symlink_to(loop.name)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows without the right
        pytest.skip(f"cannot create a symlink here: {exc}")

    with pytest.raises(ValueError, match=_REFUSAL) as caught:
        _call(store, op, scope)

    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize("scope", _SCOPES)
@pytest.mark.parametrize("below", ["pinned", "pinned/general"])
@pytest.mark.parametrize("op", ["set", "delete"])
def test_a_junction_below_the_tier_refuses_the_mutation(tmp_path, monkeypatch, op, below, scope):
    """A junction redirects a directory as a symlink does. Injected off Windows."""
    store, project, _outside = _store(tmp_path)
    store.set("rules", "team rule", **_kwargs("general", scope))
    entry = project / ".memtomem" / _TIER_DIR[scope] / below
    before = _files(project)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "is_junction", lambda self: self == entry)
        with pytest.raises(ValueError, match=_REFUSAL) as caught:
            _call(store, op, scope)

    assert str(caught.value).startswith(f".memtomem/{_TIER_DIR[scope]}/{below} in this project")
    assert _files(project) == before


@pytest.mark.parametrize("scope", _SCOPES)
@pytest.mark.parametrize("below", ["pinned", "pinned/general"])
@pytest.mark.parametrize("op", ["set", "delete"])
def test_an_entry_that_cannot_be_inspected_refuses_the_mutation(
    tmp_path, monkeypatch, op, below, scope
):
    """``lstat`` fails for the entry: nobody can vouch for it, so it is not written through."""
    store, project, _outside = _store(tmp_path)
    store.set("rules", "team rule", **_kwargs("general", scope))
    entry = project / ".memtomem" / _TIER_DIR[scope] / below
    before = _files(project)
    real_lstat = Path.lstat

    def lstat(self):
        if self == entry:
            raise PermissionError(13, "Permission denied", str(self))
        return real_lstat(self)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "lstat", lstat)
        with pytest.raises(ValueError, match=_REFUSAL) as caught:
            _call(store, op, scope)

    assert str(tmp_path) not in str(caught.value)
    assert _files(project) == before


@pytest.mark.parametrize("scope", _SCOPES)
def test_the_refusal_names_the_linked_entry_by_its_project_path(tmp_path, scope):
    store, project, outside = _store(tmp_path)
    _plant("pinned", scope, project, outside)
    with pytest.raises(ValueError) as caught:
        store.set("victim", "PROBE", **_kwargs("pinned", scope))
    message = str(caught.value)
    assert message.startswith(f".memtomem/{_TIER_DIR[scope]}/pinned in this project is a link")
    assert str(tmp_path) not in message


@pytest.mark.parametrize("scope", _SCOPES)
def test_without_a_link_the_first_write_and_the_delete_work(tmp_path, scope):
    """No ``.memtomem`` exists yet: a missing entry is not a link."""
    store, project, _outside = _store(tmp_path)
    kwargs = _kwargs("general", scope)
    block = store.set("rules", "team rule", **kwargs)
    expected = project / ".memtomem" / _TIER_DIR[scope] / "pinned" / "general" / "rules.md"
    assert block.source_path == expected.resolve()
    assert "team rule" in expected.read_text(encoding="utf-8")
    agent = store.set("rules", "planner rule", agent_id="planner", **kwargs)
    assert agent.source_path.parent.name == "planner"
    assert store.delete("rules", **kwargs) is True
    assert not expected.exists()


@pytest.mark.parametrize("scope", _SCOPES)
def test_a_link_at_the_block_file_is_replaced_or_removed_not_followed(tmp_path, scope):
    store, project, outside = _store(tmp_path)
    target = outside / "settings.json"
    target.write_bytes(b'{"hooks": {}}\n')
    leaf = project / ".memtomem" / _TIER_DIR[scope] / "pinned" / "general" / "rules.md"
    kwargs = _kwargs("general", scope)

    _symlink_or_skip(leaf, target, directory=False)
    store.set("rules", "team rule", **kwargs)
    assert not leaf.is_symlink()
    assert "team rule" in leaf.read_text(encoding="utf-8")
    assert target.read_bytes() == b'{"hooks": {}}\n'

    leaf.unlink()
    _symlink_or_skip(leaf, target, directory=False)
    assert store.delete("rules", **kwargs) is True
    assert not leaf.is_symlink() and not leaf.exists()
    assert target.read_bytes() == b'{"hooks": {}}\n'


@pytest.mark.parametrize("scope", _SCOPES)
def test_get_still_reads_through_a_linked_directory(tmp_path, scope):
    """Only the two mutations are refused; a read answers with what the path holds."""
    store, project, outside = _store(tmp_path)
    shared = outside / "shared-general"
    shared.mkdir()
    (shared / "rules.md").write_text(
        "---\nid: rules\ndescription: ''\npriority: 0\nagent_id: null\n---\nlinked rule\n",
        encoding="utf-8",
    )
    _symlink_or_skip(project / ".memtomem" / _TIER_DIR[scope] / "pinned" / "general", shared)

    block = store.get("rules", scope=scope)

    assert block is not None
    assert block.content == "linked rule"


def test_the_user_tier_still_writes_through_its_own_link(tmp_path):
    """The user's directories are the user's layout; only project tiers are judged."""
    store, _project, outside = _store(tmp_path)
    dotfiles = outside / "dotfiles-pinned"
    dotfiles.mkdir()
    _symlink_or_skip(tmp_path / "user-memory" / "pinned", dotfiles)
    store.set("rules", "my rule")
    assert "my rule" in (dotfiles / "general" / "rules.md").read_text(encoding="utf-8")
    assert store.delete("rules") is True
    assert not (dotfiles / "general" / "rules.md").exists()


def test_a_project_reached_through_a_link_above_its_root_is_not_refused(tmp_path):
    """Nothing above the project root is the repository's to deliver."""
    real = tmp_path / "real-project"
    real.mkdir()
    alias = tmp_path / "alias"
    _symlink_or_skip(alias, real)
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [tmp_path / "user-memory"]
    store = PinnedContextStore(config, project_root=alias)
    store.set("rules", "local rule", scope="project_local")
    assert (real / ".memtomem" / "memories.local" / "pinned" / "general" / "rules.md").is_file()


@pytest.mark.parametrize("op", ["get", "set", "delete"])
def test_an_invalid_id_is_reported_before_a_missing_project_context(tmp_path, op):
    """Both refusals apply; the id's comes first."""
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [tmp_path / "user-memory"]
    store = PinnedContextStore(config, project_root=None)
    with pytest.raises(ValueError, match="invalid block id"):
        if op == "get":
            store.get("../bad", scope="project_local")
        elif op == "set":
            store.set("../bad", "PROBE", scope="project_local")
        else:
            store.delete("../bad", scope="project_local")


@pytest.mark.parametrize("op", ["get", "set", "delete"])
def test_an_invalid_agent_id_is_reported_before_an_unknown_scope(tmp_path, op):
    store, _project, _outside = _store(tmp_path)
    with pytest.raises(ValueError, match="invalid agent id"):
        if op == "get":
            store.get("ok", scope="unknown", agent_id="../bad")  # type: ignore[arg-type]
        elif op == "set":
            store.set("ok", "PROBE", scope="unknown", agent_id="../bad")  # type: ignore[arg-type]
        else:
            store.delete("ok", scope="unknown", agent_id="../bad")  # type: ignore[arg-type]


@pytest.mark.parametrize("tool", ["mem_pinned_set", "mem_pinned_delete"])
@pytest.mark.asyncio
async def test_the_mcp_tool_answers_an_error_and_changes_nothing(
    bm25_only_components, tmp_path, monkeypatch, tool
):
    from helpers import StubCtx
    from memtomem.server.context import AppContext
    from memtomem.server.tools import pinned as pinned_tools

    comp, _mem_dir = bm25_only_components
    project = tmp_path / "proj_pinned_link"
    (project / ".git").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = _plant("general", "project_shared", project, outside)
    comp.config.indexing.project_memory_dirs = [project / ".memtomem" / "memories"]
    monkeypatch.chdir(project)
    ctx = StubCtx(AppContext.from_components(comp))
    before = _files(project, outside)

    if tool == "mem_pinned_set":
        out = await pinned_tools.mem_pinned_set(
            "victim", "PROBE", scope="project_shared", confirm_project_shared=True, ctx=ctx
        )
    else:
        out = await pinned_tools.mem_pinned_delete(
            "victim", scope="project_shared", confirm_project_shared=True, ctx=ctx
        )

    assert out.startswith("Error:"), out
    assert "is a link (or cannot be inspected)" in out
    assert victim.read_bytes() == b"THE USER'S OWN FILE\n"
    assert _files(project, outside) == before


@pytest.mark.parametrize("helper", ["_set_block", "_delete_block"])
@pytest.mark.asyncio
async def test_the_cli_reports_the_refusal_and_changes_nothing(tmp_path, monkeypatch, helper):
    """The real store behind the CLI helper; only the component bootstrap is replaced."""
    from memtomem.cli import pinned_cmd

    store, project, outside = _store(tmp_path)
    _plant("general", "project_local", project, outside)
    before = _files(project, outside)

    @asynccontextmanager
    async def _ctx():
        yield (None, store)

    monkeypatch.setattr(pinned_cmd, "_store_context", _ctx)
    with pytest.raises(click.ClickException) as caught:
        if helper == "_set_block":
            await pinned_cmd._set_block(
                block_id="victim",
                content="PROBE",
                description="",
                priority=0,
                scope="project_local",
                agent_id=None,
                confirm_project_shared=False,
                force_unsafe=False,
            )
        else:
            await pinned_cmd._delete_block(
                block_id="victim",
                scope="project_local",
                agent_id=None,
                confirm_project_shared=False,
            )

    assert "is a link (or cannot be inspected)" in caught.value.message
    assert _files(project, outside) == before
