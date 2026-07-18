"""Policy-gate engine tests (#1833, Quality Lab Q4).

Pure-dict tests — no storage, no pipeline. Comparisons are built through the
real compare engine over self-checking reports (reusing ``_case``/``_report``
from :mod:`test_quality_compare`), then evaluated against ``GatePolicy``
objects. Covers the two-cohort tally/metric split, the allowlist's
compared-only waiver semantics, coverage + fail-closed emptiness rules, floor
boundaries, required compatibility flags, strict policy validation, and the
emit boundary on reasons.
"""

from __future__ import annotations

import pytest

from memtomem.errors import EvalCaseValidationError
from memtomem.quality.compare import compare_reports
from memtomem.quality.gate import (
    GATE_VERDICT_KIND,
    _safe_reason,
    evaluate_gate,
    load_policy,
    serialize_gate_verdict,
)

from test_quality_compare import _case, _report


def _pol(**kw) -> object:
    return load_policy({"schema_version": 1, "kind": "replay_gate_policy", **kw})


_ALL_CAPS_ZERO = {
    k: 0
    for k in (
        "improved",
        "regressed",
        "mixed",
        "version_mismatch",
        "definition_mismatch",
        "baseline_only",
        "candidate_only",
        "excluded",
        "baseline_degraded",
        "candidate_degraded",
        "both_degraded",
    )
}


def _base() -> dict:
    return _report(
        [
            _case("A", retrieved=["r1", "x", "y"], relevant=["r1"]),
            _case("B", retrieved=["r2", "z"], relevant=["r2"]),
        ]
    )


def _regressed_candidate() -> dict:
    # A's relevant hit drops from rank 1 to rank 3.
    return _report(
        [
            _case("A", retrieved=["x", "y", "r1"], relevant=["r1"]),
            _case("B", retrieved=["r2", "z"], relevant=["r2"]),
        ]
    )


# --------------------------------------------------------------------------- #
# happy path                                                                  #
# --------------------------------------------------------------------------- #


def test_identical_reports_pass_strict_policy():
    comparison = compare_reports(_base(), _base())
    verdict = evaluate_gate(
        comparison,
        _pol(
            max_verdict_counts=_ALL_CAPS_ZERO,
            aggregate_delta_floors={
                "hit_rate": 0.0,
                "reciprocal_rank": 0.0,
                "recall_labeled": 0.0,
                "ndcg": 0.0,
            },
            min_compared_cases=2,
            require={"case_set_match": True, "profile_match": True},
        ),
    )
    assert verdict["pass"] is True
    assert verdict["violations"] == []
    assert verdict["summary_effective"]["unchanged"] == 2
    assert verdict["kind"] == GATE_VERDICT_KIND


# --------------------------------------------------------------------------- #
# verdict-count caps                                                          #
# --------------------------------------------------------------------------- #


def test_regression_fails_verdict_cap_with_case_ids():
    comparison = compare_reports(_base(), _regressed_candidate())
    verdict = evaluate_gate(comparison, _pol(max_verdict_counts={"regressed": 0}))
    assert verdict["pass"] is False
    (v,) = verdict["violations"]
    assert v["rule"] == "verdict_count"
    assert v["key"] == "regressed"
    assert v["observed"] == 1
    assert v["case_ids"] == ["A"]


def test_structural_status_still_capped_in_tally_cohort():
    # Candidate adds case C -> candidate_only; a compared-only recompute would
    # zero this and bypass the cap.
    candidate = _report(
        [
            _case("A", retrieved=["r1"], relevant=["r1"]),
            _case("B", retrieved=["r2"], relevant=["r2"]),
            _case("C", retrieved=["r3"], relevant=["r3"]),
        ]
    )
    comparison = compare_reports(_base(), candidate)
    verdict = evaluate_gate(comparison, _pol(max_verdict_counts={"candidate_only": 0}))
    assert verdict["pass"] is False
    assert verdict["violations"][0]["key"] == "candidate_only"
    assert verdict["violations"][0]["case_ids"] == ["C"]


# --------------------------------------------------------------------------- #
# allowlist                                                                   #
# --------------------------------------------------------------------------- #


def test_allowlist_waives_compared_case_from_count_and_aggregate():
    comparison = compare_reports(_base(), _regressed_candidate())
    verdict = evaluate_gate(
        comparison,
        _pol(
            max_verdict_counts={"regressed": 0},
            aggregate_delta_floors={"ndcg": 0.0, "reciprocal_rank": 0.0},
            allowlist=[{"case_id": "A", "reason": "known BM25 tie flip #1840"}],
        ),
    )
    assert verdict["pass"] is True
    assert verdict["allowlisted"] == [
        {"case_id": "A", "status": "compared", "reason": "known BM25 tie flip #1840"}
    ]
    # A is excluded from the tally too.
    assert verdict["summary_effective"]["regressed"] == 0


