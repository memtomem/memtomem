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
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Literal

import click

from memtomem.config import TargetScope
from memtomem.context import override as _override
from memtomem.context._atomic import _file_lock, _lock_path_for
from memtomem.context._canonical_txn import canonical_sidecar_lock
from memtomem.context._dir_swap import has_pending_swap
from memtomem.context._names import GENERATOR_VENDOR, InvalidNameError, validate_name
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
    "adopt_flat_to_dir",
    "classify_migrate",
    "migrate_one",
    "migrate_scope",
]


class MigratePartialError(Exception):
    """Raised when a scope-tier migrate cannot be cleanly completed.

    Specifically raised by the EXDEV-fallback path when the canonical
    has been copied to ``dst`` but the original ``src`` cannot be
    removed (permissions, open file handle, etc.). Both canonicals
    are now on disk; the next ``mm context sync`` at the source
    scope would recreate runtime fan-out at the old tier from the
    stale ``src``, producing duplicate-scope ambiguity that
    ``_detect_source_scope`` cannot resolve (#895 P2 review #5).

    The error carries both paths so the caller (CLI / web / MCP)
    can surface a remediation hint pointing at the file the user
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
    transfer route (A-5 #1276), whose un-cancellable worker thread must
    self-abort inside the route's ``asyncio.timeout`` window — the
    #1145 orphan-thread shape ``_SETTINGS_LOCK_BUDGET_S`` /
    ``_SKILLS_LOCK_BUDGET_S`` close for their engines.
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


def _stage_move(src: Path, dst_parent: Path, name_hint: str) -> tuple[Path, bool]:
    """Move *src* into a same-fs staging entry under *dst_parent*.

    Returns ``(staging_path, src_consumed)``. ``src_consumed=True`` when
    ``os.rename`` succeeded (same-FS fast path) and the source is now
    gone from disk. ``False`` when EXDEV forced a copy fallback; the
    caller is responsible for removing the source after a successful
    promote.

    Cleanup discipline: on any error the staging entry is removed before
    re-raising so callers do not have to. The src side is never touched
    on the EXDEV fallback path until the caller signals promote success.
    """
    dst_parent.mkdir(parents=True, exist_ok=True)
    suffix = f"{os.getpid()}-{secrets.token_hex(4)}"
    staging = dst_parent / f".migrate-{name_hint}-{suffix}.tmp"
    if staging.exists():
        # Crashed prior run with a colliding suffix (extremely unlikely
        # given pid+rand) — leftover is from us; safe to clear.
        if staging.is_dir():
            shutil.rmtree(staging)
        else:
            staging.unlink()
    try:
        os.rename(src, staging)
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            # Non-EXDEV failure (permissions, missing src, etc.) — surface.
            with contextlib.suppress(OSError):
                if staging.exists():
                    if staging.is_dir():
                        shutil.rmtree(staging, ignore_errors=True)
                    else:
                        staging.unlink()
            raise
        # EXDEV fallback: copy bytes into staging without touching src.
        # ``symlinks=True`` / ``follow_symlinks=False`` keep cross-FS
        # semantics identical to the same-FS ``os.rename`` path above,
        # which moves symlinks as links. The stdlib default would
        # dereference them, materializing out-of-tree target bytes into
        # staging — and from there into the (possibly git-tracked)
        # destination tier — violating the package's no-deref mirror
        # contract (``_atomic.copy_tree_atomic``). Preserving links also
        # makes dangling ones non-fatal (#1247 id 7).
        try:
            if src.is_dir():
                shutil.copytree(src, staging, symlinks=True)
            else:
                shutil.copy2(src, staging, follow_symlinks=False)
        except BaseException:
            if staging.exists():
                if staging.is_dir():
                    shutil.rmtree(staging, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        staging.unlink()
            raise
        return staging, False
    return staging, True


def _promote_move(staging: Path, dst: Path) -> None:
    """Atomic ``os.replace(staging, dst)``; refuse if dst already exists.

    Pre-condition (pinned by :func:`migrate_scope` Row 15 contract): dst
    must not exist. Re-checking inside the lock window avoids the rare
    race where the dry-run preview observed an empty dst but an external
    actor created one before the apply phase took the lock.
    """
    if dst.exists():
        raise FileExistsError(f"destination already exists: {dst}")
    dst.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, dst)


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
