"""Cross-tier / cross-project canonical artifact transfer engine (ADR-0023).

ADR-0011 PR-E4 gave ``mm context migrate`` a single-project scope move
(:func:`memtomem.context.migrate.migrate_scope`). The Context Gateway
completion campaign (#1270 item A-2) needs the general form: move OR
copy one canonical artifact between tiers **and between projects**.
This module is that engine; :func:`transfer_artifact` is the only
entry point.

The staged-move / pair-lock / fan-out primitives stay in
:mod:`memtomem.context.migrate` and are reused here verbatim
(``_acquire_pair_lock``, ``_stage_move``, ``_promote_move``,
``_existing_fanout_targets``, ``_remove_runtime_fanout_for``).
``migrate_scope`` is now a thin same-root wrapper over this engine
with byte-compatible results and error messages; the lazy import in
its body (not here) breaks the module cycle.

Pure module: filesystem + lockfile only, no wiki dependency (ADR-0008
Invariants 1 / 3). Errors raise :class:`click.ClickException` (or
:class:`memtomem.context.migrate.MigratePartialError` for the EXDEV
partial-move case) so each surface — CLI verbs (A-3 #1274), web route
(A-5 #1276), MCP action (A-13 #1283) — re-raises or translates in its
native vocabulary. Gate B confirmation (``--confirm-project-shared``,
web disclose-then-confirm) stays at the surface layer; this engine
only runs Gate A.

Two-root fan-out contract (ADR-0023 §4; Codex design-gate finding):
a cross-project move must discover stale runtime fan-out at the
**source** project root while verifying expected-render / override
bytes against the **destination** root — per-vendor overrides live
inside the artifact dir (``<name>/overrides/<vendor>.<ext>``) and
travel with it. ``_remove_runtime_fanout_for`` takes the two roots
separately for exactly this reason.

Install-provenance carry-over (ADR-0023 §9, A-4 #1275): a
``project_shared → project_shared`` transfer of a clean wiki install
carries the source's ``lock.json`` entry to the destination so ``mm
context status`` / ``update`` keep working there. The carry is gated
twice — source classified clean under
:func:`memtomem.context.dirty.is_asset_dirty` pre-stage, AND the
promoted bytes rehashing to the source entry's exact digest map
post-promote — because blessing locally-edited bytes as pristine wiki
state would let a later ``mm context update`` clobber them without its
``--force`` gate (the A-2 design-gate finding that deferred this
feature). Dirty or unprovable sources still transfer; they just land
untracked, with the reason on the result
(``provenance_reason`` / ``provenance_reason_code``).
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import secrets
import shlex
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import click

from memtomem.config import TargetScope
from memtomem.context._atomic import (
    atomic_write_bytes,
    installed_at_from_dest,
    iter_installed_files,
)
from memtomem.context._names import validate_name
from memtomem.context._skip_reasons import (
    PROVENANCE_DEST_BYTES_UNVERIFIED,
    PROVENANCE_DEST_LOCKFILE_ERROR,
    PROVENANCE_RENAMED_COPY,
    PROVENANCE_SOURCE_DIRTY,
    PROVENANCE_SOURCE_INVALID_PIN,
    PROVENANCE_SOURCE_LOCKFILE_UNREADABLE,
    PROVENANCE_SOURCE_NO_DIGESTS,
    PROVENANCE_SOURCE_UNPROVABLE,
    ProvenanceSkipCode,
)
from memtomem.context.agents import _KEY_VALUE_RE
from memtomem.context.dirty import is_asset_dirty
from memtomem.context.lockfile import (
    _HEX_DIGITS,
    Lockfile,
    LockfileError,
    digests_from_entry,
)
from memtomem.context._canonical_txn import acquire_canonical_locks
from memtomem.context.migrate import (
    _DIR_MANIFEST,
    SCOPE_MIGRATABLE_KINDS,
    MigratePartialError,
    _detect_source_scope,
    _existing_fanout_targets,
    _promote_move,
    _remove_runtime_fanout_for,
    _stage_move,
)
from memtomem.context.privacy_scan import raise_or_collect, scan_artifact_tree
from memtomem.context.scope_resolver import ArtifactKind, canonical_artifact_dir

logger = logging.getLogger(__name__)

__all__ = [
    "ProvenanceCarry",
    "TransferCollisionError",
    "TransferMode",
    "TransferResult",
    "transfer_artifact",
]


class TransferCollisionError(click.ClickException):
    """Destination path already holds an artifact (ADR-0023 §6).

    Typed subclass so non-CLI surfaces can map the collision to their
    native shape (the web transfer route returns 409
    ``destination_exists``) without matching on message text. Message
    literals are byte-identical to the plain ``ClickException`` this
    replaces, so every existing ``except ClickException`` / ``str(exc)``
    consumer — CLI verbs, MCP migrate action, the ``migrate_scope``
    wrapper's pinned wording — is untouched.
    """


TransferMode = Literal["move", "copy"]

#: Outcome of the shared→shared install-provenance carry-over (A-4 #1275).
#: ``not_applicable`` covers every transfer the carry-over does not target:
#: non-(shared→shared) tier pairs, and shared→shared sources with no
#: ``lock.json`` entry at all (not wiki-tracked — nothing to carry, quiet).
ProvenanceCarry = Literal["carried", "not_carried", "not_applicable"]

_VALID_SCOPES: tuple[TargetScope, ...] = ("user", "project_shared", "project_local")

#: ADR-0008 lockfile contract: ``wiki_commit`` is the FULL 40-char SHA.
_FULL_SHA_LEN = 40


@dataclass(frozen=True)
class TransferResult:
    """Outcome of one transfer plan or apply.

    ``transferred=False`` marks a dry-run preview. Fatal failures raise
    (:class:`click.ClickException` /
    :class:`memtomem.context.migrate.MigratePartialError`) rather than
    producing a result — same raise-on-fail discipline as
    :class:`memtomem.context.migrate.MigrateScopeResult`.

    ``dst_name`` differs from ``name`` only for a copy-rename
    (``new_name=``). ``fanout_*`` fields mirror ``MigrateScopeResult``
    and are populated for move mode only — copy never touches source
    fan-out, and destination fan-out is never generated by either mode
    (``needs_sync`` + ``sync_command`` carry the follow-up instead).
    ``sync_hint`` is the prose alternative for results whose follow-up
    is not a runnable CLI command; this engine always leaves it ``None``
    (the mcp-servers copy adapter sets it — A-12 #1282 — because no
    ``mm context sync`` phase exists for that kind). Renderers print
    ``sync_command`` as a command, else ``sync_hint`` as prose.
    ``notes`` carries non-fatal caveats the surface should show (today:
    the overrides-travel-verbatim caveat on a copy-rename).

    ``provenance`` / ``provenance_reason`` / ``provenance_reason_code``
    (A-4 #1275) report the shared→shared ``lock.json`` carry-over:
    ``carried`` means the destination entry was written (on a dry-run:
    will be, subject to apply-time re-verification); ``not_carried``
    pairs a human ``provenance_reason`` with a stable
    ``provenance_reason_code`` (the ``_skip_reasons`` contract — CLI
    prints the reason, web/MCP surfaces match on the code);
    ``not_applicable`` is every transfer the carry-over does not target
    and stays quiet (both companion fields ``None``).
    """

    kind: ArtifactKind
    name: str
    dst_name: str
    mode: TransferMode
    from_scope: TargetScope
    to_scope: TargetScope
    src_project_root: Path | None
    dst_project_root: Path | None
    src_path: Path
    dst_path: Path
    layout: Literal["dir", "flat"]
    transferred: bool
    fanout_cleaned: list[Path] = field(default_factory=list)
    fanout_backed_up: list[Path] = field(default_factory=list)
    fanout_planned: list[Path] = field(default_factory=list)
    needs_sync: bool = False
    sync_command: str | None = None
    sync_hint: str | None = None
    notes: tuple[str, ...] = ()
    provenance: ProvenanceCarry = "not_applicable"
    provenance_reason: str | None = None
    provenance_reason_code: ProvenanceSkipCode | None = None


def _resolve_root(root: Path | str | None) -> Path | None:
    return None if root is None else Path(root).expanduser().resolve()


def _sync_followup(to_scope: TargetScope, dst_project_root: Path | None) -> tuple[bool, str | None]:
    """``(needs_sync, exact follow-up command)`` for the destination tier.

    Neither transfer mode generates destination runtime fan-out — that
    stays sync's job, so dry-run and apply previews can never disagree
    with what sync would actually write. ``project_local`` has no
    runtime fan-out at all (ADR-0011 §3), so there is nothing to sync.
    User-tier sync is project-independent; project tiers must run inside
    the destination project, hence the ``cd`` prefix (the cross-project
    ``--project`` selector for sync is A-9 #1279, not yet available).
    """
    if to_scope == "project_local":
        return False, None
    cmd = f"mm context sync --scope {to_scope}"
    if to_scope != "user" and dst_project_root is not None:
        cmd = f"cd {shlex.quote(str(dst_project_root))} && {cmd}"
    return True, cmd


@dataclass(frozen=True)
class _ProvenancePlan:
    """Pre-stage outcome of the shared→shared provenance classification.

    ``carry=True`` captures the source entry's pin and digest map for the
    post-promote verification (:func:`_carry_provenance`); ``carry=False``
    pairs the human reason with its stable code. The "no ``lock.json``
    entry at all" case returns ``None`` from the classifier instead of a
    plan — not wiki-tracked is ``not_applicable``, not a skip.
    """

    carry: bool
    wiki_commit: str | None = None
    digests: dict[str, str] | None = None
    reason: str | None = None
    reason_code: ProvenanceSkipCode | None = None


def _provenance_skip(reason: str, code: ProvenanceSkipCode) -> _ProvenancePlan:
    return _ProvenancePlan(carry=False, reason=reason, reason_code=code)


def _classify_provenance_carry(
    kind: ArtifactKind,
    name: str,
    src_root: Path,
    *,
    renamed: bool,
) -> _ProvenancePlan | None:
    """Classify whether a shared→shared transfer may carry install provenance.

    Runs while the source tree is still on disk (pre-stage in apply mode,
    lock-free in dry-run). Returns ``None`` when the source has no
    ``lock.json`` entry — a non-wiki artifact has nothing to carry and the
    result stays ``not_applicable`` / quiet.

    Carry requires, in order (each failure is a typed skip):

    - a readable source lockfile (corrupt → the transfer itself must not
      fail over bookkeeping; classify and move on);
    - no copy-rename: entries are keyed by artifact name == wiki asset
      name, so an entry under the new name would point ``mm context
      update`` at a DIFFERENT wiki asset — if that asset exists, the
      carried digests would classify the renamed copy clean and let
      update silently clobber it with foreign bytes. The digest gate
      alone does not close this: a manifest with no ``name:`` key makes
      the rename rewrite a no-op, leaving the staged bytes identical.
    - a full 40-char SHA ``wiki_commit`` (the ADR-0008 stored-pin
      contract; carrying an abbreviated or malformed pin would mint
      destination provenance that ``git checkout <pin>`` forensics and
      reachability checks cannot reliably use);
    - a valid per-file digest map (:func:`digests_from_entry`) — the
      design-gate finding behind this whole feature: without byte
      evidence, "clean" can only be claimed from mtimes, and rehashing
      mtime-clean bytes would MANUFACTURE digest evidence over possibly
      backdated local edits, blessing them as wiki bytes for a later
      ``mm context update`` to clobber without the ``--force`` gate.
      Pre-digest (#1247) installs therefore don't carry until updated or
      reinstalled at the source;
    - ``is_asset_dirty`` == ``clean`` over that digest map (modified,
      added, deleted, or unreadable files all refuse — "cannot prove
      clean" protects).
    """
    try:
        entry = Lockfile.at(src_root).read_entry(kind, name)
    except LockfileError as exc:
        logger.warning(
            "transfer: source lockfile at %s unreadable while classifying "
            "provenance carry for %s/%s: %s",
            src_root,
            kind,
            name,
            exc,
        )
        return _provenance_skip(
            "source project's lock.json is unreadable; fix or remove it, then "
            "re-install at the destination to restore update lineage",
            PROVENANCE_SOURCE_LOCKFILE_UNREADABLE,
        )
    if entry is None:
        return None

    if renamed:
        return _provenance_skip(
            "renamed copy: lock.json entries are keyed by wiki asset name, so "
            "provenance under the new name would target a different wiki asset",
            PROVENANCE_RENAMED_COPY,
        )

    wiki_commit = entry.get("wiki_commit")
    if (
        not isinstance(wiki_commit, str)
        or len(wiki_commit) != _FULL_SHA_LEN
        or not set(wiki_commit) <= _HEX_DIGITS
    ):
        return _provenance_skip(
            "source entry's wiki_commit is not a full 40-char SHA (ADR-0008 pin contract)",
            PROVENANCE_SOURCE_INVALID_PIN,
        )

    digests = digests_from_entry(entry)
    if digests is None:
        return _provenance_skip(
            "source entry has no valid per-file digests (pre-digest install); "
            "run `mm context update` at the source first, then retry",
            PROVENANCE_SOURCE_NO_DIGESTS,
        )

    try:
        report = is_asset_dirty(src_root, kind, name, lock_entry=entry)
    except OSError as exc:
        logger.warning(
            "transfer: dirty classification failed for %s/%s at %s: %s",
            kind,
            name,
            src_root,
            exc,
        )
        return _provenance_skip(
            f"source tree could not be classified ({exc}); cannot prove clean",
            PROVENANCE_SOURCE_UNPROVABLE,
        )
    if report.reason == "clean":
        return _ProvenancePlan(carry=True, wiki_commit=wiki_commit, digests=digests)
    if report.reason == "dirty":
        return _provenance_skip(
            f"source has local edits ({report.summary()}); update or revert "
            f"them at the source first, or re-install at the destination",
            PROVENANCE_SOURCE_DIRTY,
        )
    # missing_dest (e.g. a flat-layout source shadowing a stale dir entry)
    # or never_installed (malformed installed_at over an existing dest):
    # the install record cannot vouch for the bytes being transferred.
    return _provenance_skip(
        f"source install record cannot vouch for the transferred bytes ({report.reason})",
        PROVENANCE_SOURCE_UNPROVABLE,
    )


def _carry_provenance(
    kind: ArtifactKind,
    dst_name: str,
    dst_path: Path,
    dst_root: Path,
    plan: _ProvenancePlan,
    *,
    lock_timeout: float | None = None,
) -> tuple[ProvenanceCarry, str | None, ProvenanceSkipCode | None]:
    """Verify the promoted bytes and upsert the destination ``lock.json`` entry.

    Best-effort by contract (issue #1275): no failure here may fail or
    un-commit the transfer — every refusal degrades to ``not_carried`` with a
    loud log. Since ADR-0030 §6 this runs INSIDE the canonical-lock span
    (canonical → lock.json order, bounded by the shared ``lock_timeout``) so a
    concurrent wiki reinstall/upsert can't interleave; best-effort is enforced
    by the caller's ``try/except``, not by releasing the lock.

    The promoted tree is REHASHED and required to equal the source
    entry's digest map exactly (keys and values). This closes the
    classify→stage TOCTOU: sidecar locks don't bind external writers, so
    an edit landing between the clean check and the staging rename would
    otherwise be blessed as pristine wiki bytes. With the equality gate,
    the only digests we ever record are byte-identical to what the wiki
    install recorded — an edit landing after the rehash leaves entry ≠
    disk, which classifies *dirty* later and makes ``mm context update``
    refuse without ``--force`` (the protective direction).

    The paired keys are recomputed per the ``lockfile.upsert_entry``
    contract, not copied verbatim: ``installed_at`` is captured from the
    promoted tree (:func:`installed_at_from_dest`) and
    ``digests_installed_at`` is stamped from it inside ``upsert_entry``;
    ``files`` derives from the rehashed map so the new entry is
    internally consistent by construction (exactly how install writes
    fresh entries).
    """
    rehashed: dict[str, str] = {}
    try:
        for file_path in iter_installed_files(dst_path):
            rel = file_path.relative_to(dst_path).as_posix()
            rehashed[rel] = hashlib.sha256(file_path.read_bytes()).hexdigest()
    except OSError as exc:
        logger.warning(
            "transfer: cannot rehash promoted tree at %s for provenance "
            "carry-over (%s); destination entry not written.",
            dst_path,
            exc,
        )
        return (
            "not_carried",
            f"promoted bytes could not be verified ({exc})",
            PROVENANCE_DEST_BYTES_UNVERIFIED,
        )
    if rehashed != plan.digests:
        logger.warning(
            "transfer: promoted bytes at %s do not match the source install "
            "digests (concurrent edit during transfer?); destination entry "
            "not written.",
            dst_path,
        )
        return (
            "not_carried",
            "promoted bytes do not match the source install digests (changed during transfer)",
            PROVENANCE_DEST_BYTES_UNVERIFIED,
        )

    assert plan.wiki_commit is not None  # carry plans always capture the pin
    try:
        Lockfile.at(dst_root).upsert_entry(
            kind,
            dst_name,
            wiki_commit=plan.wiki_commit,
            installed_at=installed_at_from_dest(dst_path),
            files=sorted(rehashed),
            files_commit=plan.wiki_commit,
            digests=rehashed,
            lock_timeout=lock_timeout,
        )
    except (LockfileError, OSError) as exc:
        logger.warning(
            "transfer: failed to write the destination lock.json entry for "
            "%s/%s at %s (%s); the transfer itself is committed — the "
            "artifact lands untracked (re-install there to restore update "
            "lineage).",
            kind,
            dst_name,
            dst_root,
            exc,
        )
        return (
            "not_carried",
            f"destination lock.json could not be written ({exc})",
            PROVENANCE_DEST_LOCKFILE_ERROR,
        )
    return "carried", None, None


def _provenance_fields(
    plan: _ProvenancePlan | None,
) -> tuple[ProvenanceCarry, str | None, ProvenanceSkipCode | None]:
    """Result-field triple for a classification outcome (dry-run / skip path)."""
    if plan is None:
        return "not_applicable", None, None
    if plan.carry:
        return "carried", None, None
    return "not_carried", plan.reason, plan.reason_code


def _remove_staging(staging: Path) -> None:
    """Best-effort removal of a staging entry whose bytes are safe elsewhere."""
    if staging.exists():
        if staging.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                staging.unlink()


def _stage_copy(src: Path, dst_parent: Path, name_hint: str) -> Path:
    """Copy *src* into a same-dir staging entry under *dst_parent*.

    Copy-mode sibling of :func:`memtomem.context.migrate._stage_move`:
    the source is NEVER consumed or mutated — staging is built from a
    byte copy, so a Gate A block or promote failure needs no rename-back
    rollback, only staging removal. Uses the same ``.migrate-…`` naming
    convention as ``_stage_move`` so every existing internal-artifact
    exclusion treats both alike.

    Symlinks are preserved as links (``symlinks=True`` /
    ``follow_symlinks=False``) — same no-deref contract as the
    ``_stage_move`` EXDEV fallback: dereferencing would materialize
    out-of-tree target bytes into the (possibly git-tracked) destination
    tier, violating the package's no-deref mirror contract
    (``_atomic.copy_tree_atomic``).
    """
    dst_parent.mkdir(parents=True, exist_ok=True)
    suffix = f"{os.getpid()}-{secrets.token_hex(4)}"
    staging = dst_parent / f".migrate-{name_hint}-{suffix}.tmp"
    if staging.exists():
        # Crashed prior run with a colliding suffix (extremely unlikely
        # given pid+rand) — leftover is from us; safe to clear. The clear
        # must stay LOUD here (pre-copy): a survivor — e.g. a symlink
        # rmtree refuses, or an undeletable dir — would make the
        # file-source copy below write INTO the leftover instead of
        # replacing the staging path.
        _remove_staging(staging)
        if staging.exists():
            raise OSError(f"could not clear leftover staging entry: {staging}")
    try:
        if src.is_dir():
            shutil.copytree(src, staging, symlinks=True)
        else:
            shutil.copy2(src, staging, follow_symlinks=False)
    except BaseException:
        _remove_staging(staging)
        raise
    return staging


def _rewrite_staged_manifest_name(
    staging: Path,
    kind: ArtifactKind,
    layout: Literal["dir", "flat"],
    new_name: str,
) -> None:
    """Rewrite the staged manifest's frontmatter ``name:`` to *new_name*.

    Copy-rename support: sync fans out under the **parsed** name
    (``_sync_atomic`` keys on ``adapter.name_of``; the dir/stem is only
    the fallback when frontmatter omits ``name``), so a renamed copy
    that kept ``name: <old>`` would fan out at the destination under the
    OLD name — colliding with, or silently clobbering, whatever
    legitimately owns that name there. This is the one deliberate
    content mutation in the transfer engine, applied to the STAGED bytes
    before the Gate A scan so the scan covers exactly what is promoted.

    Rules (mirroring the ``agents._parse_flat_yaml`` reading):

    - no frontmatter, or no column-0 ``name:`` key → no-op (the
      dir/stem fallback already yields the new name);
    - exactly one ``name:`` key line → replaced with
      ``name: <new_name>`` (``validate_name`` guarantees the value
      needs no quoting);
    - multiple ``name:`` key lines → refuse loudly: the parser keeps
      the last one, so a first-line-only rewrite could silently lose
      to a stale duplicate.

    Tolerance must match the parser's, byte-fidelity must not (Codex
    review fold): ``agents._parse_canonical_agent_text`` strips one
    leading UTF-8 BOM and normalizes CRLF before matching frontmatter
    (#1229), so a BOM/CRLF manifest that silently skipped this rewrite
    would promote under the new directory while still PARSING as the
    old name — exactly the destination collision the rewrite exists to
    close. The detection below is therefore BOM/CRLF-tolerant, but the
    mutation stays minimal: bytes go through ``read_bytes`` /
    ``write_bytes`` (no universal-newline translation), the BOM and
    every line's original ending are preserved verbatim, and only the
    ``name:`` line's content changes.

    ``versions/vN.md`` snapshots and ``overrides/<vendor>.*`` files are
    deliberately NOT rewritten: version snapshots are frozen history
    (ADR-0022) and override bytes are verbatim-by-contract. The caller
    surfaces a :attr:`TransferResult.notes` entry when overrides exist.
    """
    manifest = staging if layout == "flat" else staging / _DIR_MANIFEST[kind]
    text = manifest.read_bytes().decode("utf-8")
    bom = "\ufeff" if text.startswith("\ufeff") else ""
    lines = text[len(bom) :].splitlines(keepends=True)
    if not lines or lines[0].rstrip("\r\n") != "---":
        return
    fence = next((i for i in range(1, len(lines)) if lines[i].rstrip("\r\n") == "---"), None)
    if fence is None:
        # Unterminated frontmatter — the canonical parser rejects this
        # file outright at sync time; not this function's to repair.
        return
    name_lines = [
        i
        for i in range(1, fence)
        if (kv := _KEY_VALUE_RE.match(lines[i].rstrip("\r\n"))) is not None
        and kv.group(1) == "name"
    ]
    if not name_lines:
        return
    if len(name_lines) > 1:
        raise click.ClickException(
            f"cannot rename: {manifest.name} frontmatter has {len(name_lines)} 'name:' "
            f"lines; fix the source artifact so it has exactly one, then retry."
        )
    idx = name_lines[0]
    content = lines[idx].rstrip("\r\n")
    ending = lines[idx][len(content) :]
    lines[idx] = f"name: {new_name}{ending}"
    # Staging is private pre-promote, but the atomic-write invariant is
    # surface-wide (test_context_atomic_write_guard): no bare writes on
    # gateway modules. Mode is preserved from the copied manifest —
    # copy semantics, not the helper's 0600 default.
    atomic_write_bytes(
        manifest,
        (bom + "".join(lines)).encode("utf-8"),
        mode=stat.S_IMODE(manifest.stat().st_mode),
    )


def _offending_file_hint(blocked_path: Path, staging: Path, src_path: Path) -> str:
    """Source-anchored remediation hint for a Gate A block on staged bytes.

    The scan runs against staging, so *blocked_path* names a transient
    ``.migrate-*.tmp`` entry that is gone after rollback. Re-anchor it
    onto the source artifact so the error names the file the user can
    actually edit — including a secret hiding in a frozen
    ``versions/vN.md`` snapshot, which blocks a project_shared landing
    like any other artifact byte (ADR-0022 Gate A covers the whole
    artifact dir).
    """
    try:
        rel = blocked_path.relative_to(staging)
    except ValueError:  # defensive — scan only ever visits staging
        offending = blocked_path
    else:
        offending = src_path if rel == Path(".") else src_path / rel
    return (
        f"Offending file: {offending}\n"
        f"  Remove the secret from that source file (for an old versions/ "
        f"snapshot, delete or rewrite the snapshot), or transfer to a "
        f"non-shared tier (--to project_local) instead."
    )


def _rename_overrides_note(probe_dir: Path, dst_path: Path, name: str) -> tuple[str, ...]:
    """The copy-rename overrides caveat, or ``()`` when it doesn't apply.

    One derivation for BOTH the dry-run preview (probing the source tree)
    and the apply path (probing the staged tree, which mirrors the source
    byte-for-byte) — so the preview can never stay silent about a caveat
    the apply would print.
    """
    if not (probe_dir / "overrides").is_dir():
        return ()
    return (
        f"renamed copy keeps overrides/ verbatim — review "
        f"{dst_path / 'overrides'} for content still referring "
        f"to '{name}'",
    )


def _partial_move_message(
    kind: ArtifactKind,
    name: str,
    src_path: Path,
    dst_path: Path,
    src_scope: TargetScope,
    to_scope: TargetScope,
    errno_: int | None,
    cross_root: bool,
    sync_command: str | None,
) -> str:
    """EXDEV partial-move message; byte-identical to migrate_scope's when same-root.

    A ``project_local`` destination gets no "then run sync" instruction:
    that tier has no runtime fan-out (ADR-0011 §3), so ``mm context sync
    --scope project_local`` is a NO_FANOUT no-op and telling the user to
    run it undermines the (real) warning that follows. The stale-source
    warning stays — the SOURCE tier's fan-out hazard is what matters.
    """
    if not cross_root:
        if to_scope == "project_local":
            remove_clause = f"Remove {src_path} manually. "
        else:
            remove_clause = (
                f"Remove {src_path} manually, "
                f"then run `mm context sync --scope {to_scope}` to refresh "
                f"runtime fan-out at the new tier. "
            )
        return (
            f"Migrate {kind}/{name}: canonical copied to {dst_path} but "
            f"failed to remove stale source at {src_path} (errno={errno_}). "
            f"Both canonicals now exist on disk. {remove_clause}"
            f"Until then, do NOT run "
            f"`mm context sync --scope {src_scope}` — it would recreate "
            f"runtime fan-out from the stale source."
        )
    if to_scope == "project_local":
        remove_clause = f"Remove {src_path} manually. "
    else:
        followup = sync_command or f"mm context sync --scope {to_scope}"
        remove_clause = (
            f"Remove {src_path} manually, "
            f"then run `{followup}` to refresh runtime fan-out at the destination. "
        )
    return (
        f"Transfer {kind}/{name}: canonical copied to {dst_path} but "
        f"failed to remove stale source at {src_path} (errno={errno_}). "
        f"Both canonicals now exist on disk. {remove_clause}"
        f"Until then, do NOT run `mm context sync --scope "
        f"{src_scope}` in the source project — it would recreate runtime "
        f"fan-out from the stale source."
    )


def transfer_artifact(
    kind: ArtifactKind,
    name: str,
    *,
    src_project_root: Path | str | None,
    from_scope: TargetScope | None,
    dst_project_root: Path | str | None,
    to_scope: TargetScope,
    mode: TransferMode,
    apply_: bool,
    surface: str = "cli_context_transfer",
    new_name: str | None = None,
    lock_timeout: float | None = None,
) -> TransferResult:
    """Move or copy one canonical artifact between tiers and/or projects.

    Pure engine entry point — no prompts, no stdout writes; surfaces own
    all user-facing output and Gate B confirmation. Errors raise
    :class:`click.ClickException` so wrappers can re-raise verbatim.

    Args:
        kind: ``agents`` / ``commands`` / ``skills``
            (:data:`memtomem.context.migrate.SCOPE_MIGRATABLE_KINDS`).
        name: Source artifact name.
        src_project_root: Project root owning the source ``.memtomem``
            tree. ``None`` is valid only when the source lives at the
            user tier (auto-detect with ``None`` probes the user tier
            alone).
        from_scope: Source tier, or ``None`` to auto-detect within the
            source project (ambiguity across tiers raises, same as
            ``migrate_scope``).
        dst_project_root: Project root owning the destination tree.
            ``None`` is valid only for ``to_scope="user"``.
        to_scope: Destination tier.
        mode: ``"move"`` (source consumed, stale source fan-out cleaned)
            or ``"copy"`` (source never touched).
        apply_: ``False`` returns the dry-run plan without touching disk.
        surface: Gate A audit identifier forwarded to the staging scan.
        new_name: Copy-mode-only rename (``--as``); re-validated via
            ``validate_name``, and the staged manifest's frontmatter
            ``name:`` is rewritten to match
            (:func:`_rewrite_staged_manifest_name`).
        lock_timeout: Whole-call acquisition budget (seconds) for the
            artifact pair lock, shared across both sidecar acquisitions
            (``_acquire_pair_lock``). ``None`` (default) blocks
            indefinitely — the historical CLI/MCP behavior. The web
            route passes a bound so its un-cancellable worker thread
            self-aborts (``TimeoutError``, nothing acquired or
            committed) inside the route's own ``asyncio.timeout``
            window instead of writing after a 503 (#1145 shape).

    Apply sequence (move):

    1. Detect / validate source; resolve destination via
       ``canonical_artifact_dir(kind, to_scope, dst_project_root)``.
    2. Reject same-store pairs — same ``(project, tier)`` and the
       cross-project ``user→user`` degenerate (user tier is global).
    3. Refuse on destination collision (Row 15 parity: no ``--force``
       overwrite; replace verb remains a follow-up).
    4. Acquire BOTH name-keyed canonical locks in sorted order
       (``acquire_canonical_locks`` on ``(src_store, name)`` +
       ``(dst_store, dst_name)`` — ``str(lock_path)`` sort is a total
       order across two project roots; ADR-0030 §6).
    5. Stage via rename (EXDEV → copy fallback), Gate A scan on staging
       iff ``to_scope == "project_shared"``, promote via ``os.replace``.
       Rollback preserves "staging deleted only when the bytes are
       verified safe elsewhere".
    6. Still INSIDE the canonical-lock span (ADR-0030 §6 — so a wiki
       reinstall can't interleave; bookkeeping stays best-effort via
       try/except, not by releasing the lock, and shares the canonical
       deadline): for a shared→shared transfer, carry the install
       provenance to the destination ``lock.json`` when the source was
       classified clean pre-stage AND the promoted bytes rehash to the
       source entry's exact digest map (A-4 #1275; see
       :func:`_classify_provenance_carry` / :func:`_carry_provenance`);
       then drop the source project's entry when moving out of
       ``project_shared``.
    7. AFTER the locks release, clean stale SOURCE runtime fan-out
       (runtime targets, not canonicals) under the two-root contract
       (discovery at the source root, override/render verification
       against the destination root). Destination fan-out is NOT
       generated — the result carries ``needs_sync`` + the exact
       follow-up sync command.

    Copy mode replaces step 5's staging with a byte copy
    (:func:`_stage_copy`, source never consumed), applies the optional
    rename rewrite before the Gate A scan, and runs only the carry-over
    half of step 6 (no source fan-out cleanup, no source ``lock.json``
    change — a shared→shared copy of a clean wiki install ends with the
    entry at BOTH ends; a copy-rename never carries). ``versions/`` +
    ``versions.json`` live inside the artifact dir and travel
    implicitly in both modes.
    """
    if kind not in SCOPE_MIGRATABLE_KINDS:
        raise click.ClickException(
            f"unsupported kind for artifact transfer: {kind!r} "
            f"(use one of {SCOPE_MIGRATABLE_KINDS})"
        )
    if mode not in ("move", "copy"):
        raise click.ClickException(f"unsupported transfer mode: {mode!r} (use 'move' or 'copy')")
    validate_name(name, kind=f"{kind[:-1]} name")
    if new_name is not None:
        if mode != "copy":
            raise click.ClickException(
                "renaming (--as / new_name) is supported in copy mode only; "
                "move keeps the artifact name."
            )
        validate_name(new_name, kind=f"{kind[:-1]} name")
    if from_scope is not None and from_scope not in _VALID_SCOPES:
        raise click.ClickException(f"unsupported source scope: {from_scope!r}")
    if to_scope not in _VALID_SCOPES:
        raise click.ClickException(f"unsupported destination scope: {to_scope!r}")

    src_root = _resolve_root(src_project_root)
    dst_root = _resolve_root(dst_project_root)
    if from_scope in ("project_shared", "project_local") and src_root is None:
        raise click.ClickException(f"from_scope='{from_scope}' requires src_project_root.")
    if to_scope in ("project_shared", "project_local") and dst_root is None:
        raise click.ClickException(f"to_scope='{to_scope}' requires dst_project_root.")

    src_scope, src_path, layout = _detect_source_scope(kind, name, src_root, from_scope)

    src_store = canonical_artifact_dir(kind, src_scope, src_root)
    dst_store = canonical_artifact_dir(kind, to_scope, dst_root)
    if src_store == dst_store:
        # Same-(root, tier) pairs and the cross-project user→user
        # degenerate both collapse to "src and dst are the same store" —
        # ``canonical_artifact_dir`` is deterministic per (kind, scope,
        # root) and the user tier ignores the root entirely.
        if mode == "move" and src_scope == to_scope and src_root == dst_root:
            # Historical migrate_scope wording — the same-root wrapper
            # relies on this literal staying byte-identical.
            raise click.ClickException(f"{kind}/{name} is already at scope='{to_scope}' (no-op).")
        if src_scope == "user" and to_scope == "user":
            raise click.ClickException(
                f"user tier is global — {kind}/{name} already lives in the shared user "
                f"store ({src_store}); cross-project user→user transfer has nothing to "
                f"move or copy."
            )
        raise click.ClickException(
            f"source and destination resolve to the same canonical store ({src_store}); "
            f"same-(project, tier) transfer is not supported (a rename/duplicate verb is "
            f"out of scope for ADR-0023)."
        )

    dst_name = new_name if new_name is not None else name
    dst_path = dst_store / dst_name if layout == "dir" else dst_store / f"{dst_name}.md"

    # Pre-flight conflict check (also re-checked inside the lock).
    if dst_path.exists():
        raise TransferCollisionError(
            f"destination already exists: {dst_path}. "
            "Resolve manually or remove the existing entry first. "
            "--force does not overwrite scope-tier targets in PR-E4 "
            "(replace verb is a follow-up)."
        )

    needs_sync, sync_command = _sync_followup(to_scope, dst_root)
    cross_root = src_root != dst_root
    # Same-root moves keep migrate_scope's historical Gate A block message
    # byte-identical; the transfer-native surfaces (cross-project, copy)
    # get the source-anchored offending-file hint instead.
    transfer_hint = mode == "copy" or cross_root

    def _plan_provenance() -> _ProvenancePlan | None:
        # shared→shared (A-4 #1275) — necessarily cross-project here: the
        # same-store pairs were rejected above. Only this tier pair carries
        # install provenance; the lockfile tracks project_shared installs
        # only. ``None`` for every other pair == ``not_applicable``.
        if src_scope != "project_shared" or to_scope != "project_shared" or src_root is None:
            return None
        return _classify_provenance_carry(kind, name, src_root, renamed=new_name is not None)

    if not apply_:
        # Dry-run: compute plan, no mutation. ``fanout_planned`` previews
        # the deletion half of a move — the same selection the apply-side
        # cleanup uses, so preview and apply cannot disagree about what
        # gets removed. Copy has no deletion half by definition. The
        # provenance triple previews the same classification apply runs
        # pre-stage (apply additionally re-verifies the promoted bytes).
        provenance, provenance_reason, provenance_reason_code = _provenance_fields(
            _plan_provenance()
        )
        return TransferResult(
            kind=kind,
            name=name,
            dst_name=dst_name,
            mode=mode,
            from_scope=src_scope,
            to_scope=to_scope,
            src_project_root=src_root,
            dst_project_root=dst_root,
            src_path=src_path,
            dst_path=dst_path,
            layout=layout,
            transferred=False,
            fanout_planned=(
                [
                    target
                    for _runtime, target in _existing_fanout_targets(
                        kind, name, src_scope, src_root
                    )
                ]
                if mode == "move"
                else []
            ),
            needs_sync=needs_sync,
            sync_command=sync_command,
            provenance=provenance,
            provenance_reason=provenance_reason,
            provenance_reason_code=provenance_reason_code,
            # Preview the copy-rename overrides caveat off the SOURCE tree —
            # the same derivation the apply path runs against its staged
            # mirror, so the plan the user confirms shows every caveat the
            # apply would print (this used to be apply-only).
            notes=(
                _rename_overrides_note(src_path, dst_path, name)
                if mode == "copy" and new_name is not None and layout == "dir"
                else ()
            ),
        )

    # ── apply path ───────────────────────────────────────────────────
    notes: tuple[str, ...] = ()
    # ADR-0030 §6: name-keyed canonical locks (layout-independent), so this
    # transfer serializes with a Pull / migrate / version op on the same
    # artifact name — the path-keyed pair lock did not. ``dst_name`` may differ
    # from ``name`` (copy --as rename). ONE monotonic deadline spans the
    # canonical locks AND the downstream lock.json bookkeeping (upsert on carry,
    # remove_entry on the source), so a web worker can't outlive its route
    # timeout mutating provenance after the canonical locks release.
    _txn_deadline = None if lock_timeout is None else time.monotonic() + lock_timeout

    def _lock_remaining() -> float | None:
        return None if _txn_deadline is None else max(0.0, _txn_deadline - time.monotonic())

    with acquire_canonical_locks([(src_store, name), (dst_store, dst_name)], timeout=lock_timeout):
        # Re-check dst inside the lock window — some other process could
        # have created it between the dry-run preview and the apply
        # phase, even with our own lock-pair held (the writer would have
        # had to take the same lock, but check defensively).
        if dst_path.exists():
            raise TransferCollisionError(f"destination appeared during lock acquire: {dst_path}.")

        # Provenance classification must run while the SOURCE tree still
        # exists (move staging consumes it). Read-only; the write half
        # (`_carry_provenance`) runs below, still INSIDE this lock span, and
        # re-verifies the promoted bytes, so an edit slipping in cannot be
        # blessed (the equality gate there is the actual TOCTOU close).
        provenance_plan = _plan_provenance()

        if mode == "copy":
            staging = _stage_copy(src_path, dst_path.parent, name_hint=dst_name)
            try:
                if new_name is not None:
                    _rewrite_staged_manifest_name(staging, kind, layout, new_name)
                    if layout == "dir":
                        # Shared derivation with the dry-run preview (probed
                        # off src there) — see _rename_overrides_note.
                        notes = _rename_overrides_note(staging, dst_path, name)
                if to_scope == "project_shared":
                    scan = scan_artifact_tree(
                        staging,
                        surface=surface,
                        scope=to_scope,
                        project_root=dst_root,
                        on_blocked="fail_fast",
                    )
                    if scan.blocked:
                        raise_or_collect(
                            scan.blocked[0],
                            scope=to_scope,
                            kind=kind[:-1],
                            artifact_name=dst_name,
                            remediation_hint=_offending_file_hint(
                                scan.blocked[0].path, staging, src_path
                            ),
                        )
                try:
                    _promote_move(staging, dst_path)
                except FileExistsError as exc:
                    # TOCTOU: an external writer (one not holding our sidecar
                    # lock) created dst between the in-lock ``dst_path.exists()``
                    # check and the promote. Surface the typed collision (web
                    # 409 / CLI ``ClickException``) instead of a bare
                    # ``FileExistsError`` the engine never declares (#1385
                    # finding 3). Nested inside the outer ``try`` so the staging
                    # cleanup below still runs.
                    raise TransferCollisionError(
                        f"destination appeared during promote: {dst_path}."
                    ) from exc
            except BaseException:
                # Copy staging never consumed the source — the source
                # bytes are intact at src_path by construction, so
                # dropping staging is always safe (zero residue at the
                # destination, nothing to rename back).
                _remove_staging(staging)
                raise
        else:
            staging, src_consumed = _stage_move(src_path, dst_path.parent, name_hint=name)

            try:
                # Gate A on the staged content if landing in project_shared.
                # The scan runs against staging (the bytes about to be
                # promoted), not against src — so any in-flight edits caught
                # mid-rename are still scanned.
                if to_scope == "project_shared":
                    scan = scan_artifact_tree(
                        staging,
                        surface=surface,
                        scope=to_scope,
                        project_root=dst_root,
                        on_blocked="fail_fast",
                    )
                    if scan.blocked:
                        # Raise — project_shared has no force valve
                        # (mirrors PR-D memory-migrate and PR-E3 sync-side).
                        raise_or_collect(
                            scan.blocked[0],
                            scope=to_scope,
                            kind=kind[:-1],
                            artifact_name=name,
                            remediation_hint=(
                                _offending_file_hint(scan.blocked[0].path, staging, src_path)
                                if transfer_hint
                                else None
                            ),
                        )

                try:
                    _promote_move(staging, dst_path)
                except FileExistsError as exc:
                    # Same promote-window TOCTOU as the copy branch — typed
                    # collision so the move rollback below runs and the caller
                    # sees a 409 / clean CLI error (#1385 finding 3).
                    raise TransferCollisionError(
                        f"destination appeared during promote: {dst_path}."
                    ) from exc
            except BaseException:
                # Roll back: put bytes back at src so the caller can retry
                # without manual cleanup.
                #
                # The cleanup rule is: staging is deleted only when we KNOW
                # the bytes are safe elsewhere. There are exactly three safe
                # cases; everything else preserves staging as a recovery copy
                # and logs a loud ERROR pointing the user at it.
                #
                # Codex review #1 fold caught the "rename-back fails →
                # staging gets deleted" path. Re-review then caught a
                # subtler one: ``not src_path.exists()`` was a TOCTOU — an
                # external writer (``mm context install``, a manual file op,
                # any tool that does not take our sidecar lock) can recreate
                # ``src_path`` between our ``os.rename`` and rollback. The
                # old guard would skip rename-back (src "already there") and
                # the cleanup branch would delete staging anyway, even
                # though the bytes at src now belong to someone else.
                cleanup_staging = False
                if not src_consumed:
                    # EXDEV fallback: src was never consumed, staging is
                    # just a copy. Safe to drop.
                    cleanup_staging = True
                elif src_path.exists():
                    # Same-FS path consumed src, but src has reappeared —
                    # not by us. Don't overwrite the new src bytes; don't
                    # delete staging either. User reconciles manually.
                    logger.error(
                        "transfer rollback: src %s reappeared during apply "
                        "(another writer outside our lock); preserving staging "
                        "at %s as a recovery copy — manual reconciliation "
                        "required.",
                        src_path,
                        staging,
                    )
                elif staging.exists():
                    # Same-FS path consumed src; src is gone as expected;
                    # try the rename-back. Success consumes staging (cleanup
                    # is a no-op then); failure preserves staging.
                    try:
                        os.replace(staging, src_path)
                        cleanup_staging = True
                    except OSError as exc:
                        logger.error(
                            "transfer rollback: rename-back failed (%s); "
                            "staging at %s is the ONLY surviving copy of the "
                            "source bytes — manual recovery required (mv it "
                            "back to %s).",
                            exc,
                            staging,
                            src_path,
                        )
                # else: src_consumed and staging is gone too — nothing to do.

                if cleanup_staging:
                    _remove_staging(staging)
                raise

            # EXDEV cleanup — promoted dst now holds the bytes; src copy is
            # stale and must be removed for the move to be complete.
            if not src_consumed and src_path.exists():
                try:
                    if src_path.is_dir():
                        shutil.rmtree(src_path)
                    else:
                        src_path.unlink()
                except OSError as exc:
                    # Canonical is at dst but src cleanup failed — both
                    # canonicals are on disk. Rolling back dst would just
                    # restore the duplicate state we just resolved (and the
                    # bytes at src are presumably the same — copy succeeded).
                    # Instead, surface as a hard error so the caller does
                    # NOT report "moved" and does NOT proceed to fan-out
                    # cleanup. Without this fail-loud, the next
                    # ``mm context sync`` at src_scope would recreate runtime
                    # fan-out from the stale src (#895 P2 review #5).
                    logger.error(
                        "EXDEV cleanup: failed to remove stale src %s: %s",
                        src_path,
                        exc,
                    )
                    raise MigratePartialError(
                        _partial_move_message(
                            kind,
                            name,
                            src_path,
                            dst_path,
                            src_scope,
                            to_scope,
                            exc.errno,
                            cross_root,
                            sync_command,
                        ),
                        src_path=src_path,
                        dst_path=dst_path,
                    ) from exc

        # Lockfile bookkeeping runs INSIDE the canonical lock span (ADR-0030 §6
        # / Codex M): a concurrent wiki reinstall/upsert of this artifact must
        # not interleave between the committed move and the source-entry drop,
        # or transfer could delete a freshly reinstalled entry. It stays
        # best-effort — the try/except (NOT releasing the lock) is what
        # guarantees a bookkeeping failure never rolls back the committed
        # transfer. Order: canonical (held) → lock.json (each ``Lockfile`` op
        # takes its own sidecar beneath us).
        #
        # Provenance carry-over (A-4 #1275) runs FIRST, in both modes, so a
        # shared→shared move upserts the destination entry before dropping the
        # source one — the carried record never has a gap where neither project
        # tracks the artifact.
        provenance, provenance_reason, provenance_reason_code = _provenance_fields(provenance_plan)
        if provenance_plan is not None and provenance_plan.carry:
            assert dst_root is not None  # shared→shared implies a project root
            provenance, provenance_reason, provenance_reason_code = _carry_provenance(
                kind, dst_name, dst_path, dst_root, provenance_plan, lock_timeout=_lock_remaining()
            )

        if mode == "move" and src_scope == "project_shared" and src_root is not None:
            # The wiki-install lockfile (``lock.json``) only tracks
            # project_shared installs; moving an artifact OUT of project_shared
            # leaves its entry dangling, and `mm context status` would then
            # iterate that entry, find the canonical gone, and report the
            # (now-moved) artifact as "missing" (#1123 B4-1). Drop the stale
            # entry at the SOURCE project's lockfile, unconditionally: even when
            # the carry-over above declined or failed, the source canonical is
            # gone and a dangling entry is pure status noise.
            try:
                Lockfile.at(src_root).remove_entry(kind, name, lock_timeout=_lock_remaining())
            except Exception as exc:  # bookkeeping must never fail a committed move
                logger.warning(
                    "transfer: failed to drop stale lock.json entry for %s/%s "
                    "after moving out of project_shared (%s); `mm context status` "
                    "may report it as missing until the entry is removed.",
                    kind,
                    name,
                    exc,
                )

    # Lock released. Source runtime fan-out cleanup touches runtime targets
    # (``~/.claude`` etc.), never the canonical, and can be slow — so it runs
    # OUTSIDE the canonical lock (best-effort; a partial cleanup must not fail
    # the committed transfer).
    fanout_cleaned: list[Path] = []
    fanout_backed_up: list[Path] = []
    if mode == "move":
        # Two-root contract: discovery walks the SOURCE root's runtime tree;
        # expected-render / override verification reads the canonical at its
        # DESTINATION location (overrides travel with the artifact).
        fanout_cleaned, fanout_backed_up = _remove_runtime_fanout_for(
            kind,
            name,
            src_scope,
            src_root,
            dst_path=dst_path,
            to_scope=to_scope,
            layout=layout,
            dst_project_root=dst_root,
        )

    return TransferResult(
        kind=kind,
        name=name,
        dst_name=dst_name,
        mode=mode,
        from_scope=src_scope,
        to_scope=to_scope,
        src_project_root=src_root,
        dst_project_root=dst_root,
        src_path=src_path,
        dst_path=dst_path,
        layout=layout,
        transferred=True,
        fanout_cleaned=fanout_cleaned,
        fanout_backed_up=fanout_backed_up,
        needs_sync=needs_sync,
        sync_command=sync_command,
        notes=notes,
        provenance=provenance,
        provenance_reason=provenance_reason,
        provenance_reason_code=provenance_reason_code,
    )
