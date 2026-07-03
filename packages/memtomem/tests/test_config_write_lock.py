"""Cross-process lock for config.json read-modify-write (issue #1567).

Every writer of ``~/.memtomem/config.json`` does read → merge → atomic write.
``_atomic_write_json`` stops torn JSON, but without a lock across the whole
read→merge→write window two concurrent writers each read the pre-change file
and whichever ``os.replace``\\s second silently discards the other's delta.
``_config_write_lock`` (a ``portalocker`` sidecar lock, ``.config.json.lock``)
serializes that window.

Uses ``multiprocessing`` (not threads) because portalocker delegates to
``fcntl.flock`` / ``LockFileEx``, both process-level — a single process holding
two refs would not contend. No ``skipif(win32)``: the guarantee is meant to
hold on every supported OS. Mirrors ``test_locking_contention.py``.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import time
from pathlib import Path

import pytest

# spawn: uniform semantics across the CI matrix (Windows/macOS default).
_CTX = mp.get_context("spawn")


# ----------------------------------------------------------------- helpers


def _locked_add_section(config_path_str: str, section: str, q) -> None:
    """Locked read→merge→write adding one distinct section, as the real
    sites do (``_config_write_lock`` around ``_atomic_write_json``). The
    small sleep widens the read→write window so an unlocked version would
    reliably lose updates."""
    from memtomem.config import _atomic_write_json, _config_write_lock

    path = Path(config_path_str)
    with _config_write_lock(path):
        existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        time.sleep(0.02)
        existing[section] = {"marker": section}
        _atomic_write_json(path, existing)
    q.put(section)


def _hold_config_lock(lock_path_str: str, ready_q, release_evt) -> None:
    """Hold the sidecar lock until signalled — stands in for a concurrent
    writer owning ``.config.json.lock`` so same-process callers time out."""
    from memtomem.context._atomic import _file_lock

    with _file_lock(Path(lock_path_str)):
        ready_q.put("acquired")
        release_evt.wait(timeout=30)


# --------------------------------------------------------- lost-update pin


def test_concurrent_writers_do_not_lose_updates(tmp_path: Path):
    """Positive pin: 8 processes each add a distinct section under the lock;
    all 8 survive. Without the lock the widened window loses entries."""
    config_path = tmp_path / "config.json"
    sections = [f"s{i}" for i in range(8)]

    procs = []
    queues = []
    for section in sections:
        q = _CTX.Queue()
        p = _CTX.Process(target=_locked_add_section, args=(str(config_path), section, q))
        queues.append(q)
        procs.append(p)
        p.start()

    for q in queues:
        assert q.get(timeout=20) in sections
    for p in procs:
        p.join(timeout=10)
        assert p.exitcode == 0

    final = json.loads(config_path.read_text(encoding="utf-8"))
    assert set(final) == set(sections), (
        f"lost updates: expected all of {sections}, got {sorted(final)}"
    )


def test_lock_uses_dot_prefixed_sidecar(tmp_path: Path):
    """The lock is a sidecar next to config.json (``.config.json.lock``),
    never config.json itself — locking the data file wouldn't survive the
    ``os.replace`` inode swap."""
    from memtomem.config import _atomic_write_json, _config_write_lock

    config_path = tmp_path / "config.json"
    with _config_write_lock(config_path):
        _atomic_write_json(config_path, {"search": {"default_top_k": 7}})

    assert (tmp_path / ".config.json.lock").exists()


# ----------------------------------------------------- timeout behavior


def test_save_config_overrides_times_out_cleanly(tmp_path, monkeypatch):
    """When another process holds the lock, ``save_config_overrides`` raises
    ``TimeoutError`` (acquiring nothing) and config.json is untouched."""
    from memtomem.context._atomic import _lock_path_for

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"search": {"default_top_k": 5}}), encoding="utf-8")
    original = config_path.read_bytes()

    config_d = tmp_path / "config.d"
    config_d.mkdir()
    monkeypatch.setattr("memtomem.config._override_path", lambda: config_path)
    monkeypatch.setattr("memtomem.config._config_d_path", lambda: config_d)
    monkeypatch.setattr("memtomem.config._CONFIG_LOCK_BUDGET_S", 0.2)

    ready_q = _CTX.Queue()
    release_evt = _CTX.Event()
    lock_path = _lock_path_for(config_path)
    holder = _CTX.Process(target=_hold_config_lock, args=(str(lock_path), ready_q, release_evt))
    holder.start()
    try:
        assert ready_q.get(timeout=10) == "acquired"

        from memtomem.config import Mem2MemConfig, save_config_overrides

        with pytest.raises(TimeoutError):
            save_config_overrides(Mem2MemConfig())

        # Timeout acquires nothing, so the file is never opened for write.
        assert config_path.read_bytes() == original
    finally:
        release_evt.set()
        holder.join(timeout=5)
        assert holder.exitcode == 0


def test_config_unset_times_out_with_friendly_error(tmp_path, monkeypatch):
    """``mm config unset`` exits 1 with a friendly message (not a traceback)
    when another process holds the config lock."""
    from click.testing import CliRunner

    from memtomem.cli import cli
    from memtomem.context._atomic import _lock_path_for

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"search": {"default_top_k": 9}}), encoding="utf-8")
    original = config_path.read_bytes()

    monkeypatch.setattr("memtomem.config._override_path", lambda: config_path)
    monkeypatch.setattr("memtomem.config._CONFIG_LOCK_BUDGET_S", 0.2)

    ready_q = _CTX.Queue()
    release_evt = _CTX.Event()
    lock_path = _lock_path_for(config_path)
    holder = _CTX.Process(target=_hold_config_lock, args=(str(lock_path), ready_q, release_evt))
    holder.start()
    try:
        assert ready_q.get(timeout=10) == "acquired"

        result = CliRunner().invoke(cli, ["config", "unset", "search.default_top_k"])
        assert result.exit_code == 1
        assert "another process is writing" in result.output.lower()
        # Unset never ran, so the override is still present.
        assert config_path.read_bytes() == original
    finally:
        release_evt.set()
        holder.join(timeout=5)
        assert holder.exitcode == 0


def test_migration_persist_skips_on_timeout(tmp_path, monkeypatch, caplog):
    """The startup auto_discover migration must not brick ``mm`` when it can't
    get the lock: it logs a warning, skips the persist, and returns without
    raising (the migration retries on the next config load)."""
    import logging

    from memtomem.config import _persist_auto_discover_migration
    from memtomem.context._atomic import _lock_path_for

    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"indexing": {"auto_discover": True}}), encoding="utf-8")
    original = config_path.read_bytes()

    monkeypatch.setattr("memtomem.config._MIGRATION_LOCK_BUDGET_S", 0.2)

    ready_q = _CTX.Queue()
    release_evt = _CTX.Event()
    lock_path = _lock_path_for(config_path)
    holder = _CTX.Process(target=_hold_config_lock, args=(str(lock_path), ready_q, release_evt))
    holder.start()
    try:
        assert ready_q.get(timeout=10) == "acquired"

        with caplog.at_level(logging.WARNING, logger="memtomem.config"):
            # Must not raise.
            _persist_auto_discover_migration(config_path, [tmp_path / "mem"])

        assert config_path.read_bytes() == original
        assert any("could not lock" in r.message.lower() for r in caplog.records)
    finally:
        release_evt.set()
        holder.join(timeout=5)
        assert holder.exitcode == 0
