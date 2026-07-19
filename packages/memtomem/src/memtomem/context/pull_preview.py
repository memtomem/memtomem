"""ADR-0030 PR-B — read-only Pull preview engine.

A Pull brings one runtime's copy of an artifact into the canonical Store
(reverse of Push/fan-out). This module answers, without writing anything,
"what would a Pull land, and would the privacy gate allow it?" — the
preview-first half of the explicit Preview/Pull model.

The report is **two orthogonal axes** per candidate (ADR-0030 §4), never
collapsed:

* ``content_status`` — the Store↔candidate content relation: ``new`` /
  ``differs`` / ``identical`` / ``landing_error`` (the would-land bytes
  could not be computed — unreadable source, TOML parse failure) /
  ``store_error`` (the would-land bytes WERE computed but the Store copy
  could not be read) / ``not_importable`` (a runtime that has the artifact
  on disk but is export-only for this kind — display-only).
* ``gate_status`` — the Gate A privacy outcome for the destination tier:
  ``ok`` / ``blocked`` (hard-refuse tier, e.g. ``project_shared``) /
  ``requires_unsafe_confirmation`` (force-bypassable tiers). ``None`` for
  ``not_importable`` and ``landing_error`` rows (nothing scannable).

Two surfaces, two questions (the load-bearing distinction — Codex design
gate):

* **Gate scan + §5 landing grouping use the EXACT copier surface**
  (:func:`~memtomem.context.skills._iter_scannable_skill_files` for skills)
  — everything a Pull actually copies, including a runtime's top-level
  ``overrides/`` / ``versions/``. A secret hiding there must be scanned
  because it would be copied; two candidates whose visible skill content
  matches but whose metadata differs land *different* trees and must not be
  auto-selected.
* **The Store content comparison uses the PAYLOAD surface**
  (:func:`iter_skill_payload_files`) — the actual skill content, excluding
  Store-owned ``overrides/`` / ``versions/`` / ``versions.json`` the runtime
  legitimately lacks. Counting them would report every versioned skill as
  ``differs`` forever. This is the seed of the ADR-0030 §10 unified payload
  iterator; PR-G converges the tree digest / snapshot / scan / fan-out onto
  it. PR-B intentionally introduces **no persisted SHA digest** — landing
  grouping is in-memory structural equality, so §10's digest byte-framing
  stays PR-G's decision.

Non-goals for PR-B (later PRs): writes / snapshot / lock (PR-B2), CLI
``mm context pull`` (PR-C), Web picker (PR-D), MCP parity (PR-H). The §5
ambiguity signal is *computed* here; the refusal it drives is enforced at
``--apply`` / Web-dialog time, not here.
"""

from __future__ import annotations

import logging
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from memtomem.config import TargetScope
from memtomem.context import override as _override
from memtomem.context._gate_a import GateStatus, classify_gate_status
from memtomem.context._names import is_internal_artifact_dir
from memtomem.context._runtime_targets import (
    IMPORT_SOURCE_RUNTIMES,
    KNOWN_RUNTIMES,
    runtime_fanout_root,
)
from memtomem.context.scope_resolver import ArtifactKind, canonical_artifact_dir
from memtomem.context.versioning import _MANIFEST_FILENAME, _VERSIONS_DIRNAME

logger = logging.getLogger(__name__)

# The gate ``surface`` tag for preview scans. Inert for counters/audit
# (``classify_gate_status`` passes ``record_outcome=False``), but a distinct
# literal keeps the preview path identifiable if that ever changes.
_PREVIEW_SURFACE = "context_pull_preview"

# Store-owned internal metadata excluded from the *payload* surface (ADR-0030
# §10). The runtime side never carries these; counting them in the Store
# comparison would make every versioned / override-carrying skill read as
# ``differs`` and would contaminate PR-G's tree digest. NOT excluded from the
# copier/gate surface (a secret under them would still be copied by a Pull).
# Names are pulled from the version store's own constants so the exclusion set
# cannot drift from the writer (Codex code review — locks/temps were missing).
_OVERRIDES_DIRNAME = "overrides"
_PAYLOAD_EXCLUDED_TOP_DIRS = frozenset({_OVERRIDES_DIRNAME, _VERSIONS_DIRNAME})
# ``atomic_write_bytes`` leaves ``.{name}.*.tmp`` siblings; ``_lock_path_for``
# writes ``.{name}.lock`` — both next to ``versions.json`` at the skill root.
_MANIFEST_LOCK_NAME = f".{_MANIFEST_FILENAME}.lock"
_MANIFEST_TMP_PREFIX = f".{_MANIFEST_FILENAME}."


