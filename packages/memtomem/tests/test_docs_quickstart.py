"""Runnable contract for the public documentation's first-success path."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli


def test_documented_quickstart_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A fresh user can save and find one memory without existing notes."""
    for name in tuple(os.environ):
        if name.startswith("MEMTOMEM_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / ".local" / "share"))

    runner = CliRunner()

    init = runner.invoke(
        cli,
        ["init", "--non-interactive", "--preset", "minimal", "--mcp", "skip"],
    )
    assert init.exit_code == 0, init.output

    empty_status = runner.invoke(cli, ["status", "--json"])
    assert empty_status.exit_code == 0, empty_status.output
    assert json.loads(empty_status.output)["index"]["total_chunks"] == 0

    sentence = "Deployment checklist uses blue-green rollout"
    add = runner.invoke(cli, ["add", sentence, "--tags", "ops", "--json"])
    assert add.exit_code == 0, add.output
    add_payload = json.loads(add.output)
    assert add_payload["ok"] is True
    assert add_payload["chunks"] == 1

    search = runner.invoke(cli, ["search", "blue-green", "--format", "plain"])
    assert search.exit_code == 0, search.output
    assert sentence in search.output

    populated_status = runner.invoke(cli, ["status", "--json"])
    assert populated_status.exit_code == 0, populated_status.output
    assert json.loads(populated_status.output)["index"]["total_chunks"] >= 1
