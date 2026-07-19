"""Versioned, non-secret retrieval-profile documents (#1844).

A *retrieval profile* is a portable JSON document naming a set of
ranking-affecting configuration knobs — enough to explain or reproduce a replay
report's ``fingerprints.profile`` hash without ever carrying a secret, an
absolute path, or a value tied to one machine. Profiles let a reviewer describe
"BM25-only with 14-day decay" once and replay it against an evaluation case set
on any install (see :mod:`memtomem.quality.experiment`).

Design invariants (mirrored from :mod:`memtomem.quality.fingerprints` and the
project's privacy contract):

1. **Only index-compatible, non-secret sections are profile-eligible.** Embedding
   identity, the FTS tokenizer, storage/index paths, and LLM credentials are
   *pinned* to the ambient config — the shared corpus/index snapshot was built
   with them, so a document that changed them could not search the same vectors.
   They are rejected with a targeted error, never silently applied.
2. **Secrets/paths cannot persist or echo.** Secret-shaped field *names* are
   rejected at any depth (:func:`~memtomem.secret_masking.is_secret_key`); the
   only free-string values that survive into the canonical form (``name``,
   ``description``, ``rerank.model``) are secret-scanned and path-shape rejected.
   Validation errors are built from field locations + controlled messages and
   never chain the offending input.
3. **Equivalent definitions share one fingerprint.** Canonicalization resolves
   every omitted eligible knob to its package default and normalizes floats, so
   an omitted field and its explicitly-written default hash identically. The
   same defaults-resolved values are what :func:`apply_profile` executes, so a
   document cannot hash as one profile and run as another.
"""

from __future__ import annotations

import math
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from memtomem.config import (
    AccessConfig,
    ContextWindowConfig,
    DecayConfig,
    ImportanceConfig,
    Mem2MemConfig,
    MMRConfig,
    QueryExpansionConfig,
    RerankConfig,
    SearchConfig,
    SessionSummaryConfig,
)
from memtomem.errors import EvalCaseValidationError
from memtomem.privacy import scan
from memtomem.quality.fingerprints import _sha256_json
from memtomem.secret_masking import is_secret_key

__all__ = [
    "PROFILE_SCHEMA_VERSION",
    "PROFILE_KIND",
    "RetrievalProfileDoc",
    "load_profile_document",
    "canonicalize_profile",
    "profile_doc_fingerprint",
    "apply_profile",
    "profile_warnings",
]

PROFILE_SCHEMA_VERSION = 1
PROFILE_KIND = "retrieval_profile"

# Each eligible section maps to the config section class used to resolve
# omitted knobs to package defaults (and to run that section's own validators).
# The order here is the canonical section order in the fingerprint payload.
_SECTION_CLASSES: dict[str, type[BaseModel]] = {
    "search": SearchConfig,
    "decay": DecayConfig,
    "mmr": MMRConfig,
    "access": AccessConfig,
    "importance": ImportanceConfig,
    "context_window": ContextWindowConfig,
    "rerank": RerankConfig,
    "query_expansion": QueryExpansionConfig,
    "session_summary": SessionSummaryConfig,
}

# The eligible fields per section — the ranking-affecting, non-secret,
# index-independent subset. Mirrors the profile_fingerprint allowlist. Every
# other field of these sections (tokenizer, cache_ttl, api_key, the summary
# generation knobs, …) is pinned to the ambient config and folded back in
# :func:`apply_profile`.
_ELIGIBLE_FIELDS: dict[str, tuple[str, ...]] = {
    "search": (
        "default_top_k",
        "bm25_candidates",
        "dense_candidates",
        "rrf_k",
        "enable_bm25",
        "enable_dense",
        "rrf_weights",
    ),
    "decay": ("enabled", "half_life_days"),
    "mmr": ("enabled", "lambda_param"),
    "access": ("enabled", "max_boost"),
    "importance": ("enabled", "max_boost", "weights"),
    "context_window": ("enabled", "window_size"),
    "rerank": ("enabled", "provider", "model", "oversample", "min_pool", "max_pool"),
    "query_expansion": ("enabled", "max_terms", "strategy"),
    "session_summary": (
        "expansion_lookup_top_k",
        "expansion_score_threshold",
        "expansion_rescue_weight",
    ),
}

