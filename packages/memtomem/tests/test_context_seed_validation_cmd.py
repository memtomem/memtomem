"""Tests for ``mm context seed-validation`` — the shipped ADR-0026 §Validation seeder.

The seeder logic itself (six distinct diff states) is pinned against the real
diff engine in ``test_ctx_validation_harness``. These tests own the *CLI
surface* that exposes it from the installed wheel: wiring to the seeder, the
overwrite guard, the ``--json`` manifest, and that it stays a hidden helper.

That CLI surface is the whole point of the command existing — a naive async
user-test participant who only ``pip install``-ed memtomem cannot import
``tests/fixtures``; the command is the supported way to reproduce the first-run
state.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli

from .helpers import set_home


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> CliRunner:
    # Isolate HOME so nothing the CLI group touches reaches the real user home.
    set_home(monkeypatch, tmp_path / "home")
    return CliRunner()


def test_seed_validation_writes_store_and_runtime(runner: CliRunner, tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    result = runner.invoke(cli, ["context", "seed-validation", str(demo)])

    assert result.exit_code == 0, result.output
    # Store side (.memtomem/) and runtime side (.claude/) both seeded.
    assert (demo / ".memtomem").is_dir()
    assert (demo / ".claude").is_dir()
    # The dir becomes a project root so `mm web` from here shows the seed.
    assert (demo / "pyproject.toml").is_file()
    # Human output names the seeded artifacts + the next-step launch.
    assert "Seeded ADR-0026" in result.output
    assert "code-review" in result.output
    assert "mm web" in result.output


def test_seed_validation_creates_missing_directory(runner: CliRunner, tmp_path: Path) -> None:
    demo = tmp_path / "nested" / "demo"  # parent does not exist yet
    result = runner.invoke(cli, ["context", "seed-validation", str(demo)])

    assert result.exit_code == 0, result.output
    assert (demo / ".memtomem").is_dir()


def test_seed_validation_json_manifest(runner: CliRunner, tmp_path: Path) -> None:
    demo = tmp_path / "demo"
    result = runner.invoke(cli, ["context", "seed-validation", "--json", str(demo)])

    assert result.exit_code == 0, result.output
    manifest = json.loads(result.output)
    # All six §Validation affordances present, and project_root points at DIRECTORY.
    assert set(manifest["states"]) == {
        "out_of_sync",
        "not_yet_imported",
        "empty_type",
        "mcp_orphan",
        "parse_error",
        "in_sync",
    }
    assert Path(manifest["project_root"]) == demo


@pytest.mark.parametrize(
    "make_existing",
    [
        pytest.param(lambda d: (d / ".memtomem").mkdir(), id="memtomem-store"),
        pytest.param(lambda d: (d / ".mcp.json").write_text("{}\n"), id="mcp-json-only"),
        pytest.param(lambda d: (d / ".claude" / "skills").mkdir(parents=True), id="claude-only"),
        pytest.param(lambda d: (d / "README.md").write_text("real repo\n"), id="unrelated-file"),
    ],
)
def test_seed_validation_refuses_nonempty_dir(
    runner: CliRunner, tmp_path: Path, make_existing
) -> None:
    """Never overwrite a real project: ANY non-empty target is refused without --force.

    The seeder writes ``.memtomem/`` *and* runtime trees (``.claude/`` …),
    ``.mcp.json`` and ``pyproject.toml``; a guard that only checked ``.memtomem/``
    would silently clobber a repo that has ``.claude/`` or ``.mcp.json`` but has
    not adopted memtomem yet.
    """
    demo = tmp_path / "real-project"
    demo.mkdir()
    make_existing(demo)

    result = runner.invoke(cli, ["context", "seed-validation", str(demo)])
    assert result.exit_code != 0
    assert "is not empty" in result.output

    # --force overrides the guard (idempotent re-seed in place).
    forced = runner.invoke(cli, ["context", "seed-validation", "--force", str(demo)])
    assert forced.exit_code == 0, forced.output
    assert (demo / ".claude").is_dir()


def test_seed_validation_seeds_into_existing_empty_dir(runner: CliRunner, tmp_path: Path) -> None:
    demo = tmp_path / "empty"
    demo.mkdir()  # exists but empty — proceeds without --force
    result = runner.invoke(cli, ["context", "seed-validation", str(demo)])
    assert result.exit_code == 0, result.output
    assert (demo / ".memtomem").is_dir()


def test_seed_validation_is_hidden(runner: CliRunner) -> None:
    # Hidden QA helper — must not surface in the user-facing group help.
    result = runner.invoke(cli, ["context", "--help"])
    assert result.exit_code == 0, result.output
    assert "seed-validation" not in result.output