def test_allowlist_cannot_waive_structural_status():
    candidate = _report(
        [
            _case("A", retrieved=["r1"], relevant=["r1"]),
            _case("B", retrieved=["r2"], relevant=["r2"]),
            _case("C", retrieved=["r3"], relevant=["r3"]),
        ]
    )
    comparison = compare_reports(_base(), candidate)
    verdict = evaluate_gate(
        comparison,
        _pol(
            max_verdict_counts={"candidate_only": 0},
            allowlist=[{"case_id": "C", "reason": "intentional new case"}],
        ),
    )
    assert verdict["pass"] is False  # still counted
    assert verdict["violations"][0]["key"] == "candidate_only"
    assert any("not 'compared'" in w for w in verdict["warnings"])


def test_unmatched_allowlist_entry_warns_not_fails():
    comparison = compare_reports(_base(), _base())
    verdict = evaluate_gate(
        comparison,
        _pol(allowlist=[{"case_id": "ghost", "reason": "deleted last PR"}]),
    )
    assert verdict["pass"] is True
    assert any("matched no case" in w for w in verdict["warnings"])
    assert verdict["allowlisted"] == []


# --------------------------------------------------------------------------- #
# coverage + fail-closed emptiness                                            #
# --------------------------------------------------------------------------- #


def test_min_compared_cases_uses_pre_allowlist_count():
    # 2 compared cases, allowlist one: pre-allowlist count (2) still meets floor.
    comparison = compare_reports(_base(), _base())
    verdict = evaluate_gate(
        comparison,
        _pol(min_compared_cases=2, allowlist=[{"case_id": "A", "reason": "x"}]),
    )
    assert verdict["pass"] is True


def test_min_compared_cases_fails_when_short():
    comparison = compare_reports(_base(), _base())
    verdict = evaluate_gate(comparison, _pol(min_compared_cases=3))
    assert verdict["pass"] is False
    (v,) = verdict["violations"]
    assert v["rule"] == "min_compared_cases"
    assert v["required"] == 3
    assert v["observed"] == 2


def test_empty_metric_cohort_fails_closed_when_floor_set():
    # Both cases degraded -> no compared case -> empty metric cohort.
    degraded = _report(
        [
            _case("A", retrieved=["r1"], relevant=["r1"], degraded=True),
            _case("B", retrieved=["r2"], relevant=["r2"], degraded=True),
        ]
    )
    comparison = compare_reports(degraded, degraded)
    verdict = evaluate_gate(comparison, _pol(aggregate_delta_floors={"ndcg": 0.0}))
    assert verdict["pass"] is False
    assert verdict["violations"][0]["rule"] == "empty_metric_cohort"


def test_precision_cohort_empty_fails_closed():
    # An unlabeled retrieved item makes precision incomplete (None).
    rpt = _report(
        [
            _case("A", retrieved=["r1", "unl"], relevant=["r1"]),
            _case("B", retrieved=["r2", "unl2"], relevant=["r2"]),
        ]
    )
    comparison = compare_reports(rpt, rpt)
    verdict = evaluate_gate(comparison, _pol(aggregate_delta_floors={"precision": 0.0}))
    assert verdict["pass"] is False
    assert verdict["violations"][0]["rule"] == "precision_cohort_empty"


# --------------------------------------------------------------------------- #
# aggregate floor boundaries                                                  #
# --------------------------------------------------------------------------- #


def test_floor_boundary_at_and_below():
    # Candidate where ndcg drops by a known amount on A.
    comparison = compare_reports(_base(), _regressed_candidate())
    observed = comparison["aggregate_deltas"]["ndcg"]["delta"]
    assert observed < 0  # a real drop
    # Floor exactly at the observed delta -> passes (delta >= floor - eps).
    assert evaluate_gate(comparison, _pol(aggregate_delta_floors={"ndcg": observed}))["pass"]
    # Floor a hair above the observed delta -> fails.
    verdict = evaluate_gate(comparison, _pol(aggregate_delta_floors={"ndcg": observed + 1e-6}))
    assert verdict["pass"] is False
    assert verdict["violations"][0]["rule"] == "aggregate_delta_floor"
    assert verdict["violations"][0]["metric"] == "ndcg"


# --------------------------------------------------------------------------- #
# required compatibility flags                                                #
# --------------------------------------------------------------------------- #


