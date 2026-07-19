"""Multi-candidate retrieval experiments (#1844, scope 3-4).

An *experiment* replays one baseline profile plus N candidate profiles against a
single pinned evaluation case set and compares every candidate with the
baseline, producing one deterministic, PR-attachable result document. It never
selects a winner or changes defaults — it lays out the trade-offs.

Determinism and isolation are inherited, not re-invented:

- Every replay runs through :func:`memtomem.quality.replay.replay_cases` with
  ``record=False`` — no access counters, caches, observations, or feedback are
  touched — at one pinned ``as_of`` over one resolved case set.
- Each candidate config is produced by
  :func:`memtomem.quality.profiles.apply_profile` (pure) and run on its own
  transient component stack, so the user's project/global configuration is never
  mutated.
- Because every replay reads the same live database, the profile-INDEPENDENT
  fingerprints must be stable: the ``corpus`` and ``case_set`` axes are asserted
  equal across all reports, and a full profile-independent corpus/index snapshot
  is taken from storage before and after the run (see ``_shared_snapshot``) to
  catch a concurrent writer. The per-report ``index`` fingerprint is *not*
  compared across reports — it legitimately varies by profile, since
  ``current_fingerprints`` folds access-count / link artifacts in only when a
  profile reads them — so a candidate that enables those stages is not mistaken
  for drift.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from memtomem.errors import EvalCaseError
from memtomem.quality.compare import compare_reports
from memtomem.quality.gate import evaluate_gate
from memtomem.quality.profiles import (
    RetrievalProfileDoc,
    apply_profile,
    profile_doc_fingerprint,
    profile_warnings,
)
from memtomem.quality.replay import MAX_AS_OF_UNIX, _select_case_ids, replay_cases

if TYPE_CHECKING:
    from memtomem.quality.gate import GatePolicy
    from memtomem.server.component_factory import Components

__all__ = [
    "EXPERIMENT_SCHEMA_VERSION",
    "EXPERIMENT_KIND",
    "ProfileRun",
    "assemble_experiment",
    "run_experiment",
    "serialize_experiment",
]

EXPERIMENT_SCHEMA_VERSION = 1
EXPERIMENT_KIND = "quality_experiment"

_AMBIENT_NAME = "ambient"
# Fingerprint axes that are profile-INDEPENDENT and so must be identical for
# every replay in one run: the retrieval-visible corpus and the labeled case
# set. ``index`` is deliberately NOT here — ``current_fingerprints`` folds the
# access-count / link-topology artifacts into the index fingerprint only when a
# profile's config reads them, so two profiles over the identical database
# legitimately produce different index fingerprints. The profile-independent
# index snapshot is captured separately from storage (see ``_shared_snapshot``).
_SHARED_FINGERPRINT_AXES = ("corpus", "case_set")


def _shared_snapshot(storage: Any) -> dict[str, str]:
    """A profile-independent fingerprint of the shared corpus + index.

    Unlike the per-report index fingerprint (which includes optional artifacts
    only when the *profile* reads them), this always folds in the full superset
    — vectors, FTS rows, link topology, and access counts — so it depends only
    on the database, not on any candidate config. Captured before and after the
    replays to detect a concurrent writer mutating the shared snapshot mid-run.
    """
    from memtomem.quality.fingerprints import corpus_fingerprint, index_fingerprint

    corpus_rows = storage.read_corpus_fingerprint_rows()
    return {
        "corpus": corpus_fingerprint(corpus_rows),
        "index": index_fingerprint(
            corpus_rows,
            storage.read_vector_fingerprint_rows(),
            storage.read_fts_fingerprint_rows(),
            storage.stored_embedding_info,
            link_rows=storage.read_link_topology_rows(),
            access_rows=storage.read_access_counts(),
        ),
    }


def serialize_experiment(doc: dict[str, Any]) -> str:
    """Canonical, byte-stable serialization of an experiment result."""
    return json.dumps(doc, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


@dataclass(frozen=True)
class ProfileRun:
    """One replayed profile: its identity plus the replay report it produced."""

    name: str
    source: str  # "ambient" | "document"
    document: dict[str, Any] | None
    document_fingerprint: str | None
    report: dict[str, Any]
    warnings: tuple[str, ...] = ()


def _entry(run: ProfileRun) -> dict[str, Any]:
    """The per-profile summary block shared by baseline and candidates."""
    report = run.report
    return {
        "profile_name": run.name,
        "source": run.source,
        "document": run.document,
        "document_fingerprint": run.document_fingerprint,
        "profile_fingerprint": report["fingerprints"]["profile"],
        "deterministic": report["deterministic"],
        "nondeterministic_stages": report["nondeterministic_stages"],
        "profile_knobs": report["profile_knobs"],
        "warnings": list(run.warnings),
        "counts": report["counts"],
        "aggregate": report["aggregate"],
    }


def assemble_experiment(
    baseline: ProfileRun,
    candidates: list[ProfileRun],
    *,
    shared_snapshot: dict[str, str],
    policy: GatePolicy | None = None,
) -> dict[str, Any]:
    """Build the deterministic experiment result from replayed profiles.

    ``shared_snapshot`` is the profile-independent ``{corpus, index}`` fingerprint
    captured from storage (see :func:`_shared_snapshot`). Pure. Raises
    :class:`~memtomem.errors.EvalCaseError` on an empty candidate set, duplicate
    profile names, or profile-independent (corpus/case-set) fingerprint drift
    across reports — none of which can yield a trustworthy comparison.
    """
    # Presence + name-uniqueness are validated up front in run_experiment (before
    # any replay); these are the pure re-check for callers that build ProfileRuns
    # directly. Keep the two conditions in sync if either is changed.
    if not candidates:
        raise EvalCaseError("an experiment needs at least one candidate profile")

    names = [baseline.name] + [c.name for c in candidates]
    if len(names) != len(set(names)):
        raise EvalCaseError("profile names must be unique across the baseline and candidates")

    all_runs = [baseline, *candidates]
    # Only the profile-independent axes are compared across reports; the index
    # fingerprint legitimately varies by profile, so its stability is enforced
    # by the storage-level snapshot instead (checked in run_experiment).
    shared = {axis: baseline.report["fingerprints"][axis] for axis in _SHARED_FINGERPRINT_AXES}
    for run in all_runs:
        for axis in _SHARED_FINGERPRINT_AXES:
            if run.report["fingerprints"][axis] != shared[axis]:
                raise EvalCaseError(
                    f"{axis} fingerprint drifted across replays — the corpus or case "
                    "set changed mid-experiment; results are not comparable"
                )
    if shared["corpus"] != shared_snapshot["corpus"]:
        raise EvalCaseError("corpus fingerprint drifted between the snapshot and the replays")

    ordered = sorted(candidates, key=lambda c: c.name)
    candidate_entries: list[dict[str, Any]] = []
    for cand in ordered:
        comparison = compare_reports(baseline.report, cand.report)
        entry = _entry(cand)
        entry["comparison"] = comparison
        entry["gate"] = evaluate_gate(comparison, policy) if policy is not None else None
        candidate_entries.append(entry)

    return {
        "schema_version": EXPERIMENT_SCHEMA_VERSION,
        "kind": EXPERIMENT_KIND,
        "as_of_unix": baseline.report["as_of_unix"],
        "deterministic": all(r.report["deterministic"] for r in all_runs),
        "policy_supplied": policy is not None,
        "case_count": baseline.report["counts"]["replayed"],
        "fingerprints": {
            "corpus": shared_snapshot["corpus"],
            "index": shared_snapshot["index"],
            "case_set": shared["case_set"],
        },
        "baseline": _entry(baseline),
        "candidates": candidate_entries,
    }


def _embed_document(doc: RetrievalProfileDoc) -> tuple[dict[str, Any], str]:
    """Return the portable, non-secret document form and its fingerprint."""
    fingerprint, canonical = profile_doc_fingerprint(doc)
    embedded = {
        "schema_version": doc.schema_version,
        "kind": doc.kind,
        "name": doc.name,
        "description": doc.description,
        "knobs": canonical,
    }
    return embedded, fingerprint


async def _replay_profile(
    components: Components,
    *,
    name: str,
    doc: RetrievalProfileDoc | None,
    case_ids: list[str],
    as_of_unix: int,
) -> ProfileRun:
    """Replay one profile — the ambient config, or a document on a fresh stack."""
    if doc is None:
        report = await replay_cases(
            components.storage,
            components.search_pipeline,
            components.config,
            case_ids=case_ids,
            as_of_unix=as_of_unix,
        )
        return ProfileRun(
            name=name, source="ambient", document=None, document_fingerprint=None, report=report
        )

    from memtomem.server.component_factory import close_components, create_components

    candidate_config = apply_profile(components.config, doc)
    # Defense in depth: the profile schema forbids the tokenizer (it is pinned by
    # the FTS index), but assert it so a future schema gap cannot leak the global
    # ``set_tokenizer`` side effect into later replays via ``create_components``.
    # A wrong storage path is not guarded here — it is caught by the
    # corpus/index fingerprint drift check in ``assemble_experiment`` — and the
    # config-override path stores sqlite_path as a raw str, so a Path/str compare
    # here would false-positive. Compare tokenizers by str for the same reason.
    if str(candidate_config.search.tokenizer) != str(components.config.search.tokenizer):
        raise EvalCaseError("a profile cannot change the FTS tokenizer")

    embedded, fingerprint = _embed_document(doc)
    warnings = tuple(profile_warnings(candidate_config, doc))
    transient = await create_components(candidate_config, load_ambient_config=False)
    try:
        report = await replay_cases(
            transient.storage,
            transient.search_pipeline,
            candidate_config,
            case_ids=case_ids,
            as_of_unix=as_of_unix,
        )
    finally:
        await close_components(transient)
    return ProfileRun(
        name=doc.name,
        source="document",
        document=embedded,
        document_fingerprint=fingerprint,
        report=report,
        warnings=warnings,
    )


async def run_experiment(
    components: Components,
    *,
    baseline_doc: RetrievalProfileDoc | None,
    candidate_docs: Sequence[RetrievalProfileDoc],
    case_selectors: Sequence[str] | None = None,
    as_of_unix: int | None = None,
    policy: GatePolicy | None = None,
) -> dict[str, Any]:
    """Replay a baseline plus candidates against one case set and assemble a result.

    ``baseline_doc=None`` uses the ambient effective config as the baseline
    (named ``"ambient"``). ``as_of_unix`` is pinned once and shared by every
    replay; the case set is resolved once so all replays run the identical
    cohort. The user's configuration is never mutated (candidates run on
    transient stacks) and no search-history state changes (``record=False``).
    """
    if as_of_unix is not None and not 0 <= as_of_unix <= MAX_AS_OF_UNIX:
        raise EvalCaseError(f"as_of_unix must be between 0 and {MAX_AS_OF_UNIX} (a unix timestamp)")
    pinned = int(time.time()) if as_of_unix is None else int(as_of_unix)

    # Validate presence + name uniqueness up front, BEFORE selecting cases or
    # building any transient stack — an invalid experiment must not open a
    # component stack or make an embedding/rerank call only to exit later.
    baseline_name = _AMBIENT_NAME if baseline_doc is None else baseline_doc.name
    names = [baseline_name] + [doc.name for doc in candidate_docs]
    if not candidate_docs:
        raise EvalCaseError("an experiment needs at least one candidate profile")
    if len(names) != len(set(names)):
        raise EvalCaseError("profile names must be unique across the baseline and candidates")

    # Resolve the cohort once against the live DB so every replay runs the same
    # cases even if a case's status changes mid-run.
    selectors = list(case_selectors) if case_selectors else None
    ordered_ids, _explicit, _archived = await _select_case_ids(components.storage, selectors)
    if not ordered_ids:
        raise EvalCaseError("no evaluation cases selected")

    # Profile-independent snapshot before the replays; re-checked after so a
    # concurrent writer that mutates the shared corpus/index at any point during
    # the run (including during the last candidate's searches) is caught.
    snapshot = _shared_snapshot(components.storage)

    baseline = await _replay_profile(
        components,
        name=_AMBIENT_NAME,
        doc=baseline_doc,
        case_ids=ordered_ids,
        as_of_unix=pinned,
    )
    candidates = [
        await _replay_profile(
            components, name=doc.name, doc=doc, case_ids=ordered_ids, as_of_unix=pinned
        )
        for doc in candidate_docs
    ]

    if _shared_snapshot(components.storage) != snapshot:
        raise EvalCaseError(
            "the shared corpus/index changed during the experiment; results are not comparable"
        )
    return assemble_experiment(baseline, candidates, shared_snapshot=snapshot, policy=policy)
