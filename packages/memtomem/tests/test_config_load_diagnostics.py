"""A config section the loaders reject must be reported, not silently dropped.

The reproduction is one stale key left over from a bge-m3 install: the E5
profile requires ``dimension=384``, so ``load_config_overrides`` rejects the
whole assembled ``embedding`` section, restores its pre-override baseline and
the server runs on ``provider="none"`` — with a log line as the only trace
(#2385 item 3).

Covers the two halves of the fix: ``strict`` raises for a rejected *section*
while leaving field-level skips tolerant, and either way the rejection is kept
on the config as a load diagnostic for the status/config/web surfaces.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memtomem import config as _cfg
from memtomem import config_signature as _sig
from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides
from memtomem.errors import ConfigError

STALE_E5 = {
    "embedding": {
        "provider": "onnx",
        "model": "intfloat/multilingual-e5-small",
        "dimension": 1024,
    }
}


@pytest.fixture
def override_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect ``config.json`` for both the loader and ``build_fresh_config``.

    ``config_signature`` imports the path helpers by name, so each module
    needs its own patch.
    """
    p = tmp_path / "config.json"
    monkeypatch.setattr(_cfg, "_override_path", lambda: p)
    monkeypatch.setattr(_sig, "_override_path", lambda: p)
    return p


