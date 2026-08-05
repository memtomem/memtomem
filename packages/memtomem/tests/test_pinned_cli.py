"""CLI compatibility tests for Pinned Context agent selection (#2006)."""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import pinned_cmd


_COMMAND_CASES = (
    ("list", (), "_list_blocks"),
    ("get", ("block",), "_get_block"),
    ("set", ("block", "--content", "safe"), "_set_block"),
    ("delete", ("block",), "_delete_block"),
    ("compose", ("query",), "_compose"),
)


@pytest.mark.parametrize("flag", ("--agent-id", "--agent", "-a"))
@pytest.mark.parametrize(
    ("command", "command_args", "helper_name"),
    _COMMAND_CASES,
    ids=[case[0] for case in _COMMAND_CASES],
)
def test_agent_selector_aliases_forward_the_same_agent_id(
    monkeypatch: pytest.MonkeyPatch,
    flag: str,
    command: str,
    command_args: tuple[str, ...],
    helper_name: str,
) -> None:
    helper_fn = getattr(pinned_cmd, helper_name)
    helper = AsyncMock(return_value=None)
    monkeypatch.setattr(pinned_cmd, helper_name, helper)

    result = CliRunner().invoke(
        pinned_cmd.pinned,
        [command, *command_args, flag, "planner"],
    )

    assert result.exit_code == 0, result.output
    assert "deprecated" not in result.output.lower()
    helper.assert_awaited_once()
    call = helper.await_args
    assert call is not None
    bound = inspect.signature(helper_fn).bind(*call.args, **call.kwargs)
    assert bound.arguments["agent_id"] == "planner"


@pytest.mark.parametrize(
    ("command", "command_args", "helper_name"),
    _COMMAND_CASES,
    ids=[case[0] for case in _COMMAND_CASES],
)
def test_agent_selector_omission_preserves_none_default(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    command_args: tuple[str, ...],
    helper_name: str,
) -> None:
    helper_fn = getattr(pinned_cmd, helper_name)
    helper = AsyncMock(return_value=None)
    monkeypatch.setattr(pinned_cmd, helper_name, helper)

    result = CliRunner().invoke(pinned_cmd.pinned, [command, *command_args])

    assert result.exit_code == 0, result.output
    helper.assert_awaited_once()
    call = helper.await_args
    assert call is not None
    bound = inspect.signature(helper_fn).bind(*call.args, **call.kwargs)
    assert bound.arguments["agent_id"] is None


@pytest.mark.parametrize("command", [case[0] for case in _COMMAND_CASES])
def test_agent_selector_help_advertises_canonical_and_compatible_spellings(command: str) -> None:
    result = CliRunner().invoke(pinned_cmd.pinned, [command, "--help"])

    assert result.exit_code == 0, result.output
    agent_option = next((line for line in result.output.splitlines() if "--agent-id" in line), "")
    assert "-a, --agent-id, --agent TEXT" in agent_option
    assert "Agent identifier" in result.output
