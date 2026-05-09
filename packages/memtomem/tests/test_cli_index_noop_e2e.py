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
import shutil
import subprocess
import sys

import pytest
from click.testing import CliRunner

from memtomem.cli import cli

from .helpers import set_home


def _make_memory_dir(home: str) -> str:
    mem_dir = os.path.join(home, "memories")
    os.makedirs(mem_dir, exist_ok=True)
    with open(os.path.join(mem_dir, "test.md"), "w", encoding="utf-8") as f:
        f.write("# memo\n\nhello world this is a bm25 smoke test\n")
    return mem_dir


class TestFreshNoopIndexInline:
    def test_init_index_search_via_cli_runner(self, tmp_path, monkeypatch):
        """``CliRunner`` round-trip: init → index → search must all succeed.

        Three-layer isolation needed in-process:

        1. ``HOME`` env override — caught by ``Path.home()`` calls that run
           inside command functions (e.g. ``init_cmd.py`` config writer).
        2. Patch ``_bootstrap._CONFIG_PATH`` — that module-level constant is
           bound at import time, so ``monkeypatch.setenv`` alone leaves the
           ``cli_components`` existence check pointing at the real home.
           Previously masked locally by a pre-existing real ``~/.memtomem/
           config.json`` but exposed in CI (no leaked state).
        3. Strip ``MEMTOMEM_*`` env overrides — pydantic-settings binds
           any ``MEMTOMEM_<SECTION>__<KEY>`` from the parent shell into
           the freshly built config, so a developer's
           ``MEMTOMEM_SEARCH__ENABLE_BM25=false`` (or any indexing/storage
           override) leaks into the test and the search assertion below
           comes back as ``0 BM25 + 0 dense → 0 results``. Filter on the
           full prefix so a future config section is covered automatically.
        """
        from memtomem.cli import _bootstrap

        for var in [k for k in os.environ if k.startswith("MEMTOMEM_")]:
            monkeypatch.delenv(var, raising=False)

        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)
        monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", home / ".memtomem" / "config.json")

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
        # ``shutil.which`` adds the platform-correct suffix (``.exe`` on
        # Windows via PATHEXT, none on POSIX), so the same lookup works
        # against both ``.venv/bin/mm`` and ``.venv/Scripts/mm.exe``.
        bin_dir = os.path.dirname(sys.executable)
        mm_bin = shutil.which("mm", path=bin_dir)
        # Fail loudly instead of pytest.skip — any valid test environment
        # (``uv run pytest`` or ``uv pip install -e``) must provide the
        # ``mm`` entry point. A silent skip here would turn this subprocess
        # regression guard into CI false-green if the editable install is
        # ever dropped.
        if mm_bin is None:
            pytest.fail(
                f"mm binary not found in {bin_dir}. "
                "Run `uv pip install -e packages/memtomem[all]` before testing."
            )

        home = tmp_path / "home"
        home.mkdir()
        mem_dir = _make_memory_dir(str(home))

        env = os.environ.copy()
        # Strip developer ``MEMTOMEM_*`` overrides — ``HOME`` only
        # isolates ``~/.memtomem/config.json`` reads, but
        # pydantic-settings still applies env-var overrides from the
        # parent shell (e.g. ``MEMTOMEM_INDEXING__MEMORY_DIRS``
        # pointing at a real memory dir, or
        # ``MEMTOMEM_SEARCH__ENABLE_BM25=false`` disabling the BM25
        # path the assertions below rely on) which would
        # un-hermeticize the subprocess. Filter on the full
        # ``MEMTOMEM_`` prefix rather than a hand-curated list so any
        # new top-level config section's env binding is covered
        # automatically.
        for var in [k for k in env if k.startswith("MEMTOMEM_")]:
            env.pop(var, None)
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)  # Windows ``Path.home()`` priority
        env["XDG_CONFIG_HOME"] = str(home / ".config")

        def _run(*args: str) -> subprocess.CompletedProcess:
            # ``encoding="utf-8"`` is required: ``text=True`` alone falls
            # back to ``locale.getpreferredencoding(False)``, which is
            # ``cp949`` on Korean Windows. The CLI emits UTF-8 (em-dashes,
            # box-drawing) so the reader thread crashes mid-decode and
            # ``r.stdout`` / ``r.stderr`` come back as ``None``, surfacing
            # later as ``"argument of type 'NoneType' is not iterable"``
            # on the assertion below (#759).
            return subprocess.run(
                [mm_bin, *args],
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
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
