"""Tests for ``memtomem.context.install`` update path.

Covers PR-D C2 commits 3 and 4:

- Commit 3: ``mm context update <type> <name>`` semantics — clean drift,
  dirty refuse, ``--force`` + ``.bak``, no-op invariant,
  ``NotInstalledError``, the flipped ``AlreadyInstalledError`` message.
- Commit 4: ``--all`` orchestration — 4-state classification,
  batch-once ``current_commit``, cache reuse, refuse-blocks-batch,
  ``--yes --force`` invariant, wiki-dirty warn timing.
"""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

import memtomem.context.install as install_module
from memtomem.cli.context_cmd import context as context_group
from memtomem.context._names import InvalidNameError
from memtomem.context.install import (
    AlreadyInstalledError,
    NotInstalledError,
    StaleInstallError,
    _classify_for_all_update,
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


# ════════════════════════════════════════════════════════════════════════
# Commit 4 — --all + wiki dirty warn
# ════════════════════════════════════════════════════════════════════════


# ── helpers for --all tests ─────────────────────────────────────────────


def _seed_known_projects(path: Path, project_roots: list[Path]) -> None:
    """Write a ``known_projects.json`` listing the given roots."""
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = {
        "version": 1,
        "projects": [
            {"root": str(p), "added_at": "2026-01-01T00:00:00.000000Z", "label": None}
            for p in project_roots
        ],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


def _patch_known_projects_path(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    """Make ``ContextGatewayConfig()`` in the CLI return *path*."""

    class _FakeCfg:
        known_projects_path = path

    monkeypatch.setattr(
        "memtomem.cli.context_cmd.ContextGatewayConfig",
        lambda: _FakeCfg(),
    )


# ── _classify_for_all_update: batch-once + 4-state coverage ─────────────


def test_classify_for_all_update_calls_current_commit_once(
    wiki_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``current_commit`` runs ONCE regardless of how many projects are scanned."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})

    project_a = tmp_path / "proj_a"
    project_a.mkdir()
    project_b = tmp_path / "proj_b"
    project_b.mkdir()
    install_skill(project_a, "foo")
    install_skill(project_b, "foo")

    wiki = WikiStore.at_default()
    real_current_commit = wiki.current_commit
    call_count = {"n": 0}

    def _spy_current_commit() -> str:
        call_count["n"] += 1
        return real_current_commit()

    monkeypatch.setattr(WikiStore, "current_commit", lambda self: _spy_current_commit())

    new_commit, classifications = _classify_for_all_update(
        "skills", "foo", wiki=wiki, projects=[project_a, project_b]
    )

    # Wiki state read once for the whole batch.
    assert call_count["n"] == 1
    assert len(new_commit) == 40  # full SHA
    assert len(classifications) == 2


def test_classify_for_all_update_4_state_coverage(wiki_root: Path, tmp_path: Path) -> None:
    """One project per state — verifies all 4 states are reachable.

    Setup strategy: wiki has a single commit. All 4 projects install
    against that commit. Then 3 of them have their lockfile entries
    back-dated (or corrupted) to simulate drift / dirty / error states.
    The 4th stays at the live HEAD and classifies as ``unchanged``.
    """
    from memtomem.context.lockfile import Lockfile

    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})

    wiki = WikiStore.at_default()
    head_commit = wiki.current_commit()

    # State 1: "unchanged" — fresh install, lockfile pin == HEAD.
    proj_unchanged = tmp_path / "unchanged"
    proj_unchanged.mkdir()
    install_skill(proj_unchanged, "foo")

    # State 2: "update" — install, then back-date lockfile pin so HEAD
    # looks like a newer commit. Dest mtimes are ahead of installed_at
    # (just installed → fresh), so we also back-date installed_at to
    # ensure files are NOT classified dirty. Use a fixed past timestamp.
    # Wait — we WANT the dest tree to be clean (= reason="clean"), but
    # back-dating installed_at to far past makes ALL current files look
    # dirty. We need the OPPOSITE: future installed_at OR explicit clean
    # mtimes. Easiest: back-date both lockfile fields, then bump every
    # dest file's mtime to BEFORE installed_at_epoch via os.utime.
    proj_update = tmp_path / "update_clean"
    proj_update.mkdir()
    install_skill(proj_update, "foo")
    Lockfile.at(proj_update).upsert_entry(
        "skills",
        "foo",
        wiki_commit="0" * 40,  # ≠ HEAD → drift
        installed_at="2030-01-01T00:00:00.000000Z",  # future → all current files are clean
    )

    # State 3: "refuse" — drift + dirty.
    proj_refuse = tmp_path / "refuse"
    proj_refuse.mkdir()
    install_skill(proj_refuse, "foo")
    Lockfile.at(proj_refuse).upsert_entry(
        "skills",
        "foo",
        wiki_commit="0" * 40,
        # Past installed_at so every current file is dirty.
        installed_at="2020-01-01T00:00:00.000000Z",
    )

    # State 4: "error" — unknown lockfile version.
    proj_error = tmp_path / "error"
    proj_error.mkdir()
    (proj_error / ".memtomem").mkdir()
    (proj_error / ".memtomem" / "lock.json").write_text(
        json.dumps({"version": 99, "skills": {"foo": {"wiki_commit": "x", "installed_at": "y"}}}),
        encoding="utf-8",
    )

    new_commit, classifications = _classify_for_all_update(
        "skills",
        "foo",
        wiki=wiki,
        projects=[proj_unchanged, proj_update, proj_refuse, proj_error],
    )

    assert new_commit == head_commit
    states = {c.project_root.name: c.state for c in classifications}
    assert states == {
        "unchanged": "unchanged",
        "update_clean": "update",
        "refuse": "refuse",
        "error": "error",
    }
    # Cache pin: unchanged + error have no dirty_report; update + refuse do.
    by_name = {c.project_root.name: c for c in classifications}
    assert by_name["unchanged"].dirty_report is None
    assert by_name["error"].dirty_report is None
    assert by_name["update_clean"].dirty_report is not None
    assert by_name["update_clean"].dirty_report.reason == "clean"
    assert by_name["refuse"].dirty_report is not None
    assert by_name["refuse"].dirty_report.reason == "dirty"


def test_classify_rejects_path_traversal_name(wiki_root: Path, tmp_path: Path) -> None:
    """``_classify_for_all_update`` validates ``name`` at its boundary —
    a traversal name like ``../escape`` raises ``InvalidNameError``
    *before* any per-project loop runs.

    Defense in depth (`feedback_public_api_ship_time_validation`):
    even though the CLI ``update_cmd`` is the expected upstream caller
    and the single-asset path goes through ``_update_asset`` which
    already validates, the ``--all`` path reaches ``_classify_for_all_
    update`` directly and would feed ``name`` into ``Path`` joins
    (``src = wiki.root / asset_type / name``) without this check.
    """
    _initialized_wiki(wiki_root)
    proj = tmp_path / "p"
    proj.mkdir()

    wiki = WikiStore.at_default()
    with pytest.raises(InvalidNameError):
        _classify_for_all_update("skills", "../etc", wiki=wiki, projects=[proj])
    with pytest.raises(InvalidNameError):
        _classify_for_all_update("skills", "../../escape", wiki=wiki, projects=[proj])
    with pytest.raises(InvalidNameError):
        _classify_for_all_update("skills", "foo/bar", wiki=wiki, projects=[proj])


def test_classify_skips_projects_without_lockfile_entry(wiki_root: Path, tmp_path: Path) -> None:
    """Projects without a lockfile entry for this asset are silently
    skipped — no ``"skipped"`` row clutters the preview table."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})

    proj_with = tmp_path / "with_foo"
    proj_with.mkdir()
    install_skill(proj_with, "foo")

    proj_without = tmp_path / "without_foo"
    proj_without.mkdir()  # no lockfile, no install

    wiki = WikiStore.at_default()
    new_commit, classifications = _classify_for_all_update(
        "skills", "foo", wiki=wiki, projects=[proj_with, proj_without]
    )

    assert len(classifications) == 1
    assert classifications[0].project_root == proj_with


# ── CLI --all: empty store + no-projects-have-asset ─────────────────────


def test_cli_update_all_rejects_path_traversal_name(
    wiki_root: Path,
    project_cwd: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CLI surfaces ``InvalidNameError`` as a non-zero exit before
    touching any project. Pins that the validation gate is reachable
    through the user-facing path, not just from direct calls."""
    _initialized_wiki(wiki_root)
    proj = tmp_path / "p"
    proj.mkdir()
    known = tmp_path / "known.json"
    _seed_known_projects(known, [proj])
    _patch_known_projects_path(monkeypatch, known)

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "../etc", "--all", "--yes"])

    assert result.exit_code != 0
    assert "invalid" in result.output.lower()