def _is_store_internal_top_file(name: str) -> bool:
    """Top-level version-store metadata files (manifest + its lock/temp sidecars)."""
    if name == _MANIFEST_FILENAME or name == _MANIFEST_LOCK_NAME:
        return True
    return name.startswith(_MANIFEST_TMP_PREFIX) and name.endswith(".tmp")


ContentStatus = Literal[
    "new",
    "differs",
    "identical",
    "landing_error",
    "store_error",
    "not_importable",
]


@dataclass(frozen=True)
class PullCandidate:
    """One runtime's Pull-preview row (ADR-0030 §4). Read-only report."""

    runtime: str
    content_status: ContentStatus
    # None for not_importable (nothing to scan) and landing_error (unscannable).
    gate_status: GateStatus | None
    importable: bool
    # Candidates sharing a landing_group id would land byte-identical content
    # over the full copier surface. None for not_importable / landing_error.
    landing_group: int | None
    # The candidate's would-land copy equals a vendor override — pulling it
    # would bake the override into the base canonical (ADR-0030 §7). Warn only.
    override_warning: bool
    # Raw, UNSANITIZED engine text for *_error rows (may embed absolute paths).
    # Web/MCP boundaries sanitize; the CLI prints verbatim (DiffRow contract).
    reason: str | None
    # The would-land FULL copier surface as sorted ``(posix_relpath, bytes)``,
    # populated ONLY when ``preview_pull(..., include_content=True)`` (the CLI
    # ``--diff`` path); ``None`` otherwise. The full surface — not the payload
    # subset — so a ``--diff`` can show metadata-only divergence
    # (``overrides/`` / ``versions/``) that drives ``source_conflict`` yet
    # leaves the payload identical (ADR-0030 §4/§5). CLI-only: the web/MCP wire
    # boundaries never serialize it (raw bytes, unredacted).
    content: tuple[tuple[str, bytes], ...] | None = None


@dataclass(frozen=True)
class PullPreview:
    """The full Pull preview for one (kind, name, scope)."""

    kind: ArtifactKind
    name: str
    scope: TargetScope
    store_present: bool
    candidates: list[PullCandidate]
    # Number of distinct landing_group ids among importable candidates whose
    # landing content was computable (ADR-0030 §5).
    distinct_landing_count: int
    # >1 distinct landing group OR any importable landing_error (fail-closed:
    # an unreadable copy might be the divergent one). The refusal this drives
    # is enforced by PR-C/PR-D, not here.
    ambiguous: bool
    # Runtime auto-selected when unambiguous (priority-first of the single
    # group); None when ambiguous or nothing importable is landable.
    auto_source: str | None
    # The current Store PAYLOAD as sorted ``(posix_relpath, bytes)``, populated
    # ONLY when ``include_content=True`` (the ``--diff`` store side); ``None``
    # otherwise (and when the Store is absent or unreadable). The SAME read
    # ``content_status`` used, so a ``--diff`` can never disagree with the
    # rendered status. CLI-only, never serialized on the wire.
    store_content: tuple[tuple[str, bytes], ...] | None = None


def iter_skill_payload_files(root: Path) -> list[tuple[str, bytes]]:
    """The skill *payload* as sorted ``(posix_relpath, bytes)`` (ADR-0030 §10 seed).

    Built by filtering the copier surface
    (:func:`~memtomem.context.skills._iter_scannable_skill_files`, which already
    drops ``COPY_SKIP_NAMES`` and symlinks and fails CLOSED on ``OSError``) down
    to the artifact payload: excludes top-level ``overrides/`` and ``versions/``
    directories and a top-level ``versions.json`` — the Store-owned internal
    metadata that must never count as skill *content*. Nested files of the same
    name are kept (only the top level is Store-owned).

    Propagates ``OSError`` (unreadable subtree or file) so callers fail closed.
    PR-G generalizes this into the single tree-digest/snapshot/scan/fan-out
    iterator; keep the exclusion set identical when that lands.
    """
    return [(rel, data) for rel, data in _read_skill_tree(root) if _is_payload_relpath(rel)]


