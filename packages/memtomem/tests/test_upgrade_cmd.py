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


@pytest.fixture(autouse=True)
def _no_db_probe_by_default(monkeypatch):
    """Default tests skip the post-stop DB write-lock probe (#1606).

    ``_resolve_db_path`` loads the real user config; returning None here
    keeps every test off the real ``~/.memtomem``. Probe tests opt in by
    re-patching ``_resolve_db_path`` + ``check_db_lock``.
    """
    monkeypatch.setattr(upgrade_cmd, "_resolve_db_path", lambda: None, raising=False)


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


def _patch_liveness(
    monkeypatch,
    state: ServerState | list[ServerState],
    web: ServerState = _DEAD,
    *,
    post: list[ServerState] | None = None,
    snapshots: list[list[ServerState]] | None = None,
    web_snapshots: list[ServerState] | None = None,
) -> list[list[ServerState]]:
    """Patch initial, pre-install, and post-install complete inventories."""
    initial = state if isinstance(state, list) else ([state] if state.alive else [])
    server_snapshots = snapshots or [initial, [] if post is None else post, []]
    web_sequence = web_snapshots or [web, _DEAD, _DEAD]
    seen: list[list[ServerState]] = []
    seen_web: list[ServerState] = []

    def enumerate_servers() -> list[ServerState]:
        snapshot = server_snapshots[min(len(seen), len(server_snapshots) - 1)]
        seen.append(snapshot)
        return snapshot

    def probe_web() -> ServerState:
        snapshot = web_sequence[min(len(seen_web), len(web_sequence) - 1)]
        seen_web.append(snapshot)
        return snapshot

    monkeypatch.setattr(upgrade_cmd, "enumerate_server_liveness", enumerate_servers)
    monkeypatch.setattr(upgrade_cmd, "check_web_liveness", probe_web)
    return seen


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX fail-closed diagnostics")
def test_initial_inventory_errors_render_full_dry_run_then_refuse(
    monkeypatch, tmp_path, fake_uv, force_tty
):
    calls, _configure = fake_uv
    error_a = ServerState(
        alive=True,
        pid=None,
        pid_file=None,
        probe_error="could not enumerate server-*.pid (<runtime>: denied)",
    )
    bad_path = tmp_path / "server.pid"
    error_b = ServerState(
        alive=True,
        pid=None,
        pid_file=bad_path,
        probe_error="PermissionError: denied",
    )
    web_path = tmp_path / "web.pid"
    web = ServerState(alive=True, pid=4242, pid_file=web_path, port=8080)
    seen = _patch_liveness(monkeypatch, [error_a, error_b], web=web)

    result = CliRunner().invoke(cli, ["upgrade", "--dry-run", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["inventory_complete"] is False
    assert "Cannot verify the complete memtomem process inventory" in payload["error"]
    assert "could not enumerate" in payload["error"]
    assert str(bad_path) in payload["error"]
    assert "Refusing to reinstall" in payload["error"]
    assert "flock is held" not in payload["error"]
    assert payload["would_kill"] == [4242]
    assert payload["would_install"][-1] == "memtomem"
    assert calls == []
    assert seen == [[error_a, error_b]]

    _patch_liveness(monkeypatch, [error_a, error_b], web=web)
    human = CliRunner().invoke(cli, ["upgrade", "--dry-run"])
    assert human.exit_code == 1, human.output
    assert "memtomem upgrade plan:" in human.output
    assert "Stop running web UI" in human.output
    assert "Inventory diagnostics:" in human.output
    assert "could not enumerate" in human.output
    assert str(bad_path) in human.output
    assert "Reinstall:" in human.output


@pytest.mark.parametrize("dry_run", [False, True])
def test_windows_inventory_error_warns_and_proceeds(monkeypatch, fake_uv, force_tty, dry_run):
    calls, _configure = fake_uv
    monkeypatch.setattr(upgrade_cmd.sys, "platform", "win32")
    error = ServerState(
        alive=True,
        pid=None,
        pid_file=None,
        probe_error="could not enumerate server-*.pid (<runtime>: denied)",
    )
    seen = _patch_liveness(monkeypatch, error)
    args = ["upgrade", "--json"]
    args.append("--dry-run" if dry_run else "-y")

    result = CliRunner().invoke(cli, args)
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    assert payload["warnings"]
    assert "incomplete on Windows" in payload["warnings"][0]
    assert "could not enumerate" in payload["warnings"][0]
    if dry_run:
        assert payload["would_kill"] == []
        assert payload["would_remove"] == []
        assert calls == []
    else:
        assert calls == [["uv", "tool", "install", "--refresh", "--reinstall", "memtomem"]]
    assert seen == [[error]]


def test_windows_inventory_error_is_visible_in_human_plan(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    monkeypatch.setattr(upgrade_cmd.sys, "platform", "win32")
    error = ServerState(
        alive=True,
        pid=None,
        pid_file=None,
        probe_error="could not enumerate server-*.pid (<runtime>: denied)",
    )
    _patch_liveness(monkeypatch, error)

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 0, result.output
    assert "Inventory diagnostics:" in result.output
    assert "Warning: Process inventory is incomplete on Windows" in result.output
    assert "Stop every memtomem server" in result.output
    assert len(calls) == 1


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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX stop accounting")
def test_human_refusal_keeps_partial_stop_accounting(monkeypatch, tmp_path, fake_uv, force_tty):
    calls, _configure = fake_uv
    first_path = tmp_path / "server-aaaaaaaaaaaaaaaa.pid"
    second_path = tmp_path / "server-bbbbbbbbbbbbbbbb.pid"
    first_path.write_text("111")
    second_path.write_text("222")
    states = [
        ServerState(alive=True, pid=111, pid_file=first_path),
        ServerState(alive=True, pid=222, pid_file=second_path),
    ]
    _patch_liveness(monkeypatch, states)
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: False)
    probes = iter(
        [
            ServerState(
                alive=True,
                pid=111,
                pid_file=first_path,
                probe_error="PermissionError: denied",
            ),
            _DEAD,
        ]
    )
    monkeypatch.setattr(upgrade_cmd, "probe_pid_file", lambda _path: next(probes))

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 1, result.output
    assert "cannot verify" in result.output
    assert "Stopped before failure: 111, 222" in result.output
    assert f"Removed before failure: {second_path}" in result.output
    assert calls == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: asserts every per-store server is signaled before reinstall",
)
def test_two_store_servers_stopped_before_reinstall(monkeypatch, tmp_path, fake_uv, force_tty):
    calls, _configure = fake_uv
    pid_file_a = tmp_path / "server-aaaaaaaaaaaaaaaa.pid"
    pid_file_b = tmp_path / "server-bbbbbbbbbbbbbbbb.pid"
    pid_file_a.write_text("111")
    pid_file_b.write_text("222")
    states = [
        ServerState(alive=True, pid=111, pid_file=pid_file_a),
        ServerState(alive=True, pid=222, pid_file=pid_file_b),
    ]
    _patch_liveness(monkeypatch, states)

    events: list[tuple[str, int | None]] = []

    def fake_kill(pid: int, sig: int) -> None:
        if sig == upgrade_cmd.signal.SIGTERM:
            events.append(("term", pid))

    monkeypatch.setattr(upgrade_cmd.os, "kill", fake_kill)
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: False)
    fake_run = upgrade_cmd.subprocess.run

    def tracked_run(cmd, capture_output=True, text=True, timeout=None):
        assert not pid_file_a.exists()
        assert not pid_file_b.exists()
        events.append(("install", None))
        return fake_run(cmd, capture_output=capture_output, text=text, timeout=timeout)

    monkeypatch.setattr(upgrade_cmd.subprocess, "run", tracked_run)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 0, result.output
    assert events == [("term", 111), ("term", 222), ("install", None)]
    payload = json.loads(result.stdout)
    assert payload["killed"] == [111, 222]
    assert payload["removed"] == [str(pid_file_a), str(pid_file_b)]
    assert len(calls) == 1


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: Windows intentionally reports no automatic stops",
)
def test_two_store_servers_included_in_dry_run_json(monkeypatch, tmp_path, fake_uv, force_tty):
    calls, _configure = fake_uv
    pid_file_a = tmp_path / "server-aaaaaaaaaaaaaaaa.pid"
    pid_file_b = tmp_path / "server-bbbbbbbbbbbbbbbb.pid"
    pid_file_a.write_text("111")
    pid_file_b.write_text("222")
    states = [
        ServerState(alive=True, pid=111, pid_file=pid_file_a),
        ServerState(alive=True, pid=222, pid_file=pid_file_b),
    ]
    seen = _patch_liveness(monkeypatch, states)

    result = CliRunner().invoke(cli, ["upgrade", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["would_kill"] == [111, 222]
    assert payload["would_remove"] == [str(pid_file_a), str(pid_file_b)]
    assert calls == []
    assert seen == [states]

    _patch_liveness(monkeypatch, states)
    human = CliRunner().invoke(cli, ["upgrade", "--dry-run"])
    assert human.exit_code == 0, human.output
    assert human.output.count("Stop running server") == 2
    assert str(pid_file_a) in human.output
    assert str(pid_file_b) in human.output


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: legacy shared lock aliasing is not used on Windows",
)
def test_legacy_alias_is_not_planned_for_direct_stop(monkeypatch, tmp_path, fake_uv, force_tty):
    calls, _configure = fake_uv
    runtime_pid = tmp_path / "server-aaaaaaaaaaaaaaaa.pid"
    legacy_pid = tmp_path / ".server.pid"
    runtime_pid.write_text("111")
    legacy_pid.write_text("stale")
    monkeypatch.setattr(upgrade_cmd, "legacy_server_pid_path", lambda: legacy_pid)
    states = [
        ServerState(alive=True, pid=111, pid_file=runtime_pid),
        ServerState(
            alive=True,
            pid=999,
            pid_file=legacy_pid,
            legacy_lock_mode="shared",
        ),
    ]
    _patch_liveness(monkeypatch, states)

    result = CliRunner().invoke(cli, ["upgrade", "--dry-run", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["would_kill"] == [111]
    assert payload["would_remove"] == [str(runtime_pid)]
    assert calls == []


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: legacy exclusive holders use flock and signals",
)
def test_distinct_legacy_holder_is_stopped_with_runtime_server(
    monkeypatch, tmp_path, fake_uv, force_tty
):
    calls, _configure = fake_uv
    runtime_pid = tmp_path / "server-aaaaaaaaaaaaaaaa.pid"
    legacy_pid = tmp_path / ".server.pid"
    runtime_pid.write_text("111")
    legacy_pid.write_text("999")
    monkeypatch.setattr(upgrade_cmd, "legacy_server_pid_path", lambda: legacy_pid)
    states = [
        ServerState(alive=True, pid=111, pid_file=runtime_pid),
        ServerState(
            alive=True,
            pid=999,
            pid_file=legacy_pid,
            legacy_lock_mode="exclusive",
        ),
    ]
    _patch_liveness(monkeypatch, states)
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: False)

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 0, result.output
    assert (111, upgrade_cmd.signal.SIGTERM) in sent
    assert (999, upgrade_cmd.signal.SIGTERM) in sent
    assert not runtime_pid.exists()
    assert not legacy_pid.exists()
    assert calls


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: legacy server direct-stop compatibility uses signals",
)
def test_legacy_only_server_keeps_direct_stop_behavior(monkeypatch, tmp_path, fake_uv, force_tty):
    calls, _configure = fake_uv
    legacy_pid = tmp_path / ".server.pid"
    legacy_pid.write_text("333")
    monkeypatch.setattr(upgrade_cmd, "legacy_server_pid_path", lambda: legacy_pid)
    _patch_liveness(
        monkeypatch,
        [ServerState(alive=True, pid=333, pid_file=legacy_pid)],
    )
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: False)

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 0, result.output
    assert (333, upgrade_cmd.signal.SIGTERM) in sent
    assert not legacy_pid.exists()
    assert calls


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
    # Stays alive through the grace period, then exits after SIGKILL.
    killed = {"done": False}

    def fake_kill(pid: int, sig: int) -> None:
        sent.append((pid, sig))
        if sig == upgrade_cmd.signal.SIGKILL:
            killed["done"] = True

    monkeypatch.setattr(upgrade_cmd.os, "kill", fake_kill)
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: not killed["done"])
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
    pid_file_a = tmp_path / "server-aaaaaaaaaaaaaaaa.pid"
    pid_file_b = tmp_path / "server-bbbbbbbbbbbbbbbb.pid"
    pid_file_a.write_text("12345")
    pid_file_b.write_text("23456")
    states = [
        ServerState(alive=True, pid=12345, pid_file=pid_file_a),
        ServerState(alive=True, pid=23456, pid_file=pid_file_b),
    ]
    seen = _patch_liveness(monkeypatch, states)
    monkeypatch.setattr(upgrade_cmd.sys, "platform", "win32")

    def boom(*_a, **_k):
        raise AssertionError("os.kill must not be called on Windows")

    monkeypatch.setattr(upgrade_cmd.os, "kill", boom)

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 0, result.output
    assert "Detected Windows" in result.output
    assert calls  # uv still ran
    # We also leave the pid files alone — Windows users may need them.
    assert pid_file_a.exists()
    assert pid_file_b.exists()
    assert seen == [states]


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
    """An auto-respawned old generation is recycled once after reinstall."""
    calls, _configure = fake_uv
    pid_file = tmp_path / "server.pid"
    pid_file.write_text("12345")
    respawned = ServerState(alive=True, pid=99999, pid_file=pid_file)
    replacement = ServerState(alive=True, pid=100000, pid_file=pid_file)
    _patch_liveness(
        monkeypatch,
        ServerState(alive=True, pid=12345, pid_file=pid_file),
        snapshots=[
            [ServerState(alive=True, pid=12345, pid_file=pid_file)],
            [respawned],
            [respawned],
            [replacement],
        ],
    )
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: False)

    probes = iter([respawned, replacement])
    monkeypatch.setattr(
        upgrade_cmd,
        "probe_pid_file",
        lambda _p: next(probes),
    )

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--grace", "0.1", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["killed"] == [12345, 99999]
    assert (12345, upgrade_cmd.signal.SIGTERM) in sent
    assert (99999, upgrade_cmd.signal.SIGTERM) in sent
    assert all(pid != 100000 for pid, _sig in sent)
    assert pid_file.exists()
    assert "freshly started writer" in result.stderr
    assert calls == [["uv", "tool", "install", "--refresh", "--reinstall", "memtomem"]]


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: Windows skips the post-stop clean-server boundary",
)
def test_boundary_enumeration_error_refuses_after_completed_stops(
    monkeypatch, tmp_path, fake_uv, force_tty
):
    calls, _configure = fake_uv
    pid_file = tmp_path / "server-aaaaaaaaaaaaaaaa.pid"
    pid_file.write_text("12345")
    error = ServerState(
        alive=True,
        pid=None,
        pid_file=None,
        probe_error="could not enumerate server-*.pid (<runtime>: denied)",
    )
    _patch_liveness(
        monkeypatch,
        ServerState(alive=True, pid=12345, pid_file=pid_file),
        post=[error],
    )
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: False)

    def unexpected_db_probe():
        raise AssertionError("DB probe must not run after boundary enumeration failure")

    monkeypatch.setattr(upgrade_cmd, "_resolve_db_path", unexpected_db_probe)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["killed"] == [12345]
    assert payload["removed"] == [str(pid_file)]
    assert "Cannot verify the complete memtomem process inventory" in payload["error"]
    assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX inventory stabilization")
