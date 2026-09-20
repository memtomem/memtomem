"""``mm serve`` really starts the MCP stdio server in a subprocess.

This has to be an end-to-end subprocess test, not a mock. The failure it
guards is an argv collision that only exists across the Click/argparse
boundary: ``memtomem.server.main`` re-parses the command line with argparse,
and Click does not remove ``serve`` from ``sys.argv``. A test that patched
``server.main`` and asserted it was called would pass while the real command
died with ``unrecognized arguments: serve`` and exit code 2.

The registry record depends on this path: a client launches
``uvx memtomem`` with the record's ``packageArguments`` appended, so ``serve``
is what actually starts the server for every registry-driven install.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import NamedTuple

import pytest


_DEADLINE_S = 90.0
_INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "serve-e2e", "version": "0"},
    },
}


def _console_script(name: str) -> str:
    """The installed entry point beside the running interpreter.

    This is the file ``uvx memtomem`` resolves to, so the probe exercises the
    real ``project.scripts`` mapping rather than an import that happens to
    work. ``python -m memtomem.cli`` is not a substitute: the package has no
    ``__main__``, so it cannot be executed that way at all.

    Missing entry point is a **failure, not a skip**. Every valid test
    environment (``uv run pytest``, ``uv pip install -e``) provides these, and
    a broken or renamed mapping is precisely what this module exists to catch
    — skipping on it would turn the guard into a CI false-green at the one
    moment it matters. ``test_docs_quickstart.py`` makes the same call for the
    same reason. ``shutil.which`` supplies the platform suffix (``.exe`` via
    PATHEXT on Windows, none on POSIX).
    """
    found = shutil.which(name, path=os.path.dirname(sys.executable))
    if found is None:
        pytest.fail(
            f"{name} entry point not found beside {sys.executable}. "
            "Run `uv pip install -e packages/memtomem[all]` before testing."
        )
    return found


def _excerpt(text: str, *, head: int, tail: int) -> str:
    """Head and tail with an explicit marker for what was dropped.

    A prefix-only cut is the wrong shape for this content: the server logs a
    lot during startup and the thing worth reading — the traceback, the
    refusal, the last line before it died — is at the *end*. Cutting from the
    front reliably throws away the answer.
    """
    if len(text) <= head + tail:
        return text
    dropped = len(text) - head - tail
    return f"{text[:head]}\n… [{dropped} characters omitted] …\n{text[-tail:]}"


class _Exchange(NamedTuple):
    """Both streams, so a failure message can show why the child said nothing."""

    responses: dict[int, dict]
    stdout: str
    stderr: str

    def why(self) -> str:
        return (
            f"\nstdout={_excerpt(self.stdout, head=300, tail=500)}"
            f"\nstderr={_excerpt(self.stderr, head=300, tail=1200)}"
        )


def _env(home: Path) -> dict[str, str]:
    """An isolated HOME so the probe never reads or writes the real store."""
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["MEMTOMEM_STORAGE__SQLITE_PATH"] = str(home / "probe.db")
    # This field is a list: pydantic-settings parses it as JSON, so a bare
    # path raises SettingsError before the server ever starts.
    env["MEMTOMEM_INDEXING__MEMORY_DIRS"] = json.dumps([str(home / "notes")])
    env.pop("MEMTOMEM_TOOL_MODE", None)
    # The child calls ``app_lifespan`` → ``_load_dotenv`` → a bare
    # ``load_dotenv()``. That resolves through ``find_dotenv(usecwd=False)``,
    # which walks up from the **calling module's own file**, not from cwd —
    # so under an editable install the search reaches the checkout root and
    # finds a developer's ``.env`` there. Neither HOME nor cwd closes that
    # channel; only this does. The value is not arbitrary: python-dotenv
    # casefolds it and accepts only {1, true, t, yes, y}, so ``0``, ``""``
    # and any other string leave loading *enabled*. Pinned below, because a
    # tidy-up to an empty string would look like a no-op and silently reopen
    # the channel.
    #
    # It matters because ``app_lifespan`` calls ``build_fresh_config`` after
    # ``_load_dotenv``, and that reads ``os.environ`` at call time: measured,
    # a repo-root ``.env`` carrying ``MEMTOMEM_WARMUP__ENABLED=true`` ran
    # model warmup inside the handshake probe.
    env["PYTHON_DOTENV_DISABLED"] = "1"
    return env


def _talk(argv: list[str], home: Path, payloads: list[dict], expect_ids: set[int]) -> _Exchange:
    """Send ``payloads``, read until every id in ``expect_ids`` has answered.

    stdin stays open until then. Writing the requests and closing stdin in one
    shot races the server's own shutdown-on-EOF: ``initialize`` usually lands
    but ``tools/list`` may not, which made an earlier version of this test
    pass or fail run to run. Reading until the responses arrive removes the
    race instead of sleeping past it.

    Both streams are drained on their own threads. stderr is a pipe with a
    finite buffer and the server logs to it during startup: left unread, a
    chatty enough handshake fills the buffer, blocks the child mid-write, and
    surfaces here as a deadline with no explanation. Draining it also gives
    the assertions something to print when stdout is empty.
    """
    (home / "notes").mkdir(parents=True, exist_ok=True)
    seen: dict[int, dict] = {}
    lines: list[str] = []
    errors: list[str] = []
    broke: list[tuple[str, BaseException]] = []
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # ``text=True`` alone decodes with the locale encoding — cp949 on a
        # Korean Windows box — and the server emits UTF-8, so the reader
        # would crash mid-decode and the stream come back empty. This repo
        # has already paid for that once (#759, see
        # ``test_docs_quickstart.py``). Naming the encoding also makes the
        # decode-failure path below deterministic to test.
        encoding="utf-8",
        # Not the checkout. This is not what closes the ``.env`` channel —
        # see ``_env`` for that — but nothing here needs the repo root (the
        # entry points are absolute and ``memtomem`` comes from the installed
        # package), and a probe that runs outside the tree cannot pick up
        # whatever else happens to be sitting in it.
        cwd=home,
        env=_env(home),
    )
    assert proc.stdin and proc.stdout and proc.stderr

    def _read() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                lines.append(line)
                stripped = line.strip()
                if not stripped.startswith("{"):
                    continue
                try:
                    message = json.loads(stripped)
                except json.JSONDecodeError:
                    continue
                if isinstance(message.get("id"), int):
                    seen[message["id"]] = message
                    if expect_ids <= seen.keys():
                        return
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            broke.append(("stdout", exc))

    def _drain() -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                errors.append(line)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the main thread
            broke.append(("stderr", exc))

    reader = threading.Thread(target=_read, daemon=True)
    draining = threading.Thread(target=_drain, daemon=True)
    reader.start()
    draining.start()
    try:
        for payload in payloads:
            proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.flush()
        reader.join(timeout=_DEADLINE_S)
    finally:
        proc.kill()
        proc.wait(timeout=30)
        # Both, not only the drain: after the child is killed and reaped each
        # pipe should hit EOF, so joining is what makes the captured streams
        # complete before anything reads them. An exception inside either
        # thread would otherwise vanish entirely — a daemon thread that dies
        # takes its traceback with it, and the test would report an empty
        # stream instead of the failure.
        #
        # Not done, from the same review: *reporting* that a thread is still
        # alive here, which happens when a descendant inherits stderr and
        # outlives the child. Witnessing it needs the join timeout
        # parameterised too, and a harness parameter existing only to prove a
        # diagnostic string is growth this campaign has been removing.
        #
        # An earlier version of this comment claimed such a reader "still
        # surfaces as the assertion that was already going to fail". That is
        # false and was measured: once every expected id has arrived on
        # stdout, the test passes with the stderr capture silently truncated.
        # What is given up is completeness of a diagnostic on a passing run.
        #
        # The second join is likewise unverified rather than mutation-proof:
        # a successful exchange finishes the stdout reader before it, so no
        # test distinguishes its presence. It is kept as ordering, not
        # claimed as covered.
        reader.join(timeout=30)
        draining.join(timeout=30)

    assert not broke, f"stream reader failed: {broke}"
    return _Exchange(seen, "".join(lines), "".join(errors))


def _stderr_of(argv: list[str], home: Path) -> subprocess.CompletedProcess[str]:
    """For the cases that must fail before any protocol exchange."""
    (home / "notes").mkdir(parents=True, exist_ok=True)
    return subprocess.run(
        argv,
        input="",
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=home,
        env=_env(home),
    )


class TestServeStartsTheStdioServer:
    def test_serve_answers_initialize(self, tmp_path: Path) -> None:
        """The whole point: ``serve`` reaches the server rather than argparse."""
        exchange = _talk([_console_script("memtomem"), "serve"], tmp_path, [_INITIALIZE], {1})

        assert 1 in exchange.responses, f"no initialize response.{exchange.why()}"
        assert "result" in exchange.responses[1]
        assert "capabilities" in exchange.responses[1]["result"]

    def test_serve_exposes_the_core_tools(self, tmp_path: Path) -> None:
        """A registry-driven client must find tools, not an empty server."""
        exchange = _talk(
            [_console_script("memtomem"), "serve"],
            tmp_path,
            [
                _INITIALIZE,
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            ],
            {1, 2},
        )

        assert 2 in exchange.responses, f"no tools/list response.{exchange.why()}"
        names = {tool["name"] for tool in exchange.responses[2]["result"]["tools"]}
        # Default tool mode is "core"; mem_search / mem_add are the two the
        # server's own instructions tell a client to start with.
        assert {"mem_search", "mem_add"} <= names, sorted(names)

    def test_serve_is_stdio_only(self, tmp_path: Path) -> None:
        """Network flags stay on ``memtomem-server`` — and are refused here
        by name rather than silently ignored."""
        result = _stderr_of([_console_script("memtomem"), "serve", "--transport", "http"], tmp_path)

        assert result.returncode != 0
        assert "--transport" in (result.stderr + result.stdout)

    def test_the_dedicated_entry_point_still_works(self, tmp_path: Path) -> None:
        """``memtomem-server`` is permanent, not deprecated by this command.

        Launched as the installed script, not as ``python -c "from
        memtomem.server import main"``. The import form proves the function
        exists, which was never in doubt; it cannot see a missing or
        mis-mapped ``project.scripts`` entry — the only way this entry point
        actually breaks.
        """
        exchange = _talk([_console_script("memtomem-server")], tmp_path, [_INITIALIZE], {1})

        assert 1 in exchange.responses, f"no initialize response.{exchange.why()}"
        assert "result" in exchange.responses[1]


class TestTheProbeIsSealedOffFromDeveloperConfig:
    """A ``.env`` reaches the child through the module's path, not its cwd.

    ``find_dotenv(usecwd=False)`` — what a bare ``load_dotenv()`` uses — walks
    up from the calling frame's file. Under an editable install that path runs
    through the checkout, so moving the child's cwd does not help and only the
    kill switch does. This pins that, because the isolation is invisible when
    it works and silently gone when someone tidies the env dict.
    """

    def test_the_child_env_carries_a_truthy_kill_switch(self, tmp_path: Path) -> None:
        """Presence is not enough — python-dotenv reads the value."""
        assert _env(tmp_path).get("PYTHON_DOTENV_DISABLED", "").casefold() in {
            "1",
            "true",
            "t",
            "yes",
            "y",
        }

    def test_a_dotenv_beside_the_running_module_does_not_reach_the_child(
        self, tmp_path: Path
    ) -> None:
        """Both arms, so a passing assertion cannot mean the setup was inert."""
        module_dir = tmp_path / "pkg"
        module_dir.mkdir()
        (module_dir / ".env").write_text("MEMTOMEM_WARMUP__ENABLED=true\n", encoding="utf-8")
        script = module_dir / "probe.py"
        script.write_text(
            "import json, os\n"
            "from dotenv import load_dotenv\n"
            "load_dotenv()\n"
            "print(json.dumps(os.environ.get('MEMTOMEM_WARMUP__ENABLED')))\n",
            encoding="utf-8",
        )
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()

        def _warmup_seen_by_child(env: dict[str, str]) -> str | None:
            env.pop("MEMTOMEM_WARMUP__ENABLED", None)
            result = subprocess.run(
                [sys.executable, str(script)],
                capture_output=True,
                text=True,
                timeout=120,
                # No ``.env`` here: whatever the child finds, it did not find
                # it through its working directory.
                cwd=elsewhere,
                env=env,
            )
            assert result.returncode == 0, result.stderr
            return json.loads(result.stdout.strip())

        leaky = _env(tmp_path)
        del leaky["PYTHON_DOTENV_DISABLED"]
        assert _warmup_seen_by_child(leaky) == "true", (
            "the witness is inert: the module-relative .env was not picked up even "
            "with the kill switch removed, so the sealed arm proves nothing"
        )

        # A falsy value is the realistic drift, and it is not the same as
        # removing the variable — it reads as disabled and behaves as enabled.
        falsy = _env(tmp_path)
        falsy["PYTHON_DOTENV_DISABLED"] = "0"
        assert _warmup_seen_by_child(falsy) == "true"

        assert _warmup_seen_by_child(_env(tmp_path)) is None


class TestTheFailureMessageCarriesTheEvidence:
    """The diagnostics are code too, and untested diagnostics mislead.

    ``_talk`` also asserts that neither stream reader raised. That path is
    reachable — a text-mode read of non-UTF-8 output raises, which this repo
    has hit before (see ``test_docs_quickstart.py`` on cp949) — but it cannot
    be triggered without faking the child, so it is kept as plumbing rather
    than pinned. Without it a daemon thread would die with its traceback and
    the test would report an empty stream instead.
    """

    def test_an_excerpt_keeps_the_end_where_the_error_is(self) -> None:
        """Exact slices and the exact count, not just the first and last word.

        Asserting only ``startswith`` / ``endswith`` / "omitted" would accept
        an implementation that kept five characters from each end — the shape
        of the assertion has to rule out the shape of the bug.
        """
        text = "START" + ("x" * 5000) + "Traceback (most recent call last): boom"

        excerpt = _excerpt(text, head=300, tail=500)

        assert excerpt == (
            f"{text[:300]}\n… [{len(text) - 800} characters omitted] …\n{text[-500:]}"
        )
        assert text[-500:] in excerpt, "the tail is the half worth keeping"

    def test_a_short_stream_is_not_cut_at_all(self) -> None:
        assert _excerpt("short", head=300, tail=500) == "short"

    def test_both_streams_reach_the_failure_message(self) -> None:
        exchange = _Exchange({}, "out-side", "err-side")

        assert "out-side" in exchange.why()
        assert "err-side" in exchange.why()

    def test_a_reader_that_dies_is_re_raised_not_swallowed(self, tmp_path: Path) -> None:
        """The decode-failure path, witnessed by a child that provokes it.

        A daemon thread that raises takes its traceback with it, and the
        exchange would come back with an empty stream — reported as "the
        server said nothing" when the truth is that the harness could not read
        what it said. Review round 2 was right that unconditional execution
        proves nothing about this path, and right that a controlled child
        reaches it without parameterising anything: ``encoding="utf-8"``
        makes the decode strict and deterministic, so bytes that are not
        UTF-8 raise inside the reader on every platform.
        """
        child = (
            "import sys\n"
            "sys.stderr.buffer.write(b'\\xff\\xfe not utf-8 \\x80\\x81\\n')\n"
            "sys.stderr.buffer.flush()\n"
        )

        with pytest.raises(AssertionError, match="stream reader failed"):
            _talk([sys.executable, "-c", child], tmp_path, [], set())
