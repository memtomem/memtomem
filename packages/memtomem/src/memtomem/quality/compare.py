"""Report-to-report comparison of two replay reports (#1802).

:func:`compare_reports` is a pure function over two parsed replay-report dicts —
a *baseline* and a *candidate*. It never touches storage or the pipeline, so it
runs on an unconfigured machine (CI). Because a report file is untrusted input,
compare validates both sides against a strict typed schema, recomputes each
case's declared metrics and the case-set fingerprint, and refuses anything that
doesn't self-check — a hand-edited report cannot manufacture an improvement or
dodge the opt-in regression gate.

Comparison joins cases by ``case_id`` and classifies each: ``compared`` cases
(same definition, both healthy) yield metric deltas and an improved/regressed/
mixed/unchanged verdict; everything else (``version_mismatch``,
``definition_mismatch``, one-sided, ``excluded``, and the directional
``*_degraded`` states) is listed and counted but kept out of the deltas and the
gate's quality signal — while a *newly degraded candidate* or a structural
case-set drift still fails the opt-in gate (fail-closed).
"""

from __future__ import annotations

import json
import math
from typing import Any, TypeVar

from memtomem.errors import EvalCaseError
from memtomem.quality import metrics
from memtomem.quality.fingerprints import case_set_fingerprint
from memtomem.quality.replay import (
    REPLAY_REPORT_KIND,
    REPLAY_REPORT_SCHEMA_VERSION,
    report_case_to_fingerprint_input,
)

__all__ = [
    "COMPARISON_SCHEMA_VERSION",
    "COMPARISON_KIND",
    "compare_reports",
    "serialize_comparison",
]

COMPARISON_SCHEMA_VERSION = 1
COMPARISON_KIND = "replay_comparison"

_EPSILON = 1e-9

#: Metrics compared for every ``compared`` case. ``precision`` is handled
#: separately (it joins only when comparable on both sides).
_DELTA_METRICS = ("hit_rate", "reciprocal_rank", "recall_labeled", "ndcg")

_STAGE_OUTCOME_KEYS = (
    "bm25_error",
    "dense_error",
    "dense_suppressed_mismatch",
    "expansion_failed",
    "rerank_fallback",
    "rescue_failed",
)


