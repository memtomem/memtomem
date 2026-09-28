"""``--json`` commands answer a storage failure in JSON (#2589).

#2575 gave ``mm status --json`` the ``{"error": ...}`` envelope for a
classified failure; every other JSON-mode command that opens storage printed
a plain Click error (empty stdout) or, with no wrapper at all, a traceback.
Each row drives the real ``cli_components`` with ``create_components``
failing the way a refused storage open does, and pins the envelope shape the
command's success payload calls for (CONTRIBUTING "JSON error shape"): the
write acks with an ``ok`` flag answer ``{"ok": false, "reason": ...}``,
everything else ``{"error": ...}``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.errors import StorageStartupError

from .helpers import set_home

_LOCKED = StorageStartupError(reason_code="storage_locked", stage="open")

# (argv, envelope key set). ``{index-path}`` is replaced per test.
_COMMANDS: list[tuple[list[str], set[str]]] = [
    (["search", "q", "--format", "json"], {"error"}),
    (["recall", "--format", "json"], {"error"}),
    (["add", "a note", "--json"], {"ok", "reason"}),
    (["index", "--flush", "--json"], {"error"}),
    (["index", "{index-path}", "--debounce-window", "0", "--json"], {"error"}),
    (["mem", "rescan", "--scope", "user", "--json"], {"error"}),
    (["watchdog", "status", "--json"], {"error"}),
    (["watchdog", "run", "--json"], {"error"}),
    (["session", "start", "--json"], {"error"}),
    (["session", "list", "--json"], {"error"}),
    (["session", "events", "some-session", "--json"], {"error"}),
    (["agent", "search", "q", "--format", "json"], {"error"}),
    (["agent", "list", "--json"], {"error"}),
    (["quality", "cases", "--format", "json"], {"error"}),
    (["quality", "replay", "--format", "json"], {"error"}),
    (["pinned", "list", "--json"], {"error"}),
    # No ``--json`` flag: JSON is the command's only output (#2596).
    (["pinned", "compose", "q"], {"error"}),
    (["schedule", "list", "--json"], {"error"}),
    (["purge", "--matching-excluded", "--json"], {"ok", "reason"}),
]

# The commands that had no error wrapper before #2589 (the first five) or
# #2596 (the rest): a storage failure escaped as a raw traceback even in
# text mode.
_UNWRAPPED_TEXT: list[list[str]] = [
    ["index", "--flush"],
    ["agent", "list"],
    ["pinned", "list"],
    ["schedule", "list"],
    ["purge", "--matching-excluded"],
    ["pinned", "get", "b1"],
    ["pinned", "set", "b1", "--content", "x"],
    ["pinned", "delete", "b1"],
    ["schedule", "add", "--cron", "0 * * * *", "--job", "compaction"],
    ["schedule", "run-now", "s1"],
    ["schedule", "delete", "s1"],
    ["agent", "migrate"],
    ["agent", "register", "planner"],
    ["agent", "share", "00000000-0000-0000-0000-000000000001"],
]


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def opens(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[int]:
    """Make storage open fail with ``_LOCKED``; returns the open-attempt log.

    The debounce queue is pointed at ``tmp_path``: ``--debounce-window``
    enqueues before it opens storage, and the default queue lives in the
    real home directory.
    """
    # HOME too: session tracing loads its own config from the home directory.
    set_home(monkeypatch, tmp_path / "home")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("memtomem.cli._bootstrap._CONFIG_PATH", config_path)
    monkeypatch.setenv("MEMTOMEM_INDEX_DEBOUNCE_QUEUE", str(tmp_path / "queue.json"))
    attempts: list[int] = []

    async def fail(*args: object, **kwargs: object) -> None:
        attempts.append(1)
        raise _LOCKED

    monkeypatch.setattr("memtomem.server.component_factory.create_components", fail)
    return attempts


def _argv(argv: list[str], tmp_path: Path) -> list[str]:
    note = tmp_path / "note.md"
    note.write_text("x", encoding="utf-8")
    return [str(note) if a == "{index-path}" else a for a in argv]


@pytest.mark.parametrize(
    ("argv", "keys"), _COMMANDS, ids=lambda v: " ".join(v) if isinstance(v, list) else ""
)
def test_storage_open_failure_is_a_json_envelope(
    runner: CliRunner, opens: list[int], tmp_path: Path, argv: list[str], keys: set[str]
) -> None:
    result = runner.invoke(cli, _argv(argv, tmp_path))

    assert opens, "the command never reached storage open"
    assert result.exit_code == 1, result.output
    # ``json.loads`` tolerates leading whitespace; a stray line would not be.
    assert result.stdout.startswith("{"), result.stdout
    data = json.loads(result.stdout)
    assert set(data) == keys
    message = data["error"] if keys == {"error"} else data["reason"]
    if keys == {"ok", "reason"}:
        assert data["ok"] is False
    assert message.startswith(str(_LOCKED))
    assert "another process is writing" in message
    assert result.stderr == ""


@pytest.mark.parametrize("argv", _UNWRAPPED_TEXT, ids=" ".join)
def test_text_mode_prints_a_click_error_not_a_traceback(
    runner: CliRunner, opens: list[int], argv: list[str]
) -> None:
    result = runner.invoke(cli, argv)

    assert opens
    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit), result.exception
    assert result.stdout == ""
    assert result.stderr.startswith(f"Error: {_LOCKED}")


def test_session_end_json_answers_a_storage_failure_with_the_ok_envelope(
    runner: CliRunner, opens: list[int], monkeypatch: pytest.MonkeyPatch
) -> None:
    """``session end --json`` is a write ack with an ``ok`` flag (#2596), so a
    refused storage open answers ``{"ok": false, "reason": ...}``. It needs an
    active session to reach storage at all, hence its own test."""
    monkeypatch.setattr("memtomem.cli.session_cmd._read_current_session", lambda: "sess-1")

    result = runner.invoke(cli, ["session", "end", "--json"])

    assert opens
    assert result.exit_code == 1, result.output
    assert result.stdout.startswith("{"), result.stdout
    data = json.loads(result.stdout)
    assert set(data) == {"ok", "reason"}
    assert data["ok"] is False
    assert data["reason"].startswith(str(_LOCKED))
    assert result.stderr == ""


def test_an_unclassified_failure_stays_plain_in_json_mode(
    runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    set_home(monkeypatch, tmp_path / "home")
    config_path = tmp_path / "config.json"
    config_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr("memtomem.cli._bootstrap._CONFIG_PATH", config_path)

    async def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("memtomem.server.component_factory.create_components", fail)

    result = runner.invoke(cli, ["schedule", "list", "--json"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "boom" in result.stderr
