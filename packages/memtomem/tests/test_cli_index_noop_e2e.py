"""End-to-end pins: fresh ``mm init --provider none`` → ``mm index`` → search.

Before the fix, a fresh ``--provider none`` install did not create the
``chunks_vec`` virtual table (NoopEmbedder dim=0), and every subsequent
``upsert_chunks`` crashed with ``no such table: chunks_vec``. These tests
exercise the whole user journey end-to-end so any regression of the
unconditional write paths is caught immediately.

Two variants are kept intentionally:

* **inline** — ``CliRunner`` invocations share the process, so the fix
  is observed directly without subprocess overhead.
* **subprocess** — ``sys.executable -m memtomem`` round-trip covers the
  process boundary (``HOME`` / ``XDG_CONFIG_HOME`` / CWD plumbing) that
  in-process tests can't surface.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest
from click.testing import CliRunner

from memtomem.cli import cli


def _make_memory_dir(home: str) -> str:
    mem_dir = os.path.join(home, "memories")
    os.makedirs(mem_dir, exist_ok=True)
    with open(os.path.join(mem_dir, "test.md"), "w", encoding="utf-8") as f:
        f.write("# memo\n\nhello world this is a bm25 smoke test\n")
    return mem_dir


class TestFreshNoopIndexInline:
    def test_init_index_search_via_cli_runner(self, tmp_path, monkeypatch):
        """``CliRunner`` round-trip: init → index → search must all succeed."""
        home = tmp_path / "home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))

        mem_dir = _make_memory_dir(str(home))

        runner = CliRunner()

        r = runner.invoke(
            cli,
            [
                "init",
                "-y",
                "--provider",
                "none",
                "--memory-dir",
                mem_dir,
                "--mcp",
                "skip",
            ],
        )
        assert r.exit_code == 0, f"init failed: {r.output}"

        r = runner.invoke(cli, ["index", mem_dir])
        assert r.exit_code == 0, f"index failed: {r.output}"
        # Pre-fix: "no such table: chunks_vec". Post-fix: "1 new".
        assert "no such table" not in r.output
        assert "1 new" in r.output or "1 file" in r.output

        r = runner.invoke(cli, ["search", "hello"])
        assert r.exit_code == 0, f"search failed: {r.output}"
        assert "hello world" in r.output


class TestFreshNoopIndexSubprocess:
    def test_init_index_search_via_subprocess(self, tmp_path):
        """Out-of-process variant: catches regressions that only manifest
        across the HOME / XDG boundary (e.g. config.json path resolution).

        Uses the ``mm`` script installed by ``uv pip install -e`` (co-located
        with ``sys.executable``) rather than ``python -m memtomem`` — the
        package has no ``__main__`` module, and the installed entry point is
        what real users hit.
        """
        mm_bin = os.path.join(os.path.dirname(sys.executable), "mm")
        # Fail loudly instead of pytest.skip — any valid test environment
        # (``uv run pytest`` or ``uv pip install -e``) must provide the
        # ``mm`` entry point. A silent skip here would turn this subprocess
        # regression guard into CI false-green if the editable install is
        # ever dropped.
        if not os.path.exists(mm_bin):
            pytest.fail(
                f"mm binary not found at {mm_bin}. "
                "Run `uv pip install -e packages/memtomem[all]` before testing."
            )

        home = tmp_path / "home"
        home.mkdir()
        mem_dir = _make_memory_dir(str(home))

        env = os.environ.copy()
        env["HOME"] = str(home)
        env["XDG_CONFIG_HOME"] = str(home / ".config")

        def _run(*args: str) -> subprocess.CompletedProcess:
            return subprocess.run(
                [mm_bin, *args],
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )

        r = _run(
            "init",
            "-y",
            "--provider",
            "none",
            "--memory-dir",
            mem_dir,
            "--mcp",
            "skip",
        )
        assert r.returncode == 0, f"init failed:\nstdout={r.stdout}\nstderr={r.stderr}"

        r = _run("index", mem_dir)
        assert r.returncode == 0, f"index failed:\nstdout={r.stdout}\nstderr={r.stderr}"
        assert "no such table" not in (r.stdout + r.stderr)
        assert "1 new" in r.stdout or "1 file" in r.stdout

        r = _run("search", "hello")
        assert r.returncode == 0, f"search failed:\nstdout={r.stdout}\nstderr={r.stderr}"
        assert "hello world" in r.stdout
