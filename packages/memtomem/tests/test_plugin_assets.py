"""Cross-runtime plugin asset and optional Claude automation tests."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_CONTRACT = _ROOT / "packages/memtomem-plugin-assets/contract.toml"
_DISPATCHER = _ROOT / "packages/memtomem-claude-automation-plugin/bin/hook_dispatch.py"
_RENDERER = _ROOT / "tools/render_plugin_assets.py"


def _contract() -> dict:
    with _CONTRACT.open("rb") as handle:
        return tomllib.load(handle)


def _skill_files(root: Path) -> list[Path]:
    return sorted(root.glob("*/SKILL.md"))


def test_workflow_contract_is_safe_and_matches_runtime_assets() -> None:
    workflows = _contract()["workflows"]
    expected_tools = {"mem_add", "mem_index", "mem_recall", "mem_search", "mem_status"}
    actual_tools = {tool for workflow in workflows for tool in workflow["tools"]}
    assert actual_tools == expected_tools
    assert all("mem_do" not in workflow["tools"] for workflow in workflows)
    assert all(
        workflow["effect"] == "read" or workflow["implicit"] is False for workflow in workflows
    )

    claude = _skill_files(_ROOT / "packages/memtomem-claude-plugin/skills")
    codex = _skill_files(_ROOT / "plugins/memtomem/skills")
    assert {path.parent.name for path in claude} == {row["id"] for row in workflows}
    assert {path.parent.name for path in codex} == {row["codex_name"] for row in workflows}
    opencode = _skill_files(_ROOT / "packages/opencode-memtomem/skills")
    assert {path.parent.name for path in opencode} == {
        row["codex_name"] for row in workflows if row["effect"] == "read" and row["implicit"]
    }


def test_generated_assets_have_no_cross_runtime_or_legacy_leaks() -> None:
    claude_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _skill_files(_ROOT / "packages/memtomem-claude-plugin/skills")
    )
    codex_text = "\n".join(
        path.read_text(encoding="utf-8") for path in _skill_files(_ROOT / "plugins/memtomem/skills")
    )
    combined = claude_text + codex_text
    assert "TODO" not in combined
    assert "mem_do" not in combined
    assert "score > 0.5" not in combined
    assert "Ollama is the default" not in combined
    assert "$ARGUMENTS" not in codex_text
    assert "mcp__plugin_memtomem" not in codex_text

    opencode_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in _skill_files(_ROOT / "packages/opencode-memtomem/skills")
    )
    assert "memtomem_mem_search" in opencode_text
    assert "memtomem_mem_recall" in opencode_text
    assert "memtomem_mem_status" in opencode_text
    assert re.search(r"`mem_[a-z_]+`", opencode_text) is None
    assert "$ARGUMENTS" not in opencode_text


def test_generated_plugin_assets_are_in_sync() -> None:
    completed = subprocess.run(
        [sys.executable, str(_RENDERER), "--check"],
        cwd=_ROOT,
        capture_output=True,
        check=False,
        text=True,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


def test_core_version_is_single_sourced_across_automation_assets() -> None:
    version = _contract()["core"]["version"]
    dispatcher = _DISPATCHER.read_text(encoding="utf-8")
    match = re.search(r'^CORE_VERSION = "([^"]+)"$', dispatcher, re.MULTILINE)
    assert match and match.group(1) == version
    for path in (
        _ROOT / "packages/memtomem-claude-automation-plugin/README.md",
        _ROOT / "docs/guides/integrations/claude-code.md",
    ):
        assert f"memtomem=={version}" in path.read_text(encoding="utf-8")


@pytest.fixture
def fake_mm(tmp_path: Path) -> tuple[dict[str, str], Path]:
    script = tmp_path / "fake_mm.py"
    script.write_text(
        """import json
import os
import sys
from pathlib import Path

