#!/usr/bin/env python3
"""Run the automation plugin's hook dispatcher under the interpreter that runs this script.

The plugin's hooks launch ``uv run --no-project python .../hook_dispatch.py <Event>`` with no
interpreter requested, so uv uses the first one it finds, which can be a system Python older
than the 3.12 this monorepo targets. CI runs this script with ``uv run --no-project --python
3.8``, the oldest Python the dispatcher promises to start on; the suite runs it on its own
interpreter. Every event goes through the dispatcher as a child process against a fake ``mm``.
Standard library only, and written to run on Python 3.8.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_DISPATCHER = Path("packages/memtomem-claude-automation-plugin/bin/hook_dispatch.py")
_FAKE_MM = """import json
import os
import sys
from pathlib import Path

with Path(os.environ["FAKE_MM_LOG"]).open("a", encoding="utf-8") as handle:
    handle.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--version"]:
    print("mm, version " + os.environ["FAKE_MM_VERSION"])
elif sys.argv[1:2] == ["search"]:
    if os.environ.get("FAKE_MM_SEARCH_FAIL"):
        print(sys.argv[2], file=sys.stderr)
        raise SystemExit(2)
    print("trusted memory context")
"""


class Scenario:
    """One isolated run: its own cwd, plugin data directory, PATH and fake ``mm`` log."""

    def __init__(self, dispatcher: Path, root: Path, *, with_mm: bool, mm_version: str) -> None:
        self.dispatcher = dispatcher
        self.cwd = root / "cwd"
        self.data = root / "data"
        self.log = root / "mm.log"
        self._seen = 0
        self.cwd.mkdir()
        env = {k: v for k, v in os.environ.items() if not k.startswith("FAKE_MM_")}
        env["CLAUDE_PLUGIN_DATA"] = str(self.data)
        env["FAKE_MM_LOG"] = str(self.log)
        env["FAKE_MM_VERSION"] = mm_version
        bin_dir = root / "bin"
        bin_dir.mkdir()
        if with_mm:
            script = root / "fake_mm.py"
            script.write_text(_FAKE_MM, encoding="utf-8")
            if os.name == "nt":
                launcher = bin_dir / "mm.bat"
                launcher.write_text(
                    f'@echo off\r\n"{sys.executable}" "{script}" %*\r\n', encoding="utf-8"
                )
                env["PATHEXT"] = f".BAT{os.pathsep}{env.get('PATHEXT', '')}"
            else:
                launcher = bin_dir / "mm"
                launcher.write_text(
                    f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="utf-8"
                )
                launcher.chmod(0o755)
            env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
        else:
            # Replaced, not prefixed: a developer's own ``mm`` must not be found.
            env["PATH"] = str(bin_dir)
        self.env = env

    def dispatch(self, event: str, stdin: str) -> str:
        """Run one hook; it must exit 0 with nothing on stderr. Returns stdout."""
        completed = subprocess.run(
            [sys.executable, str(self.dispatcher), event],
            input=stdin,
            env=self.env,
            cwd=self.cwd,
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        assert completed.returncode == 0 and completed.stderr == "", (
            event,
            completed.returncode,
            completed.stderr,
        )
        return completed.stdout

    def event(self, event: str, **fields: object) -> str:
        return self.dispatch(event, json.dumps({"hook_event_name": event, **fields}))

    def calls(self) -> list:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def new_calls(self) -> list:
        """The ``mm`` calls recorded since the previous ``new_calls()``."""
        calls = self.calls()
        fresh = calls[self._seen :]
        self._seen = len(calls)
        return fresh

    def hook_log(self) -> str:
        path = self.data / "hook.log"
        return path.read_text(encoding="utf-8") if path.exists() else ""


def _context(stdout: str, event: str) -> str:
    output = json.loads(stdout)["hookSpecificOutput"]
    assert output["hookEventName"] == event, output
    return output["additionalContext"]


def _core_version(dispatcher: Path) -> str:
    match = re.search(
        r'^CORE_VERSION = "([^"]+)"$', dispatcher.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert match, f"no CORE_VERSION line in {dispatcher}"
    return match.group(1)


def run(dispatcher: Path, scratch: Path) -> None:
    version = _core_version(dispatcher)

    def scenario(name: str, *, with_mm: bool = True, mm_version: str = version) -> Scenario:
        root = scratch / name
        root.mkdir()
        return Scenario(dispatcher, root, with_mm=with_mm, mm_version=mm_version)

    # No ``mm`` on PATH: SessionStart prints the install hint; the other hooks skip and log.
    missing = scenario("missing", with_mm=False)
    hint = (
        "Install the optional automation dependency with "
        f"`uv tool install --python 3.12 memtomem=={version}`."
    )
    assert _context(missing.event("SessionStart"), "SessionStart") == hint
    assert missing.event("UserPromptSubmit", prompt="a prompt longer than twenty chars") == ""
    assert f"skipped UserPromptSubmit: compatible mm {version} not available" in missing.hook_log()

    # A compatible ``mm``: each event makes exactly the call the plugin promises, and no other.
    ok = scenario("compatible")
    assert ok.event("SessionStart") == ""
    assert ok.new_calls() == [["--version"]]
    prompt = "Find the old decision about the release train."
    assert _context(ok.event("UserPromptSubmit", prompt=prompt), "UserPromptSubmit") == (
        "trusted memory context"
    )
    assert ok.new_calls() == [["search", prompt, "--top-k", "3", "--format", "context"]]
    # Relative to the scenario's cwd, so a temp directory under an excluded name such as
    # ``.cache`` or ``build`` cannot make the dispatcher skip it.
    assert ok.event("PostToolUse", tool_name="Write", tool_input={"file_path": "notes.md"}) == ""
    assert ok.new_calls() == [["index", "--debounce-window", "5", "notes.md"]]
    assert ok.event("Stop") == ""
    assert ok.new_calls() == [["index", "--flush"]]

    # A failing search is logged with its status, never with the prompt.
    failing = scenario("failing-search")
    failing.env["FAKE_MM_SEARCH_FAIL"] = "1"
    private = "private prompt text that must not reach the hook log"
    assert failing.event("UserPromptSubmit", prompt=private) == ""
    assert "command search returned 2" in failing.hook_log(), failing.hook_log()
    assert private not in failing.hook_log()

    # Invalid stdin fails open without output and without calling ``mm``.
    invalid = scenario("invalid-input")
    assert invalid.dispatch("UserPromptSubmit", "not json") == ""
    assert "invalid input:" in invalid.hook_log(), invalid.hook_log()
    assert invalid.calls() == [], invalid.calls()

    # An ``mm`` of another version: SessionStart says so and later hooks do not call it.
    wrong = scenario("incompatible", mm_version="9.9.9")
    assert _context(wrong.event("SessionStart"), "SessionStart") == (
        f"memtomem automation requires mm {version}; found 9.9.9."
    )
    assert wrong.event("UserPromptSubmit", prompt="a prompt longer than twenty chars") == ""
    assert wrong.calls() == [["--version"]], wrong.calls()

    # A cached ``mm`` that cannot be launched goes through the runner's OSError handler.
    broken = scenario("unlaunchable", with_mm=False)
    not_executable = broken.cwd / "mm-not-executable"
    not_executable.write_text("not a program\n", encoding="utf-8")
    not_executable.chmod(0o644)
    broken.data.mkdir()
    (broken.data / "compat.json").write_text(
        json.dumps({"checked": True, "executable": str(not_executable), "version": version}),
        encoding="utf-8",
    )
    assert broken.event("UserPromptSubmit", prompt="a prompt longer than twenty chars") == ""
    assert "command search failed to run: " in broken.hook_log(), broken.hook_log()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    if sys.flags.optimize:
        # Every check, and every hook run inside one, is an ``assert``: -O would skip them all.
        print("automation dispatcher smoke: refusing to run with -O or PYTHONOPTIMIZE set")
        return 2
    dispatcher = args.repo_root.resolve() / _DISPATCHER
    with tempfile.TemporaryDirectory() as scratch:
        run(dispatcher, Path(scratch))
    print(f"automation dispatcher smoke ok on Python {sys.version.split()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
