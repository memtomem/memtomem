"""``apply_hook_copy`` stops before its transaction once its caller is gone (#2247).

The sibling of ``test_settings_sync_abandon.py``, and the case where the abort
rule has a hard limit. ``apply_hook_copy`` writes the destination canonical and
then the destination tier, and the module's whole reason for holding both
sidecar locks at once is that a tier-only or canonical-only result is a broken
copy the next sync would either prune or misread. So the checks sit before the
first write and nowhere else: an abandoned caller either gets nothing written
or gets the whole transaction, never half of it.

These drive the flag directly — no thread, no cancelled task — so each check
placement is pinned on its own. Everything stays under ``tmp_path`` in
``project_shared`` scope, so no host home is resolved.
"""

from __future__ import annotations

import json

import pytest

from memtomem.context import settings_copy as copy_mod
from memtomem.context._abandon import abandon_sync_on_exit, sync_is_abandoned
from memtomem.context.settings import CANONICAL_SETTINGS_FILE
from memtomem.context.settings_copy import apply_hook_copy, plan_hook_copy


def _inner(command: str = "mm session start") -> dict:
    return {"type": "command", "command": command, "timeout": 5000}


def _write_doc(path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def src_project(tmp_path):
    root = tmp_path / "src-proj"
    root.mkdir()
    _write_doc(
        root / CANONICAL_SETTINGS_FILE,
        {"hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [_inner()]}]}},
    )
    return root


@pytest.fixture
def dst_project(tmp_path):
    root = tmp_path / "dst-proj"
    (root / ".memtomem").mkdir(parents=True)
    return root


def _plan(src, dst):
    return plan_hook_copy(
        src,
        event="PostToolUse",
        matcher="Edit|Write",
        dst_project_root=dst,
        dst_scope="project_shared",
    )


def _dst_paths(plan):
    return plan.dst_canonical_path, plan.dst_target_path


def test_a_copy_abandoned_before_it_starts_writes_nothing(src_project, dst_project):
    plan = _plan(src_project, dst_project)
    canonical, target = _dst_paths(plan)

    with abandon_sync_on_exit() as abandoned:
        abandoned.set()
        result = apply_hook_copy(plan)

    assert not result.canonical_written and not result.target_written, result
    assert not canonical.exists() and not target.exists()
    assert any("abandoned by its caller" in w for w in result.warnings), result.warnings
    # ``needs_sync`` would tell the user to run a follow-up sync for a copy
    # that never happened.
    assert result.needs_sync is False
    # The entry check runs before Gate A and before any lock, so an abandoned
    # copy leaves no sidecar behind either.
    assert not list(dst_project.glob("**/.settings.json.lock"))


def test_abandonment_after_the_locks_still_stops_before_the_first_write(
    src_project, dst_project, monkeypatch
):
    """The post-lock check, not just the entry one.

    Waiting for the pair of locks is the longest this call can take, so it is
    where a caller most plausibly gives up — and it is still early enough that
    nothing has been written.
    """
    plan = _plan(src_project, dst_project)
    canonical, target = _dst_paths(plan)
    real_scan = copy_mod.gate_a_scan

    with abandon_sync_on_exit() as abandoned:

        def _scan_then_abandon(plan_arg, surface):
            real_scan(plan_arg, surface)
            abandoned.set()

        monkeypatch.setattr(copy_mod, "gate_a_scan", _scan_then_abandon)
        result = apply_hook_copy(plan)

    assert not result.canonical_written and not result.target_written, result
    assert not canonical.exists() and not target.exists()
    assert any("abandoned by its caller" in w for w in result.warnings), result.warnings


def test_abandonment_during_the_reads_stops_before_the_first_write(
    src_project, dst_project, monkeypatch
):
    """The pre-mutation check, not just the post-lock one.

    Both destination legs are read and validated under the locks before either
    is classified, which is long enough for a caller to give up — and still
    early enough that nothing has been written. Without this check the copy
    would apply its whole transaction behind a response that already failed.
    """
    plan = _plan(src_project, dst_project)
    canonical, target = _dst_paths(plan)
    real_read = copy_mod._read_with_mtime

    with abandon_sync_on_exit() as abandoned:

        def _read_then_abandon(path):
            out = real_read(path)
            abandoned.set()
            return out

        monkeypatch.setattr(copy_mod, "_read_with_mtime", _read_then_abandon)
        result = apply_hook_copy(plan)

    assert not result.canonical_written and not result.target_written, result
    assert not canonical.exists() and not target.exists()
    assert any("abandoned by its caller" in w for w in result.warnings), result.warnings


def test_abandonment_inside_the_transaction_still_writes_both_legs(
    src_project, dst_project, monkeypatch
):
    """The limit of the abort: past the last check, the transaction finishes.

    A caller giving up between the canonical write and the tier write must not
    leave the copy half-applied — a canonical-only copy is pruned by the next
    sync and a tier-only one is the self-destructing state the pair-lock exists
    to prevent. Finishing is the correct outcome, so this test fails if someone
    adds a check between the legs.
    """
    plan = _plan(src_project, dst_project)
    canonical, target = _dst_paths(plan)
    real_write = copy_mod._write_json

    with abandon_sync_on_exit() as abandoned:

        def _write_then_abandon(path, doc):
            real_write(path, doc)
            abandoned.set()

        monkeypatch.setattr(copy_mod, "_write_json", _write_then_abandon)
        result = apply_hook_copy(plan)

    assert result.canonical_written, result
    assert result.target_written, (
        "the tier leg was skipped after the canonical was written — an "
        "abandoned copy must finish its transaction, not leave half of it"
    )
    assert canonical.exists() and target.exists()


def test_a_copy_nobody_abandoned_applies_normally(src_project, dst_project):
    """The default: the CLI never enters the scope, so nothing changes for it."""
    plan = _plan(src_project, dst_project)
    canonical, target = _dst_paths(plan)

    result = apply_hook_copy(plan)

    assert result.canonical_written and result.target_written, result
    assert canonical.exists() and target.exists()
    assert not any("abandoned" in w for w in result.warnings), result.warnings


def test_leaving_the_scope_normally_does_not_abandon_the_next_copy(src_project, dst_project):
    """A healthy request exits the scope exactly as a cancelled one does."""
    with abandon_sync_on_exit():
        pass

    assert sync_is_abandoned() is False
    result = apply_hook_copy(_plan(src_project, dst_project))
    assert result.canonical_written and result.target_written, result
