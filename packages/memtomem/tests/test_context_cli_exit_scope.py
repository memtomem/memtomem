"""Regression pins for ``mm context`` CLI exit-code & scope consistency (#1123 B2).

Each test maps to one audit finding:

- B2-1: ``generate --agent <bogus>`` must exit non-zero (was: red line, exit 0).
- B2-2: ``diff --include=settings`` must honour ``--scope`` (was: ignored).
- B2-3: an unreadable detected agent file must yield a clean CLI error, not a
  raw traceback, in ``diff`` and ``init``.
- B2-4: the ``migrate --force`` help must not claim "no effect" with ``--to``
  (the code hard-rejects that combination).
- B2-5: ``memory-migrate`` must reject an explicit non-``.md`` source, matching
  the glob branch's filter and the documented ``.md files`` contract.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli.context_cmd import migrate_cmd
from memtomem.context.generator import GENERATORS

from .helpers import set_home


def _seed_project(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "b2-test-project"\nversion = "0"\n',
        encoding="utf-8",
    )


def _ctx_with_sections(root: Path) -> None:
    """Write a minimal valid .memtomem/context.md under ``root``."""
    ctx = root / ".memtomem" / "context.md"
    ctx.parent.mkdir(parents=True, exist_ok=True)
    ctx.write_text(
        "# Project Context\n\n## Project\n- Name: b2\n\n## Rules\n- keep\n",
        encoding="utf-8",
    )


def _runner_in_project(tmp_path, monkeypatch):
    from memtomem.cli import _bootstrap

    home = tmp_path / "home"
    home.mkdir()
    project = tmp_path / "project"
    project.mkdir()
    _seed_project(project)
    set_home(monkeypatch, home)
    monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", home / ".memtomem" / "config.json")
    monkeypatch.chdir(project)
    return CliRunner(), project


class TestGenerateUnknownAgentExitCode:
    def test_bogus_agent_exits_nonzero(self, tmp_path, monkeypatch):
        runner, project = _runner_in_project(tmp_path, monkeypatch)
        _ctx_with_sections(project)

        r = runner.invoke(cli, ["context", "generate", "--agent", "nope"])
        assert r.exit_code != 0, f"expected failure, got 0: {r.output}"
        assert "Unknown agent: nope" in r.output
        # No project memory file should have been written for the typo.
        for gen in GENERATORS.values():
            assert not (project / gen.output_path).exists()

    def test_all_still_succeeds(self, tmp_path, monkeypatch):
        runner, project = _runner_in_project(tmp_path, monkeypatch)
        _ctx_with_sections(project)

        r = runner.invoke(cli, ["context", "generate", "--agent", "all"])
        assert r.exit_code == 0, f"--agent all should succeed: {r.output}"


class TestDiffSettingsHonoursScope:
    def test_diff_settings_threads_scope(self, tmp_path, monkeypatch):
        """``diff --include=settings --scope=user`` must pass the chosen scope
        to the settings differ rather than silently resolving the default."""
        import memtomem.cli.context_cmd as ctx_cmd

        runner, project = _runner_in_project(tmp_path, monkeypatch)
        _ctx_with_sections(project)

        seen: dict[str, object] = {}

        def _fake_settings_diff(root, scope):
            seen["scope"] = scope

        monkeypatch.setattr(ctx_cmd, "_print_settings_diff", _fake_settings_diff)

        r = runner.invoke(
            cli,
            ["context", "diff", "--include=settings", "--scope", "user"],
        )
        assert r.exit_code == 0, r.output
        assert seen.get("scope") == "user", (
            f"settings diff got scope={seen.get('scope')!r}, expected 'user'"
        )


class TestDiffUnreadableAgentFile:
    def test_unreadable_agent_file_is_clean_error(self, tmp_path, monkeypatch):
        """A detected agent file that cannot be decoded yields a ClickException
        (exit 1, friendly message) rather than a raw UnicodeDecodeError."""
        runner, project = _runner_in_project(tmp_path, monkeypatch)
        _ctx_with_sections(project)
        # CLAUDE.md is detected, but contains invalid UTF-8.
        (project / "CLAUDE.md").write_bytes(b"\xff\xfe\x00not utf-8\x80")

        r = runner.invoke(cli, ["context", "diff"])
        assert r.exit_code != 0
        assert "Could not read" in r.output
        # Must be the handled error, not a leaked traceback.
        assert "Traceback" not in r.output


class TestMigrateForceHelp:
    def test_force_help_does_not_claim_no_effect(self):
        """The ``--force`` help must not say "no effect" with ``--to`` — the
        code raises UsageError for that combination, so the docs must match."""
        force_opt = next(p for p in migrate_cmd.params if p.name == "force")
        help_text = (force_opt.help or "").lower()
        assert "no effect" not in help_text
        assert "--to" in (force_opt.help or "")


class TestMemoryMigrateRejectsNonMarkdownSource:
    def test_explicit_txt_source_rejected(self, tmp_path):
        """An explicit single-file source must be .md (B2-5)."""
        from memtomem.cli.context_cmd import _resolve_memory_migrate_sources
        import click
        import pytest

        src = tmp_path / "notes.txt"
        src.write_text("not markdown", encoding="utf-8")

        with pytest.raises(click.ClickException) as exc:
            _resolve_memory_migrate_sources(str(src))
        assert ".md" in str(exc.value)

    def test_explicit_md_source_accepted(self, tmp_path):
        from memtomem.cli.context_cmd import _resolve_memory_migrate_sources

        src = tmp_path / "notes.md"
        src.write_text("# markdown", encoding="utf-8")

        result = _resolve_memory_migrate_sources(str(src))
        assert result == [src.resolve()]
