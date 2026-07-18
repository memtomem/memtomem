"""Replay engine: run stored eval cases and build a deterministic report (#1802).

Each active (or explicitly selected) evaluation case is re-run through
:meth:`SearchPipeline.search` in no-side-effects mode (``record=False``) with a
pinned ``as_of_unix``, then scored with the pure IR metrics. The output is a
deterministic JSON-able report: for a deterministic profile, two replays over the
same corpus/index/profile at the same ``as_of`` serialize byte-for-byte
identically (:func:`serialize_report` owns that contract — no timestamps, no
latency, no raw error strings, cases sorted by ``case_id``).

A report is *replay-report-to-replay-report* comparable
(:mod:`memtomem.quality.compare`): it carries the per-case ``retrieved`` ranking
(``content_hash`` only — never text or paths) and label sets so a comparator can
recompute rank movement and metrics without trusting the declared numbers.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

from memtomem.errors import EvalCaseError
from memtomem.quality import metrics
from memtomem.quality.fingerprints import case_set_fingerprint
from memtomem.quality.state import current_fingerprints, nondeterministic_stages

if TYPE_CHECKING:
    from memtomem.config import Mem2MemConfig
    from memtomem.search.pipeline import SearchPipeline
    from memtomem.storage.sqlite_backend import SqliteBackend

__all__ = [
    "REPLAY_REPORT_SCHEMA_VERSION",
    "REPLAY_REPORT_KIND",
    "MAX_AS_OF_UNIX",
    "STAGE_OUTCOME_KEYS",
    "replay_cases",
    "serialize_report",
    "report_case_to_fingerprint_input",
]

REPLAY_REPORT_SCHEMA_VERSION = 1
REPLAY_REPORT_KIND = "replay_report"

#: Upper bound on ``as_of_unix`` (1000-01-01 .. 3000-01-01). The pinned instant
#: flows into ``datetime.fromtimestamp`` via time-decay; an out-of-range value
#: would raise deep in the pipeline (a 500 on the web surface / opaque MCP
#: error) instead of a clean validation failure. Guarded once here so every
#: surface (CLI / MCP / web) inherits the same bound.
MAX_AS_OF_UNIX = 32_503_680_000

#: The canonical per-case stage-outcome keys (all booleans). One fixed set,
#: shared with the compare validator so every replayed case reports exactly
#: these six and a seventh outcome cannot drift the two modules apart.
STAGE_OUTCOME_KEYS = (
    "bm25_error",
    "dense_error",
    "dense_suppressed_mismatch",
    "expansion_failed",
    "rerank_fallback",
    "rescue_failed",
)


def serialize_report(report: dict[str, Any]) -> str:
    """Canonical bytes for a replay report (the determinism contract lives here).

    ``sort_keys`` makes key order irrelevant, ``allow_nan=False`` rejects a
    non-finite metric before it can poison a downstream comparison, and the
    report itself carries no volatile field — so two deterministic-profile
    replays at the same ``as_of`` produce identical strings.
    """
    return json.dumps(report, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def report_case_to_fingerprint_input(case_report: dict[str, Any]) -> dict[str, Any]:
    """Adapt a per-case *report* entry to :func:`case_set_fingerprint`'s input.

    The report carries labels as ``{"relevant": [...], "not_relevant": [...]}``
    (hash lists), while ``case_set_fingerprint`` expects
    ``labels=[{content_hash, judgment}]``. Replay declares its own
    ``fingerprints.case_set`` through this same adapter over the same serialized
    case payloads, so a comparator recomputing the fingerprint matches by
    construction.
    """
    labels = case_report.get("labels") or {}
    rebuilt = [{"content_hash": h, "judgment": "relevant"} for h in labels.get("relevant", [])] + [
        {"content_hash": h, "judgment": "not_relevant"} for h in labels.get("not_relevant", [])
    ]
    return {
        "case_id": case_report.get("case_id"),
        "version": case_report.get("version"),
        "query_text": case_report.get("query_text"),
        "top_k": case_report.get("top_k"),
        "filters": case_report.get("filters") or {},
        "labels": rebuilt,
    }


def _staleness(promoted: dict[str, Any], live: dict[str, str]) -> dict[str, bool | None]:
    """Per-axis staleness: ``True``/``False`` drift, ``None`` when unknowable.

    A promoted fingerprint of ``""`` (e.g. an imported case) carries no baseline
    to compare against, so its axis is ``None`` rather than a false positive.
    """
    out: dict[str, bool | None] = {}
    for axis in ("profile", "corpus", "index"):
        promoted_fp = promoted.get(axis) or ""
        out[axis] = None if not promoted_fp else (promoted_fp != live.get(axis))
    return out


async def _select_case_ids(
    storage: SqliteBackend, case_ids: "list[str] | None"
) -> tuple[list[str], set[str], int]:
    """Resolve the replay selection to canonical, de-duplicated case_ids.

    Returns ``(ordered_case_ids, explicitly_selected_ids, archived_skipped)``.
    ``None`` selects every active case (archived ones counted as skipped);
    an explicit selection resolves each id-or-name to its canonical case_id
    (an unknown selector raises :class:`EvalCaseError`) and de-duplicates while
    preserving first-selection order, so an id and its name alias never yield the
    same case twice — the comparator rejects duplicate case_ids.
    """
    if case_ids is None:
        active = await storage.list_eval_cases(status="active")
        archived = await storage.list_eval_cases(status="archived")
        return [c["case_id"] for c in active], set(), len(archived)

    ordered: list[str] = []
    seen: set[str] = set()
    for selector in case_ids:
        resolved = await storage.get_eval_case(selector)  # raises on unknown
        cid = resolved["case_id"]
        if cid not in seen:
            seen.add(cid)
            ordered.append(cid)
    return ordered, seen, 0


def _replay_case_report(
    case: dict[str, Any],
    results_hashes: list[str],
    retrieved: list[dict[str, Any]],
    stage_outcomes: dict[str, bool],
    *,
    live_fps: dict[str, str],
    invalid_filters: bool,
    explicitly_selected: bool,
) -> dict[str, Any]:
    """Assemble one case's report entry (metrics + flags + inclusion)."""
    top_k = case["top_k"]
    relevant = {lab["content_hash"] for lab in case["labels"] if lab["judgment"] == "relevant"}
    not_relevant = {
        lab["content_hash"] for lab in case["labels"] if lab["judgment"] == "not_relevant"
    }

    precision = metrics.precision_at_k(results_hashes, relevant, not_relevant, top_k)
    case_metrics = {
        "hit_rate": metrics.hit_rate_at_k(results_hashes, relevant, top_k),
        "reciprocal_rank": metrics.reciprocal_rank_at_k(results_hashes, relevant, top_k),
        "recall_labeled": metrics.recall_labeled_at_k(results_hashes, relevant, top_k),
        "ndcg": metrics.ndcg_at_k(results_hashes, {h: 1.0 for h in relevant}, top_k),
        "precision": precision,
    }

    # Sanitized portable filters: nulled when the stored filters failed
    # validation, so no legacy path-shaped value can reach the artifact.
    raw_filters = case.get("filters") or {}
    filters: dict[str, Any] = {"namespace": None, "scope": None}
    unreplayable = bool(raw_filters.get("unreplayable"))
    if not invalid_filters:
        filters["namespace"] = raw_filters.get("namespace")
        filters["scope"] = raw_filters.get("scope")
        if unreplayable:
            filters["unreplayable"] = sorted(raw_filters["unreplayable"])

    stale = _staleness(case.get("promoted_fingerprints", {}), live_fps)
    degraded = any(stage_outcomes.values())

    flags: list[str] = []
    if explicitly_selected and case["status"] == "archived":
        # An archived case only runs when named explicitly (--case). We treat
        # that as intent, so it is flagged but STILL counted in the aggregate;
        # only degradation / unreplayable / invalid filters exclude a case.
        flags.append("archived")
    for axis, drifted in stale.items():
        if drifted:
            flags.append(f"stale_{axis}")
    if precision is None:
        flags.append("incomplete_labels")
    if degraded:
        flags.append("degraded")
    if unreplayable:
        flags.append("unreplayable_filters")
    if invalid_filters:
        flags.append("invalid_filters")

    included = not (degraded or unreplayable or invalid_filters)

    return {
        "case_id": case["case_id"],
        "name": case["name"],
        "version": case["version"],
        "status": case["status"],
        "query_text": case["query_text"],
        "top_k": top_k,
        "filters": filters,
        "stale": stale,
        "flags": sorted(flags),
        "labels": {"relevant": sorted(relevant), "not_relevant": sorted(not_relevant)},
        "retrieved": retrieved,
        "metrics": case_metrics,
        "stage_outcomes": {k: stage_outcomes[k] for k in STAGE_OUTCOME_KEYS},
        "included_in_aggregate": included,
    }


