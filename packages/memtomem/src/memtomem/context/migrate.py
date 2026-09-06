"""Convert flat-layout context assets to canonical directory layout.

ADR-0008 PR-D C4. PR-C made the directory layout (e.g.
``agents/<name>/agent.md``) canonical for agents and commands; pre-PR-C
installs and reverse-imports left flat-layout files (``agents/<name>.md``)
on disk. This module classifies and converts those flat assets to the
dir layout in place.

ADR-0011 PR-E4 extends the same module with :func:`migrate_scope`, which
moves an existing canonical artifact between ADR-0011 scope tiers
(``user`` ↔ ``project_shared`` ↔ ``project_local``). The flat→dir path
and the scope-move path share the dry-run/apply/click-exception
discipline; they branch on whether ``--to <scope>`` is passed at the CLI
layer. Since ADR-0023 the scope-move orchestration lives in
:mod:`memtomem.context.transfer` (which generalizes it to cross-project
move|copy); ``migrate_scope`` remains here as a thin same-root wrapper
with byte-compatible results, and the staging / pair-lock / fan-out
primitives below are shared by both modules.

Pure module: filesystem + lockfile only, no wiki dependency (ADR-0008
Invariants 1 / 3). The CLI wrapper in
:func:`memtomem.cli.context_cmd.migrate_cmd` adds the dry-run preview,
``--apply`` gating, and confirmation prompts.

Skills are always directory layout (Agent Skills spec) so flat→dir
classification is a no-op for them — :func:`classify_migrate` returns an
empty list when invoked with ``asset_type="skills"`` and the CLI surfaces
a friendly informational message rather than an error. Scope-move
(:func:`migrate_scope`) DOES support skills since their canonical can
live at any tier per ADR-0011 §3.
"""

from __future__ import annotations

import contextlib
import errno
import logging
import os
import secrets
import shutil
import stat
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, NamedTuple

import click

from memtomem.config import TargetScope
from memtomem.context import override as _override
from memtomem.context._atomic import (
    _file_lock,
    _lock_path_for,
    rename_no_replace,
    rename_refused_by_occupant,
)
from memtomem.context._canonical_txn import canonical_sidecar_lock
from memtomem.context._dir_swap import has_pending_swap
from memtomem.context._names import (
    GENERATOR_VENDOR,
    InvalidNameError,
    is_internal_artifact_dir,
    validate_name,
)
from memtomem.context._runtime_targets import runtime_fanout_root
from memtomem.context.agents import (
    AGENT_DIR_FILENAME,
    AGENT_GENERATORS,
    CANONICAL_AGENT_ROOT,
    AgentParseError,
    list_canonical_agents,
    parse_canonical_agent,
)
from memtomem.context.commands import (
    CANONICAL_COMMAND_ROOT,
    COMMAND_DIR_FILENAME,
    COMMAND_GENERATORS,
    CommandParseError,
    list_canonical_commands,
    parse_canonical_command,
)
from memtomem.context.lockfile import Lockfile
from memtomem.context.scope_resolver import (
    ArtifactKind,
    ContextScopeError,
    canonical_artifact_dir,
)
from memtomem.context.skills import (
    SKILL_GENERATORS,
    SKILL_MANIFEST,
    _skill_effective_equal,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ASSET_DIR_FILENAMES",
    "MIGRATABLE_ASSET_TYPES",
    "ArtifactNotFoundError",
    "MigratePartialError",
    "SCOPE_MIGRATABLE_KINDS",
    "MigrateResult",
    "MigrateRow",
    "MigrateScopeResult",
    "MigrateState",
    "TransferStagingBusyError",
    "adopt_flat_to_dir",
    "classify_migrate",
    "migrate_one",
    "migrate_scope",
]


class MigratePartialError(Exception):
    """Raised when a scope-tier migrate cannot be cleanly completed.

    Specifically raised by the EXDEV-fallback path when the canonical
    has been copied to ``dst`` but the pre-move source cannot be
    removed (permissions, open file handle, etc.). Since #2313 that
    source no longer sits at its canonical path — it was parked in a
    holding entry beside it before the copy — so what survives is the
    destination canonical plus a source-side holding copy under an
    internal name, and ``src_path`` carries THAT path: the one the
    user has to deal with by hand. The move also stopped before
    clearing the source tier's runtime fan-out, which is why the
    message tells the operator not to run ``mm context sync`` at the
    source scope yet (#895 P2 review #5).

    The error carries both paths so the caller (CLI / web / MCP)
    can surface a remediation hint pointing at the entry the user
    needs to remove manually. Translation to surface-native errors
    follows the same pattern as :class:`PrivacyScanError`.
    """

    def __init__(self, message: str, *, src_path: Path, dst_path: Path) -> None:
        super().__init__(message)
        self.message = message
        self.src_path = src_path
        self.dst_path = dst_path


class ArtifactNotFoundError(click.ClickException):
    """Source artifact missing from the probed scope(s) (A-5 #1276).

    Typed subclass so non-CLI surfaces can map "not found" to their
    native shape (the web transfer route returns 404) without matching
    on message text. Message literals are byte-identical to the plain
    ``ClickException`` this replaces — every existing
    ``except ClickException`` / ``str(exc)`` consumer (CLI verbs, MCP
    actions, the ``migrate_scope`` wrapper contract) is untouched.
    Raised only by the :func:`_detect_source_scope` not-found branches;
    the multi-scope ambiguity raise stays a plain ``ClickException``
    (the artifact exists — the selector is what's wrong).
    """


MigrateState = Literal[
    "migrate",
    "noop",
    "cleanup_flat",
    "refuse_dirty",
    "skip_manual",
    "skip_orphan",
]
"""Per-asset classification produced by :func:`classify_migrate`.

- ``migrate`` — flat exists, dir absent, lockfile entry clean → safe to
  rename flat into dir layout.
- ``noop`` — already directory layout, nothing to do.
- ``cleanup_flat`` — both flat and dir exist (PR-C dir-wins collision);
  remove the flat sibling.
- ``refuse_dirty`` — flat has user edits since the recorded
  ``installed_at`` timestamp; require ``--force``.
- ``skip_manual`` — flat exists but has no lockfile entry, so it sits
  outside the install/upgrade lifecycle. Migrate refuses to touch it; the
  user resolves manually (typically by running ``mm context install``).
- ``skip_orphan`` — lockfile entry exists but neither flat nor dir is on
  disk. Rare; surfaced so users notice the dangling entry.
"""

MIGRATABLE_ASSET_TYPES: tuple[str, ...] = ("agents", "commands")
"""Asset types this command can migrate.

Skills are always directory layout (Agent Skills spec). The CLI accepts
``"skills"`` as an argument but exits with an informational message
rather than running classification.
"""

ASSET_DIR_FILENAMES: dict[str, str] = {
    "agents": AGENT_DIR_FILENAME,
    "commands": COMMAND_DIR_FILENAME,
}

_CANONICAL_ROOTS: dict[str, str] = {
    "agents": CANONICAL_AGENT_ROOT,
    "commands": CANONICAL_COMMAND_ROOT,
}


@dataclass(frozen=True)
class MigrateRow:
    """One asset's migration plan.

    ``flat_path`` and ``dir_path`` are the canonical destinations whether
    or not the file currently exists on disk; executors use them as
    targets. ``flat_dirty`` is ``None`` when the dirty check did not
    apply — either no flat file is on disk or no lockfile entry was
    found (per locked decisions #5 and #12).
    """

    asset_type: str
    name: str
    flat_path: Path
    dir_path: Path
    flat_exists: bool
    dir_exists: bool
    has_lock_entry: bool
    flat_dirty: bool | None
    state: MigrateState
    reason: str


@dataclass(frozen=True)
class MigrateResult:
    """Outcome of executing one row's plan.

    ``bak_path`` is set only when ``force`` was true and the flat file
    was dirty: a ``shutil.copy2`` snapshot is written next to the flat
    file before mutation. Mirrors the ``mm context update --force``
    convention (locked decision #6).
    """

    row: MigrateRow
    bak_path: Path | None
    ok: bool
    error: str | None


def _is_flat_file_dirty(flat_path: Path, lock_entry: dict[str, Any]) -> bool:
    """Strict ``mtime > installed_at_epoch`` for a single flat file.

    :func:`memtomem.context.dirty.is_asset_dirty` walks
    ``<root>/<type>/<name>/`` (directory only) and returns
    ``missing_dest`` for a flat file. This helper applies the same rule
    one level up. Equality with ``installed_at`` is clean (strict ``>``).

    Timezone handling matches ``dirty.py`` — ``datetime.fromisoformat``
    on the ISO-8601Z string. Python 3.11+ accepts the ``Z`` suffix
    natively, so no manual replacement is needed.

    Callers must pre-validate ``installed_at`` with
    :func:`_installed_at_parseable` (``_classify_row`` demotes unusable
    entries to ``lock_entry=None`` before reaching here). Deliberately NOT
    catching ``ValueError`` here — returning ``False`` would mean "clean"
    and approve overwriting user edits on a corrupt entry (#1247 id 1).
    """
    installed_at = lock_entry.get("installed_at")
    if not isinstance(installed_at, str):
        return False
    installed_at_epoch = datetime.fromisoformat(installed_at).timestamp()
    return flat_path.stat().st_mtime > installed_at_epoch


def _installed_at_parseable(lock_entry: dict[str, Any]) -> bool:
    """``True`` when the entry's ``installed_at`` is a parseable ISO-8601 string."""
    installed_at = lock_entry.get("installed_at")
    if not isinstance(installed_at, str):
        return False
    try:
        datetime.fromisoformat(installed_at)
    except ValueError:
        return False
    return True


def _flat_path_for(project_root: Path, asset_type: str, name: str) -> Path:
    return project_root / _CANONICAL_ROOTS[asset_type] / f"{name}.md"


def _dir_path_for(project_root: Path, asset_type: str, name: str) -> Path:
    return project_root / _CANONICAL_ROOTS[asset_type] / name


def _classify_row(
    project_root: Path,
    asset_type: str,
    name: str,
    lock_entry: dict[str, Any] | None,
) -> MigrateRow | None:
    """Classify one ``(asset_type, name)`` pair.

    Returns ``None`` when neither flat nor dir is on disk and there is
    no lockfile entry — there's nothing to surface. Otherwise the
    eight-row truth table in the C4 plan is implemented here.
    """
    flat_path = _flat_path_for(project_root, asset_type, name)
    dir_path = _dir_path_for(project_root, asset_type, name)
    asset_filename = ASSET_DIR_FILENAMES[asset_type]
    flat_exists = flat_path.is_file()
    dir_exists = (dir_path / asset_filename).is_file()

    # Treat a lockfile entry without a usable ``installed_at`` as "no
    # entry" — mirrors :func:`memtomem.context.dirty.is_asset_dirty`,
    # which collapses "no entry", "entry but missing/non-string
    # installed_at" and "unparseable installed_at string" into
    # ``never_installed``. Otherwise migrate would silently proceed
    # against a corrupt entry (no dirty check possible) and could
    # overwrite user edits. The parse probe (not just isinstance) keeps
    # an unparseable string from reaching ``_is_flat_file_dirty``'s
    # deliberately-unguarded ``fromisoformat`` (#1247 id 1).
    if lock_entry is not None and not _installed_at_parseable(lock_entry):
        logger.warning(
            "migrate: %s/%s lockfile entry missing or invalid installed_at; "
            "treating as never installed",
            asset_type,
            name,
        )
        lock_entry = None
    has_lock_entry = lock_entry is not None

    if not flat_exists and not dir_exists and not has_lock_entry:
        return None

    flat_dirty: bool | None = None
    state: MigrateState
    reason: str

    if not flat_exists and dir_exists:
        state = "noop"
        reason = "already dir layout"
    elif not flat_exists and not dir_exists:
        state = "skip_orphan"
        reason = "lockfile entry but no files on disk"
    elif flat_exists and not has_lock_entry:
        state = "skip_manual"
        reason = (
            "manual flat file collides with dir; resolve manually"
            if dir_exists
            else "manual flat file (no lockfile entry); resolve manually"
        )
    else:
        # flat_exists and has_lock_entry → dirty check applies
        assert lock_entry is not None
        flat_dirty = _is_flat_file_dirty(flat_path, lock_entry)
        if dir_exists:
            if flat_dirty:
                state = "refuse_dirty"
                reason = "flat+dir collision; flat has local edits since install"
            else:
                state = "cleanup_flat"
                reason = "flat+dir collision; dir wins, will remove flat"
        elif flat_dirty:
            state = "refuse_dirty"
            reason = "flat has local edits since install"
        else:
            state = "migrate"
            reason = "flat → dir"

    return MigrateRow(
        asset_type=asset_type,
        name=name,
        flat_path=flat_path,
        dir_path=dir_path,
        flat_exists=flat_exists,
        dir_exists=dir_exists,
        has_lock_entry=has_lock_entry,
        flat_dirty=flat_dirty,
        state=state,
        reason=reason,
    )