def test_cli_update_all_empty_known_projects_exits_zero(
    wiki_root: Path,
    project_cwd: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--all`` with empty known_projects.json exits 0 with an info message.

    cron/CI safety: a first-run before any project registration must
    not fail with a non-zero exit.
    """
    _initialized_wiki(wiki_root)
    known = tmp_path / "known.json"
    _seed_known_projects(known, [])
    _patch_known_projects_path(monkeypatch, known)

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "foo", "--all"])

    assert result.exit_code == 0, result.output
    assert "No known projects" in result.output


def test_cli_update_all_no_projects_have_asset_exits_zero(
    wiki_root: Path,
    project_cwd: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All registered projects exist on disk but none has the asset → exit 0."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})

    proj = tmp_path / "no_lock"
    proj.mkdir()  # exists but no install
    known = tmp_path / "known.json"
    _seed_known_projects(known, [proj])
    _patch_known_projects_path(monkeypatch, known)

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "foo", "--all"])

    assert result.exit_code == 0, result.output
    assert "No projects have skills/foo" in result.output


# ── CLI --all: refuse blocks batch ──────────────────────────────────────


def test_cli_update_all_refuse_without_force_blocks_batch(
    wiki_root: Path,
    project_cwd: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When any project is dirty and ``--force`` is absent, the entire
    batch refuses — no per-project writes happen."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})

    # Install in two projects; dirty one of them.
    proj_clean = tmp_path / "clean"
    proj_clean.mkdir()
    install_skill(proj_clean, "foo")

    proj_dirty = tmp_path / "dirty"
    proj_dirty.mkdir()
    install_skill(proj_dirty, "foo")
    edited = proj_dirty / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"local\n")
    _bump_mtime(edited)

    _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    known = tmp_path / "known.json"
    _seed_known_projects(known, [proj_clean, proj_dirty])
    _patch_known_projects_path(monkeypatch, known)

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "foo", "--all"])

    assert result.exit_code != 0
    assert "local edits" in result.output
    assert "--force" in result.output

    # Critical: the clean project's lockfile MUST NOT have been bumped.
    # If a partial write happened, this would catch the regression.
    lock_doc = json.loads((proj_clean / ".memtomem" / "lock.json").read_text())
    # wiki_commit on lockfile entry should still match the original install
    # (not the new wiki HEAD) — the entire batch refused before any write.
    assert (proj_clean / ".memtomem" / "skills" / "foo" / "SKILL.md").read_bytes() == b"v1\n"
    assert lock_doc["skills"]["foo"]["wiki_commit"] != WikiStore.at_default().current_commit()


# ── CLI --all: --yes invariants (no WARN unless --force) ────────────────


def test_cli_update_all_yes_skips_prompt_and_no_destructive_warning(
    wiki_root: Path,
    project_cwd: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--yes`` (without ``--force``) skips the confirm prompt and does
    NOT print the destructive WARNING. Plain ``--yes`` is non-destructive
    automation; the WARNING only fires for ``--force``-laden batches."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})

    proj_clean = tmp_path / "clean"
    proj_clean.mkdir()
    install_skill(proj_clean, "foo")
    _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    known = tmp_path / "known.json"
    _seed_known_projects(known, [proj_clean])
    _patch_known_projects_path(monkeypatch, known)

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "foo", "--all", "--yes"])

    assert result.exit_code == 0, result.output
    # Prompt-skip pin: input was empty but the command didn't hang.
    assert "Continue?" not in result.output
    # WARN-pin (negative): no destructive warning for plain --yes.
    assert "WARNING:" not in result.output
    # Update happened.
    assert "updated" in result.output
    assert (proj_clean / ".memtomem" / "skills" / "foo" / "SKILL.md").read_bytes() == b"v2\n"


def test_cli_update_all_yes_force_three_way_invariant(
    wiki_root: Path,
    project_cwd: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--yes --force`` invariant: WARNING printed AND prompt skipped AND
    batch executed (3-way conjunction). Tested as a single block so a
    regression in any one of the three is caught."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})

    proj_dirty = tmp_path / "dirty"
    proj_dirty.mkdir()
    install_skill(proj_dirty, "foo")
    edited = proj_dirty / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"local\n")
    _bump_mtime(edited)
    _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    known = tmp_path / "known.json"
    _seed_known_projects(known, [proj_dirty])
    _patch_known_projects_path(monkeypatch, known)

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "foo", "--all", "--yes", "--force"])

    assert result.exit_code == 0, result.output
    # 1. WARNING printed.
    assert "WARNING:" in result.output
    # 2. Prompt skipped.
    assert "Continue?" not in result.output
    # 3. Batch executed — bytes changed and .bak survived the dirty edit.
    bak = edited.with_suffix(edited.suffix + ".bak")
    assert bak.is_file()
    assert bak.read_bytes() == b"local\n"
    assert edited.read_bytes() == b"v2\n"


# ── CLI --all: cache reuse pin ──────────────────────────────────────────


def test_cli_update_all_mid_loop_fs_error_continues(
    wiki_root: Path,
    project_cwd: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A per-project ``OSError`` during execute marks that row ✗ and
    proceeds to the next project — the batch is not aborted on the
    first row's failure. The summary reflects the partial outcome.
    """
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})

    proj_a = tmp_path / "proj_a"
    proj_a.mkdir()
    install_skill(proj_a, "foo")

    proj_b = tmp_path / "proj_b"
    proj_b.mkdir()
    install_skill(proj_b, "foo")

    _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    known = tmp_path / "known.json"
    _seed_known_projects(known, [proj_a, proj_b])
    _patch_known_projects_path(monkeypatch, known)

    real_apply_update = install_module._apply_update
    call_count = {"n": 0}

    def _apply_update_first_fails(*args: object, **kwargs: object) -> object:
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise OSError("simulated fs error on first project")
        return real_apply_update(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr("memtomem.cli.context_cmd._apply_update", _apply_update_first_fails)

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "foo", "--all", "--yes"])

    assert result.exit_code == 0, result.output
    # First project marked ✗, second project marked ✓ — both rows reached.
    assert "✗" in result.output
    assert "simulated fs error" in result.output
    assert "✓" in result.output
    # Summary: 1 updated, 1 failed.
    assert "1 updated" in result.output
    assert "1 failed" in result.output


