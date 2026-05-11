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
from memtomem.context.status import StatusRow, classify_status
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