def classify_migrate(
    project_root: Path | str,
    asset_type: str | None = None,
    name: str | None = None,
) -> list[MigrateRow]:
    """Build a row per migratable asset under *project_root*.

    Iteration source (per locked decision #12): the union of lockfile
    entries (``Lockfile.iter_entries``) and on-disk enumeration via
    :func:`list_canonical_agents` / :func:`list_canonical_commands`,
    deduplicated by ``(asset_type, name)``. This catches both manual
    flat files (disk only → ``skip_manual``) and orphan lockfile
    entries (entry only → ``skip_orphan``).

    Wiki is not consulted (Invariants 1 / 3) — pure filesystem +
    lockfile.

    Race policy (per locked decision #11): the result is a snapshot at
    call time. The CLI executes serially against this snapshot without
    re-reading the disk; mid-batch external mutations are not detected.

    ``asset_type=None`` enumerates ``agents`` and ``commands`` together.
    ``"skills"`` returns an empty list. ``name`` may only be passed
    alongside ``asset_type``; an asset whose name is not present anywhere
    yields an empty list (the CLI surfaces "no asset to migrate").
    """
    project_root_path = Path(project_root).expanduser()
    if not project_root_path.is_dir():
        raise FileNotFoundError(f"project_root {project_root_path} is not a directory")

    if asset_type == "skills":
        return []

    if asset_type is not None and asset_type not in MIGRATABLE_ASSET_TYPES:
        raise ValueError(
            f"invalid asset_type {asset_type!r}: expected one of "
            f"{MIGRATABLE_ASSET_TYPES} or 'skills' (no-op)"
        )
    if name is not None and asset_type is None:
        raise ValueError("name requires asset_type")
    if name is not None:
        validate_name(name, kind="asset name")

    types_to_scan: tuple[str, ...] = (asset_type,) if asset_type else MIGRATABLE_ASSET_TYPES
    lockfile = Lockfile.at(project_root_path)
    doc = lockfile.load(strict=False)

    rows: list[MigrateRow] = []
    for at in types_to_scan:
        if at == "agents":
            disk_pairs = list_canonical_agents(project_root_path)
        else:
            disk_pairs = list_canonical_commands(project_root_path)
        disk_names = {(p.parent.name if layout == "dir" else p.stem) for p, layout in disk_pairs}

        section = doc.get(at)
        lock_names: set[str] = set()
        if isinstance(section, dict):
            lock_names = {n for n, v in section.items() if isinstance(v, dict)}

        if name is not None:
            all_names = [name] if name in (disk_names | lock_names) else []
        else:
            all_names = sorted(disk_names | lock_names)

        for nm in all_names:
            try:
                validate_name(nm, kind=f"{at[:-1]} name")
            except InvalidNameError as exc:
                logger.warning("migrate: skipping invalid %s name %r: %s", at, nm, exc)
                continue

            entry: dict[str, Any] | None = None
            if isinstance(section, dict):
                section_entry = section.get(nm)
                if isinstance(section_entry, dict):
                    entry = section_entry

            row = _classify_row(project_root_path, at, nm, entry)
            if row is not None:
                rows.append(row)

    return rows


def _execute_migrate(row: MigrateRow, *, force: bool) -> Path | None:
    """Rename the flat file into ``<dir>/<asset_filename>`` atomically.

    Sequence:

    1. ``shutil.copy2(flat, flat.bak)`` if the flat file is dirty and
       ``force`` is true (preserves mtime).
    2. ``mkdir(parents=True, exist_ok=True)`` on the destination dir.
    3. ``os.replace(flat, dir/<asset_filename>)`` — the single atomic
       rename. Steps 1 and 2 are preparation and don't break atomicity.

    ``installed_at`` is **not** updated: bytes are byte-identical post
    rename, so the asset's dirty-detection state must remain accurate.
    """
    bak_path: Path | None = None
    if row.flat_dirty and force:
        bak_path = row.flat_path.with_suffix(row.flat_path.suffix + ".bak")
        shutil.copy2(row.flat_path, bak_path)

    target_dir = row.dir_path
    target_dir.mkdir(parents=True, exist_ok=True)
    asset_filename = ASSET_DIR_FILENAMES[row.asset_type]
    target_file = target_dir / asset_filename
    # Race defensive: classify ran with ``dir_exists=False`` but an
    # external mutation between classify and execute could have created
    # ``target_file``. ``os.replace`` would silently overwrite it; abort
    # instead. The race policy (decision #11) accepts this gap as
    # batch-level, not per-asset — surfacing it as an error keeps the
    # other rows in the batch isolated and lets the user re-run.
    if target_file.exists():
        raise OSError(f"target {target_file} appeared after classify; refusing to overwrite")
    os.replace(row.flat_path, target_file)
    return bak_path


def _execute_cleanup_flat(row: MigrateRow, *, force: bool) -> Path | None:
    """Remove the flat sibling of an existing dir layout.

    User-edit policy (locked decision #10): when flat is dirty and
    ``force`` is true, the flat content is snapshotted to a ``.bak``
    sibling before deletion. The dir layout is **not modified** — it
    carries the canonical (wiki) bytes per PR-C policy. Users who want
    to merge the flat edit into the dir layout review the ``.bak`` and
    apply the change manually.
    """
    bak_path: Path | None = None
    if row.flat_dirty and force:
        bak_path = row.flat_path.with_suffix(row.flat_path.suffix + ".bak")
        shutil.copy2(row.flat_path, bak_path)
    row.flat_path.unlink()
    return bak_path


def migrate_one(
    project_root: Path | str,
    row: MigrateRow,
    *,
    force: bool,
) -> MigrateResult:
    """Execute a single row's migration plan.

    No-op for ``noop`` / ``skip_manual`` / ``skip_orphan``. Active
    states are ``migrate`` and ``cleanup_flat``; ``refuse_dirty`` is
    promoted to one of those two when ``force`` is true (the choice
    follows ``dir_exists``).

    Boundary self-validation (per
    ``feedback_public_api_ship_time_validation``): re-validates the
    row's name and confirms the derived destination paths fall inside
    ``<project_root>/.memtomem/<type>/``. Click already gates these at
    the CLI; this defends future callers (MCP, web routes, tests) that
    bypass Click.
    """
    project_root_path = Path(project_root).expanduser().resolve()
    if row.asset_type not in MIGRATABLE_ASSET_TYPES:
        return MigrateResult(row=row, bak_path=None, ok=True, error=None)
    validate_name(row.name, kind=f"{row.asset_type[:-1]} name")

    install_root = (project_root_path / _CANONICAL_ROOTS[row.asset_type]).resolve()
    if not (
        _is_within(row.flat_path.resolve(), install_root)
        and _is_within(row.dir_path.resolve(), install_root)
    ):
        return MigrateResult(
            row=row,
            bak_path=None,
            ok=False,
            error=f"path escapes install root: {row.flat_path} / {row.dir_path}",
        )

    bak_path: Path | None = None
    try:
        # ADR-0030 §6: hold the name-keyed canonical lock across every mutating
        # state — flat→dir replace, cleanup_flat unlink, .bak — so a concurrent
        # Pull / transfer / version op on this artifact serializes with the
        # layout change instead of racing it (the layout-independent identity is
        # why flat ``<name>.md`` and dir ``<name>/`` share one lock).
        with canonical_sidecar_lock(install_root, row.name):
            # Reclassify UNDER the lock. ``row.state`` was decided from
            # ``classify_migrate``'s call-time snapshot (its documented race
            # policy); a concurrent transfer / migrate / CRUD can change the
            # on-disk layout in the gap — e.g. a ``cleanup_flat`` whose dir was
            # moved away by a transfer would otherwise delete the now-only flat
            # canonical. Re-derive from disk and abort if the plan no longer
            # holds, rather than executing a stale decision.
            _fresh_entry = Lockfile.at(project_root_path).read_entry(row.asset_type, row.name)
            fresh = _classify_row(project_root_path, row.asset_type, row.name, _fresh_entry)
            if fresh is None or fresh.state != row.state:
                now = fresh.state if fresh is not None else "gone"
                return MigrateResult(
                    row=row,
                    bak_path=None,
                    ok=False,
                    error=f"artifact changed under lock (was {row.state}, now {now}); re-run migrate",
                )
            row = fresh  # execute against the freshly-verified on-disk state
            if row.state == "migrate":
                if row.flat_dirty and not force:
                    return MigrateResult(
                        row=row,
                        bak_path=None,
                        ok=False,
                        error="dirty flat requires --force",
                    )
                bak_path = _execute_migrate(row, force=force)
            elif row.state == "cleanup_flat":
                if row.flat_dirty and not force:
                    return MigrateResult(
                        row=row,
                        bak_path=None,
                        ok=False,
                        error="dirty flat requires --force",
                    )
                bak_path = _execute_cleanup_flat(row, force=force)
            elif row.state == "refuse_dirty":
                if not force:
                    return MigrateResult(
                        row=row,
                        bak_path=None,
                        ok=False,
                        error="dirty flat requires --force",
                    )
                if row.dir_exists:
                    bak_path = _execute_cleanup_flat(row, force=True)
                else:
                    bak_path = _execute_migrate(row, force=True)
            # noop / skip_manual / skip_orphan → no writes
    except OSError as exc:
        return MigrateResult(row=row, bak_path=None, ok=False, error=str(exc))

    return MigrateResult(row=row, bak_path=bak_path, ok=True, error=None)


def _is_within(path: Path, root: Path) -> bool:
    """Return True if *path* equals *root* or is a descendant of it."""
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


# ── ADR-0022 rank 6: adopt an unmanaged flat canonical into dir layout ──


