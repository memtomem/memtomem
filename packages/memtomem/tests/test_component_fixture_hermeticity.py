"""The shared component fixtures do not take the developer's ``config.d`` (#3647).

``components``, ``bm25_only_components`` and ``onnx_components`` in ``conftest.py``
each build their own ``Mem2MemConfig``. ``create_components`` layers
``~/.memtomem/config.d/*.json`` onto a caller-supplied config unless it is told
``load_ambient_config=False``, and stubbing ``load_config_overrides`` (what the
fixtures used to rely on) leaves that layer in place. CI has no ``config.d``, so
only a developer with a fragment saw their tests run on a different config.

Tests that build their own stack call ``isolate_memtomem_env`` and then
``create_components(config)`` with the default ``load_ambient_config=True``;
that helper keeps ``config.d`` out of reach for them (#3667).

Before the component fixture under test is built, the autouse fixture here puts
a fragment in a redirected ``config.d`` and checks that ``load_config_d``
applies it, so a pass cannot come from a fragment nobody reads. Each
``config.d`` test then checks that its config still holds the default.

The last test is about what ``components`` leaves behind (#3668): the
``MEMTOMEM_*`` names it sets are restored when building or closing the stack
raises.
"""

from __future__ import annotations

import inspect
import json
import os

import pytest

from memtomem.config import Mem2MemConfig, load_config_d
from memtomem.embedding.onnx import OnnxEmbedder
from memtomem.runtime.components import close_components, create_components

from . import conftest as shared_fixtures
from .helpers import isolate_config_paths, isolate_memtomem_env

_PROBE_VALUE = 9.5
# The declared default, read without the environment: a module-level
# ``Mem2MemConfig()`` runs at collection, before the session scrub of
# ``MEMTOMEM_*``, and would take a developer's value as the baseline.
_DEFAULT_VALUE = Mem2MemConfig.without_environment().session_summary.expansion_score_threshold


@pytest.fixture(autouse=True)
def _config_d_fragment(monkeypatch, tmp_path_factory):
    """A ``config.d`` fragment in place before any component fixture is built.

    Autouse fixtures are set up before the ones a test requests, so a fixture
    under test that did load ambient config would find this probe, not the
    developer's directory. The witness runs here, before that fixture:
    ``bm25_only_components`` calls ``isolate_memtomem_env``, which moves
    ``config.d`` away again.
    """
    root = isolate_config_paths(monkeypatch, tmp_path_factory.mktemp("ambient"))
    (root / "config.d").mkdir()
    (root / "config.d" / "50-probe.json").write_text(
        json.dumps({"session_summary": {"expansion_score_threshold": _PROBE_VALUE}}),
        encoding="utf-8",
    )
    _assert_fragment_is_live()


@pytest.fixture(autouse=True)
def _no_onnx_model_load(monkeypatch):
    """No test here may load the ONNX model; these tests check config only.

    Autouse, so the refusal is installed before ``onnx_components`` is built
    and stays until after it is torn down. Attempts are recorded as well as
    refused, and checked after teardown, because the component factory's
    best-effort cleanup could swallow the refusal's exception.
    """
    attempts: list[str] = []

    def _refuse(self):
        attempts.append("_get_model")
        raise AssertionError("a component fixture loaded the ONNX model in a config-only test")

    monkeypatch.setattr(OnnxEmbedder, "_get_model", _refuse)
    yield
    assert attempts == []


def _assert_fragment_is_live() -> None:
    witness = Mem2MemConfig()
    load_config_d(witness)
    assert witness.session_summary.expansion_score_threshold == _PROBE_VALUE
    assert _PROBE_VALUE != _DEFAULT_VALUE


def test_components_ignores_config_d(components):
    assert components.config.session_summary.expansion_score_threshold == _DEFAULT_VALUE


def test_bm25_only_components_ignores_config_d(bm25_only_components):
    comp, _mem_dir = bm25_only_components
    assert comp.config.session_summary.expansion_score_threshold == _DEFAULT_VALUE


def test_onnx_components_ignores_config_d(onnx_components):
    comp, _mem_dir = onnx_components
    assert comp.config.session_summary.expansion_score_threshold == _DEFAULT_VALUE


async def test_isolate_memtomem_env_hides_config_d(monkeypatch, tmp_path):
    """A stack built after the helper, with ``load_ambient_config`` left on (#3667)."""
    isolate_memtomem_env(monkeypatch)

    direct = Mem2MemConfig()
    load_config_d(direct)
    assert direct.session_summary.expansion_score_threshold == _DEFAULT_VALUE

    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "stack.db"
    config.indexing.memory_dirs = [mem_dir]
    config.embedding.dimension = 1024
    config.search.enable_dense = False
    comp = await create_components(config)
    try:
        assert comp.config.session_summary.expansion_score_threshold == _DEFAULT_VALUE
    finally:
        await close_components(comp)


@pytest.mark.parametrize("failing", ["setup", "teardown"])
async def test_components_restores_environment_when_it_raises(failing, monkeypatch, tmp_path):
    """Drive the fixture's generator by hand so the raise lands inside the test."""
    expected = {
        "MEMTOMEM_STORAGE__SQLITE_PATH": str(tmp_path / "test.db"),
        "MEMTOMEM_INDEXING__MEMORY_DIRS": json.dumps([str(tmp_path / "memories")]),
        "MEMTOMEM_EMBEDDING__MODEL": "bge-m3",
        "MEMTOMEM_EMBEDDING__DIMENSION": "1024",
    }
    # Set through the test's own monkeypatch, so a failing run cannot leak either.
    for name in expected:
        monkeypatch.setenv(name, "before-the-fixture")
    seen: dict[str, str | None] = {}

    async def create(config, **kwargs):
        seen.update({name: os.environ.get(name) for name in expected})
        if failing == "setup":
            raise RuntimeError("setup failed")
        return object()

    async def close(comp):
        raise RuntimeError("teardown failed")

    monkeypatch.setattr(shared_fixtures, "create_components", create)
    monkeypatch.setattr(shared_fixtures, "close_components", close)
    fixture_function = inspect.unwrap(shared_fixtures.components)

    # This context stands in for pytest finalizing the fixture's ``monkeypatch``.
    with pytest.MonkeyPatch.context() as fixture_monkeypatch:
        available = {"tmp_path": tmp_path, "monkeypatch": fixture_monkeypatch}
        generator = fixture_function(
            **{name: available[name] for name in inspect.signature(fixture_function).parameters}
        )
        if failing == "teardown":
            await anext(generator)
        with pytest.raises(RuntimeError, match=f"{failing} failed"):
            await anext(generator)

    assert seen == expected
    assert {name: os.environ.get(name) for name in expected} == dict.fromkeys(
        expected, "before-the-fixture"
    )
