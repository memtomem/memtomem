"""Tests for the pure helpers in tools/retrieval-eval/calibrate_portfolio.py.

The end-to-end calibration (`calibrate()`) builds an ONNX index over
the full 6-topic corpus and is run by the reviewer via the CLI —
excluded here to keep the test suite fast. These tests cover the
small pure functions that pin down the scoring/aggregation logic.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CALIBRATE_PATH = _REPO_ROOT / "tools" / "retrieval-eval" / "calibrate_portfolio.py"
_VALIDATOR_PATH = _REPO_ROOT / "tools" / "retrieval-eval" / "drift_validator.py"
_PORTFOLIO_PATH = _REPO_ROOT / "tools" / "retrieval-eval" / "query_portfolio.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def calibrate_mod() -> ModuleType:
    # Order matters: drift_validator is imported by calibrate_portfolio
    # at call time, query_portfolio is loaded by calibrate() at runtime
    # but not for the pure-helper tests below.
    _load("drift_validator", _VALIDATOR_PATH)
    _load("query_portfolio", _PORTFOLIO_PATH)
    return _load("calibrate_portfolio", _CALIBRATE_PATH)


# ---- build_relevance ----


def test_build_relevance_primary_match(calibrate_mod):
    tagged = [
        calibrate_mod.TaggedChunk(
            source_file=Path("/a.md"),
            heading="H1",
            primary="caching/eviction",
            secondary=(),
            lang="en",
        ),
        calibrate_mod.TaggedChunk(
            source_file=Path("/b.md"),
            heading="H2",
            primary="caching/stampede",
            secondary=(),
            lang="en",
        ),
    ]
    primary, graded = calibrate_mod.build_relevance(tagged, frozenset({"caching/eviction"}), "en")
    assert primary == {(Path("/a.md"), "H1")}
    assert graded == {(Path("/a.md"), "H1"): 1.0}


def test_build_relevance_secondary_gets_half_score(calibrate_mod):
    tagged = [
        calibrate_mod.TaggedChunk(
            source_file=Path("/a.md"),
            heading="H1",
            primary="postgres/replication",
            secondary=("observability/metrics",),
            lang="en",
        ),
    ]
    primary, graded = calibrate_mod.build_relevance(
        tagged, frozenset({"observability/metrics"}), "en"
    )
    assert primary == set(), "secondary-only match must not appear in primary-relevant"
    assert graded == {(Path("/a.md"), "H1"): 0.5}


def test_build_relevance_filters_by_language(calibrate_mod):
    tagged = [
        calibrate_mod.TaggedChunk(
            source_file=Path("/ko.md"),
            heading="H",
            primary="caching/eviction",
            secondary=(),
            lang="ko",
        ),
        calibrate_mod.TaggedChunk(
            source_file=Path("/en.md"),
            heading="H",
            primary="caching/eviction",
            secondary=(),
            lang="en",
        ),
    ]
    primary, graded = calibrate_mod.build_relevance(tagged, frozenset({"caching/eviction"}), "en")
    assert Path("/ko.md") not in {k[0] for k in graded}
    assert Path("/en.md") in {k[0] for k in graded}


def test_build_relevance_primary_beats_secondary_when_both_match(calibrate_mod):
    # primary-in-targets short-circuits secondary scoring
    tagged = [
        calibrate_mod.TaggedChunk(
            source_file=Path("/a.md"),
            heading="H",
            primary="caching/eviction",
            secondary=("caching/eviction", "postgres/indexing"),
            lang="en",
        ),
    ]
    primary, graded = calibrate_mod.build_relevance(
        tagged,
        frozenset({"caching/eviction", "postgres/indexing"}),
        "en",
    )
    assert graded == {(Path("/a.md"), "H"): 1.0}
    assert primary == {(Path("/a.md"), "H")}


# ---- compute_floors ----


def test_compute_floors_rounds_mean_times_factor_to_2dp(calibrate_mod):
    samples = {
        ("en", "direct", "recall@10"): [1.0, 0.8, 0.6],  # mean 0.8; × 0.9 = 0.72
        ("ko", "direct", "recall@10"): [0.5, 0.5, 0.5],  # mean 0.5; × 0.9 = 0.45
    }
    floors = calibrate_mod.compute_floors(samples, factor=0.9)
    assert floors[("en", "direct", "recall@10")] == 0.72
    assert floors[("ko", "direct", "recall@10")] == 0.45


def test_compute_floors_custom_factor(calibrate_mod):
    samples = {("en", "x", "m"): [1.0, 1.0]}  # mean 1.0
    assert calibrate_mod.compute_floors(samples, factor=0.8) == {("en", "x", "m"): 0.8}
    assert calibrate_mod.compute_floors(samples, factor=0.75) == {("en", "x", "m"): 0.75}


def test_compute_floors_empty_samples_produce_zero(calibrate_mod):
    samples: dict = {("en", "x", "m"): []}
    assert calibrate_mod.compute_floors(samples) == {("en", "x", "m"): 0.0}


# ---- _retrieved_key ----


def test_retrieved_key_strips_markdown_heading_prefix(calibrate_mod):
    result = SimpleNamespace(
        chunk=SimpleNamespace(
            metadata=SimpleNamespace(
                source_file=Path("/a.md"),
                heading_hierarchy=("## Set Redis maxmemory-policy",),
            )
        )
    )
    assert calibrate_mod._retrieved_key(result) == (
        Path("/a.md").resolve(),
        "Set Redis maxmemory-policy",
    )


def test_retrieved_key_handles_missing_headings(calibrate_mod):
    result = SimpleNamespace(
        chunk=SimpleNamespace(
            metadata=SimpleNamespace(
                source_file=Path("/a.md"),
                heading_hierarchy=(),
            )
        )
    )
    assert calibrate_mod._retrieved_key(result) == (Path("/a.md").resolve(), "")


def test_retrieved_key_takes_deepest_heading(calibrate_mod):
    # Match `drift_validator.parse_fixture`'s leaf-heading semantics.
    result = SimpleNamespace(
        chunk=SimpleNamespace(
            metadata=SimpleNamespace(
                source_file=Path("/a.md"),
                heading_hierarchy=("# Doc", "## Section"),
            )
        )
    )
    assert calibrate_mod._retrieved_key(result)[1] == "Section"


# ---- collect_tagged_chunks (live corpus) ----


def test_collect_tagged_chunks_counts_match_corpus(calibrate_mod):
    """Live-corpus spot check: 192 chunks across the 6 topics."""
    tagged = calibrate_mod.collect_tagged_chunks()
    assert len(tagged) == 192
    # 96 per language
    by_lang: dict[str, int] = {"ko": 0, "en": 0}
    for t in tagged:
        by_lang[t.lang] += 1
    assert by_lang == {"ko": 96, "en": 96}


# ---- _genre_of ----


def test_genre_of_recognizes_four_genres(calibrate_mod):
    assert calibrate_mod._genre_of(Path("/a/runbook.md")) == "runbook"
    assert calibrate_mod._genre_of(Path("/a/postmortem.md")) == "postmortem"
    assert calibrate_mod._genre_of(Path("/a/adr.md")) == "adr"
    assert calibrate_mod._genre_of(Path("/a/troubleshooting.md")) == "troubleshooting"
    assert calibrate_mod._genre_of(Path("/a/README.md")) is None
    assert calibrate_mod._genre_of(Path("/a/unknown.md")) is None
