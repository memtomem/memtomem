"""Tests for memtomem CLI commands.

Covers command registration, help text, argument parsing, and
basic config operations with mocked components.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.config import (
    FIELD_CONSTRAINTS,
    Mem2MemConfig,
    coerce_and_validate,
    load_config_overrides,
    save_config_overrides,
)


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
        for cmd in (
            "search",
            "add",
            "recall",
            "index",
            "config",
            "context",
            "embedding-reset",
            "reset",
            "web",
            "shell",
            "init",
        ):
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

    @patch("memtomem.config.save_config_overrides")
    @patch("memtomem.config.load_config_overrides")
    @patch("memtomem.config.Mem2MemConfig")
    def test_config_set_tokenizer_triggers_fts_rebuild(
        self, mock_cfg_cls, mock_load, mock_save, runner: CliRunner
    ) -> None:
        """Changing search.tokenizer via CLI must trigger set_tokenizer + FTS rebuild."""
        mock_cfg = MagicMock()
        mock_cfg.search.tokenizer = "unicode61"
        mock_cfg_cls.return_value = mock_cfg

        mock_storage = MagicMock()
        mock_storage.rebuild_fts = MagicMock(return_value=42)

        with (
            patch("memtomem.storage.fts_tokenizer.set_tokenizer") as mock_set_tok,
            patch("memtomem.storage.factory.create_storage", return_value=mock_storage),
        ):
            result = runner.invoke(cli, ["config", "set", "search.tokenizer", "kiwipiepy"])
            assert result.exit_code == 0

            mock_set_tok.assert_called_once_with("kiwipiepy")
            mock_storage.rebuild_fts.assert_called_once()

    @patch("memtomem.config.save_config_overrides")
    @patch("memtomem.config.load_config_overrides")
    @patch("memtomem.config.Mem2MemConfig")
    def test_config_set_non_tokenizer_no_fts_rebuild(
        self, mock_cfg_cls, mock_load, mock_save, runner: CliRunner
    ) -> None:
        """Non-tokenizer config changes must NOT trigger FTS rebuild."""
        mock_cfg = MagicMock()
        mock_cfg.search.default_top_k = 10
        mock_cfg_cls.return_value = mock_cfg

        with patch("memtomem.storage.fts_tokenizer.set_tokenizer") as mock_set_tok:
            result = runner.invoke(cli, ["config", "set", "search.default_top_k", "20"])
            assert result.exit_code == 0
            mock_set_tok.assert_not_called()

    def test_config_set_immutable_field(self, runner: CliRunner) -> None:
        """Attempting to set a non-mutable field is rejected."""
        result = runner.invoke(cli, ["config", "set", "search.nonexistent_field", "10"])
        assert result.exit_code != 0
        assert "not a mutable field" in result.output

    def test_config_set_namespace_rules_json(self, tmp_path, monkeypatch, runner: CliRunner):
        """End-to-end: `mm config set namespace.rules '[...]'` persists + reloads."""
        import json

        from memtomem.config import Mem2MemConfig, load_config_overrides

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        payload = '[{"path_glob": "docs/**/*.md", "namespace": "docs"}]'
        result = runner.invoke(cli, ["config", "set", "namespace.rules", payload])
        assert result.exit_code == 0, result.output

        data = json.loads(config_file.read_text())
        assert data["namespace"]["rules"] == [{"path_glob": "docs/**/*.md", "namespace": "docs"}]

        # Round-trip via load path: raw dict survives setattr + model_validate.
        fresh = Mem2MemConfig()
        load_config_overrides(fresh)
        from memtomem.config import NamespacePolicyRule

        rule = NamespacePolicyRule.model_validate(fresh.namespace.rules[0])
        assert rule.path_glob == "docs/**/*.md"
        assert rule.namespace == "docs"

    def test_config_set_namespace_rules_rejects_malformed(self, runner: CliRunner) -> None:
        """Malformed JSON for namespace.rules surfaces as CLI error."""
        result = runner.invoke(cli, ["config", "set", "namespace.rules", "[not valid json"])
        assert result.exit_code != 0
        assert "cannot parse JSON" in result.output


# ── Config validation helpers ───────────────────────────────────────────


class TestCoerceAndValidate:
    """Test the coerce_and_validate helper directly."""

    def test_none_constraint(self) -> None:
        assert coerce_and_validate("hello", None) == "hello"

    def test_int_coercion(self) -> None:
        constraint = {"type": int, "min": 1, "max": 100}
        assert coerce_and_validate("42", constraint) == 42

    def test_int_below_min(self) -> None:
        constraint = {"type": int, "min": 1, "max": 100}
        with pytest.raises(ValueError, match=">= 1"):
            coerce_and_validate("0", constraint)

    def test_int_above_max(self) -> None:
        constraint = {"type": int, "min": 1, "max": 100}
        with pytest.raises(ValueError, match="<= 100"):
            coerce_and_validate("200", constraint)

    def test_int_not_numeric(self) -> None:
        constraint = {"type": int}
        with pytest.raises(ValueError, match="cannot convert"):
            coerce_and_validate("abc", constraint)

    def test_bool_true_variants(self) -> None:
        constraint = {"type": bool}
        for v in ("true", "1", "yes", True):
            assert coerce_and_validate(v, constraint) is True

    def test_bool_false_variants(self) -> None:
        constraint = {"type": bool}
        for v in ("false", "0", "no", False):
            assert coerce_and_validate(v, constraint) is False

    def test_bool_invalid(self) -> None:
        constraint = {"type": bool}
        with pytest.raises(ValueError, match="cannot convert"):
            coerce_and_validate("maybe", constraint)

    def test_float_coercion(self) -> None:
        constraint = {"type": float, "min": 0.0, "max": 1.0}
        assert coerce_and_validate("0.5", constraint) == 0.5

    def test_allowed_constraint(self) -> None:
        constraint = {"type": str, "allowed": {"a", "b"}}
        assert coerce_and_validate("a", constraint) == "a"
        with pytest.raises(ValueError, match="must be one of"):
            coerce_and_validate("c", constraint)

    def test_list_float_coercion_from_string(self) -> None:
        """CSV string should be coerced to list[float]."""
        constraint = {"type": list, "item_type": float, "length": 2}
        result = coerce_and_validate("1.5,0.8", constraint)
        assert result == [1.5, 0.8]

    def test_list_float_coercion_from_list(self) -> None:
        """Passing an actual list should work (Web UI path)."""
        constraint = {"type": list, "item_type": float, "length": 2}
        result = coerce_and_validate([1.5, 0.8], constraint)
        assert result == [1.5, 0.8]

    def test_list_float_wrong_length(self) -> None:
        constraint = {"type": list, "item_type": float, "length": 2}
        with pytest.raises(ValueError, match="length 2"):
            coerce_and_validate("1.0,2.0,3.0", constraint)

    def test_list_float_invalid_element(self) -> None:
        constraint = {"type": list, "item_type": float, "length": 2}
        with pytest.raises(ValueError, match="cannot convert"):
            coerce_and_validate("abc,1.0", constraint)

    def test_rrf_weights_has_constraint(self) -> None:
        """search.rrf_weights must be registered in FIELD_CONSTRAINTS."""
        assert "search.rrf_weights" in FIELD_CONSTRAINTS

    # ── list[BaseSettings] coercion (namespace.rules) ──────────────

    def test_namespace_rules_from_json_string(self) -> None:
        """CLI path: `mm config set namespace.rules '[{...}]'` passes a JSON string."""
        from memtomem.config import NamespacePolicyRule

        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        raw = '[{"path_glob": "docs/**/*.md", "namespace": "docs"}]'
        result = coerce_and_validate(raw, constraint)
        assert isinstance(result, list) and len(result) == 1
        assert isinstance(result[0], NamespacePolicyRule)
        assert result[0].path_glob == "docs/**/*.md"
        assert result[0].namespace == "docs"

    def test_namespace_rules_from_list_of_dicts_pr253_regression(self) -> None:
        """Web UI path: PATCH /api/config sends a parsed list of dicts.

        Regression guard for PR #253: before this fix, ``coerce_and_validate``
        did not handle ``list[BaseSettings]``, so PATCH /api/config and
        ``mm config set namespace.rules ...`` stored raw dicts in
        ``cfg.namespace.rules``. That broke ``indexing/engine.py:121`` which
        accesses ``rule.path_glob`` on each entry — AttributeError on a dict.
        This test locks in that the mutation path produces model instances.
        """
        from memtomem.config import NamespacePolicyRule

        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        payload = [
            {"path_glob": "docs/**/*.md", "namespace": "docs"},
            {"path_glob": "work/**/*.md", "namespace": "work"},
        ]
        result = coerce_and_validate(payload, constraint)
        assert len(result) == 2
        assert all(isinstance(r, NamespacePolicyRule) for r in result)
        # Critical: the exact attribute access that was failing pre-PR.
        assert result[0].path_glob == "docs/**/*.md"
        assert [r.namespace for r in result] == ["docs", "work"]

    def test_namespace_rules_passthrough_for_model_instances(self) -> None:
        """Already-validated instances survive coercion unchanged."""
        from memtomem.config import NamespacePolicyRule

        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        rule = NamespacePolicyRule(path_glob="x/**", namespace="x")
        result = coerce_and_validate([rule], constraint)
        assert result == [rule]

    def test_namespace_rules_empty_list(self) -> None:
        """Empty list is valid (matches default_factory=list)."""
        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        assert coerce_and_validate([], constraint) == []
        assert coerce_and_validate("[]", constraint) == []

    def test_namespace_rules_rejects_malformed_json(self) -> None:
        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        with pytest.raises(ValueError, match="cannot parse JSON"):
            coerce_and_validate("[not json", constraint)

    def test_namespace_rules_rejects_non_list_json(self) -> None:
        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        with pytest.raises(ValueError, match="to list"):
            coerce_and_validate('{"path_glob": "x", "namespace": "y"}', constraint)

    def test_namespace_rules_rejects_scalar_entry(self) -> None:
        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        with pytest.raises(ValueError, match="item\\[0\\]: expected dict"):
            coerce_and_validate(["just-a-string"], constraint)

    def test_namespace_rules_propagates_model_validation_error(self) -> None:
        """Pydantic validator errors (e.g. empty path_glob) surface as ValueError."""
        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        with pytest.raises(ValueError, match="item\\[0\\]"):
            coerce_and_validate([{"path_glob": "", "namespace": "x"}], constraint)

    def test_field_constraints_are_well_formed(self) -> None:
        """Sanity: every declared constraint has a type and consistent bounds."""
        for key, c in FIELD_CONSTRAINTS.items():
            assert "type" in c, f"{key} missing type"
            # When both min and max are present, min must be < max
            if "min" in c and "max" in c:
                assert c["min"] < c["max"], f"{key} min >= max"


# ── save_config_overrides persistence ──────────────────────────────────


class TestSaveConfigOverrides:
    """Verify save→load round-trip for mutable and special fields."""

    def test_memory_dirs_survives_save_load(self, tmp_path, monkeypatch):
        """memory_dirs added via Web UI must survive a save→load cycle."""
        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        cfg = Mem2MemConfig()
        cfg.indexing.memory_dirs = [tmp_path / "a", tmp_path / "b"]
        save_config_overrides(cfg)

        fresh = Mem2MemConfig()
        load_config_overrides(fresh)

        loaded_dirs = [str(p) for p in fresh.indexing.memory_dirs]
        assert str(tmp_path / "a") in loaded_dirs
        assert str(tmp_path / "b") in loaded_dirs

    def test_invalid_value_falls_back_to_default(self, tmp_path, monkeypatch):
        """Invalid values in config.json should be skipped with warning, not crash."""
        import json

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        config_file.write_text(
            json.dumps(
                {
                    "search": {"default_top_k": -5},  # violates min=1
                }
            )
        )

        cfg = Mem2MemConfig()
        default_top_k = cfg.search.default_top_k
        load_config_overrides(cfg)

        # Invalid value must be rejected; field keeps code default
        assert cfg.search.default_top_k == default_top_k

    def test_invalid_value_does_not_block_valid_ones(self, tmp_path, monkeypatch):
        """One bad field must not prevent other valid fields from loading."""
        import json

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        config_file.write_text(
            json.dumps(
                {
                    "search": {"default_top_k": -5, "rrf_k": 80},
                    "decay": {"enabled": True},
                }
            )
        )

        cfg = Mem2MemConfig()
        load_config_overrides(cfg)

        # Valid fields applied, invalid one skipped
        assert cfg.search.rrf_k == 80
        assert cfg.decay.enabled is True

    def test_existing_memory_dirs_not_clobbered(self, tmp_path, monkeypatch):
        """Saving mutable fields must not destroy pre-existing memory_dirs."""
        import json

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        # Simulate mm init having written memory_dirs
        config_file.write_text(
            json.dumps(
                {
                    "indexing": {"memory_dirs": ["/pre/existing"]},
                }
            )
        )

        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        cfg.search.default_top_k = 42
        save_config_overrides(cfg)

        data = json.loads(config_file.read_text())
        assert "/pre/existing" in [str(p) for p in data["indexing"]["memory_dirs"]]

    def test_default_valued_field_not_persisted(self, tmp_path, monkeypatch):
        """Fields whose current value equals the class-level default must not
        be written. Otherwise a Web UI save would pin the default and shadow
        a ``config.d/`` fragment that sets a different value.
        """
        import json

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        cfg = Mem2MemConfig()
        # mmr.enabled default is False — simulate a Web UI "save section"
        # that dumps the whole section without the user touching mmr.
        save_config_overrides(cfg)

        data = json.loads(config_file.read_text()) if config_file.exists() else {}
        assert "mmr" not in data, (
            f"default-valued mmr section must not be persisted; got {data.get('mmr')!r}"
        )

    def test_existing_default_entry_pruned_on_save(self, tmp_path, monkeypatch):
        """An existing leftover entry that matches the current-and-default
        value must be removed on save, so the key stops shadowing fragments.
        """
        import json

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        # Simulate a prior leak: mmr.enabled=false pinned into config.json.
        config_file.write_text(json.dumps({"mmr": {"enabled": False}}))

        cfg = Mem2MemConfig()
        # cfg.mmr.enabled is False (default) and config.json has False too.
        # Next save should prune the pinned key.
        save_config_overrides(cfg)

        data = json.loads(config_file.read_text())
        assert "mmr" not in data

    def test_non_default_value_still_persists(self, tmp_path, monkeypatch):
        """Explicit non-default values must still be written."""
        import json

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        cfg = Mem2MemConfig()
        cfg.mmr.enabled = True  # default is False
        cfg.search.default_top_k = 42  # default is 10
        save_config_overrides(cfg)

        data = json.loads(config_file.read_text())
        assert data["mmr"]["enabled"] is True
        assert data["search"]["default_top_k"] == 42

    def test_memory_dirs_equal_to_default_still_persisted(self, tmp_path, monkeypatch):
        """memory_dirs is in _EXTRA_PERSIST_FIELDS and exempt from
        drop-default because its factory auto-discovers AI tool dirs via
        filesystem checks. "Equal to default" on machine A may not match
        machine B's default — so we always persist user-curated dir lists.
        """
        import json

        from memtomem.config import _default_memory_dirs

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        cfg = Mem2MemConfig()
        cfg.indexing.memory_dirs = _default_memory_dirs()
        save_config_overrides(cfg)

        data = json.loads(config_file.read_text())
        assert "memory_dirs" in data["indexing"], (
            "memory_dirs must be persisted even when equal to default "
            "(environment-dependent factory, preserve user intent)"
        )

    def test_legacy_repr_string_in_config_handled_gracefully(self, tmp_path, monkeypatch):
        """Pre-fix installations may have serialized ``namespace.rules`` via
        ``default=str`` → raw ``repr()`` strings in config.json. Loading such
        a file on upgrade must not crash: coerce rejects the shape, load path
        logs + skips, field falls back to its default. No data loss beyond
        the already-corrupt entry.
        """
        import json

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        # Case 1: whole value is a repr-ish string (not even JSON).
        config_file.write_text(
            json.dumps(
                {"namespace": {"rules": "<NamespacePolicyRule path_glob='x' namespace='y'>"}}
            )
        )
        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        assert cfg.namespace.rules == []  # fell back to default, no crash

        # Case 2: list of repr strings.
        config_file.write_text(json.dumps({"namespace": {"rules": ["<legacy repr entry>"]}}))
        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        assert cfg.namespace.rules == []

    def test_namespace_rules_round_trip(self, tmp_path, monkeypatch):
        """list[NamespacePolicyRule] survives save→load via model_dump/validate."""
        import json

        from memtomem.config import NamespacePolicyRule

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        cfg = Mem2MemConfig()
        cfg.namespace.rules = [
            NamespacePolicyRule(path_glob="docs/**/*.md", namespace="docs"),
            NamespacePolicyRule(path_glob="work/**/*.md", namespace="work"),
        ]
        save_config_overrides(cfg)

        # Persisted as list of dicts — not BaseSettings repr().
        data = json.loads(config_file.read_text())
        assert data["namespace"]["rules"] == [
            {"path_glob": "docs/**/*.md", "namespace": "docs"},
            {"path_glob": "work/**/*.md", "namespace": "work"},
        ]

        # load_config_overrides runs coerce_and_validate on each field that
        # has a FIELD_CONSTRAINTS entry. Because namespace.rules is now
        # registered, the raw dicts on disk are validated back into
        # NamespacePolicyRule instances — matching what downstream consumers
        # (indexing/engine.py) require (attribute access like `rule.path_glob`).
        fresh = Mem2MemConfig()
        load_config_overrides(fresh)
        assert all(isinstance(r, NamespacePolicyRule) for r in fresh.namespace.rules)
        assert fresh.namespace.rules == cfg.namespace.rules

    def test_section_with_only_defaults_dropped_entirely(self, tmp_path, monkeypatch):
        """If every mutable key in a section equals its default, the whole
        section is omitted from config.json (no orphan ``{}`` entries)."""
        import json

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        # Prior leak: entire decay section pinned at defaults.
        config_file.write_text(json.dumps({"decay": {"enabled": False, "half_life_days": 30.0}}))

        cfg = Mem2MemConfig()  # all defaults
        save_config_overrides(cfg)

        data = json.loads(config_file.read_text())
        assert "decay" not in data


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
