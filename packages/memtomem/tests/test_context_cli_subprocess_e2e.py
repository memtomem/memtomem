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
from memtomem.context.generator import GENERATORS

from .helpers import _MEMTOMEM_ENV_VARS, set_home

# Derived from the runtime registry rather than hand-curated so a new
# runtime added to ``GENERATORS`` (cline, aider, etc.) is automatically
# covered without this list silently under-asserting.
_GENERATED_PROJECT_FILES = tuple(g.output_path for g in GENERATORS.values())


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

        # 2. generate → every registered runtime's project memory
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


class TestContextVersionCliInline:
    """In-process round-trip over ``mm context version`` + ``sync --label``
    (ADR-0022). The pure module is covered in ``test_context_versioning.py``;
    this pins the CLI surface and the edit/deploy split end-to-end."""

    _AGENT = "---\nname: my-agent\ndescription: d\n---\n\nMARKER {m}\n"

    def _dir_agent(self, project: Path, marker: str) -> Path:
        adir = project / ".memtomem" / "agents" / "my-agent"
        adir.mkdir(parents=True, exist_ok=True)
        working = adir / "agent.md"
        working.write_text(self._AGENT.format(m=marker), encoding="utf-8")
        return working

    def test_version_create_promote_sync_roundtrip(self, tmp_path, monkeypatch):
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

        working = self._dir_agent(project, marker="A")

        # 1. create → versions/v1.md
        r = runner.invoke(cli, ["context", "version", "create", "agents", "my-agent"])
        assert r.exit_code == 0, r.output
        assert (project / ".memtomem/agents/my-agent/versions/v1.md").is_file()

        # 2. working file moves on to B
        working.write_text(self._AGENT.format(m="B"), encoding="utf-8")

        # 3. promote production → v1
        r = runner.invoke(
            cli,
            [
                "context",
                "version",
                "promote",
                "agents",
                "my-agent",
                "--to",
                "production",
                "--version",
                "v1",
            ],
        )
        assert r.exit_code == 0, r.output

        # 3b. a version-shaped label name is rejected cleanly (would be
        #     shadowed by version v1 in the resolver).
        r = runner.invoke(
            cli,
            [
                "context",
                "version",
                "promote",
                "agents",
                "my-agent",
                "--to",
                "v1",
                "--version",
                "v1",
            ],
        )
        assert r.exit_code != 0
        assert "version tag" in r.output.lower()

        # 4. list shows v1 + the production pointer
        r = runner.invoke(cli, ["context", "version", "list", "agents", "my-agent"])
        assert r.exit_code == 0, r.output
        assert "v1" in r.output and "production" in r.output

        # 5. sync --label production → frozen v1 (marker A) fans out
        r = runner.invoke(cli, ["context", "sync", "--include=agents", "--label", "production"])
        assert r.exit_code == 0, r.output
        out = (project / ".claude/agents/my-agent.md").read_text(encoding="utf-8")
        assert "MARKER A" in out and "MARKER B" not in out

        # 6. backward-compat: no --label syncs the working file (marker B)
        r = runner.invoke(cli, ["context", "sync", "--include=agents"])
        assert r.exit_code == 0, r.output
        out = (project / ".claude/agents/my-agent.md").read_text(encoding="utf-8")
        assert "MARKER B" in out

    def test_version_create_on_flat_layout_errors(self, tmp_path, monkeypatch):
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

        # Flat-layout agent (no per-artifact directory).
        flat_root = project / ".memtomem" / "agents"
        flat_root.mkdir(parents=True, exist_ok=True)
        (flat_root / "flat-agent.md").write_text(self._AGENT.format(m="A"), encoding="utf-8")

        r = runner.invoke(cli, ["context", "version", "create", "agents", "flat-agent"])
        assert r.exit_code != 0
        assert "migrate" in r.output.lower()


class TestContextInitGenerateSubprocess:
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
        # Strip developer ``MEMTOMEM_*`` overrides — ``HOME`` only
        # isolates ``~/.memtomem/config.json`` reads, but
        # pydantic-settings still applies env-var overrides from the
        # parent shell (e.g. ``MEMTOMEM_INDEXING__MEMORY_DIRS``
        # pointing at a real path) which would un-hermeticize the
        # subprocess. Mirrors what ``helpers.isolate_memtomem_env``
        # does for in-process tests.
        for var in _MEMTOMEM_ENV_VARS:
            env.pop(var, None)
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