def adopt_flat_to_dir(asset_type: str, flat_path: Path, dir_path: Path) -> Path:
    """Convert a single unmanaged flat-layout canonical to directory layout.

    The version store (ADR-0022) needs a per-artifact directory
    (``<type>/<name>/``) to hold ``versions/`` + ``versions.json``. A flat
    canonical (``<type>/<name>.md``) has no such home, so versioning is
    unavailable for it (invariant 3).

    Why this is NOT :func:`classify_migrate` / :func:`migrate_one`: those
    deliberately classify a flat file with **no lockfile entry** as
    ``skip_manual`` ("resolve manually") because it sits outside the wiki
    install/upgrade lifecycle and could be a user's hand-dropped file the
    install machinery must not silently consume. But a web-created (UI
    ``create``) canonical is a legitimate artifact that merely lacks install
    provenance — it was permanently locked out of versioning because the
    ``migrate_required`` hint pointed at ``mm context migrate``, which
    provably skips it. This function is the explicit, deliberate adopt path
    that escape hatch needs: it converts **any** flat canonical regardless of
    lockfile status, since the conversion is a single byte-identical
    ``os.replace`` and install provenance is irrelevant to giving the file a
    directory home.

    Because the bytes are unchanged and stay in the same scope, no privacy
    re-scan is needed here — a labeled ``mm context sync`` still re-scans the
    frozen ``versions/vN.md`` at deploy time (ADR-0022 Gate A), so the trust
    boundary is unaffected.

    Args:
        asset_type: ``"agents"`` or ``"commands"`` (selects the manifest name).
        flat_path: the flat canonical (``<root>/<name>.md``); must be a file.
        dir_path: the target per-artifact directory (``<root>/<name>``).

    Returns:
        The new working-canonical path (``dir_path/<manifest>``).

    Raises:
        ValueError: unknown ``asset_type``.
        FileNotFoundError: ``flat_path`` is not a file (nothing to adopt).
        FileExistsError: the dir-layout manifest already exists — a flat+dir
            collision with user-edit semantics this focused path must not
            silently resolve; ``mm context migrate`` handles that case.
        OSError: ``flat_path`` / ``dir_path`` escape the shared canonical
            root, or the rename fails.
    """
    if asset_type not in MIGRATABLE_ASSET_TYPES:
        raise ValueError(
            f"adopt_flat_to_dir: unsupported asset_type {asset_type!r} "
            f"(expected one of {MIGRATABLE_ASSET_TYPES})"
        )
    flat_path = Path(flat_path)
    dir_path = Path(dir_path)
    if not flat_path.is_file():
        raise FileNotFoundError(f"no flat canonical to adopt at {flat_path}")

    target_file = dir_path / ASSET_DIR_FILENAMES[asset_type]

    # Defense-in-depth path guard: ``dir_path`` must resolve under the flat
    # file's own canonical ``<type>`` root, so a traversal-bearing ``name``
    # (e.g. ``../evil``) can never rename outside the store. This is a backstop
    # for future callers — the sole caller already runs ``name`` through
    # ``validate_name`` — so, unlike :func:`migrate_one`, the root is derived
    # from ``flat_path`` rather than an independently resolved install root.
    canonical_root = flat_path.parent.resolve()
    if not (
        _is_within(flat_path.resolve(), canonical_root)
        and _is_within(dir_path.resolve(), canonical_root)
    ):
        raise OSError(f"path escapes canonical root: {flat_path} / {dir_path}")

    # ADR-0030 §6: hold the name-keyed canonical lock across the
    # collision-check + replace so a concurrent Pull / migrate / version op on
    # this artifact serializes with the adopt (all three enable surfaces —
    # web/CLI/MCP — reach the adopt through here). ``canonical_root`` is already
    # ``.resolve()``d above, matching every other lock caller.
    with canonical_sidecar_lock(canonical_root, flat_path.stem):
        if target_file.exists():
            # A dir-layout manifest already sits alongside the flat file — that
            # is the flat+dir collision (``cleanup_flat`` / ``refuse_dirty``),
            # which has user-edit semantics. Refuse; ``mm context migrate`` owns
            # that path.
            raise FileExistsError(
                f"directory layout already exists at {target_file}; "
                f"resolve the flat+dir collision with `mm context migrate` first"
            )

        dir_path.mkdir(parents=True, exist_ok=True)
        os.replace(flat_path, target_file)
    return target_file


# ── ADR-0011 PR-E4: scope-tier migration ──────────────────────────────


SCOPE_MIGRATABLE_KINDS: tuple[ArtifactKind, ...] = ("agents", "commands", "skills")
"""Artifact kinds supported by :func:`migrate_scope`.

Memory tier moves stay in :func:`memtomem.cli.context_cmd._memory_migrate_run`
(chunk-id-stable single-DB rename). The CLI's ``mm context migrate
memory <src> --from --to`` wires that call through; it does not enter
this module.
"""


_DIR_MANIFEST: dict[str, str] = {
    "agents": AGENT_DIR_FILENAME,
    "commands": COMMAND_DIR_FILENAME,
    "skills": SKILL_MANIFEST,
}


@dataclass(frozen=True)
class MigrateScopeResult:
    """Outcome of one scope-tier migration plan or apply.

    Set ``moved=False`` for dry-run results (preview only). Fatal
    failures raise :class:`click.ClickException` rather than producing
    a result — the absence of an ``error`` field is deliberate
    (Codex review #4 fold: dataclass vs raise hybrid is harder to
    reason about than "raise on fail, return on success").
    """

    kind: ArtifactKind
    name: str
    from_scope: TargetScope
    to_scope: TargetScope
    src_path: Path
    dst_path: Path
    layout: Literal["dir", "flat"]
    moved: bool
    fanout_cleaned: list[Path] = field(default_factory=list)
    # Diverged-target snapshots taken before removal (apply only) — see
    # ``_backup_fanout_target``. Independent of ``fanout_cleaned``: a
    # snapshot whose target then failed to delete still appears here.
    fanout_backed_up: list[Path] = field(default_factory=list)
    # Dry-run only (#1247 id 6): the runtime fan-out targets that exist
    # now and would be removed by an apply — previously the deletion half
    # of the move was invisible until after the fact.
    fanout_planned: list[Path] = field(default_factory=list)


@contextmanager
def _acquire_pair_lock(
    path_a: Path, path_b: Path, *, timeout: float | None = None
) -> Iterator[None]:
    """Acquire two sidecar locks in deterministic sorted order.

    Inverse migrations running concurrently (A: foo user→project_shared,
    B: foo project_shared→user) would deadlock if each side acquired its
    src lock first and dst lock second. Sorting by ``str(lock_path)``
    forces every caller to take the same global order, eliminating the
    cycle.

    The pair is always two locks; if both arguments resolve to the same
    sidecar (defensive — only happens when src and dst are the same file,
    which :func:`memtomem.context.transfer.transfer_artifact` rejects
    upstream) the second lock is skipped to avoid re-entrancy issues with
    portalocker on platforms where ``LOCK_EX`` does not nest.

    Cross-project note (ADR-0023): the two paths may live under two
    different project roots. ``sorted(key=str)`` over absolute lock
    paths is a total order there too, so every process — whatever pair
    of roots it works across — still acquires in one global sequence.

    ``timeout`` is a WHOLE-CALL acquisition budget shared across both
    locks (a monotonic deadline; the second acquisition gets whatever
    the first left over), not a per-lock allowance — a caller bounding
    its worst-case wait at N seconds must not discover it can stall for
    2N. ``None`` (default) blocks indefinitely, the historical behavior
    every CLI/MCP surface keeps. On expiry the underlying
    :func:`memtomem.context._atomic._file_lock` raises ``TimeoutError``
    having acquired nothing (the first lock, if already held, is
    released by its own context manager on unwind), so a timed-out
    caller has committed no filesystem change. Added for the web
    transfer route (A-5 #1276), whose un-cancellable worker thread must not
    park on a lock past the route's ``asyncio.timeout`` window — the #1145
    shape ``_SETTINGS_LOCK_BUDGET_S`` / ``_SKILLS_LOCK_BUDGET_S`` bound for
    their engines. Bounding the wait is all it does; what stops a worker
    writing behind a caller that already gave up is a cooperative abort
    (``context/_abandon.py``, #2247), which this path has not adopted.
    """
    lock_a = _lock_path_for(path_a)
    lock_b = _lock_path_for(path_b)
    deadline = None if timeout is None else time.monotonic() + timeout

    def _remaining() -> float | None:
        # 0.0 (deadline already spent) still attempts each lock once
        # non-blocking before raising — _file_lock's poll loop fails fast.
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    ordered = sorted([lock_a, lock_b], key=str)
    if ordered[0] == ordered[1]:
        with _file_lock(ordered[0], timeout=_remaining()):
            yield
        return
    # ``with A, B`` enters A before evaluating B, so the second
    # ``_remaining()`` reads the deadline AFTER the first wait finished.
    with _file_lock(ordered[0], timeout=_remaining()), _file_lock(ordered[1], timeout=_remaining()):
        yield


def transfer_staging_path(parent: Path, name_hint: str) -> Path:
    """Name one internal transfer entry under *parent*.

    The one place the transfer staging grammar is spelled, shared by
    :func:`_stage_move` (move) and
    :func:`memtomem.context.transfer._stage_copy` (copy) so the two cannot
    drift. Since #2313 it also names the EXDEV holding entry, which lives in
    the SOURCE store rather than the destination one — the grammar is about
    what the entry IS (ours, transient, never reaped), not about which side of
    the transfer it sits on. The shape is
    ``.migrate-<name>-<decimal pid>-<8 lowercase hex>.tmp``, which is exactly
    what :func:`~memtomem.context._names.is_internal_artifact_dir` matches —
    so a leftover from a crash between stage and promote is hidden from every
    predicate-aware discovery walk instead of being enumerated as a canonical
    artifact (#2304), on whichever side of the transfer it was left.

    The width stays at ``token_hex(4)`` and the predicate was taught this
    kind's width instead. Narrowing it to the six hex the other kinds use would
    have cut collision entropy from 32 bits to 24, and it would still have left
    every eight-hex leftover already on disk from a released version
    unclassified — which is most of what #2304 is about. A
    construction↔predicate parity test pins the generated name.

    Callers claim this name EXCLUSIVELY and never clear a collider
    (:func:`_claim_transfer_staging`, #2309), so the entropy now bounds how
    often a transfer fails closed on someone else's leftover rather than how
    often one gets destroyed.
    """
    return parent / f".migrate-{name_hint}-{os.getpid()}-{secrets.token_hex(4)}.tmp"


class TransferStagingBusyError(OSError):
    """Two staging names in a row were already occupied (#2309).

    Classified by PROVENANCE, not by errno: raised only by
    :func:`_claim_transfer_staging`, and only for a failed exclusive CLAIM.
    An ``EEXIST`` raised later, while filling an entry this process already
    owns, is a different failure with a different cause and must not be
    reported as a name collision.

    Constructed with a single argument so ``str(exc)`` is the remediation
    sentence rather than the ``[Errno 17] …: '<path>'`` form a three-argument
    ``OSError`` renders — the CLI prints that string as a one-line error.
    ``errno`` is therefore ``None``, which also keeps it clear of
    :func:`_stage_move`'s ``EXDEV`` test.
    """


def _claim_hit_an_occupied_name(staging: Path) -> bool:
    """True when a failed claim failed because that staging name is taken.

    **Decided by looking, never by the errno.** Every candidate errno is
    overloaded, and differently per platform, so a set of them cannot answer
    this question:

    - ``ENOTDIR`` means both "the destination holds a non-directory while the
      source is a directory" (occupied) and "a component of a path is not a
      directory" (a broken source — nothing to do with our name);
    - Windows reports ``ENOENT`` where POSIX reports ``ENOTDIR`` for that same
      broken component;
    - Windows reports ``EACCES`` for the ``O_EXCL`` open of a name held by a
      DIRECTORY, and also for a genuine permission or sharing failure.

    Presence settles all of it: if the name is there, the claim lost a race for
    it and a fresh suffix is the right answer; if it is not, whatever went
    wrong was not about the name and the original error must propagate. This is
    the shape ``bundle._collides`` already uses for the destination identity.

    ``lexists``, not ``exists``: a dangling symlink occupies the name just as
    firmly as a directory does, and answers False to the latter.

    Probed only after a claim has already failed, so a transfer that stages
    cleanly pays nothing. Both outcomes are safe — nothing is removed on either
    branch — and the probe can only ever turn a wrong "busy" back into the
    error that actually happened.
    """
    return os.path.lexists(staging)


