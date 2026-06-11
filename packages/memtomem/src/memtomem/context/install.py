"""Install a single wiki asset into ``<project>/.memtomem/<type>/<name>/``.

Implements ADR-0008 PR-B (skills) and PR-C (agents, commands). The wiki at
``~/.memtomem-wiki/`` is the source of truth; an "install" is a copytree
snapshot pinned to the wiki's HEAD commit, recorded in
:class:`memtomem.context.lockfile.Lockfile`.

Public wrappers — :func:`install_skill`, :func:`install_agent`,
:func:`install_command` — all delegate to :func:`_install_asset`. The wiki
is expected to use directory layout for every kind
(``agents/<name>/agent.md``, ``commands/<name>/command.md``); fan-out at
:mod:`memtomem.context.agents` / :mod:`memtomem.context.commands` reads
both directory and legacy flat layouts during PR-C so the install does
not strand newly-installed assets in an unread layout.

Install is intentionally non-destructive: if either a lockfile entry OR
the destination directory already exists, install refuses with a
classified error (see step 6 of the install pipeline). This forward-
protects ADR-0008 Invariant 2 ("manual edits are detected, not silently
clobbered") without depending on PR-D's mtime/dirty detection. PR-D's
``mm context update`` is the supported way to refresh an installed asset.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from memtomem.context._atomic import (
    copy_tree_atomic,
    installed_at_from_dest,
    iter_installed_files,
)
from memtomem.context._names import validate_name
from memtomem.context.dirty import DirtyReport, is_asset_dirty
from memtomem.context.lockfile import Lockfile, LockfileVersionError, manifest_from_entry
from memtomem.wiki.store import CommitNotFoundError as CommitNotFoundError
from memtomem.wiki.store import WikiStore

logger = logging.getLogger(__name__)

__all__ = [
    "AlreadyInstalledError",
    "AssetNotFoundError",
    "CommitNotFoundError",
    "InstallResult",
    "NotInstalledError",
    "ProjectInstallClassification",
    "StaleInstallError",
    "UpdateResult",
    "install_agent",
    "install_command",
    "install_skill",
    "update_agent",
    "update_command",
    "update_skill",
]


class AssetNotFoundError(RuntimeError):
    """Raised when the requested asset directory does not exist in the wiki."""


class AlreadyInstalledError(RuntimeError):
    """Raised when install would overwrite an existing lockfile entry or dest."""


class NotInstalledError(RuntimeError):
    """Raised by ``_update_asset`` when there is no lockfile entry to refresh.

    Distinguishes "you forgot to install first" from "the wiki asset moved" —
    the CLI maps both to a non-zero exit code, but the message points the
    user at ``mm context install`` rather than implying an internal error.
    """


class StaleInstallError(RuntimeError):
    """Raised by ``_update_asset`` when local edits would be clobbered.

    The dest tree has at least one file with ``mtime > installed_at`` and
    ``--force`` was not set. The message includes the count and points
    the user at ``--force`` (which preserves dirty files as ``.bak``).
    """


@dataclass(frozen=True)
class InstallResult:
    """Outcome of a successful install. Display-oriented; not persisted.

    ``files_removed`` is populated only by the ``install --all --force``
    re-extraction path, which reconciles dest-only leftovers against the
    pinned commit's file set (#1247); a fresh install never removes.
    """

    asset_type: Literal["skills", "agents", "commands"]
    name: str
    wiki_commit: str
    installed_at: str
    dest: Path
    files_written: int
    files_removed: tuple[Path, ...] = ()


@dataclass(frozen=True)
class UpdateResult:
    """Outcome of an ``mm context update`` call. Display-oriented; not persisted.

    - ``was_no_op=True`` means the wiki HEAD already matched the
      lockfile pin — the lockfile bytes were *not* touched, so
      ``installed_at`` is the value previously recorded (echoed for
      display) and ``files_written``/``bak_files_written`` are empty.
    - ``was_no_op=False`` means a real refresh happened: the dest tree
      was mirrored to the wiki bytes — files written/overwritten AND
      dest files absent upstream reconciled away (``files_removed``,
      #1247) — ``installed_at`` was re-captured after the copy, and any
      dirty files were preserved at the listed ``.bak`` paths (only
      populated when ``--force`` was used against a dirty asset).
    """

    asset_type: Literal["skills", "agents", "commands"]
    name: str
    old_wiki_commit: str
    new_wiki_commit: str
    installed_at: str
    was_no_op: bool
    bak_files_written: tuple[Path, ...]
    dest: Path
    files_written: int
    files_removed: tuple[Path, ...] = ()


@dataclass(frozen=True)
class ProjectClassification:
    """Per-project classification produced by :func:`_classify_for_all_update`.

    The dataclass *caches* the per-project lockfile read and the dirty walk
    so the execute phase can reuse them — the same expensive operations
    must not run twice between preview and write.

    State semantics:

    - ``"update"`` — wiki HEAD ≠ lockfile pin AND dest is clean. Will
      copy wiki bytes when the user confirms.
    - ``"unchanged"`` — wiki HEAD == lockfile pin. No-op; ``dirty_report``
      stays ``None`` because the dirty walk was skipped (cheap by design).
    - ``"refuse"`` — wiki HEAD ≠ lockfile pin AND dest has local edits.
      Without ``--force`` the entire batch refuses.
    - ``"error"`` — the project's lockfile is corrupt or unreadable;
      ``reason`` carries the detail. Propagates as a row-level failure
      in the execute summary.

    ``lock_entry`` is the live lockfile entry (carries ``installed_at``
    and ``wiki_commit``). ``dirty_report`` is populated only for
    ``"update"`` and ``"refuse"`` states — the two cases where the
    dirty walk actually ran.
    """

    project_root: Path
    state: Literal["update", "unchanged", "refuse", "error"]
    reason: str | None
    lock_entry: dict[str, Any] | None
    dirty_report: DirtyReport | None


def install_skill(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
) -> InstallResult:
    """Snapshot ``<wiki>/skills/<name>/`` into ``<project>/.memtomem/skills/<name>/``.

    Pins the wiki HEAD commit at the start of the operation so a concurrent
    ``git pull`` in the wiki cannot make the recorded ``wiki_commit`` drift
    from the bytes that were copied. Refuses if either the lockfile entry
    or the destination directory already exists — see module docstring.
    """
    return _install_asset(project_root, "skills", name, wiki=wiki)


def install_agent(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
) -> InstallResult:
    """Snapshot ``<wiki>/agents/<name>/`` into ``<project>/.memtomem/agents/<name>/``."""
    return _install_asset(project_root, "agents", name, wiki=wiki)


def install_command(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
) -> InstallResult:
    """Snapshot ``<wiki>/commands/<name>/`` into ``<project>/.memtomem/commands/<name>/``."""
    return _install_asset(project_root, "commands", name, wiki=wiki)


def _installed_at_epoch(lock_entry: dict[str, Any]) -> float | None:
    """Parse the entry's ``installed_at`` to an epoch, or ``None``.

    Tolerant on purpose: the reconcile mtime guard must degrade to "keep"
    (never delete) when the timestamp is missing or malformed.
    """
    raw = lock_entry.get("installed_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


def _manifest_relpaths(dest: Path) -> list[str]:
    """The asset's installed file set as sorted POSIX relpaths.

    Walks via :func:`iter_installed_files` — the exact set the dirty
    checker and ``installed_at`` capture observe, so the recorded manifest
    can never disagree with the walker about membership.
    """
    return sorted(p.relative_to(dest).as_posix() for p in iter_installed_files(dest))


def _reconcile_removed_files(
    dest: Path,
    *,
    src_has: Callable[[str], bool],
    old_installed_at_epoch: float | None,
    baked: frozenset[Path],
    manifest: frozenset[str] | None,
) -> tuple[Path, ...]:
    """Delete dest files absent from the copy source (#1247).

    ``copy_tree_atomic`` is an additive mirror; without this pass a file
    removed upstream survives in dest forever while the lockfile claims
    the new commit and status reports ``ok``. Decision rule per dest-only
    file (walked via :func:`iter_installed_files`, so ``.bak`` siblings
    and skip-listed names are never candidates):

    1. ``manifest`` valid and relpath **not** recorded → user-added file:
       keep (never silently delete user-authored content).
    2. Recorded in the manifest (or no usable manifest — legacy entry):
       delete when its mtime is ``<= old_installed_at_epoch`` (provably
       untouched bytes from a previous wiki copy) **or** when it is in
       ``baked`` (the ``--force`` path just preserved its ``.bak``).
    3. Anything else (fresh mtime, no ``.bak``, provenance unknown) →
       keep with a warning. Unreachable through the normal classify →
       refuse/--force flow, but the ``--all`` confirm gap can race edits
       past a stale dirty report — when in doubt, never delete.

    Directories the deletions empty are pruned bottom-up (``dest`` itself
    always survives). Returns the removed paths.
    """
    removed: list[Path] = []
    for f in list(iter_installed_files(dest)):
        rel = f.relative_to(dest).as_posix()
        if src_has(rel):
            continue
        if manifest is not None and rel not in manifest:
            logger.debug("reconcile: keeping user-added file %s", f)
            continue
        provably_old = (
            old_installed_at_epoch is not None and f.stat().st_mtime <= old_installed_at_epoch
        )
        if not (provably_old or f in baked):
            logger.warning("reconcile: keeping dest-only file with fresh mtime and no .bak: %s", f)
            continue
        f.unlink(missing_ok=True)
        removed.append(f)

    for f in removed:
        parent = f.parent
        while parent != dest:
            try:
                parent.rmdir()
            except OSError:
                break  # non-empty (or already gone) — stop pruning this chain
            parent = parent.parent
    return tuple(removed)


def _install_asset(
    project_root: Path | str,
    asset_type: str,
    name: str,
    *,
    wiki: WikiStore | None,
) -> InstallResult:
    """Internal: install a single asset of any type.

    Concurrency contract: same-asset races accept last-write-wins on the
    lockfile entry. Both writers pin the same ``wiki_commit`` (HEAD is read
    once per call before copy) and per-file ``atomic_write_bytes`` keeps
    individual files consistent, so byte content under ``dest`` converges
    even if the workers interleave. Distinct-asset writers serialize
    cleanly on the lockfile sidecar lock and both entries survive.

    ``installed_at`` is captured at the lockfile-upsert boundary (after the
    copytree completes) so that a subsequent ``mm context update``'s
    ``mtime > installed_at`` dirty check cannot false-positive on the
    install's own writes.
    """
    validated = validate_name(name, kind=f"{asset_type.removesuffix('s')} name")
    project_root = Path(project_root).expanduser()
    if not project_root.is_dir():
        raise FileNotFoundError(f"project root does not exist: {project_root}")

    wiki = wiki if wiki is not None else WikiStore.at_default()
    wiki.require_exists()

    src = wiki.root / asset_type / validated
    if not src.is_dir():
        raise AssetNotFoundError(f"{asset_type}/{validated} not in wiki at {wiki.root}")

    wiki_commit = wiki.current_commit()

    dest = project_root / ".memtomem" / asset_type / validated
    lock = Lockfile.at(project_root)
    existing = lock.read_entry(asset_type, validated)
    has_lock = existing is not None
    has_dest = dest.exists()
    if has_lock or has_dest:
        asset_type_singular = asset_type.removesuffix("s")
        raise AlreadyInstalledError(
            f"{asset_type}/{validated}: "
            f"lockfile_entry={'yes' if has_lock else 'no'}, "
            f"dest={'yes' if has_dest else 'no'}; "
            f"run `mm context update {asset_type_singular} {validated}` "
            f"to refresh from wiki HEAD"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    files_written = copy_tree_atomic(src, dest)

    installed_at = installed_at_from_dest(dest)
    lock.upsert_entry(
        asset_type,
        validated,
        wiki_commit=wiki_commit,
        installed_at=installed_at,
        files=_manifest_relpaths(dest),
        files_commit=wiki_commit,
    )

    return InstallResult(
        asset_type=cast('Literal["skills", "agents", "commands"]', asset_type),
        name=validated,
        wiki_commit=wiki_commit,
        installed_at=installed_at,
        dest=dest,
        files_written=files_written,
    )


def update_skill(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
    force: bool = False,
) -> UpdateResult:
    """Refresh ``<wiki>/skills/<name>/`` snapshot at ``<project>/.memtomem/skills/<name>/``.

    No-op when wiki HEAD already matches the lockfile pin. Refuses when
    local edits would be clobbered, unless ``force=True`` (which preserves
    each dirty file as ``<file>.bak`` before overwriting).
    """
    return _update_asset(project_root, "skills", name, wiki=wiki, force=force)


def update_agent(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
    force: bool = False,
) -> UpdateResult:
    """Refresh ``<wiki>/agents/<name>/`` snapshot at ``<project>/.memtomem/agents/<name>/``."""
    return _update_asset(project_root, "agents", name, wiki=wiki, force=force)


def update_command(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
    force: bool = False,
) -> UpdateResult:
    """Refresh ``<wiki>/commands/<name>/`` snapshot at ``<project>/.memtomem/commands/<name>/``."""
    return _update_asset(project_root, "commands", name, wiki=wiki, force=force)


def _update_asset(
    project_root: Path | str,
    asset_type: str,
    name: str,
    *,
    wiki: WikiStore | None,
    force: bool = False,
) -> UpdateResult:
    """Internal: refresh a single installed asset of any type.

    Pipeline:

    1. Validate ``name`` and project root.
    2. Locate the wiki + the source asset directory (``AssetNotFoundError``
       if the wiki has dropped the asset entirely).
    3. Read the existing lockfile entry — ``NotInstalledError`` if absent.
    4. Pin wiki HEAD as ``new_commit`` once (concurrent ``git pull`` in the
       wiki cannot make the recorded commit drift mid-update).
    5. **True no-op short-circuit**: when ``new_commit`` matches the lockfile
       pin, return early *without touching the lockfile*. ``installed_at``
       is echoed from the existing entry; ``was_no_op=True``.
    6. Classify the dest tree via :func:`is_asset_dirty` (using the lock
       entry we already loaded — no second lockfile read).
    7. Delegate to :func:`_apply_update` for the refuse-or-write step.

    The split lets ``mm context update --all`` (commit 4) reuse step 7
    after performing classification across all known projects up front.
    """
    validated = validate_name(name, kind=f"{asset_type.removesuffix('s')} name")
    project_root = Path(project_root).expanduser()
    if not project_root.is_dir():
        raise FileNotFoundError(f"project root does not exist: {project_root}")

    wiki = wiki if wiki is not None else WikiStore.at_default()
    wiki.require_exists()

    # NotInstalled check before asset check: if the user never installed,
    # the most useful error points at `mm context install`, not at
    # `wiki has lost the asset`.
    lock = Lockfile.at(project_root)
    lock_entry = lock.read_entry(asset_type, validated)
    if lock_entry is None:
        asset_type_singular = asset_type.removesuffix("s")
        raise NotInstalledError(
            f"{asset_type}/{validated}: no lockfile entry; "
            f"run `mm context install {asset_type_singular} {validated}` first"
        )

    src = wiki.root / asset_type / validated
    if not src.is_dir():
        raise AssetNotFoundError(f"{asset_type}/{validated} not in wiki at {wiki.root}")

    new_commit = wiki.current_commit()
    dest = project_root / ".memtomem" / asset_type / validated

    if lock_entry.get("wiki_commit") == new_commit:
        # True no-op: lockfile bytes untouched, installed_at echoed.
        return UpdateResult(
            asset_type=cast('Literal["skills", "agents", "commands"]', asset_type),
            name=validated,
            old_wiki_commit=new_commit,
            new_wiki_commit=new_commit,
            installed_at=cast(str, lock_entry.get("installed_at", "")),
            was_no_op=True,
            bak_files_written=(),
            dest=dest,
            files_written=0,
        )

    dirty_report = is_asset_dirty(project_root, asset_type, validated, lock_entry=lock_entry)

    return _apply_update(
        project_root,
        asset_type,
        validated,
        src=src,
        dest=dest,
        wiki_commit=new_commit,
        lock_entry=lock_entry,
        dirty_report=dirty_report,
        force=force,
    )


def _apply_update(
    project_root: Path,
    asset_type: str,
    name: str,
    *,
    src: Path,
    dest: Path,
    wiki_commit: str,
    lock_entry: dict[str, Any],
    dirty_report: DirtyReport,
    force: bool,
) -> UpdateResult:
    """Execute an already-classified update.

    Pre-conditions enforced by callers (``_update_asset`` for the single-
    asset path, ``mm context update --all`` orchestration for the batch
    path):

    - ``lock_entry`` is non-None (``NotInstalledError`` is raised earlier).
    - The no-op case (``lock_entry["wiki_commit"] == wiki_commit``) was
      already short-circuited; this helper unconditionally writes.
    - ``dirty_report`` was already computed; this helper does **not**
      re-walk the dest tree.

    Refuses with :class:`StaleInstallError` when ``dirty_report.reason ==
    "dirty"`` and ``force=False``. With ``force=True`` and a dirty tree,
    each dirty file is preserved alongside the wiki bytes as
    ``<file>.bak`` before the copy. ``shutil.copy2`` is used so the
    user's edit-mtime survives onto the ``.bak`` (atomic_write_bytes
    would lose it). After the copy, dest files absent from the wiki
    source are reconciled away (:func:`_reconcile_removed_files`,
    #1247) so the refreshed tree mirrors the wiki instead of growing
    additively. ``installed_at`` is captured *after* copy + reconcile,
    mirroring the C2a (#630) install invariant so a follow-up dirty
    check can't false-positive on this update's own writes.
    """
    if dirty_report.reason == "dirty" and not force:
        raise StaleInstallError(
            f"{asset_type}/{name}: {dirty_report.summary()} "
            f"since install at {dirty_report.installed_at}; "
            f"pass --force to overwrite "
            f"(each modified file gets a .bak sibling; deleted files are restored)"
        )

    bak_paths: list[Path] = []
    if force and dirty_report.reason == "dirty":
        for f in dirty_report.dirty_files:
            bak = f.with_suffix(f.suffix + ".bak")
            # shutil.copy2 preserves user edit's mtime — atomic_write_bytes
            # would lose it. Race window between copy2 and copy_tree_atomic
            # is sub-ms; acceptable for v1. Overwrite-if-exists policy
            # (prior .bak from earlier --force gets replaced).
            shutil.copy2(f, bak)
            bak_paths.append(bak)

    dest.parent.mkdir(parents=True, exist_ok=True)
    files_written = copy_tree_atomic(src, dest)

    # Mirror semantics (#1247): drop dest files the wiki no longer ships.
    # The guard epoch is the PRE-update install timestamp (the same basis
    # ``dirty_report`` classified against) — the freshly captured value
    # below is >= every current mtime and would approve deleting anything.
    files_removed = _reconcile_removed_files(
        dest,
        src_has=lambda rel: (src / rel).is_file(),
        old_installed_at_epoch=_installed_at_epoch(lock_entry),
        baked=(
            frozenset(dirty_report.dirty_files)
            if force and dirty_report.reason == "dirty"
            else frozenset()
        ),
        manifest=manifest_from_entry(lock_entry),
    )

    installed_at = installed_at_from_dest(dest)
    lock = Lockfile.at(project_root)
    lock.upsert_entry(
        asset_type,
        name,
        wiki_commit=wiki_commit,
        installed_at=installed_at,
        files=_manifest_relpaths(dest),
        files_commit=wiki_commit,
    )

    old_wiki_commit = cast(str, lock_entry.get("wiki_commit", ""))

    return UpdateResult(
        asset_type=cast('Literal["skills", "agents", "commands"]', asset_type),
        name=name,
        old_wiki_commit=old_wiki_commit,
        new_wiki_commit=wiki_commit,
        installed_at=installed_at,
        was_no_op=False,
        bak_files_written=tuple(bak_paths),
        dest=dest,
        files_written=files_written,
        files_removed=files_removed,
    )


def _classify_for_all_update(
    asset_type: str,
    name: str,
    *,
    wiki: WikiStore,
    projects: list[Path],
) -> tuple[str, list[ProjectClassification]]:
    """Classify ``asset_type/name`` across many project roots in one pass.

    Returns ``(new_commit, classifications)`` — the wiki HEAD pinned at
    the start of the call, paired with the per-project verdicts. The
    caller is expected to thread ``new_commit`` into each subsequent
    :func:`_apply_update` invocation so the execute phase writes against
    the same snapshot the user confirmed.

    Used by ``mm context update --all``. The wiki state is read **once**
    up front: ``wiki.current_commit()`` and the source-asset existence
    check both happen before the per-project loop, so every project is
    classified against the same snapshot. This guarantees the preview
    table the user confirms against matches what the execute phase will
    actually see.

    Per-project work cached on the resulting :class:`ProjectClassification`:

    - ``lock_entry`` — the lockfile read result (avoids a second read
      during the execute phase).
    - ``dirty_report`` — the :func:`is_asset_dirty` walk for the dest tree
      (only populated when ``state in {"update", "refuse"}``; the
      ``unchanged`` short-circuit skips the walk entirely since the
      lockfile pin matches HEAD and the dest tree is, by definition,
      what was installed at that pin).

    Projects without a lockfile entry for this asset are silently
    skipped (no result row): they were never in scope for this
    asset_type/name, so a "you skipped me" row would just clutter the
    preview. A corrupt lockfile is the exception — it produces an
    explicit ``"error"`` row so the user can see and triage.

    The wiki source asset is read once up front. If it's missing,
    callers get :class:`AssetNotFoundError` here, *before* any project
    loop runs — preventing a confusing per-project "asset not found"
    storm.

    ``name`` is validated at the boundary (defense in depth) — even
    though the CLI ``update_cmd`` is the expected upstream caller, this
    helper also accepts ``name`` as input to a ``Path`` join (``src =
    wiki.root / asset_type / name``) and the per-project ``dest``, so
    a malicious ``../escape`` would escape both. ``feedback_public_api_
    ship_time_validation`` — validate at the function entry, not just
    at the convenience caller.
    """
    name = validate_name(name, kind=f"{asset_type.removesuffix('s')} name")
    new_commit = wiki.current_commit()
    src = wiki.root / asset_type / name
    if not src.is_dir():
        raise AssetNotFoundError(f"{asset_type}/{name} not in wiki at {wiki.root}")

    out: list[ProjectClassification] = []
    for project_root in projects:
        try:
            lock = Lockfile.at(project_root)
            lock_entry = lock.read_entry(asset_type, name)
        except LockfileVersionError as exc:
            out.append(
                ProjectClassification(
                    project_root=project_root,
                    state="error",
                    reason=str(exc),
                    lock_entry=None,
                    dirty_report=None,
                )
            )
            continue
        except OSError as exc:
            out.append(
                ProjectClassification(
                    project_root=project_root,
                    state="error",
                    reason=str(exc),
                    lock_entry=None,
                    dirty_report=None,
                )
            )
            continue

        if lock_entry is None:
            # Asset never installed in this project — silently skip
            # (no preview-table row).
            continue

        if lock_entry.get("wiki_commit") == new_commit:
            out.append(
                ProjectClassification(
                    project_root=project_root,
                    state="unchanged",
                    reason=None,
                    lock_entry=lock_entry,
                    dirty_report=None,
                )
            )
            continue

        # Wiki advanced — classify dirty/clean for this project.
        report = is_asset_dirty(project_root, asset_type, name, lock_entry=lock_entry)
        if report.reason == "dirty":
            state: Literal["update", "unchanged", "refuse", "error"] = "refuse"
            reason: str | None = f"{report.summary()} since install"
        else:
            state = "update"
            reason = None

        out.append(
            ProjectClassification(
                project_root=project_root,
                state=state,
                reason=reason,
                lock_entry=lock_entry,
                dirty_report=report,
            )
        )

    return new_commit, out


# ── install --all (PR-D C3) ─────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectInstallClassification:
    """Per-entry verdict for ``mm context install --all``.

    Mirrors the cache-once-execute-once pattern of
    :class:`ProjectClassification` (used by ``update --all``) but the
    rows here represent ``(asset_type, name)`` *within a single project*
    rather than across projects. The pin we install at is the entry's
    stored ``wiki_commit`` — wiki HEAD plays no role (Option A).

    State semantics:

    - ``"install"`` — dest is missing. Will extract bytes at the pinned
      commit and write the lockfile entry (``installed_at`` refreshes;
      ``wiki_commit`` stays at the pin).
    - ``"skip"`` — dest exists and is clean. Default behavior is no-op;
      ``--force`` re-extracts at the pin (no ``.bak`` since there's
      nothing to preserve).
    - ``"refuse"`` — dest exists and has local edits. Without ``--force``
      the entire batch refuses (no writes). With ``--force`` each dirty
      file is preserved as ``<file>.bak`` then the pin is re-extracted.
    - ``"orphan"`` — the pinned ``wiki_commit`` is not reachable in the
      wiki repo (history rewrite, force-push past the pin). Per-entry
      skip with a yellow warning row; the batch continues so the user
      can recover the rest. v2 may add ``--force-head`` as a degraded
      fallback.
    - ``"error"`` — unrecoverable per-entry condition (corrupt
      lockfile entry, IO error walking the dest tree, asset path
      missing at the pinned commit). Batch continues; row is red.

    ``lock_entry`` carries the live entry (``wiki_commit`` + ``installed_at``);
    ``dirty_report`` is populated only when the dirty walk actually
    ran (state ∈ {``skip``, ``refuse``}).
    """

    asset_type: Literal["skills", "agents", "commands"]
    name: str
    pin_commit: str
    state: Literal["install", "skip", "refuse", "orphan", "error"]
    reason: str | None
    lock_entry: dict[str, Any] | None
    dirty_report: DirtyReport | None


def _classify_for_install_all(
    project_root: Path,
    *,
    wiki: WikiStore,
) -> list[ProjectInstallClassification]:
    """Walk ``Lockfile.iter_entries()`` once and classify each row.

    Per entry:

    1. Read pin from the entry; missing/empty → state=``error``.
    2. Reachability via :meth:`WikiStore.commit_is_reachable`; missing →
       state=``orphan``.
    3. Dest existence check:

       - missing → state=``install`` (no dirty walk needed).
       - present → run :func:`is_asset_dirty` against the cached entry:
         clean → state=``skip``, dirty → state=``refuse``.

    The ``lock_entry`` and ``dirty_report`` fields on the result are the
    live read + walk; the execute phase consumes them without re-reading.
    """
    from memtomem.context.lockfile import Lockfile  # local: tested module-level too

    wiki.require_exists()
    lockfile = Lockfile.at(project_root)

    out: list[ProjectInstallClassification] = []
    for asset_type, name, entry in lockfile.iter_entries():
        if asset_type not in ("skills", "agents", "commands"):
            continue

        pin = entry.get("wiki_commit", "") if isinstance(entry, dict) else ""
        if not isinstance(pin, str) or not pin:
            out.append(
                ProjectInstallClassification(
                    asset_type=asset_type,  # type: ignore[arg-type]
                    name=name,
                    pin_commit="",
                    state="error",
                    reason="lockfile entry missing wiki_commit",
                    lock_entry=entry if isinstance(entry, dict) else None,
                    dirty_report=None,
                )
            )
            continue

        if not wiki.commit_is_reachable(pin):
            out.append(
                ProjectInstallClassification(
                    asset_type=asset_type,  # type: ignore[arg-type]
                    name=name,
                    pin_commit=pin,
                    state="orphan",
                    reason=f"pin {pin[:12]} not reachable",
                    lock_entry=entry,
                    dirty_report=None,
                )
            )
            continue

        dest = project_root / ".memtomem" / asset_type / name
        if not dest.exists():
            out.append(
                ProjectInstallClassification(
                    asset_type=asset_type,  # type: ignore[arg-type]
                    name=name,
                    pin_commit=pin,
                    state="install",
                    reason=None,
                    lock_entry=entry,
                    dirty_report=None,
                )
            )
            continue

        report = is_asset_dirty(project_root, asset_type, name, lock_entry=entry)
        if report.reason == "dirty":
            state: Literal["install", "skip", "refuse", "orphan", "error"] = "refuse"
            reason: str | None = f"{len(report.dirty_files)} file(s) modified locally"
        else:
            # clean / missing_dest / never_installed all collapse to "skip" here:
            # missing_dest is impossible (we already saw dest.exists()), and
            # never_installed shouldn't happen (we read the entry above), so
            # this is effectively the clean path.
            state = "skip"
            reason = "already installed"

        out.append(
            ProjectInstallClassification(
                asset_type=asset_type,  # type: ignore[arg-type]
                name=name,
                pin_commit=pin,
                state=state,
                reason=reason,
                lock_entry=entry,
                dirty_report=report,
            )
        )

    return out


def _apply_pinned_install(
    project_root: Path,
    classification: ProjectInstallClassification,
    *,
    wiki: WikiStore,
    force: bool,
) -> InstallResult:
    """Execute one install at the pinned commit.

    Pre-conditions enforced by callers (CLI ``_run_install_all``):

    - ``classification.state ∈ {"install", "skip", "refuse"}`` —
      orphan/error rows are reported by the caller without invoking
      this helper.
    - ``classification.pin_commit`` is non-empty.
    - For ``state ∈ {"skip", "refuse"}``, ``classification.dirty_report``
      and ``classification.lock_entry`` are non-None.

    Behavior:

    - ``state="install"``: extract at pin → write lockfile (pin
      preserved, ``installed_at`` refreshed).
    - ``state="skip"`` + ``force=False``: caller skips; this helper
      shouldn't be reached (defense in depth — raises if it is).
    - ``state="skip"`` + ``force=True``: re-extract at pin, no ``.bak``
      (clean dest had nothing to preserve).
    - ``state="refuse"`` + ``force=False``: raise
      :class:`StaleInstallError` (caller already aborted batch; defense
      in depth).
    - ``state="refuse"`` + ``force=True``: ``shutil.copy2`` each dirty
      file to ``<file>.bak`` (mirroring ``_apply_update``), then extract
      at pin.
    """
    pin = classification.pin_commit
    asset_type = classification.asset_type
    name = classification.name
    dest = project_root / ".memtomem" / asset_type / name

    if classification.state == "skip" and not force:
        raise RuntimeError(
            f"_apply_pinned_install called for skip state without --force "
            f"(asset={asset_type}/{name}); caller should have skipped this row"
        )

    if classification.state == "refuse" and not force:
        refuse_report = classification.dirty_report
        local_edits = refuse_report.summary() if refuse_report is not None else "local edits"
        raise StaleInstallError(
            f"{asset_type}/{name}: {local_edits} would be clobbered; "
            f"pass --force to overwrite (each modified file gets a .bak sibling)"
        )

    bak_paths: list[Path] = []
    if classification.state == "refuse" and force:
        report = classification.dirty_report
        assert report is not None  # state=refuse always carries a report
        for f in report.dirty_files:
            bak = f.with_suffix(f.suffix + ".bak")
            shutil.copy2(f, bak)
            bak_paths.append(bak)

    files_written = wiki.copy_asset_at_commit(pin, asset_type, name, dest)

    # Mirror semantics (#1247): a re-extraction over an existing dest must
    # also retire dest-only leftovers (pre-B1 additive-update residue,
    # carried files from a different pin). Membership comes from the pin's
    # ls-tree set — the extraction tmpdir is internal to
    # ``copy_asset_at_commit`` (design-gate M2).
    files_removed: tuple[Path, ...] = ()
    if classification.state in ("skip", "refuse"):
        expected = set(wiki.asset_files_at_commit(pin, asset_type, name))
        entry = classification.lock_entry or {}
        files_removed = _reconcile_removed_files(
            dest,
            src_has=lambda rel: rel in expected,
            old_installed_at_epoch=_installed_at_epoch(entry),
            baked=(
                frozenset(classification.dirty_report.dirty_files)
                if classification.state == "refuse" and classification.dirty_report is not None
                else frozenset()
            ),
            manifest=manifest_from_entry(entry),
        )

    installed_at = installed_at_from_dest(dest)
    lock = Lockfile.at(project_root)
    # CRITICAL: wiki_commit stays at the pin we just restored to —
    # install --all is reproducibility (Option A), not "advance to HEAD".
    lock.upsert_entry(
        asset_type,
        name,
        wiki_commit=pin,
        installed_at=installed_at,
        files=_manifest_relpaths(dest),
        files_commit=pin,
    )

    return InstallResult(
        asset_type=cast('Literal["skills", "agents", "commands"]', asset_type),
        name=name,
        wiki_commit=pin,
        installed_at=installed_at,
        dest=dest,
        files_written=files_written,
        files_removed=files_removed,
    )
