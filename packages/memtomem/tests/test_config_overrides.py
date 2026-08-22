"""Tests for config precedence: env vars win over ~/.memtomem/config.json.

Documents the invariant that ``MEMTOMEM_<SECTION>__<FIELD>`` env vars take
precedence over persisted overrides in ``~/.memtomem/config.json``. Matches
what every ``.mcp.json`` example in the docs assumes.

Regression anchor for issue #248.
"""

from __future__ import annotations

import json
import logging
import sys
import warnings
from pathlib import Path

import pytest

from memtomem import config as _cfg
from memtomem.config import (
    Mem2MemConfig,
    assign_section_fields,
    load_config_d,
    load_config_overrides,
)

from .helpers import set_home


@pytest.fixture
def override_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the override file to tmp_path to avoid touching ~/.memtomem/."""
    p = tmp_path / "config.json"
    monkeypatch.setattr(_cfg, "_override_path", lambda: p)
    return p


@pytest.fixture
def config_d_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the config.d directory to tmp_path/config.d."""
    d = tmp_path / "config.d"
    d.mkdir()
    monkeypatch.setattr(_cfg, "_config_d_path", lambda: d)
    return d


def _clear_all_memtomem_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip ambient MEMTOMEM_* and stub provider-dir discovery to ``[]`` so
    the test env is fully deterministic.

    Stubbing ``_canonical_provider_dirs`` here (rather than autouse) prevents
    the auto_discover migration triggered by ``load_config_overrides`` from
    pulling in the dev machine's real ``~/.claude/projects/*/memory/`` etc.,
    which would otherwise pollute unrelated ``memory_dirs`` assertions.

    Migration tests that need a controlled fake list call
    ``monkeypatch.setattr(_cfg, "_canonical_provider_dirs", ...)`` AFTER
    invoking this helper — last-write-wins on monkeypatch means their
    explicit fake takes effect during the actual test logic.

    Detection tests (those exercising ``_detect_provider_dirs`` /
    ``_canonical_provider_dirs`` directly to validate scope and category
    structure) deliberately do NOT call this helper, so they hit the real
    implementation against an isolated ``HOME`` they set themselves.
    """
    import os

    for name in list(os.environ):
        if name.upper().startswith("MEMTOMEM_"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [])


def test_config_json_applies_when_no_env(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_all_memtomem_env(monkeypatch)
    override_path.write_text(
        json.dumps({"storage": {"sqlite_path": "/from/config.db"}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    assert str(cfg.storage.sqlite_path) == "/from/config.db"


def test_config_json_stale_removed_field_skipped_not_fatal(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale key for a since-removed field loads clean (skipped, not fatal).

    Regression for #1520 item 3: ``context_gateway.user_tier_enabled`` was
    removed without ever gaining a read site. A persisted config.json that
    still carries it must not break startup — the ``hasattr`` guard in
    ``load_config_overrides`` skips unknown field names within a known
    section, and sibling valid fields still apply.
    """
    _clear_all_memtomem_env(monkeypatch)
    override_path.write_text(
        json.dumps(
            {
                "context_gateway": {
                    "user_tier_enabled": True,
                    "experimental_claude_projects_scan": True,
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    assert not hasattr(cfg.context_gateway, "user_tier_enabled")
    assert cfg.context_gateway.experimental_claude_projects_scan is True


def test_config_json_cross_field_invariant_reverts_section(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config.json override violating a cross-field ``model_validator`` is
    rejected, not silently accepted (issue #1681).

    ``ConfigModel`` sub-configs don't set ``validate_assignment=True``, so the
    ``@model_validator(mode="after")`` checks never re-run for the ``setattr``
    override path — a keyless ``langfuse_enabled=true`` (which
    ``SessionTraceConfig._require_keys_when_enabled`` rejects on direct
    construction) used to slip through. ``load_config_overrides`` now
    re-validates each assembled section and reverts to the pre-override
    baseline on failure.
    """
    _clear_all_memtomem_env(monkeypatch)
    # The keyless-langfuse validator has a documented exception for the
    # Langfuse SDK's own env vars — strip them so the invariant actually fires.
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    override_path.write_text(
        json.dumps({"session_trace": {"enabled": True, "langfuse_enabled": True}}),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    # Section reverted to its known-good baseline (langfuse off), not accepted.
    assert cfg.session_trace.langfuse_enabled is False


def test_config_json_chunk_range_invariant_reverts_section(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cross-field range invariant (``min_chunk_tokens > max_chunk_tokens``)
    in config.json reverts the indexing section rather than loading an
    inconsistent config (issue #1681)."""
    _clear_all_memtomem_env(monkeypatch)
    default_min = Mem2MemConfig().indexing.min_chunk_tokens
    default_max = Mem2MemConfig().indexing.max_chunk_tokens
    override_path.write_text(
        json.dumps({"indexing": {"min_chunk_tokens": 200, "max_chunk_tokens": 100}}),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg, migrate=False)
    assert cfg.indexing.min_chunk_tokens == default_min
    assert cfg.indexing.max_chunk_tokens == default_max
    assert cfg.indexing.min_chunk_tokens <= cfg.indexing.max_chunk_tokens


def test_config_json_valid_override_still_applies(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-validation guard must not reject valid overrides: a well-formed
    section (and an unrelated sibling section) still load (issue #1681)."""
    _clear_all_memtomem_env(monkeypatch)
    override_path.write_text(
        json.dumps(
            {
                "context_window": {"enabled": True},
                "storage": {"sqlite_path": "/from/config.db"},
            }
        ),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg, migrate=False)
    assert cfg.context_window.enabled is True
    assert str(cfg.storage.sqlite_path) == "/from/config.db"


def test_config_json_invalid_rrf_weights_warns_and_keeps_default(
    override_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#2094: a persisted pair fusion cannot honor is skipped with a warning
    and the default survives — an existing config file must not become a
    startup failure."""
    _clear_all_memtomem_env(monkeypatch)
    override_path.write_text(json.dumps({"search": {"rrf_weights": [-1.0, 1.0]}}), encoding="utf-8")
    cfg = Mem2MemConfig()
    with caplog.at_level(logging.WARNING):
        load_config_overrides(cfg, migrate=False)
    assert cfg.search.rrf_weights == [1.0, 1.0]
    assert any("rrf_weights" in r.getMessage() for r in caplog.records)


def test_config_json_revalidation_does_not_re_emit_field_warnings(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The section re-validation must not re-fire field-level deprecation
    warnings for defaulted fields (issue #1681 review).

    ``model_validate(model_dump())`` materializes defaults, which would
    otherwise re-trigger e.g. the ``rerank.top_k`` migration warning for a
    user who only set ``rerank.enabled`` — spurious, and fatal under
    warnings-as-errors. A valid ``{"rerank": {"enabled": true}}`` must load
    cleanly even when ``DeprecationWarning`` is escalated to an error.
    """
    _clear_all_memtomem_env(monkeypatch)
    override_path.write_text(json.dumps({"rerank": {"enabled": True}}), encoding="utf-8")
    cfg = Mem2MemConfig()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        load_config_overrides(cfg, migrate=False)
    assert cfg.rerank.enabled is True


def test_config_json_legacy_field_deprecation_surfaced(
    override_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A user-supplied legacy field's deprecation is surfaced (not swallowed)
    without crashing under warnings-as-errors (issue #1681 review).

    The re-validation captures the ``DeprecationWarning`` triggered by
    ``rerank.top_k`` and re-emits it via the logger;
    ``catch_warnings(record=True)`` keeps it from escalating to an exception
    even under ``-W error``. The re-validation is a *check* — it does not
    persist the coerced/migrated model back — so ``top_k`` is not auto-rewritten
    into ``min_pool`` on the ``config.json`` path (that broader normalization is
    out of scope); the surfaced warning tells the operator to migrate.
    """
    import logging

    _clear_all_memtomem_env(monkeypatch)
    override_path.write_text(json.dumps({"rerank": {"top_k": 50}}), encoding="utf-8")
    cfg = Mem2MemConfig()
    with warnings.catch_warnings(), caplog.at_level(logging.WARNING):
        warnings.simplefilter("error", DeprecationWarning)
        load_config_overrides(cfg, migrate=False)
    # The deprecation is surfaced to the operator via logging, not swallowed.
    assert any(
        "DeprecationWarning" in rec.message and "top_k" in rec.message for rec in caplog.records
    )
    # The check does not mutate field types / apply the migration in place.
    assert cfg.rerank.min_pool == Mem2MemConfig().rerank.min_pool


def test_config_json_explicit_deprecated_field_at_default_still_surfaced(
    override_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An explicitly-set deprecated key whose value equals the model default is
    still surfaced (issue #1681 review, round 3).

    The migrations are presence-keyed, so ``exclude_defaults`` alone would drop
    a user's ``{"rerank": {"top_k": 20}}`` (20 == the ``top_k`` default) and
    silently hide the deprecation. The applied-keys overlay forces it back into
    the revalidation payload.
    """
    import logging

    _clear_all_memtomem_env(monkeypatch)
    override_path.write_text(json.dumps({"rerank": {"top_k": 20}}), encoding="utf-8")
    cfg = Mem2MemConfig()
    with caplog.at_level(logging.WARNING):
        load_config_overrides(cfg, migrate=False)
    assert any(
        "DeprecationWarning" in rec.message and "top_k" in rec.message for rec in caplog.records
    )


def test_env_var_wins_over_config_json_scalar(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_all_memtomem_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", "/from/env.db")
    override_path.write_text(
        json.dumps({"storage": {"sqlite_path": "/from/config.db"}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    assert Path(cfg.storage.sqlite_path).as_posix() == "/from/env.db"


def test_env_var_wins_over_config_json_list(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_all_memtomem_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_INDEXING__MEMORY_DIRS", '["/from/env"]')
    override_path.write_text(
        json.dumps({"indexing": {"memory_dirs": ["/from/config"]}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    assert [Path(p).as_posix() for p in cfg.indexing.memory_dirs] == ["/from/env"]


def test_env_and_config_coexist_on_different_fields(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env on one field, config.json on another — both should take effect."""
    _clear_all_memtomem_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", "/from/env.db")
    override_path.write_text(
        json.dumps(
            {
                "storage": {"sqlite_path": "/from/config.db"},
                "embedding": {"model": "from-config"},
            }
        ),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    assert Path(cfg.storage.sqlite_path).as_posix() == "/from/env.db"
    assert cfg.embedding.model == "from-config"


def test_regression_pr247_mcp_json_env_block(
    override_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exact ``.mcp.json`` env block shipped in docs (integrations/claude-code.md
    via PR #247) must work end-to-end after ``mm init`` has written a
    ``config.json``. One scalar (``SQLITE_PATH``), one list (``MEMORY_DIRS``).
    """
    _clear_all_memtomem_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", "~/.memtomem/memtomem.db")
    monkeypatch.setenv("MEMTOMEM_INDEXING__MEMORY_DIRS", '["~/notes"]')
    override_path.write_text(
        json.dumps(
            {
                "storage": {"sqlite_path": "/tmp/wizard.db"},
                "indexing": {"memory_dirs": ["/tmp/wizard-memories"]},
            }
        ),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    assert Path(cfg.storage.sqlite_path).as_posix() == "~/.memtomem/memtomem.db"
    assert [Path(p).as_posix() for p in cfg.indexing.memory_dirs] == ["~/notes"]


# ---------------------------------------------------------------------------
# config.d fragment loader (Phase 2b)
# ---------------------------------------------------------------------------


def test_config_d_append_merges_with_defaults(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APPEND list field: fragment entries added on top of existing list."""
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "claude-desktop.json").write_text(
        json.dumps({"indexing": {"memory_dirs": ["/from/fragment"]}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    before = list(cfg.indexing.memory_dirs)
    load_config_d(cfg)
    after = [str(p) for p in cfg.indexing.memory_dirs]
    assert "/from/fragment" in after
    for original in before:
        assert str(original) in after


def test_config_d_append_dedupes(config_d_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Duplicate APPEND entries across fragments collapse to one."""
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "01-a.json").write_text(
        json.dumps({"indexing": {"exclude_patterns": ["*.tmp", "*.log"]}}), encoding="utf-8"
    )
    (config_d_dir / "02-b.json").write_text(
        json.dumps({"indexing": {"exclude_patterns": ["*.log", "*.bak"]}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_d(cfg)
    assert cfg.indexing.exclude_patterns == ["*.tmp", "*.log", "*.bak"]


def test_config_d_replace_overwrites(config_d_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REPLACE list field: last fragment wins, prior list discarded."""
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "01-a.json").write_text(
        json.dumps({"search": {"rrf_weights": [0.5, 0.5]}}), encoding="utf-8"
    )
    (config_d_dir / "02-b.json").write_text(
        json.dumps({"search": {"rrf_weights": [0.3, 0.7]}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_d(cfg)
    assert cfg.search.rrf_weights == [0.3, 0.7]


def test_config_d_invalid_rrf_weights_fragment_warned_and_skipped(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """#2094 review: plain ``setattr`` skips pydantic validation, so the
    fragment path must run the canonical constraint validator — a pair
    ``mm config set`` rejects cannot ride in through config.d."""
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "01-bad.json").write_text(
        json.dumps({"search": {"rrf_weights": [-1.0, 1.0]}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    with caplog.at_level(logging.WARNING):
        load_config_d(cfg)
    assert cfg.search.rrf_weights == [1.0, 1.0]
    assert any("rrf_weights" in r.getMessage() for r in caplog.records)


def test_config_d_scalar_last_wins(config_d_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Scalar field: last fragment applied wins."""
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "01-a.json").write_text(
        json.dumps({"storage": {"sqlite_path": "/a.db"}}), encoding="utf-8"
    )
    (config_d_dir / "02-b.json").write_text(
        json.dumps({"storage": {"sqlite_path": "/b.db"}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_d(cfg)
    assert str(cfg.storage.sqlite_path) == "/b.db"


def test_config_d_env_wins_over_fragments(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env var set → fragment value for that field skipped."""
    _clear_all_memtomem_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", "/from/env.db")
    (config_d_dir / "a.json").write_text(
        json.dumps({"storage": {"sqlite_path": "/from/fragment.db"}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_d(cfg)
    assert Path(cfg.storage.sqlite_path).as_posix() == "/from/env.db"


def test_config_d_unknown_section_warned_but_not_fatal(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Fragment with unknown section logs a warning and is skipped; valid
    fields in the same fragment still apply."""
    import logging

    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "a.json").write_text(
        json.dumps({"storage": {"sqlite_path": "/ok.db"}, "nope_not_a_section": {"x": 1}}),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    with caplog.at_level(logging.WARNING, logger="memtomem.config"):
        load_config_d(cfg)
    assert str(cfg.storage.sqlite_path) == "/ok.db"
    assert any("nope_not_a_section" in r.message for r in caplog.records)


def test_config_d_invalid_json_warned(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Malformed JSON in a fragment is logged and skipped without killing startup."""
    import logging

    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "bad.json").write_text("{ not valid json", encoding="utf-8")
    (config_d_dir / "good.json").write_text(
        json.dumps({"storage": {"sqlite_path": "/ok.db"}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    with caplog.at_level(logging.WARNING, logger="memtomem.config"):
        load_config_d(cfg)
    assert str(cfg.storage.sqlite_path) == "/ok.db"
    assert any("bad.json" in r.message for r in caplog.records)


def test_config_d_ignores_non_json_files(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only *.json files in config.d/ are read; README.md etc. are ignored."""
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "README.md").write_text("not a fragment", encoding="utf-8")
    (config_d_dir / "a.json").write_text(
        json.dumps({"storage": {"sqlite_path": "/ok.db"}}), encoding="utf-8"
    )
    cfg = Mem2MemConfig()
    load_config_d(cfg)
    assert str(cfg.storage.sqlite_path) == "/ok.db"


def test_config_d_namespace_rules_appends(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """APPEND merge: default empty rules + fragment rule → one loaded rule."""
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "claude.json").write_text(
        json.dumps(
            {
                "namespace": {
                    "rules": [
                        {
                            "path_glob": "**/.claude/**/memory/**",
                            "namespace": "claude:memory",
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    assert cfg.namespace.rules == []
    load_config_d(cfg)
    assert len(cfg.namespace.rules) == 1
    assert cfg.namespace.rules[0].namespace == "claude:memory"


def test_config_d_namespace_rules_alphabetical_order(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fragments concatenate in alphabetical filename order — freeze this
    contract so users can rely on numeric prefixes (``10-foo.json`` before
    ``20-bar.json``) for first-match-wins precedence across fragments.
    """
    _clear_all_memtomem_env(monkeypatch)
    (config_d_dir / "20-gdrive.json").write_text(
        json.dumps(
            {
                "namespace": {
                    "rules": [
                        {"path_glob": "**/gdrive/**", "namespace": "gdrive"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (config_d_dir / "10-claude.json").write_text(
        json.dumps(
            {
                "namespace": {
                    "rules": [
                        {"path_glob": "**/claude/**", "namespace": "claude"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    load_config_d(cfg)
    assert [r.namespace for r in cfg.namespace.rules] == ["claude", "gdrive"]


def test_config_d_namespace_rules_dedup_after_home_expansion(
    config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Freeze the dedup semantics for NamespacePolicyRule: two fragments that
    declare the same rule once with a leading ``~/`` and once with the expanded
    absolute path must collapse to a single rule.

    ``_dedup_key`` hashes ``BaseModel.model_dump(mode="json")`` — i.e. the
    *post-validator* field values. Since ``path_glob`` expands ``~/`` in its
    validator, both forms share an identity after coercion, so they dedupe.
    This test pins that contract: a future refactor that moved expansion out
    of the validator (or the dedup key off ``model_dump``) would start
    producing two rules and fail here.
    """
    _clear_all_memtomem_env(monkeypatch)
    # ``as_posix()`` matches the validator's normalization. Pre-#726 this was
    # ``str(Path(...).expanduser())`` which dedup'd on POSIX by accident
    # (``str`` of a POSIX absolute path is identical to its as_posix form).
    # On Windows the raw ``str`` form leaks backslashes; the validator now
    # normalizes both ``~/`` and absolute inputs to forward slashes so dedup
    # works on every platform.
    abs_form = Path("~/some/memtomem-test/**").expanduser().as_posix()
    (config_d_dir / "10-home.json").write_text(
        json.dumps(
            {
                "namespace": {
                    "rules": [
                        {"path_glob": "~/some/memtomem-test/**", "namespace": "x"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    (config_d_dir / "20-abs.json").write_text(
        json.dumps({"namespace": {"rules": [{"path_glob": abs_form, "namespace": "x"}]}}),
        encoding="utf-8",
    )
    cfg = Mem2MemConfig()
    load_config_d(cfg)
    assert len(cfg.namespace.rules) == 1, [r.path_glob for r in cfg.namespace.rules]
    assert cfg.namespace.rules[0].path_glob == abs_form


# ---------------------------------------------------------------------------
# Enforcement: every list[*] field must declare a merge strategy.
#
# Guards against future contributors adding a ``list[X]`` field to a config
# section without picking APPEND vs REPLACE. Without this test, the fragment
# loader would silently fall through to the REPLACE branch for unannotated
# list fields, letting a fragment clobber a positional list by accident.
# ---------------------------------------------------------------------------


def test_every_list_field_declares_merge_strategy() -> None:
    """Fails loudly if a new ``list[*]`` field in any ``Mem2MemConfig`` section
    is missing a ``MergeStrategy`` annotation.
    """
    from typing import get_origin

    from memtomem.config import MergeStrategy

    missing: list[str] = []
    for section_name, section_field in Mem2MemConfig.model_fields.items():
        sec_cls = section_field.annotation
        if not (isinstance(sec_cls, type) and hasattr(sec_cls, "model_fields")):
            continue
        for field_name, info in sec_cls.model_fields.items():
            if get_origin(info.annotation) is list:
                has_strategy = any(isinstance(m, MergeStrategy) for m in info.metadata)
                if not has_strategy:
                    missing.append(f"{section_name}.{field_name}")
    assert not missing, (
        "These list[*] fields lack a MergeStrategy annotation — wrap the "
        "type in Annotated[list[X], APPEND] or Annotated[list[X], REPLACE] "
        "in config.py:\n  - " + "\n  - ".join(missing)
    )


# ---------------------------------------------------------------------------
# indexing.auto_discover migration (legacy → explicit memory_dirs)
#
# Pre-Z, ``auto_discover=True`` was a runtime flag that silently appended
# provider home dirs on every startup. Post-Z it's a one-shot migration
# trigger: ``load_config_overrides`` calls ``_migrate_auto_discover_once``,
# which converts legacy installs to explicit ``memory_dirs`` entries and
# flips the flag to False. Brand-new installs (no ``config.json``) skip
# migration so the wizard opt-in remains the only way provider dirs land
# in ``memory_dirs``.
# ---------------------------------------------------------------------------


def test_migration_noop_when_auto_discover_false(
    override_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Explicit auto_discover=False in config.json → migration skipped."""
    _clear_all_memtomem_env(monkeypatch)
    fake = tmp_path / "fake-tool"
    fake.mkdir()
    monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [fake])
    override_path.write_text(json.dumps({"indexing": {"auto_discover": False}}), encoding="utf-8")

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)

    resolved = {Path(p).expanduser().resolve() for p in cfg.indexing.memory_dirs}
    assert fake.resolve() not in resolved


def test_migration_noop_when_no_config_json(
    override_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Brand-new install (no config.json) skips migration even with
    auto_discover defaulting to True. Provider dirs stay opt-in via the
    wizard.
    """
    _clear_all_memtomem_env(monkeypatch)
    fake = tmp_path / "fake-tool"
    fake.mkdir()
    monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [fake])
    # override_path fixture sets the path but does NOT write the file

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)  # config.json doesn't exist

    resolved = {Path(p).expanduser().resolve() for p in cfg.indexing.memory_dirs}
    assert fake.resolve() not in resolved
    assert cfg.indexing.auto_discover is True  # not flipped — migration skipped


def test_migration_appends_dirs_and_flips_flag(
    override_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Legacy install (config.json present + auto_discover defaults True)
    migrates: discovered dirs append to memory_dirs and flag flips to False.
    """
    _clear_all_memtomem_env(monkeypatch)
    fake = tmp_path / "fake-tool"
    fake.mkdir()
    monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [fake])
    override_path.write_text("{}", encoding="utf-8")

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)

    resolved = {Path(p).expanduser().resolve() for p in cfg.indexing.memory_dirs}
    assert fake.resolve() in resolved
    assert cfg.indexing.auto_discover is False


def test_migration_preserves_existing_user_memory_dirs(
    override_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The most common legacy install has user-set ``memory_dirs`` *and*
    ``auto_discover`` defaulted True. Migration must preserve the explicit
    user entries alongside the newly-discovered provider dirs — dropping
    them would silently shrink the indexed corpus on first post-upgrade
    startup. Directly guards the ``_persist_auto_discover_migration``
    invariant flagged in its docstring.
    """
    _clear_all_memtomem_env(monkeypatch)
    user_dir = tmp_path / "user-explicit"
    user_dir.mkdir()
    fake_provider = tmp_path / "fake-provider"
    fake_provider.mkdir()
    monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [fake_provider])
    override_path.write_text(
        json.dumps({"indexing": {"memory_dirs": [str(user_dir)]}}), encoding="utf-8"
    )

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)

    resolved = {Path(p).expanduser().resolve() for p in cfg.indexing.memory_dirs}
    assert user_dir.resolve() in resolved, "user-set memory_dirs dropped by migration"
    assert fake_provider.resolve() in resolved, "provider dir not appended"
    assert cfg.indexing.auto_discover is False

    persisted = json.loads(override_path.read_text(encoding="utf-8"))
    # Phase 1 (#836) persists home-rooted paths in ``~/...`` portable form,
    # so compare the persisted list via ``Path.expanduser().resolve()``
    # rather than raw string equality. ``tmp_path`` lands under ``$HOME``
    # on Windows CI but typically not on POSIX, so a raw-string assertion
    # is platform-fragile.
    persisted_resolved = {
        Path(p).expanduser().resolve() for p in persisted["indexing"]["memory_dirs"]
    }
    assert user_dir.resolve() in persisted_resolved
    assert fake_provider.resolve() in persisted_resolved
    assert persisted["indexing"]["auto_discover"] is False


def test_migration_persists_to_config_json(
    override_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Migration writes both new memory_dirs entries and the False flag to disk."""
    _clear_all_memtomem_env(monkeypatch)
    fake = tmp_path / "fake-tool"
    fake.mkdir()
    monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [fake])
    override_path.write_text("{}", encoding="utf-8")

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)

    persisted = json.loads(override_path.read_text(encoding="utf-8"))
    assert persisted["indexing"]["auto_discover"] is False
    assert _cfg._portable_path_str(str(fake)) in persisted["indexing"]["memory_dirs"]


def test_migration_idempotent(
    override_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Second load_config_overrides after migration is a no-op (flag is False)."""
    _clear_all_memtomem_env(monkeypatch)
    fake = tmp_path / "fake-tool"
    fake.mkdir()
    monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [fake])
    override_path.write_text("{}", encoding="utf-8")

    cfg1 = Mem2MemConfig()
    load_config_overrides(cfg1)
    # First load goes through migration: memory_dirs is the in-memory list
    # of raw ``Path`` values. Second load reads back the persisted tilde-form
    # written by Phase 1 (#836). Resolve both before comparison so the round
    # trip is checked by filesystem identity rather than raw string form
    # (which differs on Windows where ``tmp_path`` sits under ``$HOME``).
    first_resolved = sorted(Path(p).expanduser().resolve() for p in cfg1.indexing.memory_dirs)

    cfg2 = Mem2MemConfig()
    load_config_overrides(cfg2)
    second_resolved = sorted(Path(p).expanduser().resolve() for p in cfg2.indexing.memory_dirs)

    assert first_resolved == second_resolved


def test_migration_env_var_false_skips(
    override_path: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """MEMTOMEM_INDEXING__AUTO_DISCOVER=false short-circuits migration even
    when a config.json exists (env wins over the default True)."""
    _clear_all_memtomem_env(monkeypatch)
    monkeypatch.setenv("MEMTOMEM_INDEXING__AUTO_DISCOVER", "false")
    fake = tmp_path / "fake-tool"
    fake.mkdir()
    monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [fake])
    override_path.write_text("{}", encoding="utf-8")

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)

    resolved = {Path(p).expanduser().resolve() for p in cfg.indexing.memory_dirs}
    assert fake.resolve() not in resolved
    assert cfg.indexing.auto_discover is False


# ---------------------------------------------------------------------------
# _detect_provider_dirs / _canonical_provider_dirs scope (narrowed per
# official provider docs).
# ---------------------------------------------------------------------------


def test_detect_provider_dirs_categories_are_fixed() -> None:
    """The wizard step iterates these categories — no surprise additions."""
    grouped = _cfg._detect_provider_dirs()
    assert set(grouped) == {"claude-memory", "claude-plans", "codex"}


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (r"C:\Users\foo\.claude\projects\abc\memory", "claude-memory"),
        (r"C:\Users\foo\.claude\plans", "claude-plans"),
        (r"C:\Users\foo\.codex\memories", "codex"),
        (r"C:\Users\foo\Documents\notes", "user"),
        (r"C:\Users\foo/.claude/plans", "claude-plans"),
        (r"C:\Users\foo\.claude\plans" + "\\", "claude-plans"),
        (r"\\server\share\.codex\memories", "codex"),
    ],
)
def test_categorize_memory_dir_accepts_windows_separators(
    path: str, expected: _cfg.ProviderCategory
) -> None:
    """Provider category matching is path-separator agnostic (#316)."""
    assert _cfg.categorize_memory_dir(path) == expected


def test_provider_index_conventions_cover_every_category() -> None:
    """Lock: every ProviderCategory declares an index convention.

    Mirrors the ``_PROVIDER_CATEGORY_PATTERNS`` vocabulary lock — a new
    category added to the ``ProviderCategory`` Literal without a matching
    convention trips the module-level assert at import. Pinned here too so
    the intent is visible in the test suite.
    """
    assert set(_cfg._PROVIDER_INDEX_CONVENTIONS) == _cfg._VALID_PROVIDER_CATEGORIES


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("claude-memory", frozenset({"MEMORY.md", "README.md"})),
        ("codex", frozenset({"README.md"})),
        ("claude-plans", frozenset()),
        ("user", frozenset()),
        ("unknown-category", frozenset()),
    ],
)
def test_index_excluded_filenames(category: str, expected: frozenset) -> None:
    """The centralized exclude set per category (replaces the constants that
    used to live in cli/ingest_cmd.py). Unknown categories index everything."""
    assert _cfg.index_excluded_filenames(category) == expected


def test_provider_index_file() -> None:
    assert _cfg.provider_index_file("claude-memory") == "MEMORY.md"
    assert _cfg.provider_index_file("codex") is None
    assert _cfg.provider_index_file("user") is None
    assert _cfg.provider_index_file("unknown-category") is None


def test_detect_provider_dirs_excludes_gemini(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Gemini (~/.gemini/) is intentionally out of scope — its memory is a
    single GEMINI.md file (doesn't fit the directory abstraction) and the
    parent dir contains oauth credentials. ``mm ingest gemini-memory``
    remains the supported path for Gemini users."""
    home = tmp_path / "home"
    (home / ".gemini").mkdir(parents=True)
    (home / ".gemini" / "GEMINI.md").write_text("# memory", encoding="utf-8")
    set_home(monkeypatch, home)

    grouped = _cfg._detect_provider_dirs()
    assert "gemini" not in grouped
    flat = _cfg._canonical_provider_dirs()
    assert all("gemini" not in str(d) for d in flat)


def test_detect_provider_dirs_filters_empty_claude_memory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Per-project ``memory/`` subdirs without any *.md files are skipped
    so empty session scaffolding doesn't get added to memory_dirs."""
    home = tmp_path / "home"
    proj_with = home / ".claude" / "projects" / "p1" / "memory"
    proj_without = home / ".claude" / "projects" / "p2" / "memory"
    proj_with.mkdir(parents=True)
    (proj_with / "MEMORY.md").write_text("# index", encoding="utf-8")
    proj_without.mkdir(parents=True)  # empty memory dir
    set_home(monkeypatch, home)

    grouped = _cfg._detect_provider_dirs()
    paths = {str(p) for p in grouped["claude-memory"]}
    assert str(proj_with) in paths
    assert str(proj_without) not in paths


def test_detect_provider_dirs_finds_claude_plans_and_codex(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``~/.claude/plans/`` and ``~/.codex/memories/`` land in their
    respective categories when present on disk."""
    home = tmp_path / "home"
    plans = home / ".claude" / "plans"
    codex = home / ".codex" / "memories"
    plans.mkdir(parents=True)
    codex.mkdir(parents=True)
    set_home(monkeypatch, home)

    grouped = _cfg._detect_provider_dirs()
    assert grouped["claude-plans"] == [plans]
    assert grouped["codex"] == [codex]


# ── #1522: env binding is MEMTOMEM_-prefixed only ──────────────────────────


def test_sub_configs_ignore_bare_env_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sub-configs are plain ``BaseModel``s: generic shell exports like
    ``API_KEY`` / ``ENABLED`` / ``MODEL`` must not bind, standalone or through
    ``Mem2MemConfig``'s ``default_factory`` path (#1522)."""
    monkeypatch.setenv("API_KEY", "bare-env-leak")
    monkeypatch.setenv("ENABLED", "true")
    monkeypatch.setenv("MODEL", "bare-model")

    from memtomem.config import EmbeddingConfig, SessionTraceConfig

    assert EmbeddingConfig().api_key == ""
    assert EmbeddingConfig().model == ""
    assert SessionTraceConfig().enabled is False

    cfg = Mem2MemConfig()
    assert cfg.embedding.api_key == ""
    assert cfg.session_trace.enabled is False


def test_nested_prefixed_env_still_binds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The documented ``MEMTOMEM_<SECTION>__<FIELD>`` surface keeps working
    after the sub-configs stopped being ``BaseSettings`` (#1522)."""
    monkeypatch.setenv("MEMTOMEM_EMBEDDING__API_KEY", "prefixed-ok")
    monkeypatch.setenv("MEMTOMEM_SESSION_TRACE__JSONL_ENABLED", "false")

    cfg = Mem2MemConfig()
    assert cfg.embedding.api_key == "prefixed-ok"
    assert cfg.session_trace.jsonl_enabled is False


def test_only_root_config_is_base_settings() -> None:
    """Guard: no sub-config may regrow a ``BaseSettings`` base — that would
    re-open the undocumented bare-env surface #1522 closed. New settings
    sections must inherit ``ConfigModel`` and hang off ``Mem2MemConfig``."""
    import inspect

    import pydantic
    from pydantic_settings import BaseSettings

    from memtomem.config import ConfigModel

    model_classes = {
        name: obj
        for name, obj in inspect.getmembers(_cfg, inspect.isclass)
        if issubclass(obj, pydantic.BaseModel) and obj.__module__ == _cfg.__name__
    }
    settings_classes = [n for n, o in model_classes.items() if issubclass(o, BaseSettings)]
    assert settings_classes == ["Mem2MemConfig"], settings_classes

    # Every other model must go through ConfigModel so it keeps the
    # BaseSettings-era strictness (extra="forbid", validate_default=True) —
    # a bare-BaseModel section would silently swallow config typos.
    non_strict = [
        n
        for n, o in model_classes.items()
        if n not in ("Mem2MemConfig", "ConfigModel") and not issubclass(o, ConfigModel)
    ]
    assert non_strict == [], non_strict


def test_sub_configs_keep_base_settings_strictness(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dropping the ``BaseSettings`` base must not relax validation: unknown
    keys still fail loudly (``extra="forbid"``), so an env typo like
    ``MEMTOMEM_EMBEDDING__TYPO`` or a stray key in a ``namespace.rules`` entry
    raises instead of being silently ignored (#1522)."""
    import pydantic

    from memtomem.config import NamespacePolicyRule

    monkeypatch.setenv("MEMTOMEM_EMBEDDING__TYPO", "bad")
    with pytest.raises(pydantic.ValidationError, match="typo"):
        Mem2MemConfig()
    monkeypatch.delenv("MEMTOMEM_EMBEDDING__TYPO")

    with pytest.raises(pydantic.ValidationError, match="bogus"):
        NamespacePolicyRule.model_validate({"path_glob": "notes/**", "namespace": "n", "bogus": 1})


def test_namespace_rules_survive_config_json_roundtrip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_json_default`` must serialize ``NamespacePolicyRule`` items as dicts
    (not ``str()`` fallback) now that rules are ``BaseModel``s — a missed
    isinstance sweep here corrupts ``config.json`` silently (#1522)."""
    from memtomem.config import NamespacePolicyRule, save_config_overrides

    override = tmp_path / "config.json"
    monkeypatch.setattr(_cfg, "_override_path", lambda: override)
    monkeypatch.setattr(_cfg, "_config_d_path", lambda: tmp_path / "config.d")

    cfg = Mem2MemConfig()
    rule = NamespacePolicyRule(path_glob="**/notes/**", namespace="claude:notes")
    cfg.namespace.rules = list(cfg.namespace.rules) + [rule]
    save_config_overrides(cfg)

    saved = json.loads(override.read_text(encoding="utf-8"))
    dumped_rules = saved["namespace"]["rules"]
    assert {"path_glob": "**/notes/**", "namespace": "claude:notes"} in dumped_rules
    assert all(isinstance(r, dict) for r in dumped_rules)

    fresh = Mem2MemConfig()
    load_config_overrides(fresh)
    assert any(
        r.path_glob == "**/notes/**" and r.namespace == "claude:notes"
        for r in fresh.namespace.rules
    )


class TestSearchConfigRrfWeightsValidation:
    """#2094: the issue's repro values must fail at construction."""

    @pytest.mark.parametrize(
        "bad",
        [
            [-1.0, 1.0],
            [float("nan"), 1.0],
            [float("inf"), 1.0],
            [0.0, 0.0],
            [1.0],
            [1.0, 1.0, 1.0],
            [True, False],
            [10**400, 1.0],
        ],
    )
    def test_invalid_pairs_raise(self, bad: list[float]) -> None:
        import pydantic

        from memtomem.config import SearchConfig

        with pytest.raises(pydantic.ValidationError):
            SearchConfig(rrf_weights=bad)

    def test_valid_pairs_construct(self) -> None:
        from memtomem.config import SearchConfig

        assert SearchConfig(rrf_weights=[0.0, 1.0]).rrf_weights == [0.0, 1.0]
        assert SearchConfig(rrf_weights=[2.0, 3.0]).rrf_weights == [2.0, 3.0]


class TestWriteEffectHelpers:
    """#2108: a write that lands in config.json is not the same as a write that takes effect."""

    def test_receipt_reads_the_file_snapshots_not_the_model(self) -> None:
        from memtomem.config import MISSING, SaveReceipt

        receipt = SaveReceipt(
            before={"search": {"default_top_k": 33}},
            after={"search": {"default_top_k": 44}},
        )
        assert receipt.pinned_before("search", "default_top_k") == 33
        assert receipt.pinned_after("search", "default_top_k") == 44
        # Absent key in a present section, and an absent section.
        assert receipt.pinned_after("search", "rrf_k") is MISSING
        assert receipt.pinned_after("decay", "enabled") is MISSING
        assert not receipt.pruned("search", "default_top_k")

    def test_receipt_reports_a_pruned_pin(self) -> None:
        from memtomem.config import SaveReceipt

        receipt = SaveReceipt(before={"search": {"default_top_k": 33}}, after={})
        assert receipt.pruned("search", "default_top_k")
        # A key that was never pinned is not "pruned" — it was simply absent.
        assert not receipt.pruned("search", "rrf_k")

    def test_receipt_distinguishes_pinned_null(self) -> None:
        """A pinned ``null`` is a value the file carries — not an absent key."""
        from memtomem.config import MISSING, SaveReceipt

        receipt = SaveReceipt(before={}, after={"search": {"default_top_k": None}})
        assert receipt.pinned_after("search", "default_top_k") is None
        assert receipt.pinned_after("search", "default_top_k") is not MISSING

    def test_save_returns_a_receipt_matching_the_file(self, override_path: Path) -> None:
        """End-to-end: the receipt is the file's real before/after, not a guess."""
        from memtomem.config import MISSING, Mem2MemConfig, save_config_overrides

        override_path.write_text(json.dumps({"search": {"default_top_k": 33}}))
        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        cfg.search.default_top_k = 44

        receipt = save_config_overrides(cfg)
        assert receipt.pinned_before("search", "default_top_k") == 33
        assert receipt.pinned_after("search", "default_top_k") == 44
        assert json.loads(override_path.read_text())["search"]["default_top_k"] == 44

        # Setting the default prunes the entry — the receipt says so.
        cfg.search.default_top_k = Mem2MemConfig().search.default_top_k
        receipt = save_config_overrides(cfg)
        assert receipt.pruned("search", "default_top_k")
        assert receipt.pinned_after("search", "default_top_k") is MISSING

    def test_env_var_owning_reports_the_variable_that_wins(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The helper answers "does env win?" — and env wins in any case (#2109)."""
        from memtomem.config import env_var_owning

        _clear_all_memtomem_env(monkeypatch)
        assert env_var_owning("search", "default_top_k") is None

        monkeypatch.setenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", "7")
        assert env_var_owning("search", "default_top_k") == "MEMTOMEM_SEARCH__DEFAULT_TOP_K"

    def test_env_var_owning_names_the_variable_as_the_environment_spells_it(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported name is what the user has to ``unset``, verbatim.

        On Windows ``os.environ`` normalises keys to uppercase, so there the
        canonical spelling is the only one that can come back.
        """
        from memtomem.config import env_var_owning

        _clear_all_memtomem_env(monkeypatch)
        monkeypatch.setenv("memtomem_search__default_top_k", "7")

        owning = env_var_owning("search", "default_top_k")
        assert owning is not None
        assert owning.upper() == "MEMTOMEM_SEARCH__DEFAULT_TOP_K"
        if sys.platform != "win32":
            assert owning == "memtomem_search__default_top_k"

    def test_env_var_owning_agrees_with_the_loader_on_a_lowercase_name(
        self, override_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Whatever the helper says must match who actually wins, end to end.

        Pinned to the exact winning value, not just to parity: a parity-only
        assertion still passes when both sides are wrong together.
        """
        from memtomem.config import env_var_owning

        _clear_all_memtomem_env(monkeypatch)
        monkeypatch.setenv("memtomem_search__default_top_k", "7")
        override_path.write_text(json.dumps({"search": {"default_top_k": 33}}))

        cfg = Mem2MemConfig()
        load_config_overrides(cfg)

        assert cfg.search.default_top_k == 7
        assert (env_var_owning("search", "default_top_k") is not None) == (
            cfg.search.default_top_k == 7
        )

    def test_env_var_owning_does_not_match_a_different_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memtomem.config import env_var_owning

        _clear_all_memtomem_env(monkeypatch)
        monkeypatch.setenv("MEMTOMEM_SEARCH__RRF_K", "60")
        assert env_var_owning("search", "default_top_k") is None

    def test_env_var_owning_does_not_match_a_name_that_merely_starts_the_same(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The case-insensitive scan compares whole names, not prefixes."""
        from memtomem.config import env_var_owning

        _clear_all_memtomem_env(monkeypatch)
        monkeypatch.setenv("memtomem_search__default_top_k_extra", "7")
        monkeypatch.setenv("xmemtomem_search__default_top_k", "7")
        assert env_var_owning("search", "default_top_k") is None


class TestEnvNameCaseInsensitivity:
    """#2109: pydantic reads env names case-insensitively; the loaders must too.

    pydantic-settings binds ``memtomem_search__default_top_k`` at construction
    (``case_sensitive`` defaults to false), but the loaders used to test exact
    uppercase membership before yielding — so the lowercase spelling was read
    and then silently overwritten by ``config.json``. Only an end-to-end test
    sees that: a unit test of the helper alone passed either way.
    """

    SPELLINGS = (
        "MEMTOMEM_SEARCH__DEFAULT_TOP_K",
        "memtomem_search__default_top_k",
        "Memtomem_Search__Default_Top_K",
    )

    @pytest.mark.parametrize("env_name", SPELLINGS)
    def test_env_beats_config_json_in_any_case(
        self, env_name: str, override_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_all_memtomem_env(monkeypatch)
        monkeypatch.setenv(env_name, "7")
        override_path.write_text(json.dumps({"search": {"default_top_k": 33}}), encoding="utf-8")

        cfg = Mem2MemConfig()
        load_config_overrides(cfg)

        assert cfg.search.default_top_k == 7

    @pytest.mark.parametrize("env_name", SPELLINGS)
    def test_env_beats_config_d_in_any_case(
        self, env_name: str, config_d_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _clear_all_memtomem_env(monkeypatch)
        monkeypatch.setenv(env_name, "7")
        (config_d_dir / "fragment.json").write_text(
            json.dumps({"search": {"default_top_k": 33}}), encoding="utf-8"
        )

        cfg = Mem2MemConfig()
        load_config_d(cfg)

        assert cfg.search.default_top_k == 7

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="os.environ normalises keys on Windows: two spellings cannot coexist",
    )
    def test_it_names_the_spelling_pydantic_actually_read(
        self, override_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """With colliding spellings, the helper must name the winner.

        pydantic-settings folds the environment into a case-normalised dict,
        so the later entry overwrites the earlier one. A helper that prefers
        the canonical spelling would tell the user to unset a variable that is
        not supplying the value.
        """
        import os

        from memtomem.config import env_var_owning

        _clear_all_memtomem_env(monkeypatch)
        monkeypatch.setenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", "11")
        monkeypatch.setenv("memtomem_search__default_top_k", "22")
        override_path.write_text(json.dumps({"search": {"default_top_k": 33}}), encoding="utf-8")

        cfg = Mem2MemConfig()
        load_config_overrides(cfg)

        owning = env_var_owning("search", "default_top_k")
        assert owning is not None
        # The named variable is the one holding the value in effect — whichever
        # spelling the environment happens to order last.
        assert int(os.environ[owning]) == cfg.search.default_top_k
        assert cfg.search.default_top_k != 33

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason=(
            "os.environ upper-cases keys on Windows, which folds U+017F to 'S': "
            "the name arrives canonical and pydantic does bind it"
        ),
    )
    def test_a_name_pydantic_does_not_read_does_not_displace_config_json(
        self, override_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fold the way pydantic folds, or the loaders yield to nobody.

        "MEMTOMEM_\u017fEARCH__DEFAULT_TOP_K" uppercases to the canonical name
        (U+017F LATIN SMALL LETTER LONG S uppercases to "S") but does not
        lowercase to it, so pydantic never reads it. An upper-fold in
        ``env_var_owning`` would still call it owning, and the loader would
        drop the persisted value in favour of a variable nothing bound —
        leaving the field at its default.

        POSIX only, and for the same reason the assertion is worth making
        there: Windows normalises the key to the canonical name on the way
        into ``os.environ``, so the divergence this pins cannot arise.
        """
        from memtomem.config import env_var_owning

        _clear_all_memtomem_env(monkeypatch)
        monkeypatch.setenv("MEMTOMEM_\u017fEARCH__DEFAULT_TOP_K", "7")
        override_path.write_text(json.dumps({"search": {"default_top_k": 33}}), encoding="utf-8")

        cfg = Mem2MemConfig()
        assert cfg.search.default_top_k != 7  # pydantic did not bind it
        load_config_overrides(cfg)

        assert env_var_owning("search", "default_top_k") is None
        assert cfg.search.default_top_k == 33

    def test_config_json_still_applies_when_no_spelling_is_exported(
        self, override_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The negative half: yielding to env must not become yielding always."""
        _clear_all_memtomem_env(monkeypatch)
        override_path.write_text(json.dumps({"search": {"default_top_k": 33}}), encoding="utf-8")

        cfg = Mem2MemConfig()
        load_config_overrides(cfg)

        assert cfg.search.default_top_k == 33


class TestMcpPersistenceSuffix:
    """#2108: `mem_config(persist=True)` must not claim a write the file lacks."""

    def test_claims_persistence_only_when_the_key_landed(self) -> None:
        from memtomem.config import SaveReceipt
        from memtomem.server.tools.status_config import _persistence_suffix

        receipt = SaveReceipt(before={}, after={"search": {"default_top_k": 44}})
        assert _persistence_suffix("search.default_top_k", receipt) == " (persisted to config.json)"

    def test_says_runtime_only_without_a_receipt(self) -> None:
        """persist=False never writes, so there is nothing to report on."""
        from memtomem.server.tools.status_config import _persistence_suffix

        assert _persistence_suffix("search.default_top_k", None) == (
            " (runtime only — not persisted)"
        )

    def test_flags_the_env_mask_even_when_the_key_landed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """persist=True promises the value survives a restart; env breaks that half."""
        from memtomem.config import SaveReceipt
        from memtomem.server.tools.status_config import _persistence_suffix

        receipt = SaveReceipt(before={}, after={"search": {"default_top_k": 44}})
        monkeypatch.setenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", "7")

        suffix = _persistence_suffix("search.default_top_k", receipt)
        assert "persisted to config.json" in suffix
        assert "MEMTOMEM_SEARCH__DEFAULT_TOP_K takes precedence" in suffix
        assert "a restart reads that variable" in suffix

    def test_names_the_env_var_when_the_delta_was_pruned(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memtomem.config import SaveReceipt
        from memtomem.server.tools.status_config import _persistence_suffix

        receipt = SaveReceipt(
            before={"search": {"default_top_k": 33}}, after={"search": {"rrf_k": 60}}
        )
        monkeypatch.setenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", "7")

        suffix = _persistence_suffix("search.default_top_k", receipt)
        assert "not written to config.json" in suffix
        assert "MEMTOMEM_SEARCH__DEFAULT_TOP_K takes precedence" in suffix

    def test_blames_a_lower_layer_when_no_env_var_is_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No env var to name — say "a lower layer", not "the default".

        A config.d fragment supplying the value prunes the delta too, so
        naming the default would be a guess (#2108 review).
        """
        from memtomem.config import SaveReceipt
        from memtomem.server.tools.status_config import _persistence_suffix

        monkeypatch.delenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", raising=False)

        suffix = _persistence_suffix("search.default_top_k", SaveReceipt(before={}, after={}))
        assert "not written to config.json" in suffix
        assert "a lower layer (default or config.d)" in suffix
        assert "MEMTOMEM_" not in suffix


# ---------------------------------------------------------------------------
# assign_section_fields — the shared mutation primitive (issue #2110)
# ---------------------------------------------------------------------------


class TestAssignSectionFields:
    """The one place ``mm config set`` / ``mem_config`` / ``PATCH /api/config``
    mutate a config section, so all three enforce the section's cross-field
    invariants instead of persisting a combination every later load reverts.
    """

    def test_rejects_and_restores_a_cross_field_invalid_value(self) -> None:
        cfg = Mem2MemConfig()
        default_max = cfg.indexing.max_chunk_tokens

        with pytest.raises(ValueError) as exc:
            assign_section_fields(cfg.indexing, {"max_chunk_tokens": 64})

        assert "must be <= max_chunk_tokens" in str(exc.value)
        # The "Value error, " prefix pydantic adds is stripped for display.
        assert "Value error" not in str(exc.value)
        assert cfg.indexing.max_chunk_tokens == default_max

    def test_valid_update_applies_and_returns_old_values(self) -> None:
        cfg = Mem2MemConfig()
        old = assign_section_fields(cfg.search, {"default_top_k": 20})
        assert old == {"default_top_k": Mem2MemConfig().search.default_top_k}
        assert cfg.search.default_top_k == 20

    @pytest.mark.parametrize(
        "updates",
        [
            {"min_chunk_tokens": 600, "max_chunk_tokens": 1024},
            {"max_chunk_tokens": 1024, "min_chunk_tokens": 600},
        ],
        ids=["min-first", "max-first"],
    )
    def test_pair_legal_together_applies_in_either_key_order(
        self, updates: dict[str, object]
    ) -> None:
        """A per-key check would refuse the min-first spelling: 600 is above the
        still-default max of 512. The section is validated once, as a whole."""
        cfg = Mem2MemConfig()
        assign_section_fields(cfg.indexing, dict(updates))
        assert cfg.indexing.min_chunk_tokens == 600
        assert cfg.indexing.max_chunk_tokens == 1024

    def test_failed_multi_key_update_restores_every_key(self) -> None:
        cfg = Mem2MemConfig()
        before = (
            cfg.indexing.min_chunk_tokens,
            cfg.indexing.max_chunk_tokens,
            cfg.indexing.target_chunk_tokens,
        )

        with pytest.raises(ValueError):
            assign_section_fields(
                cfg.indexing,
                {"min_chunk_tokens": 600, "max_chunk_tokens": 400, "target_chunk_tokens": 300},
            )

        assert (
            cfg.indexing.min_chunk_tokens,
            cfg.indexing.max_chunk_tokens,
            cfg.indexing.target_chunk_tokens,
        ) == before

    def test_touched_key_is_checked_even_when_it_equals_the_default(self) -> None:
        """``exclude_defaults`` drops untouched defaults from the payload; the
        touched keys are overlaid back, so setting a field *to* its default
        still participates in the invariant."""
        cfg = Mem2MemConfig()
        default_max = cfg.indexing.max_chunk_tokens
        cfg.indexing.min_chunk_tokens = default_max + 100

        with pytest.raises(ValueError, match="must be <= max_chunk_tokens"):
            assign_section_fields(cfg.indexing, {"max_chunk_tokens": default_max})

    def test_survives_warnings_as_errors_on_a_deprecated_defaulted_field(self) -> None:
        """``rerank.top_k`` is defaulted and deprecated; validating a full dump
        re-fires its ``mode="before"`` migration, which is a traceback under
        ``PYTHONWARNINGS=error`` (#2108 review)."""
        cfg = Mem2MemConfig()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            assign_section_fields(cfg.rerank, {"oversample": 3.0})
        assert cfg.rerank.oversample == 3.0