def test_cli_update_all_does_not_re_walk_for_dirty(
    wiki_root: Path,
    project_cwd: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Execute phase MUST consume the cached ``dirty_report`` from
    classification — no second ``is_asset_dirty`` call per project.

    Spy counts ``is_asset_dirty`` invocations during the whole flow:
    classify calls it once for the dirty project (state=refuse with
    --force allowed), and execute reuses the cached report. Total = 1
    per project that needed classification. Without the cache, total
    would be 2 per project.
    """
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})

    proj_dirty = tmp_path / "dirty"
    proj_dirty.mkdir()
    install_skill(proj_dirty, "foo")
    edited = proj_dirty / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"local\n")
    _bump_mtime(edited)
    _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    known = tmp_path / "known.json"
    _seed_known_projects(known, [proj_dirty])
    _patch_known_projects_path(monkeypatch, known)

    real_is_asset_dirty = install_module.is_asset_dirty
    call_count = {"n": 0}

    def _spy(*args: object, **kwargs: object):
        call_count["n"] += 1
        return real_is_asset_dirty(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(install_module, "is_asset_dirty", _spy)

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "foo", "--all", "--yes", "--force"])

    assert result.exit_code == 0, result.output
    # 1 = classification's call only. Execute used the cached DirtyReport
    # from ProjectClassification — no second walk.
    assert call_count["n"] == 1


# ── CLI: wiki dirty warn timing (single-asset & --all) ──────────────────


def test_cli_update_wiki_dirty_warn_single_asset(
    wiki_root: Path,
    project_cwd: Path,
) -> None:
    """Single-asset path: wiki dirty → warn on stderr at update entry."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    install_skill(project_cwd, "foo")

    # Make the wiki dirty.
    (wiki_root / "untracked_marker.txt").write_text("wip", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "foo"])

    assert result.exit_code == 0, result.output
    assert "wiki has uncommitted changes" in result.output


def test_cli_update_wiki_dirty_warn_all(
    wiki_root: Path,
    project_cwd: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--all path: wiki dirty → warn on stderr BEFORE classification."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    proj = tmp_path / "p"
    proj.mkdir()
    install_skill(proj, "foo")

    (wiki_root / "untracked_marker.txt").write_text("wip", encoding="utf-8")

    known = tmp_path / "known.json"
    _seed_known_projects(known, [proj])
    _patch_known_projects_path(monkeypatch, known)

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "foo", "--all", "--yes"])

    assert result.exit_code == 0, result.output
    assert "wiki has uncommitted changes" in result.output


def test_cli_update_no_wiki_dirty_warn_when_clean(
    wiki_root: Path,
    project_cwd: Path,
) -> None:
    """Negative pin: clean wiki → no dirty warn line."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    install_skill(project_cwd, "foo")

    runner = CliRunner()
    result = runner.invoke(context_group, ["update", "skill", "foo"])

    assert result.exit_code == 0, result.output
    assert "wiki has uncommitted changes" not in result.output
