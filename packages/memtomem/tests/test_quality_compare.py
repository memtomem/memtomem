"""Report-to-report comparison tests (#1802, Quality Lab PR-4).

Pure-dict tests — no storage, no pipeline. Reports are assembled through the
real metric + fingerprint functions so they self-check, then compare validates,
classifies, and gates them. Covers classification, precision comparability,
directional degradation + gate semantics, cohort-scoped aggregate deltas,
compatibility notes, and strict tamper rejection.
"""

from __future__ import annotations

import copy

import pytest

from memtomem.errors import EvalCaseError
from memtomem.quality import metrics
from memtomem.quality.compare import compare_reports
from memtomem.quality.fingerprints import case_set_fingerprint
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


def _case(
    case_id: str,
    *,
    retrieved: list[str],
    relevant: list[str],
    not_relevant: list[str] | None = None,
    top_k: int = 5,
    version: int = 1,
    name: str | None = None,
    query: str = "q",
    included: bool = True,
    degraded: bool = False,
    filters: dict | None = None,
) -> dict:
    not_relevant = not_relevant or []
    rel_set = set(relevant)
    gains = {h: 1.0 for h in rel_set}
    precision = metrics.precision_at_k(retrieved, rel_set, set(not_relevant), top_k)
    outcomes = {k: False for k in _STAGE_OUTCOME_KEYS}
    if degraded:
        outcomes["dense_error"] = True
    # Keep flags / included_in_aggregate internally consistent with the reason:
    # degraded → "degraded"; a non-degraded exclusion is an unreplayable-filter case.
    if degraded:
        flags = ["degraded"]
    elif not included:
        flags = ["unreplayable_filters"]
    else:
        flags = []
    return {
        "case_id": case_id,
        "name": name,
        "version": version,
        "status": "active",
        "query_text": query,
        "top_k": top_k,
        "filters": filters if filters is not None else {"namespace": None, "scope": None},
        "stale": {"profile": None, "corpus": None, "index": None},
        "flags": flags,
        "labels": {"relevant": sorted(rel_set), "not_relevant": sorted(not_relevant)},
        "retrieved": [
            {"content_hash": h, "score": 1.0 - i * 0.01, "rank": i, "source": "bm25"}
            for i, h in enumerate(retrieved, start=1)
        ],
        "metrics": {
            "hit_rate": metrics.hit_rate_at_k(retrieved, rel_set, top_k),
            "reciprocal_rank": metrics.reciprocal_rank_at_k(retrieved, rel_set, top_k),
            "recall_labeled": metrics.recall_labeled_at_k(retrieved, rel_set, top_k),
            "ndcg": metrics.ndcg_at_k(retrieved, gains, top_k),
            "precision": precision,
        },
        "stage_outcomes": outcomes,
        "included_in_aggregate": included and not degraded,
    }


def _report(
    cases: list[dict],
    *,
    profile: str = "prof-1",
    corpus: str = "corp-1",
    index: str = "idx-1",
    as_of: int = 1000,
    deterministic: bool = True,
    decay: bool = False,
) -> dict:
    cases = sorted(cases, key=lambda c: c["case_id"])
    case_set = case_set_fingerprint([report_case_to_fingerprint_input(c) for c in cases])
    return {
        "schema_version": REPLAY_REPORT_SCHEMA_VERSION,
        "kind": REPLAY_REPORT_KIND,
        "as_of_unix": as_of,
        "deterministic": deterministic,
        "nondeterministic_stages": [] if deterministic else ["query_expansion_llm"],
        "fingerprints": {
            "profile": profile,
            "corpus": corpus,
            "index": index,
            "case_set": case_set,
        },
        "profile_knobs": {"decay": {"enabled": decay}},
        "counts": {},
        "aggregate": {},
        "cases": cases,
    }


class TestClassification:
    def test_improved_regressed_unchanged_mixed(self):
        # baseline: relevant at rank 2 (RR .5); candidate: rank 1 (RR 1) → improved.
        base = _report([_case("c1", retrieved=["x", "r"], relevant=["r"])])
        cand = _report([_case("c1", retrieved=["r", "x"], relevant=["r"])])
        result = compare_reports(base, cand)
        assert result["summary"]["improved"] == 1
        assert result["cases"][0]["classification"] == "improved"

        # reverse → regressed
        rev = compare_reports(cand, base)
        assert rev["summary"]["regressed"] == 1

        # identical → unchanged
        same = compare_reports(base, copy.deepcopy(base))
        assert same["summary"]["unchanged"] == 1

    def test_epsilon_tie_is_unchanged(self):
        base = _report([_case("c1", retrieved=["r", "x"], relevant=["r"])])
        cand = _report([_case("c1", retrieved=["r", "x"], relevant=["r"])])
        result = compare_reports(base, cand)
        assert result["cases"][0]["classification"] == "unchanged"


