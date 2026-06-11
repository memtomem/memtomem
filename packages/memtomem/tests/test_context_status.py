"""Tests for ``memtomem.context.status`` and the ``mm context status`` CLI.

Covers PR-D C3 commit 1: read-only inventory + drift classification of
installed wiki assets, no-write invariant, exit-code contract, and the
wiki-absent / corrupt-lockfile graceful-degradation paths.

These tests construct lockfile + dest tree + wiki manually using the
same shared fixtures as ``test_context_update.py`` (``wiki_root``,
``git_identity`` from ``_wiki_fixtures``). Pin reachability tests
exercise the new ``WikiStore.commit_is_reachable`` helper added in
the same commit.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context as context_group
from memtomem.context._atomic import atomic_write_bytes, installed_at_from_dest
from memtomem.context.lockfile import Lockfile
from memtomem.context.migrate import migrate_scope
from memtomem.context.status import StatusRow, classify_status, scan_user_artifacts
from memtomem.wiki.store import WikiStore

from .helpers import set_home


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
    """Modify wiki skill files + commit. Wiki HEAD advances."""
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


def _setup_installed_at_pin(
    project: Path,
    asset_type: str,
    name: str,
    files: dict[str, bytes],
    pin: str,
) -> str:
    """Manually drop bytes + lockfile entry pinned at *pin*. Returns installed_at."""
    dest = project / ".memtomem" / asset_type / name
    dest.mkdir(parents=True)
    for relpath, data in files.items():
        target = dest / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    installed_at = installed_at_from_dest(dest)
    Lockfile.at(project).upsert_entry(asset_type, name, wiki_commit=pin, installed_at=installed_at)
    return installed_at


def _bump_mtime(path: Path, *, seconds_in_future: float = 1.0) -> None:
    import os

    future = datetime.now(timezone.utc).timestamp() + seconds_in_future
    os.utime(path, (future, future))


def _rewrite_wiki_history(wiki_root_path: Path, name: str) -> None:
    """Force-rewrite wiki history so previously-pinned commits become orphan.

    Resets the branch to the initial scaffold commit and recommits with new content,
    so any pre-rewrite SHA is no longer reachable from refs.
    """
    # Soft-reset to root, then drop and re-add the asset to produce a different SHA.
    initial = subprocess.run(
        ["git", "-C", str(wiki_root_path), "rev-list", "--max-parents=0", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "reset", "--hard", initial],
        check=True,
        capture_output=True,
    )
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_bytes(b"# rewritten\n")
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"rewrite {name}"],
        check=True,
        capture_output=True,
    )
    # Run gc so the orphaned commit is no longer reachable from any ref. Cat-file
    # would still find it if reflog/loose objects keep it; expire reflog first.
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "reflog", "expire", "--expire=now", "--all"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "gc", "--prune=now", "--quiet"],
        check=True,
        capture_output=True,
    )


# ── pure classifier tests ────────────────────────────────────────────────


def test_empty_lockfile(wiki_root: Path, tmp_path: Path) -> None:
    """No lockfile entries → empty rows, wiki_head still resolved."""
    _initialized_wiki(wiki_root)

    head, rows = classify_status(tmp_path)

    assert head is not None
    assert rows == []


def test_ok_state_pin_at_head(wiki_root: Path, tmp_path: Path) -> None:
    """Dest clean, pin == wiki HEAD → state=ok."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, pin)

    head, rows = classify_status(tmp_path)

    assert head == pin
    assert len(rows) == 1
    assert rows[0].state == "ok"
    assert rows[0].pin_commit == pin
    assert rows[0].dirty_file_count == 0
    assert rows[0].reason is None


def test_behind_state_pin_below_head(wiki_root: Path, tmp_path: Path) -> None:
    """Pin reachable, pin != HEAD, dest clean → state=behind."""
    _initialized_wiki(wiki_root)
    old_pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, old_pin)
    new_head = _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    head, rows = classify_status(tmp_path)

    assert head == new_head
    assert head != old_pin
    assert len(rows) == 1
    assert rows[0].state == "behind"
    assert rows[0].pin_commit == old_pin