# Config sections that are pinned to the ambient install wholesale. Named here
# only to give a document author a targeted error instead of a generic
# "extra inputs are not permitted". Not exhaustive — the ``extra="forbid"`` on
# ``ProfileKnobs`` is the backstop for anything not listed.
_PINNED_SECTIONS: frozenset[str] = frozenset(
    {"embedding", "llm", "storage", "indexing", "namespace", "webhook", "session_trace"}
)

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_DESCRIPTION_MAX_LEN = 500
_MODEL_MAX_LEN = 200
# A reranker model id is one or two plain segments joined by a single slash
# (e.g. ``Xenova/ms-marco-MiniLM-L-6-v2``). No leading dot, no drive letter, no
# ``..``, no backslash — those are path shapes, not model ids.
_MODEL_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


# --------------------------------------------------------------------------- #
# Scalar guards (mode="before"): run only for explicitly-present fields, before
# strict coercion. They reject the traps strict mode misses (bool-for-int,
# NaN/Inf, oversized ints) and normalize int→float for float knobs so a JSON
# author can write ``14`` for a float field. An explicit ``null`` is rejected
# (real config fields are non-nullable — null-as-omission would fail later, in
# the wrong layer); omit the field to take the default.
# --------------------------------------------------------------------------- #
def _reject_null(v: Any, name: str) -> None:
    if v is None:
        raise ValueError(f"{name}: null is not allowed; omit the field to use its default")


def _guard_int(v: Any, name: str) -> int:
    _reject_null(v, name)
    if isinstance(v, bool) or not isinstance(v, int):
        raise ValueError(f"{name} must be an integer")
    return v


def _guard_bool(v: Any, name: str) -> bool:
    _reject_null(v, name)
    if not isinstance(v, bool):
        raise ValueError(f"{name} must be a boolean")
    return v


def _guard_float(v: Any, name: str) -> float:
    _reject_null(v, name)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError(f"{name} must be a number")
    try:
        fv = float(v)
    except (OverflowError, ValueError):  # oversized int
        raise ValueError(f"{name} is out of range") from None
    if not math.isfinite(fv):
        raise ValueError(f"{name} must be a finite number")
    return fv


def _guard_float_list(v: Any, name: str, *, length: int) -> list[float]:
    _reject_null(v, name)
    if not isinstance(v, list):
        raise ValueError(f"{name} must be a list of {length} numbers")
    if len(v) != length:
        raise ValueError(f"{name} must have exactly {length} elements")
    out = [_guard_float(x, f"{name}[]") for x in v]
    if any(x < 0 for x in out):
        raise ValueError(f"{name} entries must be >= 0")
    return out


# --------------------------------------------------------------------------- #
# Per-section knob models. All strict + extra="forbid": unknown/pinned fields
# are rejected here (the before-validators above supply the type discipline
# strict mode does not, and normalize int→float for float knobs).
# --------------------------------------------------------------------------- #
class _Knobs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class SearchKnobs(_Knobs):
    default_top_k: int | None = None
    bm25_candidates: int | None = None
    dense_candidates: int | None = None
    rrf_k: int | None = None
    enable_bm25: bool | None = None
    enable_dense: bool | None = None
    rrf_weights: list[float] | None = None

    @field_validator("default_top_k", "bm25_candidates", "dense_candidates", "rrf_k", mode="before")
    @classmethod
    def _ints(cls, v: Any, info: Any) -> Any:
        return _guard_int(v, info.field_name)

    @field_validator("enable_bm25", "enable_dense", mode="before")
    @classmethod
    def _bools(cls, v: Any, info: Any) -> Any:
        return _guard_bool(v, info.field_name)

    @field_validator("rrf_weights", mode="before")
    @classmethod
    def _weights(cls, v: Any) -> Any:
        return _guard_float_list(v, "rrf_weights", length=2)


