"""Real-FS integration pins for the ``mem_context_artifact_migrate`` MCP tool
(#1147 B5-1).

The tool mirrors the CLI ``mm context migrate`` verb's two modes (flat→dir and
scope-tier) and reuses the same pure functions. These tests exercise it against
real filesystem layouts (not mocked) through the same ``.git`` + ``HOME``
fixture the CLI scope-tier tests use, and pin:

* the full validation-gate set is mirrored before mode selection;
* ``apply=False`` (the default) never mutates the filesystem;
* flat→dir apply / dirty-force / skills no-op;
* scope-tier dry-run / apply / project_shared gate / refuse-on-conflict.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memtomem.context.lockfile import Lockfile
from memtomem.server.tools.context import mem_context_artifact_migrate

_DIR_FILE = {"agents": "agent.md", "commands": "command.md"}


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A project root (with ``.git`` so ``_find_project_root`` terminates) plus a
    fake ``HOME`` so the ``user`` tier resolves under ``tmp_path``."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.chdir(project_root)
    return {"project_root": project_root, "user_home": user_home}


def _canonical_root(layout: dict[str, Path], kind: str, scope: str) -> Path:
    if scope == "user":
        return layout["user_home"] / ".memtomem" / kind
    if scope == "project_shared":
        return layout["project_root"] / ".memtomem" / kind
    if scope == "project_local":
        return layout["project_root"] / ".memtomem" / f"{kind}.local"
    raise ValueError(scope)


def _seed_flat_clean(project: Path, kind: str, name: str) -> Path:
    """Flat file + clean lock entry → classify state ``migrate``."""
    flat = project / ".memtomem" / kind / f"{name}.md"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_bytes(b"v1\n")
    installed_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    Lockfile.at(project).upsert_entry(kind, name, wiki_commit="0" * 40, installed_at=installed_at)
    epoch = datetime.fromisoformat(installed_at).timestamp()
    os.utime(flat, (epoch, epoch))  # mtime == installed_at → clean
    return flat


def _write_dir_canonical(layout: dict[str, Path], kind: str, scope: str, name: str) -> Path:
    """Dir-layout canonical artifact; returns the artifact directory."""
    d = _canonical_root(layout, kind, scope) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / _DIR_FILE[kind]).write_text("a harmless agent body\n", encoding="utf-8")
    return d


# ── flat→dir mode ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_flat_dry_run_default_does_not_mutate(layout):
    proj = layout["project_root"]
    flat = _seed_flat_clean(proj, "agents", "foo")

    out = await mem_context_artifact_migrate(asset_type="agents")
    assert "Will migrate" in out and "foo" in out
    assert "Re-call with apply=True" in out
    # The default (no apply) must leave the filesystem untouched.
    assert flat.exists()
    assert not (proj / ".memtomem" / "agents" / "foo" / "agent.md").exists()


@pytest.mark.anyio
async def test_flat_apply_migrates(layout):
    proj = layout["project_root"]
    flat = _seed_flat_clean(proj, "agents", "foo")

    out = await mem_context_artifact_migrate(asset_type="agents", apply=True)
    assert "migrated" in out
    assert not flat.exists()
    assert (proj / ".memtomem" / "agents" / "foo" / "agent.md").exists()


@pytest.mark.anyio
async def test_flat_dirty_refused_without_force_then_force(layout):
    proj = layout["project_root"]
    flat = _seed_flat_clean(proj, "agents", "foo")
    os.utime(flat, (datetime.now(timezone.utc).timestamp() + 60,) * 2)  # dirty

    refused = await mem_context_artifact_migrate(asset_type="agents", apply=True)
    assert refused.startswith("refused:")
    assert flat.exists()  # no entry written

    forced = await mem_context_artifact_migrate(asset_type="agents", apply=True, force=True)
    assert "migrated" in forced
    assert ".bak" in forced
    assert (proj / ".memtomem" / "agents" / "foo" / "agent.md").exists()


@pytest.mark.anyio
async def test_flat_skills_is_noop(layout):
    out = await mem_context_artifact_migrate(asset_type="skills")
    assert "always directory layout" in out


@pytest.mark.anyio
async def test_flat_no_assets_message(layout):
    out = await mem_context_artifact_migrate(asset_type="agents")
    assert "No flat-layout assets to migrate" in out


# ── scope-tier mode ──────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_scope_dry_run_default_does_not_mutate(layout):
    src = _write_dir_canonical(layout, "agents", "user", "foo")
    out = await mem_context_artifact_migrate(
        asset_type="agents", name="foo", to_scope="project_local"
    )
    assert out.startswith("Plan: migrate agents/foo")
    assert "Re-call with apply=True" in out
    assert (src / "agent.md").exists()  # untouched
    assert not (_canonical_root(layout, "agents", "project_local") / "foo").exists()


@pytest.mark.anyio
async def test_scope_apply_user_to_project_local(layout):
    src = _write_dir_canonical(layout, "agents", "user", "foo")
    out = await mem_context_artifact_migrate(
        asset_type="agents", name="foo", to_scope="project_local", apply=True
    )
    assert "moved agents/foo" in out
    assert not src.exists()  # src consumed
    dst = _canonical_root(layout, "agents", "project_local") / "foo" / "agent.md"
    assert dst.exists()
    assert "gitignore marker" in out.lower()  # project_local first-landing


@pytest.mark.anyio
async def test_scope_project_shared_requires_confirmation(layout):
    src = _write_dir_canonical(layout, "agents", "user", "foo")
    blocked = await mem_context_artifact_migrate(
        asset_type="agents", name="foo", to_scope="project_shared", apply=True
    )
    assert blocked.startswith("needs confirmation:")
    assert src.exists()  # untouched without confirm

    ok = await mem_context_artifact_migrate(
        asset_type="agents",
        name="foo",
        to_scope="project_shared",
        apply=True,
        confirm_project_shared=True,
    )
    assert "moved agents/foo" in ok
    assert (_canonical_root(layout, "agents", "project_shared") / "foo" / "agent.md").exists()


@pytest.mark.anyio
async def test_scope_refuses_on_destination_conflict(layout):
    _write_dir_canonical(layout, "agents", "user", "foo")
    _write_dir_canonical(layout, "agents", "project_local", "foo")  # dst already present
    # Explicit from_scope so the move is unambiguous and the dst-conflict gate
    # (not source auto-detect) is what's exercised.
    out = await mem_context_artifact_migrate(
        asset_type="agents", name="foo", from_scope="user", to_scope="project_local", apply=True
    )
    assert out.startswith("error:")
    assert "destination already exists" in out


# ── validation gates (mirror the CLI; Codex review fold) ─────────────────


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("kwargs", "needle"),
    [
        ({"asset_type": "agents", "force": True}, "force=True is only valid with apply=True"),
        ({"name": "foo"}, "name requires asset_type"),
        (
            {"asset_type": "memory", "name": "x", "to_scope": "user", "apply": True},
            "mem_context_memory_migrate",
        ),
        ({"asset_type": "agents", "name": "foo", "to_scope": "bogus"}, "Unknown to_scope"),
        ({"asset_type": "agents", "from_scope": "user"}, "from_scope requires to_scope"),
        ({"asset_type": "agents", "to_scope": "user"}, "name is required with to_scope"),
        (
            {
                "asset_type": "agents",
                "name": "foo",
                "to_scope": "user",
                "force": True,
                "apply": True,
            },
            "force does not apply to scope-tier",
        ),
        (
            {"confirm_project_shared": True, "asset_type": "agents"},
            "confirm_project_shared requires to_scope",
        ),
    ],
)
async def test_validation_gates(layout, kwargs, needle):
    out = await mem_context_artifact_migrate(**kwargs)
    assert needle in out


# ── project_local gitignore-marker warning states (#1152 review) ─────────


def _no_git_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, pyproject: bool) -> Path:
    """A project root WITHOUT .git (optionally with pyproject.toml) + a fake
    HOME holding a user-tier agent 'foo'. Returns the project root."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    if pyproject:
        (project_root / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.chdir(project_root)
    d = user_home / ".memtomem" / "agents" / "foo"
    d.mkdir(parents=True)
    (d / "agent.md").write_text("a harmless agent body\n", encoding="utf-8")
    return project_root


@pytest.mark.anyio
async def test_project_local_warns_when_no_git_pyproject_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """pyproject but no .git: the move succeeds but the MCP tool must surface
    that .gitignore protection was skipped (parity with the CLI)."""
    _no_git_layout(tmp_path, monkeypatch, pyproject=True)
    out = await mem_context_artifact_migrate(
        asset_type="agents", name="foo", to_scope="project_local", apply=True
    )
    assert "moved agents/foo" in out
    assert "git init" in out
    assert ".gitignore not appended" in out


@pytest.mark.anyio
async def test_project_local_warns_when_no_project_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """No .git and no pyproject: the move succeeds but the tool must warn that
    the project_local tier is not git-protected."""
    _no_git_layout(tmp_path, monkeypatch, pyproject=False)
    out = await mem_context_artifact_migrate(
        asset_type="agents", name="foo", to_scope="project_local", apply=True
    )
    assert "moved agents/foo" in out
    assert "no .git and no pyproject" in out
    assert "not git-protected" in out
