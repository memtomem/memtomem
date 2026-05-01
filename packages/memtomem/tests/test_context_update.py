"""Tests for ``memtomem.context.install`` update path.

Covers PR-D C2 commit 3: ``mm context update <type> <name>`` semantics —
clean drift, dirty refuse, ``--force`` + ``.bak``, no-op invariant,
``NotInstalledError``, and the flipped ``AlreadyInstalledError`` message
that now points at update.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context as context_group
from memtomem.context.install import (
    AlreadyInstalledError,
    NotInstalledError,
    StaleInstallError,
    install_skill,
    update_agent,
    update_command,
    update_skill,
)
from memtomem.wiki.store import WikiStore


# ── helpers ──────────────────────────────────────────────────────────────


def _initialized_wiki(wiki_root_path: Path) -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


def _seed_wiki_skill(wiki_root_path: Path, name: str, files: dict[str, bytes]) -> str:
    """Add ``skills/<name>/`` to wiki + git commit. Returns the commit SHA."""
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    for relpath, data in files.items():
        target = skill_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"add {name}"],
        check=True,
        capture_output=True,
    )
    return WikiStore.at_default().current_commit()


def _modify_wiki_skill(wiki_root_path: Path, name: str, files: dict[str, bytes]) -> str:
    """Modify wiki skill files + commit. Wiki HEAD advances. Returns new SHA."""
    skill_dir = wiki_root_path / "skills" / name
    for relpath, data in files.items():
        target = skill_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"modify {name}"],
        check=True,
        capture_output=True,
    )
    return WikiStore.at_default().current_commit()


def _bump_mtime(path: Path, *, seconds_in_future: float = 1.0) -> None:
    """Force *path*'s mtime to ``now + seconds_in_future``.

    Tests need a deterministic strict ``>`` margin against ``installed_at``
    that single-second filesystem precision can't eat.
    """
    future = datetime.now(timezone.utc).timestamp() + seconds_in_future
    os.utime(path, (future, future))


# ── happy paths ──────────────────────────────────────────────────────────


def test_clean_drift_updates(wiki_root: Path, tmp_path: Path) -> None:
    """Wiki HEAD advances → update copies new bytes + bumps lockfile."""
    _initialized_wiki(wiki_root)
    old_commit = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    project = tmp_path

    install_skill(project, "foo")

    new_commit = _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    result = update_skill(project, "foo")

    assert result.was_no_op is False
    assert result.old_wiki_commit == old_commit
    assert result.new_wiki_commit == new_commit
    assert old_commit != new_commit
    assert (project / ".memtomem" / "skills" / "foo" / "SKILL.md").read_bytes() == b"v2\n"

    lock_doc = json.loads((project / ".memtomem" / "lock.json").read_text())
    assert lock_doc["skills"]["foo"]["wiki_commit"] == new_commit
    assert lock_doc["skills"]["foo"]["installed_at"] == result.installed_at


def test_dirty_refuse_without_force(wiki_root: Path, tmp_path: Path) -> None:
    """Local edit + wiki advance + no --force → StaleInstallError."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    project = tmp_path
    install_skill(project, "foo")

    edited = project / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"local edit\n")
    _bump_mtime(edited)
    _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    with pytest.raises(StaleInstallError) as excinfo:
        update_skill(project, "foo")
    msg = str(excinfo.value)
    assert "modified locally" in msg
    assert "--force" in msg
    # The user's edit MUST still be on disk (no partial write happened).
    assert edited.read_bytes() == b"local edit\n"