def test_lock_before_pid_startup_windows_are_retried(monkeypatch, tmp_path, fake_uv, force_tty):
    """Shared-legacy-only and empty runtime-payload windows must settle."""
    calls, _configure = fake_uv
    runtime_pid = tmp_path / "server-aaaaaaaaaaaaaaaa.pid"
    legacy_pid = tmp_path / ".server.pid"
    runtime_pid.write_text("")
    legacy_pid.write_text("")
    monkeypatch.setattr(upgrade_cmd, "legacy_server_pid_path", lambda: legacy_pid)

    shared_alias = ServerState(
        alive=True,
        pid=None,
        pid_file=legacy_pid,
        legacy_lock_mode="shared",
    )
    empty_runtime = ServerState(alive=True, pid=None, pid_file=runtime_pid)
    ready_runtime = ServerState(alive=True, pid=321, pid_file=runtime_pid)
    seen = _patch_liveness(
        monkeypatch,
        [shared_alias],
        snapshots=[
            [shared_alias],
            [empty_runtime, shared_alias],
            [ready_runtime, shared_alias],
            [],
            [],
        ],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(upgrade_cmd.time, "sleep", sleeps.append)
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(upgrade_cmd, "probe_pid_file", lambda _path: _DEAD)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["killed"] == [321]
    assert sleeps == [0.05, 0.1]
    assert seen == [
        [shared_alias],
        [empty_runtime, shared_alias],
        [ready_runtime, shared_alias],
        [],
        [],
    ]
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX inventory stabilization")
def test_unsignalable_startup_window_fails_after_bounded_retries(
    monkeypatch, tmp_path, fake_uv, force_tty
):
    calls, _configure = fake_uv
    pid_file = tmp_path / "server.pid"
    transient = ServerState(alive=True, pid=None, pid_file=pid_file)
    seen = _patch_liveness(monkeypatch, transient, snapshots=[[transient]])
    sleeps: list[float] = []
    monkeypatch.setattr(upgrade_cmd.time, "sleep", sleeps.append)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 1, result.output
    assert "no signalable PID" in json.loads(result.stdout)["error"]
    assert sleeps == [0.05, 0.1]
    assert seen == [[transient], [transient], [transient]]
    assert calls == []


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX boundary stabilization")
def test_preinstall_boundary_retries_empty_pid_payload(monkeypatch, tmp_path, fake_uv, force_tty):
    calls, _configure = fake_uv
    pid_file = tmp_path / "server.pid"
    transient = ServerState(alive=True, pid=None, pid_file=pid_file)
    ready = ServerState(alive=True, pid=777, pid_file=pid_file)
    seen = _patch_liveness(
        monkeypatch,
        _DEAD,
        snapshots=[[], [transient], [ready], [ready]],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(upgrade_cmd.time, "sleep", sleeps.append)
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(upgrade_cmd, "probe_pid_file", lambda _path: _DEAD)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["killed"] == [777]
    assert sleeps == [0.05]
    assert seen == [[], [transient], [ready], [ready]]
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX post-install stabilization")
def test_postinstall_snapshot_retries_before_accepting_new_generation(
    monkeypatch, tmp_path, fake_uv, force_tty
):
    calls, _configure = fake_uv
    cutoff = upgrade_cmd.datetime.fromisoformat("2026-08-05T02:00:00+00:00")
    monkeypatch.setattr(upgrade_cmd, "_utc_now", lambda: cutoff)
    pid_file = tmp_path / "server.pid"
    transient = ServerState(alive=True, pid=None, pid_file=pid_file)
    ready = ServerState(
        alive=True,
        pid=777,
        pid_file=pid_file,
        started="2026-08-05T02:00:00.001000+00:00",
    )
    seen = _patch_liveness(
        monkeypatch,
        _DEAD,
        snapshots=[[], [], [transient], [ready]],
    )
    sleeps: list[float] = []
    monkeypatch.setattr(upgrade_cmd.time, "sleep", sleeps.append)

    def unexpected_kill(*_args):
        raise AssertionError("the stabilized new generation must stay running")

    monkeypatch.setattr(upgrade_cmd.os, "kill", unexpected_kill)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["killed"] == []
    assert sleeps == [0.05]
    assert seen == [[], [], [transient], [ready]]
    assert len(calls) == 1


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only: Windows skips the post-stop clean-server boundary",
)
def test_server_starting_during_reinstall_is_recycled(monkeypatch, tmp_path, fake_uv, force_tty):
    calls, _configure = fake_uv
    new_pid_file = tmp_path / "server-bbbbbbbbbbbbbbbb.pid"
    new_server = ServerState(alive=True, pid=777, pid_file=new_pid_file)
    replacement = ServerState(alive=True, pid=888, pid_file=new_pid_file)
    seen = _patch_liveness(
        monkeypatch,
        _DEAD,
        snapshots=[[], [], [new_server], [replacement]],
    )
    events: list[tuple[str, int | None]] = []
    fake_run = upgrade_cmd.subprocess.run

    def tracked_run(cmd, capture_output=True, text=True, timeout=None):
        events.append(("install", None))
        return fake_run(cmd, capture_output=capture_output, text=text, timeout=timeout)

    monkeypatch.setattr(upgrade_cmd.subprocess, "run", tracked_run)
    monkeypatch.setattr(
        upgrade_cmd.os,
        "kill",
        lambda pid, sig: (
            events.append(("term", pid)) if sig == upgrade_cmd.signal.SIGTERM else None
        ),
    )
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(upgrade_cmd, "probe_pid_file", lambda _p: replacement)

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 0, result.output
    assert events == [("install", None), ("term", 777)]
    assert len(calls) == 1
    # Initial, pre-install, and post-install are the only unconditional
    # complete inventories; the replacement uses the targeted path re-probe.
    assert seen == [[], [], [new_server]]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX generation timestamps")
def test_processes_started_after_install_are_not_recycled(
    monkeypatch, tmp_path, fake_uv, force_tty
):
    calls, _configure = fake_uv
    cutoff = upgrade_cmd.datetime.fromisoformat("2026-08-05T02:00:00+00:00")
    monkeypatch.setattr(upgrade_cmd, "_utc_now", lambda: cutoff)
    server = ServerState(
        alive=True,
        pid=777,
        pid_file=tmp_path / "server.pid",
        started="2026-08-05T02:00:00.001000+00:00",
    )
    web = ServerState(
        alive=True,
        pid=888,
        pid_file=tmp_path / "web.pid",
        port=8080,
        started="2026-08-05T02:00:00.002000+00:00",
    )
    seen = _patch_liveness(
        monkeypatch,
        _DEAD,
        snapshots=[[], [], [server]],
        web_snapshots=[_DEAD, _DEAD, web],
    )

    def unexpected_kill(*_args):
        raise AssertionError("a verified new-generation process must remain running")

    monkeypatch.setattr(upgrade_cmd.os, "kill", unexpected_kill)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["killed"] == []
    assert seen == [[], [], [server]]
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX generation verification")
def test_retirement_pid_still_present_reports_partial_failure(
    monkeypatch, tmp_path, fake_uv, force_tty
):
    calls, _configure = fake_uv
    pid_file = tmp_path / "server.pid"
    retirement = ServerState(alive=True, pid=777, pid_file=pid_file)
    _patch_liveness(
        monkeypatch,
        _DEAD,
        snapshots=[[], [], [retirement], [retirement]],
    )
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(upgrade_cmd, "probe_pid_file", lambda _p: retirement)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["ok"] is False
    assert payload["reinstalled"] == "memtomem"
    assert payload["cleanup_complete"] is False
    assert "retirement pid 777 still holds" in payload["error"]
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX generation verification")
def test_reused_pid_with_new_start_stamp_is_not_misclassified(
    monkeypatch, tmp_path, fake_uv, force_tty
):
    calls, _configure = fake_uv
    cutoff = upgrade_cmd.datetime.fromisoformat("2026-08-05T02:00:00+00:00")
    monkeypatch.setattr(upgrade_cmd, "_utc_now", lambda: cutoff)
    pid_file = tmp_path / "server.pid"
    old = ServerState(
        alive=True,
        pid=777,
        pid_file=pid_file,
        started="2026-08-05T01:59:00+00:00",
    )
    replacement = ServerState(
        alive=True,
        pid=777,
        pid_file=pid_file,
        started="2026-08-05T02:00:01+00:00",
    )
    _patch_liveness(monkeypatch, _DEAD, snapshots=[[], [], [old]])
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda _pid, _sig: None)
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: False)
    monkeypatch.setattr(upgrade_cmd, "probe_pid_file", lambda _path: replacement)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["killed"] == [777]
    assert len(calls) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX generation verification")
