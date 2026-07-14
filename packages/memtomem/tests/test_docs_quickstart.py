"""Runnable contract for the public documentation's first-success path.

The Quickstart in the public READMEs and Getting Started guide promises a
fresh user can ``mm init`` → ``mm status`` → ``mm add`` → ``mm search`` and
find the memory back, with no existing notes directory or connected editor.

This runs that exact journey through the installed ``mm`` entry point in a
**subprocess**, not in-process via ``CliRunner``. Module-level constants
like ``_bootstrap._CONFIG_PATH`` are bound at import time to the real
``HOME``, so an in-process invocation reuses whatever ``HOME`` the test
session imported under and cannot honor a per-test ``monkeypatch.setenv``
— exactly the process-boundary gap ``test_context_cli_subprocess_e2e.py``
documents (see #759). The subprocess re-resolves ``HOME`` / ``USERPROFILE``
/ ``XDG_CONFIG_HOME`` on its own, so the round trip is genuinely isolated.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from .helpers import _MEMTOMEM_ENV_VARS


def test_documented_quickstart_round_trip(tmp_path: Path) -> None:
    """A fresh user can save and find one memory without existing notes."""
    # ``shutil.which`` adds the platform-correct suffix (``.exe`` on Windows
    # via PATHEXT, none on POSIX), matching both ``.venv/bin/mm`` and
    # ``.venv/Scripts/mm.exe``.
    bin_dir = os.path.dirname(sys.executable)
    mm_bin = shutil.which("mm", path=bin_dir)
    # Fail loudly instead of pytest.skip — any valid test environment
    # (``uv run pytest`` or ``uv pip install -e``) provides the ``mm`` entry
    # point. A silent skip would turn this first-success guard into a CI
    # false-green if the editable install is ever dropped.
    if mm_bin is None:
        pytest.fail(
            f"mm binary not found in {bin_dir}. "
            "Run `uv pip install -e packages/memtomem[all]` before testing."
        )

    home = tmp_path / "home"
    home.mkdir()

    env = os.environ.copy()
    # ``HOME`` only isolates ``~/.memtomem/config.json`` reads; strip
    # developer ``MEMTOMEM_*`` overrides so pydantic-settings does not
    # re-point the subprocess at a real path (mirrors the subprocess e2e
    # tests and ``helpers.isolate_memtomem_env``).
    for var in _MEMTOMEM_ENV_VARS:
        env.pop(var, None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)  # Windows ``Path.home()`` priority
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    env["XDG_DATA_HOME"] = str(home / ".local" / "share")

    def _run(*args: str) -> subprocess.CompletedProcess:
        # ``encoding="utf-8"`` is required: ``text=True`` alone falls back to
        # ``locale.getpreferredencoding(False)`` (``cp949`` on Korean
        # Windows). The CLI emits UTF-8, so the reader would crash mid-decode
        # and ``stdout`` come back ``None`` (#759).
        return subprocess.run(
            [mm_bin, *args],
            env=env,
            cwd=str(home),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    def _status_json(result: subprocess.CompletedProcess) -> dict:
        assert result.returncode == 0, (
            f"status failed:\nstdout={result.stdout}\nstderr={result.stderr}"
        )
        payload = json.loads(result.stdout)
        # The report body must be present as well as the successful exit code.
        assert "error" not in payload, f"status returned an error shape: {payload}"
        return payload

    init = _run("init", "--non-interactive", "--preset", "minimal", "--mcp", "skip")
    assert init.returncode == 0, f"init failed:\nstdout={init.stdout}\nstderr={init.stderr}"

    empty = _status_json(_run("status", "--json"))
    assert empty["index"]["total_chunks"] == 0
    assert empty["config"]["db_path"] == str((home / ".memtomem" / "memtomem.db").resolve())

    sentence = "Deployment checklist uses blue-green rollout"
    add = _run("add", sentence, "--tags", "ops", "--json")
    assert add.returncode == 0, f"add failed:\nstdout={add.stdout}\nstderr={add.stderr}"
    add_payload = json.loads(add.stdout)
    assert add_payload["ok"] is True, add_payload
    assert add_payload["chunks"] == 1, add_payload

    search = _run("search", "blue-green", "--format", "plain")
    assert search.returncode == 0, f"search failed:\nstdout={search.stdout}\nstderr={search.stderr}"
    assert sentence in search.stdout, search.stdout

    populated = _status_json(_run("status", "--json"))
    assert populated["index"]["total_chunks"] >= 1
