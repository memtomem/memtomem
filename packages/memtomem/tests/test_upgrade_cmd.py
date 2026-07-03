"""Tests for ``mm upgrade`` — kill-then-reinstall hygiene wrapper (#443)."""

from __future__ import annotations

import json
import sys

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


_DEAD = ServerState(alive=False, pid=None, pid_file=None)


def _patch_liveness(monkeypatch, state: ServerState, web: ServerState = _DEAD) -> None:
    """Patch both probes; web defaults to dead so tests never touch the real runtime dir."""
    monkeypatch.setattr(upgrade_cmd, "check_server_liveness", lambda: state)
    monkeypatch.setattr(upgrade_cmd, "check_web_liveness", lambda: web)


# ---------------------------------------------------------------- tests


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Asserts the POSIX message; Windows takes the skipping-process-termination branch (covered by test_windows_skips_kill)",
)
def test_no_running_server_just_reinstalls(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 0, result.output
    assert "No running server or web UI detected" in result.output
    assert calls == [["uv", "tool", "install", "--refresh", "--reinstall", "memtomem"]]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: SIGTERM kill path; Windows skips process termination entirely (covered by test_windows_skips_kill)",
)
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: SIGKILL escalation path; Windows skips process termination entirely (covered by test_windows_skips_kill)",
)
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: exercises the SIGKILL respawn-detection path; Windows skips kill entirely (covered by test_windows_skips_kill)",
)
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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: web-UI kill path; Windows skips process termination entirely (covered by test_windows_skips_web_kill)",
)
def test_running_web_ui_is_stopped(monkeypatch, tmp_path, fake_uv, force_tty):
    """#1569: a live ``mm web`` must be stopped, not survive the byte-swap."""
    calls, _configure = fake_uv
    web_pid_file = tmp_path / "web.pid"
    web_pid_file.write_text("4242\n8080\n2026-07-03T00:00:00+00:00\n")
    web_info_file = tmp_path / "web.json"
    web_info_file.write_text('{"pid": 4242, "port": 8080}')
    _patch_liveness(
        monkeypatch,
        _DEAD,
        web=ServerState(alive=True, pid=4242, pid_file=web_pid_file, port=8080),
    )

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda pid: False)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--grace", "1"])
    assert result.exit_code == 0, result.output
    assert "Stop running web UI (pid 4242" in result.output
    assert (4242, upgrade_cmd.signal.SIGTERM) in sent
    assert not web_pid_file.exists()
    # SIGKILL-path leftover metadata sidecar is swept alongside the pid file.
    assert not web_info_file.exists()
    assert "Stopped pid 4242." in result.output
    assert calls  # uv was invoked


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: kill paths; Windows skips process termination entirely (covered by test_windows_skips_web_kill)",
)
def test_server_and_web_both_stopped(monkeypatch, tmp_path, fake_uv, force_tty):
    _calls, _configure = fake_uv
    server_pid_file = tmp_path / "server.pid"
    server_pid_file.write_text("12345")
    web_pid_file = tmp_path / "web.pid"
    web_pid_file.write_text("4242\n8080\n2026-07-03T00:00:00+00:00\n")
    _patch_liveness(
        monkeypatch,
        ServerState(alive=True, pid=12345, pid_file=server_pid_file),
        web=ServerState(alive=True, pid=4242, pid_file=web_pid_file, port=8080),
    )

    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda pid: False)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--grace", "1"])
    assert result.exit_code == 0, result.output
    assert (12345, upgrade_cmd.signal.SIGTERM) in sent
    assert (4242, upgrade_cmd.signal.SIGTERM) in sent
    assert not server_pid_file.exists()
    assert not web_pid_file.exists()
    assert "Stopped pids 12345, 4242." in result.output


def test_dry_run_json_includes_web(monkeypatch, tmp_path, fake_uv, force_tty):
    calls, _configure = fake_uv
    web_pid_file = tmp_path / "web.pid"
    web_pid_file.write_text("4242\n8080\n2026-07-03T00:00:00+00:00\n")
    _patch_liveness(
        monkeypatch,
        _DEAD,
        web=ServerState(alive=True, pid=4242, pid_file=web_pid_file, port=8080),
    )

    result = CliRunner().invoke(cli, ["upgrade", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["would_kill"] == [4242]
    assert payload["would_remove"] == [str(web_pid_file)]
    assert calls == []
    assert web_pid_file.exists()


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: exercises the sidecar respawn-detection path; Windows skips kill entirely",
)
def test_web_sidecar_kept_if_respawned_after_pid_cleanup(monkeypatch, tmp_path, fake_uv, force_tty):
    """A web UI respawned between the pid-file cleanup and the sidecar sweep
    must keep its fresh ``web.json``."""
    _calls, _configure = fake_uv
    web_pid_file = tmp_path / "web.pid"
    web_pid_file.write_text("4242\n8080\n2026-07-03T00:00:00+00:00\n")
    web_info_file = tmp_path / "web.json"
    web_info_file.write_text('{"pid": 99999, "port": 8080}')
    _patch_liveness(
        monkeypatch,
        _DEAD,
        web=ServerState(alive=True, pid=4242, pid_file=web_pid_file, port=8080),
    )
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda pid: False)

    # First re-probe (pid-file unlink guard in _stop_server) sees no holder;
    # second re-probe (sidecar sweep) sees the respawned web UI.
    probes = iter(
        [
            ServerState(alive=False, pid=None, pid_file=web_pid_file),
            ServerState(alive=True, pid=99999, pid_file=web_pid_file),
        ]
    )
    monkeypatch.setattr(upgrade_cmd, "probe_pid_file", lambda p: next(probes))

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--grace", "0.1"])
    assert result.exit_code == 0, result.output
    assert not web_pid_file.exists()
    assert web_info_file.exists()


def test_windows_dry_run_json_reports_no_kills(monkeypatch, tmp_path, fake_uv, force_tty):
    """Windows skips the kill stage, so dry-run JSON must not claim otherwise."""
    calls, _configure = fake_uv
    server_pid_file = tmp_path / "server.pid"
    server_pid_file.write_text("12345")
    web_pid_file = tmp_path / "web.pid"
    web_pid_file.write_text("4242\n8080\n2026-07-03T00:00:00+00:00\n")
    _patch_liveness(
        monkeypatch,
        ServerState(alive=True, pid=12345, pid_file=server_pid_file),
        web=ServerState(alive=True, pid=4242, pid_file=web_pid_file, port=8080),
    )
    monkeypatch.setattr(upgrade_cmd.sys, "platform", "win32")

    result = CliRunner().invoke(cli, ["upgrade", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["would_kill"] == []
    assert payload["would_remove"] == []
    assert calls == []


def test_windows_skips_web_kill(monkeypatch, tmp_path, fake_uv, force_tty):
    calls, _configure = fake_uv
    web_pid_file = tmp_path / "web.pid"
    web_pid_file.write_text("4242\n8080\n2026-07-03T00:00:00+00:00\n")
    _patch_liveness(
        monkeypatch,
        _DEAD,
        web=ServerState(alive=True, pid=4242, pid_file=web_pid_file, port=8080),
    )
    monkeypatch.setattr(upgrade_cmd.sys, "platform", "win32")

    def boom(*_a, **_k):
        raise AssertionError("os.kill must not be called on Windows")

    monkeypatch.setattr(upgrade_cmd.os, "kill", boom)

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 0, result.output
    assert "Detected Windows" in result.output
    assert "mm web" in result.output
    assert calls  # uv still ran
    assert web_pid_file.exists()


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
