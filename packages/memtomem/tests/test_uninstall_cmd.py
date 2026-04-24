"""Tests for ``mm uninstall`` — local state cleanup CLI.

Coverage spans the install-context inventory, flag combinations, server
liveness refusal, partial-deletion error path, and the ``RuntimeProfile``
private-import pin so any rename/move in ``cli.init_cmd`` breaks here
loud and immediate (MEDIUM 6 mitigation).
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli


@contextlib.contextmanager
def _hold_pid_lock(pid_file: Path) -> Iterator[None]:
    """Hold an exclusive flock on ``pid_file`` for the duration of the block.

    Mirrors what ``server/__init__.py:main`` does at runtime so the
    flock-based liveness probe (#387) sees a live writer.
    """
    fp = open(pid_file, "rb+")
    try:
        fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            fcntl.flock(fp, fcntl.LOCK_UN)
    finally:
        fp.close()


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Tmp HOME with both env override and _bootstrap._CONFIG_PATH patched.

    Mirrors the isolation pattern from ``test_cli_index_noop_e2e.py``: the
    module-level ``_bootstrap._CONFIG_PATH = Path.home() / ...`` is bound
    at import time, so ``monkeypatch.setenv("HOME")`` alone leaves it
    pointing at the developer's real home. Patching it directly is
    required for hermetic tests.

    Also isolates ``$XDG_RUNTIME_DIR`` so the new runtime pid file
    location (#412) lives under ``tmp_path`` rather than the developer's
    real ``/run/user/{uid}/memtomem/`` or a shared ``/tmp`` subdir.
    """
    from memtomem.cli import _bootstrap
    from memtomem.cli import uninstall_cmd

    h = tmp_path / "home"
    h.mkdir()
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)  # _runtime_paths validator requires owner-only
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(xdg))
    monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", h / ".memtomem" / "config.json")
    monkeypatch.setattr(uninstall_cmd, "_DEFAULT_STATE_DIR", h / ".memtomem")
    return h


def _seed_state(home: Path, *, with_db: bool = True, with_fragments: bool = True) -> Path:
    """Populate ~/.memtomem/ with realistic state for inventory tests."""
    state = home / ".memtomem"
    state.mkdir(parents=True, exist_ok=True)

    (state / "config.json").write_text('{"embedding": {"provider": "none"}}', encoding="utf-8")
    (state / "config.json.bak-2026-04-22T00-00-00").write_text("{}", encoding="utf-8")
    if with_fragments:
        (state / "config.d").mkdir()
        (state / "config.d" / "claude.json").write_text("{}", encoding="utf-8")
    if with_db:
        (state / "memtomem.db").write_bytes(b"sqlite-fake")
        (state / "memtomem.db-wal").write_bytes(b"wal")
        (state / "memtomem.db-shm").write_bytes(b"shm")
    (state / "memories").mkdir()
    (state / "memories" / "x.md").write_text("# hello", encoding="utf-8")
    (state / ".current_session").write_text("sess-id", encoding="utf-8")
    return state


# -------------------------------------------------------------------- 1


class TestEmptyState:
    def test_no_state_directory_exits_cleanly(self, home):
        result = CliRunner().invoke(cli, ["uninstall"])
        assert result.exit_code == 0, result.output
        assert "No memtomem state to remove" in result.output
        assert "Binary install detected" in result.output


# -------------------------------------------------------------------- 2


class TestDefaultDeletion:
    def test_default_removes_everything(self, home):
        state = _seed_state(home)
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "Removed:" in result.output
        assert not state.exists(), f"state dir should be pruned, found: {list(state.iterdir())}"


# -------------------------------------------------------------------- 3


