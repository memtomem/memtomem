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
    "ContextGlobalRuntimeCoverage",
    "ContextGlobalStoreCounts",
    "ContextPullApplyNeedsConfirmation",
    "ContextPullApplyResponse",
    "ContextPullDriftRow",
    "ContextPullDriftSummary",
    "ContextPullPreviewCandidate",
    "ContextPullPreviewResponse",
    "ContextStatusGlobalResponse",
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


# ── GET /context/{kind}/{name}/pull-preview (ADR-0030 PR-B) ──────────────


class ContextPullPreviewCandidate(BaseModel):
    """One runtime's Pull-preview row (ADR-0030 §4). Two orthogonal axes.

    ``content_status`` / ``gate_status`` are CLOSED sets the route constructs
    from :mod:`memtomem.context.pull_preview` — ``Literal`` (not open ``str``)
    so a token spelled ``not importable`` vs ``not_importable`` 500s loudly
    (per this module's ground rules; ``test_context_pull_preview.py`` pins the
    parity with the engine enums). ``reason`` is display-sanitized at the
    route (``sanitize_diff_reason``) — never the raw engine text."""

    runtime: str
    content_status: Literal[
        "new", "differs", "identical", "landing_error", "store_error", "not_importable"
    ]
    gate_status: Literal["ok", "blocked", "requires_unsafe_confirmation"] | None
    importable: bool
    landing_group: int | None
    override_warning: bool
    reason: str | None


class ContextPullPreviewResponse(BaseModel):
    """Read-only Pull preview for one ``(kind, name, target_scope)``.

    ``ambiguous`` (>1 distinct landing group, or a fail-closed landing error)
    and ``auto_source`` are the §5 signal the CLI/Web Pull surfaces enforce a
    refusal on later (PR-C/PR-D); this response only reports them."""

    kind: str
    name: str
    target_scope: str
    store_present: bool
    candidates: list[ContextPullPreviewCandidate]
    distinct_landing_count: int
    ambiguous: bool
    auto_source: str | None


# ── POST /context/{kind}/{name}/pull (ADR-0030 PR-D) ─────────────────────


class ContextPullApplyResponse(BaseModel):
    """Result of a Pull apply — ADR-0030 PR-D.

    Mirrors :class:`memtomem.context.pull_apply.PullApplyResult` on the wire.
    The engine is result-coded *on purpose* ("so the Web/MCP surfaces get a
    stable ``reason_code`` and the ``source_conflict`` payload travels with
    it"), so every prepare/commit *domain decision* — the ``applied`` write and
    every actionable refusal alike — returns HTTP 200 with this body (the four
    statuses with genuine HTTP meaning are mapped to error codes instead: see
    ``status`` below); the client picker branches
    on ``status``. Only the infrastructural ``lock_timeout`` escapes as a 503
    ``_error`` envelope (the one status the engine docstring maps to HTTP), so
    it is absent from ``status`` here.

    ``status`` is a CLOSED ``Literal`` — the *domain-decision* subset of the
    engine's ``PullApplyStatus`` the route returns on a 200. The four statuses
    that carry HTTP semantics are mapped to error codes and never appear in this
    body: ``lock_timeout`` → 503, ``plan_stale`` → 409, and the two
    infrastructural write failures (``snapshot_failed`` / ``write_failed``) →
    500. ``test_web_routes_context_pull_apply.py`` pins that partition against
    the engine enum, exactly as ``ContextPullPreviewCandidate`` pins
    ``content_status``. Every ``reason`` (top-level and per-candidate) is
    display-sanitized at the route; ``gate_blocked`` carries a fixed path-free
    message (never the secret location); no raw bytes and no absolute ``dst``
    ever reach the wire (``canonical_path`` is project-relative / ``~``-
    collapsed, or ``None``)."""

    status: Literal[
        "applied",
        "source_conflict",
        "nothing_importable",
        "selected_landing_error",
        "canonical_exists",
        "skills_overwrite_unsupported",
        "snapshot_requires_dir_layout",
        "target_conflict",
        "gate_blocked",
    ]
    kind: str
    name: str
    target_scope: str
    reason: str
    reason_code: str | None
    selected_runtime: str | None
    write_outcome: str | None
    duplicate_runtimes: list[str]
    canonical_path: str | None
    candidates: list[ContextPullPreviewCandidate]
    distinct_landing_count: int
    gate_status: Literal["ok", "blocked", "requires_unsafe_confirmation"] | None
    gate_hits: int | None
    force_bypassable: bool