class TestRankMovement:
    def test_null_when_not_retrieved(self):
        base = _report([_case("c1", retrieved=["r", "x"], relevant=["r"])])
        cand = _report([_case("c1", retrieved=["x", "y"], relevant=["r"])])
        result = compare_reports(base, cand)
        movement = result["cases"][0]["rank_movement"]
        assert movement == [{"content_hash": "r", "baseline_rank": 1, "candidate_rank": None}]


class TestPrecisionComparability:
    def test_status_transitions(self):
        # baseline complete (both labeled), candidate incomplete (unlabeled hit).
        base = _report([_case("c1", retrieved=["r", "n"], relevant=["r"], not_relevant=["n"])])
        cand = _report([_case("c1", retrieved=["r", "u"], relevant=["r"], not_relevant=["n"])])
        result = compare_reports(base, cand)
        entry = result["cases"][0]
        assert entry["precision_status"] == "candidate_incomplete"
        # precision delta must NOT be part of the metric deltas when not comparable.
        assert "precision" not in entry["metric_deltas"]

    def test_comparable_precision_joins_deltas(self):
        base = _report([_case("c1", retrieved=["r", "n"], relevant=["r"], not_relevant=["n"])])
        cand = _report([_case("c1", retrieved=["n", "r"], relevant=["r"], not_relevant=["n"])])
        result = compare_reports(base, cand)
        entry = result["cases"][0]
        assert entry["precision_status"] == "comparable"
        assert "precision" in entry["metric_deltas"]


