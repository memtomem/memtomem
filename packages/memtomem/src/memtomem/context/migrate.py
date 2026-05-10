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
layer.

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
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator, Literal

import click

from memtomem.config import TargetScope
from memtomem.context._atomic import _file_lock, _lock_path_for
from memtomem.context._names import InvalidNameError, validate_name
from memtomem.context._runtime_targets import runtime_fanout_root
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
from memtomem.context.privacy_scan import (
    raise_or_collect,
    scan_artifact_tree,
)
from memtomem.context.scope_resolver import (
    ArtifactKind,
    ContextScopeError,
    canonical_artifact_dir,
)
from memtomem.context.skills import SKILL_MANIFEST

logger = logging.getLogger(__name__)

__all__ = [
    "ASSET_DIR_FILENAMES",
    "MIGRATABLE_ASSET_TYPES",
    "SCOPE_MIGRATABLE_KINDS",
    "MigrateResult",
    "MigrateRow",
    "MigrateScopeResult",
    "MigrateState",
    "classify_migrate",
    "migrate_one",
    "migrate_scope",
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

    Set ``moved=False`` for dry-run results (preview only) and for
    skipped rows. ``error`` is non-``None`` only on apply-time failures
    that the helper recovered from (e.g. partial rollback succeeded);
    fatal failures raise :class:`click.ClickException` and never produce
    a ``MigrateScopeResult``.
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
    error: str | None = None


@contextmanager
def _acquire_pair_lock(path_a: Path, path_b: Path) -> Iterator[None]:
    """Acquire two sidecar locks in deterministic sorted order.

    Inverse migrations running concurrently (A: foo user→project_shared,
    B: foo project_shared→user) would deadlock if each side acquired its
    src lock first and dst lock second. Sorting by ``str(lock_path)``
    forces every caller to take the same global order, eliminating the
    cycle.

    The pair is always two locks; if both arguments resolve to the same
    sidecar (defensive — only happens when src and dst are the same file,
    which :func:`migrate_scope` rejects upstream) the second lock is
    skipped to avoid re-entrancy issues with portalocker on platforms
    where ``LOCK_EX`` does not nest.
    """
    lock_a = _lock_path_for(path_a)
    lock_b = _lock_path_for(path_b)
    ordered = sorted([lock_a, lock_b], key=str)
    if ordered[0] == ordered[1]:
        with _file_lock(ordered[0]):
            yield
        return
    with _file_lock(ordered[0]), _file_lock(ordered[1]):
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
        try:
            if src.is_dir():
                shutil.copytree(src, staging)
            else:
                shutil.copy2(src, staging)
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


def _remove_runtime_fanout_for(
    kind: ArtifactKind,
    name: str,
    scope: TargetScope,
    project_root: Path | None,
) -> list[Path]:
    """Remove runtime fan-out targets for one artifact at one scope.

    Best-effort cleanup invoked after a successful canonical move so the
    pre-migration scope's runtime entries (``~/.claude/agents/foo.md``
    etc.) do not linger as orphans. Returns the list of removed paths
    for telemetry / verification.

    Walks every known runtime (claude / gemini / codex). Tuples that
    :func:`runtime_fanout_root` reports as ``NO_FANOUT`` are skipped
    (project_local entries, codex commands at project tiers, etc.).
    KeyError surfaces a programming error in the table — fail-loud.
    """
    removed: list[Path] = []
    for runtime in ("claude", "gemini", "codex"):
        try:
            root = runtime_fanout_root(kind, runtime, scope, project_root)
        except KeyError:
            # Unknown (kind, runtime, scope) tuple — table gap. Skip
            # rather than fail the migration (canonical is already
            # moved at this point); the table is the contract source-
            # of-truth and a missing tuple should be caught by the
            # unit tests on _runtime_targets, not here.
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
            if target.is_symlink():
                # Defensive: never follow / rmtree a symlink — could
                # point outside the runtime root.
                continue
            if target.is_dir():
                try:
                    shutil.rmtree(target)
                    removed.append(target)
                except OSError as exc:
                    logger.warning("fanout cleanup: failed to remove %s: %s", target, exc)
        else:
            target = root / f"{name}.md"
            if target.is_file():
                try:
                    target.unlink()
                    removed.append(target)
                except OSError as exc:
                    logger.warning("fanout cleanup: failed to remove %s: %s", target, exc)
    return removed


def _detect_source_scope(
    kind: ArtifactKind,
    name: str,
    project_root: Path,
    explicit_from: TargetScope | None,
) -> tuple[TargetScope, Path, Literal["dir", "flat"]]:
    """Locate the unique scope where *name*'s canonical lives.

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
            # Skills have no flat layout; probe stops here.
            continue
        flat_candidate = root / f"{name}.md"
        if flat_candidate.is_file():
            candidates.append((s, flat_candidate, "flat"))

    if not candidates:
        if explicit_from is not None:
            raise click.ClickException(f"{kind}/{name} not found at scope='{explicit_from}'.")
        raise click.ClickException(
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
) -> MigrateScopeResult:
    """Move a canonical artifact between ADR-0011 scope tiers.

    Pure module entry point — no Click prompts, no stdout writes; the
    CLI wrapper in :mod:`memtomem.cli.context_cmd` owns all user-facing
    output. Errors raise :class:`click.ClickException` so the wrapper
    can re-raise verbatim.

    Apply sequence:

    1. Auto-detect or validate ``from_scope`` via
       :func:`_detect_source_scope`; reject same-source-as-target.
    2. Resolve ``dst_path`` (mirrors src layout — dir or flat).
    3. Dry-run gate — return the plan without touching disk if
       ``apply_=False``.
    4. Refuse on dst conflict (PR-E4 Row 15: ``--force`` does not
       overwrite scope-tier targets; replace verb is a follow-up).
    5. Acquire src + dst sidecar locks in sorted order
       (:func:`_acquire_pair_lock`).
    6. Stage src → ``<dst.parent>/.migrate-<name>-<pid>-<rand>.tmp``
       via rename; fall back to copytree on EXDEV.
    7. Gate A scan on staging when ``to_scope == project_shared``.
       Block raises :class:`click.ClickException` with the standard
       project_shared block message; rollback puts src back.
    8. Promote staging → dst via ``os.replace``.
    9. EXDEV cleanup (rmtree src) when the rename fell back.
    10. Best-effort cleanup of stale src runtime fan-out targets
        (``~/.claude/agents/<name>.md`` etc.) — outside the lock so a
        partial cleanup failure does not roll back the canonical move.

    Returns:
        :class:`MigrateScopeResult` with ``moved=True`` on apply
        success, ``moved=False`` on dry-run, and ``fanout_cleaned``
        listing every runtime path removed.
    """
    if kind not in SCOPE_MIGRATABLE_KINDS:
        raise click.ClickException(
            f"unsupported kind for scope migration: {kind!r} (use one of {SCOPE_MIGRATABLE_KINDS})"
        )
    validate_name(name, kind=f"{kind[:-1]} name")
    if from_scope is not None and from_scope == to_scope:
        raise click.ClickException("--from and --to must differ.")

    project_root = Path(project_root).expanduser().resolve()

    src_scope, src_path, layout = _detect_source_scope(kind, name, project_root, from_scope)
    if src_scope == to_scope:
        raise click.ClickException(f"{kind}/{name} is already at scope='{to_scope}' (no-op).")

    dst_root = canonical_artifact_dir(kind, to_scope, project_root)
    dst_path = dst_root / name if layout == "dir" else dst_root / f"{name}.md"

    # Pre-flight conflict check (also re-checked inside the lock).
    if dst_path.exists():
        raise click.ClickException(
            f"destination already exists: {dst_path}. "
            "Resolve manually or remove the existing entry first. "
            "--force does not overwrite scope-tier targets in PR-E4 "
            "(replace verb is a follow-up)."
        )

    if not apply_:
        # Dry-run: compute plan, no mutation.
        return MigrateScopeResult(
            kind=kind,
            name=name,
            from_scope=src_scope,
            to_scope=to_scope,
            src_path=src_path,
            dst_path=dst_path,
            layout=layout,
            moved=False,
        )

    # ── apply path ───────────────────────────────────────────────────
    with _acquire_pair_lock(src_path, dst_path):
        # Re-check dst inside the lock window — some other process could
        # have created it between the dry-run preview and the apply
        # phase, even with our own lock-pair held (the writer would have
        # had to take the same lock, but check defensively).
        if dst_path.exists():
            raise click.ClickException(f"destination appeared during lock acquire: {dst_path}.")

        staging, src_consumed = _stage_move(src_path, dst_path.parent, name_hint=name)

        try:
            # Gate A on the staged content if landing in project_shared.
            # The scan runs against staging (the bytes about to be
            # promoted), not against src — so any in-flight edits caught
            # mid-rename are still scanned.
            if to_scope == "project_shared":
                scan = scan_artifact_tree(
                    staging,
                    surface="cli_context_migrate",
                    scope=to_scope,
                    project_root=project_root,
                    on_blocked="fail_fast",
                )
                if scan.blocked:
                    # Raise — project_shared has no force valve in PR-E4
                    # (mirrors PR-D memory-migrate and PR-E3 sync-side).
                    raise_or_collect(
                        scan.blocked[0],
                        scope=to_scope,
                        kind=kind[:-1],
                        artifact_name=name,
                    )

            _promote_move(staging, dst_path)
        except BaseException:
            # Roll back: put bytes back at src so the caller can retry
            # without manual cleanup.
            if src_consumed and not src_path.exists() and staging.exists():
                # Same-FS fast path consumed src; rename staging back.
                with contextlib.suppress(OSError):
                    os.replace(staging, src_path)
            # Drop staging if still present (EXDEV fallback path or the
            # rename-back above failed).
            if staging.exists():
                if staging.is_dir():
                    shutil.rmtree(staging, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        staging.unlink()
            raise

        # EXDEV cleanup — promoted dst now holds the bytes; src copy is
        # stale and must be removed for the migration to be complete.
        if not src_consumed and src_path.exists():
            try:
                if src_path.is_dir():
                    shutil.rmtree(src_path)
                else:
                    src_path.unlink()
            except OSError as exc:
                # Canonical is migrated; src cleanup failed. Surface but
                # don't roll back the canonical move (would re-create
                # the duplicate state we just resolved). User can re-run
                # — _detect_source_scope will then see only src and
                # treat it as the source for a re-migrate.
                logger.warning(
                    "EXDEV cleanup: failed to remove stale src %s: %s",
                    src_path,
                    exc,
                )

    # Lock released. Cleanup stale src runtime fan-out (best-effort).
    fanout_cleaned = _remove_runtime_fanout_for(kind, name, src_scope, project_root)

    return MigrateScopeResult(
        kind=kind,
        name=name,
        from_scope=src_scope,
        to_scope=to_scope,
        src_path=src_path,
        dst_path=dst_path,
        layout=layout,
        moved=True,
        fanout_cleaned=fanout_cleaned,
    )