def test_dirty_state_after_local_edit(wiki_root: Path, tmp_path: Path) -> None:
    """Dest mtime > installed_at → state=dirty + count + reason."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, pin)
    edited = tmp_path / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"manual edit\n")
    _bump_mtime(edited)

    _, rows = classify_status(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "dirty"
    assert rows[0].dirty_file_count == 1
    assert "modified locally" in (rows[0].reason or "")


def test_missing_state_dest_deleted(wiki_root: Path, tmp_path: Path) -> None:
    """Lockfile entry exists but dest dir deleted → state=missing."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, pin)
    shutil.rmtree(tmp_path / ".memtomem" / "skills" / "foo")

    _, rows = classify_status(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "missing"
    assert rows[0].reason == "dest missing"


def test_stale_pin_when_wiki_absent(wiki_root: Path, tmp_path: Path) -> None:
    """Wiki absent → wiki_head=None, rows still render, clean rows go stale-pin."""
    # Don't init the wiki.
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, "0" * 40)

    head, rows = classify_status(tmp_path)

    assert head is None
    assert len(rows) == 1
    assert rows[0].state == "stale-pin"
    assert rows[0].reason == "wiki not present"


def test_stale_pin_when_history_rewritten(wiki_root: Path, tmp_path: Path) -> None:
    """Pin not reachable in wiki (force-pushed past) → state=stale-pin."""
    _initialized_wiki(wiki_root)
    orphan_pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, orphan_pin)

    _rewrite_wiki_history(wiki_root, "foo")

    _, rows = classify_status(tmp_path)

    assert len(rows) == 1
    assert rows[0].state == "stale-pin"
    assert "not reachable" in (rows[0].reason or "")


def test_dirty_takes_priority_over_behind(wiki_root: Path, tmp_path: Path) -> None:
    """If both dirty and pin != HEAD, dirty wins (single state per row)."""
    _initialized_wiki(wiki_root)
    old_pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, old_pin)
    edited = tmp_path / ".memtomem" / "skills" / "foo" / "SKILL.md"
    edited.write_bytes(b"local edit\n")
    _bump_mtime(edited)
    _modify_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v2\n"})

    _, rows = classify_status(tmp_path)

    assert rows[0].state == "dirty"
    assert rows[0].dirty_file_count == 1


def test_iter_order_alphabetical_by_type_then_name(wiki_root: Path, tmp_path: Path) -> None:
    """Rows preserve iter_entries() order: agents → commands → skills, alpha within."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "zeta", {"SKILL.md": b"\n"}, pin)
    _setup_installed_at_pin(tmp_path, "skills", "alpha", {"SKILL.md": b"\n"}, pin)
    _setup_installed_at_pin(tmp_path, "agents", "bar", {"agent.md": b"\n"}, pin)
    _setup_installed_at_pin(tmp_path, "commands", "baz", {"command.md": b"\n"}, pin)

    _, rows = classify_status(tmp_path)

    types = [r.asset_type for r in rows]
    names_skills = [r.name for r in rows if r.asset_type == "skills"]
    assert types == ["agents", "commands", "skills", "skills"]
    assert names_skills == ["alpha", "zeta"]


def test_no_write_invariant(monkeypatch, wiki_root: Path, tmp_path: Path) -> None:
    """classify_status must NEVER write — atomic_write_bytes is the only mutation primitive."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, pin)

    write_calls: list[tuple] = []

    original = atomic_write_bytes

    def fail_write(*args, **kwargs):
        write_calls.append((args, kwargs))
        return original(*args, **kwargs)

    import memtomem.context._atomic as _atomic_mod

    monkeypatch.setattr(_atomic_mod, "atomic_write_bytes", fail_write)
    monkeypatch.setattr("memtomem.context.lockfile.atomic_write_bytes", fail_write)

    classify_status(tmp_path)

    assert write_calls == []


# ── CLI tests ────────────────────────────────────────────────────────────


def test_cli_status_empty_lockfile_exits_0(
    wiki_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    _initialized_wiki(wiki_root)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])
    assert result.exit_code == 0
    assert "No wiki assets installed" in result.output
    assert "scope project_shared" in result.output


def test_cli_status_renders_states(
    wiki_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, pin)

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])

    assert result.exit_code == 0
    assert "skills" in result.output
    assert "foo" in result.output
    assert pin[:12] in result.output
    assert "Summary:" in result.output
    assert "1 ok" in result.output


