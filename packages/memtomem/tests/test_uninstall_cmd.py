"""Tests for ``mm uninstall`` — local state cleanup CLI.

Coverage spans the install-context inventory, flag combinations, server
liveness refusal, partial-deletion error path, and the ``RuntimeProfile``
private-import pin so any rename/move in ``cli.init_cmd`` breaks here
loud and immediate (MEDIUM 6 mitigation).
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli


# ---------------------------------------------------------------- fixtures


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Tmp HOME with both env override and _bootstrap._CONFIG_PATH patched.

    Mirrors the isolation pattern from ``test_cli_index_noop_e2e.py``: the
    module-level ``_bootstrap._CONFIG_PATH = Path.home() / ...`` is bound
    at import time, so ``monkeypatch.setenv("HOME")`` alone leaves it
    pointing at the developer's real home. Patching it directly is
    required for hermetic tests.
    """
    from memtomem.cli import _bootstrap
    from memtomem.cli import uninstall_cmd

    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
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
    def test_refuses_when_server_alive(self, home):
        state = _seed_state(home)
        # Use the current process pid — guaranteed alive for the test duration.
        (state / ".server.pid").write_text(str(os.getpid()), encoding="utf-8")

        result = CliRunner().invoke(cli, ["uninstall", "-y"])
        assert result.exit_code == 2
        assert "Server still running" in result.output
        assert str(os.getpid()) in result.output
        # nothing deleted
        assert (state / "memtomem.db").exists()
        assert (state / "config.json").exists()

    def test_force_overrides_liveness(self, home):
        state = _seed_state(home)
        (state / ".server.pid").write_text(str(os.getpid()), encoding="utf-8")

        result = CliRunner().invoke(cli, ["uninstall", "-y", "--force"])
        assert result.exit_code == 0, result.output
        assert not state.exists()


# -------------------------------------------------------------------- 13


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
        assert not (state / "memtomem.db").exists()
