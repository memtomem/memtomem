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

Later additions (2026-07-02 cli-surface review):

- A missing ``.memtomem/context.md`` refusal in the *mutating* commands
  (``generate`` / single-project ``sync``) must exit non-zero — it used to
  print a red line and still exit 0, so a script/CI wrapping the command
  could not detect the failure.
- The ``sync`` one-line summary and the ``seed-validation --force`` help must
  match what the code actually does (artifact fan-out; any-non-empty guard).
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
    # load_config_overrides gives MEMTOMEM_HOOKS__TARGET_SCOPE precedence over
    # config.json (config.py: env wins), so a value inherited from the
    # dev/CI environment would override the per-test hooks.target_scope and make
    # the omitted-scope assertions non-hermetic. Scrub it for every CLI run here.
    monkeypatch.delenv("MEMTOMEM_HOOKS__TARGET_SCOPE", raising=False)
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


class TestMissingContextRefusalExitCode:
    """A missing ``.memtomem/context.md`` refusal in the mutating context
    commands must exit non-zero, not exit 0 after a red line.

    ``mm context generate`` / single-project ``mm context sync`` with no
    ``context.md`` and no ``--include`` print
    ``context.md not found. Run 'mm context init' first.`` in red and used to
    ``return`` / ``return False`` — leaving exit code 0, so a script or CI
    wrapping the command saw success. The fix raises ``SystemExit(1)`` after
    the existing ``secho`` (matching the ``seed-validation`` scan leg), so the
    exact red guidance text is preserved but the process exits non-zero.
    """

    def test_generate_missing_context_exits_nonzero(self, tmp_path, monkeypatch):
        runner, project = _runner_in_project(tmp_path, monkeypatch)
        # No .memtomem/context.md written (and no --include artifact leg).
        r = runner.invoke(cli, ["context", "generate"])
        assert r.exit_code != 0, f"expected failure, got 0: {r.output}"
        assert "not found. Run 'mm context init' first." in r.output

    def test_sync_missing_context_exits_nonzero(self, tmp_path, monkeypatch):
        runner, project = _runner_in_project(tmp_path, monkeypatch)
        # No .memtomem/context.md written (and no --include artifact leg).
        r = runner.invoke(cli, ["context", "sync"])
        assert r.exit_code != 0, f"expected failure, got 0: {r.output}"
        assert "not found. Run 'mm context init' first." in r.output
        # The refusal must not also print the success line.
        assert "Synced." not in r.output


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

    def test_diff_settings_omitted_scope_follows_config(self, tmp_path, monkeypatch):
        """With ``--scope`` omitted, the settings differ must follow the
        configured ``hooks.target_scope`` — like generate/sync — not a fixed
        artifact-tier default.

        Regression pin for the B2-2 fix: the ``diff`` ``--scope`` option default
        was ``"project_shared"`` (never None), so ``_resolve_cli_scope`` was
        handed a non-None override and could not fall back to config. Diff then
        compared the project_shared settings tier while generate/sync wrote the
        configured tier, producing misleading missing/out-of-sync output.
        """
        import memtomem.cli.context_cmd as ctx_cmd

        runner, project = _runner_in_project(tmp_path, monkeypatch)
        _ctx_with_sections(project)

        # ``_runner_in_project`` points HOME at ``tmp_path / "home"`` via
        # ``set_home``; ``load_config_overrides`` reads ``~/.memtomem/config.json``.
        # Use a tier distinct from BOTH the old buggy option default
        # ("project_shared") and the Mem2MemConfig field default ("user"), so a
        # pass proves the configured value was actually consulted.
        cfg_path = tmp_path / "home" / ".memtomem" / "config.json"
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text('{"hooks": {"target_scope": "project_local"}}', encoding="utf-8")

        seen: dict[str, object] = {}

        def _fake_settings_diff(root, scope):
            seen["scope"] = scope

        monkeypatch.setattr(ctx_cmd, "_print_settings_diff", _fake_settings_diff)

        r = runner.invoke(cli, ["context", "diff", "--include=settings"])
        assert r.exit_code == 0, r.output
        assert seen.get("scope") == "project_local", (
            f"omitted --scope: settings diff got scope={seen.get('scope')!r}, "
            "expected 'project_local' (configured hooks.target_scope), not the "
            "artifact-tier default"
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


class TestSyncHelpDocumentsFanout:
    def test_sync_help_mentions_artifact_kinds(self):
        """The ``mm context sync`` help used to claim only
        "Sync context.md to all detected agent files" — stale now that sync
        also fans out skills / agents / commands / settings (+ opt-in
        mcp-servers) via ``--include`` and honours --all-projects/--scope/
        --label/--force-unsafe."""
        from memtomem.cli.context_cmd import sync_cmd

        help_text = sync_cmd.help or ""
        assert help_text.strip() != "Sync context.md to all detected agent files."
        low = help_text.lower()
        for token in ("skills", "agents", "commands", "settings", "mcp-servers", "--include"):
            assert token in low, f"sync help should mention {token!r}: {help_text!r}"

    def test_sync_short_help_signals_fanout(self):
        """The short help (first docstring line — what Click shows in the
        ``mm context --help`` command list) must mention the artifact fan-out,
        not just agent files."""
        from memtomem.cli.context_cmd import sync_cmd

        short = sync_cmd.get_short_help_str(limit=200).lower()
        assert "artifact" in short, f"sync short help should signal fan-out: {short!r}"


class TestSeedValidationForceHelp:
    def test_force_help_matches_non_empty_guard(self):
        """The ``seed-validation --force`` help said it re-seeds only when the
        dir "already contains a .memtomem/ Store", but the guard refuses ANY
        non-empty directory. The help must match the code."""
        from memtomem.cli.context_cmd import seed_validation_cmd

        force_opt = next(p for p in seed_validation_cmd.params if p.name == "force")
        help_text = (force_opt.help or "").lower()
        assert "already contains a .memtomem" not in help_text
        assert "non-empty" in help_text


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