class TestKeepConfig:
    def test_keep_config_preserves_config_surface(self, home):
        state = _seed_state(home)
        result = CliRunner().invoke(cli, ["uninstall", "-y", "--keep-config"])
        assert result.exit_code == 0, result.output
        assert (state / "config.json").exists()
        assert (state / "config.d" / "claude.json").exists()
        assert (state / "config.json.bak-2026-04-22T00-00-00").exists()
        # data side wiped
        assert not (state / "memtomem.db").exists()
        assert not (state / "memories" / "x.md").exists()


# -------------------------------------------------------------------- 4


class TestKeepData:
    def test_keep_data_preserves_db_and_memories(self, home):
        state = _seed_state(home)
        result = CliRunner().invoke(cli, ["uninstall", "-y", "--keep-data"])
        assert result.exit_code == 0, result.output
        assert (state / "memtomem.db").exists()
        assert (state / "memtomem.db-wal").exists()
        assert (state / "memories" / "x.md").exists()
        # config side wiped
        assert not (state / "config.json").exists()
        assert not (state / "config.d").exists()


# -------------------------------------------------------------------- 5


class TestCustomStoragePath:
    def test_custom_storage_path_in_inventory_and_deleted(self, home, tmp_path, monkeypatch):
        """``storage.sqlite_path`` outside ~/.memtomem/ should still be cleaned."""
        custom_dir = tmp_path / "elsewhere"
        custom_dir.mkdir()
        custom_db = custom_dir / "foo.db"
        custom_db.write_bytes(b"sqlite-fake")
        (custom_dir / "foo.db-wal").write_bytes(b"wal")
        (custom_dir / "unrelated.txt").write_text("user file", encoding="utf-8")

        # Seed config that points to the custom path
        state = home / ".memtomem"
        state.mkdir()
        (state / "config.json").write_text(
            json.dumps({"storage": {"sqlite_path": str(custom_db)}}), encoding="utf-8"
        )

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "custom storage path" in result.output
        # custom DB siblings deleted
        assert not custom_db.exists()
        assert not (custom_dir / "foo.db-wal").exists()
        # unrelated sibling left alone
        assert (custom_dir / "unrelated.txt").exists(), "non-DB siblings must NOT be deleted"


# -------------------------------------------------------------------- 6


class TestUserMemoryDirsUntouched:
    def test_user_managed_memory_dirs_never_deleted(self, home, tmp_path):
        user_notes = tmp_path / "Documents" / "notes"
        user_notes.mkdir(parents=True)
        (user_notes / "important.md").write_text("# user data", encoding="utf-8")

        state = home / ".memtomem"
        state.mkdir()
        (state / "config.json").write_text(
            json.dumps({"indexing": {"memory_dirs": [str(user_notes)]}}), encoding="utf-8"
        )

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert (user_notes / "important.md").exists(), (
            "user-managed memory_dirs MUST stay — only ~/.memtomem/memories/ is in scope"
        )


# -------------------------------------------------------------------- 7


class TestExternalsDetectedNotModified:
    def test_external_mcp_files_detected_but_unmodified(self, home):
        claude_json = home / ".claude.json"
        original_text = json.dumps({"mcpServers": {"memtomem": {"command": "mm-server"}}})
        claude_json.write_text(original_text, encoding="utf-8")

        # state dir so we don't hit the empty-state fast path
        state = home / ".memtomem"
        state.mkdir()
        (state / "config.json").write_text("{}", encoding="utf-8")

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "External integrations" in result.output
        assert ".claude.json" in result.output
        # external file untouched
        assert claude_json.exists()
        assert claude_json.read_text(encoding="utf-8") == original_text


# -------------------------------------------------------------------- 8