def _is_payload_relpath(rel: str) -> bool:
    parts = rel.split("/")
    top = parts[0]
    # Store-owned top-level dirs, plus our own crash-leftover staging/move-aside
    # trees (``is_internal_artifact_dir`` — the same predicate the extract/reap
    # paths use so "hidden" and "excluded" can't drift).
    if top in _PAYLOAD_EXCLUDED_TOP_DIRS or is_internal_artifact_dir(top):
        return False
    if len(parts) == 1 and _is_store_internal_top_file(top):
        return False
    return True


def _read_skill_tree(root: Path) -> list[tuple[str, bytes]]:
    """Full copier surface of a skill dir as sorted ``(posix_relpath, bytes)``.

    Uses the copier-surface iterator so gate scanning and §5 grouping see the
    exact bytes a Pull would land. Raises ``OSError`` (fail closed).
    """
    # Imported lazily to avoid import-order coupling with the large skills
    # module (which imports the gate/override leaves this module also uses).
    from memtomem.context.skills import _iter_scannable_skill_files

    files: list[tuple[str, bytes]] = []
    for path in _iter_scannable_skill_files(root):
        rel = path.relative_to(root).as_posix()
        files.append((rel, path.read_bytes()))
    files.sort()
    return files


# ── internal working row (mutable; grouping needs a second pass) ──────────


@dataclass
class _Cand:
    runtime: str
    importable: bool
    content_status: ContentStatus
    gate_status: GateStatus | None
    override_warning: bool
    reason: str | None
    # Full copier surface of the would-land content (for §5 grouping); None
    # on landing_error / not_importable. Agents/commands are a single
    # ``[("", bytes)]`` entry.
    landing_full: list[tuple[str, bytes]] | None
    # Payload surface of the would-land content (for the Store comparison).
    landing_payload: list[tuple[str, bytes]] | None
    landing_group: int | None = None


def _worst_gate(statuses: list[GateStatus]) -> GateStatus:
    """Most-restrictive gate status wins (blocked > requires_unsafe > ok)."""
    order = {"ok": 0, "requires_unsafe_confirmation": 1, "blocked": 2}
    return max(statuses, key=lambda s: order[s]) if statuses else "ok"


def _runtime_candidate_path(
    kind: ArtifactKind,
    runtime: str,
    name: str,
    scope: TargetScope,
    project_root: Path | None,
) -> Path | None:
    """Exact on-disk path a Pull would read for this candidate, or None.

    Returns ``None`` when the runtime has no fan-out for this (kind, scope)
    — i.e. nothing to pull from. Uses the shared per-(kind, runtime) suffix
    table as the single source of truth (never a hardcoded ``.md``) so
    export-only codex ``.toml`` / kimi ``.yaml`` files are found and marked
    ``not_importable`` rather than silently probed as a missing ``.md``.
    """
    root = runtime_fanout_root(kind, runtime, scope, project_root)
    if root is None:
        return None
    if kind == "skills":
        from memtomem.context.skills import SKILL_MANIFEST

        return root / name / SKILL_MANIFEST
    from memtomem.context.migrate import _NON_SKILL_FANOUT_SUFFIX

    suffix = _NON_SKILL_FANOUT_SUFFIX.get(kind, {}).get(runtime)
    if suffix is None:
        return None
    return root / f"{name}{suffix}"


