"""CLI compatibility tests for Pinned Context agent selection (#2006)."""

from __future__ import annotations

import inspect
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
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


_EMPTY_LINE = "No Pinned Context blocks found."
_CREATE_HINT = "mm pinned set <block_id> --content"


def _fake_store(monkeypatch: pytest.MonkeyPatch, blocks: list[SimpleNamespace]) -> None:
    @asynccontextmanager
    async def fake():
        yield None, SimpleNamespace(list=lambda agent_id=None: blocks)

    monkeypatch.setattr(pinned_cmd, "_store_context", fake)


@pytest.mark.parametrize("agent_args", ((), ("--agent-id", "planner")), ids=("no-agent", "agent"))
def test_list_empty_text_output_names_the_create_command(
    monkeypatch: pytest.MonkeyPatch, agent_args: tuple[str, ...]
) -> None:
    _fake_store(monkeypatch, [])

    result = CliRunner().invoke(pinned_cmd.pinned, ["list", *agent_args])

    assert result.exit_code == 0, result.output
    assert _EMPTY_LINE in result.output
    assert _CREATE_HINT in result.output


def test_list_empty_json_output_stays_an_empty_array(monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_store(monkeypatch, [])

    result = CliRunner().invoke(pinned_cmd.pinned, ["list", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == []
    assert _CREATE_HINT not in result.output


def test_list_non_empty_output_has_no_create_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    block = SimpleNamespace(block_id="demo", scope="user", agent_id=None, description="tone")
    _fake_store(monkeypatch, [block])

    result = CliRunner().invoke(pinned_cmd.pinned, ["list"])

    assert result.exit_code == 0, result.output
    assert result.output == "demo [user] tone\n"
    assert _CREATE_HINT not in result.output


class TestSetFileErrors:
    """``mm pinned set --file`` reports a bad path as an error, not a traceback."""

    @pytest.fixture
    def set_block(self, monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
        helper = AsyncMock(return_value=None)
        monkeypatch.setattr(pinned_cmd, "_set_block", helper)
        return helper

    def test_missing_file_is_a_usage_error(self, set_block: AsyncMock, tmp_path) -> None:
        missing = tmp_path / "missing.md"
        result = CliRunner().invoke(pinned_cmd.pinned, ["set", "block", "--file", str(missing)])

        assert result.exit_code == 2, result.output
        assert isinstance(result.exception, SystemExit)
        assert "does not exist" in result.output
        set_block.assert_not_awaited()

    def test_directory_is_a_usage_error(self, set_block: AsyncMock, tmp_path) -> None:
        result = CliRunner().invoke(pinned_cmd.pinned, ["set", "block", "--file", str(tmp_path)])

        assert result.exit_code == 2, result.output
        assert isinstance(result.exception, SystemExit)
        assert "is a directory" in result.output
        set_block.assert_not_awaited()

    def test_non_utf8_file_is_reported(self, set_block: AsyncMock, tmp_path) -> None:
        binary = tmp_path / "block.bin"
        binary.write_bytes(b"\xff\xfe\x00not utf-8")
        result = CliRunner().invoke(pinned_cmd.pinned, ["set", "block", "--file", str(binary)])

        assert result.exit_code == 1, result.output
        assert isinstance(result.exception, SystemExit)
        assert result.output.startswith(f"Error: Could not read {binary}: ")
        set_block.assert_not_awaited()

    def test_readable_file_is_passed_through(self, set_block: AsyncMock, tmp_path) -> None:
        body = tmp_path / "block.md"
        body.write_text("Prefer short answers.\n", encoding="utf-8")
        result = CliRunner().invoke(pinned_cmd.pinned, ["set", "block", "--file", str(body)])

        assert result.exit_code == 0, result.output
        set_block.assert_awaited_once()
        assert set_block.await_args is not None
        assert set_block.await_args.args[1] == "Prefer short answers.\n"
