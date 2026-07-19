"""Static checks on the committed quality-gate fixture assets (#1833 PR-B).

No indexing, no ``mm`` binary — these run in the default test job and only assert
that the committed ``tools/quality-gate/`` assets are internally consistent:
the policy parses, the baseline passes its own policy under self-comparison, and
the generated ``cases.json`` lines up with the hand-written ``fixture.json``.

Deliberately NO committed-baseline-vs-fresh-candidate assertion here: that would
require indexing and would make the advisory gate de-facto required through this
blocking job before the introduction window ends. Self-comparison is
OS-independent, so it stays a pure consistency check.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from memtomem.quality.compare import compare_reports
from memtomem.quality.gate import GATE_VERDICT_KIND, evaluate_gate, load_policy
from memtomem.quality.metrics import precision_at_k

_ASSETS = Path(__file__).resolve().parents[3] / "tools" / "quality-gate"


def _load(name: str) -> dict:
    return json.loads((_ASSETS / name).read_text(encoding="utf-8"))


def test_policy_parses_under_gate_policy() -> None:
    policy = load_policy(_load("policy.json"))
    assert policy.kind == "replay_gate_policy"
    # Labels are complete over the corpus, so precision is comparable + floored.
    assert policy.aggregate_delta_floors.get("precision") == 0.0
    # The precision-cohort-coverage rule is enabled at 0 (#1837): any compared
    # case with incomplete precision is a violation. Pinned here so removing it
    # from the committed policy fails a test rather than silently dropping the
    # production protection (the behaviour tests use synthetic policies).
    assert policy.max_incomplete_precision_cases == 0
    # every verdict class except `unchanged` is capped.
    assert "unchanged" not in policy.max_verdict_counts
    assert all(v == 0 for v in policy.max_verdict_counts.values())


def test_baseline_self_comparison_passes_committed_policy() -> None:
    baseline = _load("baseline_replay.json")
    policy = load_policy(_load("policy.json"))
    comparison = compare_reports(baseline, baseline)
    verdict = evaluate_gate(comparison, policy)
    assert verdict["pass"] is True, verdict["violations"]
    assert verdict["kind"] == GATE_VERDICT_KIND
    # Every case compares as unchanged against itself.
    assert verdict["summary_effective"]["unchanged"] == len(baseline["cases"])


def test_cases_envelope_is_well_formed() -> None:
    cases = _load("cases.json")
    assert cases["schema_version"] == 1
    assert cases["kind"] == "eval_case_set"
    assert len(cases["cases"]) == 12
    for case in cases["cases"]:
        judgments = {label["judgment"] for label in case["labels"]}
        assert judgments <= {"relevant", "not_relevant"}
        # Complete judgments over the corpus: at least one relevant hit and a
        # not_relevant tail (so precision is comparable, never vacuous).
        assert any(label["judgment"] == "relevant" for label in case["labels"])
        assert any(label["judgment"] == "not_relevant" for label in case["labels"])
        assert case["top_k"] > 0


def test_baseline_precision_cohort_is_complete() -> None:
    # The precision floor only fires when precision is comparable; complete
    # labels must leave no case with incomplete precision.
    baseline = _load("baseline_replay.json")
    assert baseline["counts"]["incomplete_precision"] == 0
    assert baseline["aggregate"]["mean_precision"]["incomplete"] == 0


def test_precision_floor_catches_appended_off_target_result() -> None:
    # The point of complete labels + the precision floor: a same-corpus candidate
    # that appends one labelled not_relevant result after the rank-1 hit leaves
    # the rank metrics flat but drops precision, so the gate must fail it.
    baseline = _load("baseline_replay.json")
    policy = load_policy(_load("policy.json"))
    candidate = copy.deepcopy(baseline)
    case = candidate["cases"][0]
    retrieved_hashes = {r["content_hash"] for r in case["retrieved"]}
    appended = next(h for h in case["labels"]["not_relevant"] if h not in retrieved_hashes)
    case["retrieved"].append(
        {
            "content_hash": appended,
            "score": 0.001,
            "rank": len(case["retrieved"]) + 1,
            "source": "bm25",
        }
    )
    # Recompute the case's precision so the candidate survives compare's
    # tamper-resistant metric revalidation (mirrors what a real replay emits).
    ranked = [r["content_hash"] for r in case["retrieved"]]
    case["metrics"]["precision"] = precision_at_k(
        ranked, set(case["labels"]["relevant"]), set(case["labels"]["not_relevant"]), case["top_k"]
    )

    verdict = evaluate_gate(compare_reports(baseline, candidate), policy)
    assert verdict["pass"] is False
    assert any(
        v["rule"] == "aggregate_delta_floor" and v.get("metric") == "precision"
        for v in verdict["violations"]
    )


def test_committed_policy_catches_unlabelled_retrieved_chunk() -> None:
    # The #1837 coverage rule, exercised through the *committed* policy: a
    # candidate that retrieves a chunk absent from the corpus (so unlabelled)
    # makes that case's precision incomplete, dropping it from the precision
    # cohort. Rank metrics stay flat, so only the coverage rule catches it.
    baseline = _load("baseline_replay.json")
    policy = load_policy(_load("policy.json"))
    candidate = copy.deepcopy(baseline)
    case = candidate["cases"][0]
    # A sha256-shaped hash that appears in no label -> genuinely unlabelled.
    unlabelled = "0" * 64
    labelled = set(case["labels"]["relevant"]) | set(case["labels"]["not_relevant"])
    assert unlabelled not in labelled
    case["retrieved"].append(
        {
            "content_hash": unlabelled,
            "score": 0.001,
            "rank": len(case["retrieved"]) + 1,
            "source": "bm25",
        }
    )
    # Recompute precision so the candidate survives compare's metric revalidation;
    # an unlabelled hit in top-k makes it incomplete (None).
    ranked = [r["content_hash"] for r in case["retrieved"]]
    case["metrics"]["precision"] = precision_at_k(
        ranked, set(case["labels"]["relevant"]), set(case["labels"]["not_relevant"]), case["top_k"]
    )
    assert case["metrics"]["precision"] is None

    verdict = evaluate_gate(compare_reports(baseline, candidate), policy)
    assert verdict["pass"] is False
    (v,) = [x for x in verdict["violations"] if x["rule"] == "precision_cohort_coverage"]
    assert v["observed"] == 1
    assert v["case_ids"] == [case["case_id"]]


def test_fixture_case_ids_match_generated_cases() -> None:
    fixture = _load("fixture.json")
    cases = _load("cases.json")
    fixture_ids = {cd["case_id"] for cd in fixture["case_defs"]}
    generated_ids = {c["case_id"] for c in cases["cases"]}
    assert fixture_ids == generated_ids


def test_min_compared_cases_matches_fixture_size() -> None:
    fixture = _load("fixture.json")
    policy = load_policy(_load("policy.json"))
    assert policy.min_compared_cases == len(fixture["case_defs"])