#: A staging entry's filesystem identity: ``(st_dev, st_ino)`` off an ``lstat``.
StagingIdentity = tuple[int, int]


class StagingIdentityLostError(OSError):
    """The staging pathname no longer names the object we claimed (#2314).

    Raised only by :meth:`StagingClaim.assert_still_ours`, immediately before
    an operation that would CONSUME the entry (promote, rename-back, hard-link
    promote). It means the pathname was re-pointed at something we did not
    create, so consuming it would move a stranger's bytes into a canonical
    name — bytes Gate A never scanned.

    A separate class from :class:`TransferStagingBusyError` because the
    remediation differs: a busy name is retried with a fresh suffix, while a
    lost identity is an unexplained mutation of a private path and stops the
    operation.
    """


@dataclass(slots=True)
class StagingClaim:
    """The staging entry an exclusive claim created, carried as an OBJECT.

    A successful claim proves we owned a *pathname* at that instant, and
    nothing more (#2314). Every later step that removes or consumes staging
    used to reach for the pathname again, so an entry renamed aside and
    replaced between the claim and the cleanup meant we deleted — or
    promoted — somebody else's object. Recording ``(st_dev, st_ino)`` at claim
    time lets those steps ask "is this still the thing I made?" first.

    **A narrowing, not a close.** The verifying ``lstat`` and the
    ``rmtree`` / ``unlink`` / ``rename`` that follows are two syscalls, so a
    replacement landing between them is still consumed. Closing that would
    need ``unlinkat`` / ``renameat`` against a claim-time directory fd, which
    has no Windows spelling and no ``shutil.rmtree`` support. The residual
    windows are named in the module docs rather than implied away: claim →
    capture, the fill itself (``copytree`` populates whatever the name points
    at), and verify → act.

    ``identity`` is ``None`` only when the ``lstat`` right after our own
    successful claim failed — an anomaly, since the entry provably existed a
    syscall earlier. Removal and consumption both refuse on that, because
    "cannot establish ownership" is indistinguishable from the race being
    defended against, and this package's standing asymmetry is that a leaked
    staging tree is cheap while a destroyed canonical is not
    (``_names.REAPABLE_INTERNAL_ARTIFACT_KINDS``).

    ``identity`` is likewise ``None`` on a filesystem that reports
    ``st_ino == 0`` for everything (POSIX requires real inode numbers; some
    Windows network shares do not supply a file index). Comparing zeros would
    pass for any replacement while reading like a guarantee — a check that
    cannot fail is worse than no check, because the next reader believes it.
    The cost is stated plainly: a transfer staged on such a filesystem refuses
    to promote instead of promoting something it cannot vouch for, and
    :func:`_staging_identity` logs why.
    """

    path: Path
    identity: StagingIdentity | None

    @classmethod
    def capture(cls, path: Path) -> "StagingClaim":
        """Record the identity of the entry a claim just created at *path*."""
        return cls(path=path, identity=_staging_identity(path))

    def adopt(self, placed: StagingIdentity) -> None:
        """Record an identity we KNOW we placed at :attr:`path`.

        Exactly one in-tree writer replaces the staging entry itself:
        ``transfer._rewrite_staged_manifest_name`` writes through
        ``atomic_write_bytes`` (``mkstemp`` + ``os.replace``), and for a FLAT
        artifact the manifest IS the staging entry — so a ``--as`` rename
        legitimately swaps the inode. Leaving the old identity there would make
        the later cleanup read our own rewrite as a stranger's entry and
        preserve it, which is the guaranteed leak #2314 argues against.

        *placed* must come from the writer's own descriptor, never from an
        ``lstat`` of :attr:`path` afterwards. Re-reading the pathname to
        "refresh" would ADOPT whatever occupies it — so an entry a usurper put
        there before the rewrite would be rewritten, blessed as ours, and
        promoted onto the canonical name. That is a worse bug than the one
        this class exists to close, and the reason the obligation is expressed
        as "carry the number you placed" rather than "look again".
        """
        self.identity = placed

    def still_ours(self) -> bool:
        """Whether the pathname still names the object this claim created."""
        if self.identity is None:
            return False
        return _staging_identity(self.path) == self.identity

    def assert_still_ours(self, action: str) -> None:
        """Refuse *action* when the pathname now names something else.

        *action* is a short verb phrase and must not carry a path: both wires
        that render this (``error_redact`` for MCP, the web route's twin)
        hard-truncate an engine reason at 200 characters, so a remediation
        written after the paths reaches a remote caller as a promise with no
        instruction attached. Instruction FIRST, paths LAST — the same shape
        :class:`TransferStagingBusyError` uses, pinned by a surface test on
        each wire.

        An ABSENT entry is not a mismatch and is deliberately allowed through:
        there is nothing there to consume, and the operation's own ``ENOENT``
        is the honest error for it — for a move, "staging vanished" is a
        different emergency from "something is in the way", and only the
        syscall can tell the caller which one happened (the classification
        :func:`~memtomem.context._atomic.rename_refused_by_occupant` performs).
        The check fires only for the state it exists to catch: an entry that IS
        there and is not ours.
        """
        if not os.path.lexists(self.path) or self.still_ours():
            return
        if self.identity is None:
            reason = (
                "transfer staging cannot be verified: this filesystem does not "
                "report file identity, so nothing was promoted and nothing was "
                "removed. Stage on a filesystem that reports it, then retry."
            )
        else:
            reason = (
                "transfer staging was replaced out of band: it no longer names "
                "the entry this transfer created, so nothing was promoted and "
                "nothing was removed. Inspect it by hand, then retry."
            )
        raise StagingIdentityLostError(f"{reason} Refused: {action}. Staging: {self.path}")


def _staging_identity(path: Path) -> StagingIdentity | None:
    """``(st_dev, st_ino)`` for *path*, or ``None`` when it cannot be trusted.

    ``lstat``, never ``stat``: staging can BE a symlink
    (:func:`_stage_copy_into` preserves a link source as a link), and
    following it would compare the identity of the link's target — which some
    other writer owns — instead of the entry we created.

    ``st_ino == 0`` is ``None``, not an identity. POSIX requires real inode
    numbers and Windows supplies a file index on NTFS and ReFS, but a
    filesystem that cannot answer reports zero for EVERY entry — and a
    comparison of zeros is vacuously true, so keeping it would hand back a
    check that passes for any replacement while reading like a guarantee. An
    unanswerable question must not be answered "yes": callers treat ``None``
    as "not provably ours" and refuse to remove or consume, which is the same
    asymmetry the rest of this module runs on.
    """
    try:
        info = path.lstat()
    except OSError as exc:
        # Distinct from the zero-inode case below, and logged as such: this is
        # a probe that FAILED (a permission problem, a vanished parent), not a
        # platform that cannot answer. Telling an operator their filesystem
        # lacks file identity when the real event was an EACCES sends them to
        # fix the wrong thing.
        logger.warning("could not read the identity of staging %s: %s", path, exc)
        return None
    if info.st_ino == 0:
        logger.warning(
            "filesystem holding %s does not report file identity (st_ino is 0), "
            "so this transfer cannot prove a staging entry is still its own; it "
            "will refuse to promote or remove it rather than act on the name. "
            "Stage on a filesystem that reports file identity.",
            path,
        )
        return None
    return (info.st_dev, info.st_ino)


def _remove_staging_entry(staging: Path) -> None:
    """Best-effort removal of a staging entry whose bytes are safe elsewhere.

    The removal ladder ONLY — the ownership question is the caller's, and
    :func:`_discard_claimed_staging` is the answer for anyone holding a claim.

    The symlink test comes first, and both other tests are reached only when
    the entry is not a link: ``exists()`` and ``is_dir()`` follow links, so a
    dangling staging link answers False to the first (leaked, never removed)
    and a link to a directory answers True to the second (``rmtree`` refuses a
    link, also a leak). Staging can be a link since :func:`_stage_copy_into`
    preserves a symlink source as a link rather than dereferencing it.
    """
    if staging.is_symlink():
        with contextlib.suppress(OSError):
            staging.unlink()
        return
    if staging.exists():
        if staging.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
        else:
            with contextlib.suppress(OSError):
                staging.unlink()


def _discard_claimed_staging(claim: StagingClaim) -> None:
    """Remove the entry *claim* created — or nothing at all (#2314).

    The identity gate is the whole point: a cleanup that reaches for the
    pathname deletes whatever is sitting there, and "whatever is sitting
    there" is only our own entry until someone renames ours aside and drops
    their own in its place. On a mismatch (or an unreadable identity) this
    removes NOTHING and logs a warning naming the path, because the leftover
    it declines to touch is now invisible to discovery and reaped by nobody.

    An entry that is simply GONE is not a mismatch: the rename-back and the
    promote both consume staging on success, and their callers still run the
    cleanup arm afterwards. That case returns silently — there is nothing to
    remove and nothing to warn about — so the warning below means "something
    is there and it is not ours", which is the only state an operator has to
    act on.

    Callers that did NOT create the entry — ``bundle._reap_own_staging``
    sweeping a previous run's leftovers — have no claim to compare against and
    use :func:`_remove_staging_entry` directly.
    """
    if not os.path.lexists(claim.path):
        return
    if not claim.still_ours():
        logger.warning(
            "leaving staging %s in place: it no longer names the entry this "
            "transfer created (identity changed out of band), so removing it "
            "would delete an object we did not make. Inspect it by hand.",
            claim.path,
        )
        return
    _remove_staging_entry(claim.path)


