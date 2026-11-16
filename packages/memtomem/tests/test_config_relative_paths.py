"""A shared daemon refuses relative configured paths; every other process keeps them (#3381)."""

from __future__ import annotations

import typing
from pathlib import Path

import pytest
from pydantic import BaseModel

import memtomem.config as config_module
from memtomem.config import (
    Mem2MemConfig,
    RelativeConfigPathError,
    assign_section_fields,
    check_relative_config_paths,
    relative_config_paths,
)
from memtomem.embedding.profiles import E5_TOKENIZER

CHECKED = [
    (section_cls, field)
    for section_cls, fields in config_module._RELATIVE_PATH_FIELDS.items()
    for field in fields
]

# Path-like by type or name, and deliberately not checked. Empty today:
# ``namespace.rules[].path_glob`` is a glob (relative globs are valid) and does
# not match the name rule, so it needs no entry.
EXEMPT: set[tuple[str, str]] = set()


@pytest.fixture
def refusing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config_module, "_refuse_relative_paths", True)


def _section_of(config: Mem2MemConfig, section_cls: type) -> BaseModel:
    return getattr(config, config_module._SECTION_NAMES[section_cls])


def _relative_value(section_cls: type, field: str) -> object:
    current = getattr(_section_of(Mem2MemConfig(), section_cls), field)
    return [Path("relative")] if isinstance(current, list) else "relative"


@pytest.mark.parametrize("section_cls, field", CHECKED, ids=lambda v: getattr(v, "__name__", v))
def test_each_checked_field_is_detected(section_cls: type, field: str) -> None:
    config = Mem2MemConfig()
    setattr(_section_of(config, section_cls), field, _relative_value(section_cls, field))
    assert relative_config_paths(config) == [f"{config_module._SECTION_NAMES[section_cls]}.{field}"]


def test_defaults_have_no_relative_path() -> None:
    assert relative_config_paths(Mem2MemConfig()) == []


@pytest.mark.parametrize(
    "field, value",
    [
        ("chunk_tokenizer_path", ""),
        ("chunk_tokenizer_path", E5_TOKENIZER),
        ("index_masking_manifest_path", ""),
        ("chunk_tokenizer_path", "~/tokenizer.json"),
    ],
)
def test_values_that_are_not_relative_paths(field: str, value: str) -> None:
    config = Mem2MemConfig()
    setattr(config.indexing, field, value)
    assert relative_config_paths(config) == []


def test_another_model_id_is_a_path_to_the_tokenizer_resolver() -> None:
    config = Mem2MemConfig()
    config.indexing.chunk_tokenizer_path = "model:other@1"
    assert relative_config_paths(config) == ["indexing.chunk_tokenizer_path"]


@pytest.mark.parametrize("store", [":memory:", "file:x.db?mode=memory"])
def test_unnameable_stores_are_not_relative_paths(store: str) -> None:
    config = Mem2MemConfig()
    config.storage.sqlite_path = Path(store)
    assert relative_config_paths(config) == []


def test_inert_outside_a_daemon() -> None:
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [Path(".")]
    check_relative_config_paths(config)  # no flag, no refusal
    assign_section_fields(config.session_trace, {"jsonl_path": Path("x.jsonl")})
    assert config.session_trace.jsonl_path == Path("x.jsonl")


def test_check_refuses_in_a_daemon(refusing: None) -> None:
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [Path(".")]
    with pytest.raises(RelativeConfigPathError, match="indexing.memory_dirs"):
        check_relative_config_paths(config)


def test_assignment_refuses_and_restores_in_a_daemon(refusing: None) -> None:
    section = Mem2MemConfig().session_trace
    before = section.jsonl_path
    with pytest.raises(ValueError, match="session_trace.jsonl_path"):
        assign_section_fields(section, {"jsonl_path": Path("x.jsonl")})
    assert section.jsonl_path == before


def test_build_fresh_config_refuses_in_a_daemon(
    refusing: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.config_signature import build_fresh_config

    monkeypatch.setenv("MEMTOMEM_INDEXING__MEMORY_DIRS", '["relative-dir"]')
    with pytest.raises(RelativeConfigPathError, match="indexing.memory_dirs"):
        build_fresh_config(migrate=False, strict_overrides=False)


def _path_like_fields(model: type[BaseModel], seen: set[type]) -> list[tuple[str, str]]:
    found: list[tuple[str, str]] = []
    if model in seen:
        return found
    seen.add(model)
    for name, info in model.model_fields.items():
        annotation = info.annotation
        nested = [
            arg
            for arg in (annotation, *typing.get_args(annotation))
            if isinstance(arg, type) and issubclass(arg, BaseModel)
        ]
        for inner in typing.get_args(annotation):
            nested.extend(
                arg
                for arg in typing.get_args(inner)
                if isinstance(arg, type) and issubclass(arg, BaseModel)
            )
        for sub in nested:
            found.extend(_path_like_fields(sub, seen))
        if nested:
            continue
        by_type = "Path" in str(annotation)
        by_name = name.endswith(("_path", "_dir", "_dirs", "_file"))
        if by_type or by_name:
            found.append((model.__name__, name))
    return found


def test_every_path_like_field_is_classified() -> None:
    """A new path-like config field fails here until it is checked or exempted."""
    checked = {(cls.__name__, field) for cls, field in CHECKED}
    unclassified = {
        entry
        for entry in _path_like_fields(Mem2MemConfig, set())
        if entry not in checked and entry not in EXEMPT
    }
    assert unclassified == set()


def test_the_walk_reaches_models_inside_lists() -> None:
    seen: set[type] = set()
    _path_like_fields(Mem2MemConfig, seen)
    assert config_module.NamespacePolicyRule in seen


def test_the_classification_has_no_stale_entries() -> None:
    present = set(_path_like_fields(Mem2MemConfig, set()))
    checked = {(cls.__name__, field) for cls, field in CHECKED}
    assert checked <= present
    assert EXEMPT <= present


def test_an_uncheckable_path_is_refused_and_rolled_back(refusing: None) -> None:
    section = Mem2MemConfig().session_trace
    before = section.jsonl_path
    with pytest.raises(ValueError, match="cannot be checked"):
        assign_section_fields(section, {"jsonl_path": Path("~mmd-no-such-user-72ac/t.jsonl")})
    assert section.jsonl_path == before
