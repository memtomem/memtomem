"""Multi-candidate experiment assembly tests (#1844, PR-2).

Pure-dict tests — no storage, no pipeline. Replay reports are assembled through
the real metric + fingerprint functions so they self-check, then
``assemble_experiment`` compares/gates them. Covers name ordering, duplicate /
empty rejection, fingerprint-drift rejection, per-candidate independent gate
verdicts, the top-level determinism roll-up, and byte-stable serialization.
"""

from __future__ import annotations

import copy

import pytest

from memtomem.errors import EvalCaseError
from memtomem.quality import metrics
from memtomem.quality.experiment import (
    EXPERIMENT_KIND,
    EXPERIMENT_SCHEMA_VERSION,
    ProfileRun,
    assemble_experiment,
    serialize_experiment,
)
from memtomem.quality.fingerprints import case_set_fingerprint
from memtomem.quality.gate import load_policy
from memtomem.quality.replay import (
    REPLAY_REPORT_KIND,
    REPLAY_REPORT_SCHEMA_VERSION,
    report_case_to_fingerprint_input,
)

_STAGE_OUTCOME_KEYS = (
    "bm25_error",
    "dense_error",
    "dense_suppressed_mismatch",
    "expansion_failed",
    "rerank_fallback",
    "rescue_failed",
)


def _case(case_id: str, *, retrieved: list[str], relevant: list[str], top_k: int = 5) -> dict:
    rel_set = set(relevant)
    gains = {h: 1.0 for h in rel_set}
    return {
        "case_id": case_id,
        "name": None,
        "version": 1,
        "status": "active",
        "query_text": "q",
        "top_k": top_k,
        "filters": {"namespace": None, "scope": None},
        "stale": {"profile": None, "corpus": None, "index": None},
        "flags": [],
        "labels": {"relevant": sorted(rel_set), "not_relevant": []},
        "retrieved": [
            {"content_hash": h, "score": 1.0 - i * 0.01, "rank": i, "source": "bm25"}
            for i, h in enumerate(retrieved, start=1)
        ],
        "metrics": {
            "hit_rate": metrics.hit_rate_at_k(retrieved, rel_set, top_k),
            "reciprocal_rank": metrics.reciprocal_rank_at_k(retrieved, rel_set, top_k),
            "recall_labeled": metrics.recall_labeled_at_k(retrieved, rel_set, top_k),
            "ndcg": metrics.ndcg_at_k(retrieved, gains, top_k),
            "precision": metrics.precision_at_k(retrieved, rel_set, set(), top_k),
        },
        "stage_outcomes": {k: False for k in _STAGE_OUTCOME_KEYS},
        "included_in_aggregate": True,
    }


def _report(
    cases: list[dict],
    *,
    profile: str = "prof-1",
    corpus: str = "corp-1",
    index: str = "idx-1",
    case_set: str | None = None,
    as_of: int = 1000,
    deterministic: bool = True,
) -> dict:
    cases = sorted(cases, key=lambda c: c["case_id"])
    cs = (
        case_set
        if case_set is not None
        else case_set_fingerprint([report_case_to_fingerprint_input(c) for c in cases])
    )
    evaluated = sum(1 for c in cases if c["included_in_aggregate"])
    return {
        "schema_version": REPLAY_REPORT_SCHEMA_VERSION,
        "kind": REPLAY_REPORT_KIND,
        "as_of_unix": as_of,
        "deterministic": deterministic,
        "nondeterministic_stages": [] if deterministic else ["query_expansion_llm"],
        "fingerprints": {"profile": profile, "corpus": corpus, "index": index, "case_set": cs},
        "profile_knobs": {"decay": {"enabled": False}},
        "counts": {"replayed": len(cases)},
        "aggregate": {
            "mean_hit_rate": metrics.mean(c["metrics"]["hit_rate"] for c in cases),
            "mrr": metrics.mean(c["metrics"]["reciprocal_rank"] for c in cases),
            "mean_recall_labeled": metrics.mean(c["metrics"]["recall_labeled"] for c in cases),
            "mean_ndcg": metrics.mean(c["metrics"]["ndcg"] for c in cases),
            "evaluated_cases": evaluated,
        },
        "cases": cases,
    }


def _run(name: str, report: dict, *, source: str = "document") -> ProfileRun:
    return ProfileRun(
        name=name,
        source=source,
        document=None if source == "ambient" else {"name": name},
        document_fingerprint=None if source == "ambient" else f"docfp-{name}",
        report=report,
    )


def _baseline() -> ProfileRun:
    return _run(
        "ambient", _report([_case("c1", retrieved=["x", "r"], relevant=["r"])]), source="ambient"
    )