def _claim_transfer_staging(
    parent: Path,
    name_hint: str,
    claim: Callable[[Path], None],
) -> StagingClaim:
    """Claim a fresh transfer entry name exclusively, or fail closed (#2309).

    Used for the destination-store staging entry in both modes and, since
    #2313, for the source-store holding entry an EXDEV move parks its source
    in — the same exclusivity question either way. Both come back as a
    :class:`StagingClaim`, because both are later CONSUMED by name (promoted,
    renamed back, removed) and neither may act on a pathname it no longer owns
    (#2314).

    *claim* must be an exclusive-create and NOTHING else — a no-replace
    rename, ``mkdir(exist_ok=False)``, :func:`_create_exclusive_file`,
    ``os.symlink``. Whether a failure means "the name was taken" is settled by
    :func:`_claim_hit_an_occupied_name`, which looks rather than reading the
    errno; any other failure propagates unchanged, so a missing source or a
    permission problem is never mistaken for a busy name.

    Filling the claimed entry happens in the CALLER, after this returns. That
    split is the point: an ``EEXIST`` raised deeper inside a tree copy is not a
    collision on our own name, and retrying it against a fresh name would be
    nonsense. Keeping the claim alone inside this helper is what lets the
    typed error below mean "the name was taken" and nothing else.

    **Nothing here removes anything.** The predecessor cleared a colliding
    entry on the theory that a name carrying our pid and eight random hex could
    only be our own crashed leftover. The first half held; the second did not.
    :func:`_stage_move` renames the source into staging on the same filesystem,
    so between that rename and the promote the staging tree IS the artifact —
    the source is gone from disk. Clearing a collider to make room could
    therefore delete the only surviving copy of somebody's artifact. That is
    the same asymmetry ``_names`` encodes by classifying ``.migrate-*`` as ours
    while keeping it out of ``REAPABLE_INTERNAL_ARTIFACT_KINDS``: a leaked
    directory is cheap, a deleted canonical is not.

    One retry with a fresh suffix, then :class:`TransferStagingBusyError`,
    naming both occupied paths so the operator can decide what they are. Matches
    the receipt transport's staging claim
    (``bundle.receive_artifact_bundle``). Retrying forever would spin against a
    permanent obstruction — two independent collisions on 32 bits of entropy
    means something other than chance.

    Returns a :class:`StagingClaim`, not a path: this is the one place a
    staging entry is created, so capturing its identity here is what makes
    "you cannot hold a staging path without knowing which object it was"
    structural rather than a rule every call site has to remember (#2314).
    """
    first = transfer_staging_path(parent, name_hint)
    try:
        claim(first)
    except OSError:
        if not _claim_hit_an_occupied_name(first):
            raise
    else:
        return StagingClaim.capture(first)

    second = transfer_staging_path(parent, name_hint)
    try:
        claim(second)
    except OSError as exc:
        if not _claim_hit_an_occupied_name(second):
            raise
        # dict.fromkeys, not a set: order matters for the reader, and the two
        # names coincide whenever a caller has pinned the suffix (the forced
        # -collision tests do exactly that), where naming one path twice
        # would read as a bug in the message rather than in the world.
        occupied = " and ".join(dict.fromkeys((str(first), str(second))))
        # Instruction FIRST, inside 188 characters, paths LAST. Both wires
        # (``error_redact`` for MCP, the web route's twin) hard-truncate an
        # engine reason to 200 characters, so a remediation written after the
        # paths reaches a remote caller as a promise with no instruction
        # attached. The CLI prints this string whole, which is where the paths
        # are worth having; on the wire they are what gets cut. A surface test
        # on each wire pins that the instruction survives.
        raise TransferStagingBusyError(
            "transfer staging is blocked: a name it needs is already taken and "
            "was NOT removed, because that entry can be an interrupted move's "
            "only copy of an artifact. Inspect it by hand, then retry. "
            f"Occupied: {occupied}"
        ) from exc
    return StagingClaim.capture(second)


def _link_target_is_directory(link: Path) -> bool:
    """Whether *link* is a DIRECTORY symlink, without resolving it.

    Only Windows distinguishes the two kinds, and it needs the answer up front:
    :func:`os.symlink` there takes ``target_is_directory`` and cannot infer it
    when the target is missing, so recreating a dangling link without this
    makes a file link where the source had a directory link. POSIX has one kind
    of symlink and ignores the flag entirely.

    Read off the link itself (``lstat``, never a resolve) so a dangling link
    still answers, which is the only case where the answer is not inferable.
    """
    if os.name != "nt":
        return False
    try:
        attrs = link.lstat().st_file_attributes
    except (OSError, AttributeError):
        # Unknowable — fall back to the target if it happens to resolve.
        return link.is_dir()
    return bool(attrs & stat.FILE_ATTRIBUTE_DIRECTORY)


