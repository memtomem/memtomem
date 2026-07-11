"""Typed response models for the Context Gateway wire (#1692 PR 7).

These models formalize the shapes the gateway routes have emitted as plain
dicts since the campaign stabilized them (PRs 0–6). Attaching them via
``response_model=`` makes shape drift a loud 500 instead of a silent wire
change; the golden wire fixtures (``tests/test_web_wire_fixtures*.py``)
remain the byte-level pin.

Ground rules for editing this module:

- **Field order is wire order.** The goldens compare rendered JSON text, so
  a model's field declaration order must transcribe its builder's dict
  insertion order exactly. Additive fields go LAST, matching the handlers'
  append-only discipline.
- **Exactly one field in this module has a default** —
  ``ContextImportReport.dry_run``. Every other field is required (nullable
  where the builder emits ``None``), so a handler that stops emitting a key
  fails validation instead of silently null-filling the wire. The routes
  that need omitted-key semantics (overview, projects, imports) pair their
  model with ``response_model_exclude_unset=True``; any future defaulted
  field on those models must be unconditionally set by the handler or it
  will vanish from the wire.
- **``extra="allow"`` only for verbatim-embedded bodies.** Sync-all phase
  entries carry the per-type engine reports untouched (module contract in
  ``routes/context_sync_all.py``); their shapes are pinned by the goldens,
  not by these models.
- **Open vocabularies stay ``str`` / ``dict[str, int]``** (row states,
  reason codes, per-status diff counts with conditional and hyphenated
  keys); ``Literal`` is reserved for values the route itself constructs
  from a closed set.
- Unions are non-discriminated on purpose: the natural ``status``
  discriminators collide across variants (``"error"``, ``"failed"``), and
  every union below has disjoint *required* fields, so smart-union
  validation resolves exactly one member.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

__all__ = [
    "ContextErrorEnvelope",
    "ContextImportNeedsConfirmation",
    "ContextImportReport",
    "ContextImportSkippedRow",
    "ContextImportedArtifact",
    "ContextKindCountError",
    "ContextOverviewResponse",
    "ContextProjectScope",
    "ContextProjectsResponse",
    "ContextRegistryWarning",
    "ContextRuntimeCoverageEntry",
    "ContextRuntimeRegistration",
    "ContextRuntimesResponse",
    "ContextSettingsError",
    "ContextSettingsSummary",
    "ContextStatusAllCrashed",
    "ContextStatusAllProject",
    "ContextStatusAllResponse",
    "ContextStatusAllSkipped",
    "ContextStatusAllSummary",
    "ContextStatusRow",
    "ContextStatusWarning",
    "ContextSyncAllProjectsReport",
    "ContextSyncAllProjectsSummary",
    "ContextSyncAllReport",
    "ContextSyncAllSummary",
    "ContextSyncPhase",
    "ContextSyncPhaseError",
    "ContextSyncProjectExecuted",
    "ContextSyncProjectFailed",
    "ContextSyncProjectSkipped",
    "ContextWikiInstalls",
]


# ── Shared leaves ─────────────────────────────────────────────────────────


class ContextKindCountError(BaseModel):
    """Count-shaped per-kind error envelope (``_error_payload(shape="total")``)."""

    total: int
    error: bool
    error_kind: str
    error_message: str


class ContextSettingsSummary(BaseModel):
    """``summarize_settings_statuses`` roll-up — ``error`` is a COUNT here,
    ``status`` the roll-up string (``in_sync``/``out_of_sync``/``error``)."""

    total: int
    in_sync: int
    out_of_sync: int
    missing_target: int
    error: int
    status: str


class ContextSettingsError(BaseModel):
    """Status-shaped settings error envelope (``_error_payload(shape="status")``)."""

    status: Literal["error"]
    error_kind: str
    error_message: str


#: Per-kind diff summary: an open count vocabulary (``summarize_diff_with_canonical``
#: emits one snake_cased key per engine status, conditionally) or the count-shaped
#: error envelope when the diff scan raised. Typed models first so smart-union
#: strict validation resolves them before the open dict.
ContextKindCounts = ContextKindCountError | dict[str, int]


class ContextRuntimeCoverageEntry(BaseModel):
    """``compute_runtime_coverage`` entry.

    ``installed`` / ``memtomem_registered`` are conditionally OMITTED (not
    null) when the registry probe had no row for the runtime — the consuming
    routes pass ``response_model_exclude_unset=True`` to preserve that.
    """

    name: str
    available: bool
    installed: bool | None = None
    memtomem_registered: bool | None = None


class ContextErrorEnvelope(BaseModel):
    """Status-all collector-crash envelope (``error_kind, message, http_status``
    order — the sync-all surfaces order theirs differently, see
    :class:`ContextSyncPhaseError`)."""

    error_kind: str
    message: str
    http_status: int


# ── GET /context/overview ─────────────────────────────────────────────────


class ContextWikiInstalls(BaseModel):
    total: int
    behind: int


class ContextOverviewResponse(BaseModel):
    target_scope: str
    project_root: str
    detected_runtimes: list[ContextRuntimeCoverageEntry]
    last_synced_at: str | None
    wiki_installs: ContextWikiInstalls | None
    skills: ContextKindCounts
    commands: ContextKindCounts
    agents: ContextKindCounts
    mcp_servers: ContextKindCounts
    settings: ContextSettingsSummary | ContextSettingsError
    # Appended last: the wire goldens pin key positions, so additive fields
    # never displace existing keys (#1692 PR 5 precedent).
    detected_runtimes_unavailable: bool


# ── GET /context/runtimes ─────────────────────────────────────────────────


class ContextRuntimeRegistration(BaseModel):
    """``RuntimeStatus.to_dict()`` — per-client probe failures ride in
    ``error_kind``; the response-level ``runtimes_status`` is disjoint."""

    name: str
    installed: bool
    memtomem_registered: bool
    mms_registered: bool
    registered_locations: list[str]
    config_paths: list[str]
    error_kind: str | None


class ContextStatusWarning(BaseModel):
    """Runtimes-route availability warning — the registry warning minus
    ``skipped_rows`` (no row concept there), by design."""

    reason_code: str
    error_kind: str
    message: str
    retryable: bool


class ContextRuntimesResponse(BaseModel):
    project_root: str
    runtimes: list[ContextRuntimeRegistration]
    runtimes_status: Literal["ok", "unavailable"]
    warnings: list[ContextStatusWarning]


# ── GET /context/projects ─────────────────────────────────────────────────


class ContextRegistryWarning(BaseModel):
    """``known_projects.json`` load-report warning (#1699). ``skipped_rows``
    is non-null only for row-level degradation."""

    reason_code: str
    error_kind: str
    message: str
    retryable: bool
    skipped_rows: int | None


class ContextProjectScope(BaseModel):
    """One ``_scope_to_dict`` row. ``counts`` / ``runtime_coverage`` and their
    ``*_unavailable`` sidecars are ``None`` when their ``?include=`` section
    wasn't requested — null means "not computed", distinct from empty."""

    project_scope_id: str
    scope_id: str
    label: str
    root: str | None
    tier: str
    sources: list[str]
    missing: bool
    stale: bool
    experimental: bool
    enabled: bool
    sync_eligible: bool
    counts: dict[str, int] | None
    runtime_coverage: list[ContextRuntimeCoverageEntry] | None
    counts_unavailable: list[str] | None
    runtime_coverage_unavailable: bool | None


class ContextProjectsResponse(BaseModel):
    target_scope: str
    scopes: list[ContextProjectScope]
    registry_status: Literal["ok", "unavailable"]
    warnings: list[ContextRegistryWarning]


# ── GET /context/status-all ───────────────────────────────────────────────


class ContextStatusAllSkipped(BaseModel):
    """Ineligible-scope entry (shared ``sync_skip_reason`` codes)."""

    project_scope_id: str
    label: str
    root: str | None
    status: Literal["skipped"]
    reason_code: str
    message: str


class ContextStatusAllCrashed(BaseModel):
    """Total collector-crash entry — the entry-level ``error`` object is
    reserved for this; per-kind probe failures stay inside ``diff_counts``."""

    project_scope_id: str
    label: str
    root: str
    status: Literal["error"]
    error: ContextErrorEnvelope


class ContextStatusRow(BaseModel):
    """Serialized ``StatusRow``. ``pin_commit`` / ``installed_at`` are empty
    strings (not null) for draft rows; ``state`` stays open for future
    ``StatusState`` values (new states must not 500 the fleet view)."""

    asset_type: str
    name: str
    pin_commit: str
    installed_at: str
    state: str
    dirty_file_count: int
    reason: str | None
    tier: str


class ContextStatusAllProject(BaseModel):
    """Executed-project entry (``_status_all_entry``). ``status: "error"``
    here means a corrupt lockfile or a per-kind diff probe that raised —
    distinct from :class:`ContextStatusAllCrashed` (disjoint required
    fields resolve the union, not the colliding ``status`` values)."""

    project_scope_id: str
    label: str
    root: str
    status: Literal["ok", "drift", "error"]
    wiki_head: str | None
    lockfile_error: str | None
    state_counts: dict[str, int]
    diff_counts: dict[str, ContextSettingsSummary | ContextSettingsError | ContextKindCounts]
    rows: list[ContextStatusRow]


class ContextStatusAllSummary(BaseModel):
    """Counts only — deliberately no roll-up status string; fleet health is
    ``drifted + errors == 0``, derivable."""

    projects_total: int
    executed: int
    drifted: int
    clean: int
    errors: int
    skipped: int


class ContextStatusAllResponse(BaseModel):
    target_scope: str
    projects: list[ContextStatusAllSkipped | ContextStatusAllCrashed | ContextStatusAllProject]
    summary: ContextStatusAllSummary


# ── POST /context/sync-all (+ /context/sync-all-projects) ────────────────


class ContextSyncPhase(BaseModel):
    """One phase entry: ``{type, status, **native}``.

    The per-type engine reports (and a failed phase's ``error`` envelope,
    which can carry extra detail keys like strict-drop's ``generated``) ride
    as extras BY CONTRACT — phase entries embed the native bodies verbatim
    (ADR-0024; ``routes/context_sync_all.py`` module docstring). Their
    shapes are pinned by the wire goldens, not this model. One permissive
    model for all variants on purpose: the settings phase can be ``failed``
    with native ``results`` and no ``error`` key, so an ok/failed model
    split would either strip keys or null-inject.
    """

    model_config = ConfigDict(extra="allow")

    type: str
    status: Literal["ok", "failed", "needs_confirmation"]


class ContextSyncAllSummary(BaseModel):
    """Run-level roll-up (``_summarize``) — totals are counts, not
    classifications; skip severity stays with per-row ``reason_code``."""

    status: Literal["ok", "failed", "partial"]
    changed: bool
    outcome: Literal["changed", "noop"]
    ok: int
    failed: int
    needs_confirmation: int
    generated_total: int
    skipped_total: int


class ContextSyncAllReport(BaseModel):
    phases: list[ContextSyncPhase]
    summary: ContextSyncAllSummary


class ContextSyncPhaseError(BaseModel):
    """Batch project-level failure envelope (``error_kind, http_status,
    message`` order — matches ``_phase_error_envelope``, differs from the
    status-all :class:`ContextErrorEnvelope`). ``extra="allow"``: dict
    details keep their extra keys so partial fan-out stays visible."""

    model_config = ConfigDict(extra="allow")

    error_kind: str
    http_status: int
    message: str


class ContextSyncProjectSkipped(BaseModel):
    project_scope_id: str
    label: str
    root: str | None
    status: Literal["skipped"]
    reason_code: str
    message: str


class ContextSyncProjectFailed(BaseModel):
    """Engine error or lock timeout for one project — completed ``phases``
    are kept (their writes are real); no ``summary`` (the loop didn't
    finish), which is what disambiguates this from
    :class:`ContextSyncProjectExecuted` at ``status: "failed"``."""

    project_scope_id: str
    label: str
    root: str
    status: Literal["failed"]
    phases: list[ContextSyncPhase]
    error: ContextSyncPhaseError


class ContextSyncProjectExecuted(BaseModel):
    """Completed project entry — ``status`` mirrors its own ``summary.status``."""

    project_scope_id: str
    label: str
    root: str
    status: Literal["ok", "failed", "partial"]
    phases: list[ContextSyncPhase]
    summary: ContextSyncAllSummary


class ContextSyncAllProjectsSummary(BaseModel):
    """Batch roll-up (``_summarize_projects``)."""

    status: Literal["ok", "failed", "partial"]
    projects_total: int
    executed: int
    ok: int
    partial: int
    failed: int
    skipped: int
    generated_total: int
    skipped_rows_total: int


class ContextSyncAllProjectsReport(BaseModel):
    projects: list[
        ContextSyncProjectSkipped | ContextSyncProjectFailed | ContextSyncProjectExecuted
    ]
    summary: ContextSyncAllProjectsSummary


# ── POST /context/<kind>/import family ────────────────────────────────────


class ContextImportedArtifact(BaseModel):
    name: str
    canonical_path: str
    source_runtime: str | None
    duplicate_candidates: list[str]


class ContextImportSkippedRow(BaseModel):
    name: str
    reason: str
    reason_code: str


class ContextImportReport(BaseModel):
    """``_import_payload`` shape, shared by every import route and the
    host-write gate's nested ``plan``.

    ``dry_run`` is the ONLY defaulted field in this module: the bulk routes
    always emit it, the single-item routes never do, and the import routes
    pair this model with ``response_model_exclude_unset=True`` so an
    omitted key stays omitted (absent ≠ ``null`` on this wire).
    """

    imported: list[ContextImportedArtifact]
    skipped: list[ContextImportSkippedRow]
    project_root: str
    scanned_dirs: list[str]
    dry_run: bool | None = None


class ContextImportNeedsConfirmation(BaseModel):
    """Unconfirmed user-tier import: the ``host_write_gate`` disclosure
    envelope with the dry-run preview nested under ``plan`` (#1263)."""

    status: Literal["needs_confirmation"]
    confirm: str
    reason: str
    host_targets: list[str]
    plan: ContextImportReport
