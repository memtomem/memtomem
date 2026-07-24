"""Tests for ``mm uninstall`` — local state cleanup CLI.

Coverage spans the install-context inventory, flag combinations, server
liveness refusal, partial-deletion error path, and the ``RuntimeProfile``
private-import pin so any rename/move in ``cli.init_cmd`` breaks here
loud and immediate (MEDIUM 6 mitigation).
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import shutil
import sqlite3
import time
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from .helpers import set_home


@contextlib.contextmanager
def _hold_pid_lock(pid_file: Path) -> Iterator[None]:
    """Hold an exclusive lock on ``pid_file`` for the duration of the block.

    Mirrors what ``server/__init__.py:main`` does at runtime so the
    portalocker-based liveness probe (#387, #817) sees a live writer.
    Cross-platform via ``portalocker``; on Windows ``"rb+"`` open is
    required by the ``MsvcrtLocker`` backend (see ``cli/_liveness.py:54``).
    """
    import portalocker

    fp = open(pid_file, "rb+")
    try:
        portalocker.lock(fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
        try:
            yield
        finally:
            portalocker.unlock(fp)
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

    On Windows, ``_runtime_paths.runtime_dir()`` skips the
    ``XDG_RUNTIME_DIR`` branch entirely (``XDG_RUNTIME_DIR`` is a Linux/
    systemd convention; the POSIX-mode-bit gate guarding it is
    meaningless on NTFS) and falls through to
    ``tempfile.gettempdir() / memtomem-0``. ``gettempdir()`` resolves to
    the user-shared ``%LOCALAPPDATA%\\Temp\\``, so leftover artifacts
    from prior pytest runs or concurrent tools blocked the prune
    assertion in ``TestRuntimePidCleanedWithOther`` (#759 failure 3).
    Pin ``tempfile.tempdir`` to a per-test path so the runtime dir is
    always test-scoped, regardless of platform.
    """
    import tempfile

    from memtomem.cli import _bootstrap
    from memtomem.cli import uninstall_cmd

    h = tmp_path / "home"
    h.mkdir()
    # The external-MCP probe also checks ``Path.cwd() / ".mcp.json"`` (the
    # project-local Claude config). Pin cwd into the tmp home so the repo's
    # own ``.mcp.json`` doesn't leak into the probe and trip the negative
    # "not reported" assertions — CI runs pytest from the repo root, which
    # ships a real ``.mcp.json``.
    monkeypatch.chdir(h)
    xdg = tmp_path / "xdg_runtime"
    xdg.mkdir()
    os.chmod(xdg, 0o700)  # _runtime_paths validator requires owner-only
    fake_tempdir = tmp_path / "tempdir"
    fake_tempdir.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(fake_tempdir))
    set_home(monkeypatch, h)
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
        # Per-install provenance key sidecar (ADR-0006 Axis F.3) — a secret that
        # must be wiped with the data, not left for a same-path reinstall.
        (state / "memtomem.provenance_key").write_text("ab" * 32, encoding="utf-8")
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


class TestPreResetBackupInventory:
    """``mm reset --backup`` snapshots (``<db>.pre-reset-<ts>.bak``, #1574
    item 7) are DB-stem siblings the suffix loop misses — they must be
    registered in the database group or they silently survive uninstall
    and keep the state dir non-empty (blocking the final prune)."""

    def test_backup_wiped_by_default_and_dir_pruned(self, home):
        state = _seed_state(home)
        bak = state / "memtomem.db.pre-reset-20260703T120000.bak"
        bak.write_bytes(b"sqlite-fake-backup")
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert not bak.exists(), "pre-reset backup survived a default uninstall"
        assert not state.exists(), f"state dir not pruned, found: {list(state.iterdir())}"

    def test_keep_data_preserves_backup(self, home):
        state = _seed_state(home)
        bak = state / "memtomem.db.pre-reset-20260703T120000.bak"
        bak.write_bytes(b"sqlite-fake-backup")
        result = CliRunner().invoke(cli, ["uninstall", "-y", "--keep-data"])
        assert result.exit_code == 0, result.output
        assert bak.exists(), "backup is user data — --keep-data must preserve it"


# -------------------------------------------------------------------- 3


class TestKeepConfig:
    def test_keep_config_preserves_config_surface(self, home):
        state = _seed_state(home)
        result = CliRunner().invoke(cli, ["uninstall", "-y", "--keep-config"])
        assert result.exit_code == 0, result.output
        assert (state / "config.json").exists()
        assert (state / "config.d" / "claude.json").exists()
        assert (state / "config.json.bak-2026-04-22T00-00-00").exists()
        # data side wiped (incl. the provenance key — it is data, not config)
        assert not (state / "memtomem.db").exists()
        assert not (state / "memtomem.provenance_key").exists()
        assert not (state / "memories" / "x.md").exists()


# -------------------------------------------------------------------- 4


class TestKeepData:
    def test_keep_data_preserves_db_and_memories(self, home):
        state = _seed_state(home)
        result = CliRunner().invoke(cli, ["uninstall", "-y", "--keep-data"])
        assert result.exit_code == 0, result.output
        assert (state / "memtomem.db").exists()
        assert (state / "memtomem.db-wal").exists()
        # keeping data keeps the provenance key, so prior self-exports still verify
        assert (state / "memtomem.provenance_key").exists()
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
        (custom_dir / "foo.provenance_key").write_text("ab" * 32, encoding="utf-8")
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
        # custom DB siblings + provenance key deleted
        assert not custom_db.exists()
        assert not (custom_dir / "foo.db-wal").exists()
        assert not (custom_dir / "foo.provenance_key").exists()
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


