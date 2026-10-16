"""The root group resolves subcommands lazily without changing what it exposes.

``memtomem.cli`` used to import every command module at import time, so
``mm version`` paid for the config, storage, embedding and search stacks. The group now
holds a ``_LazyCommands`` mapping that imports a module only when its command
is read. These tests pin both halves: the registry is the same set of commands,
and importing the package or asking about names imports none of them.
"""

from __future__ import annotations

import subprocess
import sys

import click
import pytest
from click.testing import CliRunner

from memtomem.cli import _LAZY_SUBCOMMANDS, _LazyCommands, cli

# The registry as it stood when every module was imported eagerly. A command
# dropped from (or renamed in) the lazy map fails here, not in a user's shell.
EXPECTED_COMMANDS = {
    "activity",
    "add",
    "agent",
    "config",
    "context",
    "doctor",
    "embedding-reset",
    "gc",
    "index",
    "ingest",
    "init",
    "mem",
    "memory",
    "pinned",
    "purge",
    "quality",
    "recall",
    "reset",
    "review",
    "schedule",
    "search",
    "serve",
    "session",
    "shell",
    "status",
    "sync-doctor",
    "tags",
    "uninstall",
    "upgrade",
    "version",
    "warmup",
    "watchdog",
    "web",
    "wiki",
}


def test_registry_is_the_expected_command_set():
    assert set(cli.commands) == EXPECTED_COMMANDS
    assert len(cli.commands) == len(EXPECTED_COMMANDS)


@pytest.mark.parametrize("name", sorted(_LAZY_SUBCOMMANDS))
def test_each_lazy_entry_loads_the_command_it_names(name):
    command = cli.commands[name]
    assert isinstance(command, click.Command)
    assert command.name == name


def test_importing_the_cli_imports_no_command_module():
    modules = {module for module, _ in _LAZY_SUBCOMMANDS.values()}
    code = (
        "import sys, memtomem.cli\n"
        f"print(sorted(m for m in {sorted(modules)!r} if m in sys.modules))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    ).stdout
    assert out.strip() == "[]"


def test_version_runs_without_importing_command_modules():
    modules = {module for module, _ in _LAZY_SUBCOMMANDS.values()}
    code = (
        "import sys\n"
        "from memtomem.cli import cli\n"
        "try:\n"
        "    cli(['version'], standalone_mode=False)\n"
        "finally:\n"
        f"    print(sorted(m for m in {sorted(modules)!r} if m in sys.modules))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True, timeout=60
    ).stdout
    assert out.splitlines()[-1] == "[]"


# A spec naming a module that does not exist: any operation that imported it
# would raise, so these show which operations stay import-free.
_MISSING = {"ghost": ("memtomem.cli._no_such_module", "ghost")}


def test_names_membership_and_length_do_not_import():
    commands = _LazyCommands(_MISSING)
    assert "ghost" in commands
    assert "other" not in commands
    assert list(commands) == ["ghost"]
    assert len(commands) == 1


def test_reading_an_entry_imports_its_module():
    commands = _LazyCommands(_MISSING)
    with pytest.raises(ModuleNotFoundError):
        commands["ghost"]


def test_unknown_name_is_a_key_error():
    commands = _LazyCommands(_MISSING)
    with pytest.raises(KeyError):
        commands["other"]
    assert commands.get("other") is None


def test_assignment_overrides_a_lazy_entry_without_importing():
    commands = _LazyCommands(_MISSING)
    replacement = click.Command("ghost")
    commands["ghost"] = replacement
    assert commands["ghost"] is replacement
    assert list(commands) == ["ghost"]
    assert len(commands) == 1


def test_assignment_adds_a_new_name_after_the_lazy_ones():
    commands = _LazyCommands(_MISSING)
    extra = click.Command("extra")
    commands["extra"] = extra
    assert list(commands) == ["ghost", "extra"]
    assert len(commands) == 2


def test_deletion_removes_both_lazy_and_loaded_entries():
    commands = _LazyCommands(_MISSING)
    commands["extra"] = click.Command("extra")
    del commands["ghost"]
    del commands["extra"]
    assert list(commands) == []
    with pytest.raises(KeyError):
        del commands["ghost"]


def test_nested_group_lookup_resolves_through_the_lazy_entry():
    assert "search" in cli.commands["agent"].commands


def test_root_help_lists_every_command():
    result = CliRunner().invoke(cli, ["--help"])
    assert result.exit_code == 0, result.output
    listed = set()
    in_commands = False
    for line in result.output.splitlines():
        if line.strip() == "Commands:":
            in_commands = True
            continue
        # A wrapped help line is indented deeper than the two spaces before a name.
        if in_commands and line.startswith("  ") and not line.startswith("   "):
            listed.add(line.split()[0])
    assert listed == EXPECTED_COMMANDS


def test_unknown_command_is_rejected():
    result = CliRunner().invoke(cli, ["serch"])
    assert result.exit_code != 0
    assert "No such command 'serch'" in result.output
    # Clicks that define ``NoSuchCommand`` (8.4.0+) suggest close names from
    # ``Group.commands``; older ones, down to the 8.1 floor, suggest nothing.
    if hasattr(click.exceptions, "NoSuchCommand"):
        assert "search" in result.output
