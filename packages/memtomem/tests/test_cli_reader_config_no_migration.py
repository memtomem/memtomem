"""Read-only CLI configuration loads must not migrate legacy files (#2419)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli

from .helpers import set_home


@pytest.fixture(params=[False, True], ids=["no-indexing", "explicit-auto-discover"])
def legacy_config(request, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_home(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path)
    data = {"embedding": {"provider": "none"}}
    if request.param:
        data["indexing"] = {"auto_discover": True, "memory_dirs": ["~/notes"]}
        root = tmp_path / "notes"
    else:
        root = tmp_path / ".memtomem" / "memories"
    note = root / "_imported" / "safe.md"
    note.parent.mkdir(parents=True)
    note.write_text("Plain prose.", encoding="utf-8")
    config = tmp_path / ".memtomem" / "config.json"
    config.parent.mkdir(exist_ok=True)
    config.write_text(json.dumps(data), encoding="utf-8")
    return config


@pytest.mark.parametrize(
    "args",
    [
        ["mem", "rescan-files"],
        ["mem", "rescan-files", "--json"],
        ["embedding-reset"],
        ["embedding-reset", "--mode", "status"],
    ],
)
def test_readers_leave_legacy_config_untouched(legacy_config: Path, args: list[str]) -> None:
    before = legacy_config.read_bytes()
    mtime = legacy_config.stat().st_mtime_ns

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert legacy_config.read_bytes() == before
    assert legacy_config.stat().st_mtime_ns == mtime
    assert not legacy_config.with_name(".config.json.lock").exists()
    if args[0] == "embedding-reset":
        assert "Embedding Status" in result.output
        assert "No mismatch" in result.output
        # This fix protects configuration; storage initialization is retained.
        assert legacy_config.with_name("memtomem.db").exists()
    elif "--json" in args:
        assert json.loads(result.output) == {"scanned": 1, "violations": [], "errors": []}
    else:
        assert "1 file(s) scanned" in result.output


@pytest.mark.parametrize("mode", ["apply-current", "revert-to-stored"])
def test_recovery_modes_still_migrate_legacy_config(legacy_config: Path, mode: str) -> None:
    args = ["embedding-reset", "--mode", mode]
    if mode == "apply-current":
        args.append("--yes")

    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 0, result.output
    assert (
        json.loads(legacy_config.read_text(encoding="utf-8"))["indexing"]["auto_discover"] is False
    )
    assert legacy_config.with_name(".config.json.lock").exists()
    assert ("DB reset to" if mode == "apply-current" else "nothing to revert") in result.output