def test_post_install_inventory_error_reports_partial_failure(monkeypatch, fake_uv, force_tty):
    calls, _configure = fake_uv
    error = ServerState(
        alive=True,
        pid=None,
        pid_file=None,
        probe_error="could not enumerate server-*.pid (<runtime>: denied)",
    )
    _patch_liveness(monkeypatch, _DEAD, snapshots=[[], [], [error], [error]])

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["reinstalled"] == "memtomem"
    assert payload["cleanup_complete"] is False
    assert "could not enumerate" in payload["error"]
    assert len(calls) == 1


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


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Asserts the POSIX would_kill payload; Windows skips the kill stage so the arrays are empty (covered by test_windows_dry_run_json_reports_no_kills)",
)
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


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX Web UI generation recycle")
def test_web_auto_respawn_is_recycled_after_install(monkeypatch, tmp_path, fake_uv, force_tty):
    calls, _configure = fake_uv
    web_pid_file = tmp_path / "web.pid"
    web_pid_file.write_text("4242\n8080\n2026-07-03T00:00:00+00:00\n")
    web_info_file = tmp_path / "web.json"
    web_info_file.write_text('{"pid": 6000, "port": 8080}')
    initial = ServerState(alive=True, pid=4242, pid_file=web_pid_file, port=8080)
    respawned = ServerState(alive=True, pid=5000, pid_file=web_pid_file, port=8080)
    replacement = ServerState(alive=True, pid=6000, pid_file=web_pid_file, port=8080)
    _patch_liveness(
        monkeypatch,
        _DEAD,
        web=initial,
        web_snapshots=[initial, respawned, respawned, replacement],
    )
    probes = iter([respawned, replacement])
    monkeypatch.setattr(upgrade_cmd, "probe_pid_file", lambda _p: next(probes))
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr(upgrade_cmd.os, "kill", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(upgrade_cmd, "_pid_alive", lambda _pid: False)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["killed"] == [4242, 5000]
    assert (4242, upgrade_cmd.signal.SIGTERM) in sent
    assert (5000, upgrade_cmd.signal.SIGTERM) in sent
    assert all(pid != 6000 for pid, _sig in sent)
    assert web_pid_file.exists()
    assert web_info_file.exists()
    assert len(calls) == 1


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

    # Decline confirmation by feeding "n" to the prompt.
    result = CliRunner().invoke(cli, ["upgrade", "--json"], input="n\n")
    assert result.exit_code == 0, result.output
    # #1640: stdout must be a single JSON document — the prompt rides
    # stderr under --json, so `mm upgrade --json | jq` works on cancel.
    payload = json.loads(result.stdout)
    assert payload == {"ok": True, "cancelled": True}
    assert "Proceed with upgrade?" in result.stderr
    assert calls == []


def test_cancel_json_win_prompt_branch(monkeypatch, fake_uv, force_tty):
    """#1640: forcing click's WIN prompt branch must not pollute the JSON
    ack — _prompts.confirm never enters click's prompt machinery."""
    import click.termui

    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, ServerState(alive=False, pid=None, pid_file=None))
    monkeypatch.setattr(click.termui, "WIN", True)

    result = CliRunner().invoke(cli, ["upgrade", "--json"], input="n\n")
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == {"ok": True, "cancelled": True}
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


