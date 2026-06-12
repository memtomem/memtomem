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

# Another process held a destination sidecar lock past the engine's whole-call
# acquisition budget (``skills._SKILLS_LOCK_BUDGET_S``). Emitted instead of
# blocking indefinitely so an async web caller offloading to a thread can never
# orphan a worker that writes after its request already timed out (#1145 shape).
LOCK_TIMEOUT: Final = "lock_timeout"

# The fan-out destination already holds non-skill content — a directory with
# files but no SKILL.md manifest, or a plain file — that
# ``skills._promote_staging`` refuses to overwrite. Emitted as a typed
# per-destination skip instead of letting the IsADirectoryError /
# NotADirectoryError crash the sync mid-batch (#1229, which also broke the
# project_shared all-or-nothing promote). The human reason names the
# conflicting path so the user can remove it (or add a SKILL.md) and re-run.
TARGET_CONFLICT: Final = "target_conflict"

# Two canonical files (different stems) declare the same frontmatter ``name:``.
# ``out_path`` is a pure function of (target, name), so both would land on the
# SAME runtime file — last-writer-wins with both writes reported as generated
# (#1247). ``sync_atomic_artifact`` keeps the first-seen canonical (sorted
# order, deterministic) and emits this typed skip for every later claimant.
# The human reason names both source paths so the user can rename one.
# Loud emit, not silent — feedback_defensive_noise.md.
DUPLICATE_NAME: Final = "duplicate_name"

# The fan-out target already matches the merged canonical state byte-for-byte,
# so the engine skipped the write (#1247 id 43 — MCP servers, where N
# definitions converge on the single ``.mcp.json``). Typed rather than a bare
# empty result: the Sync All no-op detector treats "only no_canonical_root
# skips" as nothing-to-sync, and a fully-in-sync MCP-only project is NOT that.
IN_SYNC: Final = "in_sync"

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

# Transfer install-provenance skip codes (A-4 #1275) — emitted in
# ``TransferResult.provenance_reason_code`` when a project_shared →
# project_shared transfer does NOT carry the source's ``lock.json`` entry
# to the destination. Same (human reason, stable code) pairing contract as
# the sync/import codes above: CLI prints ``provenance_reason``, the web
# route (A-5) / MCP action (A-13) match on the code.
#
# Source-side classification (pre-stage, while the source tree still
# exists):
PROVENANCE_SOURCE_LOCKFILE_UNREADABLE: Final = "source_lockfile_unreadable"
PROVENANCE_RENAMED_COPY: Final = "renamed_copy"
PROVENANCE_SOURCE_INVALID_PIN: Final = "source_invalid_pin"
PROVENANCE_SOURCE_NO_DIGESTS: Final = "source_no_digests"
PROVENANCE_SOURCE_DIRTY: Final = "source_dirty"
PROVENANCE_SOURCE_UNPROVABLE: Final = "source_unprovable"
# Destination-side verification (post-promote, best-effort):
PROVENANCE_DEST_BYTES_UNVERIFIED: Final = "dest_bytes_unverified"
PROVENANCE_DEST_LOCKFILE_ERROR: Final = "dest_lockfile_error"

# Closed set for the provenance codes — separate from `SkipCode` because the
# consumer surface differs (transfer result field, not per-item `skipped`
# triples), and mixing the sets would let a fan-out code typo masquerade as
# a provenance outcome (and vice versa) at construction sites.
ProvenanceSkipCode = Literal[
    "source_lockfile_unreadable",
    "renamed_copy",
    "source_invalid_pin",
    "source_no_digests",
    "source_dirty",
    "source_unprovable",
    "dest_bytes_unverified",
    "dest_lockfile_error",
]

# Closed set of skip codes — typing dataclass `skipped` triples and route
# response builders against `SkipCode` catches typos at the construction site
# instead of letting an arbitrary string slip through to the wire.
SkipCode = Literal[
    "no_canonical_root",
    "unknown_runtime",
    "parse_error",
    "no_project_fanout_for_runtime",
    "lock_timeout",
    "target_conflict",
    "duplicate_name",
    "in_sync",
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
