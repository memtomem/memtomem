"""``create_components()`` loads ambient config the way the MCP server does (#2667).

``mm index``, every other CLI command that opens components, and ``mm web`` call
``create_components()`` with no config and let it load ``config.d`` and
``config.json``. That load has to match the canonical builder: chunk budgets
are validated against the embedding profile the *final* config selects, so a
``config.d`` chunk setting must survive ``config.json`` choosing E5.

The tests stop component construction at the budget check, which receives the
exact config the components are built from — no storage, no embedder, no model.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import pytest

from memtomem.config_signature import build_fresh_config
from memtomem.embedding.profiles import PROFILE_INDEXING_FIELDS
from memtomem.runtime.components import create_components

from .helpers import set_home

_E5 = {"provider": "onnx", "model": "multilingual-e5-small", "dimension": 384}


class _Captured(Exception):
    """Raised by the budget-check stand-in once it has recorded the config."""


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_home(monkeypatch, tmp_path)
    for name in list(os.environ):
        if name.upper().startswith("MEMTOMEM_"):
            monkeypatch.delenv(name)
    (tmp_path / ".memtomem" / "config.d").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list:
    import memtomem.chunking.bounded as bounded

    seen: list = []

    def _stop(config, previous=None):
        seen.append(config)
        raise _Captured

    monkeypatch.setattr(bounded, "validate_budget_configuration", _stop)
    return seen


def _write(path: Path, data: object) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _components_config(captured: list):
    with pytest.raises(_Captured):
        asyncio.run(create_components())
    assert len(captured) == 1
    return captured[0]


def _profile(config) -> dict:
    return {f: getattr(config.indexing, f) for f in PROFILE_INDEXING_FIELDS}


@pytest.mark.parametrize("layer", ["config.json", "config.d", "env"])
def test_profile_matches_canonical_builder_for_each_layer(home, captured, monkeypatch, layer):
    dot = home / ".memtomem"
    if layer == "config.json":
        _write(dot / "config.json", {"embedding": _E5})
    elif layer == "config.d":
        _write(dot / "config.d" / "10-model.json", {"embedding": _E5})
    else:
        monkeypatch.setenv("MEMTOMEM_EMBEDDING", json.dumps(_E5))

    config = _components_config(captured)

    assert config.indexing.hard_max_chunk_tokens == 384
    assert _profile(config) == _profile(build_fresh_config(migrate=False))


def test_fragment_chunk_setting_survives_e5_chosen_in_config_json(home, captured):
    """The #2667 case: the fragment is validated against E5, not generic defaults.

    Validated against the generic ``target_chunk_tokens`` (384) the fragment's
    320 was rejected and dropped, so the stack ran with 384.
    """
    dot = home / ".memtomem"
    _write(dot / "config.d" / "10-chunk.json", {"indexing": {"max_chunk_tokens": 320}})
    _write(dot / "config.json", {"embedding": _E5})

    config = _components_config(captured)

    assert config.indexing.max_chunk_tokens == 320
    assert _profile(config) == _profile(build_fresh_config(migrate=False))


def test_malformed_config_json_is_still_tolerated(home, captured):
    (home / ".memtomem" / "config.json").write_text("{not json", encoding="utf-8")

    config = _components_config(captured)

    assert (
        config.embedding.provider
        == build_fresh_config(migrate=False, strict_overrides=False).embedding.provider
    )


def test_legacy_auto_discover_config_is_still_migrated(home, captured):
    path = home / ".memtomem" / "config.json"
    _write(path, {"embedding": {"provider": "none"}})

    _components_config(captured)

    assert json.loads(path.read_text(encoding="utf-8"))["indexing"]["auto_discover"] is False


def test_caller_supplied_config_keeps_its_base(home, captured, tmp_path):
    from memtomem.config import Mem2MemConfig

    base = Mem2MemConfig()
    base.storage.sqlite_path = tmp_path / "caller.db"

    with pytest.raises(_Captured):
        asyncio.run(create_components(base))

    assert captured[0].storage.sqlite_path == tmp_path / "caller.db"
