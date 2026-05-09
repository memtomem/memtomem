"""End-to-end pins for ``mm context`` subcommands across the process boundary.

The bulk of context-gateway coverage lives in unit/integration tests
(``test_context_settings.py``, ``test_context_agents.py``,
``test_context_install.py`` etc.), but those run in-process via
``CliRunner``. Module-level constants like ``_bootstrap._CONFIG_PATH``
are bound at import time, so an in-process invocation can mask
regressions that only surface across the ``HOME`` / ``Path.home()`` /
cwd boundary (see #759 for an analogous case in ``mm index``).

This module mirrors the inline + subprocess split from
``test_cli_index_noop_e2e.py``: the inline class catches behavioral
regressions cheaply, and the subprocess class re-runs the same scenario
against the installed ``mm`` entry point so process-boundary plumbing
(env var resolution, cwd-anchored ``_find_project_root()``, lazy
imports) is covered.

The host-write invariant (``mm context generate`` without
``--include=settings`` MUST NOT touch the fake ``HOME``'s
``~/.claude/settings.json`` or ``~/.codex/agents/``) is asserted in
both modes — the central guard for the "context-gateway sync
user-scope pollution" memo.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli

from .helpers import set_home

_GENERATED_PROJECT_FILES = (
    "CLAUDE.md",
    ".cursorrules",
    "GEMINI.md",
    "AGENTS.md",
    ".github/copilot-instructions.md",
)


def _seed_project(root: Path) -> None:
    """Make ``root`` look like a memtomem-eligible project root.

    ``_find_project_root()`` (``cli/context_cmd.py:87``) walks up from
    cwd looking for ``.git`` or ``pyproject.toml``; a bare ``tmp_path``
    has neither, and the walk would escape into the developer's real
    repo. A minimal ``pyproject.toml`` is the cheapest marker.
    """
    (root / "pyproject.toml").write_text(
        '[project]\nname = "e2e-test-project"\nversion = "0"\n',
        encoding="utf-8",
    )


class TestContextInitGenerateInline:
    """In-process ``CliRunner`` round-trip over the empty-project flow.

    Pinpoints behavioral regressions cheaply; complements the subprocess
    class below which catches process-boundary regressions.
    """

    def test_init_then_generate_then_detect_then_status(self, tmp_path, monkeypatch):
        from memtomem.cli import _bootstrap

        home = tmp_path / "home"
        home.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        _seed_project(project)

        set_home(monkeypatch, home)
        monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", home / ".memtomem" / "config.json")
        monkeypatch.chdir(project)

        runner = CliRunner()

        # 1. init → empty template (no agent files exist yet)
        r = runner.invoke(cli, ["context", "init"])
        assert r.exit_code == 0, f"init failed: {r.output}"
        assert (project / ".memtomem" / "context.md").is_file()

        # 2. generate → 5 runtimes' worth of project memory
        r = runner.invoke(cli, ["context", "generate"])
        assert r.exit_code == 0, f"generate failed: {r.output}"
        for rel in _GENERATED_PROJECT_FILES:
            assert (project / rel).is_file(), f"missing {rel}\noutput={r.output}"

        # 3. detect → agent files now present
        r = runner.invoke(cli, ["context", "detect"])
        assert r.exit_code == 0, f"detect failed: {r.output}"
        assert "agent file(s)" in r.output

        # 4. status → no wiki assets installed; exit 0
        r = runner.invoke(cli, ["context", "status"])
        assert r.exit_code == 0, f"status failed: {r.output}"

        # 5. Host-write invariant — fake HOME must remain untouched
        #    when --include=settings is not passed. If this ever flips
        #    to "settings sync runs by default", the failure here is
        #    the loud signal we want.
        assert not (home / ".claude" / "settings.json").exists()
        assert not (home / ".codex" / "agents").exists()


class TestContextSubprocess:
    """Out-of-process ``mm`` round-trip — process-boundary regressions
    that the in-process variant cannot surface (HOME / USERPROFILE /
    cwd / module-level constants bound at import time)."""

    def test_init_generate_detect_status_via_mm_binary(self, tmp_path):
        # ``shutil.which`` adds the platform-correct suffix (``.exe`` on
        # Windows via PATHEXT, none on POSIX), so the same lookup works
        # against both ``.venv/bin/mm`` and ``.venv/Scripts/mm.exe``.
        bin_dir = os.path.dirname(sys.executable)
        mm_bin = shutil.which("mm", path=bin_dir)
        # Fail loudly instead of pytest.skip — any valid test environment
        # (``uv run pytest`` or ``uv pip install -e``) provides the
        # ``mm`` entry point. A silent skip would turn this subprocess
        # regression guard into CI false-green if the editable install
        # is ever dropped.
        if mm_bin is None:
            pytest.fail(
                f"mm binary not found in {bin_dir}. "
                "Run `uv pip install -e packages/memtomem[all]` before testing."
            )

        home = tmp_path / "home"
        home.mkdir()
        project = tmp_path / "project"
        project.mkdir()
        _seed_project(project)

        env = os.environ.copy()
        env["HOME"] = str(home)
        env["USERPROFILE"] = str(home)  # Windows ``Path.home()`` priority
        env["XDG_CONFIG_HOME"] = str(home / ".config")

        def _run(*args: str) -> subprocess.CompletedProcess:
            # ``encoding="utf-8"`` is required: ``text=True`` alone falls
            # back to ``locale.getpreferredencoding(False)``, which is
            # ``cp949`` on Korean Windows. The CLI emits UTF-8 (em-dashes,
            # box-drawing) so the reader thread crashes mid-decode and
            # ``r.stdout`` / ``r.stderr`` come back as ``None`` (#759).
            return subprocess.run(
                [mm_bin, *args],
                env=env,
                cwd=str(project),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=60,
            )

        r = _run("context", "init")
        assert r.returncode == 0, f"init failed:\nstdout={r.stdout}\nstderr={r.stderr}"
        assert (project / ".memtomem" / "context.md").is_file()

        r = _run("context", "generate")
        assert r.returncode == 0, f"generate failed:\nstdout={r.stdout}\nstderr={r.stderr}"
        for rel in _GENERATED_PROJECT_FILES:
            assert (project / rel).is_file(), f"missing {rel}\nstdout={r.stdout}"

        r = _run("context", "detect")
        assert r.returncode == 0, f"detect failed:\nstdout={r.stdout}\nstderr={r.stderr}"
        assert "agent file(s)" in r.stdout

        r = _run("context", "status")
        assert r.returncode == 0, f"status failed:\nstdout={r.stdout}\nstderr={r.stderr}"

        # Host-write invariant — same as the inline class. Subprocess
        # mode is what catches a ``Path.home()`` call that bypasses
        # ``HOME`` env on Windows (``USERPROFILE``-priority), or a
        # module-level path constant bound before the env override.
        assert not (home / ".claude" / "settings.json").exists()
        assert not (home / ".codex" / "agents").exists()