class TestBinaryHintPerOrigin:
    @pytest.mark.parametrize(
        "origin,expected_substring",
        [
            ("uv-tool", "uv tool uninstall memtomem"),
            ("uvx", "ephemeral"),
            ("venv-relative", "uv pip uninstall memtomem"),
            ("system", "pip uninstall memtomem"),
            ("unknown", "which mm"),
        ],
    )
    def test_hint_text_per_origin(self, home, monkeypatch, origin, expected_substring):
        from memtomem.cli import init_cmd
        from memtomem.cli import uninstall_cmd

        fake_profile = init_cmd.RuntimeProfile(
            cwd_install_type="pypi",
            cwd_install_dir=None,
            runtime_interpreter=Path("/usr/bin/python3"),
            workspace_venv_path=Path("/tmp/foo/.venv") if origin == "venv-relative" else None,
            mm_binary_origin=origin,
            runtime_matches_workspace=(origin == "venv-relative"),
        )
        monkeypatch.setattr(uninstall_cmd, "_runtime_profile", lambda: fake_profile)

        result = CliRunner().invoke(cli, ["uninstall"])
        assert result.exit_code == 0, result.output
        assert expected_substring in result.output


# -------------------------------------------------------------------- 9


class TestNonTtyAbort:
    def test_non_tty_without_yes_aborts(self, home):
        _seed_state(home)
        # CliRunner's default input is a non-TTY StringIO → isatty() is False.
        result = CliRunner().invoke(cli, ["uninstall"], input="")
        assert result.exit_code != 0
        assert "non-interactive shell" in result.output
        # state should be untouched
        assert (home / ".memtomem" / "memtomem.db").exists()


# -------------------------------------------------------------------- 10


class TestInteractiveCancellation:
    def test_interactive_no_cancels_without_changes(self, home, monkeypatch):
        """Interactive 'n' must produce a distinct cancellation message
        from the non-TTY abort path so users + tests can tell them apart.

        ``CliRunner`` substitutes ``sys.stdin`` with a ``StringIO`` whose
        ``isatty()`` returns False, so we patch the ``_isatty`` indirection
        in ``uninstall_cmd`` directly to flip the TTY check to True.
        """
        from memtomem.cli import uninstall_cmd

        _seed_state(home)
        monkeypatch.setattr(uninstall_cmd, "_isatty", lambda: True)

        result = CliRunner().invoke(cli, ["uninstall"], input="n\n")
        assert result.exit_code == 1
        assert "Cancelled" in result.output  # distinct from non-TTY's "non-interactive shell"
        assert "non-interactive shell" not in result.output
        # untouched
        assert (home / ".memtomem" / "memtomem.db").exists()


# -------------------------------------------------------------------- 11


class TestRuntimeProfileImportPin:
    """If init_cmd renames or moves _runtime_profile / RuntimeProfile this
    test breaks immediately. The follow-up is either to update
    uninstall_cmd.py to track the move, or extract the runtime profile to a
    shared module — see plan MEDIUM 6."""

    def test_runtime_profile_symbols_importable(self):
        import dataclasses

        from memtomem.cli.init_cmd import RuntimeProfile, _runtime_profile

        assert callable(_runtime_profile)
        # Frozen dataclass — fields live on the dataclass spec, not the class
        # __dict__, so use dataclasses.fields() rather than hasattr.
        field_names = {f.name for f in dataclasses.fields(RuntimeProfile)}
        assert "mm_binary_origin" in field_names
        assert "cwd_install_type" in field_names
        # Actually buildable (no args, returns the dataclass).
        prof = _runtime_profile()
        assert isinstance(prof, RuntimeProfile)


# -------------------------------------------------------------------- 12


