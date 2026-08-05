"""CLI compatibility tests for Pinned Context agent selection (#2006)."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import pinned_cmd


_COMMAND_CASES = (
    ("list", (), "_list_blocks", 0),
    ("get", ("block",), "_get_block", 2),
    ("set", ("block", "--content", "safe"), "_set_block", 5),
    ("delete", ("block",), "_delete_block", 2),
    ("compose", ("query",), "_compose", 1),
)


@pytest.mark.parametrize("flag", ("--agent-id", "--agent", "-a"))
@pytest.mark.parametrize(
    ("command", "command_args", "helper_name", "agent_arg_index"),
    _COMMAND_CASES,
    ids=[case[0] for case in _COMMAND_CASES],
)
def test_agent_selector_aliases_forward_the_same_agent_id(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    command: str,
    command_args: tuple[str, ...],
    helper_name: str,
    agent_arg_index: int,
) -> None:
    helper = AsyncMock(return_value=None)
    monkeypatch.setattr(pinned_cmd, helper_name, helper)

    result = CliRunner().invoke(
        pinned_cmd.pinned,
        [command, *command_args, flag, "planner"],
    )

    assert result.exit_code == 0, result.output
    assert "deprecated" not in result.output.lower()
    helper.assert_awaited_once()
    assert helper.await_args.args[agent_arg_index] == "planner"


@pytest.mark.parametrize(
    ("command", "command_args", "helper_name", "agent_arg_index"),
    _COMMAND_CASES,
    ids=[case[0] for case in _COMMAND_CASES],
)
def test_agent_selector_omission_preserves_none_default(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    command_args: tuple[str, ...],
    helper_name: str,
    agent_arg_index: int,
) -> None:
    helper = AsyncMock(return_value=None)
    monkeypatch.setattr(pinned_cmd, helper_name, helper)

    result = CliRunner().invoke(pinned_cmd.pinned, [command, *command_args])

    assert result.exit_code == 0, result.output
    helper.assert_awaited_once()
    assert helper.await_args.args[agent_arg_index] is None


@pytest.mark.parametrize("command", [case[0] for case in _COMMAND_CASES])
def test_agent_selector_help_advertises_canonical_and_compatible_spellings(command: str) -> None:
    result = CliRunner().invoke(pinned_cmd.pinned, [command, "--help"])

    assert result.exit_code == 0, result.output
    agent_option = next(line for line in result.output.splitlines() if "--agent-id" in line)
    assert "-a, --agent-id, --agent TEXT" in agent_option
    assert "Agent identifier." in agent_option
