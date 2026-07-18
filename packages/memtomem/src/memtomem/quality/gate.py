"""Policy-driven gate over a replay comparison (#1833, Quality Lab Q4).

:func:`compare_reports` (``quality/compare.py``) stays pure and advisory — it
classifies and counts, but never decides pass/fail. This module layers a
*declarative gate* on top of its output: a committed JSON policy (verdict-count
caps, signed per-metric aggregate-delta floors, required compatibility flags,
a minimum compared-case coverage floor, and an optional per-case allowlist)
evaluated against one comparison into a deterministic verdict + pass/fail.

Two design invariants (Codex design-gate rounds 3-6):

* **Two cohorts.** Count caps are recomputed over *all* non-allowlisted cases
  (so mismatch / degraded / one-sided statuses are still capped), while
  aggregate floors use only the ``compared`` non-allowlisted subset. Zeroing
  the tally from the compared cohort alone would silently bypass the structural
  caps.
* **The allowlist waives compared cases only.** ``case_set_match`` and the
  other compatibility flags are fingerprints over whole reports; a per-case
  entry cannot retroactively change one. So an allowlisted id that resolves to
  a structural / non-compared status is *not* excluded — it stays counted and
  emits a warning telling the maintainer to refresh the baseline instead.

The emit boundary matches replay/compare: the verdict carries case_ids
verbatim (inheriting compare's existing gated-identifier contract) but never
raw prose — every allowlist ``reason`` is sanitized before it is emitted.
"""

from __future__ import annotations

import json
import math
import unicodedata
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictBool,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from memtomem.errors import EvalCaseValidationError
from memtomem.privacy import has_emit_risk

__all__ = [
    "GATE_POLICY_SCHEMA_VERSION",
    "GATE_POLICY_KIND",
    "GATE_VERDICT_SCHEMA_VERSION",
    "GATE_VERDICT_KIND",
    "GatePolicy",
    "load_policy",
    "evaluate_gate",
    "serialize_gate_verdict",
]

GATE_POLICY_SCHEMA_VERSION = 1
GATE_POLICY_KIND = "replay_gate_policy"
GATE_VERDICT_SCHEMA_VERSION = 1
GATE_VERDICT_KIND = "replay_gate_verdict"

_EPSILON = 1e-9  # mirrors quality/compare.py::_EPSILON — floor comparisons tolerate float noise

#: The exact 12 verdict-count keys the compare report's ``summary`` carries;
#: a policy may cap any subset of these. Kept in sync with
#: ``quality/compare.py::compare_reports`` (asserted by test_quality_gate).
_SUMMARY_KEYS = frozenset(
    {
        "improved",
        "regressed",
        "unchanged",
        "mixed",
        "version_mismatch",
        "definition_mismatch",
        "baseline_only",
        "candidate_only",
        "excluded",
        "baseline_degraded",
        "candidate_degraded",
        "both_degraded",
    }
)

#: Rank metrics carried on every ``compared`` case; ``precision`` joins only
#: when comparable on both sides, so it is handled separately.
_RANK_METRICS = ("hit_rate", "reciprocal_rank", "recall_labeled", "ndcg")
_METRIC_KEYS = frozenset({*_RANK_METRICS, "precision"})

#: The six advisory compatibility booleans a policy may require to be true.
_COMPAT_FLAGS = frozenset(
    {
        "case_set_match",
        "corpus_match",
        "index_match",
        "profile_match",
        "as_of_match",
        "deterministic_inputs",
    }
)

_REDACTED_REASON = "[redacted-reason]"
_REASON_MAX_LEN = 200

#: Unicode categories barred from an emitted reason: control (C0/C1 → ``Cc``),
#: format/bidi (``Cf``), and line/paragraph separators (``Zl``/``Zp``). These
#: cover ASCII newlines/tabs as well as C1 controls, U+2028/2029, and bidi
#: overrides that could inject newlines or visually spoof table/log output.
_UNSAFE_REASON_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


