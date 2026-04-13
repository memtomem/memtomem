"""Tests for memtomem CLI commands.

Covers command registration, help text, argument parsing, and
basic config operations with mocked components.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli.config_cmd import _coerce_and_validate, _FIELD_CONSTRAINTS


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── Top-level CLI ───────────────────────────────────────────────────────


class TestCLIGroup:
    """Test root CLI group registration and help."""

    def test_help_returns_zero(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0

    def test_help_shows_description(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert "markdown-first memory infrastructure" in result.output

    def test_short_help_flag(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["-h"])
        assert result.exit_code == 0
        assert "markdown-first memory infrastructure" in result.output

    def test_registered_subcommands(self, runner: CliRunner) -> None:
        """All expected subcommands appear in help output."""
        result = runner.invoke(cli, ["--help"])
        for cmd in ("search", "add", "recall", "index", "config", "context",
                     "embedding-reset", "reset", "web", "shell", "init"):
            assert cmd in result.output, f"'{cmd}' not found in help output"

    def test_unknown_command(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["nonexistent"])
        assert result.exit_code != 0


# ── Search command ──────────────────────────────────────────────────────


class TestSearchCLI:
    """Test search subcommand argument parsing and help."""

    def test_search_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "Search the knowledge base" in result.output

    def test_search_options_listed(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["search", "--help"])
        for opt in ("--top-k", "--source-filter", "--tag-filter", "--namespace", "--format"):
            assert opt in result.output, f"'{opt}' not found in search help"

    def test_search_missing_query(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["search"])
        assert result.exit_code != 0


# ── Config commands ─────────────────────────────────────────────────────


class TestConfigCLI:
    """Test config show/set subcommands."""

    def test_config_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "--help"])
        assert result.exit_code == 0
        assert "View or modify" in result.output

    def test_config_show_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "show", "--help"])
        assert result.exit_code == 0
        assert "--format" in result.output

    def test_config_set_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "set", "--help"])
        assert result.exit_code == 0
        assert "KEY" in result.output
        assert "VALUE" in result.output

    @patch("memtomem.config.load_config_overrides")
    @patch("memtomem.config.Mem2MemConfig")
    def test_config_show_table(self, mock_cfg_cls, mock_load, runner: CliRunner) -> None:
        mock_cfg = MagicMock()
        mock_cfg.model_dump.return_value = {
            "search": {"default_top_k": 10},
            "embedding": {"provider": "ollama", "api_key": "sk-secret"},
        }
        mock_cfg_cls.return_value = mock_cfg

        result = runner.invoke(cli, ["config", "show"])
        assert result.exit_code == 0
        assert "[search]" in result.output
        assert "default_top_k" in result.output
        # API key should be masked
        assert "***" in result.output
        assert "sk-secret" not in result.output

    @patch("memtomem.config.load_config_overrides")
    @patch("memtomem.config.Mem2MemConfig")
    def test_config_show_json(self, mock_cfg_cls, mock_load, runner: CliRunner) -> None:
        mock_cfg = MagicMock()
        mock_cfg.model_dump.return_value = {"search": {"default_top_k": 10}}
        mock_cfg_cls.return_value = mock_cfg

        result = runner.invoke(cli, ["config", "show", "--format", "json"])
        assert result.exit_code == 0
        assert '"default_top_k": 10' in result.output

    def test_config_set_bad_key_format(self, runner: CliRunner) -> None:
        """Key without a dot separator is rejected."""
        result = runner.invoke(cli, ["config", "set", "noperiod", "10"])
        assert result.exit_code != 0

    def test_config_set_immutable_field(self, runner: CliRunner) -> None:
        """Attempting to set a non-mutable field is rejected."""
        result = runner.invoke(cli, ["config", "set", "search.nonexistent_field", "10"])
        assert result.exit_code != 0
        assert "not a mutable field" in result.output


# ── Config validation helpers ───────────────────────────────────────────


class TestCoerceAndValidate:
    """Test the _coerce_and_validate helper directly."""

    def test_none_constraint(self) -> None:
        assert _coerce_and_validate("hello", None) == "hello"

    def test_int_coercion(self) -> None:
        constraint = {"type": int, "min": 1, "max": 100}
        assert _coerce_and_validate("42", constraint) == 42

    def test_int_below_min(self) -> None:
        constraint = {"type": int, "min": 1, "max": 100}
        with pytest.raises(ValueError, match=">= 1"):
            _coerce_and_validate("0", constraint)

    def test_int_above_max(self) -> None:
        constraint = {"type": int, "min": 1, "max": 100}
        with pytest.raises(ValueError, match="<= 100"):
            _coerce_and_validate("200", constraint)

    def test_int_not_numeric(self) -> None:
        constraint = {"type": int}
        with pytest.raises(ValueError, match="cannot convert"):
            _coerce_and_validate("abc", constraint)

    def test_bool_true_variants(self) -> None:
        constraint = {"type": bool}
        for v in ("true", "1", "yes", True):
            assert _coerce_and_validate(v, constraint) is True

    def test_bool_false_variants(self) -> None:
        constraint = {"type": bool}
        for v in ("false", "0", "no", False):
            assert _coerce_and_validate(v, constraint) is False

    def test_bool_invalid(self) -> None:
        constraint = {"type": bool}
        with pytest.raises(ValueError, match="cannot convert"):
            _coerce_and_validate("maybe", constraint)

    def test_float_coercion(self) -> None:
        constraint = {"type": float, "min": 0.0, "max": 1.0}
        assert _coerce_and_validate("0.5", constraint) == 0.5

    def test_allowed_constraint(self) -> None:
        constraint = {"type": str, "allowed": {"a", "b"}}
        assert _coerce_and_validate("a", constraint) == "a"
        with pytest.raises(ValueError, match="must be one of"):
            _coerce_and_validate("c", constraint)

    def test_field_constraints_are_well_formed(self) -> None:
        """Sanity: every declared constraint has type, min, and max."""
        for key, c in _FIELD_CONSTRAINTS.items():
            assert "type" in c, f"{key} missing type"
            assert "min" in c, f"{key} missing min"
            assert "max" in c, f"{key} missing max"
            assert c["min"] < c["max"], f"{key} min >= max"


# ── Other subcommands (help text) ───────────────────────────────────────


class TestSubcommandHelp:
    """Verify help text is reachable for remaining subcommands."""

    def test_init_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["init", "--help"])
        assert result.exit_code == 0
        assert "wizard" in result.output.lower()

    def test_index_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["index", "--help"])
        assert result.exit_code == 0
        assert "--recursive" in result.output
        assert "--force" in result.output

    def test_add_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["add", "--help"])
        assert result.exit_code == 0
        assert "--title" in result.output
        assert "--tags" in result.output

    def test_recall_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["recall", "--help"])
        assert result.exit_code == 0
        assert "--since" in result.output
        assert "--until" in result.output
        assert "--format" in result.output

    def test_embedding_reset_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["embedding-reset", "--help"])
        assert result.exit_code == 0
        assert "apply-current" in result.output
        assert "revert-to-stored" in result.output

    def test_reset_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["reset", "--help"])
        assert result.exit_code == 0
        assert "Delete ALL data" in result.output
        assert "--yes" in result.output

    def test_context_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["context", "--help"])
        assert result.exit_code == 0
        assert "detect" in result.output
        assert "generate" in result.output

    def test_web_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["web", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output

    def test_shell_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["shell", "--help"])
        assert result.exit_code == 0
        assert "Interactive" in result.output
