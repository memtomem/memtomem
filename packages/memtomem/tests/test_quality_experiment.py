"""Multi-candidate experiment assembly tests (#1844, PR-2).

Pure-dict tests — no storage, no pipeline. Replay reports are assembled through
the real metric + fingerprint functions so they self-check, then
``assemble_experiment`` compares/gates them. Covers name ordering, duplicate /
empty rejection, fingerprint-drift rejection, per-candidate independent gate
verdicts, the top-level determinism roll-up, and byte-stable serialization.
"""

from __future__ import annotations

import copy
import json

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


def _run(
    name: str,
    report: dict,
    *,
    source: str = "document",
    warnings: tuple[str, ...] = (),
) -> ProfileRun:
    return ProfileRun(
        name=name,
        source=source,
        document=None if source == "ambient" else {"name": name},
        document_fingerprint=None if source == "ambient" else f"docfp-{name}",
        report=report,
        warnings=warnings,
    )


def _baseline() -> ProfileRun:
    return _run(
        "ambient", _report([_case("c1", retrieved=["x", "r"], relevant=["r"])]), source="ambient"
    )


# The profile-independent storage snapshot; corpus must match the reports' corpus.
_SNAP = {"corpus": "corp-1", "index": "idx-full"}


def _assemble(base, cands, *, snapshot=None, policy=None):
    return assemble_experiment(base, cands, shared_snapshot=snapshot or _SNAP, policy=policy)