def _safe_reason(reason: str) -> str:
    """Sanitize an allowlist reason for emission into the verdict / logs.

    Layers control-char and length limits on top of the shared secret + path
    core (:func:`memtomem.privacy.has_emit_risk`): a reason that could leak a
    secret or a path, or that carries any control / format / separator
    character (a log-injection or spoofing shape), is replaced wholesale;
    anything over the length cap is truncated.
    """
    if has_emit_risk(reason) or any(
        unicodedata.category(c) in _UNSAFE_REASON_CATEGORIES for c in reason
    ):
        return _REDACTED_REASON
    if len(reason) > _REASON_MAX_LEN:
        return reason[:_REASON_MAX_LEN]
    return reason


# --------------------------------------------------------------------------- #
# Policy schema                                                               #
# --------------------------------------------------------------------------- #


class RequireFlags(BaseModel):
    """Compatibility booleans a policy demands be true in the comparison.

    Each defaults to ``False`` (not required). Strict so a JSON ``"false"``
    string is rejected rather than coerced.
    """

    model_config = ConfigDict(extra="forbid", strict=True)

    case_set_match: StrictBool = False
    corpus_match: StrictBool = False
    index_match: StrictBool = False
    profile_match: StrictBool = False
    as_of_match: StrictBool = False
    deterministic_inputs: StrictBool = False


class AllowlistEntry(BaseModel):
    """A per-case waiver: exclude one ``compared`` case from counts + floors."""

    model_config = ConfigDict(extra="forbid", strict=True)

    case_id: StrictStr
    reason: StrictStr

    @field_validator("case_id", "reason")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty string")
        return v