async def replay_cases(
    storage: SqliteBackend,
    pipeline: SearchPipeline,
    config: Mem2MemConfig,
    *,
    case_ids: "list[str] | None" = None,
    as_of_unix: int | None = None,
) -> dict[str, Any]:
    """Replay eval cases and build the deterministic replay report dict.

    ``case_ids`` selects by id-or-name (``None`` = all active). ``as_of_unix``
    pins temporal validity + decay for every case (``None`` = now, pinned once).
    Runs each case through ``pipeline.search(..., record=False)`` — no access
    counters, observations, or cache reads/writes are mutated.
    """
    if as_of_unix is not None and not 0 <= as_of_unix <= MAX_AS_OF_UNIX:
        raise ValueError(
            f"as_of_unix must be between 0 and {MAX_AS_OF_UNIX} "
            f"(a representable unix timestamp), got {as_of_unix}"
        )
    pinned_as_of = int(time.time()) if as_of_unix is None else int(as_of_unix)
    live_fps, knobs = current_fingerprints(storage, config)
    nd_stages = nondeterministic_stages(config, pipeline)

    ordered_ids, explicit_ids, archived_skipped = await _select_case_ids(storage, case_ids)

    case_reports: list[dict[str, Any]] = []
    for cid in ordered_ids:
        case = await storage.get_eval_case(cid)

        # Defensive portable-filter gate for legacy rows predating the validator:
        # an invalid filter never reaches search (run unfiltered) nor the report
        # (values nulled), and the case is excluded from aggregates.
        invalid_filters = False
        try:
            storage.validate_case_filters(case.get("filters"))
        except EvalCaseError:
            invalid_filters = True

        raw_filters = case.get("filters") or {}
        namespace = None if invalid_filters else raw_filters.get("namespace")
        scope = None if invalid_filters else raw_filters.get("scope")

        results, stats = await pipeline.search(
            case["query_text"],
            top_k=case["top_k"],
            namespace=namespace,
            scope=scope,
            # Replay runs at user tier by design: project-scoped runs are
            # unpromotable (their project_context_root is unrecorded), so there
            # is no project context to thread. Explicit ``None`` documents that
            # decision and satisfies the ADR-0011 scope-threading guard.
            project_context_root=None,
            as_of_unix=pinned_as_of,
            record=False,
        )

        # Deduplicate by content_hash, keeping first (best-ranked) occurrence —
        # several chunks can share a hash, and the metrics credit each distinct
        # id once (see quality/metrics.py). Deduping here keeps top-k positions,
        # aggregate scoring, and rank movement all keyed to distinct identities.
        deduped: list[Any] = []
        seen_hashes: set[str] = set()
        for r in results:
            if r.chunk.content_hash not in seen_hashes:
                seen_hashes.add(r.chunk.content_hash)
                deduped.append(r)
        results_hashes = [r.chunk.content_hash for r in deduped]
        # Ranks are the 1-based array positions (the comparator requires this),
        # so the emitted list is self-consistent regardless of the pipeline's own
        # rank field. ``source`` is a stage label ("bm25"/"dense"/...), not a path.
        retrieved = [
            {"content_hash": r.chunk.content_hash, "score": r.score, "rank": i, "source": r.source}
            for i, r in enumerate(deduped, start=1)
        ]
        stage_outcomes = {
            "bm25_error": stats.bm25_error is not None,
            "dense_error": stats.dense_error is not None,
            "dense_suppressed_mismatch": stats.dense_suppressed_mismatch,
            "expansion_failed": stats.expansion_failed,
            # A reranker that had a candidate pool (fused_total>0) ran but did not
            # stamp the results "rerank" scale → it silently fell back to the
            # fused order (#1767). Gate on the pre-filter pool, not the final
            # result list: source/validity filters can empty the results after a
            # real fallback, and ``bool(results)`` would then hide it.
            "rerank_fallback": stats.fused_total > 0
            and stats.rerank_applied
            and stats.score_scale != "rerank",
            "rescue_failed": stats.rescue_failed,
        }

        case_reports.append(
            _replay_case_report(
                case,
                results_hashes,
                retrieved,
                stage_outcomes,
                live_fps=live_fps,
                invalid_filters=invalid_filters,
                explicitly_selected=cid in explicit_ids,
            )
        )

    case_reports.sort(key=lambda c: c["case_id"])

    case_set_fp = case_set_fingerprint([report_case_to_fingerprint_input(c) for c in case_reports])

    included = [c for c in case_reports if c["included_in_aggregate"]]
    precisions = [
        c["metrics"]["precision"] for c in included if c["metrics"]["precision"] is not None
    ]
    aggregate = {
        "k_note": "each case scored at its own top_k",
        "mean_hit_rate": metrics.mean(c["metrics"]["hit_rate"] for c in included),
        "mrr": metrics.mean(c["metrics"]["reciprocal_rank"] for c in included),
        "mean_recall_labeled": metrics.mean(c["metrics"]["recall_labeled"] for c in included),
        "mean_ndcg": metrics.mean(c["metrics"]["ndcg"] for c in included),
        "mean_precision": {
            "value": metrics.mean(precisions),
            "evaluated": len(precisions),
            "incomplete": len(included) - len(precisions),
        },
        "evaluated_cases": len(included),
    }

    counts = {
        # ``replayed`` is the number of cases in this report. (A separate
        # ``selected`` count was dropped as redundant — every selected case is
        # replayed; ``archived_skipped`` covers what selection left out.)
        "replayed": len(case_reports),
        "archived_skipped": archived_skipped,
        "stale": sum(1 for c in case_reports if any(c["stale"].values())),
        "degraded": sum(1 for c in case_reports if "degraded" in c["flags"]),
        "excluded_from_aggregate": sum(1 for c in case_reports if not c["included_in_aggregate"]),
        "incomplete_precision": sum(1 for c in included if c["metrics"]["precision"] is None),
    }

    return {
        "schema_version": REPLAY_REPORT_SCHEMA_VERSION,
        "kind": REPLAY_REPORT_KIND,
        "as_of_unix": pinned_as_of,
        "deterministic": not nd_stages,
        "nondeterministic_stages": nd_stages,
        "fingerprints": {
            "profile": live_fps["profile"],
            "corpus": live_fps["corpus"],
            "index": live_fps["index"],
            "case_set": case_set_fp,
        },
        "profile_knobs": knobs,
        "counts": counts,
        "aggregate": aggregate,
        "cases": case_reports,
    }