def test_cli_status_corrupt_lockfile_exits_1(
    wiki_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    _initialized_wiki(wiki_root)
    lockfile_path = tmp_path / ".memtomem" / "lock.json"
    lockfile_path.parent.mkdir(parents=True)
    lockfile_path.write_text(json.dumps({"version": 999, "skills": {}}))

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])

    assert result.exit_code == 1
    assert "lock.json" in result.output


def test_cli_status_invalid_json_lockfile_exits_1_no_traceback(
    wiki_root: Path,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Outright JSON corruption degrades the same way the version mismatch
    does — error message + exit 1, never a traceback (#1247 id 16: status is
    read-only, so it reports instead of refusing)."""
    _initialized_wiki(wiki_root)
    lockfile_path = tmp_path / ".memtomem" / "lock.json"
    lockfile_path.parent.mkdir(parents=True)
    lockfile_path.write_text("not valid json {{", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])

    assert result.exit_code == 1
    assert "lock.json" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_cli_status_wiki_absent_renders_with_annotation(
    wiki_root: Path,  # fixture sets MEMTOMEM_WIKI_PATH but we don't init
    tmp_path: Path,
    monkeypatch,
) -> None:
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, "0" * 40)
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])

    assert result.exit_code == 0
    assert "wiki not present" in result.output
    assert "foo" in result.output


# ── project_local draft scan tests (#923) ────────────────────────────────


def _seed_local_draft(project: Path, asset_type: str, name: str, manifest: str) -> None:
    """Create ``<proj>/.memtomem/<asset_type>.local/<name>/<manifest>``.

    Matches the canonical-side path scope_resolver.canonical_artifact_dir
    resolves for ``project_local``. The manifest file is what
    classify_status's draft scanner uses to confirm the dir is a real
    artifact (same probe migrate uses).
    """
    draft_dir = project / ".memtomem" / f"{asset_type}.local" / name
    draft_dir.mkdir(parents=True)
    (draft_dir / manifest).write_bytes(b"# draft\n")


def test_classify_status_scans_project_local_drafts(wiki_root: Path, tmp_path: Path) -> None:
    """All three artifact kinds produce project_local rows from real on-disk dirs."""
    _initialized_wiki(wiki_root)
    _seed_local_draft(tmp_path, "agents", "draft-agent", "agent.md")
    _seed_local_draft(tmp_path, "commands", "draft-cmd", "command.md")
    _seed_local_draft(tmp_path, "skills", "draft-skill", "SKILL.md")

    _, rows = classify_status(tmp_path)

    by_kind = {(r.asset_type, r.name): r for r in rows}
    assert ("agents", "draft-agent") in by_kind
    assert ("commands", "draft-cmd") in by_kind
    assert ("skills", "draft-skill") in by_kind
    for row in (
        by_kind["agents", "draft-agent"],
        by_kind["commands", "draft-cmd"],
        by_kind["skills", "draft-skill"],
    ):
        assert row.tier == "project_local"
        assert row.state == "local-draft"
        assert row.pin_commit == ""
        assert row.installed_at == ""
        assert row.reason is None


def test_draft_scan_skips_staging_leftovers(wiki_root: Path, tmp_path: Path) -> None:
    """Crash-leftover staging trees under a draft-tier canonical root must
    not surface as local-draft rows (#1229)."""
    _initialized_wiki(wiki_root)
    _seed_local_draft(tmp_path, "skills", ".staging-x-99999-abc123.tmp", "SKILL.md")

    _, rows = classify_status(tmp_path)

    assert not any(r.name == ".staging-x-99999-abc123.tmp" for r in rows)


def test_classify_status_skips_directories_missing_kind_manifest(
    wiki_root: Path, tmp_path: Path
) -> None:
    """A directory under ``agents.local/`` without ``agent.md`` is NOT emitted.

    Mirrors migrate._detect_source_scope's contract: a directory only
    counts as an artifact when its kind-specific manifest is present.
    """
    _initialized_wiki(wiki_root)
    bogus = tmp_path / ".memtomem" / "agents.local" / "no-manifest-here"
    bogus.mkdir(parents=True)
    (bogus / "README.md").write_bytes(b"# not the manifest\n")

    _, rows = classify_status(tmp_path)

    assert rows == []


def test_classify_status_absent_local_dirs_do_not_crash(wiki_root: Path, tmp_path: Path) -> None:
    """No ``.local/`` directories on disk → scanner yields nothing, no error."""
    _initialized_wiki(wiki_root)

    _, rows = classify_status(tmp_path)

    assert rows == []


def _seed_local_flat_draft(project: Path, asset_type: str, name: str) -> None:
    """Create a flat-layout draft ``<proj>/.memtomem/<asset_type>.local/<name>.md``.

    Flat layout is the legacy single-file shape for agents and commands
    (skills have always been dir-only). Used by tests covering the
    flat-layout side of the project_local scan.
    """
    local_root = project / ".memtomem" / f"{asset_type}.local"
    local_root.mkdir(parents=True, exist_ok=True)
    (local_root / f"{name}.md").write_bytes(b"# flat draft\n")


def test_classify_status_scans_flat_layout_agents_and_commands(
    wiki_root: Path, tmp_path: Path
) -> None:
    """Flat-layout drafts at ``.local/<name>.md`` are emitted for agents and commands.

    Mirrors migrate._detect_source_scope's dual-layout recognition. Skills
    are dir-only and covered by the sibling skip test.
    """
    _initialized_wiki(wiki_root)
    _seed_local_flat_draft(tmp_path, "agents", "flat-agent")
    _seed_local_flat_draft(tmp_path, "commands", "flat-cmd")

    _, rows = classify_status(tmp_path)

    by_kind = {(r.asset_type, r.name): r for r in rows}
    assert ("agents", "flat-agent") in by_kind
    assert ("commands", "flat-cmd") in by_kind
    for row in (by_kind["agents", "flat-agent"], by_kind["commands", "flat-cmd"]):
        assert row.tier == "project_local"
        assert row.state == "local-draft"


def test_classify_status_skills_flat_layout_is_not_recognised(
    wiki_root: Path, tmp_path: Path
) -> None:
    """A ``.md`` at the top of ``skills.local/`` is NOT emitted — skills are dir-only.

    Pins parity with migrate._detect_source_scope's ``if kind == "skills":
    continue`` after the dir probe (migrate.py:792).
    """
    _initialized_wiki(wiki_root)
    _seed_local_flat_draft(tmp_path, "skills", "bogus-flat-skill")

    _, rows = classify_status(tmp_path)

    assert rows == []


def test_classify_status_dir_layout_shadows_flat_sibling(wiki_root: Path, tmp_path: Path) -> None:
    """When a name has both dir and flat layout in the same .local/, dir wins.

    Mirrors the ``continue`` in migrate._detect_source_scope after a
    successful dir match — the flat sibling is silently shadowed to
    avoid a duplicate row.
    """
    _initialized_wiki(wiki_root)
    _seed_local_draft(tmp_path, "agents", "twin", "agent.md")
    _seed_local_flat_draft(tmp_path, "agents", "twin")

    _, rows = classify_status(tmp_path)

    twin_rows = [r for r in rows if r.asset_type == "agents" and r.name == "twin"]
    assert len(twin_rows) == 1
    assert twin_rows[0].tier == "project_local"


def test_classify_status_shows_both_rows_on_name_collision(wiki_root: Path, tmp_path: Path) -> None:
    """Same name in lock.json and .local/ → two rows, project_shared before project_local."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, pin)
    _seed_local_draft(tmp_path, "skills", "foo", "SKILL.md")

    _, rows = classify_status(tmp_path)

    foo_rows = [r for r in rows if r.asset_type == "skills" and r.name == "foo"]
    assert len(foo_rows) == 2
    assert foo_rows[0].tier == "project_shared"
    assert foo_rows[0].state == "ok"
    assert foo_rows[1].tier == "project_local"
    assert foo_rows[1].state == "local-draft"


def test_cli_status_renders_draft_no_fanout_annotation(
    wiki_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """CLI output for a project_local row carries the exact ``(draft, no fan-out)`` string."""
    _initialized_wiki(wiki_root)
    _seed_local_draft(tmp_path, "agents", "draft-only", "agent.md")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status", "--scope", "project_local"])

    assert result.exit_code == 0
    assert "draft-only" in result.output
    assert "(draft, no fan-out)" in result.output
    assert "1 local-draft" in result.output
    assert "scope project_local" in result.output


def test_cli_status_default_hides_project_local_drafts(
    wiki_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """Default status view stays on project_shared; project_local is opt-in."""
    _initialized_wiki(wiki_root)
    _seed_local_draft(tmp_path, "agents", "draft-only", "agent.md")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])

    assert result.exit_code == 0
    assert "draft-only" not in result.output
    assert "(draft, no fan-out)" not in result.output


def test_cli_status_no_annotation_on_project_shared_rows(
    wiki_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """The annotation is absent for lockfile-tracked (project_shared) rows."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "shared-skill", {"SKILL.md": b"v1\n"})
    _setup_installed_at_pin(tmp_path, "skills", "shared-skill", {"SKILL.md": b"v1\n"}, pin)

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])

    assert result.exit_code == 0
    assert "shared-skill" in result.output
    assert "(draft, no fan-out)" not in result.output