class TestProbeExternalParsedMCP:
    """#975: parsed MCP config check avoids substring false positives."""

    def _seed_state_dir(self, home: Path) -> Path:
        state = home / ".memtomem"
        state.mkdir()
        (state / "config.json").write_text("{}", encoding="utf-8")
        return state

    def test_json_mcp_servers_memtomem_reported(self, home):
        """Valid JSON with ``mcpServers.memtomem`` is still reported."""
        self._seed_state_dir(home)
        path = home / ".cursor" / "mcp.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"mcpServers": {"memtomem": {"command": "mm-server"}}}),
            encoding="utf-8",
        )
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "External integrations" in result.output
        assert "mcp.json" in result.output

    def test_kimi_share_dir_mcp_servers_memtomem_reported(self, home, monkeypatch):
        """Kimi MCP config under ``$KIMI_SHARE_DIR`` is reported when present."""
        self._seed_state_dir(home)
        share_dir = home / "kimi-share"
        monkeypatch.setenv("KIMI_SHARE_DIR", str(share_dir))
        path = share_dir / "mcp.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"mcpServers": {"memtomem": {"command": "mm-server"}}}),
            encoding="utf-8",
        )
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "External integrations" in result.output
        assert "kimi-share" in result.output
        assert "mcp.json" in result.output

    def test_json_unrelated_text_not_reported(self, home):
        """JSON that only mentions 'memtomem' in a comment/description is NOT reported."""
        self._seed_state_dir(home)
        path = home / ".cursor" / "mcp.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "other-server": {"command": "some-tool"},
                    },
                    "description": "This config works with memtomem for context",
                }
            ),
            encoding="utf-8",
        )
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "External integrations" not in result.output

    def test_json_no_mcp_servers_key_not_reported(self, home):
        """JSON without an ``mcpServers`` key is NOT reported even if
        'memtomem' appears elsewhere in the config."""
        self._seed_state_dir(home)
        path = home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"someOtherKey": {"memtomem": "mentioned-but-not-mcp"}}),
            encoding="utf-8",
        )
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "External integrations" not in result.output

    def test_toml_mcp_servers_memtomem_reported(self, home):
        """TOML (Codex config) with ``mcp_servers.memtomem`` is reported."""
        import tomllib as _tl

        self._seed_state_dir(home)
        path = home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True)
        toml_text = 'command = "mm-server"\n[mcp_servers.memtomem]\n'
        path.write_text(toml_text, encoding="utf-8")
        # Sanity: tomllib round-trips
        parsed = _tl.loads(toml_text)
        assert "mcp_servers" in parsed
        assert "memtomem" in parsed["mcp_servers"]

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "External integrations" in result.output
        assert "config.toml" in result.output

    def test_toml_unrelated_text_not_reported(self, home):
        """TOML with 'memtomem' only in a comment or non-MCP section is NOT reported."""
        self._seed_state_dir(home)
        path = home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True)
        # "memtomem" appears only in a comment and in an unrelated value
        path.write_text(
            "# This config used to reference memtomem\n"
            "[unrelated]\n"
            'note = "not about memtomem here"\n',
            encoding="utf-8",
        )
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        # Must NOT be reported — "memtomem" is only in comment / unrelated value,
        # not under a parsed mcp_servers key.
        assert "External integrations" not in result.output

    def test_toml_no_mcp_servers_section_not_reported(self, home):
        """TOML with ``memtomem = true`` at top level (not under mcp_servers)
        is NOT reported — the parsed check only looks under ``mcp_servers``."""
        self._seed_state_dir(home)
        path = home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text("memtomem = true\n", encoding="utf-8")
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "External integrations" not in result.output

    def test_invalid_json_ignored_no_crash(self, home):
        """Malformed JSON must NOT crash uninstall — uninstall is a recovery path."""
        self._seed_state_dir(home)
        path = home / ".claude.json"
        path.write_text("{this is not valid json //}", encoding="utf-8")

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        # The substring "memtomem" isn't in the bad json, so no external is reported
        assert "External integrations" not in result.output

    def test_invalid_toml_ignored_no_crash(self, home):
        """Malformed TOML must NOT crash uninstall — parsed check fails, no fallback."""
        self._seed_state_dir(home)
        path = home / ".codex" / "config.toml"
        path.parent.mkdir(parents=True)
        path.write_text("[mcp_servers\n  memtomem = bad\n", encoding="utf-8")

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        # Invalid TOML → parse fails → silently skipped. No crash, no false report.
        assert "External integrations" not in result.output

    def test_windsurf_mcp_config_json_parsed(self, home):
        """Windsurf ``mcp_config.json`` is a JSON file and uses the parsed path."""
        self._seed_state_dir(home)
        path = home / ".codeium" / "windsurf" / "mcp_config.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"mcpServers": {"memtomem": {"command": "mm-server"}}}),
            encoding="utf-8",
        )
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "External integrations" in result.output
        assert "mcp_config.json" in result.output

    def test_gemini_settings_json_parsed(self, home):
        """Gemini ``settings.json`` uses the parsed JSON path.
        No ``mcpServers`` key → NOT reported, even though 'memtomem' appears in text."""
        self._seed_state_dir(home)
        path = home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({"gemini": {"version": "1.0", "description": "uses memtomem"}}),
            encoding="utf-8",
        )
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "External integrations" not in result.output

    def test_nonexistent_files_skipped_silently(self, home):
        """Files that don't exist are simply skipped — no error, no output."""
        self._seed_state_dir(home)
        # No external files created → _probe must return empty list.
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "External integrations" not in result.output


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
            runtime_interpreter=Path("python3"),
            workspace_venv_path=Path("foo/.venv") if origin == "venv-relative" else None,
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
        must still refuse — the flock probe checks both locations.

        Cross-platform via portalocker (#817/#819). On Windows
        ``LockFileEx`` blocks ``read`` from other handles too, so
        ``probe_pid_file`` cannot read the pid number out of the locked
        file and the message takes the unknown-pid branch with a
        Sysinternals/Resource Monitor hint instead of the POSIX
        ``lsof`` + recorded-pid path. Both branches must surface
        "Server still running" + ``exit_code == 2`` + state preserved.
        """
        state = _seed_state(home)
        pid_file = state / ".server.pid"
        # The PID inside the file is just for the user-facing message now;
        # the flock probe (#387) is what decides alive/dead.
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        with _hold_pid_lock(pid_file):
            result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert "Server still running" in result.output
        if sys.platform == "win32":
            # Windows: pid is None (read blocked by LockFileEx), so the
            # message points at handle.exe / Resource Monitor, not lsof.
            assert "handle.exe" in result.output or "Resource Monitor" in result.output, (
                f"Windows refusal must point at a holder-finder; got: {result.output!r}"
            )
        else:
            assert str(os.getpid()) in result.output
        # nothing deleted
        assert (state / "memtomem.db").exists()
        assert (state / "config.json").exists()

    def test_refuses_when_server_alive_at_runtime_path(self, home):
        """Post-#412 servers hold the flock at
        ``$XDG_RUNTIME_DIR/memtomem/server.pid``. The probe must see it
        even though the pid file lives outside ``~/.memtomem/``.

        Same Windows caveat as ``test_refuses_when_server_alive_at_legacy_path``:
        the recorded pid is unreachable behind ``LockFileEx``, so the
        message routes through the unknown-pid branch with a
        Sysinternals hint.
        """
        from memtomem._runtime_paths import ensure_runtime_dir

        _seed_state(home)
        pid_file = ensure_runtime_dir() / "server.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        with _hold_pid_lock(pid_file):
            result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert "Server still running" in result.output
        if sys.platform == "win32":
            assert "handle.exe" in result.output or "Resource Monitor" in result.output, (
                f"Windows refusal must point at a holder-finder; got: {result.output!r}"
            )
        else:
            assert str(os.getpid()) in result.output
        assert (home / ".memtomem" / "memtomem.db").exists()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "POSIX-only contract: --force unlinks the locked pid file via "
            "unlink-while-open. The Windows mirror of this contract lives in "
            "test_force_refuses_on_windows_when_pid_locked (#730 + #819)."
        ),
    )
    def test_force_overrides_liveness(self, home):
        state = _seed_state(home)
        pid_file = state / ".server.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        with _hold_pid_lock(pid_file):
            result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])

        assert result.exit_code == 0, result.output
        assert not state.exists()

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason=(
            "Windows-only contract (#730 + #819): --force cannot wipe a "
            "locked pid file because NTFS refuses unlink-while-open "
            "(WinError 32). The refusal must be clean — exit 2 + actionable "
            "hint + state preserved — never a half-wiped state dir."
        ),
    )
    def test_force_refuses_on_windows_when_pid_locked(self, home):
        """Windows mirror of ``test_force_overrides_liveness``.

        POSIX ``--force`` succeeds via ``unlink-while-open``; Windows
        cannot, so the same invocation must refuse cleanly rather than
        partially wipe state. This pins the #730 destructive-CLI
        contract on the pid-lock side, complementing
        ``test_force_refuses_on_windows_when_writer_alive`` which pins
        the same contract on the DB-lock side.
        """
        state = _seed_state(home)
        pid_file = state / ".server.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")

        with _hold_pid_lock(pid_file):
            result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])

        assert result.exit_code == 2, result.output
        # State preserved — refusal fires before any deletion runs.
        assert state.exists()
        assert (state / "memtomem.db").exists()
        assert (state / "config.json").exists()
        assert pid_file.exists()
        # Actionable Windows-native hint must surface; the refusal text
        # came from #730 and uses Sysinternals/Task Manager wording.
        assert (
            "handle.exe" in result.output
            or "WinError 32" in result.output
            or "cannot wipe" in result.output
        ), f"Windows --force refusal must point at a holder-finder; got: {result.output!r}"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX-specific 'lsof' wording (#819 follow-up); Windows uses handle.exe / Resource Monitor",
    )
    def test_refuses_with_unknown_pid_branch_when_pid_file_empty(self, home):
        """Empty pid file + flock held = the truncate-race fingerprint.

        When a pre-fix concurrent server start truncated the live
        server's pid file, the recorded pid is gone but the flock is
        still held. The user-facing message must distinguish this from
        the normal case so the user can run ``lsof <pidfile>`` to find
        the holder — falling back to the generic ``pid None`` was
        confusing enough to file in this PR.
        """
        from memtomem._runtime_paths import ensure_runtime_dir

        _seed_state(home)
        pid_file = ensure_runtime_dir() / "server.pid"
        pid_file.write_text("", encoding="utf-8")  # empty content, exists

        with _hold_pid_lock(pid_file):
            result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert "pid unknown" in result.output, (
            "empty pid + held flock must surface the 'pid unknown' branch, "
            f"not the generic 'pid None' message; got: {result.output!r}"
        )
        assert "lsof" in result.output, (
            "empty-pid branch must point at lsof so the user can identify "
            "the flock holder without another diagnostic round-trip"
        )
        # Refusal still protects state — same WAL-corruption invariant.
        assert (home / ".memtomem" / "memtomem.db").exists()


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
            # "Server still running" path must NOT trigger — no .server.pid here.
            assert "Server still running" not in result.output
            # Hint contents differ per platform (#730): POSIX advertises
            # `lsof` + `--force`; Windows can't override --force, so it must
            # NOT advertise it and must point at Windows-native tools.
            if sys.platform == "win32":
                assert "lsof" not in result.output
                assert "pass --force" not in result.output
                assert (
                    "handle.exe" in result.output
                    or "Task Manager" in result.output
                    or "Get-Process" in result.output
                )
            else:
                assert "lsof" in result.output
                assert "pass --force" in result.output
            # Nothing deleted while the writer is alive.
            assert db_path.exists()
        finally:
            conn.rollback()
            conn.close()

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "POSIX-only contract: --force wipes via unlink-while-open even "
            "though the writer still holds the inode. Windows variant lives "
            "in test_force_refuses_on_windows_when_writer_alive (#730)."
        ),
    )
    def test_force_overrides_db_lock_posix(self, home):
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

    @pytest.mark.skipif(
        sys.platform != "win32",
        reason=(
            "Windows-only contract (#730): --force cannot wipe an open SQLite "
            "DB on Windows (WinError 32 on unlink-while-open), so --force "
            "must refuse cleanly instead of producing a half-wiped state dir."
        ),
    )
    def test_force_refuses_on_windows_when_writer_alive(self, home):
        db_path, conn = self._make_real_db(home)
        # _make_real_db seeds config.json + memories/ alongside the DB.
        # _delete_inventory wipes those BEFORE the DB, so checking they
        # survive proves the refusal fired before any deletion ran (#730).
        config_path = home / ".memtomem" / "config.json"
        memories_dir = home / ".memtomem" / "memories"
        assert config_path.exists()
        assert memories_dir.exists()
        try:
            conn.execute("BEGIN IMMEDIATE")
            result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])
            assert result.exit_code == 2, result.output
            assert "Windows" in result.output
            assert "stop the writer" in result.output.lower()
            # Refusal fired BEFORE _delete_inventory: nothing wiped.
            assert db_path.exists()
            assert config_path.exists()
            assert memories_dir.exists()
            # Partial-wipe message must NOT appear — that would mean
            # _delete_inventory ran and crashed mid-way.
            assert "Deletion failed at" not in result.output
        finally:
            try:
                conn.rollback()
            except sqlite3.ProgrammingError:
                pass
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

        def _boom(_cfg, *, migrate=True):
            raise PermissionError("fake permission denied on config.json")

        monkeypatch.setattr("memtomem.config.load_config_overrides", _boom)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "config unreadable" in result.output
        assert "fake permission denied" in result.output
        # default DB path used as fallback → DB still gets cleaned up
        assert "Removed:" in result.output


# -------------------------------------------------------------------- 15
#
# Transactional staging (#757): the wipe is staged via os.replace into a
# sibling .uninstall-staging-<pid>/ dir, then rmtree'd on success. A
# mid-stage failure rolls back so the user's state dir is never left
# half-gone, and a cross-FS layout is refused with a clean message
# instead of being half-staged.


class TestTransactionalStaging:
    """Acceptance criteria from the issue:

    - Mid-stage failure → original state dir intact (no half-state).
    - --keep-config / --keep-data still work (covered by sibling tests
      above; this class just adds the failure-injection cases).
    - Cross-FS layout → refusal-or-fallback per the chosen design.
    """

    def _snapshot(self, root: Path) -> dict[str, bytes | str]:
        """Return ``{relpath: contents}`` for every file under ``root``,
        excluding any ``.uninstall-staging-*`` subtree (transient state
        we don't want included in the equality check).

        Keys use ``as_posix()`` so the snapshot is comparable across
        platforms — ``str(Path("memories/x.md"))`` is ``memories/x.md``
        on POSIX but ``memories\\x.md`` on Windows, which would make
        the hardcoded ``"memories/x.md" in before`` sanity check fail
        on the Windows lane (caught in CI).

        ``config.json`` is recorded as a presence sentinel rather than
        its bytes because ``_load_config_safely`` may legitimately
        rewrite it (e.g. the ``auto_discover`` migration) before
        staging begins — that mutation is orthogonal to whether
        staging is transactional. We still verify the file *exists*
        post-rollback, which is the staging invariant."""
        from memtomem.cli.uninstall_cmd import _STAGING_PREFIX

        snap: dict[str, bytes | str] = {}
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(root)
            if any(part.startswith(_STAGING_PREFIX) for part in rel.parts):
                continue
            key = rel.as_posix()
            if key == "config.json":
                snap[key] = "<exists>"
            else:
                snap[key] = path.read_bytes()
        return snap

    def test_rollback_restores_original_state_on_mid_stage_failure(self, home, monkeypatch):
        """Inject an OSError on the Nth staging-direction os.replace and
        assert every original file is back where it started.

        Filters on the staging-prefix in ``dst`` so unrelated os.replace
        calls (e.g. config-load's atomic rewrite of ``config.json``)
        don't shift the call counter.

        N=2 leaves at least one earlier move that needs to be rolled
        back, so the test exercises the rollback path, not just the
        first-call-fails edge case."""
        from memtomem.cli import uninstall_cmd

        state = _seed_state(home)
        before = self._snapshot(state)
        # Sanity: snapshot is non-trivial (catches a future _seed_state shrink).
        assert "config.json" in before
        assert "memtomem.db" in before
        assert "memories/x.md" in before

        real_replace = os.replace
        stage_calls = {"n": 0}

        def _flaky_replace(src, dst, **kwargs):
            if uninstall_cmd._STAGING_PREFIX in str(dst):
                stage_calls["n"] += 1
                if stage_calls["n"] == 2:
                    raise OSError(errno.EACCES, "fake permission denied during stage")
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr(uninstall_cmd.os, "replace", _flaky_replace)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 2, result.output
        assert "Deletion failed" in result.output
        assert "Rolled back" in result.output

        after = self._snapshot(state)
        assert after == before, (
            f"rollback did not restore original layout.\n"
            f"missing: {set(before) - set(after)}\n"
            f"extra:   {set(after) - set(before)}"
        )
        # No staging dir leftovers (rollback cleans up empty roots).
        leftover = sorted(
            p.name for p in state.iterdir() if p.name.startswith(uninstall_cmd._STAGING_PREFIX)
        )
        assert leftover == [], f"unexpected staging leftovers: {leftover}"

    def test_rollback_cleans_nested_scaffold_across_two_roots(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        """The scaffold path, which every other rollback test misses.

        Everything ``_seed_state`` produces sits directly under the state
        dir, so ``rel`` is one component, ``dst.parent`` *is* the staging
        root, and ``_record_scaffold`` iterates zero times. A registry
        sentinel is the case that does not degenerate: it lives at
        ``<runtime>/instances/<name>``, so staging it creates a second
        root under a different anchor *and* an ``instances/`` directory
        inside it. Both roots must come back clean, which also exercises
        the deepest-first ordering across unrelated roots."""
        from memtomem.cli import uninstall_cmd

        reg = registry_at_runtime_dir
        state = _seed_state(home)
        entry = _seed_sentinel(reg, aged=True)
        rt = reg.instances_dir().parent

        real_replace = os.replace

        def _fail_on_db(src, dst, **kwargs):
            if uninstall_cmd._STAGING_PREFIX in str(dst) and Path(src).name == "memtomem.db":
                raise OSError(errno.EACCES, "fake stage failure")
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr(uninstall_cmd.os, "replace", _fail_on_db)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2, result.output
        assert entry.exists(), "the sentinel must be rolled back"
        for anchor in (state, rt):
            leftover = sorted(
                str(p.relative_to(anchor))
                for p in anchor.rglob(f"{uninstall_cmd._STAGING_PREFIX}*")
            )
            assert leftover == [], f"staging leftovers under {anchor}: {leftover}"

    def test_partial_mkdir_scaffold_is_still_pruned(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        """``mkdir(parents=True)`` can create part of the chain and then
        raise. Recording after it would lose exactly those directories —
        the ones cleanup exists to remove — so the record must be taken
        first."""
        from memtomem.cli import uninstall_cmd

        reg = registry_at_runtime_dir
        state = _seed_state(home)
        _seed_sentinel(reg, aged=True)
        rt = reg.instances_dir().parent

        real_mkdir = Path.mkdir

        def _partial_mkdir(self, mode=0o777, parents=False, exist_ok=False):
            if parents and self.name == "instances" and uninstall_cmd._STAGING_PREFIX in str(self):
                real_mkdir(self, mode=mode, parents=True, exist_ok=True)
                raise OSError(errno.EACCES, "fake failure after creating the chain")
            return real_mkdir(self, mode=mode, parents=parents, exist_ok=exist_ok)

        monkeypatch.setattr(Path, "mkdir", _partial_mkdir)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2, result.output
        for anchor_dir in (state, rt):
            leftover = sorted(
                str(p.relative_to(anchor_dir))
                for p in anchor_dir.rglob(f"{uninstall_cmd._STAGING_PREFIX}*")
            )
            assert leftover == [], f"staging leftovers under {anchor_dir}: {leftover}"

    def test_rollback_failure_surfaces_recovery_path(self, home, monkeypatch):
        """When BOTH stage AND rollback fail, the user must see the
        staging dir path so they can recover manually. Without this,
        partial-state recovery is impractical (the issue's whole
        motivation).

        Setup: forward staging move #1 succeeds, #2 trips the failure
        path, and the rollback move (recognized by ``_STAGING_PREFIX``
        in ``src``, not ``dst``) ALSO fails — the disaster scenario."""
        from memtomem.cli import uninstall_cmd

        _seed_state(home)
        real_replace = os.replace
        stage_calls = {"n": 0}

        def _broken_replace(src, dst, **kwargs):
            src_str, dst_str = str(src), str(dst)
            if uninstall_cmd._STAGING_PREFIX in dst_str:
                # Forward stage move.
                stage_calls["n"] += 1
                if stage_calls["n"] == 2:
                    raise OSError(errno.EACCES, "fake stage failure")
                return real_replace(src, dst, **kwargs)
            if uninstall_cmd._STAGING_PREFIX in src_str:
                # Rollback move — refuse so the recovery path activates.
                raise OSError(errno.EACCES, "fake rollback failure")
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr(uninstall_cmd.os, "replace", _broken_replace)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 2, result.output
        assert "Deletion failed" in result.output
        assert "Rollback also failed" in result.output
        assert uninstall_cmd._STAGING_PREFIX in result.output, (
            "user must be told the staging dir path for manual recovery"
        )

    def test_failed_rollback_cleanup_spares_directory_links(self, home, monkeypatch):
        """Belt and braces for the survivor: no ``rmdir`` may ever target
        a directory link in the staging tree.

        Cleanup no longer walks staged content at all, so this cannot
        trigger today — it pins the outcome rather than the mechanism, so
        that reintroducing a walk fails here too. POSIX survives the old
        shape by accident (``rmdir`` on a symlink is ENOTDIR), hence the
        assertion is on the call: on Windows ``RemoveDirectoryW`` removes
        the reparse point, silently eating part of the tree the error
        message tells the user to recover by hand."""
        from memtomem.cli import uninstall_cmd

        state = _seed_state(home)
        outside = home / "outside-target"
        outside.mkdir()
        (outside / "keep.md").write_text("# keep", encoding="utf-8")
        link = state / "memories" / "linked"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")

        real_replace = os.replace

        def _broken_replace(src, dst, **kwargs):
            if uninstall_cmd._STAGING_PREFIX in str(dst):
                if Path(src).name == "memtomem.db":
                    raise OSError(errno.EACCES, "fake stage failure")
                return real_replace(src, dst, **kwargs)
            if uninstall_cmd._STAGING_PREFIX in str(src):
                raise OSError(errno.EACCES, "fake rollback failure")
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr(uninstall_cmd.os, "replace", _broken_replace)

        real_rmdir = Path.rmdir
        rmdir_targets: list[str] = []

        def _recording_rmdir(self):
            rmdir_targets.append(self.name)
            return real_rmdir(self)

        monkeypatch.setattr(Path, "rmdir", _recording_rmdir)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2, result.output
        assert "linked" not in rmdir_targets, (
            f"cleanup tried to rmdir a directory link; targets={rmdir_targets}"
        )
        assert (outside / "keep.md").exists(), "link target must be untouched"

    def test_failed_rollback_cleanup_never_walks_staged_content(self, home, monkeypatch):
        """Cleanup must prune only the scaffold it created, never walk the
        survivor.

        The link case above is one symptom; this is the defect. Walking
        with ``rglob`` reaches *any* empty directory inside the staged
        tree — no link required — and on Windows it also descends a
        staged junction, pruning inside its target, because ``rglob``
        goes through ``Path.walk`` whose
        ``is_dir(follow_symlinks=False)`` is True for a junction. A
        per-entry link check cannot help once the walk has crossed."""
        from memtomem.cli import uninstall_cmd

        state = _seed_state(home)
        (state / "memories" / "keepdir").mkdir()

        real_replace = os.replace

        def _broken_replace(src, dst, **kwargs):
            if uninstall_cmd._STAGING_PREFIX in str(dst):
                if Path(src).name == "memtomem.db":
                    raise OSError(errno.EACCES, "fake stage failure")
                return real_replace(src, dst, **kwargs)
            if uninstall_cmd._STAGING_PREFIX in str(src):
                raise OSError(errno.EACCES, "fake rollback failure")
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr(uninstall_cmd.os, "replace", _broken_replace)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2, result.output
        staged = [
            p
            for p in state.glob(f"{uninstall_cmd._STAGING_PREFIX}*/memories/keepdir")
            if p.is_dir()
        ]
        assert staged, (
            "an empty directory inside the unrecovered content was pruned; "
            "cleanup walked user data instead of its own scaffold"
        )

    def test_cross_fs_precheck_does_not_follow_a_linked_source(self, home, monkeypatch):
        """``os.replace`` moves the *entry*, which lives on the anchor's
        filesystem even when it is a link to another volume. Statting
        through the link reports the target's device and refuses a move
        that would have succeeded."""
        state = _seed_state(home)
        outside = home / "elsewhere"
        outside.mkdir()
        link = state / "uploads"
        try:
            link.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")

        real_stat = Path.stat

        def _stat(self, *, follow_symlinks=True):
            st = real_stat(self, follow_symlinks=follow_symlinks)
            if follow_symlinks and self == link:
                fields = list(st)
                fields[2] = 0x5EEDBEEF  # st_dev — pretend the target is elsewhere
                return os.stat_result(tuple(fields))
            return st

        monkeypatch.setattr(Path, "stat", _stat)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert "different filesystem" not in result.output, result.output
        assert result.exit_code == 0, result.output

    def test_cross_fs_layout_refused_cleanly(self, home, monkeypatch):
        """Simulate EXDEV from os.replace and assert the user sees a
        cross-filesystem refusal — not a generic stage-failed error —
        with the original state dir untouched.

        Real cross-FS layouts (bind mount inside ~/.memtomem) are too
        environment-dependent to set up in CI, so we monkeypatch the
        EXDEV path instead. Same code path as a real cross-FS layout
        would take."""
        from memtomem.cli import uninstall_cmd

        state = _seed_state(home)
        before = self._snapshot(state)

        def _exdev_replace(src, dst, **kwargs):
            raise OSError(errno.EXDEV, "fake cross-device link")

        monkeypatch.setattr(uninstall_cmd.os, "replace", _exdev_replace)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 2, result.output
        assert "different" in result.output.lower() and "filesystem" in result.output.lower()
        # Original state intact — refusal fires before any partial wipe.
        after = self._snapshot(state)
        assert after == before


# (#448 / #625) The single-file regex pin that used to live here is
# superseded by ``test_no_posix_only_imports.py``, which AST-scans every
# module under ``packages/memtomem/src/memtomem/`` for module-level
# POSIX-only imports — fcntl, pwd, grp, termios, resource. The earlier
# regex covered only ``cli/uninstall_cmd.py``, which is exactly why
# PR #623's regression in ``context/_atomic.py`` slipped past it.


# ------------------------------------------------------------------- #1935


@pytest.fixture
def registry_at_runtime_dir(home, monkeypatch):
    """Anchor the instance registry at the *same* runtime dir uninstall
    stages against.

    The conftest ``_isolated_instance_registry`` default points the
    registry at its own tmp dir, which is outside every staging anchor —
    correct for hermeticity, wrong for these tests, which exercise the
    production layout where ``instances/`` lives under
    ``_runtime_paths.runtime_dir()`` (here: the ``home`` fixture's
    isolated ``$XDG_RUNTIME_DIR``).
    """
    import memtomem._instance_registry as reg
    from memtomem._runtime_paths import ensure_runtime_dir, runtime_dir

    monkeypatch.setattr(reg, "runtime_dir", runtime_dir)
    monkeypatch.setattr(reg, "ensure_runtime_dir", ensure_runtime_dir)
    return reg


def _seed_sentinel(reg, *, aged: bool = False) -> Path:
    """Create one parseable sentinel file (unlocked unless the caller
    locks it) in the registry's sentinel dir."""
    reg.ensure_runtime_dir()  # 0o700 — a bare mkdir(parents=True) would
    # create the runtime dir 0o755 and trip the safety validator later
    d = reg.instances_dir()
    d.mkdir(mode=0o700, exist_ok=True)
    entry = d / f"12345-1-{'d' * 16}-aaaaaaaa-bbbbbbbb.lock"
    entry.write_bytes(b"")
    if aged:
        old = time.time() - reg._STALE_GRACE_S - 10
        os.utime(entry, (old, old))
    return entry


class TestInstanceRegistryGateRefuses:
    """LIVE/UNKNOWN/UNTRUSTED registry evidence refuses unconditionally
    (#1935, #1942).

    This closes the two probes' blind spot: a *secondary* server owns no
    ``server.pid``, and an *idle* server holds no SQLite write lock, so
    only the sentinel flock proves it is alive.
    """

    def test_live_sentinel_refuses(self, home, registry_at_runtime_dir):
        reg = registry_at_runtime_dir
        state = _seed_state(home)
        entry = _seed_sentinel(reg)
        with _hold_pid_lock(entry):
            result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 2
        assert "live memtomem-server instance is registered" in result.output
        assert (state / "memtomem.db").exists()
        assert entry.exists()

    def test_live_sentinel_refuses_despite_force(self, home, registry_at_runtime_dir):
        """--force covers the stale-pid heuristic, not positive liveness —
        and the refusal must not advertise an override that doesn't apply."""
        reg = registry_at_runtime_dir
        state = _seed_state(home)
        entry = _seed_sentinel(reg)
        with _hold_pid_lock(entry):
            result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])
        assert result.exit_code == 2
        assert "--force does not override" in result.output
        assert "pass --force" not in result.output
        assert (state / "memtomem.db").exists()

    def test_live_sentinel_refuses_despite_force_keep_data(self, home, registry_at_runtime_dir):
        """The round-8 gate case: ``--force --keep-data`` preserves the DB
        but would stage the live sentinel — must refuse before staging."""
        reg = registry_at_runtime_dir
        state = _seed_state(home)
        entry = _seed_sentinel(reg)
        with _hold_pid_lock(entry):
            result = CliRunner().invoke(cli, ["uninstall", "-y", "--force", "--keep-data"])
        assert result.exit_code == 2
        assert entry.exists(), "a live sentinel must never be staged"
        assert (state / "memtomem.db").exists()

    def test_sidecar_timeout_is_unknown_and_refuses(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        """Fail-closed: a probe that cannot complete never means 'empty'."""
        reg = registry_at_runtime_dir
        state = _seed_state(home)
        _seed_sentinel(reg)
        monkeypatch.setattr(reg, "_LOCK_TIMEOUT_S", 0.2)
        reg.ensure_runtime_dir()
        sidecar = reg.registry_sidecar_path()
        sidecar.write_bytes(b"")
        with _hold_pid_lock(sidecar):
            result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 2
        assert "did not complete" in result.output
        # A timeout is transient — "retry" stays the right advice here,
        # and the persistent-cause wording must not leak in (#1942).
        assert "Retry in a moment" in result.output
        assert "Remove or repair" not in result.output
        assert "--force does not override" in result.output
        assert (state / "memtomem.db").exists()

    def test_no_state_with_untrusted_registry_still_refuses(self, home, registry_at_runtime_dir):
        """The empty-state fast path must not outrun the registry gate:
        an untrusted registry makes ``_registry_has_sentinels`` answer
        False because it cannot *see* the registry — "No state" + exit 0
        would silently bypass the refusal and its remediation (#1942)."""
        reg = registry_at_runtime_dir
        reg.ensure_runtime_dir()
        try:
            reg.instances_dir().symlink_to(home / "nowhere")
        except OSError:
            pytest.skip("symlinks unavailable")

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert "No memtomem state to remove" not in result.output
        assert str(reg.instances_dir()) in result.output, "refusal must name the path"
        assert "Remove or repair" in result.output

    def test_no_state_with_unknown_registry_still_refuses(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        """Same fast-path gate for the transient cause: refuse with the
        retry advice instead of claiming there is nothing to remove."""
        from memtomem._instance_registry import UninstallProbeResult
        from memtomem.cli import uninstall_cmd

        monkeypatch.setattr(
            uninstall_cmd,
            "_probe_registry_liveness",
            lambda: UninstallProbeResult("UNKNOWN"),
        )

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert "No memtomem state to remove" not in result.output
        assert "Retry in a moment" in result.output

    def test_force_still_overrides_pid_heuristic_when_registry_empty(
        self, home, registry_at_runtime_dir
    ):
        """The pre-existing ``--force`` contract survives: with no registry
        evidence, a held ``server.pid`` is still overridable (POSIX)."""
        if sys.platform == "win32":
            pytest.skip("POSIX --force contract; Windows mirror exists elsewhere")
        state = _seed_state(home)
        pid_file = state / ".server.pid"
        pid_file.write_text(str(os.getpid()), encoding="utf-8")
        with _hold_pid_lock(pid_file):
            result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])
        assert result.exit_code == 0, result.output
        assert not state.exists()


class TestInstanceRegistryInventory:
    """Stale sentinels are ordinary transient state: inventoried, staged,
    deleted; the sidecar is retained infrastructure."""

    def test_stale_sentinels_deleted_and_dir_pruned(self, home, registry_at_runtime_dir):
        reg = registry_at_runtime_dir
        _seed_state(home)
        entry = _seed_sentinel(reg, aged=True)
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert not entry.exists()
        assert not reg.instances_dir().exists(), "emptied sentinel dir must be pruned"

    def test_registry_only_leftover_is_offered_not_no_state(self, home, registry_at_runtime_dir):
        """A crashed server's leftover sentinel with no other state must
        not hit the 'No state' fast path — it is real removable state."""
        reg = registry_at_runtime_dir
        entry = _seed_sentinel(reg, aged=True)
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "No memtomem state to remove" not in result.output
        assert not entry.exists()

    def test_sidecar_only_state_hits_no_state_fast_path(self, home, registry_at_runtime_dir):
        """Second-uninstall scenario: only the retained sidecar remains —
        it is infrastructure, not state, so the fast path still fires and
        the sidecar survives."""
        reg = registry_at_runtime_dir
        reg.ensure_runtime_dir()
        sidecar = reg.registry_sidecar_path()
        sidecar.write_bytes(b"")
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "No memtomem state to remove" in result.output
        assert sidecar.exists()

    def test_sidecar_never_inventoried_with_real_state(self, home, registry_at_runtime_dir):
        reg = registry_at_runtime_dir
        _seed_state(home)
        reg.ensure_runtime_dir()
        sidecar = reg.registry_sidecar_path()
        sidecar.write_bytes(b"")
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert sidecar.exists(), "the mutation sidecar must never be deleted"
        assert "instances.registry.lock" not in result.output


def _make_junction(link: Path, target: Path) -> None:
    """Create an NTFS junction at *link* pointing at *target*.

    Junctions are the Windows-only redirect that ``lstat`` still reports
    as ``S_IFDIR``, so they slip past every symlink-shaped guard. No
    elevation needed, unlike Windows symlinks.
    """
    import _winapi

    _winapi.CreateJunction(str(target), str(link))


_windows_only = pytest.mark.skipif(sys.platform != "win32", reason="junctions are NTFS-only")


class TestInstanceRegistrySymlinkGuard:
    """A symlinked ``instances/`` must never be trusted or staged through
    (#1935 review): the fail-closed probe reports UNTRUSTED → refusal
    that names the offending path and prescribes removing/repairing it —
    not "retry in a moment", which can never resolve a link (#1942) —
    and the inventory/prune side never lists across the link."""

    def test_symlinked_instances_dir_refuses_and_touches_nothing(
        self, home, registry_at_runtime_dir, tmp_path
    ):
        reg = registry_at_runtime_dir
        state = _seed_state(home)
        victim_dir = tmp_path / "victim"
        victim_dir.mkdir()
        victim = victim_dir / "precious.txt"
        victim.write_text("do not touch", encoding="utf-8")
        reg.ensure_runtime_dir()
        try:
            reg.instances_dir().symlink_to(victim_dir)
        except OSError:
            pytest.skip("symlinks unavailable")

        result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])

        assert result.exit_code == 2
        assert str(reg.instances_dir()) in result.output, "refusal must name the path"
        assert "Remove or repair" in result.output
        assert "Retry in a moment" not in result.output
        assert "--force does not override" in result.output
        assert victim.read_text(encoding="utf-8") == "do not touch"
        assert "precious" not in result.output, "inventory must not list across the link"
        assert (state / "memtomem.db").exists()

    def test_junction_verdict_refuses_and_stages_nothing(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        """The junction *wiring*, pinned where CI actually runs it. The
        case above only executes on the Windows shard, which is exactly
        how the earlier link claim shipped untested; here a real
        ``instances/`` is made to answer ``is_junction()`` so the whole
        chain — untrusted → UNTRUSTED → refuse naming the path, nothing
        staged — is proven on every platform. What stays Windows-only is
        the narrow fact that a real junction answers True.

        Both guards are covered, and they fail differently: the probe
        (``_dir_state``) is what refuses, while ``_real_registry_dir`` is
        what keeps the *listing* out of the inventory — and inventory
        collection runs before the refusal block, so without it the
        target's filenames reach the user's screen even on a refused
        run."""
        reg = registry_at_runtime_dir
        state = _seed_state(home)
        entry = _seed_sentinel(reg)
        instances = reg.instances_dir()
        monkeypatch.setattr(Path, "is_junction", lambda self: self == instances)

        result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])

        assert result.exit_code == 2
        assert str(instances) in result.output, "refusal must name the path"
        assert "Remove or repair" in result.output
        assert "Retry in a moment" not in result.output
        assert (state / "memtomem.db").exists()
        assert entry.exists(), "a junctioned registry must never be staged"
        assert entry.name not in result.output, "inventory must not list across the junction"

    def test_junctioned_runtime_anchor_refuses_and_stages_nothing(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        """Guarding only the leaf leaves the whole chain open one level
        up: a junctioned *runtime dir* holds an ordinary ``instances/``
        inside the target, which passes every check made on the leaf.
        Both the probe (via ``ensure_runtime_dir``) and the inventory
        (via ``_real_registry_dir``'s anchor check) must refuse."""
        reg = registry_at_runtime_dir
        state = _seed_state(home)
        entry = _seed_sentinel(reg)
        anchor = reg.instances_dir().parent
        monkeypatch.setattr(Path, "is_junction", lambda self: self == anchor)

        result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])

        assert result.exit_code == 2
        assert str(anchor) in result.output, "refusal must name the runtime dir"
        assert "Remove or repair" in result.output
        assert "Retry in a moment" not in result.output
        assert (state / "memtomem.db").exists()
        assert entry.exists(), "a junctioned runtime anchor must never be staged"
        assert entry.name not in result.output, "inventory must not list under the junction"

    @_windows_only
    def test_junctioned_instances_dir_refuses_and_touches_nothing(
        self, home, registry_at_runtime_dir, tmp_path
    ):
        """The same contract for the redirect ``lstat`` cannot see. Before
        the junction check, ``_real_registry_dir`` handed the *target's*
        files to ``_collect_inventory``, which staged them with
        ``os.replace`` and deleted them with the staging tree — this case
        loses unrelated user files, it does not merely leak their names."""
        reg = registry_at_runtime_dir
        state = _seed_state(home)
        victim_dir = tmp_path / "victim"
        victim_dir.mkdir()
        victim = victim_dir / "precious.txt"
        victim.write_text("do not touch", encoding="utf-8")
        reg.ensure_runtime_dir()
        _make_junction(reg.instances_dir(), victim_dir)

        result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])

        assert result.exit_code == 2
        assert str(reg.instances_dir()) in result.output, "refusal must name the path"
        assert "Remove or repair" in result.output
        assert "Retry in a moment" not in result.output
        assert victim.read_text(encoding="utf-8") == "do not touch"
        assert list(victim_dir.iterdir()) == [victim]
        assert "precious" not in result.output, "inventory must not list across the junction"
        assert (state / "memtomem.db").exists()


