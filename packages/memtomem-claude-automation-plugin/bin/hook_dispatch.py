#!/usr/bin/env python3
"""Fail-open Claude hook dispatcher for the optional automation plugin."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


CORE_VERSION = "0.3.12"
MAX_QUERY_CHARS = 500
MAX_CONTEXT_CHARS = 10_000
SUPPORTED_SUFFIXES = {
    ".c",
    ".cpp",
    ".go",
    ".h",
    ".hpp",
    ".java",
    ".js",
    ".json",
    ".jsx",
    ".kt",
    ".md",
    ".py",
    ".rb",
    ".rs",
    ".sh",
    ".swift",
    ".toml",
    ".ts",
    ".tsx",
    ".yaml",
    ".yml",
}
EXCLUDED_PARTS = {
    ".cache",
    ".git",
    ".next",
    ".nuxt",
    ".venv",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "venv",
}


def _data_dir() -> Path:
    configured = os.environ.get("CLAUDE_PLUGIN_DATA")
    path = (
        Path(configured).expanduser()
        if configured
        else Path.home() / ".claude" / "memtomem-automation"
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _log(message: str) -> None:
    try:
        stamp = datetime.now(UTC).isoformat(timespec="seconds")
        with (_data_dir() / "hook.log").open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {message}\n")
    except OSError:
        pass


def _read_payload() -> dict[str, Any] | None:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        _log(f"invalid input: {exc}")
        return None
    if not isinstance(value, dict):
        _log("invalid input: top-level JSON must be an object")
        return None
    return value


def _version_from(output: str) -> str | None:
    match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", output)
    return match.group(1) if match else None


def _probe_mm() -> tuple[str | None, str | None]:
    executable = shutil.which("mm")
    if not executable:
        return None, None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"mm version probe failed: {exc}")
        return executable, None
    return executable, _version_from(completed.stdout + completed.stderr)


def _mm_probe(*, refresh: bool = False) -> tuple[str | None, str | None]:
    cache = _data_dir() / "compat.json"
    if not refresh:
        try:
            value = json.loads(cache.read_text(encoding="utf-8"))
            executable = value.get("executable")
            version = value.get("version")
            if value.get("checked") is True:
                if executable and not Path(executable).is_file():
                    executable = None
                return executable, version
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass

    executable, version = _probe_mm()
    try:
        cache.write_text(
            json.dumps({"checked": True, "executable": executable, "version": version}) + "\n",
            encoding="utf-8",
        )
    except OSError:
        pass
    return executable, version


def _run(mm: str, args: list[str], timeout: float) -> subprocess.CompletedProcess[str] | None:
    subcommand = args[0] if args else "unknown"
    try:
        completed = subprocess.run(
            [mm, *args],
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _log(f"command {subcommand} failed to run: {type(exc).__name__}")
        return None
    if completed.returncode != 0:
        _log(f"command {subcommand} returned {completed.returncode}")
    return completed


def _emit_context(event: str, context: str) -> None:
    payload = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context[:MAX_CONTEXT_CHARS],
        }
    }
    print(json.dumps(payload, ensure_ascii=False))


def _session_start() -> None:
    executable, version = _mm_probe(refresh=True)
    if executable and version == CORE_VERSION:
        return
    if not executable:
        detail = f"Install the optional automation dependency with `uv tool install memtomem=={CORE_VERSION}`."
    else:
        detail = f"memtomem automation requires mm {CORE_VERSION}; found {version or 'an unknown version'}."
    _emit_context("SessionStart", detail)


def _user_prompt(payload: dict[str, Any], mm: str) -> None:
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or len(prompt.strip()) <= 20:
        return
    query = prompt.strip()[:MAX_QUERY_CHARS]
    completed = _run(mm, ["search", query, "--top-k", "3", "--format", "context"], 4)
    if completed and completed.returncode == 0 and completed.stdout.strip():
        _emit_context("UserPromptSubmit", completed.stdout.strip())


def _post_tool_use(payload: dict[str, Any], mm: str) -> None:
    if payload.get("tool_name") not in {"Write", "Edit"}:
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    raw_path = tool_input.get("file_path")
    if not isinstance(raw_path, str) or not raw_path.strip():
        return
    path = Path(raw_path).expanduser()
    if path.suffix.lower() not in SUPPORTED_SUFFIXES or EXCLUDED_PARTS.intersection(path.parts):
        return
    _run(mm, ["index", "--debounce-window", "5", str(path)], 8)


def _stop(mm: str) -> None:
    _run(mm, ["index", "--flush"], 9)


def main() -> int:
    event = sys.argv[1] if len(sys.argv) == 2 else ""
    payload = _read_payload()
    if payload is None or payload.get("hook_event_name") != event:
        return 0

    if event == "SessionStart":
        _session_start()
        return 0

    executable, version = _mm_probe()
    if not executable or version != CORE_VERSION:
        _log(f"skipped {event}: compatible mm {CORE_VERSION} not available")
        return 0
    mm = executable
    if event == "UserPromptSubmit":
        _user_prompt(payload, mm)
    elif event == "PostToolUse":
        _post_tool_use(payload, mm)
    elif event == "Stop":
        _stop(mm)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