class DecayKnobs(_Knobs):
    enabled: bool | None = None
    half_life_days: float | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _bool(cls, v: Any, info: Any) -> Any:
        return _guard_bool(v, info.field_name)

    @field_validator("half_life_days", mode="before")
    @classmethod
    def _float(cls, v: Any, info: Any) -> Any:
        return _guard_float(v, info.field_name)


class MMRKnobs(_Knobs):
    enabled: bool | None = None
    lambda_param: float | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _bool(cls, v: Any, info: Any) -> Any:
        return _guard_bool(v, info.field_name)

    @field_validator("lambda_param", mode="before")
    @classmethod
    def _float(cls, v: Any, info: Any) -> Any:
        return _guard_float(v, info.field_name)


class AccessKnobs(_Knobs):
    enabled: bool | None = None
    max_boost: float | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _bool(cls, v: Any, info: Any) -> Any:
        return _guard_bool(v, info.field_name)

    @field_validator("max_boost", mode="before")
    @classmethod
    def _float(cls, v: Any, info: Any) -> Any:
        return _guard_float(v, info.field_name)


class ImportanceKnobs(_Knobs):
    enabled: bool | None = None
    max_boost: float | None = None
    weights: list[float] | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _bool(cls, v: Any, info: Any) -> Any:
        return _guard_bool(v, info.field_name)

    @field_validator("max_boost", mode="before")
    @classmethod
    def _float(cls, v: Any, info: Any) -> Any:
        return _guard_float(v, info.field_name)

    @field_validator("weights", mode="before")
    @classmethod
    def _weights(cls, v: Any) -> Any:
        # Four recency/access/importance/length weights (see ImportanceConfig).
        return _guard_float_list(v, "weights", length=4)


class ContextWindowKnobs(_Knobs):
    enabled: bool | None = None
    window_size: int | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _bool(cls, v: Any, info: Any) -> Any:
        return _guard_bool(v, info.field_name)

    @field_validator("window_size", mode="before")
    @classmethod
    def _int(cls, v: Any, info: Any) -> Any:
        return _guard_int(v, info.field_name)


class RerankKnobs(_Knobs):
    enabled: bool | None = None
    provider: str | None = None  # closed vocabulary via _provider below
    model: str | None = None
    oversample: float | None = None
    min_pool: int | None = None
    max_pool: int | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _bool(cls, v: Any, info: Any) -> Any:
        return _guard_bool(v, info.field_name)

    @field_validator("oversample", mode="before")
    @classmethod
    def _float(cls, v: Any, info: Any) -> Any:
        return _guard_float(v, info.field_name)

    @field_validator("min_pool", "max_pool", mode="before")
    @classmethod
    def _ints(cls, v: Any, info: Any) -> Any:
        return _guard_int(v, info.field_name)

    @field_validator("provider", mode="before")
    @classmethod
    def _provider(cls, v: Any) -> Any:
        _reject_null(v, "provider")
        # Closed vocabulary — the underlying config field is an open string, so
        # the document layer is where the allowlist lives.
        if v not in ("fastembed", "local", "cohere"):
            raise ValueError("provider must be 'fastembed', 'local', or 'cohere'")
        return v

    @field_validator("model", mode="before")
    @classmethod
    def _model(cls, v: Any) -> Any:
        _reject_null(v, "model")
        if not isinstance(v, str):
            raise ValueError("model must be a string")
        if not v or len(v) > _MODEL_MAX_LEN:
            raise ValueError(f"model must be 1-{_MODEL_MAX_LEN} characters")
        if scan(v):
            # Secret-shaped model id — reject without echoing the value.
            raise ValueError("model must not contain a secret-shaped value")
        # A model id is one or two plain segments joined by a single slash. Any
        # other slash/backslash/drive/dot shape is a path, not a model id.
        segments = v.split("/")
        if len(segments) > 2 or not all(_MODEL_SEGMENT_RE.match(s) for s in segments):
            raise ValueError("model must be a plain model id (no path-shaped value)")
        return v