class TestDestructiveBoundaryReprobe:
    """#1935 review round 2: liveness is re-sampled after confirmation,
    at the destructive boundary — a server that registers while the user
    sits on the prompt must refuse, not have its live state staged."""

    def test_registry_going_live_between_probe_and_delete_refuses(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        from memtomem._instance_registry import UninstallProbeResult
        from memtomem.cli import uninstall_cmd

        state = _seed_state(home)
        calls: list[int] = []

        def flapping_probe():
            calls.append(1)
            return UninstallProbeResult("NONE" if len(calls) == 1 else "LIVE")

        monkeypatch.setattr(uninstall_cmd, "_probe_registry_liveness", flapping_probe)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert len(calls) == 2, "the probe must run again at the destructive boundary"
        assert "became active while uninstall was waiting" in result.output
        assert (state / "memtomem.db").exists()
        assert (state / "config.json").exists()

    def test_registry_turning_untrusted_at_boundary_names_the_path(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        """A link that appears while the user sits on the prompt is the
        persistent cause, not a process that "became active" — the
        boundary refusal must give the same remove-or-repair remediation
        as the first gate (#1942)."""
        from memtomem._instance_registry import UninstallProbeResult
        from memtomem.cli import uninstall_cmd

        reg = registry_at_runtime_dir
        state = _seed_state(home)
        calls: list[int] = []

        def flapping_probe():
            calls.append(1)
            if len(calls) == 1:
                return UninstallProbeResult("NONE")
            return UninstallProbeResult("UNTRUSTED", untrusted_path=reg.instances_dir())

        monkeypatch.setattr(uninstall_cmd, "_probe_registry_liveness", flapping_probe)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert str(reg.instances_dir()) in result.output, "refusal must name the path"
        assert "Remove or repair" in result.output
        assert "became active" not in result.output
        assert (state / "memtomem.db").exists()

    def test_registry_turning_unknown_at_boundary_keeps_retry_advice(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        """A transient probe failure at the boundary is not evidence
        that "a process became active" — the refusal must keep the
        retry remediation, mirroring the first gate (#1942)."""
        from memtomem._instance_registry import UninstallProbeResult
        from memtomem.cli import uninstall_cmd

        state = _seed_state(home)
        calls: list[int] = []

        def flapping_probe():
            calls.append(1)
            return UninstallProbeResult("NONE" if len(calls) == 1 else "UNKNOWN")

        monkeypatch.setattr(uninstall_cmd, "_probe_registry_liveness", flapping_probe)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert "Retry in a moment" in result.output
        assert "became active" not in result.output
        assert (state / "memtomem.db").exists()

    def test_server_going_live_between_probe_and_delete_refuses(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        from memtomem.cli import uninstall_cmd
        from memtomem.cli._liveness import ServerState

        state = _seed_state(home)
        calls: list[int] = []

        def flapping_liveness():
            calls.append(1)
            if len(calls) == 1:
                return ServerState(alive=False, pid=None, pid_file=None)
            return ServerState(alive=True, pid=4242, pid_file=state / ".server.pid")

        monkeypatch.setattr(uninstall_cmd, "_check_server_liveness", flapping_liveness)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert len(calls) == 2
        assert (state / "memtomem.db").exists()

    def test_reprobe_keeps_posix_force_authority_over_pid_heuristic(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        """--force retains exactly its prior authority at the boundary:
        the POSIX pid/db-lock heuristics stay overridable there too."""
        if sys.platform == "win32":
            pytest.skip("POSIX --force contract")
        from memtomem.cli import uninstall_cmd
        from memtomem.cli._liveness import ServerState

        state = _seed_state(home)
        monkeypatch.setattr(
            uninstall_cmd,
            "_check_server_liveness",
            lambda: ServerState(alive=True, pid=4242, pid_file=state / ".server.pid"),
        )

        result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])

        assert result.exit_code == 0, result.output
        assert not state.exists()


class TestRegistrationSymlinkGuard:
    def test_registration_refuses_symlinked_instances_dir(
        self, home, registry_at_runtime_dir, tmp_path
    ):
        reg = registry_at_runtime_dir
        victim_dir = tmp_path / "victim-target"
        victim_dir.mkdir()
        reg.ensure_runtime_dir()
        try:
            reg.instances_dir().symlink_to(victim_dir)
        except OSError:
            pytest.skip("symlinks unavailable")
        db = tmp_path / "s.db"
        db.write_bytes(b"x")
        assert reg.register_instance(db) is None
        assert list(victim_dir.iterdir()) == [], "no sentinel may land in the link target"

    @_windows_only
    def test_registration_refuses_junctioned_instances_dir(
        self, home, registry_at_runtime_dir, tmp_path
    ):
        """``_dir_state`` gates registration as well as the uninstall
        probe, so its junction blindness let sentinels be written into an
        unrelated directory."""
        reg = registry_at_runtime_dir
        victim_dir = tmp_path / "victim-target"
        victim_dir.mkdir()
        reg.ensure_runtime_dir()
        _make_junction(reg.instances_dir(), victim_dir)
        db = tmp_path / "s.db"
        db.write_bytes(b"x")
        assert reg.register_instance(db) is None
        assert list(victim_dir.iterdir()) == [], "no sentinel may land in the junction target"

    def test_db_lock_going_live_between_probe_and_delete_refuses(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        from memtomem.cli import uninstall_cmd
        from memtomem.cli._db_lock import DbLockState

        state = _seed_state(home)
        calls: list[int] = []

        def flapping_db_lock(_path):
            calls.append(1)
            return DbLockState(locked=len(calls) > 1, probe_error=None)

        monkeypatch.setattr(uninstall_cmd, "_check_db_lock", flapping_db_lock)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert len(calls) == 2
        assert (state / "memtomem.db").exists()
        assert (state / "config.json").exists()


class TestPruneIsFailureSafe:
    """``_delete_inventory``'s directory prunes run *after* the staging
    move, so a directory that vanishes underneath one of them must not
    raise and skip the prunes that follow (#1937 review follow-up)."""

    def test_vanished_registry_dir_does_not_abort_the_prune_sequence(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        """The stat→listing window, made deterministic: hand the prune a
        directory that no longer exists. The pre-fix shape evaluated
        ``any(reg_dir.iterdir())`` outside its ``try`` and propagated
        ``FileNotFoundError`` out of ``_delete_inventory``, losing the
        runtime-dir prune and the success summary with it."""
        from memtomem.cli import uninstall_cmd

        state = _seed_state(home)
        ghost = home / "gone-instances"
        monkeypatch.setattr(uninstall_cmd, "_real_registry_dir", lambda: ghost)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 0, result.output
        assert result.exception is None
        assert not state.exists()

    def test_unreadable_dir_reports_not_pruned_instead_of_raising(self, tmp_path, monkeypatch):
        from memtomem.cli.uninstall_cmd import _prune_if_empty

        d = tmp_path / "d"
        d.mkdir()

        def _boom(_self):
            raise PermissionError("nope")

        monkeypatch.setattr(Path, "iterdir", _boom)
        assert _prune_if_empty(d) is False
        assert d.exists()

    def test_prunes_empty_removes_and_reports(self, tmp_path):
        from memtomem.cli.uninstall_cmd import _prune_if_empty

        d = tmp_path / "empty"
        d.mkdir()
        assert _prune_if_empty(d) is True
        assert not d.exists()

    def test_leaves_non_empty_and_missing_alone(self, tmp_path):
        from memtomem.cli.uninstall_cmd import _prune_if_empty

        full = tmp_path / "full"
        full.mkdir()
        (full / "x").write_text("x", encoding="utf-8")
        assert _prune_if_empty(full) is False
        assert full.exists()

        assert _prune_if_empty(tmp_path / "missing") is False

    def test_never_prunes_a_symlinked_dir_target(self, tmp_path):
        """The refusal must come from the link check, not from POSIX luck:
        ``rmdir`` on a symlink fails with ``ENOTDIR`` here, but Windows
        ``RemoveDirectoryW`` would happily unlink the reparse point."""
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")
        from memtomem.cli.uninstall_cmd import _prune_if_empty

        assert _prune_if_empty(link) is False
        assert link.is_symlink()
        assert target.is_dir()

    def test_link_refusal_precedes_listing_and_rmdir(self, tmp_path, monkeypatch):
        """Runs the Windows contract on POSIX: only the link check can
        produce the refusal here, since both later steps are rigged to
        fail the test if reached. Without that rigging the case passes on
        ``ENOTDIR`` alone and keeps claiming a safety the Windows shard
        disproves. ``iterdir`` is rigged too so the guard cannot drift
        below the listing, where a link to a *non-empty* directory would
        refuse for the wrong reason and an empty one would be pruned."""
        target = tmp_path / "target"
        target.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")

        def _reached(step):
            def _fail(_self, *_a, **_kw):
                raise AssertionError(f"{step} reached for a directory link")

            return _fail

        monkeypatch.setattr(Path, "iterdir", _reached("iterdir"))
        monkeypatch.setattr(Path, "rmdir", _reached("rmdir"))
        from memtomem.cli.uninstall_cmd import _prune_if_empty

        assert _prune_if_empty(link) is False
        assert link.is_symlink()

    def test_junction_refusal_precedes_listing_and_rmdir(self, tmp_path, monkeypatch):
        """The ordering contract on the *junction* axis, runnable
        everywhere. The symlink rig above cannot carry it — that case
        still passes with the junction check moved below the listing —
        and the real-junction case only runs on the Windows shard."""
        d = tmp_path / "plain"
        d.mkdir()

        def _reached(step):
            def _fail(_self, *_a, **_kw):
                raise AssertionError(f"{step} reached for a junction")

            return _fail

        monkeypatch.setattr(Path, "is_junction", lambda self: self == d)
        monkeypatch.setattr(Path, "iterdir", _reached("iterdir"))
        monkeypatch.setattr(Path, "rmdir", _reached("rmdir"))
        from memtomem.cli.uninstall_cmd import _prune_if_empty

        assert _prune_if_empty(d) is False
        assert d.is_dir()

    @_windows_only
    def test_never_prunes_a_junction(self, tmp_path):
        """Junctions are the case ``is_symlink()`` alone misses: Windows
        tags them ``IO_REPARSE_TAG_MOUNT_POINT``, so they stay
        directory-shaped to ``lstat`` and to ``is_symlink()`` while
        ``rmdir`` still removes the link."""
        import _winapi

        target = tmp_path / "target"
        target.mkdir()
        junction = tmp_path / "junction"
        _winapi.CreateJunction(str(target), str(junction))
        from memtomem.cli.uninstall_cmd import _prune_if_empty

        assert junction.is_junction()
        assert not junction.is_symlink()
        assert _prune_if_empty(junction) is False
        assert junction.is_junction()
        assert target.is_dir()


# ------------------------------------------------------------------- #1946


def _dangling_link(state: Path, name: str) -> Path:
    """Replace ``state/name`` (if seeded) with a dangling symlink."""
    link = state / name
    if link.is_dir():
        shutil.rmtree(link)
    try:
        link.symlink_to(state / "no-such-target")
    except OSError:
        pytest.skip("symlinks unavailable")
    return link


def _removed_line(output: str) -> str:
    return next(line for line in output.splitlines() if line.startswith("Removed:"))


class TestDanglingOwnedSubdirLinks:
    """A dangling ``config.d``/``memories``/``uploads`` link is our own
    leftover (#1946): inventoried, staged as an entry move, gone on a
    normal uninstall — and retained under the matching keep flag. A live
    dir link keeps the entry-moves/target-untouched behavior (#1940/#1943).
    """

    def test_dangling_config_d_symlink_inventoried_and_removed(self, home):
        state = _seed_state(home, with_fragments=False)
        link = _dangling_link(state, "config.d")
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "config.d" in result.output, "dangling link missing from the inventory"
        removed = _removed_line(result.output)
        assert "fragments" in removed and "state dir" in removed
        assert not os.path.lexists(link)
        assert not state.exists(), f"state dir not pruned, found: {list(state.iterdir())}"

    def test_dangling_memories_symlink_removed_and_state_pruned(self, home):
        state = _seed_state(home)
        link = _dangling_link(state, "memories")
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "memories" in _removed_line(result.output)
        assert not os.path.lexists(link)
        assert not state.exists(), f"state dir not pruned, found: {list(state.iterdir())}"

    def test_dangling_uploads_symlink_removed_even_with_both_keep_flags(self, home):
        state = _seed_state(home)
        link = _dangling_link(state, "uploads")
        result = CliRunner().invoke(cli, ["uninstall", "-y", "--keep-config", "--keep-data"])
        assert result.exit_code == 0, result.output
        assert "uploads" in _removed_line(result.output)
        assert not os.path.lexists(link), "uploads has no keep flag — the link must go"
        assert (state / "config.json").exists()
        assert (state / "memtomem.db").exists()

    def test_dangling_config_d_symlink_retained_under_keep_config(self, home):
        state = _seed_state(home, with_fragments=False)
        link = _dangling_link(state, "config.d")
        result = CliRunner().invoke(cli, ["uninstall", "-y", "--keep-config"])
        assert result.exit_code == 0, result.output
        assert os.path.lexists(link), "--keep-config must retain the config.d entry"
        assert "fragments" not in _removed_line(result.output)

    def test_dangling_memories_symlink_retained_under_keep_data(self, home):
        state = _seed_state(home)
        link = _dangling_link(state, "memories")
        result = CliRunner().invoke(cli, ["uninstall", "-y", "--keep-data"])
        assert result.exit_code == 0, result.output
        assert os.path.lexists(link), "--keep-data must retain the memories entry"
        assert "memories" not in _removed_line(result.output)

    def test_only_a_dangling_link_is_not_nothing_to_delete(self, home):
        """The issue's exact trap: bytes total is 0, so a byte-based gate
        prints "Nothing to delete" and exits 0 with the link in place."""
        state = home / ".memtomem"
        state.mkdir()
        link = _dangling_link(state, "config.d")
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "Nothing to delete" not in result.output
        assert not os.path.lexists(link)
        assert not state.exists(), f"state dir not pruned, found: {list(state.iterdir())}"

    def test_unresolvable_link_target_is_treated_as_dangling_and_removed(self, home):
        """``Path.exists()`` propagates ``EACCES`` (target beneath an
        unsearchable directory) instead of returning False — the
        classifier must call that dangling, not crash the inventory."""
        if sys.platform == "win32":
            pytest.skip("POSIX permission bits required")
        state = _seed_state(home)
        locked = home / "locked"
        inner = locked / "inner"
        inner.mkdir(parents=True)
        link = state / "uploads"
        try:
            link.symlink_to(inner / "gone")
        except OSError:
            pytest.skip("symlinks unavailable")
        os.chmod(locked, 0o000)
        try:
            result = CliRunner().invoke(cli, ["uninstall", "-y"])
        finally:
            os.chmod(locked, 0o700)
        assert result.exit_code == 0, result.output
        assert not os.path.lexists(link)
        assert not state.exists(), f"state dir not pruned, found: {list(state.iterdir())}"

    def test_unresolvable_link_nested_in_owned_dir_does_not_abort(self, home):
        """``rglob`` listing hits ``is_file()`` on a nested unresolvable
        link — the inventory must skip it, and the whole-dir stage move
        still removes it with its tree."""
        if sys.platform == "win32":
            pytest.skip("POSIX permission bits required")
        state = _seed_state(home)
        locked = home / "locked"
        inner = locked / "inner"
        inner.mkdir(parents=True)
        nested = state / "memories" / "bad"
        try:
            nested.symlink_to(inner / "gone")
        except OSError:
            pytest.skip("symlinks unavailable")
        os.chmod(locked, 0o000)
        try:
            result = CliRunner().invoke(cli, ["uninstall", "-y"])
        finally:
            os.chmod(locked, 0o700)
        assert result.exit_code == 0, result.output
        assert not os.path.lexists(nested)
        assert not state.exists(), f"state dir not pruned, found: {list(state.iterdir())}"

    def test_live_link_to_empty_dir_is_not_nothing_to_delete(self, home):
        """Zero listed files, zero bytes — but the plan stages the link
        entry, so a byte/file-count gate would falsely short-circuit."""
        state = home / ".memtomem"
        state.mkdir()
        target = home / "empty-target"
        target.mkdir()
        link = state / "uploads"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "Nothing to delete" not in result.output
        # The staged entry must show a row, not "(nothing found)" — the
        # user is asked to confirm deleting it.
        assert "nothing found" not in result.output
        assert "uploads" in result.output
        assert not os.path.lexists(link)
        assert target.is_dir(), "the linked-to directory must be untouched"
        assert not state.exists(), f"state dir not pruned, found: {list(state.iterdir())}"

    def test_empty_owned_dir_alone_is_deleted_not_nothing(self, home):
        """An empty real owned dir is our own skeleton: staged and gone,
        not "Nothing to delete" with the state dir left behind."""
        state = home / ".memtomem"
        state.mkdir()
        (state / "memories").mkdir()
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "Nothing to delete" not in result.output
        # Shown, not "(nothing found)" — the confirmation asks about it.
        assert "nothing found" not in result.output
        assert "memories" in result.output
        assert not state.exists(), f"state dir not pruned, found: {list(state.iterdir())}"

    def test_empty_config_d_retained_under_keep_config(self, home):
        """The prune backstop must honor keep flags: an empty config.d is
        retained state under --keep-config, not swept as a stray skeleton.
        config.json is seeded so staging proceeds (the run isn't a no-op)."""
        state = home / ".memtomem"
        state.mkdir()
        (state / "config.json").write_text("{}", encoding="utf-8")
        (state / "config.d").mkdir()
        (state / "uploads").mkdir()  # no keep flag → still removed
        result = CliRunner().invoke(cli, ["uninstall", "-y", "--keep-config"])
        assert result.exit_code == 0, result.output
        assert (state / "config.d").is_dir(), "--keep-config must retain empty config.d"
        assert (state / "config.json").exists()
        assert not (state / "uploads").exists(), "uploads has no keep flag"

    def test_empty_memories_retained_under_keep_data(self, home):
        state = home / ".memtomem"
        state.mkdir()
        (state / "memtomem.db").write_bytes(b"x")
        (state / "memories").mkdir()
        # config.json is config-side, so --keep-data deletes it: staging
        # proceeds and _delete_inventory (hence the prune loop) actually
        # runs — without it the gate short-circuits and the prune is never
        # reached, so the keep-flag guard would pass for the wrong reason.
        (state / "config.json").write_text("{}", encoding="utf-8")
        result = CliRunner().invoke(cli, ["uninstall", "-y", "--keep-data"])
        assert result.exit_code == 0, result.output
        assert (state / "memories").is_dir(), "--keep-data must retain empty memories"
        assert (state / "memtomem.db").exists()
        assert not (state / "config.json").exists()

    def test_substitute_dir_row_reads_zero_bytes(self, home):
        """A live link to an empty external dir shows the entry, but its
        follow-stat inode size must not inflate the delete total — we
        remove only the link, never the target's bytes."""
        state = home / ".memtomem"
        state.mkdir()
        (state / "memtomem.db").write_bytes(b"1234")  # 4 B of real deletable state
        target = home / "empty-target"
        target.mkdir()
        link = state / "uploads"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert "uploads" in result.output
        # Total reflects only the db's 4 B, not the target dir's inode size.
        assert "Total to delete: ~4 B" in result.output, result.output

    def test_entry_probe_calls_only_genuine_absence_absent(self, home, monkeypatch):
        """``lexists`` semantics (every lstat error → absent) would let
        the staging loop silently skip a planned entry; only ENOENT /
        ENOTDIR may read as absent."""
        from memtomem.cli import uninstall_cmd

        assert uninstall_cmd._entry_present(home / "nope") is False

        def _eacces(path):
            raise PermissionError(errno.EACCES, "denied")

        monkeypatch.setattr(uninstall_cmd.os, "lstat", _eacces)
        assert uninstall_cmd._entry_present(home / "nope") is True

    def test_mid_stage_failure_rolls_dangling_link_back(self, home, monkeypatch):
        """Uploads stage before the database in the plan, so failing the
        db move exercises rollback of an already-staged dangling link."""
        from memtomem.cli import uninstall_cmd

        state = _seed_state(home)
        link = _dangling_link(state, "uploads")

        real_replace = os.replace

        def _fail_on_db(src, dst, **kwargs):
            if uninstall_cmd._STAGING_PREFIX in str(dst) and Path(src).name == "memtomem.db":
                raise OSError(errno.EACCES, "fake stage failure")
            return real_replace(src, dst, **kwargs)

        monkeypatch.setattr(uninstall_cmd.os, "replace", _fail_on_db)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 2, result.output
        assert os.path.lexists(link), "rollback must restore the link entry"
        assert (state / "memtomem.db").exists()

    def test_live_dir_link_entry_moved_target_untouched(self, home):
        """Success-path pin of the #1940/#1943 contract: the link *entry*
        is deleted, the linked-to content is not."""
        state = _seed_state(home)
        target = home / "elsewhere"
        target.mkdir()
        (target / "keep.md").write_text("# keep", encoding="utf-8")
        link = state / "uploads"
        try:
            link.symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert not os.path.lexists(link)
        assert (target / "keep.md").exists(), "uninstall followed the link into the target"

    @_windows_only
    def test_dangling_junction_config_d_removed(self, home):
        """A dangling junction is lstat-``S_IFDIR`` while ``is_dir()`` is
        False — the shape ``is_symlink()``-only detection misses."""
        state = _seed_state(home, with_fragments=False)
        victim = home / "victim"
        victim.mkdir()
        junction = state / "config.d"
        _make_junction(junction, victim)
        victim.rmdir()
        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 0, result.output
        assert not os.path.lexists(junction)
        assert not state.exists(), f"state dir not pruned, found: {list(state.iterdir())}"

    @_windows_only
    def test_dangling_junction_retained_under_keep_config(self, home):
        state = _seed_state(home, with_fragments=False)
        victim = home / "victim"
        victim.mkdir()
        junction = state / "config.d"
        _make_junction(junction, victim)
        victim.rmdir()
        result = CliRunner().invoke(cli, ["uninstall", "-y", "--keep-config"])
        assert result.exit_code == 0, result.output
        assert os.path.lexists(junction), "--keep-config must retain the config.d entry"


# ------------------------------------------------------------------- #1936


def _bar_child_setup(rt_str: str):
    """Point a spawned child's registry module at ``rt_str``."""
    import memtomem._instance_registry as _reg

    target = Path(rt_str)

    def _rt() -> Path:
        return target

    def _ensure() -> Path:
        target.mkdir(mode=0o700, exist_ok=True)
        return target

    _reg.runtime_dir = _rt
    _reg.ensure_runtime_dir = _ensure
    return _reg


def _child_try_exclusive_barrier(rt_str: str, q) -> None:
    """Attempt the conflicting acquire from a separate process."""
    _reg = _bar_child_setup(rt_str)
    try:
        _reg.acquire_uninstall_lifecycle_barrier(timeout_s=1.0).release()
    except Exception as exc:  # noqa: BLE001 — the type name is the signal
        q.put(("refused", type(exc).__name__))
        return
    q.put(("acquired", ""))


def _child_hold_shared_barrier(rt_str: str, q, release) -> None:
    """Stand in for a live server holding the barrier shared."""
    _reg = _bar_child_setup(rt_str)
    barrier = _reg.acquire_server_lifecycle_barrier()
    q.put(("held",))
    release.wait(60)
    barrier.release()


def _child_uninstall_blocking_in_staging(home_str: str, rt_str: str, q, release) -> None:
    """Run a *real* ``mm uninstall`` that parks inside its staging phase.

    The barrier is only useful if uninstall keeps holding it across the
    deletion — a child that merely grabs the lock would pass the same
    assertions even if production released it right after probing.

    The runtime dir is **injected as the parent's already-resolved path**,
    never re-derived from the environment. Letting the child recompute it
    put the two processes on different barrier files on Windows, where
    ``runtime_dir()`` ignores ``$XDG_RUNTIME_DIR`` entirely and falls
    through to ``tempfile.gettempdir()`` — so the parent's acquire
    succeeded and the test claimed a refusal that never happened.
    """
    import os

    os.environ["HOME"] = home_str
    os.environ["USERPROFILE"] = home_str  # ``Path.home()`` on Windows

    from click.testing import CliRunner

    from memtomem.cli import cli
    from memtomem.cli import uninstall_cmd as _uninstall
    from memtomem.cli import _bootstrap
    import memtomem._instance_registry as _reg

    rt = Path(rt_str)

    def _rt() -> Path:
        return rt

    def _ensure_rt() -> Path:
        rt.mkdir(mode=0o700, parents=True, exist_ok=True)
        return rt

    # Every seam that resolves the runtime dir, because they are separate
    # module-level imports: the registry resolves the barrier path, while
    # uninstall holds its own for the staging anchor / prune and for the
    # pid file it inventories. Leaving ``server_pid_path`` unpatched would
    # point the child's inventory at the *real* runtime dir, outside the
    # test sandbox — staging a file this test never created.
    _reg.runtime_dir = _rt
    _reg.ensure_runtime_dir = _ensure_rt
    _uninstall.runtime_dir = _rt
    _uninstall.server_pid_path = lambda: rt / "server.pid"

    home = Path(home_str)
    _bootstrap._CONFIG_PATH = home / ".memtomem" / "config.json"
    _uninstall._DEFAULT_STATE_DIR = home / ".memtomem"

    real_stage = _uninstall._stage_inventory

    def blocking_stage(*args, **kwargs):
        q.put(("staging",))
        release.wait(60)
        return real_stage(*args, **kwargs)

    _uninstall._stage_inventory = blocking_stage
    result = CliRunner().invoke(cli, ["uninstall", "-y"])
    q.put(("done", result.exit_code))


class TestLifecycleBarrierRefusesUninstall:
    """A server holding the barrier blocks deletion even when nothing
    else can see it (#1936).

    Deliberately seeds **no sentinel**: that isolates the barrier from
    the #1935 registry gate, and it is the real-world case the lifetime
    hold exists for — a server whose ``register_instance`` failed has an
    open store that nothing advertises.
    """

    def test_shared_holder_refuses_deletion(self, home, registry_at_runtime_dir, monkeypatch):
        import multiprocessing as mp

        from memtomem.cli import uninstall_cmd

        reg = registry_at_runtime_dir
        state = _seed_state(home)
        monkeypatch.setattr(reg, "_BARRIER_TIMEOUT_S", 0.3)

        ctx = mp.get_context("spawn")
        q, release = ctx.Queue(), ctx.Event()
        holder = ctx.Process(
            target=_child_hold_shared_barrier, args=(str(reg.runtime_dir()), q, release)
        )
        holder.start()
        try:
            assert q.get(timeout=30)[0] == "held"
            assert reg.probe_all_for_uninstall().state == "NONE", (
                "no sentinel — registry sees nothing"
            )

            result = CliRunner().invoke(cli, ["uninstall", "-y"])

            assert result.exit_code == 2, result.output
            assert "lifecycle barrier" in result.output
            assert (state / "memtomem.db").exists()
            assert (state / "config.json").exists()
        finally:
            release.set()
            holder.join(timeout=30)
            if holder.is_alive():
                holder.kill()
                holder.join(timeout=30)
        assert uninstall_cmd is not None  # import pin

    def test_force_does_not_override_the_barrier(self, home, registry_at_runtime_dir, monkeypatch):
        """A held flock is never stale — the kernel releases it when its
        holder dies — so there is nothing here for ``--force`` to
        legitimately override, and the output must not suggest otherwise.
        """
        import multiprocessing as mp

        reg = registry_at_runtime_dir
        state = _seed_state(home)
        monkeypatch.setattr(reg, "_BARRIER_TIMEOUT_S", 0.3)

        ctx = mp.get_context("spawn")
        q, release = ctx.Queue(), ctx.Event()
        holder = ctx.Process(
            target=_child_hold_shared_barrier, args=(str(reg.runtime_dir()), q, release)
        )
        holder.start()
        try:
            assert q.get(timeout=30)[0] == "held"

            result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])

            assert result.exit_code == 2, result.output
            assert "--force" not in result.output
            assert (state / "memtomem.db").exists()
        finally:
            release.set()
            holder.join(timeout=30)
            if holder.is_alive():
                holder.kill()
                holder.join(timeout=30)


class TestUninstallHoldsBarrierThroughStaging:
    """The other schedule: a real uninstall inside staging fails a server
    start *before* it opens the store."""

    def test_server_init_refused_while_uninstall_stages(
        self, home, registry_at_runtime_dir, monkeypatch
    ):
        import multiprocessing as mp

        reg = registry_at_runtime_dir
        _seed_state(home)
        # Hand the child the *resolved* path — see the helper's docstring
        # for why re-deriving it from env put the two processes on
        # different barrier files on Windows.
        rt = str(reg.ensure_runtime_dir())
        monkeypatch.setattr(reg, "_BARRIER_TIMEOUT_S", 0.3)

        ctx = mp.get_context("spawn")
        q, release = ctx.Queue(), ctx.Event()
        worker = ctx.Process(
            target=_child_uninstall_blocking_in_staging, args=(str(home), rt, q, release)
        )
        worker.start()
        try:
            assert q.get(timeout=60)[0] == "staging", "uninstall never reached staging"
            # Uninstall is parked *inside* staging, still holding the
            # barrier: a server starting now must be refused.
            with pytest.raises(reg.BarrierTimeout):
                reg.acquire_server_lifecycle_barrier(timeout_s=0.3)
        finally:
            release.set()
            worker.join(timeout=60)
            if worker.is_alive():
                worker.kill()
                worker.join(timeout=30)


class TestUninstallAlwaysReleasesTheBarrier:
    """Every exit path frees the barrier.

    These re-acquire **inside the test**: the autouse fixture sweeps
    leaked barriers at teardown, so a later green test would say nothing
    about whether production released anything.
    """

    @staticmethod
    def _assert_free(reg) -> None:
        """Prove the barrier is free from another *process*.

        Same-process re-acquisition is the weaker check: Windows can grant
        a second handle in the owning process, so a dropped ``release()``
        could pass there and then be hidden by the autouse sweep.
        """
        import multiprocessing as mp

        ctx = mp.get_context("spawn")
        q = ctx.Queue()
        child = ctx.Process(target=_child_try_exclusive_barrier, args=(str(reg.runtime_dir()), q))
        child.start()
        try:
            outcome, detail = q.get(timeout=30)
        finally:
            child.join(timeout=30)
            if child.is_alive():
                child.kill()
                child.join(timeout=30)
        assert outcome == "acquired", f"uninstall left the barrier held ({detail})"

    def test_released_after_successful_delete(self, home, registry_at_runtime_dir):
        reg = registry_at_runtime_dir
        _seed_state(home)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 0, result.output
        self._assert_free(reg)

    def test_released_after_late_refusal(self, home, registry_at_runtime_dir, monkeypatch):
        """The boundary re-probe exits with ``SystemExit`` from inside the
        held region — the ``finally`` must still run."""
        from memtomem._instance_registry import UninstallProbeResult
        from memtomem.cli import uninstall_cmd

        reg = registry_at_runtime_dir
        _seed_state(home)
        calls: list[int] = []

        def flapping_probe():
            calls.append(1)
            return UninstallProbeResult("NONE" if len(calls) == 1 else "LIVE")

        monkeypatch.setattr(uninstall_cmd, "_probe_registry_liveness", flapping_probe)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        assert len(calls) == 2
        self._assert_free(reg)

    def test_released_after_staging_failure(self, home, registry_at_runtime_dir, monkeypatch):
        from memtomem.cli import uninstall_cmd

        reg = registry_at_runtime_dir
        _seed_state(home)

        def boom(*_a, **_k):
            raise uninstall_cmd._UninstallStagingError(
                failing_path=Path("x"),
                original=OSError("nope"),
                rollback_errors=[],
                staging_roots=[],
            )

        monkeypatch.setattr(uninstall_cmd, "_delete_inventory", boom)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 2
        self._assert_free(reg)


class TestBarrierIsRetainedInfrastructure:
    """Production layout: the barrier file survives uninstall and is never
    inventoried — and, as a consequence, the runtime dir stops being
    pruned. Pinned with ``registry_at_runtime_dir`` because the suite-wide
    isolation would otherwise park the barrier outside every staging
    anchor and hide all of this.
    """

    def test_barrier_survives_and_keeps_the_runtime_dir(self, home, registry_at_runtime_dir):
        reg = registry_at_runtime_dir
        _seed_state(home)

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 0, result.output
        barrier = reg.lifecycle_barrier_path()
        assert barrier.exists(), "retained infrastructure must not be deleted"
        assert str(barrier) not in result.output, "barrier must never be inventoried"
        assert reg.runtime_dir().exists(), (
            "the retained barrier keeps the runtime dir non-empty — expected; "
            "the runtime dir is volatile and self-cleans"
        )

    def test_barrier_only_state_still_takes_the_empty_state_fast_path(
        self, home, registry_at_runtime_dir
    ):
        """A leftover ``lifecycle.lock`` alone is not user state."""
        reg = registry_at_runtime_dir
        reg.ensure_runtime_dir()
        reg.acquire_server_lifecycle_barrier(timeout_s=5.0).release()
        assert reg.lifecycle_barrier_path().exists()

        result = CliRunner().invoke(cli, ["uninstall", "-y"])

        assert result.exit_code == 0, result.output
        assert "No memtomem state to remove" in result.output