# --------------------------------------------------------------------------- #
# Ordering + structure
# --------------------------------------------------------------------------- #
def test_candidates_ordered_by_name():
    base = _baseline()
    b = _run("bravo", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pb"))
    a = _run("alpha", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pa"))
    result = assemble_experiment(base, [b, a])
    assert [c["profile_name"] for c in result["candidates"]] == ["alpha", "bravo"]
    assert result["kind"] == EXPERIMENT_KIND
    assert result["schema_version"] == EXPERIMENT_SCHEMA_VERSION
    assert result["case_count"] == 1
    assert result["baseline"]["source"] == "ambient"
    assert result["candidates"][0]["comparison"]["kind"] == "replay_comparison"


def test_shared_fingerprints_hoisted_to_top_level():
    base = _baseline()
    c = _run("c", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pc"))
    result = assemble_experiment(base, [c])
    assert set(result["fingerprints"]) == {"corpus", "index", "case_set"}
    assert result["fingerprints"]["corpus"] == "corp-1"


# --------------------------------------------------------------------------- #
# Rejections
# --------------------------------------------------------------------------- #
def test_empty_candidates_rejected():
    with pytest.raises(EvalCaseError):
        assemble_experiment(_baseline(), [])


def test_duplicate_candidate_names_rejected():
    base = _baseline()
    r = _report([_case("c1", retrieved=["r", "x"], relevant=["r"])])
    with pytest.raises(EvalCaseError):
        assemble_experiment(base, [_run("dup", r), _run("dup", copy.deepcopy(r))])


def test_candidate_name_colliding_with_baseline_rejected():
    base = _baseline()  # named "ambient"
    r = _report([_case("c1", retrieved=["r", "x"], relevant=["r"])])
    with pytest.raises(EvalCaseError):
        assemble_experiment(base, [_run("ambient", r)])


@pytest.mark.parametrize("axis", ["corpus", "index", "case_set"])
def test_fingerprint_drift_rejected(axis):
    base = _baseline()
    drifted = _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pc")
    if axis == "case_set":
        drifted["fingerprints"]["case_set"] = "tampered"
    else:
        drifted["fingerprints"][axis] = "tampered"
    with pytest.raises(EvalCaseError):
        assemble_experiment(base, [_run("c", drifted)])


# --------------------------------------------------------------------------- #
# Gate per candidate
# --------------------------------------------------------------------------- #
def _policy_no_ndcg_regression() -> object:
    return load_policy(
        {
            "schema_version": 1,
            "kind": "replay_gate_policy",
            "require": {"case_set_match": True},
            "aggregate_delta_floors": {"ndcg": 0.0},
        }
    )


def test_gate_evaluated_independently_per_candidate():
    base = _baseline()  # baseline ndcg at rank 2
    better = _run(
        "better", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pg")
    )
    worse = _run(
        "worse", _report([_case("c1", retrieved=["x", "y"], relevant=["r"])], profile="pw")
    )
    result = assemble_experiment(base, [better, worse], policy=_policy_no_ndcg_regression())
    by_name = {c["profile_name"]: c for c in result["candidates"]}
    assert result["policy_supplied"] is True
    assert by_name["better"]["gate"]["pass"] is True
    assert by_name["worse"]["gate"]["pass"] is False


def test_no_policy_leaves_gate_null():
    base = _baseline()
    c = _run("c", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pc"))
    result = assemble_experiment(base, [c])
    assert result["policy_supplied"] is False
    assert result["candidates"][0]["gate"] is None


# --------------------------------------------------------------------------- #
# Determinism roll-up + byte stability
# --------------------------------------------------------------------------- #
def test_top_level_deterministic_is_and_of_entries():
    base = _baseline()
    nd = _run(
        "nd",
        _report(
            [_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pn", deterministic=False
        ),
    )
    result = assemble_experiment(base, [nd])
    assert result["deterministic"] is False
    assert result["candidates"][0]["nondeterministic_stages"] == ["query_expansion_llm"]


def test_serialize_is_byte_identical_across_key_shuffles():
    base = _baseline()
    c = _run("c", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pc"))
    first = serialize_experiment(assemble_experiment(base, [c]))
    # Re-assemble from deep copies with reversed dict insertion order.
    base2 = _run(
        "ambient",
        {k: base.report[k] for k in reversed(list(base.report))},
        source="ambient",
    )
    c2 = _run("c", {k: c.report[k] for k in reversed(list(c.report))})
    second = serialize_experiment(assemble_experiment(base2, [c2]))
    assert first == second


def test_serialize_rejects_nan():
    base = _baseline()
    c = _run("c", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pc"))
    result = assemble_experiment(base, [c])
    result["aggregate_smuggled"] = float("nan")
    with pytest.raises(ValueError):
        serialize_experiment(result)