class QueryExpansionKnobs(_Knobs):
    enabled: bool | None = None
    max_terms: int | None = None
    strategy: str | None = None

    @field_validator("enabled", mode="before")
    @classmethod
    def _bool(cls, v: Any, info: Any) -> Any:
        return _guard_bool(v, info.field_name)

    @field_validator("max_terms", mode="before")
    @classmethod
    def _max_terms(cls, v: Any) -> Any:
        iv = _guard_int(v, "max_terms")
        if iv < 1:
            raise ValueError("max_terms must be >= 1")
        return iv

    @field_validator("strategy", mode="before")
    @classmethod
    def _strategy(cls, v: Any) -> Any:
        _reject_null(v, "strategy")
        if v not in ("tags", "headings", "both", "llm"):
            raise ValueError("strategy must be 'tags', 'headings', 'both', or 'llm'")
        return v


class SessionSummaryKnobs(_Knobs):
    # Only the rescue-leg knobs are eligible; the summary-generation knobs are
    # write-time and pinned to ambient. expansion_lookup_top_k stays >= 1 (the
    # core SessionSummaryConfig contract; rescue-off is a separate follow-up).
    expansion_lookup_top_k: int | None = None
    expansion_score_threshold: float | None = None
    expansion_rescue_weight: float | None = None

    @field_validator("expansion_lookup_top_k", mode="before")
    @classmethod
    def _lookup(cls, v: Any) -> Any:
        iv = _guard_int(v, "expansion_lookup_top_k")
        if iv < 1:
            raise ValueError("expansion_lookup_top_k must be >= 1")
        return iv

    @field_validator("expansion_score_threshold", "expansion_rescue_weight", mode="before")
    @classmethod
    def _floats(cls, v: Any, info: Any) -> Any:
        return _guard_float(v, info.field_name)


class ProfileKnobs(_Knobs):
    search: SearchKnobs = SearchKnobs()
    decay: DecayKnobs = DecayKnobs()
    mmr: MMRKnobs = MMRKnobs()
    access: AccessKnobs = AccessKnobs()
    importance: ImportanceKnobs = ImportanceKnobs()
    context_window: ContextWindowKnobs = ContextWindowKnobs()
    rerank: RerankKnobs = RerankKnobs()
    query_expansion: QueryExpansionKnobs = QueryExpansionKnobs()
    session_summary: SessionSummaryKnobs = SessionSummaryKnobs()