# ------------------------------------------- post-stop DB probe (#1606)


def _patch_db_probe(monkeypatch, tmp_path, *, locked: bool):
    """Route the probe at a fake DB path with a scripted lock state."""
    from memtomem.cli._db_lock import DbLockState

    db_path = tmp_path / "memory.db"
    monkeypatch.setattr(upgrade_cmd, "_resolve_db_path", lambda: db_path, raising=False)
    monkeypatch.setattr(
        upgrade_cmd,
        "check_db_lock",
        lambda _p: DbLockState(locked=locked, probe_error=None),
        raising=False,
    )
    return db_path


def test_db_lock_warns_but_proceeds(monkeypatch, tmp_path, fake_uv, force_tty):
    """An unknown writer (no pid file) must produce a warning, not a refusal."""
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, _DEAD)
    _patch_db_probe(monkeypatch, tmp_path, locked=True)

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 0, result.output
    assert "write lock" in result.output
    assert "previous version" in result.output
    assert "lsof" in result.output
    assert calls  # reinstall proceeded despite the lock


def test_db_lock_warning_in_json_output(monkeypatch, tmp_path, fake_uv, force_tty):
    _calls, _configure = fake_uv
    _patch_liveness(monkeypatch, _DEAD)
    _patch_db_probe(monkeypatch, tmp_path, locked=True)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["db_lock_warning"] is True