def test_dirty_force_writes_bak(wiki_root: Path, tmp_path: Path) -> None:
    """--force preserves the user edit's bytes in .bak before overwriting."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    project = tmp_path
    install_skill(project, "foo")

    edited = project / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"my edit\n")
    _bump_mtime(edited)
    _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    result = update_skill(project, "foo", force=True)

    bak = edited.with_suffix(edited.suffix + ".bak")
    assert bak.is_file()
    assert bak.read_bytes() == b"my edit\n"
    # Wiki bytes won the overwrite.
    assert edited.read_bytes() == b"v2\n"
    assert result.bak_files_written == (bak,)
    assert result.was_no_op is False


def test_dirty_force_only_dirty_files_get_bak(wiki_root: Path, tmp_path: Path) -> None:
    """Clean files have no .bak sibling — only edited files get one."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(
        wiki_root,
        "foo",
        {"SKILL.md": b"v1\n", "scripts/run.sh": b"original\n"},
    )
    project = tmp_path
    install_skill(project, "foo")

    skill_dir = project / ".memtomem" / "skills" / "foo"
    edited = skill_dir / "scripts" / "run.sh"
    edited.write_bytes(b"my edit\n")
    _bump_mtime(edited)
    _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n", "scripts/run.sh": b"new\n"})

    result = update_skill(project, "foo", force=True)

    skill_md = skill_dir / "SKILL.md"
    assert not (skill_md.with_suffix(skill_md.suffix + ".bak")).exists()
    bak = edited.with_suffix(edited.suffix + ".bak")
    assert bak.is_file()
    assert bak.read_bytes() == b"my edit\n"
    assert result.bak_files_written == (bak,)


# ── failure paths ────────────────────────────────────────────────────────


def test_not_installed_raises(wiki_root: Path, tmp_path: Path) -> None:
    """No lockfile entry → NotInstalledError points at install."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    project = tmp_path

    with pytest.raises(NotInstalledError) as excinfo:
        update_skill(project, "foo")
    msg = str(excinfo.value)
    assert "no lockfile entry" in msg
    assert "mm context install skill foo" in msg


# ── no-op invariant pin (lockfile mtime + installed_at + flag) ──────────


def test_no_op_invariant_pin(wiki_root: Path, tmp_path: Path) -> None:
    """No-op update MUST NOT touch the lockfile bytes.

    Pin: lockfile mtime + bytes unchanged AND installed_at echoed AND
    was_no_op=True. The lockfile-mtime pin is the load-bearing assertion
    — any future regression that re-writes the lockfile on no-op
    (e.g. someone "refreshing" installed_at) breaks this test loudly.
    """
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    project = tmp_path
    install_result = install_skill(project, "foo")

    lock_path = project / ".memtomem" / "lock.json"
    pre_bytes = lock_path.read_bytes()
    pre_mtime = lock_path.stat().st_mtime

    result = update_skill(project, "foo")

    # Positive marker — bytes untouched.
    assert lock_path.read_bytes() == pre_bytes
    # Positive marker — mtime untouched (no rewrite happened).
    assert lock_path.stat().st_mtime == pre_mtime
    # Positive marker — installed_at echoed from prior install.
    assert result.installed_at == install_result.installed_at
    # Positive marker — flag set.
    assert result.was_no_op is True
    assert result.bak_files_written == ()
    assert result.files_written == 0


# ── pin-and-invert: AlreadyInstalledError points at update ──────────────


def test_already_installed_message_points_to_update(wiki_root: Path, tmp_path: Path) -> None:
    """``AlreadyInstalledError`` now points at ``mm context update``.

    Pin-and-invert pair (per ``feedback_pin_invert_symmetric_assertion``):

    - Positive marker: ``"mm context update"`` in the message; the
      asset-type-singular form (``skill``, not ``skills``) matches the
      CLI argument the user types.
    - Negative marker: ``"reserved for PR-D"`` is gone; this is the
      string that lived there before C2 and must not return.
    """
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    project = tmp_path

    install_skill(project, "foo")

    with pytest.raises(AlreadyInstalledError) as excinfo:
        install_skill(project, "foo")
    msg = str(excinfo.value)

    # Positive markers — current invariant.
    assert "mm context update skill foo" in msg
    assert "refresh from wiki HEAD" in msg
    # Negative marker — the would-be-old message must not be present.
    assert "reserved for PR-D" not in msg
    # The diagnostic prefix is preserved (composes with other tests).
    assert "lockfile_entry=yes" in msg
    assert "dest=yes" in msg


# ── CLI: no-op exit clean ────────────────────────────────────────────────


@pytest.fixture
def project_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Tmp project root (with sentinel ``.git``) wired as cwd for
    ``_find_project_root``. Same shape as the install CLI tests'
    fixture so behavior parity is easy to reason about."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".git").mkdir()
    monkeypatch.chdir(project)
    return project


def test_cli_update_no_op_exits_clean(
    wiki_root: Path,
    project_cwd: Path,
) -> None:
    """``mm context update`` no-op prints ``unchanged`` with exit 0."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    install_skill(project_cwd, "foo")

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "foo"])

    assert result.exit_code == 0, result.output
    assert "unchanged" in result.output