def _create_exclusive_file(path: Path) -> None:
    """Create an empty regular file at *path*, or fail because it is taken.

    ``O_CREAT | O_EXCL`` is the file-shaped claim, and on POSIX that is the
    whole story: the pair is specified to fail with ``EEXIST`` when the final
    component is a symlink, dangling or not.

    **Windows does not honour that**, which is what makes the verification
    below load-bearing rather than defensive. Its ``open`` follows a reparse
    point at the final component, so a dangling symlink sitting on our staging
    name lets the create SUCCEED — against the link's missing target. The
    directory entry then still belongs to whoever made that link, while we
    believe we own it: the copy would write through the link to a path someone
    else chose, the promote would move THEIR link into the canonical
    destination, and a cleanup on failure would unlink an entry we never
    created. That is precisely the guarantee #2309 exists to establish, lost on
    one platform.

    So the claim is only complete once we have looked at what we claimed. If it
    is a symlink, we followed one: the name is not ours. The file we created at
    the link's target is ours, though — ``O_EXCL`` proves it did not exist a
    moment ago — so removing it puts the filesystem back, and the raised
    ``FileExistsError`` sends the caller to a fresh suffix.

    ``O_BINARY`` is absent off Windows and suppresses newline translation on
    it; the file is empty either way, but the flag keeps the idiom uniform with
    the rest of the package.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    os.close(os.open(path, flags, 0o600))
    if not path.is_symlink():
        return
    created = os.readlink(path)
    with contextlib.suppress(OSError):
        os.unlink(os.path.join(path.parent, created))
    raise FileExistsError(
        errno.EEXIST,
        "staging name is held by a symlink the create followed",
        str(path),
    )


def _refuse_junction(path: Path) -> None:
    """Refuse a Windows directory junction rather than reproducing it wrongly.

    A junction is not a symlink, so it would otherwise reach ``copytree``,
    which deliberately recurses INTO one rather than reproducing it — the same
    out-of-tree materialization the no-deref mirror contract exists to prevent
    (``_atomic.copy_tree_atomic``). It cannot be recreated with
    :func:`os.symlink`, so there is nothing to turn it into.

    Lifted out of :func:`_stage_copy_into` so :func:`_stage_move` can ask the
    question about the SOURCE, before the holding rename moves it (#2313).
    Probing after the rename would test the holding entry instead, and a
    refusal that has already moved the artifact is worse than one that has
    not touched it.
    """
    if path.is_junction():
        raise OSError(
            errno.EINVAL,
            "refusing to copy a directory junction: it cannot be reproduced as "
            "a link, and following it would materialize out-of-tree bytes into "
            "the destination store",
            str(path),
        )


def _drop_partial_fill(staging: StagingClaim, src: Path, *, src_claim: StagingClaim | None) -> None:
    """Remove a half-filled staging entry, unless it is the last copy (#2313).

    The fill failed, so *staging* holds an incomplete tree this call created
    and normally owns. Dropping it is right whenever the bytes it was copying
    still exist at *src* — always true for copy mode (*src_claim* is ``None``:
    the canonical artifact was never consumed).

    For an EXDEV move the source is the holding entry this transfer parked,
    and "still there" is settled by IDENTITY, not by presence: a replacement
    occupies that pathname exactly as convincingly as our own entry did, and
    dropping the partial copy on that evidence would complete a loss the
    failure only started. So the partial tree is preserved and named at ERROR
    whenever the holding claim is gone or no longer ours — a recovery copy
    nobody can name is a copy nobody recovers.

    Two questions in order, and both must answer "yes" before anything is
    removed: is dropping this entry safe for the BYTES (above), and is the
    entry still the OBJECT this call created (:func:`_discard_claimed_staging`,
    #2314)? The second is what keeps a cleanup from deleting an entry someone
    replaced our claimed name with — it removes nothing and logs on a mismatch.

    Best-effort removal, as before: this runs while an exception is in flight
    and must not replace it with its own.
    """
    if src_claim is not None and not src_claim.still_ours():
        logger.error(
            "transfer staging: the copy of %s failed and that entry is gone "
            "or is no longer the one this transfer parked; "
            "preserving the partial copy at %s — it is incomplete, but it is "
            "the only thing left holding any of those bytes, so it is kept "
            "for manual recovery rather than removed.",
            src,
            staging.path,
        )
        return
    _discard_claimed_staging(staging)


def _stage_copy_into(
    src: Path,
    dst_parent: Path,
    name_hint: str,
    *,
    src_claim: StagingClaim | None = None,
) -> StagingClaim:
    """Build a staging entry under *dst_parent* from a byte copy of *src*.

    Shared by the :func:`_stage_move` EXDEV fallback and by
    ``transfer._stage_copy``, so copy-mode staging has one implementation. The
    source is never consumed or mutated.

    Links are preserved as links, never dereferenced: the same-FS rename path
    moves a link as a link, and the stdlib copy default would instead
    materialize out-of-tree target bytes into staging — and from there into the
    (possibly git-tracked) destination tier — violating the package's no-deref
    mirror contract (``_atomic.copy_tree_atomic``). Preserving links also makes
    dangling ones non-fatal (#1247 id 7).

    The link test comes FIRST because :meth:`Path.is_dir` follows links: a
    top-level symlink to a directory would otherwise take the tree branch and
    land here as a real directory while the rename path lands it as a link.

    It also tests :meth:`Path.is_junction`, because a Windows directory
    junction is not a symlink and would otherwise reach ``copytree``, which
    deliberately recurses INTO a junction rather than reproducing it — the same
    out-of-tree materialization by another name. A junction cannot be recreated
    with ``os.symlink``, so it is refused rather than silently turned into
    something else.

    Cleanup on a failed fill removes only an entry this call created — the
    claim ran first and succeeded, so the entry is provably ours, and the
    removal re-checks that identity rather than trusting the pathname a second
    time (#2314). A failed claim creates nothing and therefore cleans nothing.

    The fill itself is still addressed by name: ``copytree`` populates, and
    ``copy2`` writes through, whatever the pathname points at when they run.
    An entry replaced inside that window is written to, not deleted, and only
    the identity-checked cleanup declines to compound it. Closing the fill
    window too would need an fd-relative copy tree, which the stdlib does not
    offer — recorded here as a known residual rather than fixed.

    *src_claim* is present when *src* is an entry THIS transfer created and
    parked — the EXDEV move's holding entry — and absent for copy mode, which
    reads the canonical artifact and never consumes it. It answers two
    questions the pathname cannot.

    Before the read: is *src* still the object we parked? Copying without
    asking would let an entry someone put on that name be copied into staging
    and promoted onto a canonical name, which is the #2314 hazard arriving by
    a different door (a read rather than a delete).

    After a failed fill: may the half-filled staging entry be dropped? Copy
    mode always may. The EXDEV path may only while its holding entry is still
    ours — mere presence is not enough, since a replacement occupies the
    pathname just as convincingly as our own entry did, and dropping the
    partial copy on that evidence completes a loss the failure only started.
    When it is not ours, the partial tree is preserved and named instead:
    incomplete, but the last thing on disk carrying any of those bytes.
    """
    if src_claim is not None:
        src_claim.assert_still_ours("copy the parked source into staging")
    _refuse_junction(src)

    if src.is_symlink():
        # The link IS the payload; there is nothing to fill afterwards.
        # ``os.symlink`` refuses an existing name natively — it creates a
        # directory entry rather than opening one, so it does not follow a
        # link already sitting there the way ``open`` does on Windows — which
        # makes it its own exclusive claim.
        target = os.readlink(src)
        target_is_dir = _link_target_is_directory(src)
        claimed = _claim_transfer_staging(
            dst_parent,
            name_hint,
            lambda path: os.symlink(target, path, target_is_directory=target_is_dir),
        )
        with contextlib.suppress(OSError, NotImplementedError):
            shutil.copystat(src, claimed.path, follow_symlinks=False)
        return claimed

    if src.is_dir():
        claimed = _claim_transfer_staging(
            dst_parent, name_hint, lambda path: path.mkdir(exist_ok=False)
        )
        try:
            # ``dirs_exist_ok`` because the claim already created the root —
            # the directory copytree would otherwise refuse is the empty one we
            # just made and own.
            shutil.copytree(src, claimed.path, symlinks=True, dirs_exist_ok=True)
        except BaseException:
            _drop_partial_fill(claimed, src, src_claim=src_claim)
            raise
        return claimed

    claimed = _claim_transfer_staging(dst_parent, name_hint, _create_exclusive_file)
    try:
        # Writes through our own placeholder. ``copy2`` onto an existing
        # DIRECTORY would instead copy INTO it, which is exactly the hazard the
        # old clear-the-collider comment worried about; the claim makes the
        # destination a regular file we created, so that shape cannot arise.
        shutil.copy2(src, claimed.path, follow_symlinks=False)
    except BaseException:
        _drop_partial_fill(claimed, src, src_claim=src_claim)
        raise
    return claimed


class StagedMove(NamedTuple):
    """What :func:`_stage_move` left on disk.

    ``staging`` is the DESTINATION-store entry the caller promotes.
    ``holding`` is the source, renamed aside in the SOURCE store, and is
    ``None`` on the same-filesystem path where the staging entry IS the
    source (#2313). Either way the source is gone from its canonical path by
    the time this returns — the two paths differ in WHERE the pre-move bytes
    live, never in whether the canonical name is free.

    Both are :class:`StagingClaim`, not paths: each is later CONSUMED by name —
    the staging entry is promoted, the holding entry is renamed back or
    removed — and neither may act on a pathname that no longer names what the
    claim created (#2314).

    A tuple rather than a flag: the caller needs the holding path to remove
    it after the promote and to rename it back on failure, and a boolean
    would have to be paired with a path anyway.
    """

    staging: StagingClaim
    holding: StagingClaim | None


class RestoreOutcome(Enum):
    """What :func:`_restore_source` was able to do (#2313 + #2314).

    Three answers, not two. ``REFUSED`` and ``IDENTITY_LOST`` both leave the
    bytes where they are, but they are different events with different
    remediations: a refused rename means something occupies the source name
    and the entry we hold is still ours to move, while a lost identity means
    the entry itself is no longer the object we parked — the artifact is
    somewhere we can no longer name, and the operator has to go looking rather
    than retry. Collapsing them into one boolean is what made the caller
    report a Gate A block for a state where the artifact had gone missing.
    """

    RESTORED = "restored"
    REFUSED = "refused"
    IDENTITY_LOST = "identity_lost"


def _restore_source(entry: StagingClaim, src: Path, *, allow_cross_parent: bool) -> RestoreOutcome:
    """Rename *entry* back onto *src*, and say what happened.

    The one rename-back, shared by :func:`_stage_move`'s own unwind and by
    ``transfer.transfer_artifact``'s rollback ladder, because both are asking
    the identical question: the entry named here holds the pre-move bytes, and
    it may be put back only onto a name nobody else has taken.

    **The messages are cardinality-neutral, deliberately.** This function sees
    one entry; how many copies of the source bytes exist is not a question it
    can answer. The predecessor called *entry* "the ONLY surviving copy",
    which is exact on the same-filesystem path — staging IS the pre-move tree
    — and wrong on the EXDEV one, where the destination-side copy survives on
    purpose (#2313) and the operator was sent to one path while two held the
    bytes. The caller knows whether it is holding a second copy, so the caller
    counts (:func:`~memtomem.context.transfer._log_rollback_survivors`).

    **Nothing here removes anything, on any path.** A refusal returns False
    having touched nothing; the caller decides what to preserve, and every
    caller preserves it. That is the same rule the staging claim follows
    (:func:`_claim_transfer_staging`): a leaked directory is cheap, a deleted
    canonical is not. It is a promise about what THIS function does, not about
    what is left: a concurrent actor can remove *entry* inside the rename
    window, which is why the failure probes it rather than describing it from
    the fact that we created it.

    No-replace, never :func:`os.replace`: an external writer can recreate the
    source path while we are staging, and a replacing rename would delete
    bytes that are not ours to delete. Which message to log is decided by
    LOOKING rather than by the errno — "something is at src" is a claim about
    the world, and the errno spellings for an occupied rename target differ
    per platform. ``lexists`` on both probes, so a dangling symlink counts:
    at *src* it occupies the name, at *entry* it is still the parked
    artifact's only name.

    **Both halves of the message are probed.** The failure says why the
    rename was refused AND where the bytes are, and the second half used to
    be assumed — *entry* is ours, so it must still be there. That is the
    assumption #2327 removed from the rest of this ladder, and the same
    writer that can occupy *src* can remove *entry* inside the same window.
    A path named as a recovery copy has to be a path that exists; when it
    does not, the message says the bytes are gone rather than sending a hand
    recovery after them.

    *allow_cross_parent* is the caller's assertion that the two paths are on
    one filesystem while living in different directories: true for the
    rollback of a same-FS move (staging sits in the destination store, the
    source in another), false for the EXDEV holding entry, which is a sibling
    of the source by construction and therefore keeps the promote-shape guard.

    Takes the CLAIM, because this rename is a CONSUMER: doing it by name would
    move whatever occupies that pathname onto the canonical source path, and if
    someone replaced our entry there, that is their object being published
    under our name while the artifact we parked stays lost under theirs
    (#2314). A lost identity leaves everything where it is, like a refused
    rename, but reports :attr:`RestoreOutcome.IDENTITY_LOST` so the caller can
    say the artifact is unaccounted for instead of blaming whatever failure
    sent it here.
    """
    if os.path.lexists(entry.path) and not entry.still_ours():
        # Only for an entry that IS there and is not ours. An ABSENT one is a
        # different event — nothing was substituted, the bytes simply are not
        # here — and it belongs to the rename below, whose ``ENOENT`` says so
        # in the caller's own vocabulary. Same rule as
        # :meth:`StagingClaim.assert_still_ours`.
        logger.error(
            "transfer rollback: %s no longer names the entry this transfer "
            "created (replaced out of band, or this filesystem cannot report "
            "file identity); neither renaming it back to %s nor removing it. "
            "The source bytes may survive under another name in that store — "
            "manual reconciliation required.",
            entry.path,
            src,
        )
        return RestoreOutcome.IDENTITY_LOST
    try:
        rename_no_replace(entry.path, src, allow_cross_parent=allow_cross_parent)
    except OSError as exc:
        preserved = os.path.lexists(entry.path)
        if os.path.lexists(src):
            if preserved:
                logger.error(
                    "transfer rollback: rename-back refused (%s) — an entry we "
                    "did not create occupies src %s; preserving the pre-move "
                    "bytes at %s — manual reconciliation required.",
                    exc,
                    src,
                    entry.path,
                )
            else:
                logger.error(
                    "transfer rollback: rename-back refused (%s) — an entry we "
                    "did not create occupies src %s, and %s, which held the "
                    "pre-move bytes, is gone too — manual reconciliation "
                    "required.",
                    exc,
                    src,
                    entry.path,
                )
        elif preserved:
            logger.error(
                "transfer rollback: rename-back failed (%s); the pre-move bytes "
                "are preserved at %s — manual recovery required (mv it back to "
                "%s).",
                exc,
                entry.path,
                src,
            )
        else:
            logger.error(
                "transfer rollback: rename-back failed (%s) and %s, which held "
                "the pre-move bytes, is gone too — nothing left to put back at "
                "%s.",
                exc,
                entry.path,
                src,
            )
        return RestoreOutcome.REFUSED
    return RestoreOutcome.RESTORED


def _stage_move(src: Path, dst_parent: Path, name_hint: str) -> StagedMove:
    """Move *src* into a staging entry under *dst_parent*.

    Returns :class:`StagedMove`. The source is consumed by an ATOMIC RENAME on
    both paths, which is the property the caller's cleanup rests on: on the
    same filesystem the rename lands directly in the destination store
    (``holding`` is ``None``); across filesystems the rename can only reach a
    sibling, so the source is first parked in a holding entry beside itself and
    the bytes are copied from THAT into staging (#2313).

    **Why the copy may not read the canonical path.** A cross-filesystem copy
    takes as long as the artifact is large, and an out-of-band writer — an
    editor, a shell, a ``git checkout``, anything that does not take our
    sidecar lock — can rewrite or replace the source inside that window. The
    predecessor copied from the canonical path and the caller then removed
    that path BY NAME after the promote, so such a writer's bytes were
    deleted and the destination held the older snapshot: the newer version
    existed nowhere. Parking the source first means a writer that resolves
    the canonical path finds it free, and whatever it puts there is nobody
    else's to delete.

    The guarantee is about writers that RESOLVE the path — create, replace,
    rename. A writer holding a descriptor opened before the rename still
    writes into the pre-move inode, which a cross-filesystem move can only
    ever snapshot; those bytes are lost when the holding entry is removed,
    exactly as they were when the source was removed by name. Only the
    same-filesystem path preserves them, and only because the inode it
    promotes is the one that descriptor points at.

    The staging and holding names are both claimed EXCLUSIVELY and a collider
    is never removed (:func:`_claim_transfer_staging`); both carry the
    ``.migrate-`` grammar, so a leftover is hidden from every predicate-aware
    walk and reaped by nothing.

    **Crash states.** A crash between the holding rename and the caller's
    post-promote removal leaves a ``.migrate-`` entry in the SOURCE store:
    before the promote it is the only copy of the artifact, after it a stale
    duplicate of the canonical now at the destination. That is the same
    recovery policy the same-filesystem path already has — hidden, never
    reaped, recovered by hand — but not the same set of states: the EXDEV path
    can also be caught with a partially filled staging tree beside a complete
    holding entry, and with both a promoted destination and a stale holding
    entry, neither of which a single atomic rename can produce.

    Cleanup discipline: a failed claim created nothing, so it cleans nothing;
    a failure after the holding rename renames it back, and preserves it
    loudly if that is refused, so the source bytes always exist somewhere this
    function names.
    """
    dst_parent.mkdir(parents=True, exist_ok=True)
    try:
        staging = _claim_transfer_staging(
            dst_parent,
            name_hint,
            lambda path: rename_no_replace(src, path, allow_cross_parent=True),
        )
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            # Non-EXDEV failure (permissions, missing src, a second collision,
            # a filesystem without the primitive) — surface. Nothing was
            # created: the rename is atomic and it did not happen.
            raise
        # EXDEV: src is on another filesystem. Park it beside itself first, so
        # the copy reads an entry only we can name.
        #
        # The junction question is asked HERE, about the source, rather than
        # inside the copy about the holding entry — a refusal that has not
        # moved the artifact is better than one that has.
        _refuse_junction(src)
        holding = _claim_transfer_staging(
            src.parent,
            name_hint,
            # Same parent by construction, so the promote-shape guard stays
            # on: a holding entry that is not a sibling of the source is a
            # bug, not a cross-device move.
            lambda path: rename_no_replace(src, path),
        )
        try:
            return StagedMove(
                # The copy reads the holding entry, which IS the artifact
                # here — the canonical path is already empty — so a failed
                # fill may not assume the source will still be there to
                # rebuild from (#2313).
                _stage_copy_into(holding.path, dst_parent, name_hint, src_claim=holding),
                holding,
            )
        except BaseException:
            # The source is renamed away at this point, so unwinding is this
            # function's job: the caller's rollback ladder never sees a stage
            # that failed. ``_restore_source`` preserves the holding entry and
            # logs when it cannot put it back, so the bytes are never dropped
            # to make a failure tidy.
            outcome = _restore_source(holding, src, allow_cross_parent=False)
            if outcome is RestoreOutcome.IDENTITY_LOST:
                # The parked source is not where we parked it any more, and
                # the canonical name is empty: the artifact is unaccounted
                # for. Replacing the in-flight failure with that fact is the
                # honest report — whatever made the stage fail matters less
                # than an artifact nobody can name (#2314).
                raise MigratePartialError(
                    f"transfer left {src.name} unaccounted for: the move parked "
                    f"the source and that entry was replaced out of band, so it "
                    f"could not be put back. Do NOT retry — look for the "
                    f"artifact in {src.parent} under a name it was renamed to, "
                    f"and restore it to {src} by hand. The entry now at "
                    f"{holding.path} belongs to whoever put it there.",
                    src_path=src,
                    dst_path=dst_parent,
                )
            raise
    return StagedMove(staging, None)


def _promote_move(staging: StagingClaim, dst: Path) -> None:
    """Promote *staging* onto an absent *dst*, or refuse atomically (#2312).

    Takes the CLAIM, not a path, and verifies the pathname still names the
    object we created before consuming it (#2314). Promotion is the one
    by-name consumer whose failure is not a leak: renaming a stranger's entry
    onto the canonical name publishes bytes Gate A scanned on a different
    object. On a mismatch it raises
    :class:`StagingIdentityLostError` and promotes nothing; the caller's
    rollback ladder then re-asks the same question before touching staging
    again. The verify→rename pair is still two syscalls — see
    :class:`StagingClaim` for the residual window this narrows rather than
    closes.

    Pre-condition (pinned by :func:`migrate_scope` Row 15 contract): dst
    must not exist. The refusal is the SYSCALL's, not a check of ours.

    The predecessor read ``dst.exists()`` and then called
    :func:`os.replace`, which loses three destination shapes. A regular
    file or an empty directory created in the window between the two calls
    is silently replaced (POSIX ``rename`` replaces both), and a dangling
    symlink is invisible to :meth:`Path.exists` altogether, so the guard
    never fired and the replace unlinked somebody's link. Only a non-empty
    directory happened to fail, which is why the destination-race coverage
    could not tell a real guard from that one (ADR-0037 §6).

    :func:`rename_no_replace` refuses all three in the kernel, with no
    window to race. Its shared-parent default is kept: a promote moves
    staging onto the canonical name beside it, so a cross-parent call here
    is a caller bug and the early ``EXDEV`` is worth keeping.

    Raises :class:`FileExistsError` on an occupied destination — the
    boundary contract both call sites in :mod:`~memtomem.context.transfer`
    translate into the typed :class:`TransferCollisionError` the surfaces
    declare. Every other ``OSError`` propagates unchanged, including an
    ``ENOTDIR`` that turns out not to be about the destination at all —
    :func:`~memtomem.context._atomic.rename_refused_by_occupant` settles that
    one by looking.
    """
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
    except FileExistsError as exc:
        # ``exist_ok=True`` swallows the error only when what is already
        # there IS a directory; a plain file or a dangling symlink on the
        # store's own path still raises. That must NOT reach the caller as
        # a bare FileExistsError, which is this function's signal for "the
        # destination is taken" — the destination does not even have a
        # directory to live in, and reporting a collision would send the
        # operator looking for an artifact that is not there. ENOTDIR says
        # what is actually wrong, and is not a FileExistsError, so the
        # caller re-raises it instead of translating it.
        raise NotADirectoryError(
            errno.ENOTDIR,
            f"destination parent is not a directory: {dst.parent}",
            str(dst),
        ) from exc
    staging.assert_still_ours("promote onto the canonical name")
    try:
        rename_no_replace(staging.path, dst)
    except OSError as exc:
        if rename_refused_by_occupant(exc, dst):
            raise FileExistsError(f"destination already exists: {dst}") from exc
        raise


# Per-(kind, runtime) file suffix for non-skill fan-out cleanup. Source of
# truth lives next to the generators (``commands._COMMAND_RUNTIME_SUFFIX``
# / ``agents._AGENT_RUNTIME_SUFFIX``), but importing those introduces a
# circular-import risk at module load time and would also re-couple
# migrate.py to internal names. Mirror them verbatim here — a regression
# guard (``test_e4_runtime_suffix_parity_with_generators``) pins the
# two-way agreement so a future runtime addition cannot drift the cleanup
# silently.
_NON_SKILL_FANOUT_SUFFIX: dict[ArtifactKind, dict[str, str]] = {
    "agents": {"claude": ".md", "gemini": ".md", "codex": ".toml", "kimi": ".yaml"},
    "commands": {"claude": ".md", "gemini": ".toml", "codex": ".md"},
}


# Generator registries by artifact kind, for the fan-out divergence check.
# Keyed ``f"{runtime}_{kind}"`` (``claude_agents``, ``gemini_commands``, …)
# — the registries' own ``gen.name`` convention. A missing key means sync
# has no writer for that (kind, runtime) at all (today: codex commands —
# the ``~/.codex/prompts`` table row is a reserved placeholder), so a file
# found there is necessarily foreign.
_FANOUT_GENERATORS: dict[ArtifactKind, dict[str, Any]] = {
    "agents": AGENT_GENERATORS,
    "commands": COMMAND_GENERATORS,
    "skills": SKILL_GENERATORS,
}


def _existing_fanout_targets(
    kind: ArtifactKind,
    name: str,
    scope: TargetScope,
    project_root: Path | None,
) -> list[tuple[str, Path]]:
    """``(runtime, target)`` pairs the fan-out cleanup would act on.

    Shared by the dry-run preview (``MigrateScopeResult.fanout_planned``)
    and the post-move cleanup so the two can never disagree about the
    deletion half of a migrate (#1247 id 6; Codex design review — the
    preview must not list paths apply intentionally leaves alone).

    Walks every known runtime (claude / gemini / codex / kimi). Excluded,
    with a warning where the exclusion is news:

    * tuples :func:`runtime_fanout_root` reports as ``NO_FANOUT``
      (project_local entries, codex commands at project tiers, etc.);
    * unknown (kind, runtime, scope) tuples — table gap; skip rather
      than fail the migration (the table is the contract source of
      truth and a missing tuple should be caught by the unit tests on
      ``_runtime_targets``, not here);
    * targets that don't exist on disk;
    * symlinked targets — never follow / deref / remove one (a symlink
      in a runtime root is user hand-routing, not sync output: the
      generators only ever ``os.replace`` regular files into place);
    * (kind, runtime) pairs with no registered generator — sync can
      never have written the file, so removing it would be pure
      collateral on a hand-authored file. Leave it in place.

    Per-runtime suffix: agents/codex and commands/gemini write
    ``.toml``, the other non-skill pairs write ``.md``. The cleanup
    must use the same suffix the generator used at sync time or the
    stale artifact survives and the runtime can still discover and
    invoke the moved-away command/agent (#895 P2 review #2).
    """
    targets: list[tuple[str, Path]] = []
    generators = _FANOUT_GENERATORS[kind]
    for runtime in ("claude", "gemini", "codex", "kimi"):
        try:
            root = runtime_fanout_root(kind, runtime, scope, project_root)
        except KeyError:
            logger.warning(
                "fanout cleanup: no table entry for (%s, %s, %s); skipping",
                kind,
                runtime,
                scope,
            )
            continue
        if root is None:
            continue
        if kind == "skills":
            target = root / name
            exists = target.is_dir()
        else:
            suffix = _NON_SKILL_FANOUT_SUFFIX[kind].get(runtime, ".md")
            target = root / f"{name}{suffix}"
            exists = target.is_file()
        if target.is_symlink():
            logger.warning(
                "fanout cleanup: %s is a symlink; leaving it in place (never deref)",
                target,
            )
            continue
        if not exists:
            continue
        if generators.get(f"{runtime}_{kind}") is None:
            logger.warning(
                "fanout cleanup: no %s generator for runtime %s — sync never "
                "wrote %s; leaving the foreign file in place",
                kind,
                runtime,
                target,
            )
            continue
        targets.append((runtime, target))
    return targets


def _fanout_target_matches(
    kind: ArtifactKind,
    name: str,
    runtime: str,
    target: Path,
    parsed_item: Any | None,
    dst_path: Path,
    to_scope: TargetScope,
    dst_project_root: Path | None,
) -> bool:
    """True when *target* byte-matches what sync would write for this artifact.

    Reconstructs the expected fan-out content from the canonical **at its
    post-move location** (``dst_path``): the moved bytes are identical to
    what sync last read at the source scope, and per-vendor overrides live
    inside the artifact dir (``<name>/overrides/<vendor>.<ext>``) so they
    moved with it. Override resolution therefore reads the DESTINATION
    project root (ADR-0023 two-root contract — for a same-root move the
    two roots coincide and this is the historical behavior; ``None`` is
    valid for a user-tier destination, where the project root is unused).
    The expected-bytes rule mirrors ``_sync_atomic`` Phase 2 exactly —
    override bytes verbatim when one resolves, else the generator render
    — the same comparison ``diff_agents`` / ``diff_commands`` /
    ``_skill_effective_equal`` already pin.

    Any read failure returns ``False`` — the same "report drift, never
    mask it" posture as diff — so uncertainty routes to the backup path
    rather than a silent delete.
    """
    gen = _FANOUT_GENERATORS[kind][f"{runtime}_{kind}"]
    vendor = GENERATOR_VENDOR.get(gen.name)
    override_bytes: bytes | None = None
    if vendor is not None:
        override_path = _override.resolve(dst_project_root, kind, name, vendor, scope=to_scope)
        if override_path is not None:
            try:
                override_bytes = override_path.read_bytes()
            except OSError:
                return False
    if kind == "skills":
        try:
            return _skill_effective_equal(dst_path, target, override_bytes)
        except OSError:
            return False
    if override_bytes is not None:
        expected = override_bytes
    elif parsed_item is None:
        # Canonical unreadable/unparseable — sync would skip it, so the
        # target's provenance is unknowable. Treat as diverged.
        return False
    else:
        content, _dropped = gen.render(parsed_item)
        expected = content.encode("utf-8")
    try:
        return expected == target.read_bytes()
    except OSError:
        return False


def _backup_fanout_target(target: Path) -> Path | None:
    """Snapshot a diverged runtime fan-out target before removal.

    Files: sibling ``<name>.<ext>.bak`` via ``shutil.copy2`` (mirrors
    ``_execute_cleanup_flat``; an older ``.bak`` is overwritten —
    newest snapshot wins). Suffix-filtered discovery never lists it and
    runtimes don't load ``.bak``.

    Skill dirs: ``<runtime_root>/.bak/<name>/`` via
    ``shutil.copytree(symlinks=True)`` — deliberately NOT a sibling
    ``<name>.bak``: ``validate_name`` accepts dots, so a skill-shaped
    sibling would surface as a phantom "missing canonical" diff row, be
    re-imported into canonical by ``extract_skills_to_canonical``
    (#1229's round-trip failure mode), and be discoverable by the real
    runtimes. One level down under a manifest-less ``.bak/`` parent,
    every single-level ``<root>/*/SKILL.md`` discovery (ours and the
    runtimes') stays blind to it. Internal-pattern names
    (``.old-…-<pid>-<hex>.tmp``) are off the table — the sync-time
    reaper (``skills._recover_and_reap_internal_dirs``) deletes those.

    Backups are never auto-deleted by memtomem; the warning at the call
    site and the CLI/MCP move summary name them for manual review.

    Returns the backup path, or ``None`` (with a loud warning) when the
    snapshot could not be taken — the caller must then KEEP the target,
    because deleting without a backup is exactly the loss this guard
    exists to prevent.
    """
    try:
        if target.is_dir():
            bak = target.parent / ".bak" / target.name
            if bak.is_dir():
                shutil.rmtree(bak)
            elif bak.exists():
                bak.unlink()
            bak.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(target, bak, symlinks=True)
        else:
            bak = target.with_name(target.name + ".bak")
            shutil.copy2(target, bak)
    except (OSError, shutil.Error) as exc:
        logger.warning(
            "fanout cleanup: failed to snapshot diverged %s (%s); leaving the target in place",
            target,
            exc,
        )
        return None
    return bak


def _remove_runtime_fanout_for(
    kind: ArtifactKind,
    name: str,
    scope: TargetScope,
    src_project_root: Path | None,
    *,
    dst_path: Path,
    to_scope: TargetScope,
    layout: Literal["dir", "flat"],
    dst_project_root: Path | None,
) -> tuple[list[Path], list[Path]]:
    """Remove runtime fan-out targets for one artifact at one scope.

    Best-effort cleanup invoked after a successful canonical move so the
    pre-migration scope's runtime entries (``~/.claude/agents/foo.md``,
    ``~/.gemini/commands/foo.toml``, ``~/.codex/agents/foo.toml`` etc.)
    do not linger as orphans (#895 P2). Target selection — including the
    symlink / foreign-file exclusions — lives in
    :func:`_existing_fanout_targets`, shared with the dry-run preview.

    Two-root contract (ADR-0023 §4): the single ``project_root`` this
    helper used to take drove BOTH stale fan-out discovery and the
    expected-render/override verification, which diverge in a
    cross-project move. ``src_project_root`` anchors discovery (where
    the stale runtime entries live — the artifact's pre-move project);
    ``dst_project_root`` anchors verification (where the canonical and
    its travelling ``overrides/`` now live). A same-root move passes
    the same path for both, which is byte-for-byte the historical
    behavior.

    #1247 id 6: deletion is no longer unconditional. Each target is
    byte-compared against what sync would write
    (:func:`_fanout_target_matches`); a diverged or unverifiable target
    is snapshotted (:func:`_backup_fanout_target`) before removal, and
    KEPT if the snapshot fails. Runtime-side edits and hand-authored
    name collisions stay recoverable while the moved-away name still
    stops being discoverable (#895's original point).

    Returns ``(removed, backed_up)`` for telemetry / verification —
    independent lists: a snapshot whose target then failed to delete
    still appears in ``backed_up``.
    """
    removed: list[Path] = []
    backed_up: list[Path] = []
    targets = _existing_fanout_targets(kind, name, scope, src_project_root)
    if not targets:
        return removed, backed_up

    # Parse the canonical once (agents/commands; skills compare trees).
    parsed_item: Any | None = None
    if kind != "skills":
        manifest = dst_path if layout == "flat" else dst_path / _DIR_MANIFEST[kind]
        try:
            if kind == "agents":
                parsed_item = parse_canonical_agent(manifest, layout=layout)
            else:
                parsed_item = parse_canonical_command(manifest, layout=layout)
        except (OSError, AgentParseError, CommandParseError) as exc:
            logger.warning(
                "fanout cleanup: canonical at %s unreadable/unparseable (%s); "
                "treating every runtime target as diverged",
                manifest,
                exc,
            )

    for runtime, target in targets:
        matches = _fanout_target_matches(
            kind, name, runtime, target, parsed_item, dst_path, to_scope, dst_project_root
        )
        if not matches:
            bak = _backup_fanout_target(target)
            if bak is None:
                continue
            backed_up.append(bak)
            logger.warning(
                "fanout cleanup: %s diverged from the canonical render; "
                "snapshotted to %s before removal — review and delete the "
                "backup manually",
                target,
                bak,
            )
        try:
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
            removed.append(target)
        except OSError as exc:
            logger.warning("fanout cleanup: failed to remove %s: %s", target, exc)
    return removed, backed_up


def _detect_source_scope(
    kind: ArtifactKind,
    name: str,
    project_root: Path | None,
    explicit_from: TargetScope | None,
    *,
    marker_counts_as_presence: bool = False,
) -> tuple[TargetScope, Path, Literal["dir", "flat"]]:
    """Locate the unique scope where *name*'s canonical lives.

    ``project_root=None`` (transfer engine, source side without a
    project context) restricts the probe to the user tier — the
    project-tier ``canonical_artifact_dir`` calls raise
    :class:`ContextScopeError` and are skipped by the existing
    ``continue``. Callers that mean a project tier must pass a root;
    :func:`memtomem.context.transfer.transfer_artifact` pre-checks
    that pairing before calling here.

    Uses :func:`canonical_artifact_dir` + on-disk probes (NOT
    ``list_canonical_*`` because PR-E4 only operates on a single name:
    listing the whole tree just to filter is wasteful, and we still want
    to surface flat-layout legacy entries). Returns
    ``(scope, src_path, layout)`` where ``src_path`` is the directory or
    file to move:

    - ``layout="dir"``  → ``src_path = <canonical_root> / <name>``
      (the directory that contains the manifest file).
    - ``layout="flat"`` → ``src_path = <canonical_root> / f"{name}.md"``
      (a single legacy file). Skills never have flat layout.

    When ``explicit_from`` is set, only that scope is checked and a miss
    raises with a scope-specific message. Otherwise all three scopes are
    checked and ambiguity (>1 match) raises with the candidate list so
    the user can disambiguate via ``--from``.

    ``marker_counts_as_presence`` (ADR-0030 §10, opt-in, **transfer only**): a
    skills root carrying a live swap marker for *name* counts as a ``"dir"``
    candidate even while the ``<root>/<name>/`` tree is absent — the window
    between a swap's two renames. Without it the artifact is unreachable by the
    one operation able to repair it: discovery raises
    :class:`ArtifactNotFoundError` before any lock is taken, so the recovery
    prelude never runs.

    It stays **off by default** because presence that outruns the tree changes
    what every downstream caller may assume. Transfer opts in *and* re-verifies
    the full layout+manifest contract under its canonical locks, after the
    prelude; :func:`migrate_scope` and the other callers have no such re-check,
    so for them a marker-only hit would be a "found" that later code reads as a
    real directory. The probe itself is read-only
    (:func:`~memtomem.context._dir_swap.has_pending_swap`), so a dry-run stays
    side-effect free.
    """
    candidates: list[tuple[TargetScope, Path, Literal["dir", "flat"]]] = []
    scopes_to_check: tuple[TargetScope, ...]
    if explicit_from is not None:
        scopes_to_check = (explicit_from,)
    else:
        scopes_to_check = ("user", "project_shared", "project_local")
    manifest = _DIR_MANIFEST[kind]
    for s in scopes_to_check:
        try:
            root = canonical_artifact_dir(kind, s, project_root)
        except ContextScopeError:
            continue
        if not root.is_dir():
            continue
        if is_internal_artifact_dir(name):
            # A crash leftover is not a transferable source. The lister and the
            # name resolver both refuse these (ADR-0037 §6); this probe reaches
            # the filesystem directly, so it has to refuse them too or every
            # verb built on it — migrate, transfer, bundle export — inherits the
            # bypass.
            continue
        dir_candidate = root / name
        if dir_candidate.is_dir() and (dir_candidate / manifest).is_file():
            candidates.append((s, dir_candidate, "dir"))
            continue  # dir wins over flat — same convention as list_canonical_*
        if kind == "skills":
            # Skills have no flat layout; probe stops here — except that an
            # interrupted swap can leave the tree absent while the marker still
            # proves the artifact belongs to this scope (see the docstring).
            if marker_counts_as_presence and has_pending_swap(root, name):
                candidates.append((s, dir_candidate, "dir"))
            continue
        flat_candidate = root / f"{name}.md"
        if flat_candidate.is_file():
            candidates.append((s, flat_candidate, "flat"))

    if not candidates:
        if explicit_from is not None:
            raise ArtifactNotFoundError(f"{kind}/{name} not found at scope='{explicit_from}'.")
        raise ArtifactNotFoundError(
            f"{kind}/{name} not found in any scope (user / project_shared / project_local)."
        )
    if len(candidates) > 1:
        listed = ", ".join(f"{s} ({p})" for s, p, _ in candidates)
        raise click.ClickException(
            f"{kind}/{name} exists in multiple scopes: {listed}. "
            f"Pass --from <scope> to disambiguate."
        )
    return candidates[0]


def migrate_scope(
    kind: ArtifactKind,
    name: str,
    *,
    from_scope: TargetScope | None,
    to_scope: TargetScope,
    project_root: Path,
    apply_: bool,
    surface: str = "cli_context_migrate",
) -> MigrateScopeResult:
    """Move a canonical artifact between ADR-0011 scope tiers.

    Thin same-root wrapper over
    :func:`memtomem.context.transfer.transfer_artifact` (ADR-0023): the
    staged-move orchestration that used to live here moved to the
    transfer engine when cross-project support landed. This wrapper pins
    the historical surface — same signature, byte-compatible
    :class:`MigrateScopeResult` values, and byte-identical error
    messages for every same-root case reachable from the shipping
    surfaces (the pre-delegation checks below own the literals the
    transfer engine words differently for its own callers). One
    documented exception, unreachable through the Click/MCP gates: an
    invalid ``from_scope``/``to_scope`` literal now raises a clear
    "unsupported source/destination scope" instead of the old
    misleading "not found at scope='<bogus>'" / raw
    :class:`ContextScopeError` (ADR-0023 §Backward compatibility).

    Pure module entry point — no Click prompts, no stdout writes; the
    CLI wrapper in :mod:`memtomem.cli.context_cmd` owns all user-facing
    output. Errors raise :class:`click.ClickException` so the wrapper
    can re-raise verbatim.

    Args:
        surface: Gate A audit identifier forwarded to the staging scan.
            The CLI relies on the default ``"cli_context_migrate"``; the
            MCP tool passes ``"mcp_context_artifact_migrate"`` (#1246 —
            previously MCP-driven moves were misattributed to the CLI
            literal).

    Returns:
        :class:`MigrateScopeResult` with ``moved=True`` on apply
        success, ``moved=False`` on dry-run. ``fanout_cleaned`` lists
        every runtime path removed and ``fanout_backed_up`` every
        ``.bak`` snapshot taken for diverged targets (apply);
        ``fanout_planned`` previews the same target selection (dry-run).
    """
    if kind not in SCOPE_MIGRATABLE_KINDS:
        raise click.ClickException(
            f"unsupported kind for scope migration: {kind!r} (use one of {SCOPE_MIGRATABLE_KINDS})"
        )
    validate_name(name, kind=f"{kind[:-1]} name")
    if from_scope is not None and from_scope == to_scope:
        raise click.ClickException("--from and --to must differ.")

    project_root = Path(project_root).expanduser().resolve()

    # Local import: transfer.py imports this module's primitives at load
    # time; importing it lazily here keeps the cycle one-directional.
    from memtomem.context.transfer import transfer_artifact

    result = transfer_artifact(
        kind,
        name,
        src_project_root=project_root,
        from_scope=from_scope,
        dst_project_root=project_root,
        to_scope=to_scope,
        mode="move",
        apply_=apply_,
        surface=surface,
    )
    return MigrateScopeResult(
        kind=result.kind,
        name=result.name,
        from_scope=result.from_scope,
        to_scope=result.to_scope,
        src_path=result.src_path,
        dst_path=result.dst_path,
        layout=result.layout,
        moved=result.transferred,
        fanout_cleaned=result.fanout_cleaned,
        fanout_backed_up=result.fanout_backed_up,
        fanout_planned=result.fanout_planned,
    )