class TestServerAliveRefuses:
    def test_refuses_when_server_alive_at_legacy_path(self, home):
        """Pre-#412 servers still write ``~/.memtomem/.server.pid``. The
        mixed-version upgrade path (old server running, new uninstall)
        must still refuse — the flock probe checks both locations."""
        state = _seed_state(home)
        pid_file = state / ".server.pid"
        # The PID inside the file is just for the user-facing message now;
        # the flock probe (#387) is what decides alive/dead.
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        with _hold_pid_lock(pid_file):
            result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert "Server still running" in result.output
        assert str(os.getpid()) in result.output
        # nothing deleted
        assert (state / "memtomem.db").exists()
        assert (state / "config.json").exists()

    def test_refuses_when_server_alive_at_runtime_path(self, home):
        """Post-#412 servers hold the flock at
        ``$XDG_RUNTIME_DIR/memtomem/server.pid``. The probe must see it
        even though the pid file lives outside ``~/.memtomem/``."""
        from memtomem._runtime_paths import ensure_runtime_dir

        _seed_state(home)
        pid_file = ensure_runtime_dir() / "server.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        with _hold_pid_lock(pid_file):
            result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert "Server still running" in result.output
        assert str(os.getpid()) in result.output
        assert (home / ".memtomem" / "memtomem.db").exists()

    def test_force_overrides_liveness(self, home):
        state = _seed_state(home)
        pid_file = state / ".server.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        with _hold_pid_lock(pid_file):
            result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])

        assert result.exit_code == 0, result.output
        assert not state.exists()


class TestPidRecyclingDoesNotFalsePositive:
    """#387: a recorded PID that happens to point at a live unrelated process
    must not trip the liveness probe. With the old ``os.kill(pid, 0)`` probe
    this returned alive → uninstall refused. With the flock probe the absence
    of a lock holder is the sole signal."""

    def test_pid_alive_but_no_lock_means_dead(self, home):
        state = _seed_state(home)
        # Our own PID — definitely alive — but nobody is holding the flock.
        (state / ".server.pid").write_text(str(os.getpid()), encoding="utf-8")

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 0, result.output
        assert "Server still running" not in result.output
        assert not state.exists()

    def test_runtime_pid_alive_but_no_lock_means_dead(self, home):
        """Same as above at the new runtime location — a stale
        ``server.pid`` with a recycled live PID but no flock holder must
        not refuse the uninstall."""
        from memtomem._runtime_paths import ensure_runtime_dir

        _seed_state(home)
        (ensure_runtime_dir() / "server.pid").write_text(str(os.getpid()), encoding="utf-8")

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 0, result.output
        assert "Server still running" not in result.output
        assert not (home / ".memtomem").exists()


class TestRuntimePidCleanedWithOther:
    """The runtime pid file lives outside ``~/.memtomem/`` but is still
    transient server state — uninstall must clean it up so a reinstall
    starts fresh. The runtime subdir is rmdir'd if we empty it."""

    def test_runtime_pid_deleted_and_subdir_pruned(self, home):
        from memtomem._runtime_paths import ensure_runtime_dir, runtime_dir

        _seed_state(home)
        rt = ensure_runtime_dir()
        pid_file = rt / "server.pid"
        pid_file.write_text("0", encoding="utf-8")

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 0, result.output
        assert not pid_file.exists(), "runtime pid file must be deleted"
        # subdir should be gone too — we own it
        assert not runtime_dir().exists(), "empty runtime subdir must be pruned"

    def test_runtime_subdir_preserved_when_unrelated_files_present(self, home):
        """Pin the empty-check precondition on the ``rmdir`` call so a
        future condition invert (``not any(iterdir())`` → ``any(...)``)
        wouldn't silently wipe unrelated files someone else parked in
        the runtime subdir. ``mm uninstall`` is scoped; other memtomem
        entry points (or a future #384 expansion) may legitimately
        register more pid files in the same dir."""
        from memtomem._runtime_paths import ensure_runtime_dir, runtime_dir

        _seed_state(home)
        rt = ensure_runtime_dir()
        # Our own pid file is cleaned, but a sibling registered by
        # another memtomem tool must survive.
        (rt / "server.pid").write_text("0", encoding="utf-8")
        sibling = rt / "someone-elses.pid"
        sibling.write_text("42", encoding="utf-8")

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 0, result.output
        # server.pid must be gone; sibling must remain; subdir must NOT rmdir.
        assert not (rt / "server.pid").exists()
        assert sibling.exists(), "unrelated file in runtime subdir must survive"
        assert runtime_dir().exists(), "runtime subdir must not be pruned when other files remain"