class ContextPullApplyNeedsConfirmation(BaseModel):
    """Unconfirmed Pull write — the shared disclose-then-confirm envelope
    (``_confirm.needs_confirmation_envelope``) for a Pull that WOULD write.

    Returned (HTTP 200 — consent is application state, not a transport error)
    when ``prepare_pull`` yields a committable plan but the destination tier's
    opt-in is absent: ``project_shared`` needs ``confirm_project_shared`` (its
    canonical is git-tracked), and ``user`` goes through the #1263
    ``host_write_gate`` (``allow_host_writes``, disclosing the host paths).
    ``confirm`` names the exact body flag to re-POST with; ``host_targets`` is
    the host paths the confirmed write lands on (``[]`` for project_shared,
    which writes inside the project root, not a host path)."""

    status: Literal["needs_confirmation"]
    confirm: str
    reason: str
    host_targets: list[str]


# ── GET /context/status-global — user-tier portal (ADR-0030 §9 + §1, PR-F) ──


class ContextGlobalStoreCounts(BaseModel):
    """Per-kind canonical inventory of the user Store (the 'global library')."""

    skills: int
    agents: int
    commands: int


class ContextGlobalRuntimeCoverage(BaseModel):
    """Host runtime coverage for the user tier.

    A normalized :func:`memtomem.context.runtime_coverage.compute_runtime_coverage`
    entry: the raw helper OMITS ``installed`` / ``memtomem_registered`` when the
    registry probe found no client, but this module forbids optional keys, so the
    handler fills them with ``None`` (probe unavailable) instead."""

    name: str
    available: bool
    installed: bool | None
    memtomem_registered: bool | None


class ContextPullDriftRow(BaseModel):
    """One Store artifact's pull-direction drift verdict (ADR-0030 §1).

    ``verdict`` is a CLOSED ``Literal`` — the reduced badge view of the engine's
    ``content_status`` — pinned against
    :data:`memtomem.context.pull_preview.PullDriftVerdict` by
    ``test_context_status_global.py`` (same discipline as
    ``ContextPullPreviewCandidate``). ``reason`` is display-sanitized at the
    route (never raw engine text / absolute paths)."""

    kind: str
    name: str
    verdict: Literal["differs", "identical", "error"]
    runtimes: list[str]
    reason: str | None


class ContextPullDriftSummary(BaseModel):
    """Store-wide pull-direction drift summary (ADR-0030 §1 stage-1 probe).

    ``has_pull_drift`` fires the portal badge/glance-dot on a DEFINITE runtime
    divergence only (``differs > 0``); an ``error`` row is an indeterminate
    check-failure, surfaced separately, never asserted as drift."""

    has_pull_drift: bool
    total: int
    differs: int
    errors: int
    identical: int
    rows: list[ContextPullDriftRow]


class ContextStatusGlobalResponse(BaseModel):
    """User-tier global portal status — ADR-0030 §9 + §1 (PR-F).

    A SEPARATE endpoint from ``GET /context/status-all`` (which is
    ``project_shared``-only and per-project): the user tier is one global
    ``~/.memtomem`` Store with no per-project fan-out, so it gets its own frozen
    wire shape rather than a mode-discriminated overload of the fleet endpoint
    (ADR-0030 §9). Read-only — the drift summary is a detection probe; writes
    stay explicit Pull/Push (ADR-0030 §1)."""

    scope: Literal["user"]
    store: ContextGlobalStoreCounts
    runtime_coverage: list[ContextGlobalRuntimeCoverage]
    pull_drift: ContextPullDriftSummary
