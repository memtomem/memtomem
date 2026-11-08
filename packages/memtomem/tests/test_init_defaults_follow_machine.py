"""What pressing Enter through ``mm init`` selects on the machine it runs on (#3149, #3150)."""

from __future__ import annotations

import sys
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from memtomem.cli import _bootstrap, cli, init_cmd

from .helpers import set_home

_OPTIONAL_MODULES = ("fastembed", "huggingface_hub", "tokenizers")


def _block_optional_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """CI installs every extra, so absence is simulated: ``find_spec`` returns None and
    ``import`` raises for a ``None`` entry in ``sys.modules``."""
    for name in _OPTIONAL_MODULES:
        monkeypatch.setitem(sys.modules, name, None)


def _run_step(step, state: dict, user_input: str = "\n") -> str:
    @click.command()
    def harness() -> None:
        step(state)

    result = CliRunner().invoke(harness, input=user_input)
    assert result.exit_code == 0, result.output
    return result.output


class _Profile:
    def __init__(self, kind: str, venv: Path | None) -> None:
        self.cwd_install_type = kind
        self.workspace_venv_path = venv


@pytest.mark.parametrize(("onnx_will_work", "expected"), [(True, "english"), (False, "minimal")])
def test_enter_at_preset_picker_follows_onnx(
    monkeypatch: pytest.MonkeyPatch, onnx_will_work: bool, expected: str
) -> None:
    monkeypatch.setattr(init_cmd, "_onnx_preset_will_work", lambda state: onnx_will_work)
    state: dict = {}
    output = _run_step(init_cmd._step_preset_picker, state)
    assert state["_preset_choice"] == expected
    assert ("needs memtomem[onnx]" in output) is not onnx_will_work


def test_explicit_onnx_preset_stays_selectable_without_the_extra(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_cmd, "_onnx_preset_will_work", lambda state: False)
    state: dict = {}
    _run_step(init_cmd._step_preset_picker, state, "2\n")
    assert state["_preset_choice"] == "english"


def test_pypi_install_reads_the_running_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(init_cmd, "_have_module", lambda name: name != "fastembed")
    assert init_cmd._onnx_preset_will_work({}) is False
    monkeypatch.setattr(init_cmd, "_have_module", lambda name: True)
    assert init_cmd._onnx_preset_will_work({}) is True


def test_base_only_workspace_is_not_rescued_by_the_running_interpreter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").touch()
    monkeypatch.setattr(init_cmd, "_have_module", lambda name: True)
    monkeypatch.setattr(init_cmd, "_probe_workspace_extras", lambda py: set())
    state = {"_profile": _Profile("source", tmp_path)}
    assert init_cmd._onnx_preset_will_work(state) is False


def test_failed_workspace_probe_counts_as_missing_only_for_the_picker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").touch()
    monkeypatch.setattr(init_cmd, "_have_module", lambda name: True)
    monkeypatch.setattr(init_cmd, "_probe_workspace_extras", lambda py: None)
    monkeypatch.setattr("memtomem.cli.web._missing_web_deps", lambda: None)
    state = {"_profile": _Profile("source", tmp_path), "provider": "onnx"}
    assert init_cmd._onnx_preset_will_work(state) is False
    assert init_cmd._collect_missing_extras(state) == []


def test_workspace_interpreter_that_cannot_run_defaults_the_picker_to_minimal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "python").touch()

    def refuse(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(init_cmd, "_run", refuse)
    monkeypatch.setattr(init_cmd, "_have_module", lambda name: True)
    state: dict = {"_profile": _Profile("source", tmp_path)}
    _run_step(init_cmd._step_preset_picker, state)
    assert state["_preset_choice"] == "minimal"


def test_workspace_without_an_interpreter_counts_as_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(init_cmd, "_have_module", lambda name: True)
    state = {"_profile": _Profile("project", tmp_path)}
    assert init_cmd._onnx_preset_will_work(state) is False


def test_workspace_python_finds_the_windows_layout(tmp_path: Path) -> None:
    (tmp_path / "Scripts").mkdir()
    py = tmp_path / "Scripts" / "python.exe"
    py.touch()
    assert init_cmd._workspace_python({"_profile": _Profile("source", tmp_path)}) == py


@pytest.mark.parametrize(("found", "expected"), [(True, 1), (False, 3)])
def test_enter_at_connect_step_follows_the_claude_cli(
    monkeypatch: pytest.MonkeyPatch, found: bool, expected: int
) -> None:
    monkeypatch.setattr(init_cmd, "_claude_cli_found", lambda: found)
    state: dict = {}
    output = _run_step(init_cmd._step_mcp, state)
    assert state["mcp_choice"] == expected
    assert ("claude CLI not found" in output) is not found


def test_explicit_claude_choice_without_the_cli_still_runs_the_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(init_cmd, "_claude_cli_found", lambda: False)
    state: dict = {}
    _run_step(init_cmd._step_mcp, state, "1\n")
    assert state["mcp_choice"] == 1
    calls: list[bool] = []
    monkeypatch.setattr("memtomem.cli._claude_mcp.preflight", lambda: calls.append(True) or False)
    assert init_cmd._preflight_mcp_choice(state["mcp_choice"]) is False
    assert calls == [True]


@pytest.mark.parametrize(
    ("provider", "missing", "expected", "absent"),
    [
        ("onnx", ["onnx"], "'mm add', 'mm search' and 'mm index'", "Reranking"),
        ("onnx", ["onnx", "web"], "'mm add', 'mm search', 'mm index'", "Reranking"),
        ("none", ["onnx"], "Reranking keeps the original order", "mm add"),
        ("none", ["onnx", "web"], "Reranking keeps the original order", "mm add"),
    ],
)
def test_missing_extra_warning_matches_what_needs_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    provider: str,
    missing: list[str],
    expected: str,
    absent: str,
) -> None:
    monkeypatch.setattr(init_cmd, "_extra_install_hint", lambda extras, state: "hint")
    init_cmd._emit_missing_extras_warning(missing, {"provider": provider})
    output = capsys.readouterr().out
    assert expected in output
    assert absent not in output


def test_enter_at_every_prompt_ends_with_a_working_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    # Resolved once at import, possibly under an earlier test's HOME.
    monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", home / ".memtomem" / "config.json")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    monkeypatch.setattr(init_cmd, "_isatty", lambda: True)
    monkeypatch.setattr(init_cmd, "_claude_cli_found", lambda: False)
    _block_optional_modules(monkeypatch)

    runner = CliRunner()
    setup = runner.invoke(init_cmd.init, [], input="\n" * 12)
    assert setup.exit_code == 0, setup.output
    assert (home / ".memtomem" / "config.json").is_file()

    added = runner.invoke(cli, ["add", "retry policy note"])
    assert added.exit_code == 0, added.output
    found = runner.invoke(cli, ["search", "retry"])
    assert found.exit_code == 0, found.output
    assert "retry policy note" in found.output