class TestDegradationGate:
    def test_candidate_degraded_status_and_gate(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"])])
        cand = _report([_case("c1", retrieved=["r"], relevant=["r"], degraded=True)])
        result = compare_reports(base, cand)
        assert result["cases"][0]["status"] == "candidate_degraded"
        assert result["summary"]["candidate_degraded"] == 1
        # Not counted as a quality verdict.
        assert result["summary"]["regressed"] == 0

    def test_both_degraded_status(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"], degraded=True)])
        cand = _report([_case("c1", retrieved=["r"], relevant=["r"], degraded=True)])
        result = compare_reports(base, cand)
        assert result["cases"][0]["status"] == "both_degraded"
        assert result["summary"]["both_degraded"] == 1

    def test_baseline_degraded_status(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"], degraded=True)])
        cand = _report([_case("c1", retrieved=["r"], relevant=["r"])])
        result = compare_reports(base, cand)
        assert result["cases"][0]["status"] == "baseline_degraded"


class TestStructuralStatuses:
    def test_version_mismatch_excluded(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"], version=1)])
        cand = _report([_case("c1", retrieved=["r"], relevant=["r"], version=2)])
        result = compare_reports(base, cand)
        assert result["cases"][0]["status"] == "version_mismatch"
        assert result["summary"]["version_mismatch"] == 1
        assert "classification" not in result["cases"][0]

    def test_definition_mismatch_on_same_id_version(self):
        # Same id+version but different query text (possible via import --replace).
        base = _report([_case("c1", retrieved=["r"], relevant=["r"], query="alpha")])
        cand = _report([_case("c1", retrieved=["r"], relevant=["r"], query="beta")])
        result = compare_reports(base, cand)
        assert result["cases"][0]["status"] == "definition_mismatch"

    def test_one_sided_cases(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"])])
        cand = _report(
            [
                _case("c1", retrieved=["r"], relevant=["r"]),
                _case("c2", retrieved=["r"], relevant=["r"]),
            ]
        )
        result = compare_reports(base, cand)
        statuses = {c["case_id"]: c["status"] for c in result["cases"]}
        assert statuses["c2"] == "candidate_only"
        assert result["summary"]["candidate_only"] == 1

    def test_excluded_unreplayable_not_degraded(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"], included=False)])
        cand = _report([_case("c1", retrieved=["r"], relevant=["r"], included=False)])
        result = compare_reports(base, cand)
        assert result["cases"][0]["status"] == "excluded"


class TestAggregateCohort:
    def test_dropped_hard_case_does_not_masquerade_as_improvement(self):
        # c1 is a hard miss in both. c2 becomes degraded in candidate. The
        # aggregate must be computed over the compared cohort (c1 only), so
        # dropping c2 can't lift the mean.
        base = _report(
            [
                _case("c1", retrieved=["x"], relevant=["r"]),  # miss, hit_rate 0
                _case("c2", retrieved=["r"], relevant=["r"]),  # hit, hit_rate 1
            ]
        )
        cand = _report(
            [
                _case("c1", retrieved=["x"], relevant=["r"]),
                _case("c2", retrieved=["r"], relevant=["r"], degraded=True),
            ]
        )
        result = compare_reports(base, cand)
        agg = result["aggregate_deltas"]
        assert agg["cohort_size"] == 1  # only c1 is compared
        assert agg["hit_rate"]["baseline"] == 0.0
        assert agg["hit_rate"]["candidate"] == 0.0
        assert agg["hit_rate"]["delta"] == 0.0


class TestCompatibility:
    def test_identical_profile_note(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"])])
        result = compare_reports(base, copy.deepcopy(base))
        assert result["compatibility"]["profile_match"] is True
        assert any("identical" in n for n in result["compatibility"]["notes"])

    def test_as_of_note_always_present_decay_caveat_conditional(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"])], as_of=1000)
        cand = _report([_case("c1", retrieved=["r"], relevant=["r"])], as_of=2000)
        no_decay = compare_reports(base, cand)
        as_of_notes = [n for n in no_decay["compatibility"]["notes"] if "as_of differs" in n]
        assert as_of_notes and "decay" not in as_of_notes[0]

        base_d = _report([_case("c1", retrieved=["r"], relevant=["r"])], as_of=1000, decay=True)
        cand_d = _report([_case("c1", retrieved=["r"], relevant=["r"])], as_of=2000, decay=True)
        with_decay = compare_reports(base_d, cand_d)
        decay_notes = [n for n in with_decay["compatibility"]["notes"] if "as_of differs" in n]
        assert decay_notes and "decay" in decay_notes[0]

    def test_nondeterministic_input_note(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"])], deterministic=False)
        cand = _report([_case("c1", retrieved=["r"], relevant=["r"])])
        result = compare_reports(base, cand)
        assert result["compatibility"]["deterministic_inputs"] is False
        assert any("nondeterministic" in n for n in result["compatibility"]["notes"])


class TestValidation:
    def test_wrong_kind_rejected(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"])])
        bad = copy.deepcopy(base)
        bad["kind"] = "not_a_report"
        with pytest.raises(EvalCaseError):
            compare_reports(bad, base)

    def test_tampered_metric_rejected(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"])])
        bad = copy.deepcopy(base)
        bad["cases"][0]["metrics"]["hit_rate"] = 0.123  # inconsistent with retrieved
        with pytest.raises(EvalCaseError):
            compare_reports(base, bad)

    def test_tampered_case_set_fingerprint_rejected(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"])])
        bad = copy.deepcopy(base)
        # Drop a case but keep the declared case_set fingerprint → mismatch.
        bad["cases"].append(_case("c2", retrieved=["r"], relevant=["r"]))
        with pytest.raises(EvalCaseError):
            compare_reports(base, bad)

    def test_non_finite_score_rejected(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"])])
        bad = copy.deepcopy(base)
        bad["cases"][0]["retrieved"][0]["score"] = float("inf")
        with pytest.raises(EvalCaseError):
            compare_reports(base, bad)

    def test_overlapping_labels_rejected(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"])])
        bad = copy.deepcopy(base)
        bad["cases"][0]["labels"]["not_relevant"] = ["r"]  # r is both
        with pytest.raises(EvalCaseError):
            compare_reports(base, bad)

    def test_bad_rank_ordering_rejected(self):
        base = _report([_case("c1", retrieved=["r", "x"], relevant=["r"])])
        bad = copy.deepcopy(base)
        bad["cases"][0]["retrieved"][1]["rank"] = 5  # not 1-based position
        with pytest.raises(EvalCaseError):
            compare_reports(base, bad)

    def test_retrieved_longer_than_top_k_rejected(self):
        base = _report([_case("c1", retrieved=["r"], relevant=["r"], top_k=1)])
        bad = copy.deepcopy(base)
        # Pad the ranking past top_k with a consistent extra item (rank == pos).
        bad["cases"][0]["retrieved"].append(
            {"content_hash": "extra", "score": 0.5, "rank": 2, "source": "bm25"}
        )
        with pytest.raises(EvalCaseError):
            compare_reports(base, bad)

    def test_single_field_degraded_tamper_rejected(self):
        # A genuinely degraded candidate whose "degraded" flag is stripped to
        # dodge the gate (status would flip candidate_degraded → excluded) must
        # be rejected for internal inconsistency, not silently pass.
        base = _report([_case("c1", retrieved=["r"], relevant=["r"])])
        cand = _report([_case("c1", retrieved=["r"], relevant=["r"], degraded=True)])
        tampered = copy.deepcopy(cand)
        case = tampered["cases"][0]
        assert case["stage_outcomes"]["dense_error"] is True  # still really degraded
        case["flags"] = [f for f in case["flags"] if f != "degraded"]
        case["included_in_aggregate"] = True
        with pytest.raises(EvalCaseError):
            compare_reports(base, tampered)