class RetrievalProfileDoc(BaseModel):
    """A validated, non-secret retrieval-profile document."""

    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int
    kind: str
    name: str
    description: str | None = None
    knobs: ProfileKnobs = ProfileKnobs()

    @model_validator(mode="before")
    @classmethod
    def _guard_envelope(cls, data: Any) -> Any:
        """Guard the envelope before pydantic coerces it.

        Rejects the ``Literal``-style int trap (``True``/``1.0`` for
        ``schema_version``), secret-shaped field names at any depth, and the
        known pinned config sections — each with a controlled, value-free
        message.
        """
        if not isinstance(data, dict):
            return data
        # schema_version: strict + a plain-int type check (bool is an int
        # subclass and 1.0 == 1, so the annotation alone is not enough).
        if "schema_version" in data:
            sv = data["schema_version"]
            if type(sv) is not int or sv != PROFILE_SCHEMA_VERSION:
                raise ValueError(f"schema_version must be {PROFILE_SCHEMA_VERSION}")
        if data.get("kind") != PROFILE_KIND:
            raise ValueError(f"kind must be {PROFILE_KIND!r}")

        _reject_secret_keys(data)

        knobs = data.get("knobs")
        if isinstance(knobs, dict):
            for section in knobs:
                if section in _PINNED_SECTIONS:
                    raise ValueError(
                        f"section {section!r} is pinned to the ambient config "
                        "and cannot appear in a profile document"
                    )
        return data

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError("name must be 1-64 chars of [a-z0-9_-] starting with [a-z0-9]")
        # The charset cannot express a path, but it can still spell a
        # secret-shaped token (e.g. an ``sk-`` prefix), so scan it — name feeds
        # the experiment output and ordering.
        if scan(v):
            raise ValueError("name must not contain a secret-shaped value")
        return v

    @field_validator("description")
    @classmethod
    def _description(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if len(v) > _DESCRIPTION_MAX_LEN:
            raise ValueError(f"description must be <= {_DESCRIPTION_MAX_LEN} characters")
        # Allow tab/newline/carriage-return so a note authored on Windows and
        # round-tripped as escaped "\r\n" in JSON is not rejected.
        if any(ord(c) < 32 and c not in "\t\n\r" for c in v):
            raise ValueError("description must not contain control characters")
        if "/" in v or "\\" in v or scan(v):
            raise ValueError("description must not contain a path or secret-shaped value")
        return v


def _reject_secret_keys(value: Any, *, path: str = "") -> None:
    """Recursively reject secret-shaped field names anywhere in the document."""
    if isinstance(value, dict):
        for key, child in value.items():
            if isinstance(key, str) and is_secret_key(key):
                loc = f"{path}.{key}" if path else key
                raise ValueError(f"secret-shaped field {loc!r} is not allowed")
            _reject_secret_keys(child, path=f"{path}.{key}" if path else str(key))
    elif isinstance(value, list):
        for i, child in enumerate(value):
            _reject_secret_keys(child, path=f"{path}[{i}]")


def _safe_validation_summary(exc: ValidationError) -> str:
    """Build an emit-safe error summary from a ValidationError.

    Uses only the field location and pydantic's message (which never echoes the
    offending value) — never ``err['input']`` — and the caller raises with
    ``from None`` so the chained cause cannot leak the input through a logged
    traceback.
    """
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"]) or "<root>"
        parts.append(f"{loc}: {err['msg']}")
    return "; ".join(parts)


def load_profile_document(data: Any) -> RetrievalProfileDoc:
    """Validate a parsed profile document.

    Runs the full validation surface — envelope guards, per-field type/shape
    discipline, and the section-constraint checks performed by
    :func:`canonicalize_profile` (positivity, range, pool bounds) — under one
    emit-safe error boundary. All failures raise
    :class:`~memtomem.errors.EvalCaseValidationError` with a message built from
    field locations only, never chaining the offending input.
    """
    try:
        doc = RetrievalProfileDoc.model_validate(data)
    except ValidationError as e:
        raise EvalCaseValidationError(
            f"invalid retrieval profile: {_safe_validation_summary(e)}"
        ) from None
    # Eagerly canonicalize so section-level constraint violations (e.g. a
    # negative top_k, max_pool < min_pool) surface here, under the same
    # boundary, rather than later at apply/fingerprint time.
    try:
        canonicalize_profile(doc)
    except ValidationError as e:
        raise EvalCaseValidationError(
            f"invalid retrieval profile: {_safe_validation_summary(e)}"
        ) from None
    return doc


def _normalize(value: Any) -> Any:
    """Deterministic float normalization (recurses into lists)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return 0.0 if value == 0 else value  # collapse -0.0
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def canonicalize_profile(doc: RetrievalProfileDoc) -> dict[str, Any]:
    """Return the defaults-resolved, normalized eligible knob dict.

    For each eligible section the explicitly-set fields are fed through the real
    config section class — resolving every omitted knob to its package default
    and running that section's own validators — then the eligible fields are
    extracted and float-normalized. An omitted field and its explicitly-written
    default therefore produce the same canonical value.
    """
    canonical: dict[str, Any] = {}
    for section, cls in _SECTION_CLASSES.items():
        knobs = getattr(doc.knobs, section)
        set_fields = knobs.model_dump(exclude_none=True)
        resolved = cls(**set_fields)
        canonical[section] = {
            field: _normalize(getattr(resolved, field)) for field in _ELIGIBLE_FIELDS[section]
        }
    return canonical


def profile_doc_fingerprint(doc: RetrievalProfileDoc) -> tuple[str, dict[str, Any]]:
    """Return ``(sha256, canonical_knobs)`` — the document's definition identity.

    Excludes ``name``/``description`` so renaming a profile does not change its
    identity. This is the portable, install-independent hash; the *effective*
    replay-condition hash remains
    :func:`~memtomem.quality.fingerprints.profile_fingerprint` over the applied
    config (which folds in the pinned ambient sections).
    """
    canonical = canonicalize_profile(doc)
    payload = {"schema_version": PROFILE_SCHEMA_VERSION, "knobs": canonical}
    return _sha256_json(payload), canonical


def apply_profile(ambient: Mem2MemConfig, doc: RetrievalProfileDoc) -> Mem2MemConfig:
    """Return a candidate config: ambient with the profile's eligible knobs.

    Pure — ``ambient`` is never mutated. The eligible fields of each eligible
    section are replaced by the *same* defaults-resolved canonical values used
    by :func:`profile_doc_fingerprint`; every pinned field (tokenizer,
    cache_ttl, api_key, the summary-generation knobs) and every pinned section
    (embedding, llm, storage, indexing, namespace, …) is kept from ``ambient``.

    The result is produced by ``model_validate`` on a full payload rather than
    ``model_copy(update=...)``: model_copy would skip root validation and share
    untouched nested sections with ``ambient``, whereas ``model_validate``
    revalidates the whole config into an independent object. It is inherited
    from ``BaseModel`` and does not re-run ``BaseSettings`` environment loading.
    """
    canonical = canonicalize_profile(doc)
    payload = ambient.model_dump(mode="python")
    # Round-tripping a dumped config back through model_validate re-runs the
    # section before-validators, including deprecation shims that warn when a
    # deprecated field is present. Strip such fields so apply stays quiet. Today
    # rerank.top_k (migrated to rerank.min_pool, whose effective value is already
    # in the payload) is the only one; extend this list if another section grows
    # a deprecated-on-load field, or those apply calls will each emit a warning.
    if isinstance(payload.get("rerank"), dict):
        payload["rerank"].pop("top_k", None)
    for section, eligible_values in canonical.items():
        payload[section] = {**payload[section], **eligible_values}
    return type(ambient).model_validate(payload)


def profile_warnings(config: Mem2MemConfig, doc: RetrievalProfileDoc) -> list[str]:
    """Non-fatal advisories about a profile that will run but may surprise.

    These are not errors — the profile is valid and replayable — but flag
    knob combinations whose effect is inert or nondeterministic so the
    experiment output can surface them.
    """
    warnings: list[str] = []
    if config.mmr.enabled and not config.search.enable_dense:
        # MMR diversifies over dense vectors; the pipeline skips it when dense
        # retrieval is off, so lambda_param is inert.
        warnings.append("mmr_inactive_dense_disabled")
    if (
        config.query_expansion.enabled
        and config.query_expansion.strategy == "llm"
        and not config.llm.enabled
    ):
        warnings.append("llm_strategy_without_provider")
    if config.rerank.enabled and config.rerank.provider == "cohere" and not config.rerank.api_key:
        warnings.append("rerank_cohere_without_api_key")
    return sorted(warnings)