def _probe_present(path: Path) -> tuple[bool, OSError | None]:
    """Fail-closed presence probe (ENOENT vs other OSError, Codex Major 3).

    ``Path.is_file()`` swallows permission errors into ``False`` — a
    permission-hidden divergent copy would then vanish from the preview and
    defeat §5 fail-closed. ``os.stat`` distinguishes: ENOENT → truly absent
    (no row); any other ``OSError`` → present-but-uncomputable (an importable
    candidate becomes a ``landing_error`` row).
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return False, None
    except OSError as exc:
        return False, exc
    return stat.S_ISREG(st.st_mode), None


def _read_landing(
    kind: ArtifactKind,
    runtime: str,
    name: str,
    scope: TargetScope,
    project_root: Path | None,
    path: Path,
) -> tuple[list[tuple[str, bytes]], list[tuple[str, bytes]], bytes]:
    """Compute the would-land content for one importable candidate.

    Returns ``(full_surface, payload_surface, override_compare_bytes)``. For
    skills the two surfaces differ (copier vs payload); for agents/commands
    both are a single ``[("", bytes)]``. ``override_compare_bytes`` is the RAW
    bytes §7 compares against the vendor override (raw-vs-raw, no conversion:
    the runtime file's bytes; for skills the top-level ``SKILL.md`` bytes).

    Raises ``OSError`` / ``ValueError`` (TOML parse, decode) on a landing
    failure — the caller maps that to ``landing_error``.
    """
    if kind == "skills":
        skill_dir = path.parent
        full = _read_skill_tree(skill_dir)
        manifest_name = _skill_manifest_name()
        manifest = next((data for rel, data in full if rel == manifest_name), None)
        if manifest is None:
            # ``SKILL.md`` vanished between the presence probe and the tree read
            # (or the dir holds only excluded internals): a captured tree with
            # no manifest is not a valid skill, and staging it would promote an
            # invalid canonical. Fail closed → the caller maps it to
            # ``landing_error`` (parity with ``skills._stage_skill``, which
            # refuses a manifest-less source).
            raise FileNotFoundError(f"source skill missing {manifest_name}: {skill_dir}")
        payload = [(rel, data) for rel, data in full if _is_payload_relpath(rel)]
        return full, payload, manifest
    if kind == "commands" and runtime == "gemini":
        from memtomem.context.commands import _gemini_toml_to_canonical

        # Landing = converted canonical MD (ADR-0030 §5). Override compare is
        # raw-vs-raw against the raw TOML (both are TOML for gemini commands).
        raw = path.read_bytes()
        landed = _gemini_toml_to_canonical(path).encode("utf-8")
        return [("", landed)], [("", landed)], raw
    # agents (claude/gemini) and claude commands: byte passthrough.
    raw = path.read_bytes()
    return [("", raw)], [("", raw)], raw


def _skill_manifest_name() -> str:
    from memtomem.context.skills import SKILL_MANIFEST

    return SKILL_MANIFEST


def _gate_landing(landing_full: list[tuple[str, bytes]], scope: TargetScope) -> GateStatus:
    """Worst gate status over every file the copier surface would land."""
    statuses = [
        classify_gate_status(
            data.decode("utf-8", errors="replace"), scope=scope, surface=_PREVIEW_SURFACE
        )
        for _rel, data in landing_full
    ]
    return _worst_gate(statuses)


def _override_warning(
    kind: ArtifactKind,
    name: str,
    vendor: str,
    scope: TargetScope,
    project_root: Path | None,
    compare_bytes: bytes,
) -> bool:
    """True when the candidate's raw copy byte-equals its vendor override (§7)."""
    try:
        ov = _override.resolve(project_root, kind, name, vendor, scope=scope)
        if ov is None:
            return False
        return ov.read_bytes() == compare_bytes
    except OSError:
        return False


def _read_store(
    kind: ArtifactKind,
    name: str,
    scope: TargetScope,
    project_root: Path | None,
) -> tuple[bool, list[tuple[str, bytes]] | None, OSError | None]:
    """Read the current Store copy's *payload*.

    Returns ``(present, payload, error)``. ``present=False`` → the Store has
    no ``<name>`` (candidates land as ``new``). ``error`` set (with
    ``present=True``) → the Store copy exists but could not be read
    (candidates land as ``store_error``). Uses the existing flat/dir resolver
    for agents/commands (valid dir wins, else valid flat — never a naive
    ``<name>.md`` shortcut that could pick a malformed dir over a valid flat).
    """
    if kind == "skills":
        store_dir = canonical_artifact_dir("skills", scope, project_root) / name
        try:
            st = os.stat(store_dir)
        except FileNotFoundError:
            return False, None, None
        except OSError as exc:
            return True, None, exc
        if not stat.S_ISDIR(st.st_mode):
            return False, None, None
        try:
            return True, iter_skill_payload_files(store_dir), None
        except OSError as exc:
            return True, None, exc
    # agents / commands. Fail-closed presence probe of BOTH candidate layouts
    # via ``os.stat`` FIRST, so a permission-hidden Store copy is a
    # deterministic ``store_error`` on every platform (Codex code review
    # Blocker; the CI-observed drift): the ADR-0008 resolver uses
    # ``Path.is_file()``, which SWALLOWS ``EACCES`` into ``False`` on macOS but
    # RAISES it on Linux — relying on it made the unreadable-dir case resolve to
    # ``new`` on macOS and ``store_error`` on Linux. ``os.stat`` raises on both.
    canonical_root = canonical_artifact_dir(kind, scope, project_root)
    dir_filename = _atomic_dir_filename(kind)
    for candidate in (canonical_root / name / dir_filename, canonical_root / f"{name}.md"):
        _present, err = _probe_present(candidate)
        if err is not None:
            return True, None, err
    # Both candidates cleanly probed (present or ENOENT) → use the shared
    # resolver for the layout decision (valid dir wins over valid flat).
    resolved = _resolve_canonical_atomic(kind, name, scope, project_root)
    if resolved is None:
        return False, None, None
    path, _layout = resolved
    try:
        return True, [("", path.read_bytes())], None
    except OSError as exc:
        return True, None, exc


def _atomic_dir_filename(kind: ArtifactKind) -> str:
    """The ADR-0008 dir-layout manifest filename for agents / commands."""
    if kind == "agents":
        from memtomem.context.agents import AGENT_DIR_FILENAME

        return AGENT_DIR_FILENAME
    from memtomem.context.commands import COMMAND_DIR_FILENAME

    return COMMAND_DIR_FILENAME


def _resolve_canonical_atomic(
    kind: ArtifactKind,
    name: str,
    scope: TargetScope,
    project_root: Path | None,
) -> tuple[Path, str] | None:
    from memtomem.context._atomic_reverse import resolve_artifact_under_root

    canonical_root = canonical_artifact_dir(kind, scope, project_root)
    return resolve_artifact_under_root(
        canonical_root,
        name,
        artifact_label=kind,
        dir_filename=_atomic_dir_filename(kind),
        logger=logger,
    )


@dataclass
class _Collected:
    """The one collection pass shared by preview and apply (ADR-0030 PR-C).

    ``preview_pull`` and ``pull_apply.prepare_pull`` both build their §5 signal
    from this SAME object so the enforced refusal can never drift from the
    displayed preview. The Store bytes are captured here (not re-read) so
    ``content_status``, ``store_content`` (``--diff``), and the plan
    precondition all reference one read.
    """

    store_present: bool
    store_payload: list[tuple[str, bytes]] | None
    store_err: OSError | None
    working: list[_Cand]


def _collect(
    kind: ArtifactKind,
    name: str,
    *,
    scope: TargetScope,
    project_root: Path | None,
    scan_gate: bool = True,
) -> _Collected:
    """Read the Store + every runtime candidate once, capturing landing bytes.

    Pure/read-only (no writes, no privacy-counter mutation, no audit). Each
    importable, present, computable candidate carries its captured
    ``landing_full`` / ``landing_payload`` — the bytes a Pull WOULD land — so a
    downstream commit can write exactly what was judged. ``kind`` must be a key
    of :data:`IMPORT_SOURCE_RUNTIMES` (a bad kind is a ``KeyError``).

    ``scan_gate=False`` skips the per-candidate ``classify_gate_status`` scan
    (leaving ``gate_status=None``): the apply path (``pull_apply.prepare_pull``)
    runs its OWN single audited Gate A decision over just the selected
    candidate, so scanning every candidate here too would double-scan the
    selected payload and waste a full-tree scan on the rest. The preview keeps
    the default so its table can show a per-candidate gate column.
    """
    eligible = set(IMPORT_SOURCE_RUNTIMES[kind])
    store_present, store_payload, store_err = _read_store(kind, name, scope, project_root)

    working: list[_Cand] = []
    for runtime in KNOWN_RUNTIMES:
        path = _runtime_candidate_path(kind, runtime, name, scope, project_root)
        if path is None:
            continue  # no fan-out for this (kind, runtime, scope) — nothing to pull
        present, probe_err = _probe_present(path)
        importable = runtime in eligible

        if not importable:
            # Display-only export-only row — only when definitively present.
            if present:
                working.append(
                    _Cand(
                        runtime=runtime,
                        importable=False,
                        content_status="not_importable",
                        gate_status=None,
                        override_warning=False,
                        reason=None,
                        landing_full=None,
                        landing_payload=None,
                    )
                )
            continue

        if not present and probe_err is None:
            continue  # ENOENT — truly absent, no row

        if probe_err is not None:
            working.append(
                _Cand(
                    runtime=runtime,
                    importable=True,
                    content_status="landing_error",
                    gate_status=None,
                    override_warning=False,
                    reason=str(probe_err),
                    landing_full=None,
                    landing_payload=None,
                )
            )
            continue

        # Present + importable — compute the would-land content.
        try:
            landing_full, landing_payload, override_raw = _read_landing(
                kind, runtime, name, scope, project_root, path
            )
        except (OSError, ValueError) as exc:
            working.append(
                _Cand(
                    runtime=runtime,
                    importable=True,
                    content_status="landing_error",
                    gate_status=None,
                    override_warning=False,
                    reason=str(exc),
                    landing_full=None,
                    landing_payload=None,
                )
            )
            continue

        gate = _gate_landing(landing_full, scope) if scan_gate else None
        override = _override_warning(kind, name, runtime, scope, project_root, override_raw)
        content = _content_status(store_present, store_payload, store_err, landing_payload)
        # A store_error row carries the (unsanitized) Store read error as its
        # diagnostic — the wire boundary redacts it (Codex code review Minor).
        reason = str(store_err) if content == "store_error" and store_err is not None else None
        working.append(
            _Cand(
                runtime=runtime,
                importable=True,
                content_status=content,
                gate_status=gate,
                override_warning=override,
                reason=reason,
                landing_full=landing_full,
                landing_payload=landing_payload,
            )
        )

    return _Collected(
        store_present=store_present,
        store_payload=store_payload,
        store_err=store_err,
        working=working,
    )


def preview_pull(
    kind: ArtifactKind,
    name: str,
    *,
    scope: TargetScope,
    project_root: Path | None,
    include_content: bool = False,
) -> PullPreview:
    """Build the read-only Pull preview for ``(kind, name, scope)``.

    Pure: no disk writes, no privacy-counter mutation, no audit lines. Reads
    the real ``~``/project canonical + override + runtime trees (no injected
    base — that would split-brain the Store comparison against override
    resolution; tests isolate via ``HOME``).

    ``kind`` must be a key of :data:`IMPORT_SOURCE_RUNTIMES` (the caller — the
    web route — validates and 400s otherwise; a bad kind here is a KeyError).

    ``include_content=True`` additionally captures the FULL copier surface onto
    each candidate's ``content`` and the Store payload onto ``store_content``
    for the CLI ``--diff`` — CLI-only; the web/MCP wire boundaries never
    serialize those raw-byte fields (they default ``None``).
    """
    collected = _collect(kind, name, scope=scope, project_root=project_root)
    distinct, ambiguous, auto_source = _group_and_resolve(collected.working)
    candidates = [
        PullCandidate(
            runtime=c.runtime,
            content_status=c.content_status,
            gate_status=c.gate_status,
            importable=c.importable,
            landing_group=c.landing_group,
            override_warning=c.override_warning,
            reason=c.reason,
            content=(
                tuple(c.landing_full) if include_content and c.landing_full is not None else None
            ),
        )
        for c in collected.working
    ]
    return PullPreview(
        kind=kind,
        name=name,
        scope=scope,
        store_present=collected.store_present,
        candidates=candidates,
        distinct_landing_count=distinct,
        ambiguous=ambiguous,
        auto_source=auto_source,
        store_content=(
            tuple(collected.store_payload)
            if include_content and collected.store_payload is not None
            else None
        ),
    )


def _content_status(
    store_present: bool,
    store_payload: list[tuple[str, bytes]] | None,
    store_err: OSError | None,
    landing_payload: list[tuple[str, bytes]],
) -> ContentStatus:
    if not store_present:
        return "new"
    if store_err is not None or store_payload is None:
        return "store_error"
    return "identical" if landing_payload == store_payload else "differs"


def _group_and_resolve(working: list[_Cand]) -> tuple[int, bool, str | None]:
    """Assign landing_group ids over the FULL copier surface; compute §5 signal.

    Groups importable candidates whose landing was computable (``landing_full``
    is set — includes ``store_error`` rows, excludes ``landing_error`` and
    ``not_importable``). Grouping is in-memory structural equality (no digest).
    ``ambiguous`` is >1 distinct group OR any importable ``landing_error``
    (fail-closed). ``auto_source`` is the priority-first runtime of the single
    group when unambiguous.
    """
    groups: list[list[tuple[str, bytes]]] = []
    has_landing_error = False
    for cand in working:
        if cand.content_status == "landing_error" and cand.importable:
            has_landing_error = True
            continue
        if cand.landing_full is None:
            continue  # not_importable
        for gid, rep in enumerate(groups):
            if rep == cand.landing_full:
                cand.landing_group = gid
                break
        else:
            cand.landing_group = len(groups)
            groups.append(cand.landing_full)

    distinct = len(groups)
    ambiguous = distinct > 1 or has_landing_error
    auto_source: str | None = None
    if not ambiguous and distinct == 1:
        auto_source = next(
            (c.runtime for c in working if c.landing_group == 0),
            None,
        )
    return distinct, ambiguous, auto_source


# ── ADR-0030 §1 stage-1 pull-direction drift probe ──────────────────────
#
# Detection is automatic; writes never are (ADR-0030 §1). This probe answers
# ONE question over a whole canonical Store — "does any pull-eligible runtime
# hold a copy that differs from the Store?" — to feed the user-tier portal's
# "runtime copy differs from Store — Preview/Pull" badge (PR-F). It is the
# reduced, cheap sibling of :func:`preview_pull`: it reuses the SAME collection
# pass (:func:`_collect`) with ``scan_gate=False`` so a Store-wide sweep never
# runs the per-file Gate A privacy scan — the badge needs only
# ``content_status``, which ``_collect`` computes unconditionally. The full
# two-axis preview (with the gate column) is computed lazily, per artifact, only
# when the user actually opens a Pull.

# The kinds a Pull can target (keys of IMPORT_SOURCE_RUNTIMES). Pinned so the
# probe and the eligibility table cannot drift.
_PULL_DRIFT_KINDS: tuple[ArtifactKind, ...] = ("skills", "agents", "commands")

# A REDUCED view of ContentStatus for the portal badge: ``differs`` (a runtime
# copy diverges — the definite drift the badge fires on), ``error`` (the Store
# or a runtime copy was unreadable, so drift is indeterminate), ``identical``
# (nothing pull-eligible diverges — in sync or no runtime copy to pull).
PullDriftVerdict = Literal["differs", "identical", "error"]


@dataclass(frozen=True)
class PullDriftRow:
    """One Store artifact's pull-direction drift verdict (ADR-0030 §1)."""

    kind: ArtifactKind
    name: str
    verdict: PullDriftVerdict
    # Runtimes whose copy differs from the Store (``verdict == "differs"``);
    # empty otherwise.
    runtimes: tuple[str, ...]
    # Raw, UNSANITIZED diagnostic for ``verdict == "error"`` (may embed absolute
    # paths). The web/MCP wire boundary redacts it (``_redact_pull_reason``);
    # None otherwise.
    reason: str | None


@dataclass(frozen=True)
class PullDriftSummary:
    """Store-wide pull-direction drift summary for the user-tier portal (PR-F)."""

    scope: TargetScope
    rows: tuple[PullDriftRow, ...]
    differs: int
    errors: int
    identical: int
    total: int

    @property
    def has_pull_drift(self) -> bool:
        """Whether the badge/glance-dot fires — a definite runtime divergence.

        Only ``differs`` counts: an ``error`` row is an *unknown*, surfaced as a
        separate check-failed hint, not asserted as drift (ADR-0009 direction
        framing — never claim a state we could not compute)."""
        return self.differs > 0


def _store_artifact_names(
    kind: ArtifactKind, scope: TargetScope, project_root: Path | None
) -> list[str]:
    """Canonical names present in the Store for ``kind`` (name-dispatch by layout).

    Mirrors the overview handler's derivation so the probe can never disagree
    with what a Pull would target. ``user`` scope ignores ``project_root``
    (``canonical_artifact_dir`` resolves ``~/.memtomem/<kind>``); the listers
    require a ``Path``, so a harmless home sentinel is passed when None.
    """
    root = project_root if project_root is not None else Path.home()
    if kind == "skills":
        from memtomem.context.skills import list_canonical_skills

        return [p.name for p in list_canonical_skills(root, scope=scope)]
    if kind == "agents":
        from memtomem.context.agents import canonical_agent_name, list_canonical_agents

        return [
            canonical_agent_name(p, layout)
            for p, layout in list_canonical_agents(root, scope=scope)
        ]
    if kind == "commands":
        from memtomem.context.commands import canonical_command_name, list_canonical_commands

        return [
            canonical_command_name(p, layout)
            for p, layout in list_canonical_commands(root, scope=scope)
        ]
    raise ValueError(f"kind {kind!r} is not a Pull target")  # pragma: no cover


def _drift_row(
    kind: ArtifactKind, name: str, *, scope: TargetScope, project_root: Path | None
) -> PullDriftRow:
    """Reduce one artifact's ``_collect`` pass to a badge verdict (read-only).

    Priority: a definite ``differs`` wins over an ``error`` (an unreadable
    copy must not mask a divergence we CAN see); ``error`` wins over
    ``identical`` (a store/landing read failure is indeterminate, not "in
    sync"). A per-artifact collection failure is caught and reported as an
    ``error`` row so one unreadable artifact can't blank the whole portal.
    """
    try:
        collected = _collect(kind, name, scope=scope, project_root=project_root, scan_gate=False)
    except OSError as exc:  # store/runtime walk failed outright — surface, don't crash
        return PullDriftRow(kind=kind, name=name, verdict="error", runtimes=(), reason=str(exc))

    differing = [
        c.runtime for c in collected.working if c.importable and c.content_status == "differs"
    ]
    if differing:
        return PullDriftRow(
            kind=kind, name=name, verdict="differs", runtimes=tuple(differing), reason=None
        )

    errored = [
        c
        for c in collected.working
        if c.importable and c.content_status in ("store_error", "landing_error")
    ]
    if errored:
        # A store_error row may carry a None reason; take the first non-None one.
        reason = next((c.reason for c in errored if c.reason is not None), None)
        return PullDriftRow(kind=kind, name=name, verdict="error", runtimes=(), reason=reason)

    # An unreadable Store with NO runtime copy present yields no candidate row at
    # all, so ``store_err`` rides on ``_Collected`` alone — it must still be an
    # ``error`` (indeterminate), never fall through to ``identical`` (Codex F1).
    if collected.store_err is not None:
        return PullDriftRow(
            kind=kind, name=name, verdict="error", runtimes=(), reason=str(collected.store_err)
        )

    # No divergence, no error → in sync. NOTE a ``new`` candidate (Store absent
    # yet a runtime holds a landable copy) also lands here — but the probe only
    # visits names ``_store_artifact_names`` already resolved in the Store, and
    # the lister + ``_read_store`` resolve the SAME canonical path, so a listed
    # name is ``store_present`` and ``new`` cannot arise. This fall-through is the
    # deliberate resting state for that coupling; if the lister/reader ever
    # diverge, a stray ``new`` reads as "not drift" (safe), not a crash.
    return PullDriftRow(kind=kind, name=name, verdict="identical", runtimes=(), reason=None)


def probe_pull_drift(
    *, scope: TargetScope = "user", project_root: Path | None = None
) -> PullDriftSummary:
    """Read-only pull-direction drift summary over a canonical Store (ADR-0030 §1).

    For every artifact already in the Store (skills/agents/commands), report
    whether any pull-eligible runtime holds a DIFFERENT copy. Pure: no writes,
    no privacy-counter mutation, no audit lines — it reuses :func:`_collect`
    with ``scan_gate=False`` so a whole-Store sweep costs one Store read plus up
    to ``len(KNOWN_RUNTIMES)`` runtime-tree reads per artifact and runs NO
    Gate A privacy scans. Never raises for a single bad artifact (those become
    ``error`` rows).

    ``scope`` defaults to ``user`` (the ``~/.memtomem`` global Store — the only
    consumer today, the PR-F portal); ``project_root`` is required for a project
    tier and ignored for ``user``.
    """
    rows: list[PullDriftRow] = []
    for kind in _PULL_DRIFT_KINDS:
        for name in _store_artifact_names(kind, scope, project_root):
            rows.append(_drift_row(kind, name, scope=scope, project_root=project_root))

    differs = sum(1 for r in rows if r.verdict == "differs")
    errors = sum(1 for r in rows if r.verdict == "error")
    identical = sum(1 for r in rows if r.verdict == "identical")
    return PullDriftSummary(
        scope=scope,
        rows=tuple(rows),
        differs=differs,
        errors=errors,
        identical=identical,
        total=len(rows),
    )