# --------------------------------------------------------------------------- #
# Ordering + structure
# --------------------------------------------------------------------------- #
def test_candidates_ordered_by_name():
    base = _baseline()
    b = _run("bravo", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pb"))
    a = _run("alpha", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pa"))
    result = _assemble(base, [b, a])
    assert [c["profile_name"] for c in result["candidates"]] == ["alpha", "bravo"]
    assert result["kind"] == EXPERIMENT_KIND
    assert result["schema_version"] == EXPERIMENT_SCHEMA_VERSION
    assert result["case_count"] == 1
    assert result["baseline"]["source"] == "ambient"
    assert result["candidates"][0]["comparison"]["kind"] == "replay_comparison"


def test_shared_fingerprints_hoisted_to_top_level():
    base = _baseline()
    c = _run("c", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pc"))
    result = _assemble(base, [c])
    assert set(result["fingerprints"]) == {"corpus", "index", "case_set"}
    assert result["fingerprints"]["corpus"] == "corp-1"
    # The top-level index comes from the profile-independent storage snapshot.
    assert result["fingerprints"]["index"] == "idx-full"


def test_profile_warnings_survive_experiment_json_for_baseline_and_candidate():
    baseline = _run(
        "baseline",
        _report([_case("c1", retrieved=["x", "r"], relevant=["r"])]),
        warnings=("baseline_warning",),
    )
    candidate = _run(
        "candidate",
        _report(
            [_case("c1", retrieved=["r", "x"], relevant=["r"])],
            profile="candidate-profile",
        ),
        warnings=("rerank_provider_model_mismatch",),
    )

    payload = json.loads(serialize_experiment(_assemble(baseline, [candidate])))

    assert payload["baseline"]["warnings"] == ["baseline_warning"]
    assert payload["candidates"][0]["warnings"] == ["rerank_provider_model_mismatch"]


# --------------------------------------------------------------------------- #
# Rejections
# --------------------------------------------------------------------------- #
def test_empty_candidates_rejected():
    with pytest.raises(EvalCaseError):
        _assemble(_baseline(), [])


def test_duplicate_candidate_names_rejected():
    base = _baseline()
    r = _report([_case("c1", retrieved=["r", "x"], relevant=["r"])])
    with pytest.raises(EvalCaseError):
        _assemble(base, [_run("dup", r), _run("dup", copy.deepcopy(r))])


def test_candidate_name_colliding_with_baseline_rejected():
    base = _baseline()  # named "ambient"
    r = _report([_case("c1", retrieved=["r", "x"], relevant=["r"])])
    with pytest.raises(EvalCaseError):
        _assemble(base, [_run("ambient", r)])


@pytest.mark.parametrize("axis", ["corpus", "case_set"])
def test_profile_independent_fingerprint_drift_rejected(axis):
    # corpus and case_set are profile-independent, so a mismatch across reports
    # is real drift and must be rejected.
    base = _baseline()
    drifted = _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pc")
    drifted["fingerprints"][axis] = "tampered"
    with pytest.raises(EvalCaseError):
        _assemble(base, [_run("c", drifted)])


def test_differing_index_fingerprint_is_allowed():
    # The index fingerprint legitimately varies by profile (access/link
    # artifacts are folded in only when a profile reads them), so a candidate
    # with a different index fingerprint over the same corpus is NOT drift.
    base = _baseline()
    cand = _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pc", index="idx-2")
    result = _assemble(base, [_run("c", cand)])
    assert result["candidates"][0]["comparison"]["compatibility"]["index_match"] is False


def test_snapshot_corpus_mismatch_rejected():
    # The storage snapshot must agree with the reports' corpus.
    base = _baseline()
    c = _run("c", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pc"))
    with pytest.raises(EvalCaseError):
        _assemble(base, [c], snapshot={"corpus": "different", "index": "idx-full"})


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
    result = _assemble(base, [better, worse], policy=_policy_no_ndcg_regression())
    by_name = {c["profile_name"]: c for c in result["candidates"]}
    assert result["policy_supplied"] is True
    assert by_name["better"]["gate"]["pass"] is True
    assert by_name["worse"]["gate"]["pass"] is False


def test_no_policy_leaves_gate_null():
    base = _baseline()
    c = _run("c", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pc"))
    result = _assemble(base, [c])
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
    result = _assemble(base, [nd])
    assert result["deterministic"] is False
    assert result["candidates"][0]["nondeterministic_stages"] == ["query_expansion_llm"]


def test_serialize_is_byte_identical_across_key_shuffles():
    base = _baseline()
    c = _run("c", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pc"))
    first = serialize_experiment(_assemble(base, [c]))
    # Re-assemble from deep copies with reversed dict insertion order.
    base2 = _run(
        "ambient",
        {k: base.report[k] for k in reversed(list(base.report))},
        source="ambient",
    )
    c2 = _run("c", {k: c.report[k] for k in reversed(list(c.report))})
    second = serialize_experiment(_assemble(base2, [c2]))
    assert first == second


def test_serialize_rejects_nan():
    base = _baseline()
    c = _run("c", _report([_case("c1", retrieved=["r", "x"], relevant=["r"])], profile="pc"))
    result = _assemble(base, [c])
    result["aggregate_smuggled"] = float("nan")
    with pytest.raises(ValueError):
        serialize_experiment(result)


# --------------------------------------------------------------------------- #
# run_experiment — pre-checks + post-run drift (fake storage, no real replay)
# --------------------------------------------------------------------------- #
import asyncio  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from memtomem.quality.experiment import run_experiment  # noqa: E402
from memtomem.quality.profiles import load_profile_document  # noqa: E402


def _doc(name):
    return load_profile_document(
        {"schema_version": 1, "kind": "retrieval_profile", "name": name, "knobs": {}}
    )


def test_run_experiment_rejects_duplicate_names_before_replay(monkeypatch):
    replayed = []

    async def spy_replay(*a, **k):
        replayed.append(1)
        raise AssertionError("should not replay when names are invalid")

    monkeypatch.setattr("memtomem.quality.experiment._replay_profile", spy_replay)
    comp = SimpleNamespace(storage=object())
    with pytest.raises(EvalCaseError):
        asyncio.run(
            run_experiment(comp, baseline_doc=None, candidate_docs=[_doc("dup"), _doc("dup")])
        )
    assert not replayed  # rejected before any transient stack / replay


def test_run_experiment_empty_candidates_rejected_before_replay(monkeypatch):
    async def spy_replay(*a, **k):
        raise AssertionError("should not replay")

    monkeypatch.setattr("memtomem.quality.experiment._replay_profile", spy_replay)
    with pytest.raises(EvalCaseError):
        asyncio.run(
            run_experiment(SimpleNamespace(storage=object()), baseline_doc=None, candidate_docs=[])
        )


def test_run_experiment_detects_post_run_drift(monkeypatch):
    # First snapshot differs from the second → a writer changed the DB mid-run.
    snaps = iter([{"corpus": "a", "index": "i"}, {"corpus": "b", "index": "i"}])
    monkeypatch.setattr("memtomem.quality.experiment._shared_snapshot", lambda storage: next(snaps))

    async def fake_select(storage, selectors):
        return (["c1"], set(), 0)

    monkeypatch.setattr("memtomem.quality.experiment._select_case_ids", fake_select)

    async def fake_replay(components, *, name, doc, case_ids, as_of_unix):
        return _run(name, _report([_case("c1", retrieved=["r"], relevant=["r"])]))

    monkeypatch.setattr("memtomem.quality.experiment._replay_profile", fake_replay)
    with pytest.raises(EvalCaseError):
        asyncio.run(
            run_experiment(
                SimpleNamespace(storage=object()),
                baseline_doc=None,
                candidate_docs=[_doc("cand")],
                as_of_unix=1000,
            )
        )