@pytest.fixture
def config_d_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "config.d"
    d.mkdir()
    monkeypatch.setattr(_cfg, "_config_d_path", lambda: d)
    monkeypatch.setattr(_sig, "_config_d_path", lambda: d)
    return d


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient ``MEMTOMEM_*`` and provider-dir discovery.

    Mirrors ``test_config_overrides._clear_all_memtomem_env``: without it the
    developer's own environment decides which keys the loader skips.
    """
    import os

    for name in list(os.environ):
        if name.upper().startswith("MEMTOMEM_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [])


class TestRejectedSectionDiagnostics:
    def test_strict_raises_with_the_field_level_reason(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        override_path.write_text(json.dumps(STALE_E5))
        cfg = Mem2MemConfig()
        before = cfg.embedding.model_copy(deep=True)

        with pytest.raises(ConfigError, match="multilingual-e5-small requires dimension=384"):
            load_config_overrides(cfg, migrate=False, strict=True)

        # Restored before raising: a caller that catches this must not be left
        # holding the half-assembled section the setattr loop built.
        assert cfg.embedding == before

    def test_strict_error_names_the_section_and_the_file(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        override_path.write_text(json.dumps(STALE_E5))

        with pytest.raises(ConfigError) as excinfo:
            load_config_overrides(Mem2MemConfig(), migrate=False, strict=True)

        assert "[embedding]" in str(excinfo.value)
        assert str(override_path) in str(excinfo.value)

    def test_build_fresh_config_is_strict_by_default(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        override_path.write_text(json.dumps(STALE_E5))

        with pytest.raises(ConfigError, match="multilingual-e5-small requires dimension=384"):
            _sig.build_fresh_config(migrate=False)

    def test_tolerant_keeps_the_baseline_and_records_the_rejection(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        raw = json.dumps(STALE_E5)
        override_path.write_text(raw)

        cfg = _sig.build_fresh_config(migrate=False, strict_overrides=False)

        assert cfg.embedding.provider == "none"
        (diagnostic,) = cfg.load_diagnostics
        assert diagnostic.section == "embedding"
        assert diagnostic.layer == "config.json"
        assert diagnostic.path == str(override_path)
        assert "multilingual-e5-small requires dimension=384" in diagnostic.error
        # Reading never rewrites the user's file.
        assert override_path.read_text() == raw

    def test_status_warning_payload_is_strings_only(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        """The status renderer special-cases dict values as the embedding
        ``provider/model (Nd)`` block, so a nested payload renders as a
        ``KeyError`` there."""
        override_path.write_text(json.dumps(STALE_E5))

        cfg = _sig.build_fresh_config(migrate=False, strict_overrides=False)
        warning = cfg.load_diagnostics[0].as_status_warning()

        assert warning["kind"] == "config_section_rejected"
        assert warning["section"] == "embedding"
        assert "dimension=384" in warning["fix"]
        assert all(isinstance(value, str) for value in warning.values())

    def test_fragment_rejection_is_recorded_against_the_config_d_layer(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        (config_d_dir / "10-embedding.json").write_text(json.dumps(STALE_E5))
        cfg = Mem2MemConfig()

        load_config_d(cfg)

        (diagnostic,) = cfg.load_diagnostics
        assert diagnostic.layer == "config.d"
        assert diagnostic.section == "embedding"
        assert "dimension=384" in diagnostic.error

    def test_rejected_fragment_then_valid_override_reports_the_event_not_the_state(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        """The later layer supplies a working section; the diagnostic still
        stands, because it describes what *that file* lost, not what is in
        effect."""
        (config_d_dir / "10-embedding.json").write_text(json.dumps(STALE_E5))
        override_path.write_text(
            json.dumps({"embedding": {"provider": "onnx", "model": "bge-m3", "dimension": 1024}})
        )

        cfg = _sig.build_fresh_config(migrate=False, strict_overrides=False)

        assert (cfg.embedding.provider, cfg.embedding.model) == ("onnx", "bge-m3")
        (diagnostic,) = cfg.load_diagnostics
        assert (diagnostic.layer, diagnostic.section) == ("config.d", "embedding")

    def test_reloading_the_same_object_does_not_duplicate_entries(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        (config_d_dir / "10-embedding.json").write_text(json.dumps(STALE_E5))
        override_path.write_text(json.dumps(STALE_E5))
        cfg = Mem2MemConfig()

        for _ in range(3):
            load_config_d(cfg)
            load_config_overrides(cfg, migrate=False)

        assert sorted(d.layer for d in cfg.load_diagnostics) == ["config.d", "config.json"]

    def test_a_fixed_file_clears_the_previous_entry(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        override_path.write_text(json.dumps(STALE_E5))
        cfg = Mem2MemConfig()
        load_config_overrides(cfg, migrate=False)
        assert len(cfg.load_diagnostics) == 1

        override_path.write_text(json.dumps({"search": {"default_top_k": 7}}))
        load_config_overrides(cfg, migrate=False)

        assert cfg.load_diagnostics == ()

    def test_each_loader_only_clears_its_own_layer(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        (config_d_dir / "10-embedding.json").write_text(json.dumps(STALE_E5))
        override_path.write_text(json.dumps({"search": {"default_top_k": 7}}))
        cfg = Mem2MemConfig()

        load_config_d(cfg)
        load_config_overrides(cfg, migrate=False)

        assert [d.layer for d in cfg.load_diagnostics] == ["config.d"]

    def test_diagnostics_stay_out_of_the_config_document(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        """``model_dump`` feeds ``save_config_overrides``, the config
        signature and ``mm config show --json``; a load record must not leak
        into any of them."""
        override_path.write_text(json.dumps(STALE_E5))
        cfg = Mem2MemConfig()
        load_config_overrides(cfg, migrate=False)

        dumped = cfg.model_dump()

        assert cfg.load_diagnostics  # the record exists
        assert "load_diagnostics" not in dumped
        assert "_load_diagnostics" not in dumped

    def test_diagnostics_survive_model_copy(self, override_path: Path, config_d_dir: Path) -> None:
        override_path.write_text(json.dumps(STALE_E5))
        cfg = Mem2MemConfig()
        load_config_overrides(cfg, migrate=False)

        assert cfg.model_copy(deep=True).load_diagnostics == cfg.load_diagnostics


class TestStrictBoundaryStaysNarrow:
    """Strict mode rejects *sections*, never a field-level skip.

    A field an upgrade removed, or one whose value is out of range, must not
    close the Web write gate — only a rejected section does, because it
    silently discards an explicit choice.
    """

    def _load_strict(self, payload: dict, override_path: Path) -> Mem2MemConfig:
        override_path.write_text(json.dumps(payload))
        cfg = Mem2MemConfig()
        load_config_overrides(cfg, migrate=False, strict=True)
        return cfg

    def test_out_of_range_constrained_value_is_skipped_not_raised(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        default = Mem2MemConfig().embedding.onnx_batch_size

        cfg = self._load_strict(
            {"embedding": {"onnx_batch_size": 0, "batch_size": 32}}, override_path
        )

        assert cfg.embedding.onnx_batch_size == default
        assert cfg.embedding.batch_size == 32  # the valid sibling still applies
        assert cfg.load_diagnostics == ()

    def test_unknown_section_is_skipped_not_raised(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        cfg = self._load_strict(
            {"no_such_section": {"x": 1}, "search": {"default_top_k": 7}}, override_path
        )

        assert cfg.search.default_top_k == 7

    def test_unknown_field_is_skipped_not_raised(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        cfg = self._load_strict(
            {"search": {"removed_by_an_upgrade": 1, "default_top_k": 7}}, override_path
        )

        assert cfg.search.default_top_k == 7

    def test_env_owned_key_is_skipped_not_raised(
        self, override_path: Path, config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", "3")

        cfg = self._load_strict({"search": {"default_top_k": 7}}, override_path)

        assert cfg.search.default_top_k == 3

    def test_unconstrained_type_error_reaches_section_validation_and_raises(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        """The boundary is *where* the error surfaces, not what the user
        meant: ``dimension`` has no ``FIELD_CONSTRAINTS`` entry, so a bad type
        gets past the per-key loop and fails the section."""
        with pytest.raises(ConfigError, match=r"\[embedding\]"):
            self._load_strict({"embedding": {"dimension": "abc"}}, override_path)

    def test_inactive_cross_field_combination_still_rejects_the_section(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        """Documented consequence of the chosen boundary: each value passes
        its own constraint and reranking is off, but the section validator
        rejects the pair, so a strict re-read refuses the file while a
        tolerant load behaves exactly as it did before."""
        payload = {"rerank": {"enabled": False, "min_pool": 100, "max_pool": 10}}

        with pytest.raises(ConfigError, match=r"\[rerank\]"):
            self._load_strict(payload, override_path)

        override_path.write_text(json.dumps(payload))
        tolerant = Mem2MemConfig()
        load_config_overrides(tolerant, migrate=False)

        assert tolerant.rerank.min_pool == Mem2MemConfig().rerank.min_pool
        assert tolerant.load_diagnostics[0].section == "rerank"


class TestSectionLookupUsesDeclaredFields:
    """Only declared config fields are sections.

    Both loaders used to resolve a section with a bare ``getattr``, so any
    attribute name in the file became a "section" whose ``.model_copy()``
    raised outside the validation handler — which a tolerant load, the whole
    point of which is to stay repairable, could not absorb.
    """

    @pytest.mark.parametrize("name", ["load_diagnostics", "_load_diagnostics", "model_dump"])
    def test_attribute_names_are_ignored_as_unknown_sections(
        self, name: str, override_path: Path, config_d_dir: Path
    ) -> None:
        override_path.write_text(json.dumps({name: {}, "search": {"default_top_k": 7}}))
        cfg = Mem2MemConfig()

        load_config_overrides(cfg, migrate=False, strict=True)

        assert cfg.search.default_top_k == 7
        assert cfg.load_diagnostics == ()

    @pytest.mark.parametrize("name", ["load_diagnostics", "_load_diagnostics", "model_dump"])
    def test_fragments_ignore_attribute_names_too(
        self, name: str, override_path: Path, config_d_dir: Path
    ) -> None:
        (config_d_dir / "10-x.json").write_text(
            json.dumps({name: {}, "search": {"default_top_k": 7}})
        )
        cfg = Mem2MemConfig()

        load_config_d(cfg)

        assert cfg.search.default_top_k == 7


class TestThePydanticContractTheRecordRestsOn:
    """Measured behaviour of the private attribute this design depends on.

    Recording the rejection on the config only works if the attribute is
    there on every config the loaders can be handed, and if a copy does not
    share one list with its source. Both are pydantic's behaviour rather than
    ours, so they are pinned here: a minor-version change that alters either
    turns a silent wrong answer into a red test.
    """

    def test_a_construct_built_config_still_has_a_readable_empty_list(self) -> None:
        """``model_construct`` skips validation but still initialises private
        attributes, so the loaders never meet a config without one."""
        built = Mem2MemConfig.model_construct()

        assert built.load_diagnostics == ()

    def test_a_deep_copy_gets_its_own_list(self, override_path: Path, config_d_dir: Path) -> None:
        """The two whole-config copies in the tree both pass ``deep=True``;
        a shallow copy shares the list, so appending through one would grow
        the other's record."""
        override_path.write_text(json.dumps(STALE_E5))
        cfg = Mem2MemConfig()
        load_config_overrides(cfg, migrate=False)

        clone = cfg.model_copy(deep=True)
        clone._load_diagnostics.clear()

        assert len(cfg.load_diagnostics) == 1