# -------------------------------------------------------------------- 13


class TestDbWriterLockRefuses:
    """Active SQLite writer without a .server.pid (mm web / watchdog / ad-hoc)
    must also block the uninstall — the gap #384 called out.

    The probe relies on ``BEGIN IMMEDIATE`` raising ``SQLITE_BUSY`` when
    another connection holds RESERVED-or-above. Holding an open
    ``BEGIN IMMEDIATE`` transaction in the test process reproduces this
    cross-process lock contention within a single pytest run.
    """

    def _make_real_db(self, home: Path) -> tuple[Path, sqlite3.Connection]:
        state = home / ".memtomem"
        state.mkdir(parents=True, exist_ok=True)
        # Keep parity with _seed_state for non-DB files so the inventory
        # path is exercised end-to-end, but seed a *real* SQLite DB.
        (state / "config.json").write_text('{"embedding": {"provider": "none"}}', encoding="utf-8")
        (state / "memories").mkdir(exist_ok=True)
        db_path = state / "memtomem.db"
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE _probe (id INTEGER)")
        conn.commit()
        return db_path, conn

    def test_refuses_when_writer_holds_lock(self, home):
        db_path, conn = self._make_real_db(home)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = CliRunner().invoke(cli, ["uninstall", "-y"])
            assert result.exit_code == 2, result.output
            assert "holds a write lock" in result.output
            assert str(db_path) in result.output
            assert "lsof" in result.output
            # "Server still running" path must NOT trigger — no .server.pid here.
            assert "Server still running" not in result.output
            # Nothing deleted while the writer is alive.
            assert db_path.exists()
        finally:
            conn.rollback()
            conn.close()

    def test_force_overrides_db_lock(self, home):
        db_path, conn = self._make_real_db(home)
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])
            assert result.exit_code == 0, result.output
            # State dir gets wiped — even if the lock-holding connection
            # still has the inode, the directory entry is gone.
            assert not db_path.exists()
        finally:
            try:
                conn.rollback()
            except sqlite3.ProgrammingError:
                pass  # connection may be invalidated after the file vanished
            conn.close()

    def test_proceeds_when_db_exists_but_no_writer(self, home):
        db_path, conn = self._make_real_db(home)
        conn.close()  # release before probing — no writer held.
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "holds a write lock" not in result.output
        assert not db_path.exists()


class TestPidStaleProceeds:
    def test_proceeds_when_pid_stale(self, home, monkeypatch):
        """Pick a pid that's guaranteed dead (high number, not currently
        in use). os.kill(stale_pid, 0) raises ProcessLookupError → not alive.
        """
        state = _seed_state(home)

        # Find a stale pid — start at a high number and bump until os.kill
        # raises ProcessLookupError. Skip if PermissionError (alive but
        # not ours, can happen at low PIDs).
        stale_pid = 999_999
        for candidate in range(999_999, 999_900, -1):
            try:
                os.kill(candidate, 0)
            except ProcessLookupError:
                stale_pid = candidate
                break
            except (PermissionError, OSError):
                continue
        else:
            pytest.skip("could not find a stale pid for testing")

        (state / ".server.pid").write_text(str(stale_pid), encoding="utf-8")

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "Server still running" not in result.output
        assert not state.exists()


# -------------------------------------------------------------------- 14


class TestConfigFallback:
    def test_falls_back_when_config_load_raises(self, home, monkeypatch):
        """``_load_config_safely`` must catch any exception escaping
        ``load_config_overrides`` and fall back to the default DB path.

        ``load_config_overrides`` already swallows malformed JSON itself
        (logs WARNING, returns), so we monkeypatch it to raise outright —
        that's the failure mode the safety net was added for.
        """
        state = home / ".memtomem"
        state.mkdir()
        (state / "config.json").write_text("{}", encoding="utf-8")
        (state / "memtomem.db").write_bytes(b"sqlite-fake")

        def _boom(_cfg):
            raise PermissionError("fake permission denied on config.json")

        monkeypatch.setattr("memtomem.config.load_config_overrides", _boom)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "config unreadable" in result.output
        assert "fake permission denied" in result.output
        # default DB path used as fallback → DB still gets cleaned up
        assert "Removed:" in result.output