# ── mm context diff --scope tests (#936) ────────────────────────────────


def test_cli_diff_default_scope_emits_project_shared_suffix(tmp_path: Path, monkeypatch) -> None:
    """`mm context diff` defaults to project_shared and threads it to helpers.

    Empty-state messages from ``_print_{skills,agents,commands}_diff`` echo
    the active scope, so an unseeded project surfaces the default scope on
    every kind. Pins the CLI-level default.
    """
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(context_group, ["diff", "--include", "skills,agents,commands"])

    assert result.exit_code == 0, result.output
    assert "(no skills to compare in project_shared)" in result.output
    assert "(no sub-agents to compare in project_shared)" in result.output
    assert "(no commands to compare in project_shared)" in result.output


def test_cli_diff_scope_project_local_threads_to_helpers(tmp_path: Path, monkeypatch) -> None:
    """`mm context diff --scope=project_local` plumbs the flag into diff_*.

    project_local has no runtime fan-out (ADR-0011 PR-E3) so diff lists are
    empty even with a seeded draft — what we assert here is the suffix change
    from the default test, which proves the CLI flag reaches the diff_*
    callsites (not just the empty-message format string).
    """
    (tmp_path / ".git").mkdir()
    _seed_local_draft(tmp_path, "skills", "draft-skill", "SKILL.md")
    monkeypatch.chdir(tmp_path)
    runner = CliRunner()

    result = runner.invoke(
        context_group,
        ["diff", "--scope", "project_local", "--include", "skills,agents,commands"],
    )

    assert result.exit_code == 0, result.output
    assert "(no skills to compare in project_local)" in result.output
    assert "(no sub-agents to compare in project_local)" in result.output
    assert "(no commands to compare in project_local)" in result.output