def test_windows_live_server_does_not_suppress_db_lock_warning(
    monkeypatch, tmp_path, fake_uv, force_tty
):
    """Windows leaves known old processes up, so a DB lock still warns."""
    _calls, _configure = fake_uv
    monkeypatch.setattr(upgrade_cmd.sys, "platform", "win32")
    pid_file = tmp_path / "server.pid"
    state = ServerState(alive=True, pid=12345, pid_file=pid_file)
    seen = _patch_liveness(monkeypatch, state)
    _patch_db_probe(monkeypatch, tmp_path, locked=True)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["db_lock_warning"] is True
    assert seen == [[state]]


def test_no_db_lock_no_warning(monkeypatch, tmp_path, fake_uv, force_tty):
    _calls, _configure = fake_uv
    _patch_liveness(monkeypatch, _DEAD)
    _patch_db_probe(monkeypatch, tmp_path, locked=False)

    result = CliRunner().invoke(cli, ["upgrade", "-y", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output.strip().splitlines()[-1])
    assert payload["ok"] is True
    assert payload["db_lock_warning"] is False

    human = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert human.exit_code == 0, human.output
    assert "write lock" not in human.output


def test_config_failure_skips_probe(monkeypatch, fake_uv, force_tty):
    """A broken config must not block the upgrade — the probe is skipped."""
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, _DEAD)
    monkeypatch.setattr(upgrade_cmd, "_resolve_db_path", lambda: None, raising=False)

    def boom(_p):
        raise AssertionError("check_db_lock must not run without a resolved db path")

    monkeypatch.setattr(upgrade_cmd, "check_db_lock", boom, raising=False)

    result = CliRunner().invoke(cli, ["upgrade", "-y"])
    assert result.exit_code == 0, result.output
    assert "write lock" not in result.output
    assert calls


def test_dry_run_skips_db_probe(monkeypatch, tmp_path, fake_uv, force_tty):
    """Dry-run never stops processes, so the post-stop probe must not run."""
    calls, _configure = fake_uv
    _patch_liveness(monkeypatch, _DEAD)

    def boom():
        raise AssertionError("_resolve_db_path must not run on --dry-run")

    monkeypatch.setattr(upgrade_cmd, "_resolve_db_path", boom, raising=False)

    result = CliRunner().invoke(cli, ["upgrade", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert calls == []
