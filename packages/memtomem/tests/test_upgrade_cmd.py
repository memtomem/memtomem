"""Tests for ``mm upgrade`` — kill-then-reinstall hygiene wrapper (#443)."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli import upgrade_cmd
from memtomem.cli._liveness import ServerState


@pytest.fixture
def force_tty(monkeypatch):
    monkeypatch.setattr(upgrade_cmd, "_isatty", lambda: True)


@pytest.fixture(autouse=True)
def _no_extras_by_default(monkeypatch):
    """Default tests assume the auto-detect probe finds nothing.

    Individual tests opt in to a non-empty receipt by re-patching
    ``_detect_installed_extras``.
    """
    monkeypatch.setattr(upgrade_cmd, "_detect_installed_extras", lambda: [])


@pytest.fixture
def fake_uv(monkeypatch):
    """Capture subprocess.run invocations and return scripted results."""

    calls: list[list[str]] = []

    class _Result:
        def __init__(self, returncode: int = 0, stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = stderr

    state = {"result": _Result(), "raise_exc": None}

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(list(cmd))
        if state["raise_exc"] is not None:
            raise state["raise_exc"]
        return state["result"]

    monkeypatch.setattr(upgrade_cmd.subprocess, "run", fake_run)

    def configure(*, returncode: int = 0, stderr: str = "", raise_exc=None):
        state["result"] = _Result(returncode=returncode, stderr=stderr)
        state["raise_exc"] = raise_exc

    return calls, configure


def _patch_liveness(monkeypatch, state: ServerState) -> None:
    monkeypatch.setattr(upgrade_cmd, "check_server_liveness", lambda: state)


# ---------------------------------------------------------------- tests


def test_no_running_server_just_reinstalls(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 0, result.output
    assert "No running server detected" in result.output
    assert calls == [["uv", "tool", "install", "--refresh", "--reinstall", "memtomem"]]


def test_running_server_sigterm_path(monkeypatch, tmp_path, fake_uv, force_tty):
    calls, _configure = fake_uv
    pid_file = tmp_path / "server.pid"
    pid_file.write_text("12345")
    _patch_liveness(monkeypatch, ServerState(alive=True, pid=12345, pid_file=pid_file))

    sent: list[tuple[int, int]] = []

    def fake_kill(pid: int, sig: int) -> None:
        sent.append((pid, sig))

    # _pid_alive() returns False on the first poll → graceful exit path.
    monkeypatch.setattr(upgrade_cmd.os, "kill", fake_kill)
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda pid: False)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--grace", "1"])
    assert result.exit_code == 0, result.output
    assert sent and sent[0][1] == upgrade_cmd.signal.SIGTERM
    assert all(s != upgrade_cmd.signal.SIGKILL for _pid, s in sent)
    assert not pid_file.exists()
    assert calls  # uv was invoked


def test_running_server_escalates_to_sigkill(monkeypatch, tmp_path, fake_uv, force_tty):
    _calls, _configure = fake_uv
    pid_file = tmp_path / "server.pid"
    pid_file.write_text("12345")
    _patch_liveness(monkeypatch, ServerState(alive=True, pid=12345, pid_file=pid_file))

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    # Stays alive forever → grace expires → SIGKILL.
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda pid: True)
    monkeypatch.setattr(upgrade_cmd.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        upgrade_cmd.time,
        "monotonic",
        _make_monotonic([0.0, 0.0, 1.0, 2.0]),  # past deadline immediately
    )

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--grace", "0.5"])
    assert result.exit_code == 0, result.output
    sigs = [s for _pid, s in sent]
    assert upgrade_cmd.signal.SIGTERM in sigs
    assert upgrade_cmd.signal.SIGKILL in sigs
    assert not pid_file.exists()


def test_windows_skips_kill(monkeypatch, tmp_path, fake_uv, force_tty):
    calls, _configure = fake_uv
    pid_file = tmp_path / "server.pid"
    pid_file.write_text("12345")
    _patch_liveness(monkeypatch, ServerState(alive=True, pid=12345, pid_file=pid_file))
    monkeypatch.setattr(upgrade_cmd.sys, "platform", "win32")

    def boom(*_a, **_k):
        raise AssertionError("os.kill must not be called on Windows")

    monkeypatch.setattr(upgrade_cmd.os, "kill", boom)

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 0, result.output
    assert "Detected Windows" in result.output
    assert calls  # uv still ran
    # We also leave the pid file alone — Windows users may need it.
    assert pid_file.exists()


def test_version_pin_passes_to_uv(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--version", "0.1.30"])
    assert result.exit_code == 0, result.output
    assert calls == [["uv", "tool", "install", "--refresh", "--reinstall", "memtomem==0.1.30"]]


def test_uv_failure_propagates(monkeypatch, fake_uv, force_tty):
    _calls, configure = fake_uv
    configure(returncode=1, stderr="resolver: no matching version")
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 1
    assert "uv tool install failed" in result.output
    assert "no matching version" in result.output


def test_dry_run_does_nothing(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))

    result = CliRunner().invoke(cli, ["upgrade", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert calls == []
    assert "Reinstall:" in result.output


def test_json_output_shape_success(monkeypatch, fake_uv, force_tty):
    _calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["reinstalled"] == "memtomem"
    assert payload["killed"] == []
    assert payload["removed"] == []


def test_non_tty_without_yes_aborts(monkeypatch, fake_uv):
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))
    monkeypatch.setattr(upgrade_cmd, "_isatty", lambda: False)

    result = CliRunner().invoke(cli, ["upgrade"])
    assert result.exit_code != 0
    assert calls == []


def test_extras_auto_detected_from_receipt(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))
    monkeypatch.setattr(upgrade_cmd, "_detect_installed_extras", lambda: ["all"])

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 0, result.output
    assert calls == [["uv", "tool", "install", "--refresh", "--reinstall", "memtomem[all]"]]
    assert "auto-detected" in result.output


def test_extras_flag_overrides_detection(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))
    monkeypatch.setattr(upgrade_cmd, "_detect_installed_extras", lambda: ["all"])

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--extras", "onnx,web"])
    assert result.exit_code == 0, result.output
    assert calls == [["uv", "tool", "install", "--refresh", "--reinstall", "memtomem[onnx,web]"]]


def test_extras_none_suppresses(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))
    monkeypatch.setattr(upgrade_cmd, "_detect_installed_extras", lambda: ["all"])

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--extras", "none"])
    assert result.exit_code == 0, result.output
    assert calls == [["uv", "tool", "install", "--refresh", "--reinstall", "memtomem"]]


def test_extras_combined_with_version_pin(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))
    monkeypatch.setattr(upgrade_cmd, "_detect_installed_extras", lambda: ["all"])

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--version", "0.1.32"])
    assert result.exit_code == 0, result.output
    assert calls == [["uv", "tool", "install", "--refresh", "--reinstall", "memtomem[all]==0.1.32"]]


def test_pid_file_unlink_skipped_if_respawned(monkeypatch, tmp_path, fake_uv, force_tty):
    """SIGKILL path: a fresh server respawns at the same pid file path
    inside the settle window. We must NOT delete its lockfile."""
    _calls, _configure = fake_uv
    pid_file = tmp_path / "server.pid"
    pid_file.write_text("12345")
    _patch_liveness(monkeypatch, ServerState(alive=True, pid=12345, pid_file=pid_file))
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda pid: False)

    # Re-probe at unlink time sees a live writer (the respawn).
    monkeypatch.setattr(
        upgrade_cmd,
        "probe_pid_file",
        lambda p: ServerState(alive=True, pid=99999, pid_file=p),
    )

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--grace", "0.1"])
    assert result.exit_code == 0, result.output
    assert pid_file.exists()
    assert "freshly started writer" in result.output


def test_version_specifier_rejected(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--version", ">=0.1.30"])
    assert result.exit_code != 0
    assert "not a bare PEP 440 release" in result.output
    assert calls == []


def test_version_prerelease_accepted(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--version", "0.1.30rc1"])
    assert result.exit_code == 0, result.output
    assert calls == [["uv", "tool", "install", "--refresh", "--reinstall", "memtomem==0.1.30rc1"]]


def test_cancel_exits_zero_and_json_consistent(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))

    # Decline confirmation by feeding "n" to click.confirm.
    result = CliRunner().invoke(cli, ["upgrade", "--json"], input="n\n")
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload == {"ok": True, "cancelled": True}
    assert calls == []


def _make_monotonic(values: list[float]):
    """Helper: sequential monotonic stamps then sticky last value."""
    state = {"i": 0}

    def _now() -> float:
        i = state["i"]
        if i < len(values):
            state["i"] += 1
            return values[i]
        return values[-1]

    return _now