# ── CLI: variant dispatch ────────────────────────────────────────────────


def test_cli_update_dispatches_to_correct_wrapper(
    project_cwd: Path,
    wiki_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``update_cmd`` routes ``skill / agent / command`` to the right wrapper.

    Mocks the 3 wrappers so the test pins the CLI dispatch shape without
    needing to seed wiki content for all 3 asset types. A regression
    that miswires (e.g. ``skill`` calling ``update_agent``) fails this
    test loudly.
    """
    # Need wiki initialized so the CLI doesn't error out before dispatch.
    _initialized_wiki(wiki_root)

    calls: list[tuple[str, str]] = []

    def _fake(asset_type: str):
        def _impl(root: Path, name: str, *, wiki: object = None, force: bool = False) -> object:
            calls.append((asset_type, name))
            # Return a sentinel UpdateResult — minimal fields the CLI prints.
            from memtomem.context.install import UpdateResult

            return UpdateResult(
                asset_type=f"{asset_type}s",  # type: ignore[arg-type]
                name=name,
                old_wiki_commit="0" * 40,
                new_wiki_commit="0" * 40,
                installed_at="2026-01-01T00:00:00.000000Z",
                was_no_op=True,
                bak_files_written=(),
                dest=root / ".memtomem" / f"{asset_type}s" / name,
                files_written=0,
            )

        return _impl

    monkeypatch.setattr("memtomem.cli.context_cmd.update_skill", _fake("skill"))
    monkeypatch.setattr("memtomem.cli.context_cmd.update_agent", _fake("agent"))
    monkeypatch.setattr("memtomem.cli.context_cmd.update_command", _fake("command"))

    runner = CliRunner()
    for asset_type, name in [("skill", "alpha"), ("agent", "beta"), ("command", "gamma")]:
        result = runner.invoke(context_group, ["update", asset_type, name])
        assert result.exit_code == 0, f"{asset_type}/{name}: {result.output}"

    assert calls == [("skill", "alpha"), ("agent", "beta"), ("command", "gamma")]


# ── CLI: stale refuse → exit non-zero with hint ─────────────────────────


def test_cli_update_stale_refuse_message(
    wiki_root: Path,
    project_cwd: Path,
) -> None:
    """CLI surfaces ``StaleInstallError`` text + non-zero exit."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    install_skill(project_cwd, "foo")

    edited = project_cwd / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"local\n")
    _bump_mtime(edited)
    _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "foo"])

    assert result.exit_code != 0
    assert "modified locally" in result.output
    assert "--force" in result.output


# ── coverage: silence unused-import warnings on agent/command wrappers ──


def test_update_agent_command_wrappers_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    """``update_agent`` / ``update_command`` route through ``_update_asset``
    with the right ``asset_type`` literal. Direct call sites (not via CLI)
    must keep the 3-wrapper API stable for downstream Python callers."""
    seen: list[str] = []

    def _fake_update_asset(
        project_root: object, asset_type: str, name: str, *, wiki: object, force: bool
    ) -> object:
        seen.append(asset_type)
        from memtomem.context.install import UpdateResult

        return UpdateResult(
            asset_type=asset_type,  # type: ignore[arg-type]
            name=name,
            old_wiki_commit="0" * 40,
            new_wiki_commit="0" * 40,
            installed_at="2026-01-01T00:00:00.000000Z",
            was_no_op=True,
            bak_files_written=(),
            dest=Path("/dev/null"),
            files_written=0,
        )

    monkeypatch.setattr("memtomem.context.install._update_asset", _fake_update_asset)

    update_agent(Path("/tmp/p"), "a")
    update_command(Path("/tmp/p"), "c")
    update_skill(Path("/tmp/p"), "s")

    assert seen == ["agents", "commands", "skills"]
