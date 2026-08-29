"""``apply_migration`` stops before its transaction once its caller is gone (#2247).

Defensive coverage: every caller today is the CLI, which never enters
``abandon_sync_on_exit`` and therefore never sees a set flag. The checks exist
so the first threaded dispatcher inherits a placement chosen against this
engine's transaction rather than picking its own — and these tests are what
make that placement a contract instead of a comment.

The placement is the whole point. ``apply_migration`` writes the target tier
and then cleans the source, holding both sidecar locks precisely because an
interruption between the two is what loses an entry from BOTH tiers. So the
checks sit before the target write and nowhere after it: an abandoned caller
gets nothing written, or gets the entire move.
"""

from __future__ import annotations

import json

import pytest

from memtomem.context import settings_migrate as migrate_mod
from memtomem.context._abandon import abandon_sync_on_exit, sync_is_abandoned
from memtomem.context.settings import CANONICAL_SETTINGS_FILE
from memtomem.context.settings_migrate import apply_migration, plan_migration

from .helpers import set_home


def _inner(command: str = "mm session start") -> dict:
    return {"type": "command", "command": command, "timeout": 5000}


def _hooks() -> dict:
    return {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [_inner()]}]}


def _write(path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def project_root(tmp_path):
    root = tmp_path / "project"
    (root / ".git").mkdir(parents=True)
    (root / ".claude").mkdir()
    return root


@pytest.fixture
def plan(project_root, fake_home):
    """A one-move user → project_local migration, ready to apply."""
    _write(project_root / CANONICAL_SETTINGS_FILE, {"hooks": _hooks()})
    _write(fake_home / ".claude" / "settings.json", {"hooks": _hooks()})
    built = plan_migration(project_root, source_scope="user", target_scope="project_local")
    assert len(built.moves) == 1, built
    return built


def _target_hooks(plan) -> dict:
    return json.loads(plan.target_path.read_text(encoding="utf-8")).get("hooks", {})


def _source_hooks(plan) -> dict:
    return json.loads(plan.source_path.read_text(encoding="utf-8")).get("hooks", {})


def test_a_migration_abandoned_before_it_starts_writes_nothing(plan):
    with abandon_sync_on_exit() as abandoned:
        abandoned.set()
        result = apply_migration(plan)

    assert not result.target_written and not result.source_written, result
    assert not plan.target_path.exists(), "the target tier was created anyway"
    assert _source_hooks(plan) == _hooks(), "the source was cleaned despite the abort"
    assert any("abandoned by its caller" in w for w in result.warnings), result.warnings
    # Entry check runs before lock acquisition, so no sidecar is left behind.
    assert not list(plan.target_path.parent.glob(".*.lock"))


def test_abandonment_while_waiting_for_the_locks_stops_before_the_reads(plan, monkeypatch):
    """The post-lock check, distinguished from the pre-write one.

    Waiting for the pair of sidecar locks is the longest this apply can take,
    so it is where a caller most plausibly gives up. The pre-write check would
    also stop this run, which is why the assertion is that no tier was even
    *read*: only the post-lock check can make that true.
    """
    real_lock = migrate_mod._file_lock
    real_read = migrate_mod._read_with_mtime
    reads: list = []

    with abandon_sync_on_exit() as abandoned:

        def _lock_then_abandon(path, timeout):
            ctx = real_lock(path, timeout=timeout)
            abandoned.set()
            return ctx

        def _recording_read(path):
            reads.append(path)
            return real_read(path)

        monkeypatch.setattr(migrate_mod, "_file_lock", _lock_then_abandon)
        monkeypatch.setattr(migrate_mod, "_read_with_mtime", _recording_read)
        result = apply_migration(plan)

    assert reads == [], (
        "the apply read the tiers after its caller gave up — the post-lock "
        "check did not fire and only the pre-write one would have stopped it"
    )
    assert not result.target_written and not result.source_written, result
    assert any("abandoned by its caller" in w for w in result.warnings), result.warnings


def test_abandonment_during_the_reclassification_stops_before_the_target_write(plan, monkeypatch):
    """The pre-write check, not just the two earlier ones.

    Re-reading and re-classifying both tiers under the locks takes long enough
    for a caller to give up in between, and that is still before any write.
    """
    real_read = migrate_mod._read_with_mtime

    with abandon_sync_on_exit() as abandoned:

        def _read_then_abandon(path):
            out = real_read(path)
            abandoned.set()
            return out

        monkeypatch.setattr(migrate_mod, "_read_with_mtime", _read_then_abandon)
        result = apply_migration(plan)

    assert not result.target_written and not result.source_written, result
    assert not plan.target_path.exists()
    assert _source_hooks(plan) == _hooks()
    assert any("abandoned by its caller" in w for w in result.warnings), result.warnings


def test_abandonment_between_the_two_writes_still_cleans_the_source(plan, monkeypatch):
    """The limit of the abort: past the last check, the transaction finishes.

    Stopping after the target write and before the source clean-up is the
    duplicate state the pair-lock discipline exists to survive, and doing it
    deliberately would be choosing the failure mode. This test fails if someone
    adds a check between the legs.
    """
    real_write = migrate_mod._write_json

    with abandon_sync_on_exit() as abandoned:

        def _write_then_abandon(path, doc):
            real_write(path, doc)
            abandoned.set()

        monkeypatch.setattr(migrate_mod, "_write_json", _write_then_abandon)
        result = apply_migration(plan)

    assert result.target_written, result
    assert result.source_written, (
        "the source clean-up was skipped after the target write — an abandoned "
        "migration must finish its transaction, not leave the entry in both tiers"
    )
    assert "PostToolUse" in _target_hooks(plan)
    assert _source_hooks(plan).get("PostToolUse", []) == []


def test_a_migration_nobody_abandoned_applies_normally(plan):
    """The default, and the only path any caller takes today (the CLI)."""
    result = apply_migration(plan)

    assert result.target_written and result.source_written, result
    assert not any("abandoned" in w for w in result.warnings), result.warnings
    assert sync_is_abandoned() is False