class GatePolicy(BaseModel):
    """Declarative gate policy — the single source of every threshold."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1]
    kind: Literal["replay_gate_policy"]
    description: str | None = None
    require: RequireFlags = RequireFlags()
    max_verdict_counts: dict[str, int] = {}
    aggregate_delta_floors: dict[str, float] = {}
    min_compared_cases: int = 0
    allowlist: list[AllowlistEntry] = []

    @model_validator(mode="before")
    @classmethod
    def _validate_maps(cls, data: Any) -> Any:
        """Guard the two open-keyed maps before pydantic coerces them.

        Keys must be drawn from the fixed vocabularies; count values must be
        real non-negative ints (not bools, floats, or coercible strings) and
        floor values real finite numbers (not bools / NaN / inf / overflowing
        ints). Running in ``before`` mode lets us reject the coercions
        ``strict=True`` doesn't catch and normalize floor ints to floats so the
        strict ``dict[str, float]`` field accepts a policy that writes ``0``.
        """
        if not isinstance(data, dict):
            return data
        data = dict(data)  # don't mutate the caller's dict

        # ``Literal[1]`` still coerces ``True`` (bool ⊂ int, ``True == 1``) and
        # ``1.0`` even under strict mode, so pin schema_version to a real int
        # here (``type(...) is int`` excludes bool) and let ``Literal[1]``
        # enforce the value.
        if "schema_version" in data and type(data["schema_version"]) is not int:
            raise ValueError("schema_version must be an integer")

        counts = data.get("max_verdict_counts")
        if counts is not None:
            if not isinstance(counts, dict):
                raise ValueError("max_verdict_counts must be an object")
            for key, val in counts.items():
                if key not in _SUMMARY_KEYS:
                    raise ValueError(f"unknown verdict key {key!r} in max_verdict_counts")
                if isinstance(val, bool) or not isinstance(val, int):
                    raise ValueError(f"max_verdict_counts[{key!r}] must be an integer")
                if val < 0:
                    raise ValueError(f"max_verdict_counts[{key!r}] must be >= 0")

        floors = data.get("aggregate_delta_floors")
        if floors is not None:
            if not isinstance(floors, dict):
                raise ValueError("aggregate_delta_floors must be an object")
            normalized: dict[str, float] = {}
            for key, val in floors.items():
                if key not in _METRIC_KEYS:
                    raise ValueError(f"unknown metric {key!r} in aggregate_delta_floors")
                if isinstance(val, bool) or not isinstance(val, (int, float)):
                    raise ValueError(f"aggregate_delta_floors[{key!r}] must be a number")
                try:
                    fval = float(val)
                except OverflowError as e:
                    raise ValueError(f"aggregate_delta_floors[{key!r}] is out of range") from e
                if not math.isfinite(fval):
                    raise ValueError(f"aggregate_delta_floors[{key!r}] must be finite")
                normalized[key] = fval
            data["aggregate_delta_floors"] = normalized

        mcc = data.get("min_compared_cases")
        if mcc is not None and (isinstance(mcc, bool) or not isinstance(mcc, int) or mcc < 0):
            raise ValueError("min_compared_cases must be an integer >= 0")

        return data

    @model_validator(mode="after")
    def _unique_allowlist(self) -> GatePolicy:
        ids = [e.case_id for e in self.allowlist]
        if len(ids) != len(set(ids)):
            raise ValueError("allowlist has duplicate case_id entries")
        return self


def load_policy(data: Any) -> GatePolicy:
    """Validate a parsed policy dict into a :class:`GatePolicy`.

    Wraps pydantic's :class:`ValidationError` in
    :class:`~memtomem.errors.EvalCaseValidationError` so every surface treats a
    malformed policy the same way it treats a malformed eval case. The message
    is derived from pydantic's structured errors (field locations + reasons),
    which never echo secrets or paths.
    """
    try:
        return GatePolicy.model_validate(data)
    except ValidationError as e:
        summary = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in e.errors()
        )
        raise EvalCaseValidationError(f"invalid gate policy: {summary}") from e


# --------------------------------------------------------------------------- #
# Evaluation                                                                  #
# --------------------------------------------------------------------------- #


def _verdict_key(case: dict[str, Any]) -> str:
    """The summary bucket a comparison case counts toward."""
    status = case["status"]
    return case["classification"] if status == "compared" else status


def evaluate_gate(comparison: dict[str, Any], policy: GatePolicy) -> dict[str, Any]:
    """Evaluate one comparison against ``policy`` into a deterministic verdict.

    ``comparison`` is the output of :func:`memtomem.quality.compare.compare_reports`
    (already validated by construction). The returned dict is canonical-ready
    (see :func:`serialize_gate_verdict`) and carries only case_ids, counts,
    metric names, and sanitized reasons — no memory content, paths, or names.
    """
    cases = comparison["cases"]
    case_by_id = {c["case_id"]: c for c in cases}

    allow_ids = [e.case_id for e in policy.allowlist]
    reason_by_id = {e.case_id: e.reason for e in policy.allowlist}

    # An allowlist entry waives a case only when it resolves to a `compared`
    # status; structural / non-compared matches stay counted and warn.
    effective_allowlisted = {
        cid for cid in allow_ids if cid in case_by_id and case_by_id[cid]["status"] == "compared"
    }

    warnings: list[str] = []
    allowlisted: list[dict[str, Any]] = []
    for cid in allow_ids:
        case = case_by_id.get(cid)
        if case is None:
            warnings.append(f"allowlist case_id {cid!r} matched no case in the comparison")
            continue
        status = case["status"]
        allowlisted.append(
            {"case_id": cid, "status": status, "reason": _safe_reason(reason_by_id[cid])}
        )
        if cid not in effective_allowlisted:
            warnings.append(
                f"allowlist case_id {cid!r} has status {status!r}, not 'compared'; "
                "the allowlist cannot waive structural case-set changes — refresh the baseline"
            )

    violations: list[dict[str, Any]] = []

    # --- required compatibility flags -------------------------------------- #
    compat = comparison["compatibility"]
    require = policy.require.model_dump()
    for flag in sorted(_COMPAT_FLAGS):
        if require.get(flag) and not compat.get(flag):
            violations.append(
                {
                    "rule": "compatibility",
                    "key": flag,
                    "expected": True,
                    "observed": compat.get(flag),
                }
            )

    # --- verdict-count caps over the tally cohort -------------------------- #
    # Tally cohort = all cases minus effective-allowlisted; each keeps its real
    # status/classification so structural buckets are still counted.
    tally: dict[str, int] = dict.fromkeys(_SUMMARY_KEYS, 0)
    ids_by_key: dict[str, list[str]] = {k: [] for k in _SUMMARY_KEYS}
    for case in cases:
        if case["case_id"] in effective_allowlisted:
            continue
        key = _verdict_key(case)
        tally[key] += 1
        ids_by_key[key].append(case["case_id"])

    for key in sorted(policy.max_verdict_counts):
        cap = policy.max_verdict_counts[key]
        observed = tally[key]
        if observed > cap:
            violations.append(
                {
                    "rule": "verdict_count",
                    "key": key,
                    "allowed": cap,
                    "observed": observed,
                    "case_ids": sorted(ids_by_key[key]),
                }
            )

    # --- coverage: pre-allowlist compared count ---------------------------- #
    pre_allowlist_compared = sum(1 for c in cases if c["status"] == "compared")
    if policy.min_compared_cases and pre_allowlist_compared < policy.min_compared_cases:
        violations.append(
            {
                "rule": "min_compared_cases",
                "required": policy.min_compared_cases,
                "observed": pre_allowlist_compared,
            }
        )

    # --- aggregate delta floors over the metric cohort --------------------- #
    metric_cohort = [
        c for c in cases if c["status"] == "compared" and c["case_id"] not in effective_allowlisted
    ]
    rank_floors = {m: f for m, f in policy.aggregate_delta_floors.items() if m in _RANK_METRICS}
    if rank_floors and not metric_cohort:
        violations.append({"rule": "empty_metric_cohort", "metrics": sorted(rank_floors)})
    else:
        for metric in sorted(rank_floors):
            floor = rank_floors[metric]
            mean_delta = _mean(c["metric_deltas"][metric]["delta"] for c in metric_cohort)
            if mean_delta < floor - _EPSILON:
                violations.append(
                    {
                        "rule": "aggregate_delta_floor",
                        "metric": metric,
                        "floor": floor,
                        "observed": mean_delta,
                    }
                )

    # --- precision floor over its own comparable subset -------------------- #
    if "precision" in policy.aggregate_delta_floors:
        floor = policy.aggregate_delta_floors["precision"]
        precision_cohort = [c for c in metric_cohort if "precision" in c["metric_deltas"]]
        if not precision_cohort:
            violations.append({"rule": "precision_cohort_empty", "metric": "precision"})
        else:
            mean_delta = _mean(c["metric_deltas"]["precision"]["delta"] for c in precision_cohort)
            if mean_delta < floor - _EPSILON:
                violations.append(
                    {
                        "rule": "aggregate_delta_floor",
                        "metric": "precision",
                        "floor": floor,
                        "observed": mean_delta,
                    }
                )

    violations.sort(key=lambda v: (v["rule"], v.get("key") or v.get("metric") or ""))
    warnings.sort()
    allowlisted.sort(key=lambda a: a["case_id"])

    return {
        "schema_version": GATE_VERDICT_SCHEMA_VERSION,
        "kind": GATE_VERDICT_KIND,
        "pass": not violations,
        "violations": violations,
        "allowlisted": allowlisted,
        "warnings": warnings,
        "summary_effective": tally,
    }


def _mean(values: Any) -> float:
    xs = list(values)
    return sum(xs) / len(xs) if xs else 0.0


def serialize_gate_verdict(verdict: dict[str, Any]) -> str:
    """Canonical bytes for a gate verdict (deterministic, NaN-rejecting)."""
    return json.dumps(verdict, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
