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

    def test_version_flag_prints_package_version(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert result.output.strip().startswith("memtomem ")

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
    """Verify delta-only save semantics: persisted values equal cfg minus the
    comparand (defaults + env + config.d/ fragments + env-dependent factories).
    """

    @pytest.fixture
    def isolated(self, tmp_path, monkeypatch):
        """Isolate config.json, config.d/, and provider-dir discovery from the
        dev machine.

        Without isolation, ``build_comparand`` reads the developer's real
        ``~/.memtomem/config.d/`` fragments and the legacy auto_discover
        migration could pull in ``~/.claude/projects`` etc., producing
        per-machine comparand differences that mask the intended behavior.
        Provider-dir detection is stubbed to ``[]`` so tests that exercise
        ``memory_dirs`` delta semantics behave identically regardless of
        what AI tools the dev machine has installed.
        """
        config_file = tmp_path / "config.json"
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.setattr("memtomem.config._config_d_path", lambda: config_d)
        monkeypatch.setattr("memtomem.config._canonical_provider_dirs", lambda: [])
        monkeypatch.setattr(
            "memtomem.config._detect_provider_dirs",
            lambda: {"claude-memory": [], "claude-plans": [], "codex": []},
        )
        return {"config_file": config_file, "config_d": config_d, "tmp_path": tmp_path}

    # ── Load-path defensive tests (unrelated to delta semantic) ────────

    def test_memory_dirs_survives_save_load(self, isolated):
        """User-added memory_dirs (distinct from factory) must survive save→load."""
        tmp_path = isolated["tmp_path"]

        cfg = Mem2MemConfig()
        cfg.indexing.memory_dirs = [tmp_path / "a", tmp_path / "b"]
        save_config_overrides(cfg)

        fresh = Mem2MemConfig()
        load_config_overrides(fresh)

        loaded_dirs = [str(p) for p in fresh.indexing.memory_dirs]
        assert str(tmp_path / "a") in loaded_dirs
        assert str(tmp_path / "b") in loaded_dirs

    def test_invalid_value_falls_back_to_default(self, isolated):
        """Invalid values in config.json should be skipped with warning, not crash."""
        import json

        isolated["config_file"].write_text(
            json.dumps({"search": {"default_top_k": -5}})  # violates min=1
        )

        cfg = Mem2MemConfig()
        default_top_k = cfg.search.default_top_k
        load_config_overrides(cfg)

        assert cfg.search.default_top_k == default_top_k

    def test_invalid_value_does_not_block_valid_ones(self, isolated):
        """One bad field must not prevent other valid fields from loading."""
        import json

        isolated["config_file"].write_text(
            json.dumps(
                {
                    "search": {"default_top_k": -5, "rrf_k": 80},
                    "decay": {"enabled": True},
                }
            ),
            encoding="utf-8",
        )

        cfg = Mem2MemConfig()
        load_config_overrides(cfg)

        assert cfg.search.rrf_k == 80
        assert cfg.decay.enabled is True

    def test_existing_memory_dirs_not_clobbered(self, isolated):
        """Saving an unrelated mutable field must not erase pinned memory_dirs."""
        import json

        isolated["config_file"].write_text(
            json.dumps({"indexing": {"memory_dirs": ["/pre/existing"]}})
        )

        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        cfg.search.default_top_k = 42
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "/pre/existing" in [str(p) for p in data["indexing"]["memory_dirs"]]

    # ── Delta semantic (renamed from drop-default) ─────────────────────

    def test_comparand_equal_field_not_persisted(self, isolated):
        """Fields whose current value equals the comparand (default/env/fragment-
        derived) must not be written. Prevents default-flush over config.d/
        fragments — same coverage as pre-Z ``drop-default``, now generalized.
        """
        import json

        cfg = Mem2MemConfig()
        # mmr.enabled default is False — simulate a Web UI "save section"
        # that dumps the whole section without the user touching mmr.
        save_config_overrides(cfg)

        data = (
            json.loads(isolated["config_file"].read_text())
            if isolated["config_file"].exists()
            else {}
        )
        assert "mmr" not in data, (
            f"comparand-equal mmr section must not be persisted; got {data.get('mmr')!r}"
        )

    def test_existing_comparand_equal_entry_pruned(self, isolated):
        """An existing leftover entry that now matches the comparand must be
        removed on the next save, so the key stops shadowing fragments."""
        import json

        isolated["config_file"].write_text(json.dumps({"mmr": {"enabled": False}}))

        cfg = Mem2MemConfig()
        # cfg.mmr.enabled is False (matches comparand) → pruned on save.
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "mmr" not in data

    def test_non_default_value_still_persists(self, isolated):
        """Explicit values that differ from the comparand must still be written."""
        import json

        cfg = Mem2MemConfig()
        cfg.mmr.enabled = True  # default is False
        cfg.search.default_top_k = 42  # default is 10
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert data["mmr"]["enabled"] is True
        assert data["search"]["default_top_k"] == 42

    def test_section_with_only_comparand_equal_fields_dropped(self, isolated):
        """If every mutable key in a section equals the comparand, the whole
        section is omitted (no orphan ``{}`` entries)."""
        import json

        isolated["config_file"].write_text(
            json.dumps({"decay": {"enabled": False, "half_life_days": 30.0}})
        )

        cfg = Mem2MemConfig()  # all defaults = comparand
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "decay" not in data

    # ── New tests for Z design ─────────────────────────────────────────

    def test_fragment_value_not_dragged_to_config_json(self, isolated):
        """Regression guard for fragment drag-in (`project_fragment_dragin_gap.md`).

        Fragment defines ``exclude_patterns``; unrelated field save must not
        copy the fragment value into config.json. Before Z, save persisted
        the full effective value, which silently copied fragment contents
        into the REPLACE layer and froze later fragment edits.
        """
        import json

        (isolated["config_d"] / "noise.json").write_text(
            json.dumps({"indexing": {"exclude_patterns": ["*.tmp", "node_modules/"]}})
        )

        # Match web/MCP in-process flow: fragments already merged.
        cfg = Mem2MemConfig()
        from memtomem.config import load_config_d

        load_config_d(cfg)
        load_config_overrides(cfg)
        assert "*.tmp" in cfg.indexing.exclude_patterns  # fragment visible

        cfg.search.default_top_k = 42  # unrelated mutation
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "exclude_patterns" not in data.get("indexing", {}), (
            f"fragment exclude_patterns must not drag into config.json; got {data}"
        )

    def test_env_value_not_dragged(self, isolated, monkeypatch):
        """Env-sourced values must not copy into config.json on save."""
        import json

        monkeypatch.setenv("MEMTOMEM_MMR__ENABLED", "true")

        cfg = Mem2MemConfig()  # picks up env at construction
        assert cfg.mmr.enabled is True

        cfg.search.default_top_k = 42
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "mmr" not in data, (
            f"env-sourced mmr.enabled must not drag into config.json; got {data}"
        )

    def test_memory_dirs_factory_default_not_persisted(self, isolated):
        """memory_dirs == env-dependent factory output → dropped on save.

        This flips the pre-Z ``_EXTRA_PERSIST_FIELDS`` exemption: the
        factory output is now included in the comparand, so machine-A
        save doesn't pin factory-specific paths into config.json for
        migration to machine-B. Companion of
        ``test_machine_migration_requires_active_reset`` (this test locks
        the *drop* direction, the other locks the *active-reset* one).
        """
        import json

        from memtomem.config import _default_memory_dirs

        cfg = Mem2MemConfig()
        cfg.indexing.memory_dirs = _default_memory_dirs()  # matches factory
        cfg.mmr.enabled = True  # unrelated non-comparand so file is non-empty
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "memory_dirs" not in data.get("indexing", {}), (
            f"factory-default memory_dirs must be dropped on save under Z; got {data}"
        )

    def test_save_is_idempotent(self, isolated):
        """Two consecutive saves produce byte-identical output.

        Guards against order-dependency in comparand build (e.g. glob
        ordering, set iteration) or diff computation.
        """
        cfg = Mem2MemConfig()
        cfg.mmr.enabled = True
        cfg.search.default_top_k = 42

        save_config_overrides(cfg)
        first = isolated["config_file"].read_text()
        save_config_overrides(cfg)
        second = isolated["config_file"].read_text()

        assert first == second, f"idempotent save broken:\n---\n{first}\n---\n{second}"

    def test_machine_migration_requires_active_reset(self, isolated):
        """Machine-A config.json with machine-A-only paths carried to
        machine-C keeps those paths pinned until the user actively resets.

        Z doesn't auto-clean historical leftovers that don't match the
        local comparand (the REPLACE layer semantics preclude that);
        docs must tell users to run ``cfg.memory_dirs = _default_memory_dirs()``
        + save, or (future) ``mm config unset memory_dirs``.
        """
        import json
        from pathlib import Path

        from memtomem.config import _default_memory_dirs

        # Seed a config.json that pins a path not part of the local factory output.
        machine_a_dirs = [str(Path("~/.memtomem/memories").expanduser()), "/machine-a-only"]
        isolated["config_file"].write_text(
            json.dumps({"indexing": {"memory_dirs": machine_a_dirs}})
        )

        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        cfg.search.default_top_k = 99  # unrelated save
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "/machine-a-only" in [
            str(p) for p in data.get("indexing", {}).get("memory_dirs", [])
        ], "machine-A path must stay pinned on unrelated save (doc: user must reset actively)"

        # Active reset: drops cleanly
        cfg.indexing.memory_dirs = _default_memory_dirs()
        save_config_overrides(cfg)
        data2 = json.loads(isolated["config_file"].read_text())
        assert "memory_dirs" not in data2.get("indexing", {})

    def test_comparand_build_suppresses_warnings(self, isolated, caplog):
        """build_comparand(quiet=True) must not emit WARNING-level logs
        for malformed fragments. Without this, every save on a machine
        with any malformed fragment prints the same warning repeatedly."""
        import logging

        from memtomem.config import build_comparand

        # Malformed fragment that would normally warn on each load.
        (isolated["config_d"] / "bad.json").write_text('{"unknown_section": {"foo": 1}}')

        caplog.set_level(logging.WARNING, logger="memtomem.config")
        build_comparand(quiet=True)
        assert not caplog.records, (
            f"comparand build should not emit warnings; got {[r.message for r in caplog.records]}"
        )

        # Control: quiet=False still emits (proves the suppression is targeted).
        from memtomem.config import load_config_d

        caplog.clear()
        probe = Mem2MemConfig()
        load_config_d(probe, quiet=False)
        assert any(
            "unknown_section" in r.message.lower() or "unknown" in r.message.lower()
            for r in caplog.records
        ), f"quiet=False must still emit warnings; got {[r.message for r in caplog.records]}"

    # ── Unchanged — unrelated to delta semantic ────────────────────────

    def test_legacy_repr_string_in_config_handled_gracefully(self, isolated):
        """Pre-fix installations may have serialized ``namespace.rules`` via
        ``default=str`` → raw ``repr()`` strings in config.json. Loading such
        a file on upgrade must not crash: coerce rejects the shape, load path
        logs + skips, field falls back to its default. No data loss beyond
        the already-corrupt entry.
        """
        import json

        # Case 1: whole value is a repr-ish string (not even JSON).
        isolated["config_file"].write_text(
            json.dumps(
                {"namespace": {"rules": "<NamespacePolicyRule path_glob='x' namespace='y'>"}}
            )
        )
        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        assert cfg.namespace.rules == []

        # Case 2: list of repr strings.
        isolated["config_file"].write_text(
            json.dumps({"namespace": {"rules": ["<legacy repr entry>"]}})
        )
        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        assert cfg.namespace.rules == []

    def test_save_creates_parent_directory_if_missing(self, tmp_path, monkeypatch) -> None:
        """Structural guard for the ``path.parent.mkdir`` removal in
        ``save_config_overrides``: the helper is now responsible for creating
        the config directory. Every other ``isolated`` test writes into the
        already-existing ``tmp_path``, so without this test a future
        regression (e.g. dropping the helper's ``mkdir`` too) would pass CI.
        """
        nested_dir = tmp_path / "brand" / "new" / ".memtomem"
        config_file = nested_dir / "config.json"
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.setattr("memtomem.config._config_d_path", lambda: config_d)

        assert not nested_dir.exists()

        cfg = Mem2MemConfig()
        cfg.mmr.enabled = True  # force a non-comparand write
        save_config_overrides(cfg)

        assert config_file.exists()
        assert nested_dir.is_dir()

    def test_save_atomic_on_replace_failure(self, isolated, monkeypatch) -> None:
        """``save_config_overrides`` now writes via ``_atomic_write_json``.
        If ``os.replace`` fails mid-write, the existing ``config.json`` must
        stay byte-identical and no ``.config.*.tmp`` orphan should linger.
        Failure-mode complement to the happy-path coverage above.
        """
        import json as _json
        import os as _os

        original = _json.dumps({"mmr": {"enabled": True}}, indent=2)
        isolated["config_file"].write_text(original, encoding="utf-8")

        def fail_replace(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(_os, "replace", fail_replace)

        cfg = Mem2MemConfig()
        cfg.search.default_top_k = 42  # force a non-comparand field to trigger a write

        with pytest.raises(OSError, match="simulated replace failure"):
            save_config_overrides(cfg)

        assert isolated["config_file"].read_text(encoding="utf-8") == original
        orphans = [
            p
            for p in isolated["tmp_path"].iterdir()
            if p.name.startswith(".config.") and p.name.endswith(".tmp")
        ]
        assert not orphans, f"orphan tmp file(s) after failed atomic write: {orphans}"

    def test_namespace_rules_round_trip(self, isolated):
        """list[NamespacePolicyRule] survives save→load via model_dump/validate."""
        import json

        from memtomem.config import NamespacePolicyRule

        cfg = Mem2MemConfig()
        cfg.namespace.rules = [
            NamespacePolicyRule(path_glob="docs/**/*.md", namespace="docs"),
            NamespacePolicyRule(path_glob="work/**/*.md", namespace="work"),
        ]
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert data["namespace"]["rules"] == [
            {"path_glob": "docs/**/*.md", "namespace": "docs"},
            {"path_glob": "work/**/*.md", "namespace": "work"},
        ]

        fresh = Mem2MemConfig()
        load_config_overrides(fresh)
        assert all(isinstance(r, NamespacePolicyRule) for r in fresh.namespace.rules)
        assert fresh.namespace.rules == cfg.namespace.rules


# ── Config unset ────────────────────────────────────────────────────────


class TestConfigUnset:
    """Output matrix + idempotence + atomic write + fragment-reappearance
    regression coverage for ``mm config unset``.
    """

    @pytest.fixture
    def isolated(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.setattr("memtomem.config._config_d_path", lambda: config_d)
        return {"config_file": config_file, "config_d": config_d, "tmp_path": tmp_path}

    def test_unset_removes_pinned_key(self, isolated, runner: CliRunner) -> None:
        import json as _json

        isolated["config_file"].write_text(
            _json.dumps({"mmr": {"enabled": True, "lambda_param": 0.5}})
        )

        result = runner.invoke(cli, ["config", "unset", "mmr.enabled"])
        assert result.exit_code == 0, result.output
        assert "Removed: mmr.enabled" in result.output

        data = _json.loads(isolated["config_file"].read_text())
        assert "enabled" not in data["mmr"]
        assert data["mmr"]["lambda_param"] == 0.5

    def test_unset_removes_empty_section(self, isolated, runner: CliRunner) -> None:
        import json as _json

        isolated["config_file"].write_text(
            _json.dumps({"mmr": {"enabled": True}, "search": {"default_top_k": 42}})
        )

        result = runner.invoke(cli, ["config", "unset", "mmr.enabled"])
        assert result.exit_code == 0, result.output

        data = _json.loads(isolated["config_file"].read_text())
        assert "mmr" not in data
        assert data["search"]["default_top_k"] == 42

    def test_unset_deletes_empty_config_file(self, isolated, runner: CliRunner) -> None:
        import json as _json

        isolated["config_file"].write_text(_json.dumps({"mmr": {"enabled": True}}))

        result = runner.invoke(cli, ["config", "unset", "mmr.enabled"])
        assert result.exit_code == 0, result.output
        assert not isolated["config_file"].exists()
        assert "config.json now empty, file removed." in result.output

    def test_unset_extra_mutation_field_allowed(self, isolated, runner: CliRunner) -> None:
        """memory_dirs is not in MUTABLE_FIELDS but IS valid for unset."""
        import json as _json

        isolated["config_file"].write_text(
            _json.dumps({"indexing": {"memory_dirs": ["/machine-a-only"]}})
        )

        result = runner.invoke(cli, ["config", "unset", "indexing.memory_dirs"])
        assert result.exit_code == 0, result.output
        assert "Removed: indexing.memory_dirs" in result.output

    def test_unset_memory_dirs_emits_domain_warning(self, isolated, runner: CliRunner) -> None:
        import json as _json

        isolated["config_file"].write_text(
            _json.dumps({"indexing": {"memory_dirs": ["/machine-a-only"]}})
        )

        result = runner.invoke(cli, ["config", "unset", "indexing.memory_dirs"])
        assert result.exit_code == 0, result.output
        assert "mm memory-dirs list" in result.output
        assert "mm index" in result.output

    def test_unset_typo_suggests_similar_canonical_key(self, isolated, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "unset", "mmr.enabld"])
        assert result.exit_code == 1
        assert "Skipped mmr.enabld" in result.output
        assert "did you mean 'mmr.enabled'" in result.output

    def test_unset_unknown_key_without_suggestion(self, isolated, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "unset", "completely_unrelated_xyz"])
        assert result.exit_code == 1
        assert "Skipped completely_unrelated_xyz" in result.output
        assert "did you mean" not in result.output

    def test_unset_multiple_keys_best_effort(self, isolated, runner: CliRunner) -> None:
        import json as _json

        isolated["config_file"].write_text(
            _json.dumps({"mmr": {"enabled": True}, "search": {"default_top_k": 42}})
        )

        result = runner.invoke(cli, ["config", "unset", "mmr.enabled", "foo.bar"])
        assert result.exit_code == 1
        assert "Removed: mmr.enabled" in result.output
        assert "Skipped foo.bar" in result.output

        data = _json.loads(isolated["config_file"].read_text())
        assert "mmr" not in data
        assert data["search"]["default_top_k"] == 42

    def test_unset_canonical_already_unset_is_idempotent_success(
        self, isolated, runner: CliRunner
    ) -> None:
        """Canonical key not pinned → exit 0 + ``(already at default)``."""
        # config.json doesn't exist — simulating a fresh install.
        result = runner.invoke(cli, ["config", "unset", "mmr.enabled"])
        assert result.exit_code == 0, result.output
        assert "already at default" in result.output
        assert not isolated["config_file"].exists()

    def test_unset_on_malformed_config_reports_error(self, isolated, runner: CliRunner) -> None:
        isolated["config_file"].write_text("{not valid json")

        result = runner.invoke(cli, ["config", "unset", "mmr.enabled"])
        assert result.exit_code == 1
        assert "malformed" in result.output.lower()
        assert "mm init --fresh" in result.output

    def test_unset_fragment_value_reappears(self, isolated, runner: CliRunner) -> None:
        """End-to-end: fragment mmr.enabled=true shadowed by config.json=false;
        after unset, fragment layer wins on reload."""
        import json as _json

        (isolated["config_d"] / "noise.json").write_text(_json.dumps({"mmr": {"enabled": True}}))
        isolated["config_file"].write_text(_json.dumps({"mmr": {"enabled": False}}))

        # Confirm the shadowing baseline before unset.
        from memtomem.config import load_config_d

        baseline = Mem2MemConfig()
        load_config_d(baseline)
        load_config_overrides(baseline)
        assert baseline.mmr.enabled is False

        result = runner.invoke(cli, ["config", "unset", "mmr.enabled"])
        assert result.exit_code == 0, result.output

        fresh = Mem2MemConfig()
        load_config_d(fresh)
        load_config_overrides(fresh)
        assert fresh.mmr.enabled is True

    def test_atomic_write_preserves_original_on_failure(self, tmp_path, monkeypatch):
        import os as _os

        from memtomem.config import _atomic_write_json

        path = tmp_path / "config.json"
        original = '{"original": true}'
        path.write_text(original)

        def fail_replace(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(_os, "replace", fail_replace)

        with pytest.raises(OSError, match="simulated replace failure"):
            _atomic_write_json(path, {"new": True})

        assert path.read_text() == original
        orphans = [
            p
            for p in tmp_path.iterdir()
            if p.name.startswith(".config.") and p.name.endswith(".tmp")
        ]
        assert not orphans, f"orphan tmp file(s) left behind: {orphans}"

    def test_atomic_write_cleans_up_tmp_on_success(self, tmp_path) -> None:
        import json as _json

        from memtomem.config import _atomic_write_json

        path = tmp_path / "config.json"
        _atomic_write_json(path, {"ok": True})

        assert path.exists()
        assert _json.loads(path.read_text()) == {"ok": True}
        orphans = [
            p
            for p in tmp_path.iterdir()
            if p.name.startswith(".config.") and p.name.endswith(".tmp")
        ]
        assert not orphans


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
