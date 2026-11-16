"""The light entry modules of #3381: what they import, and where they hand over.

A stdio bridge to a shared daemon only pays off if the process that runs it
never imports :mod:`memtomem.server`, which registers every MCP tool. These
tests run each light path in a fresh interpreter and read ``sys.modules``
afterwards, because an import made by an earlier test in this process would
hide one made by the path under test.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

import memtomem

HEAVY = ("memtomem.server", "mcp")

# Last line of every probe: which heavy modules the child imported, and which
# ``memtomem`` it imported them from.
_REPORT = """
import json, sys
import memtomem
print(json.dumps({
    "heavy": sorted(m for m in %r if m in sys.modules),
    "origin": memtomem.__file__,
}))
""" % (HEAVY,)

# The ``memtomem`` this test process imported: the child must import the same
# one, whatever its cwd. A relative ``PYTHONPATH`` would otherwise resolve
# against the child's cwd and silently test another checkout.
_SOURCE_ROOT = str(Path(memtomem.__file__).resolve().parents[1])


def _run(code: str, tmp_path: Path, *, as_file: bool = True, dotenv: bool = False) -> dict:
    """Run ``code`` in a fresh interpreter with a scratch HOME; return its last JSON line.

    python-dotenv stays disabled unless ``dotenv`` is set: its search walks up
    from the package directory, so a developer's ``~/.env`` above the checkout
    would reach the child whatever HOME says. The probes that enable it never
    let the search walk a real directory.
    """
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "MEMTOMEM_STORAGE__SQLITE_PATH": str(tmp_path / "store.db"),
        "PYTHONPATH": os.pathsep.join(
            [_SOURCE_ROOT, *filter(None, os.environ.get("PYTHONPATH", "").split(os.pathsep))]
        ),
        "PYTHON_DOTENV_DISABLED": "1",
    }
    if dotenv:
        env.pop("PYTHON_DOTENV_DISABLED")
    body = textwrap.dedent(code) + _REPORT
    if as_file:
        script = tmp_path / "probe.py"
        script.write_text(body, encoding="utf-8")
        argv = [sys.executable, str(script)]
    else:
        argv = [sys.executable, "-c", body]
    done = subprocess.run(
        argv, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=60, check=False
    )
    assert done.returncode == 0, done.stderr
    lines = [json.loads(line) for line in done.stdout.splitlines() if line.startswith("{")]
    report = lines[-1]
    assert Path(report["origin"]).resolve() == Path(memtomem.__file__).resolve()
    return {k: v for line in lines for k, v in line.items()}


def test_store_resolution_and_dotenv_do_not_import_the_server(tmp_path: Path) -> None:
    """Everything the bridge needs before it connects, run for real."""
    result = _run(
        """
        from memtomem import _dotenv, _server_entry, _store_identity

        _dotenv.load_dotenv()
        _server_entry.parse_server_args(["--transport", "stdio"])
        path = _store_identity.configured_store_path()
        assert path is not None
        _store_identity.resolve_shared_store(path)
        """,
        tmp_path,
    )
    assert result["heavy"] == []


def test_loading_a_dotenv_does_not_import_the_server(tmp_path: Path) -> None:
    """The enabled path, parsing included, against a scratch ``.env`` only."""
    dotenv_file = tmp_path / "scratch.env"
    dotenv_file.write_text("MEMTOMEM_T3381_PROBE=loaded\n", encoding="utf-8")
    result = _run(
        f"""
        import json, os
        import dotenv
        from memtomem import _dotenv

        dotenv.find_dotenv = lambda *a, **k: {str(dotenv_file)!r}
        _dotenv.load_dotenv()
        print(json.dumps({{"probe": os.environ.get("MEMTOMEM_T3381_PROBE")}}))
        """,
        tmp_path,
        dotenv=True,
    )
    assert result["probe"] == "loaded"
    assert result["heavy"] == []


def test_help_does_not_import_the_server(tmp_path: Path) -> None:
    result = _run(
        """
        import contextlib, io
        from memtomem._server_entry import main

        with contextlib.redirect_stdout(io.StringIO()):
            try:
                main(["--help"])
            except SystemExit as exc:
                assert exc.code == 0
        """,
        tmp_path,
    )
    assert result["heavy"] == []


def test_the_witness_sees_a_server_import(tmp_path: Path) -> None:
    """The probe can fail: importing the server shows up in its report."""
    result = _run("import memtomem.server\n", tmp_path)
    assert result["heavy"] == sorted(HEAVY)


_DOTENV_START = """
import json
import dotenv.main
from memtomem import _dotenv