def serialize_comparison(comparison: dict[str, Any]) -> str:
    """Canonical bytes for a comparison result (deterministic, NaN-rejecting)."""
    return (
        json.dumps(comparison, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


# --------------------------------------------------------------------------- #
# Input validation                                                            #
# --------------------------------------------------------------------------- #


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise EvalCaseError(message)


_T = TypeVar("_T")


def _require_type(value: Any, typ: type[_T], message: str) -> _T:
    """Assert ``value`` is ``typ``, raising :class:`EvalCaseError` — and narrow it."""
    if not isinstance(value, typ):
        raise EvalCaseError(message)
    return value


def _finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _validate_report(report: Any, side: str) -> None:
    """Strict typed validation of one replay report (raises EvalCaseError).

    Field typing is not enough on its own: compare also recomputes the case-set
    fingerprint and every case's metrics after this passes, so a well-typed but
    internally inconsistent report is still rejected downstream. This pass makes
    those recomputations safe (no raw TypeError) and closes the cheap tamper
    vectors (wrong kind/version, duplicate ids, non-finite scores, bad ranks).
    """
    _require(isinstance(report, dict), f"{side} report must be a JSON object")
    _require(
        report.get("schema_version") == REPLAY_REPORT_SCHEMA_VERSION,
        f"{side} report has unsupported schema_version {report.get('schema_version')!r}",
    )
    _require(
        report.get("kind") == REPLAY_REPORT_KIND,
        f"{side} report has unsupported kind {report.get('kind')!r}",
    )
    _require(
        isinstance(report.get("as_of_unix"), int), f"{side} report 'as_of_unix' must be an int"
    )
    fingerprints = report.get("fingerprints")
    _require(isinstance(fingerprints, dict), f"{side} report 'fingerprints' must be an object")
    for key in ("profile", "corpus", "index", "case_set"):
        _require(
            isinstance(fingerprints.get(key), str),
            f"{side} report fingerprint {key!r} must be a string",
        )
    _require(
        isinstance(report.get("deterministic"), bool),
        f"{side} report 'deterministic' must be a boolean",
    )
    cases = report.get("cases")
    _require(isinstance(cases, list), f"{side} report 'cases' must be a list")

    seen_ids: set[str] = set()
    for case in cases:
        _validate_case(case, side)
        cid = case["case_id"]
        _require(cid not in seen_ids, f"{side} report has duplicate case_id {cid!r}")
        seen_ids.add(cid)

    # The declared case-set fingerprint must match a recomputation from the case
    # payloads, so a truncated/edited report can't borrow a valid fingerprint.
    recomputed = case_set_fingerprint([report_case_to_fingerprint_input(c) for c in cases])
    _require(
        fingerprints["case_set"] == recomputed,
        f"{side} report 'case_set' fingerprint does not match its cases (tampered or truncated)",
    )


def _validate_case(case: Any, side: str) -> None:
    _require(isinstance(case, dict), f"{side} case must be a JSON object")
    cid = case.get("case_id")
    _require(
        isinstance(cid, str) and bool(cid), f"{side} case 'case_id' must be a non-empty string"
    )
    _require(
        isinstance(case.get("version"), int) and not isinstance(case.get("version"), bool),
        f"{side} case {cid!r} 'version' must be an int",
    )
    top_k = case.get("top_k")
    _require(
        isinstance(top_k, int) and not isinstance(top_k, bool) and top_k > 0,
        f"{side} case {cid!r} 'top_k' must be a positive int",
    )
    _require(
        isinstance(case.get("query_text"), str),
        f"{side} case {cid!r} 'query_text' must be a string",
    )
    _require(
        isinstance(case.get("included_in_aggregate"), bool),
        f"{side} case {cid!r} 'included_in_aggregate' must be a boolean",
    )

    outcomes = _require_type(
        case.get("stage_outcomes"), dict, f"{side} case {cid!r} 'stage_outcomes' must be an object"
    )
    _require(
        set(outcomes) == set(_STAGE_OUTCOME_KEYS),
        f"{side} case {cid!r} 'stage_outcomes' must have exactly {list(_STAGE_OUTCOME_KEYS)}",
    )
    for key in _STAGE_OUTCOME_KEYS:
        _require(
            isinstance(outcomes[key], bool),
            f"{side} case {cid!r} stage outcome {key!r} must be a boolean",
        )

    _validate_flags_consistency(case, outcomes, side, cid)
    _validate_labels(case, side, cid)
    _validate_retrieved(case, side, cid)
    _validate_metrics(case, side, cid)


def _validate_flags_consistency(
    case: dict[str, Any], outcomes: dict[str, Any], side: str, cid: str
) -> None:
    """Reconcile ``flags`` / ``included_in_aggregate`` with ``stage_outcomes``.

    These three fields are all independently editable, and the gate keys off the
    derived comparison status. Without this cross-check, deleting only
    ``"degraded"`` from a genuinely degraded case would flip its status from
    ``candidate_degraded`` to ``excluded`` — which the gate does not count —
    letting a broken candidate pass. Requiring internal consistency closes that
    single-field tamper (a fully self-consistent forgery of runtime stage
    outcomes is not recomputable and out of scope).
    """
    flags = _require_type(case.get("flags"), list, f"{side} case {cid!r} 'flags' must be a list")
    _require(
        all(isinstance(f, str) for f in flags),
        f"{side} case {cid!r} 'flags' must be a list of strings",
    )
    degraded_expected = any(outcomes.values())
    _require(
        ("degraded" in flags) == degraded_expected,
        f"{side} case {cid!r} 'degraded' flag disagrees with its stage_outcomes",
    )
    # Inclusion is exactly "not degraded, not filter-excluded".
    expected_included = not (
        degraded_expected or "unreplayable_filters" in flags or "invalid_filters" in flags
    )
    _require(
        case["included_in_aggregate"] == expected_included,
        f"{side} case {cid!r} 'included_in_aggregate' disagrees with its flags/stage_outcomes",
    )


def _validate_labels(case: dict[str, Any], side: str, cid: str) -> None:
    labels = _require_type(
        case.get("labels"), dict, f"{side} case {cid!r} 'labels' must be an object"
    )
    for name in ("relevant", "not_relevant"):
        values = _require_type(
            labels.get(name),
            list,
            f"{side} case {cid!r} labels {name!r} must be a list of non-empty strings",
        )
        _require(
            all(isinstance(h, str) and h for h in values),
            f"{side} case {cid!r} labels {name!r} must be a list of non-empty strings",
        )
        _require(
            len(set(values)) == len(values),
            f"{side} case {cid!r} labels {name!r} has duplicate hashes",
        )
    _require(
        not (set(labels["relevant"]) & set(labels["not_relevant"])),
        f"{side} case {cid!r} labels overlap: a hash is both relevant and not_relevant",
    )


def _validate_retrieved(case: dict[str, Any], side: str, cid: str) -> None:
    retrieved = _require_type(
        case.get("retrieved"), list, f"{side} case {cid!r} 'retrieved' must be a list"
    )
    for i, item in enumerate(retrieved, start=1):
        _require(isinstance(item, dict), f"{side} case {cid!r} retrieved item must be an object")
        _require(
            isinstance(item.get("content_hash"), str) and bool(item.get("content_hash")),
            f"{side} case {cid!r} retrieved 'content_hash' must be a non-empty string",
        )
        _require(
            _finite_number(item.get("score")),
            f"{side} case {cid!r} retrieved 'score' must be a finite number",
        )
        # Ranks must equal their 1-based positions — not merely increase — so a
        # report can't reorder scoring against declared ranks.
        _require(
            item.get("rank") == i,
            f"{side} case {cid!r} retrieved 'rank' must equal its 1-based position",
        )
        _require(
            isinstance(item.get("source"), str),
            f"{side} case {cid!r} retrieved 'source' must be a string",
        )


def _validate_metrics(case: dict[str, Any], side: str, cid: str) -> None:
    case_metrics = _require_type(
        case.get("metrics"), dict, f"{side} case {cid!r} 'metrics' must be an object"
    )
    for key in _DELTA_METRICS:
        _require(
            _finite_number(case_metrics.get(key)),
            f"{side} case {cid!r} metric {key!r} must be a finite number",
        )
    precision = case_metrics.get("precision")
    _require(
        precision is None or _finite_number(precision),
        f"{side} case {cid!r} metric 'precision' must be a finite number or null",
    )

    # Recompute every metric from the validated retrieved + labels + top_k: the
    # case-set fingerprint doesn't cover retrieved/metrics, so this is the only
    # thing stopping a hand-edited number from manufacturing a delta.
    ordered = [item["content_hash"] for item in case["retrieved"]]
    relevant = set(case["labels"]["relevant"])
    not_relevant = set(case["labels"]["not_relevant"])
    top_k = case["top_k"]
    expected = {
        "hit_rate": metrics.hit_rate_at_k(ordered, relevant, top_k),
        "reciprocal_rank": metrics.reciprocal_rank_at_k(ordered, relevant, top_k),
        "recall_labeled": metrics.recall_labeled_at_k(ordered, relevant, top_k),
        "ndcg": metrics.ndcg_at_k(ordered, {h: 1.0 for h in relevant}, top_k),
    }
    for key, want in expected.items():
        _require(
            abs(case_metrics[key] - want) <= _EPSILON,
            f"{side} case {cid!r} metric {key!r} does not match its retrieved ranking",
        )
    want_precision = metrics.precision_at_k(ordered, relevant, not_relevant, top_k)
    if want_precision is None:
        _require(
            precision is None,
            f"{side} case {cid!r} declares a precision but its labels are incomplete",
        )
    else:
        _require(
            precision is not None and abs(precision - want_precision) <= _EPSILON,
            f"{side} case {cid!r} metric 'precision' does not match its retrieved ranking",
        )


# --------------------------------------------------------------------------- #
# Comparison                                                                  #
# --------------------------------------------------------------------------- #


def _definition_key(case: dict[str, Any]) -> tuple[Any, ...]:
    """Canonical definition identity — same inputs a case-set fingerprint covers."""
    labels = case["labels"]
    return (
        case["query_text"],
        case["top_k"],
        json.dumps(case.get("filters") or {}, sort_keys=True),
        tuple(sorted(labels["relevant"])),
        tuple(sorted(labels["not_relevant"])),
    )


def _first_rank(retrieved: list[dict[str, Any]], content_hash: str) -> int | None:
    for item in retrieved:
        if item["content_hash"] == content_hash:
            return item["rank"]
    return None


def _classify(deltas: dict[str, float]) -> str:
    improved = any(d > _EPSILON for d in deltas.values())
    regressed = any(d < -_EPSILON for d in deltas.values())
    if improved and regressed:
        return "mixed"
    if improved:
        return "improved"
    if regressed:
        return "regressed"
    return "unchanged"


def _precision_status(base: float | None, cand: float | None) -> str:
    base_ok = base is not None
    cand_ok = cand is not None
    if base_ok and cand_ok:
        return "comparable"
    if not base_ok and not cand_ok:
        return "both_incomplete"
    return "baseline_incomplete" if not base_ok else "candidate_incomplete"


def _compare_case(base: dict[str, Any], cand: dict[str, Any]) -> dict[str, Any]:
    """Compare one joined case, returning its comparison entry with a status."""
    entry: dict[str, Any] = {"case_id": base["case_id"], "name": cand.get("name")}

    if base["version"] != cand["version"]:
        entry["status"] = "version_mismatch"
        return entry
    if _definition_key(base) != _definition_key(cand):
        entry["status"] = "definition_mismatch"
        return entry

    base_degraded = not base["included_in_aggregate"] and "degraded" in base.get("flags", [])
    cand_degraded = not cand["included_in_aggregate"] and "degraded" in cand.get("flags", [])
    if base_degraded and cand_degraded:
        entry["status"] = "both_degraded"
        return entry
    if cand_degraded:
        entry["status"] = "candidate_degraded"
        return entry
    if base_degraded:
        entry["status"] = "baseline_degraded"
        return entry
    if not base["included_in_aggregate"] or not cand["included_in_aggregate"]:
        # Non-degraded exclusion (e.g. unreplayable filters): comparable numbers
        # don't exist, so keep it out of the quality signal without failing.
        entry["status"] = "excluded"
        return entry

    entry["status"] = "compared"
    deltas = {key: cand["metrics"][key] - base["metrics"][key] for key in _DELTA_METRICS}
    precision_status = _precision_status(base["metrics"]["precision"], cand["metrics"]["precision"])
    if precision_status == "comparable":
        deltas["precision"] = cand["metrics"]["precision"] - base["metrics"]["precision"]

    entry["classification"] = _classify(deltas)
    entry["precision_status"] = precision_status
    entry["metric_deltas"] = {
        key: {
            "baseline": base["metrics"][key],
            "candidate": cand["metrics"][key],
            "delta": deltas[key],
        }
        for key in deltas
    }
    entry["rank_movement"] = [
        {
            "content_hash": h,
            "baseline_rank": _first_rank(base["retrieved"], h),
            "candidate_rank": _first_rank(cand["retrieved"], h),
        }
        for h in sorted(base["labels"]["relevant"])
    ]
    return entry


def _aggregate_deltas(
    base_by_id: dict[str, dict[str, Any]],
    cand_by_id: dict[str, dict[str, Any]],
    compared_ids: list[str],
) -> dict[str, Any]:
    """Recompute both aggregate sides over the ``compared`` cohort only.

    Subtracting the reports' own top-level aggregates would compare different
    case populations whenever anything is degraded/excluded/one-sided — dropping
    a hard case could masquerade as improvement. Precision uses its own paired
    cohort (cases comparable on both sides).
    """
    out: dict[str, Any] = {"cohort_size": len(compared_ids)}
    for key in _DELTA_METRICS:
        base_val = metrics.mean(base_by_id[cid]["metrics"][key] for cid in compared_ids)
        cand_val = metrics.mean(cand_by_id[cid]["metrics"][key] for cid in compared_ids)
        out[key] = {"baseline": base_val, "candidate": cand_val, "delta": cand_val - base_val}

    precision_ids = [
        cid
        for cid in compared_ids
        if base_by_id[cid]["metrics"]["precision"] is not None
        and cand_by_id[cid]["metrics"]["precision"] is not None
    ]
    base_p = metrics.mean(base_by_id[cid]["metrics"]["precision"] for cid in precision_ids)
    cand_p = metrics.mean(cand_by_id[cid]["metrics"]["precision"] for cid in precision_ids)
    out["precision"] = {
        "baseline": base_p,
        "candidate": cand_p,
        "delta": cand_p - base_p,
        "cohort_size": len(precision_ids),
    }
    return out


def _compatibility(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Advisory compatibility block — never gates, only explains the deltas."""
    bfp = baseline["fingerprints"]
    cfp = candidate["fingerprints"]
    as_of_match = baseline["as_of_unix"] == candidate["as_of_unix"]
    profile_match = bfp["profile"] == cfp["profile"]
    corpus_match = bfp["corpus"] == cfp["corpus"]
    index_match = bfp["index"] == cfp["index"]
    deterministic_inputs = baseline["deterministic"] and candidate["deterministic"]

    notes: list[str] = []
    if not as_of_match:
        # as_of always moves the temporal-validity filter, so note it regardless
        # of decay; add the decay-drift caveat only when decay is on either side.
        note = (
            f"as_of differs ({baseline['as_of_unix']} vs {candidate['as_of_unix']}); "
            "temporal-validity eligibility may differ between reports"
        )
        if _decay_enabled(baseline) or _decay_enabled(candidate):
            note += "; decay is enabled, so score deltas also include decay drift"
        notes.append(note)
    if profile_match:
        notes.append("profiles are identical — metric deltas should be zero")
    if not corpus_match:
        notes.append("corpus differs; deltas conflate profile and data changes")
    if not index_match:
        notes.append("index differs; deltas conflate profile and data changes")
    if not deterministic_inputs:
        notes.append("a report used a nondeterministic profile; small deltas may be noise")

    return {
        "case_set_match": bfp["case_set"] == cfp["case_set"],
        "corpus_match": corpus_match,
        "index_match": index_match,
        "profile_match": profile_match,
        "as_of_match": as_of_match,
        "deterministic_inputs": deterministic_inputs,
        "notes": notes,
    }


def _decay_enabled(report: dict[str, Any]) -> bool:
    knobs = report.get("profile_knobs") or {}
    decay = knobs.get("decay") if isinstance(knobs, dict) else None
    return bool(decay.get("enabled")) if isinstance(decay, dict) else False


def compare_reports(baseline: dict[str, Any], candidate: dict[str, Any]) -> dict[str, Any]:
    """Compare two replay reports; returns a deterministic comparison dict.

    Both inputs are validated (typed schema + case-set + metric recomputation)
    before anything is compared, so a malformed or tampered report raises
    :class:`EvalCaseError` rather than producing a misleading result.
    """
    _validate_report(baseline, "baseline")
    _validate_report(candidate, "candidate")

    base_by_id = {c["case_id"]: c for c in baseline["cases"]}
    cand_by_id = {c["case_id"]: c for c in candidate["cases"]}

    cases: list[dict[str, Any]] = []
    for cid in sorted(set(base_by_id) | set(cand_by_id)):
        base = base_by_id.get(cid)
        cand = cand_by_id.get(cid)
        if base is None:
            name = cand.get("name") if cand else None
            cases.append({"case_id": cid, "name": name, "status": "candidate_only"})
        elif cand is None:
            cases.append({"case_id": cid, "name": base.get("name"), "status": "baseline_only"})
        else:
            cases.append(_compare_case(base, cand))

    summary = {
        "improved": 0,
        "regressed": 0,
        "unchanged": 0,
        "mixed": 0,
        "version_mismatch": 0,
        "definition_mismatch": 0,
        "baseline_only": 0,
        "candidate_only": 0,
        "excluded": 0,
        "baseline_degraded": 0,
        "candidate_degraded": 0,
        "both_degraded": 0,
    }
    for entry in cases:
        status = entry["status"]
        if status == "compared":
            summary[entry["classification"]] += 1
        else:
            summary[status] += 1

    compared_ids = [c["case_id"] for c in cases if c["status"] == "compared"]
    aggregate_deltas = _aggregate_deltas(base_by_id, cand_by_id, compared_ids)

    return {
        "schema_version": COMPARISON_SCHEMA_VERSION,
        "kind": COMPARISON_KIND,
        "compatibility": _compatibility(baseline, candidate),
        "aggregate_deltas": aggregate_deltas,
        "summary": summary,
        "cases": cases,
    }