def test_required_flag_violation():
    # Different profile fingerprint -> profile_match False.
    comparison = compare_reports(_base(), _report(_base()["cases"], profile="prof-2"))
    verdict = evaluate_gate(comparison, _pol(require={"profile_match": True}))
    assert verdict["pass"] is False
    (v,) = [x for x in verdict["violations"] if x["rule"] == "compatibility"]
    assert v["key"] == "profile_match"
    assert v["expected"] is True and v["observed"] is False


# --------------------------------------------------------------------------- #
# policy validation                                                           #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "bad",
    [
        {"schema_version": 2, "kind": "replay_gate_policy"},
        {"schema_version": 1, "kind": "wrong_kind"},
        {"schema_version": 1, "kind": "replay_gate_policy", "unknown": 1},
        {"schema_version": 1, "kind": "replay_gate_policy", "max_verdict_counts": {"bogus": 0}},
        {
            "schema_version": 1,
            "kind": "replay_gate_policy",
            "max_verdict_counts": {"regressed": "0"},
        },
        {
            "schema_version": 1,
            "kind": "replay_gate_policy",
            "max_verdict_counts": {"regressed": True},
        },
        {
            "schema_version": 1,
            "kind": "replay_gate_policy",
            "max_verdict_counts": {"regressed": -1},
        },
        {
            "schema_version": 1,
            "kind": "replay_gate_policy",
            "aggregate_delta_floors": {"bogus": 0.0},
        },
        {
            "schema_version": 1,
            "kind": "replay_gate_policy",
            "aggregate_delta_floors": {"ndcg": float("nan")},
        },
        {"schema_version": 1, "kind": "replay_gate_policy", "require": {"case_set_match": "false"}},
        {"schema_version": 1, "kind": "replay_gate_policy", "require": {"bogus": True}},
        {
            "schema_version": 1,
            "kind": "replay_gate_policy",
            "allowlist": [{"case_id": "a", "reason": " "}],
        },
        {
            "schema_version": 1,
            "kind": "replay_gate_policy",
            "allowlist": [{"case_id": "a", "reason": "x"}, {"case_id": "a", "reason": "y"}],
        },
        # strict schema_version: JSON true / 1.0 must not coerce to Literal[1].
        {"schema_version": True, "kind": "replay_gate_policy"},
        {"schema_version": 1.0, "kind": "replay_gate_policy"},
        # an oversized integer floor overflows float() — must be a clean reject.
        {
            "schema_version": 1,
            "kind": "replay_gate_policy",
            "aggregate_delta_floors": {"ndcg": 10**400},
        },
        {
            "schema_version": 1,
            "kind": "replay_gate_policy",
            "aggregate_delta_floors": {"ndcg": float("inf")},
        },
    ],
)
def test_invalid_policies_rejected(bad):
    with pytest.raises(EvalCaseValidationError):
        load_policy(bad)


def test_floor_written_as_int_is_accepted():
    # A policy may write a floor as 0 (int); it normalizes to 0.0 under strict.
    policy = load_policy(
        {"schema_version": 1, "kind": "replay_gate_policy", "aggregate_delta_floors": {"ndcg": 0}}
    )
    assert policy.aggregate_delta_floors["ndcg"] == 0.0


# --------------------------------------------------------------------------- #
# emit boundary + serialization                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "raw,expected_redacted",
    [
        ("benign reason #123", False),
        ("한글 사유도 통과", False),
        ("/Users/me/secret", True),
        ("C:\\Users\\me", True),
        ("has\nnewline", True),
        ("has\ttab", True),
        ("c1-control\x85here", True),  # NEL (C1 control, category Cc)
        ("line\u2028sep", True),  # U+2028 line separator (Zl)
        ("para\u2029sep", True),  # U+2029 paragraph separator (Zp)
        ("bidi\u202eoverride", True),  # RLO bidi override (Cf)
        ("ghp_" + "A" * 36, True),
    ],
)
def test_reason_emit_safety(raw, expected_redacted):
    out = _safe_reason(raw)
    assert (out == "[redacted-reason]") is expected_redacted


def test_reason_length_capped():
    assert len(_safe_reason("x" * 500)) == 200


def test_verdict_serialization_deterministic():
    comparison = compare_reports(_base(), _base())
    verdict = evaluate_gate(comparison, _pol(min_compared_cases=1))
    a = serialize_gate_verdict(verdict)
    b = serialize_gate_verdict(verdict)
    assert a == b
    assert a.endswith("\n")


def test_summary_keys_match_compare_report():
    # The gate's _SUMMARY_KEYS vocabulary must equal compare's summary keys.
    from memtomem.quality.gate import _SUMMARY_KEYS

    comparison = compare_reports(_base(), _base())
    assert set(comparison["summary"]) == set(_SUMMARY_KEYS)