# ── unused-fixture helpers (silence ruff F401) ───────────────────────────

_ = pytest  # keep pytest import alive if no test uses it directly


# Type-check helper to ensure StatusRow shape stays exported (catches refactors).
def test_status_row_dataclass_shape() -> None:
    row = StatusRow(
        asset_type="skills",
        name="foo",
        pin_commit="0" * 40,
        installed_at="2026-05-01T00:00:00Z",
        state="ok",
        dirty_file_count=0,
        reason=None,
    )
    assert row.asset_type == "skills"
    assert row.dirty_file_count == 0
    # New tier field defaults to project_shared so existing call sites
    # (and the lockfile-walk branch in classify_status) remain valid
    # without explicit tier= updates.
    assert row.tier == "project_shared"


# ── migrate drops the dangling lockfile entry (#1123 B4-1) ─────────────────


def test_status_no_missing_after_scope_migration_drops_lockfile_entry(
    monkeypatch, wiki_root: Path, tmp_path: Path
) -> None:
    """Migrating a project_shared install out of project_shared must drop
    its lock.json entry, so status no longer iterates a stale entry and
    reports the moved artifact as 'missing' (#1123 B4-1)."""
    set_home(monkeypatch, tmp_path / "home")
    _initialized_wiki(wiki_root)
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, "a" * 40)
    assert Lockfile.at(tmp_path).read_entry("skills", "foo") is not None

    migrate_scope(
        "skills",
        "foo",
        from_scope="project_shared",
        to_scope="project_local",
        project_root=tmp_path,
        apply_=True,
    )

    # Lockfile entry is gone …
    assert Lockfile.at(tmp_path).read_entry("skills", "foo") is None
    # … and status surfaces only the project_local draft — no 'missing' row.
    _, rows = classify_status(tmp_path)
    foo_rows = [r for r in rows if r.asset_type == "skills" and r.name == "foo"]
    assert [r.state for r in foo_rows] == ["local-draft"]
    assert all(r.state != "missing" for r in rows)


