"""Backward-compatible enumeration / extraction across command canonical
layouts. Mirrors :mod:`tests.test_context_agents_bc_layout` for slash
commands (``commands/<name>/command.md`` directory layout).
"""

from __future__ import annotations

import logging
from pathlib import Path

from memtomem.context.commands import (
    CANONICAL_COMMAND_ROOT,
    canonical_command_name,
    extract_commands_to_canonical,
    list_canonical_commands,
    parse_canonical_command,
)


SAMPLE_COMMAND = """---
description: Simple prompt
---

run $ARGUMENTS
"""


def _write_flat_command(project_root: Path, name: str) -> Path:
    root = project_root / CANONICAL_COMMAND_ROOT
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{name}.md"
    target.write_text(SAMPLE_COMMAND)
    return target


def _write_dir_command(project_root: Path, name: str) -> Path:
    root = project_root / CANONICAL_COMMAND_ROOT / name
    root.mkdir(parents=True, exist_ok=True)
    target = root / "command.md"
    target.write_text(SAMPLE_COMMAND)
    return target


# ── list_canonical_commands enumeration ─────────────────────────────────


def test_list_commands_flat_only_unchanged(tmp_path: Path) -> None:
    _write_flat_command(tmp_path, "alpha")
    _write_flat_command(tmp_path, "beta")
    result = list_canonical_commands(tmp_path)
    names = [p.stem for p, _ in result]
    layouts = [layout for _, layout in result]
    assert names == ["alpha", "beta"]
    assert layouts == ["flat", "flat"]


def test_list_commands_dir_only_enumerated(tmp_path: Path) -> None:
    _write_dir_command(tmp_path, "gamma")
    _write_dir_command(tmp_path, "delta")
    result = list_canonical_commands(tmp_path)
    names = [p.parent.name for p, _ in result]
    layouts = [layout for _, layout in result]
    assert names == ["delta", "gamma"]
    assert layouts == ["dir", "dir"]


def test_list_commands_both_layouts_dir_wins(
    tmp_path: Path,
    caplog: "logging.LogCaptureFixture",
) -> None:
    _write_flat_command(tmp_path, "shared")
    _write_dir_command(tmp_path, "shared")
    with caplog.at_level(logging.WARNING):
        result = list_canonical_commands(tmp_path)
    assert len(result) == 1
    path, layout = result[0]
    assert layout == "dir"
    assert path.name == "command.md"
    assert any(
        "both flat" in rec.message and rec.levelno == logging.WARNING for rec in caplog.records
    )


# ── canonical_command_name helper ──────────────────────────────────────


def test_canonical_command_name_dispatch_on_layout(tmp_path: Path) -> None:
    """Mirror of agents helper test — same dispatch shape, layout tag
    drives the dispatch."""
    flat = tmp_path / CANONICAL_COMMAND_ROOT / "foo.md"
    dir_path = tmp_path / CANONICAL_COMMAND_ROOT / "foo" / "command.md"
    literal_command_flat = tmp_path / CANONICAL_COMMAND_ROOT / "command.md"
    assert canonical_command_name(flat, "flat") == "foo"
    assert canonical_command_name(dir_path, "dir") == "foo"
    # Literal `command.md` flat file is NOT misread as dir-form.
    assert canonical_command_name(literal_command_flat, "flat") == "command"


# ── parse_canonical_command layout dispatch ────────────────────────────


def test_parse_dir_layout_uses_parent_name(tmp_path: Path) -> None:
    target = tmp_path / CANONICAL_COMMAND_ROOT / "foo" / "command.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\ndescription: x\n---\n\nbody\n")
    cmd = parse_canonical_command(target, layout="dir")
    assert cmd.name == "foo"


def test_parse_flat_layout_with_filename_command(tmp_path: Path) -> None:
    """Flat file literally named ``command.md`` must NOT be misclassified
    as dir form. Layout tag is the source of truth."""
    target = tmp_path / CANONICAL_COMMAND_ROOT / "command.md"
    target.parent.mkdir(parents=True)
    target.write_text("---\ndescription: x\n---\n\nbody\n")
    cmd = parse_canonical_command(target, layout="flat")
    assert cmd.name == "command"


# ── extract_commands_to_canonical layout policy ────────────────────────


def test_extract_preserves_flat_layout_when_only_flat_exists(
    tmp_path: Path,
) -> None:
    _write_flat_command(tmp_path, "legacy")
    runtime = tmp_path / ".claude/commands"
    runtime.mkdir(parents=True)
    (runtime / "legacy.md").write_text(SAMPLE_COMMAND.replace("Simple", "UPDATED"))
    extract_commands_to_canonical(tmp_path, overwrite=True)
    flat_path = tmp_path / CANONICAL_COMMAND_ROOT / "legacy.md"
    assert flat_path.is_file()
    assert "UPDATED" in flat_path.read_text()
    assert not (tmp_path / CANONICAL_COMMAND_ROOT / "legacy" / "command.md").exists()


def test_extract_writes_dir_layout_for_new_command(tmp_path: Path) -> None:
    runtime = tmp_path / ".claude/commands"
    runtime.mkdir(parents=True)
    (runtime / "fresh.md").write_text(SAMPLE_COMMAND)
    extract_commands_to_canonical(tmp_path)
    assert (tmp_path / CANONICAL_COMMAND_ROOT / "fresh" / "command.md").is_file()
    assert not (tmp_path / CANONICAL_COMMAND_ROOT / "fresh.md").exists()


def test_extract_warns_when_both_layouts_present(
    tmp_path: Path,
    caplog: "logging.LogCaptureFixture",
) -> None:
    _write_flat_command(tmp_path, "shared")
    _write_dir_command(tmp_path, "shared")
    runtime = tmp_path / ".claude/commands"
    runtime.mkdir(parents=True)
    (runtime / "shared.md").write_text(SAMPLE_COMMAND.replace("Simple", "FROM RUNTIME"))
    with caplog.at_level(logging.WARNING):
        extract_commands_to_canonical(tmp_path, overwrite=True)
    dir_path = tmp_path / CANONICAL_COMMAND_ROOT / "shared" / "command.md"
    assert "FROM RUNTIME" in dir_path.read_text()
    flat_path = tmp_path / CANONICAL_COMMAND_ROOT / "shared.md"
    assert flat_path.is_file()
    assert "FROM RUNTIME" not in flat_path.read_text()
    assert any(
        "silently divergent" in rec.message and rec.levelno == logging.WARNING
        for rec in caplog.records
    )
