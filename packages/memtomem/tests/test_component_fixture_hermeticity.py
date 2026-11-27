"""The shared component fixtures do not take the developer's ``config.d`` (#3647).

``components``, ``bm25_only_components`` and ``onnx_components`` in ``conftest.py``
each build their own ``Mem2MemConfig``. ``create_components`` layers
``~/.memtomem/config.d/*.json`` onto a caller-supplied config unless it is told
``load_ambient_config=False``, and stubbing ``load_config_overrides`` (what the
fixtures used to rely on) leaves that layer in place. CI has no ``config.d``, so
only a developer with a fragment saw their tests run on a different config.

Each test here puts a fragment in a redirected ``config.d`` before the fixture
under test is built, checks that the fragment is one ``load_config_d`` applies
(so a pass cannot come from a fragment nobody reads), and then checks the
fixture's config still holds the default.
"""

from __future__ import annotations

import json

import pytest

from memtomem.config import Mem2MemConfig, load_config_d
from memtomem.embedding.onnx import OnnxEmbedder

from .helpers import isolate_config_paths

_PROBE_VALUE = 9.5
# The declared default, read without the environment: a module-level
# ``Mem2MemConfig()`` runs at collection, before the session scrub of
# ``MEMTOMEM_*``, and would take a developer's value as the baseline.
_DEFAULT_VALUE = Mem2MemConfig.without_environment().session_summary.expansion_score_threshold


@pytest.fixture(autouse=True)
def _config_d_fragment(monkeypatch, tmp_path_factory):
    """A ``config.d`` fragment in place before any component fixture is built.

    Autouse fixtures are set up before the ones a test requests, so the
    fixture under test reads this directory, not the developer's.
    """
    root = isolate_config_paths(monkeypatch, tmp_path_factory.mktemp("ambient"))
    (root / "config.d").mkdir()
    (root / "config.d" / "50-probe.json").write_text(
        json.dumps({"session_summary": {"expansion_score_threshold": _PROBE_VALUE}}),
        encoding="utf-8",
    )


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
    _assert_fragment_is_live()
    assert components.config.session_summary.expansion_score_threshold == _DEFAULT_VALUE


def test_bm25_only_components_ignores_config_d(bm25_only_components):
    comp, _mem_dir = bm25_only_components
    _assert_fragment_is_live()
    assert comp.config.session_summary.expansion_score_threshold == _DEFAULT_VALUE


def test_onnx_components_ignores_config_d(onnx_components):
    comp, _mem_dir = onnx_components
    _assert_fragment_is_live()
    assert comp.config.session_summary.expansion_score_threshold == _DEFAULT_VALUE