class TestResetHappensBeforeEveryEarlyReturn:
    """A layer that no longer rejects anything must clear its old entries.

    Each loader returns early on several shapes — a missing file, unreadable
    or malformed JSON, a non-object root, a missing ``config.d`` directory.
    The reset has to sit above all of them, or a stale rejection outlives the
    file that caused it and every surface keeps reporting it.
    """

    def _rejected_once(self, override_path: Path) -> Mem2MemConfig:
        override_path.write_text(json.dumps(STALE_E5))
        cfg = Mem2MemConfig()
        load_config_overrides(cfg, migrate=False)
        assert len(cfg.load_diagnostics) == 1
        return cfg

    def test_a_deleted_config_json_clears_its_entry(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        cfg = self._rejected_once(override_path)
        override_path.unlink()

        load_config_overrides(cfg, migrate=False)

        assert cfg.load_diagnostics == ()

    def test_malformed_json_clears_the_previous_entry(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        cfg = self._rejected_once(override_path)
        override_path.write_text('{"search":')

        load_config_overrides(cfg, migrate=False)

        assert cfg.load_diagnostics == ()

    def test_a_non_object_root_clears_the_previous_entry(
        self, override_path: Path, config_d_dir: Path
    ) -> None:
        cfg = self._rejected_once(override_path)
        override_path.write_text('["not", "a", "dict"]')

        load_config_overrides(cfg, migrate=False)

        assert cfg.load_diagnostics == ()

    def test_a_removed_config_d_directory_clears_its_entries(
        self, override_path: Path, config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fragment = config_d_dir / "10-embedding.json"
        fragment.write_text(json.dumps(STALE_E5))
        cfg = Mem2MemConfig()
        load_config_d(cfg)
        assert len(cfg.load_diagnostics) == 1

        fragment.unlink()
        config_d_dir.rmdir()
        load_config_d(cfg)

        assert cfg.load_diagnostics == ()