def test_status_no_missing_after_scope_migration_to_user_drops_lockfile_entry(
    monkeypatch, wiki_root: Path, tmp_path: Path
) -> None:
    """The same lock.json cleanup must apply on the other project_shared exit
    path — migrating project_shared → user (#1123 B4-1)."""
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    _initialized_wiki(wiki_root)
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"v1\n"}, "a" * 40)
    assert Lockfile.at(tmp_path).read_entry("skills", "foo") is not None

    migrate_scope(
        "skills",
        "foo",
        from_scope="project_shared",
        to_scope="user",
        project_root=tmp_path,
        apply_=True,
    )

    # Lockfile entry dropped; status surfaces no 'missing' row …
    assert Lockfile.at(tmp_path).read_entry("skills", "foo") is None
    _, rows = classify_status(tmp_path)
    assert all(r.state != "missing" for r in rows)
    # … and the artifact now lives in the user tier.
    user_keys = {(r.asset_type, r.name) for r in scan_user_artifacts()}
    assert ("skills", "foo") in user_keys


# ── user-tier scan (#1123 B4-2) ────────────────────────────────────────────


def _seed_user_artifact_dir(home: Path, asset_type: str, name: str, manifest: str) -> None:
    """Create ``~/.memtomem/<asset_type>/<name>/<manifest>`` under *home*."""
    d = home / ".memtomem" / asset_type / name
    d.mkdir(parents=True)
    (d / manifest).write_bytes(b"# user draft\n")


