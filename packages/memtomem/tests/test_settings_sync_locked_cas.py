"""Cross-process locked compare-and-swap for the inline settings-rule routes.

``resolve`` / ``delete`` / ``promote`` hold only the in-process
``_gateway_lock``, which is invisible to a separate-process writer (a CLI
``mm context sync`` or ``apply_hook_copy``). Their write now goes through
:func:`_locked_cas_write`, which takes the per-file sidecar ``_file_lock`` (the
SAME lock ``generate_all_settings`` / ``apply_hook_copy`` hold across their
read-merge-write of these files) plus an mtime compare-and-swap, so a
concurrent cross-process write landing between the route's read and write is
detected and REFUSED instead of silently clobbered (#1123 B3-3 shape).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memtomem.context._atomic import _file_lock, _lock_path_for
from memtomem.web.routes import settings_sync
from memtomem.web.routes.settings_sync import _locked_cas_write


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_writes_when_mtime_matches(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    expected = path.stat().st_mtime_ns

    wrote, new_mtime = _locked_cas_write(path, expected, {"hooks": {"PostToolUse": []}})

    assert wrote is True
    assert new_mtime == path.stat().st_mtime_ns
    assert _read(path) == {"hooks": {"PostToolUse": []}}


def test_refuses_and_preserves_bytes_when_mtime_changed(tmp_path: Path) -> None:
    """A concurrent writer changed the file after the route's read: the CAS
    refuses (no clobber) and echoes the current mtime for the client refresh."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"original": True}), encoding="utf-8")
    # The route captured an mtime that no longer matches the file on disk.
    stale_expected = path.stat().st_mtime_ns - 1

    wrote, current = _locked_cas_write(path, stale_expected, {"clobber": True})

    assert wrote is False
    assert current == path.stat().st_mtime_ns
    assert _read(path) == {"original": True}  # NOT clobbered


def test_fresh_create_when_absent(tmp_path: Path) -> None:
    """``expected=None`` is the absent-at-read case (a fresh canonical create);
    it writes and makes the parent dir."""
    path = tmp_path / "sub" / "settings.json"

    wrote, new_mtime = _locked_cas_write(path, None, {"hooks": {}})

    assert wrote is True
    assert _read(path) == {"hooks": {}}
    assert new_mtime == path.stat().st_mtime_ns


def test_refuses_when_file_appeared_after_absent_read(tmp_path: Path) -> None:
    """Absent at read (``expected=None``) but a file appeared cross-process
    before our locked write: caught, not overwritten."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"appeared": True}), encoding="utf-8")

    wrote, current = _locked_cas_write(path, None, {"clobber": True})

    assert wrote is False
    assert current == path.stat().st_mtime_ns
    assert _read(path) == {"appeared": True}


def test_participates_in_sidecar_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The CAS acquires the per-file sidecar ``_file_lock``: while another
    holder (a separate fd, standing in for the cross-process CLI writer) holds
    it, the write blocks to the budget and raises ``TimeoutError`` — proving
    real mutual exclusion, not just the in-process ``_gateway_lock`` check.
    Nothing is written under contention."""
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"hooks": {}}), encoding="utf-8")
    expected = path.stat().st_mtime_ns
    monkeypatch.setattr(settings_sync, "_SETTINGS_LOCK_BUDGET_S", 0.2)

    with _file_lock(_lock_path_for(path)):
        with pytest.raises(TimeoutError):
            _locked_cas_write(path, expected, {"hooks": {"X": []}})

    assert _read(path) == {"hooks": {}}  # never written while contended