seen = []

def spy(path):
    seen.append(path)
    return iter(())  # record the start, walk nothing real

dotenv.main._walk_to_root = spy
_dotenv.load_dotenv()
print(json.dumps({"start": seen[0] if seen else None}))
"""


def test_dotenv_search_starts_at_the_package_when_run_from_a_file(tmp_path: Path) -> None:
    """The installed package, not the cwd: the frame python-dotenv inspects is ``_dotenv``."""
    result = _run(_DOTENV_START, tmp_path, dotenv=True)
    assert Path(result["start"]) == Path(memtomem.__file__).resolve().parent


def test_dotenv_search_keeps_python_dotenv_cwd_fallback(tmp_path: Path) -> None:
    """With no ``__main__.__file__`` python-dotenv searches from the cwd, as before."""
    result = _run(_DOTENV_START, tmp_path, as_file=False, dotenv=True)
    assert Path(result["start"]).resolve() == tmp_path.resolve()


class TestEveryEntryHandsOverToRunStandalone:
    """One parser for every way of starting a server."""

    @pytest.fixture
    def calls(self, monkeypatch: pytest.MonkeyPatch) -> list:
        import memtomem.server as server_mod

        seen: list = []
        monkeypatch.setattr(server_mod, "run_standalone", seen.append)
        return seen

    def test_entry_main(self, calls: list) -> None:
        from memtomem._server_entry import main

        main(["--transport", "http", "--port", "9123"])

        assert [(a.transport, a.port) for a in calls] == [("http", 9123)]

    def test_server_main(self, calls: list) -> None:
        import memtomem.server as server_mod

        server_mod.main(["--transport", "sse"])

        assert [a.transport for a in calls] == ["sse"]

    def test_mm_serve(self, calls: list) -> None:
        from click.testing import CliRunner

        from memtomem.cli import cli

        result = CliRunner().invoke(cli, ["serve"])

        assert result.exit_code == 0, result.output
        assert [a.transport for a in calls] == ["stdio"]

    def test_bad_option_exits_before_the_server(self, calls: list) -> None:
        from memtomem._server_entry import main

        with pytest.raises(SystemExit) as exc:
            main(["--transport", "carrier-pigeon"])

        assert exc.value.code == 2
        assert calls == []


def test_mm_serve_reaches_the_dispatcher_without_the_server(tmp_path: Path) -> None:
    """``mm serve`` must not import the server on its own way to the dispatcher."""
    result = _run(
        """
        import json
        import memtomem._server_entry as entry

        entry.main = lambda argv: print(json.dumps({"argv": argv}))
        from memtomem.cli import cli

        try:
            cli(["serve"])
        except SystemExit as exc:
            assert exc.code == 0
        """,
        tmp_path,
    )
    assert result["argv"] == []
    assert result["heavy"] == []


def test_console_script_points_at_the_light_entry() -> None:
    """``pyproject.toml`` is the mapping CI installs; the e2e tests launch it."""
    import tomllib

    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    scripts = tomllib.loads(pyproject.read_text(encoding="utf-8"))["project"]["scripts"]

    assert scripts["memtomem-server"] == "memtomem._server_entry:main"