def test_scan_user_artifacts_recognises_dir_and_flat_layout(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    _seed_user_artifact_dir(home, "agents", "dir-agent", "agent.md")
    (home / ".memtomem" / "agents" / "flat-agent.md").write_bytes(b"# flat\n")
    _seed_user_artifact_dir(home, "skills", "myskill", "SKILL.md")

    rows = list(scan_user_artifacts())

    keys = {(r.asset_type, r.name) for r in rows}
    assert ("agents", "dir-agent") in keys
    assert ("agents", "flat-agent") in keys  # flat .md recognised for agents
    assert ("skills", "myskill") in keys
    for r in rows:
        assert r.tier == "user"
        assert r.state == "local-draft"
        assert r.pin_commit == "" and r.installed_at == ""


def test_scan_user_artifacts_skills_flat_layout_not_recognised(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    (home / ".memtomem" / "skills").mkdir(parents=True)
    (home / ".memtomem" / "skills" / "loose.md").write_bytes(b"# not a skill\n")

    assert list(scan_user_artifacts()) == []


def test_classify_status_stays_project_rooted_no_user_tier(monkeypatch, tmp_path: Path) -> None:
    """classify_status must NOT scan ~/.memtomem — folding the global user
    tier into it would couple every status read (and test) to the caller's
    real home. User rows are the CLI's job (#1123 B4-2)."""
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    _seed_user_artifact_dir(home, "agents", "u", "agent.md")

    _, rows = classify_status(tmp_path)

    assert all(r.tier != "user" for r in rows)


def test_cli_status_user_scope_lists_user_artifacts(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()
    _seed_user_artifact_dir(home, "agents", "myagent", "agent.md")

    result = CliRunner().invoke(
        context_group, ["status", "--scope", "user"], catch_exceptions=False
    )

    assert result.exit_code == 0
    assert "myagent" in result.output
    assert "scope user" in result.output


def test_cli_status_user_scope_empty_message(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    set_home(monkeypatch, home)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".git").mkdir()

    result = CliRunner().invoke(
        context_group, ["status", "--scope", "user"], catch_exceptions=False
    )

    assert result.exit_code == 0
    assert "No user-scope assets found" in result.output


# ── flat-layout reason (#1247 id 0) ──────────────────────────────────────


def _seed_flat_installed(project: Path, asset_type: str, name: str, pin: str) -> Path:
    """Seed the #1247 id 0 population: flat ``<type>/<name>.md`` + lock entry, no dir."""
    flat = project / ".memtomem" / asset_type / f"{name}.md"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_bytes(b"# flat\n")
    installed_at = datetime.fromtimestamp(flat.stat().st_mtime, tz=timezone.utc).isoformat()
    Lockfile.at(project).upsert_entry(asset_type, name, wiki_commit=pin, installed_at=installed_at)
    return flat


def test_status_flat_layout_row_points_at_migrate(wiki_root: Path, tmp_path: Path) -> None:
    """#1247 id 0: a flat-layout install must not render as "dest missing" —
    the flat file exists and is what fan-out actually serves. The reason
    points at the migrate verb instead."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "anchor", {"SKILL.md": b"x\n"})
    _seed_flat_installed(tmp_path, "agents", "foo", pin)

    _wiki_head, rows = classify_status(tmp_path)

    [row] = [r for r in rows if r.asset_type == "agents" and r.name == "foo"]
    assert row.state == "missing"
    assert row.tier == "project_shared"
    assert "migrate agent foo" in (row.reason or "")
    assert "dest missing" not in (row.reason or "")


def test_status_missing_without_flat_keeps_dest_missing_reason(
    wiki_root: Path, tmp_path: Path
) -> None:
    """Negative pin for the flat hint: no flat sibling → reason unchanged."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"x\n"})
    _setup_installed_at_pin(tmp_path, "agents", "bar", {"agent.md": b"x\n"}, pin)
    shutil.rmtree(tmp_path / ".memtomem" / "agents" / "bar")

    _wiki_head, rows = classify_status(tmp_path)

    [row] = [r for r in rows if r.name == "bar"]
    assert row.state == "missing"
    assert row.reason == "dest missing"


# ── malformed installed_at end-to-end (#1247 id 1) ───────────────────────


def test_classify_status_survives_malformed_installed_at(wiki_root: Path, tmp_path: Path) -> None:
    """One corrupt entry must not crash the whole status walk — the healthy
    sibling still classifies and the corrupt row degrades to missing."""
    _initialized_wiki(wiki_root)
    pin_a = _seed_wiki_skill(wiki_root, "alpha", {"SKILL.md": b"a\n"})
    _setup_installed_at_pin(tmp_path, "skills", "alpha", {"SKILL.md": b"a\n"}, pin_a)
    pin_b = _seed_wiki_skill(wiki_root, "beta", {"SKILL.md": b"b\n"})
    _setup_installed_at_pin(tmp_path, "skills", "beta", {"SKILL.md": b"b\n"}, pin_b)

    lock_path = tmp_path / ".memtomem" / "lock.json"
    doc = json.loads(lock_path.read_text(encoding="utf-8"))
    doc["skills"]["alpha"]["installed_at"] = "yesterday"
    lock_path.write_text(json.dumps(doc), encoding="utf-8")

    _wiki_head, rows = classify_status(tmp_path)  # pre-fix: ValueError escaped

    by_name = {r.name: r for r in rows}
    assert by_name["beta"].state == "ok"
    assert by_name["alpha"].state == "missing"


# ── untracked project_shared canonicals (#1247 id 8) ─────────────────────


def test_classify_status_lists_untracked_project_shared_canonicals(
    wiki_root: Path, tmp_path: Path
) -> None:
    """init-imported / migrate-moved-in canonicals (no lockfile entry) must
    surface as state="untracked" — they are actively served by sync fan-out,
    and ADR-0016 §6 names status as the per-tier inspection surface."""
    _initialized_wiki(wiki_root)
    agent_dir = tmp_path / ".memtomem" / "agents" / "foo"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.md").write_text("# foo\n", encoding="utf-8")
    flat_cmd = tmp_path / ".memtomem" / "commands" / "bar.md"
    flat_cmd.parent.mkdir(parents=True)
    flat_cmd.write_text("# bar\n", encoding="utf-8")

    _wiki_head, rows = classify_status(tmp_path)  # pre-fix: zero rows

    by_key = {(r.asset_type, r.name): r for r in rows}
    foo = by_key[("agents", "foo")]
    assert foo.state == "untracked"
    assert foo.tier == "project_shared"
    assert foo.pin_commit == "" and foo.installed_at == ""
    assert "not lockfile-tracked" in (foo.reason or "")
    bar = by_key[("commands", "bar")]
    assert bar.state == "untracked"


def test_untracked_scan_skips_lockfile_tracked_names(wiki_root: Path, tmp_path: Path) -> None:
    """A lockfile-tracked install renders exactly one row — the untracked
    scan must not double-report it."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"x\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"x\n"}, pin)

    _wiki_head, rows = classify_status(tmp_path)

    rows_for_foo = [r for r in rows if r.name == "foo"]
    assert len(rows_for_foo) == 1
    assert rows_for_foo[0].state != "untracked"


def test_untracked_scan_skips_tracked_flat_population(wiki_root: Path, tmp_path: Path) -> None:
    """The id 0 population (flat + entry) renders as the lockfile "missing"
    row with the migrate hint — not additionally as untracked."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "anchor", {"SKILL.md": b"x\n"})
    _seed_flat_installed(tmp_path, "agents", "foo", pin)

    _wiki_head, rows = classify_status(tmp_path)

    rows_for_foo = [r for r in rows if r.name == "foo"]
    assert len(rows_for_foo) == 1
    assert rows_for_foo[0].state == "missing"


def test_cli_status_renders_untracked_rows(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    _initialized_wiki(wiki_root)
    agent_dir = tmp_path / ".memtomem" / "agents" / "foo"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.md").write_text("# foo\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])

    assert result.exit_code == 0
    assert "foo" in result.output
    assert "0 asset(s) installed (+ 1 untracked)" in result.output
    assert "1 untracked" in result.output
    assert "No wiki assets installed" not in result.output


# ── degraded wiki (#1247 id 9) ───────────────────────────────────────────


def test_classify_status_degrades_when_wiki_has_no_head(wiki_root: Path, tmp_path: Path) -> None:
    """A wiki with .git but no HEAD (clone of an empty remote) must degrade
    like the absent-wiki case, not escape as a RuntimeError traceback."""
    wiki_root.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", str(wiki_root), "init", "-b", "main"], check=True, capture_output=True
    )
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"x\n"}, "0" * 40)

    wiki_head, rows = classify_status(tmp_path)  # pre-fix: RuntimeError escaped

    assert wiki_head is None
    [row] = rows
    assert row.state == "stale-pin"
    assert "wiki unusable" in (row.reason or "")


def test_classify_status_degrades_when_git_binary_missing(
    wiki_root: Path, tmp_path: Path, monkeypatch
) -> None:
    """git vanishing from PATH surfaces as OSError from subprocess — same
    degrade path (the probe's OSError arm)."""
    _initialized_wiki(wiki_root)
    pin = _seed_wiki_skill(wiki_root, "foo", {"SKILL.md": b"x\n"})
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"x\n"}, pin)

    def _no_git(*args: object, **kwargs: object) -> None:
        raise FileNotFoundError("git not found")

    monkeypatch.setattr("memtomem.wiki.store.subprocess.run", _no_git)

    wiki_head, rows = classify_status(tmp_path)

    assert wiki_head is None
    [row] = rows
    assert row.state == "stale-pin"
    assert "wiki unusable" in (row.reason or "")


def test_cli_status_degraded_wiki_exits_zero(wiki_root: Path, tmp_path: Path, monkeypatch) -> None:
    """Exit-0 contract: status is the diagnostic verb; a degraded wiki must
    render an annotated header, not a traceback (pre-fix: exit 1 + raw
    RuntimeError). The header must say unusable, not "not present" — the
    wiki IS on disk."""
    wiki_root.mkdir(parents=True)
    subprocess.run(
        ["git", "-C", str(wiki_root), "init", "-b", "main"], check=True, capture_output=True
    )
    _setup_installed_at_pin(tmp_path, "skills", "foo", {"SKILL.md": b"x\n"}, "0" * 40)

    monkeypatch.chdir(tmp_path)
    runner = CliRunner()
    result = runner.invoke(context_group, ["status"])

    assert result.exit_code == 0, result.output
    assert "present but unusable" in result.output
    assert "wiki not present" not in result.output
    assert "stale-pin" in result.output