with Path(os.environ["FAKE_MM_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    print(os.environ.get("FAKE_MM_VERSION", "mm, version 0.3.8"))
elif sys.argv[1:2] == ["search"]:
    if os.environ.get("FAKE_MM_SEARCH_FAIL"):
        print(sys.argv[2], file=sys.stderr)
        raise SystemExit(2)
    print("trusted memory context")
""",
        encoding="utf-8",
    )
    if os.name == "nt":
        executable = tmp_path / "mm.bat"
        executable.write_text(
            f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n',
            encoding="utf-8",
        )
    else:
        executable = tmp_path / "mm"
        executable.write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n',
            encoding="utf-8",
        )
        executable.chmod(0o755)
    log = tmp_path / "mm.log"
    env = os.environ.copy()
    env.update(
        {
            "PATH": f"{tmp_path}{os.pathsep}{env.get('PATH', '')}",
            "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
            "FAKE_MM_LOG": str(log),
        }
    )
    if os.name == "nt":
        env["PATHEXT"] = f".BAT{os.pathsep}{env.get('PATHEXT', '')}"
    return env, log


def _dispatch(event: str, payload: object, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(_DISPATCHER), event],
        input=json.dumps(payload),
        env=env,
        capture_output=True,
        check=False,
        text=True,
        timeout=15,
    )


def _calls(log: Path) -> list[list[str]]:
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def test_automation_prompt_uses_json_stdin_and_argv_safely(
    fake_mm: tuple[dict[str, str], Path],
) -> None:
    env, log = fake_mm
    start = _dispatch("SessionStart", {"hook_event_name": "SessionStart"}, env)
    assert start.returncode == 0
    assert start.stdout == ""

    injection_target = log.parent / "hook-injection"
    prompt = f"Find the old decision; $(touch {injection_target}) and 'quotes'."
    result = _dispatch(
        "UserPromptSubmit",
        {"hook_event_name": "UserPromptSubmit", "prompt": prompt},
        env,
    )
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert output["hookSpecificOutput"]["additionalContext"] == "trusted memory context"
    search = next(call for call in _calls(log) if call[:1] == ["search"])
    assert search == ["search", prompt, "--top-k", "3", "--format", "context"]
    assert not injection_target.exists()


def test_automation_indexes_only_supported_write_paths_and_flushes(
    fake_mm: tuple[dict[str, str], Path], tmp_path: Path
) -> None:
    env, log = fake_mm
    _dispatch("SessionStart", {"hook_event_name": "SessionStart"}, env)
    target = tmp_path / "notes.md"
    ignored = tmp_path / "node_modules" / "ignored.md"
    for path in (target, ignored):
        _dispatch(
            "PostToolUse",
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "Write",
                "tool_input": {"file_path": str(path)},
            },
            env,
        )
    _dispatch("Stop", {"hook_event_name": "Stop"}, env)
    calls = _calls(log)
    assert ["index", "--debounce-window", "5", str(target)] in calls
    assert all(str(ignored) not in call for call in calls)
    assert ["index", "--flush"] in calls
    assert all("session" not in call for call in calls)


@pytest.mark.parametrize("payload", ["not an object", None, [], {"wrong": "event"}])
def test_automation_fails_open_on_invalid_input(
    fake_mm: tuple[dict[str, str], Path], payload: object
) -> None:
    env, _ = fake_mm
    result = _dispatch("UserPromptSubmit", payload, env)
    assert result.returncode == 0


def test_automation_reports_incompatible_dependency(fake_mm: tuple[dict[str, str], Path]) -> None:
    env, log = fake_mm
    env["FAKE_MM_VERSION"] = "mm, version 9.9.9"
    result = _dispatch("SessionStart", {"hook_event_name": "SessionStart"}, env)
    assert result.returncode == 0
    output = json.loads(result.stdout)
    assert "requires mm 0.3.8" in output["hookSpecificOutput"]["additionalContext"]
    _dispatch(
        "UserPromptSubmit",
        {"hook_event_name": "UserPromptSubmit", "prompt": "A sufficiently long prompt"},
        env,
    )
    assert _calls(log) == [["--version"]]


def test_automation_failure_log_does_not_store_prompt(
    fake_mm: tuple[dict[str, str], Path],
) -> None:
    env, log = fake_mm
    _dispatch("SessionStart", {"hook_event_name": "SessionStart"}, env)
    env["FAKE_MM_SEARCH_FAIL"] = "1"
    prompt = "private prompt text that must not reach the hook log"
    result = _dispatch(
        "UserPromptSubmit",
        {"hook_event_name": "UserPromptSubmit", "prompt": prompt},
        env,
    )
    assert result.returncode == 0
    hook_log = (log.parent / "data" / "hook.log").read_text(encoding="utf-8")
    assert prompt not in hook_log
    assert "command search returned 2" in hook_log