class TestWindowsFcntlGuard:
    """(#448) ``import fcntl`` at module level broke ``cli/__init__.py:_register``
    on Windows — every ``mm`` subcommand crashed before even parsing argv.

    The fix moves ``fcntl`` to a lazy import inside ``_probe_pid_file``, gated
    by ``sys.platform != "win32"``. On Windows the probe returns conservative
    ``alive=True`` when the pid file exists (matching the existing
    unsupported-filesystem fallback) so ``mm uninstall`` still refuses unless
    ``--force``.
    """

    def test_no_module_level_fcntl_import(self):
        """Source-scan pin: ``fcntl`` must not appear as a top-level import
        in ``uninstall_cmd.py``. A future refactor that re-hoists the
        import would silently reintroduce the Windows crash — CI runs on
        Linux so a runtime test can't catch it.
        """
        import inspect
        import re

        from memtomem.cli import uninstall_cmd

        src = inspect.getsource(uninstall_cmd)
        # Top-of-file block only — anything nested (``    import fcntl``)
        # inside a function is fine and in fact the intended layout.
        top_level = re.findall(r"(?m)^import fcntl\b", src)
        assert top_level == [], (
            "uninstall_cmd.py must not import fcntl at module level — "
            "that crashes `mm` on Windows at CLI registration time (#448)."
        )

    def test_probe_returns_alive_on_win32_when_pid_file_exists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """With ``sys.platform`` patched to ``"win32"``, ``_probe_pid_file``
        must return ``alive=True`` without ever touching ``fcntl``. The pid
        value is still read (for the user-facing message) but the live/dead
        decision is conservative."""
        from memtomem.cli import uninstall_cmd

        pid_file = tmp_path / "server.pid"
        pid_file.write_text("4242", encoding="utf-8")

        monkeypatch.setattr(uninstall_cmd.sys, "platform", "win32")

        state = uninstall_cmd._probe_pid_file(pid_file)

        assert state.alive is True
        assert state.pid == 4242
        assert state.pid_file == pid_file

    def test_probe_returns_dead_on_win32_when_pid_file_missing(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Windows fallback must not invent liveness out of nothing — an
        absent pid file means the generic ``not pid_file.exists()`` branch
        fires first and returns ``alive=False``. Guards against a future
        refactor that hoists the Windows guard above the existence check.
        """
        from memtomem.cli import uninstall_cmd

        pid_file = tmp_path / "missing.pid"
        monkeypatch.setattr(uninstall_cmd.sys, "platform", "win32")

        state = uninstall_cmd._probe_pid_file(pid_file)

        assert state.alive is False
        assert state.pid is None
        assert state.pid_file is None

    def test_force_overrides_win32_conservative_liveness(
        self, home, monkeypatch: pytest.MonkeyPatch
    ):
        """End-to-end: under the Windows fallback, ``mm uninstall -y`` refuses
        (conservative alive=True) but ``--force`` completes cleanup. Matches
        the documented escape hatch for unsupported-filesystem cases."""
        from memtomem.cli import uninstall_cmd

        state = _seed_state(home)
        pid_file = state / ".server.pid"
        pid_file.write_text("0", encoding="utf-8")

        monkeypatch.setattr(uninstall_cmd.sys, "platform", "win32")

        refused = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert refused.exit_code == 2, refused.output
        assert "Server still running" in refused.output
        assert state.exists(), "refused uninstall must not touch state dir"

        forced = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])
        assert forced.exit_code == 0, forced.output
        assert not state.exists()
        assert not (state / "memtomem.db").exists()
