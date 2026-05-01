"""Convert flat-layout context assets to canonical directory layout.

ADR-0008 PR-D C4. PR-C made the directory layout (e.g.
``agents/<name>/agent.md``) canonical for agents and commands; pre-PR-C
installs and reverse-imports left flat-layout files (``agents/<name>.md``)
on disk. This module classifies and converts those flat assets to the
dir layout in place.

Pure module: filesystem + lockfile only, no wiki dependency (ADR-0008
Invariants 1 / 3). The CLI wrapper in
:func:`memtomem.cli.context_cmd.migrate_cmd` adds the dry-run preview,
``--apply`` gating, and confirmation prompts.

Skills are always directory layout (Agent Skills spec) and are not in
scope here — :func:`classify_migrate` returns an empty list when invoked
with ``asset_type="skills"`` and the CLI surfaces a friendly informational
message rather than an error.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from memtomem.context._names import InvalidNameError, validate_name
from memtomem.context.agents import (
    AGENT_DIR_FILENAME,
    CANONICAL_AGENT_ROOT,
    list_canonical_agents,
)
from memtomem.context.commands import (
    CANONICAL_COMMAND_ROOT,
    COMMAND_DIR_FILENAME,
    list_canonical_commands,
)
from memtomem.context.lockfile import Lockfile

logger = logging.getLogger(__name__)

__all__ = [
    "ASSET_DIR_FILENAMES",
    "MIGRATABLE_ASSET_TYPES",
    "MigrateResult",
    "MigrateRow",
    "MigrateState",
    "classify_migrate",
    "migrate_one",
]


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
    """
    installed_at = lock_entry.get("installed_at")
    if not isinstance(installed_at, str):
        return False
    installed_at_epoch = datetime.fromisoformat(installed_at).timestamp()
    return flat_path.stat().st_mtime > installed_at_epoch


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
    # which collapses both "no entry" and "entry but missing/non-string
    # installed_at" into ``never_installed``. Otherwise migrate would
    # silently proceed against a corrupt entry (no dirty check possible)
    # and could overwrite user edits.
    if lock_entry is not None and not isinstance(lock_entry.get("installed_at"), str):
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
