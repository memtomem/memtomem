"""``mm daemon`` without a daemon: listing failures, names, platforms (#3381).

The commands against a real daemon are in ``test_server_daemon.py``.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

import memtomem.cli._liveness as liveness
import memtomem.cli.daemon_cmd as daemon_cmd
from memtomem.cli import cli


@pytest.fixture
def unlistable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        liveness,
        "enumerate_daemons",
        lambda: liveness.DaemonInventory([], error="could not enumerate server-*.pid (denied)"),
    )


def test_status_fails_closed_when_daemons_cannot_be_listed(unlistable: None) -> None:
    result = CliRunner().invoke(cli, ["daemon", "status"])
    assert result.exit_code == 1
    assert "Cannot list shared daemons: could not enumerate server-*.pid (denied)" in result.output


def test_status_json_failure_is_one_error_document(unlistable: None) -> None:
    result = CliRunner().invoke(cli, ["daemon", "status", "--json"])
    assert result.exit_code == 1
    assert json.loads(result.stdout) == {
        "error": "Cannot list shared daemons: could not enumerate server-*.pid (denied). "
        "No daemon was signalled or reported."
    }


@pytest.mark.skipif(daemon_cmd.os.name == "nt", reason="stop is a no-op on Windows")
def test_stop_signals_nothing_when_daemons_cannot_be_listed(
    unlistable: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[tuple[int, int]] = []
    monkeypatch.setattr("memtomem.cli.upgrade_cmd.os.kill", lambda *a: sent.append(a))
    result = CliRunner().invoke(cli, ["daemon", "stop"])
    assert result.exit_code == 1
    assert sent == []


def test_status_with_no_daemon_exits_3(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(liveness, "enumerate_daemons", lambda: liveness.DaemonInventory([]))
    result = CliRunner().invoke(cli, ["daemon", "status", "--json"])
    assert result.exit_code == 3
    assert json.loads(result.stdout) == {"daemons": [], "count": 0}


@pytest.mark.parametrize("command", ["status", "stop"])
def test_an_invalid_name_is_a_usage_error(command: str) -> None:
    result = CliRunner().invoke(cli, ["daemon", command, "Bad_Name"])
    assert result.exit_code == 2
    assert "Invalid value for NAME" in result.output


def test_stop_on_windows_says_there_is_nothing_to_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(daemon_cmd.os, "name", "nt")
    result = CliRunner().invoke(cli, ["daemon", "stop"])
    assert result.exit_code == 0
    assert result.output == "shared daemons run on POSIX only; nothing to stop\n"


def test_a_lifetime_lock_with_an_unknown_name_is_reported_not_guessed(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("_MEMTOMEM_TEST_RUNTIME_DIR", str(tmp_path))
    odd = tmp_path / "server-daemon-Not_A_Name.pid"
    odd.write_text("999999\n")
    result = CliRunner().invoke(cli, ["daemon", "status", "--json"])
    assert result.exit_code == 3
    payload = json.loads(result.stdout)
    assert payload["daemons"] == []
    assert payload["warnings"] == [f"{odd}: not a daemon name this version knows"]


def test_doctor_shows_a_daemon_listing_warning_in_its_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    monkeypatch.setenv("_MEMTOMEM_TEST_RUNTIME_DIR", str(tmp_path))
    odd = tmp_path / "server-daemon-Not_A_Name.pid"
    odd.write_text("999999\n")
    result = CliRunner().invoke(cli, ["doctor"])
    assert f"{odd}: not a daemon name this version knows" in result.output
