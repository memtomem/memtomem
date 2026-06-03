"""Machine-readable skip reason codes for context fan-out / import results.

The :class:`~memtomem.context.skills.SkillSyncResult`,
:class:`~memtomem.context.commands.CommandSyncResult`,
:class:`~memtomem.context.agents.AgentSyncResult` and the shared
``ExtractResult`` types record skipped items as ``(name, reason, reason_code)``
tuples. ``reason`` is human-readable (used in CLI output and UI tooltips);
``reason_code`` is stable identifier that the web UI matches on so toast
copy can change without breaking client logic.
"""

from __future__ import annotations

from typing import Final, Literal

# Sync (canonical → runtime) skip codes.
NO_CANONICAL_ROOT: Final = "no_canonical_root"
UNKNOWN_RUNTIME: Final = "unknown_runtime"
PARSE_ERROR: Final = "parse_error"
# ADR-0011 PR-E: emitted when ``_runtime_targets.RUNTIME_FANOUT_TABLE`` returns
# ``None`` for the requested ``(artifact, runtime, scope)`` tuple — i.e. that
# combination has no fan-out target by design (e.g. ``project_local`` per
# ADR §3, or ``(commands, codex, project_*)`` since Codex CLI prompts live
# user-only). Loud emit, not silent — feedback_defensive_noise.md.
NO_PROJECT_FANOUT_FOR_RUNTIME: Final = "no_project_fanout_for_runtime"

# Import (runtime → canonical) skip codes.
INVALID_NAME: Final = "invalid_name"
ALREADY_IMPORTED: Final = "already_imported"
CANONICAL_EXISTS: Final = "canonical_exists"
TOML_PARSE_ERROR: Final = "toml_parse_error"
# ADR-0011 PR-E2: emitted when ``enforce_write_guard`` blocks an import to a
# user/project_local destination. ``PRIVACY_BLOCKED_PROJECT_SHARED`` is the
# distinct signal that a force-unsafe bypass was attempted into git-tracked
# memory and hard-refused (ADR §5). project_shared destinations RAISE
# ``ClickException`` rather than skipping, so these codes only appear in
# ``ExtractResult.skipped`` for user / project_local destinations.
PRIVACY_BLOCKED: Final = "privacy_blocked"
PRIVACY_BLOCKED_PROJECT_SHARED: Final = "privacy_blocked_project_shared"

# Versioning (ADR-0022) skip codes — emitted in Phase 1 of
# ``sync_atomic_artifact`` when a label-aware ``resolve_canonical_bytes``
# cannot resolve the requested label/version for one artifact. Per-item
# isolation (a single artifact's missing label does not abort the whole
# fan-out), consistent with the existing parse/read skip handling.
LABEL_NOT_FOUND: Final = "label_not_found"
VERSION_NOT_FOUND: Final = "version_not_found"
# A non-``latest`` label / bare version tag was requested for a flat-layout
# artifact, which has no per-artifact ``versions/`` store (ADR-0022 §3).
# ``latest`` / no-label sync on a flat artifact is unaffected.
VERSIONING_REQUIRES_DIR_LAYOUT: Final = "versioning_requires_dir_layout"

# Closed set of skip codes — typing dataclass `skipped` triples and route
# response builders against `SkipCode` catches typos at the construction site
# instead of letting an arbitrary string slip through to the wire.
SkipCode = Literal[
    "no_canonical_root",
    "unknown_runtime",
    "parse_error",
    "no_project_fanout_for_runtime",
    "invalid_name",
    "already_imported",
    "canonical_exists",
    "toml_parse_error",
    "privacy_blocked",
    "privacy_blocked_project_shared",
    "label_not_found",
    "version_not_found",
    "versioning_requires_dir_layout",
]
