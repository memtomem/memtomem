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

import hashlib
import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, cast

from memtomem.context._atomic import (
    DIRTY_SKIP_SUFFIXES,
    copy_tree_atomic,
    installed_at_from_dest,
    is_copy_skipped_rel,
    iter_installed_files,
)
from memtomem.context._names import validate_name
from memtomem.context.dirty import DirtyReport, is_asset_dirty
from memtomem.context.lockfile import (
    Lockfile,
    LockfileError,
    digests_from_entry,
    manifest_from_entry,
)
from memtomem.context.privacy_scan import (
    raise_or_collect,
    scan_artifact_tree,
    scan_text_content,
)
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


class ProjectRootMissingError(FileNotFoundError):
    """Raised by ``_install_asset`` / ``_update_asset`` when the destination
    project root does not exist (the ``project_root.is_dir()`` guard).

    Subclasses ``FileNotFoundError`` (not ``RuntimeError`` like its siblings) so
    every existing ``except FileNotFoundError`` caller — the CLI verbs, any
    ``str(exc)`` consumer — keeps working byte-for-byte. The distinct type lets
    the web route map ONLY this guard to a fixed 404 ``project_root_missing``
    envelope; a bare ``FileNotFoundError`` from a later source-walk / copy race
    (``iter_installed_files``, ``copy_tree_atomic``, ``installed_at_from_dest``)
    must NOT be mislabeled as a missing destination project (#1385 finding 4).
    """


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

    The dataclass caches the per-project lockfile read (``lock_entry``) for
    the execute phase. ``dirty_report`` is **preview-only** (#1247 id 13):
    it renders the table the user confirms against, and
    :func:`_apply_update` re-classifies the dest tree at apply time because
    the confirm prompt between preview and write is unbounded.

    State semantics:

    - ``"update"`` — wiki HEAD ≠ lockfile pin AND dest is clean. Will
      copy wiki bytes when the user confirms.
    - ``"unchanged"`` — wiki HEAD == lockfile pin. No-op; ``dirty_report``
      stays ``None`` because the dirty walk was skipped (cheap by design).
    - ``"refuse"`` — wiki HEAD ≠ lockfile pin AND the write can't be
      proven safe: dest has local edits, a live flat-layout sibling
      would be shadowed, or the entry's ``installed_at`` is unusable
      over an existing dest (#1247). Without ``--force`` the entire
      batch refuses.
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
    lock_timeout: float | None = None,
) -> InstallResult:
    """Snapshot ``<wiki>/skills/<name>/`` into ``<project>/.memtomem/skills/<name>/``.

    Pins the wiki HEAD commit at the start of the operation so a concurrent
    ``git pull`` in the wiki cannot make the recorded ``wiki_commit`` drift
    from the bytes that were copied. Refuses if either the lockfile entry
    or the destination directory already exists — see module docstring.
    """
    return _install_asset(project_root, "skills", name, wiki=wiki, lock_timeout=lock_timeout)


def install_agent(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
    lock_timeout: float | None = None,
) -> InstallResult:
    """Snapshot ``<wiki>/agents/<name>/`` into ``<project>/.memtomem/agents/<name>/``."""
    return _install_asset(project_root, "agents", name, wiki=wiki, lock_timeout=lock_timeout)


def install_command(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
    lock_timeout: float | None = None,
) -> InstallResult:
    """Snapshot ``<wiki>/commands/<name>/`` into ``<project>/.memtomem/commands/<name>/``."""
    return _install_asset(project_root, "commands", name, wiki=wiki, lock_timeout=lock_timeout)


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


def _flat_layout_probe(
    dest: Path,
    asset_type: str,
    name: str,
    lock_entry: dict[str, Any] | None,
) -> tuple[Path | None, bool]:
    """Probe the legacy flat-layout sibling of a dir-layout dest (#1247).

    Returns ``(flat_path, dirty_or_unprovable)``. ``flat_path`` is
    ``<dest_parent>/<name>.md`` when it exists as a file AND ``dest`` itself
    is not a directory, else ``None``. Writing the dir layout next to a live
    flat file flips fan-out serving from the flat bytes to the wiki bytes
    (dir wins in ``list_canonical_agents``/``commands``), so callers must
    refuse when the flat file can't be proven clean. The ``dest.is_dir()``
    short-circuit scopes the guard to that serving FLIP: when the dir
    already exists, dir-wins already happened and this write isn't what
    shadows the flat file.

    The dirty rule matches ``migrate._is_flat_file_dirty`` — strict
    ``mtime > installed_at_epoch`` — with one addition: an entry whose
    ``installed_at`` is missing/non-string/unparseable
    (:func:`_installed_at_epoch` → ``None``) counts as dirty-or-unprovable.
    "Can't prove clean" must protect, not proceed — without this, a
    malformed timestamp (classified ``never_installed``) would skip the
    guard entirely (#1247 design gate M1). Skills have no flat layout;
    other asset types return ``(None, False)``.
    """
    if asset_type not in ("agents", "commands") or dest.is_dir():
        return None, False
    flat = dest.parent / f"{name}.md"
    if not flat.is_file():
        return None, False
    epoch = _installed_at_epoch(lock_entry or {})
    if epoch is None:
        return flat, True
    return flat, flat.stat().st_mtime > epoch


def _reconcile_removed_files(
    dest: Path,
    *,
    src_has: Callable[[str], bool],
    old_installed_at_epoch: float | None,
    baked: frozenset[Path],
    manifest: frozenset[str] | None,
    old_digests: dict[str, str] | None = None,
) -> tuple[Path, ...]:
    """Delete dest files absent from the copy source (#1247).

    ``copy_tree_atomic`` is an additive mirror; without this pass a file
    removed upstream survives in dest forever while the lockfile claims
    the new commit and status reports ``ok``. Decision rule per dest-only
    file (walked via :func:`iter_installed_files`, so ``.bak`` siblings
    and skip-listed names are never candidates):

    When ``old_digests`` is valid (the OLD entry recorded digests, #1247
    id 15), it is the **single provenance set for both decisions** — the
    ``files`` manifest is not consulted at all on this branch (a
    hand-merged divergent manifest must not indefinitely protect a
    digest-tracked, wiki-dropped file):

    1. relpath **not** in ``old_digests`` → user-added file: keep.
    2. relpath recorded → delete when the current bytes hash to the
       recorded digest (provably untouched — this also retires the
       legacy false-KEEP of an untouched file with fresh mtime, e.g. a
       cross-machine checkout) **or** when it is in ``baked``. An
       unreadable file is neither provable nor baked → rule 3.
    3. Anything else → keep with a warning (when in doubt, never delete).

    Legacy fallback (no valid old digests):

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
        if old_digests is not None:
            if rel not in old_digests:
                logger.debug("reconcile: keeping user-added file %s", f)
                continue
            provably_untouched = f in baked
            if not provably_untouched:
                try:
                    provably_untouched = (
                        hashlib.sha256(f.read_bytes()).hexdigest() == old_digests[rel]
                    )
                except OSError:
                    provably_untouched = False  # unreadable: can't prove — rule 3
            if not provably_untouched:
                logger.warning(
                    "reconcile: keeping dest-only file with unproven bytes and no .bak: %s", f
                )
                continue
        else:
            if manifest is not None and rel not in manifest:
                logger.debug("reconcile: keeping user-added file %s", f)
                continue
            provably_old = (
                old_installed_at_epoch is not None and f.stat().st_mtime <= old_installed_at_epoch
            )
            if not (provably_old or f in baked):
                logger.warning(
                    "reconcile: keeping dest-only file with fresh mtime and no .bak: %s", f
                )
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


def _gate_a_scan_src_tree(
    src: Path,
    *,
    surface: str,
    project_root: Path,
    asset_type: str,
    name: str,
) -> None:
    """Gate A over the copier's effective file set of a wiki working-tree asset.

    Iterates :func:`iter_installed_files` rather than handing the directory
    to :func:`scan_artifact_tree` directly — the walker shares
    ``copy_tree_atomic``'s skip rules, so a wiki-shipped ``*.bak`` that the
    copier would never install cannot false-block the install (#1247;
    scan set == copy set). Raises
    :class:`memtomem.context.privacy_scan.PrivacyBlockedError` on the first
    hit; callers run this BEFORE any dest mutation so refusal leaves zero
    residue.
    """
    kind = asset_type.removesuffix("s")
    for path in iter_installed_files(src):
        result = scan_artifact_tree(
            path, surface=surface, scope="project_shared", project_root=project_root
        )
        if result.blocked:
            blocked = result.blocked[0]
            raise_or_collect(
                blocked,
                scope="project_shared",
                kind=kind,
                artifact_name=name,
                remediation_hint=(
                    f"Remove the secret from the wiki copy at {blocked.path} "
                    f"and re-run — wiki bytes must be clean before they can land "
                    f"in the git-tracked project tree."
                ),
            )


def _gate_a_scan_dirty_files(
    dirty_files: tuple[Path, ...] | list[Path] | frozenset[Path],
    *,
    surface: str,
    project_root: Path,
    asset_type: str,
    name: str,
) -> None:
    """Gate A over the dirty dest files a ``--force`` run would ``.bak``-snapshot.

    The ``.bak`` sibling is a NEW file in the git-tracked tree (mirroring the
    version-create snapshot standard): a secret in a locally edited file must
    refuse the forced update before any ``.bak`` lands, not silently
    duplicate into a second commit-able path (#1247).
    """
    kind = asset_type.removesuffix("s")
    for path in sorted(dirty_files):
        result = scan_artifact_tree(
            path, surface=surface, scope="project_shared", project_root=project_root
        )
        if result.blocked:
            blocked = result.blocked[0]
            raise_or_collect(
                blocked,
                scope="project_shared",
                kind=kind,
                artifact_name=name,
                remediation_hint=(
                    f"Remove the secret from {blocked.path} (a local edit that "
                    f"--force would preserve as a .bak sibling in the git-tracked "
                    f"tree), then re-run with --force."
                ),
            )


def _gate_a_scan_pinned_asset(
    wiki: WikiStore,
    pin: str,
    asset_type: str,
    name: str,
    *,
    surface: str,
    project_root: Path,
) -> None:
    """Gate A over the bytes a pinned re-extraction would write.

    The wiki working tree is irrelevant here — the extractor reads git
    objects at *pin*, so the scan does too (:meth:`WikiStore.
    read_asset_file_at_commit`), which also closes the scan→write TOCTOU:
    a commit's bytes are immutable. Rels are filtered with
    :func:`is_copy_skipped_rel`, the same predicate the extractor applies,
    so the scan observes exactly the file set that would land. Bytes decode
    with ``errors="replace"`` mirroring :func:`scan_artifact_tree`'s
    contract — an ASCII secret embedded in a non-UTF8 blob still blocks.
    """
    kind = asset_type.removesuffix("s")
    for rel in wiki.asset_files_at_commit(pin, asset_type, name):
        if is_copy_skipped_rel(rel):
            continue
        text = wiki.read_asset_file_at_commit(pin, asset_type, name, rel).decode(
            "utf-8", errors="replace"
        )
        scan = scan_text_content(
            text,
            source_path=wiki.root / asset_type / name / rel,
            surface=surface,
            scope="project_shared",
            project_root=project_root,
        )
        if scan.decision in ("blocked", "blocked_project_shared"):
            raise_or_collect(
                scan,
                scope="project_shared",
                kind=kind,
                artifact_name=name,
                remediation_hint=(
                    f"The pinned wiki commit {pin[:12]} ships a secret in "
                    f"{asset_type}/{name}/{rel}; fix the asset in the wiki and "
                    f"re-pin to a clean commit before restoring."
                ),
            )


def _install_asset(
    project_root: Path | str,
    asset_type: str,
    name: str,
    *,
    wiki: WikiStore | None,
    lock_timeout: float | None = None,
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
    legacy ``mtime > installed_at`` dirty check cannot false-positive on
    the install's own writes. An editor racing the install used to be
    absorbed by that scalar capture (edit mtime folded under the post-copy
    max → permanently clean → silent clobber, #1247 id 15); the per-file
    ``digests`` recorded from the copier's written bytes close that
    window — a concurrent edit at ANY point leaves bytes ≠ digest → dirty.
    """
    validated = validate_name(name, kind=f"{asset_type.removesuffix('s')} name")
    project_root = Path(project_root).expanduser()
    if not project_root.is_dir():
        raise ProjectRootMissingError(f"project root does not exist: {project_root}")

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
        if has_dest and not has_lock:
            # Half-install leftovers (copy succeeded, lockfile write failed
            # or interrupted) and hand-placed trees both land here. The
            # update hint would dead-end (`mm context update` raises
            # NotInstalledError without an entry and points back at
            # install — a circle, #1247 id 4). No auto-adopt either:
            # install can't tell a leftover from hand-placed content it
            # must not clobber.
            hint = (
                f"dest exists with no lockfile entry — hand-placed files or an "
                f"install interrupted before its lockfile write; inspect {dest}, "
                f"remove the directory if it's disposable, then re-run "
                f"`mm context install {asset_type_singular} {validated}`"
            )
        else:
            hint = (
                f"run `mm context update {asset_type_singular} {validated}` "
                f"to refresh from wiki HEAD"
            )
        raise AlreadyInstalledError(
            f"{asset_type}/{validated}: "
            f"lockfile_entry={'yes' if has_lock else 'no'}, "
            f"dest={'yes' if has_dest else 'no'}; "
            f"{hint}"
        )

    # Gate A (ADR-0011 §5, #1247): wiki bytes are user-tier — secrets are
    # legal there — but dest is the git-tracked project_shared canonical.
    # Scan before mkdir so refusal leaves zero residue (no empty type dir,
    # no dest bytes, no lockfile entry).
    _gate_a_scan_src_tree(
        src,
        surface="cli_context_install",
        project_root=project_root,
        asset_type=asset_type,
        name=validated,
    )

    dest.parent.mkdir(parents=True, exist_ok=True)
    digest_map = copy_tree_atomic(src, dest, skip_suffixes=DIRTY_SKIP_SUFFIXES)

    # files= derives from the copier's WRITTEN set, not a post-copy re-walk
    # (#1247 id 15) — a concurrent addition landing during the copy can no
    # longer be absorbed into the manifest as if wiki-shipped.
    installed_at = installed_at_from_dest(dest)
    lock.upsert_entry(
        asset_type,
        validated,
        wiki_commit=wiki_commit,
        installed_at=installed_at,
        files=sorted(digest_map),
        files_commit=wiki_commit,
        digests=digest_map,
        lock_timeout=lock_timeout,
    )

    return InstallResult(
        asset_type=cast('Literal["skills", "agents", "commands"]', asset_type),
        name=validated,
        wiki_commit=wiki_commit,
        installed_at=installed_at,
        dest=dest,
        files_written=len(digest_map),
    )


def update_skill(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
    force: bool = False,
    lock_timeout: float | None = None,
) -> UpdateResult:
    """Refresh ``<wiki>/skills/<name>/`` snapshot at ``<project>/.memtomem/skills/<name>/``.

    No-op when wiki HEAD already matches the lockfile pin. Refuses when
    local edits would be clobbered, unless ``force=True`` (which preserves
    each dirty file as ``<file>.bak`` before overwriting).
    """
    return _update_asset(
        project_root, "skills", name, wiki=wiki, force=force, lock_timeout=lock_timeout
    )


def update_agent(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
    force: bool = False,
    lock_timeout: float | None = None,
) -> UpdateResult:
    """Refresh ``<wiki>/agents/<name>/`` snapshot at ``<project>/.memtomem/agents/<name>/``."""
    return _update_asset(
        project_root, "agents", name, wiki=wiki, force=force, lock_timeout=lock_timeout
    )


def update_command(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
    force: bool = False,
    lock_timeout: float | None = None,
) -> UpdateResult:
    """Refresh ``<wiki>/commands/<name>/`` snapshot at ``<project>/.memtomem/commands/<name>/``."""
    return _update_asset(
        project_root, "commands", name, wiki=wiki, force=force, lock_timeout=lock_timeout
    )


def _update_asset(
    project_root: Path | str,
    asset_type: str,
    name: str,
    *,
    wiki: WikiStore | None,
    force: bool = False,
    lock_timeout: float | None = None,
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
    6. Delegate to :func:`_apply_update`, which classifies the dest tree
       via :func:`is_asset_dirty` immediately before its gates (#1247
       id 13) and then refuses or writes.

    The split lets ``mm context update --all`` (commit 4) reuse step 6
    after rendering a preview classification across all known projects
    up front.
    """
    validated = validate_name(name, kind=f"{asset_type.removesuffix('s')} name")
    project_root = Path(project_root).expanduser()
    if not project_root.is_dir():
        raise ProjectRootMissingError(f"project root does not exist: {project_root}")

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

    return _apply_update(
        project_root,
        asset_type,
        validated,
        src=src,
        dest=dest,
        wiki_commit=new_commit,
        lock_entry=lock_entry,
        force=force,
        lock_timeout=lock_timeout,
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
    force: bool,
    surface: str = "cli_context_update",
    lock_timeout: float | None = None,
) -> UpdateResult:
    """Execute an update against apply-time dirty evidence.

    Pre-conditions enforced by callers (``_update_asset`` for the single-
    asset path, ``mm context update --all`` orchestration for the batch
    path):

    - ``lock_entry`` is non-None (``NotInstalledError`` is raised earlier).
    - The no-op case (``lock_entry["wiki_commit"] == wiki_commit``) was
      already short-circuited; this helper unconditionally writes.

    The dest tree is classified HERE, immediately before the gates. A
    report computed earlier — in particular before ``--all``'s unbounded
    confirm prompt — is preview-only and must never gate writes: a
    stale-clean report would silently clobber an edit made while the user
    sat on the prompt, and a stale dirty set would ``.bak`` too few files
    under ``--force`` (#1247 id 13; same plan-time-trust bug as #1135
    B4-3). The single-asset path pays no extra walk (it no longer
    pre-computes); ``--all`` walks twice per actionable project — once for
    the preview table, once here for the gate.

    Refuses with :class:`StaleInstallError` when ``dirty_report.reason ==
    "dirty"`` and ``force=False``. With ``force=True`` and a dirty tree,
    each dirty file is preserved alongside the wiki bytes as
    ``<file>.bak`` before the copy. An existing dest whose entry has an
    unusable ``installed_at`` (``reason == "never_installed"``) refuses
    the same way — nothing is provably clean — and ``--force`` preserves
    EVERY current dest file as ``.bak`` (#1247). ``shutil.copy2`` is used
    so the user's edit-mtime survives onto the ``.bak``
    (atomic_write_bytes would lose it). After the copy, dest files absent
    from the wiki
    source are reconciled away (:func:`_reconcile_removed_files`,
    #1247) so the refreshed tree mirrors the wiki instead of growing
    additively. ``installed_at`` is captured *after* copy + reconcile,
    mirroring the C2a (#630) install invariant so a follow-up dirty
    check can't false-positive on this update's own writes.
    """
    dirty_report = is_asset_dirty(project_root, asset_type, name, lock_entry=lock_entry)

    if dirty_report.walk_failed:
        # The dest tree could not be fully enumerated (an unreadable subtree).
        # We cannot identify the at-risk files to back up, so even --force must
        # NOT proceed: copy + reconcile would mutate the readable files and
        # only then fail on the unreadable subtree, leaving a partial update
        # with no .bak. Fail loudly BEFORE any mutation (is_asset_dirty used to
        # raise straight out here; the status walk no longer crashes, so the
        # refusal is explicit now).
        raise StaleInstallError(
            f"{asset_type}/{name}: {dirty_report.summary()} under "
            f"{dest} — refusing to update even with --force, because the "
            f"at-risk files can't be enumerated to back up; fix permissions and retry"
        )

    if dirty_report.reason == "dirty" and not force:
        raise StaleInstallError(
            f"{asset_type}/{name}: {dirty_report.summary()} "
            f"since install at {dirty_report.installed_at}; "
            f"pass --force to overwrite "
            f"(each modified file gets a .bak sibling; deleted files are restored)"
        )

    # Unprovable install record (#1247 impl gate): the entry exists but its
    # installed_at is missing/non-string/unparseable, so NO file in an
    # EXISTING dest tree can be proven clean — refusing is the only
    # non-destructive default. (Pre-#1247 a malformed string crashed with a
    # ValueError here; degrading that crash to never_installed must not
    # degrade it into a silent overwrite.) --force proceeds with EVERY
    # current dest file preserved as .bak, not just a dirty subset — there
    # is no epoch to subset by.
    unprovable = dirty_report.reason == "never_installed" and dest.is_dir()
    if unprovable and not force:
        raise StaleInstallError(
            f"{asset_type}/{name}: install record unusable (missing or malformed "
            f"installed_at) — local edits cannot be ruled out; pass --force to "
            f"overwrite (every current file gets a .bak sibling)"
        )

    # Flat-layout guard (#1247 id 0): a flat file + lockfile entry with no
    # dest dir classifies missing_dest (or never_installed when the entry's
    # installed_at is unusable), which sails past the dirty gate above —
    # yet writing the dir layout silently stops the flat file being served.
    # Refuse like migrate's refuse_dirty unless --force. No .bak and no
    # deletion under --force: nothing overwrites the flat file (it stays on
    # disk, merely shadowed) and consolidation is `mm context migrate`'s job.
    if dirty_report.reason in ("missing_dest", "never_installed"):
        flat_path, flat_dirty = _flat_layout_probe(dest, asset_type, name, lock_entry)
        if flat_path is not None:
            kind = asset_type.removesuffix("s")
            if flat_dirty and not force:
                raise StaleInstallError(
                    f"{asset_type}/{name}: flat-layout file {flat_path.name} has local "
                    f"edits (or no provable install time) and the dir-layout install "
                    f"would stop it being served; run `mm context migrate {kind} {name}` "
                    f"first, or pass --force to write the dir layout anyway (the flat "
                    f"file is kept on disk but stops being served)"
                )
            logger.warning(
                "%s/%s: dir layout will shadow flat file %s (dir wins at fan-out); "
                "run `mm context migrate %s %s` to consolidate",
                asset_type,
                name,
                flat_path,
                kind,
                name,
            )

    # Gate A (ADR-0011 §5, #1247): both scans precede the .bak loop — the
    # first dest mutation with no rollback — so a privacy refusal leaves
    # no .bak, no copied bytes, and (upserts trail copies) no lockfile drift.
    _gate_a_scan_src_tree(
        src,
        surface=surface,
        project_root=project_root,
        asset_type=asset_type,
        name=name,
    )
    files_to_bak: tuple[Path, ...] = ()
    if force and dirty_report.reason == "dirty":
        files_to_bak = dirty_report.dirty_files
    elif force and unprovable:
        files_to_bak = tuple(iter_installed_files(dest))
    if files_to_bak:
        _gate_a_scan_dirty_files(
            files_to_bak,
            surface=surface,
            project_root=project_root,
            asset_type=asset_type,
            name=name,
        )

    bak_paths: list[Path] = []
    for f in files_to_bak:
        bak = f.with_suffix(f.suffix + ".bak")
        # shutil.copy2 preserves user edit's mtime — atomic_write_bytes
        # would lose it. Race window between copy2 and copy_tree_atomic
        # is sub-ms; acceptable for v1. Overwrite-if-exists policy
        # (prior .bak from earlier --force gets replaced).
        shutil.copy2(f, bak)
        bak_paths.append(bak)

    dest.parent.mkdir(parents=True, exist_ok=True)
    digest_map = copy_tree_atomic(src, dest, skip_suffixes=DIRTY_SKIP_SUFFIXES)

    # Mirror semantics (#1247): drop dest files the wiki no longer ships.
    # Membership is the copier's RETURNED written set, not a re-walk of
    # ``src`` (#1247 id 15 impl gate): the wiki working tree is mutable,
    # so a post-copy walk reads a DIFFERENT snapshot than the one copied —
    # a file dropped from the worktree in that window would erase a file
    # this update just wrote (when its bytes are unchanged between
    # versions they still match the OLD digests → "provably untouched")
    # while ``files=``/``digests=`` below record it — a phantom entry.
    # The map shares the copier's skip rules by construction, covering
    # B5's symlink concern (a wiki file replaced by a symlink is absent
    # from the map, so the old regular file in dest reconciles away). The
    # guard epoch is the PRE-update install timestamp (the same basis
    # ``dirty_report`` classified against) — the freshly captured value
    # below is >= every current mtime and would approve deleting
    # anything. ``old_digests`` likewise comes from the OLD entry.
    files_removed = _reconcile_removed_files(
        dest,
        src_has=lambda rel: rel in digest_map,
        old_installed_at_epoch=_installed_at_epoch(lock_entry),
        baked=frozenset(files_to_bak),
        manifest=manifest_from_entry(lock_entry),
        old_digests=digests_from_entry(lock_entry),
    )

    installed_at = installed_at_from_dest(dest)
    lock = Lockfile.at(project_root)
    lock.upsert_entry(
        asset_type,
        name,
        wiki_commit=wiki_commit,
        installed_at=installed_at,
        files=sorted(digest_map),
        files_commit=wiki_commit,
        digests=digest_map,
        lock_timeout=lock_timeout,
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
        files_written=len(digest_map),
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
      what was installed at that pin). **Preview-only** (#1247 id 13):
      it renders the table the user confirms against, but the execute
      phase re-classifies inside :func:`_apply_update` — an unbounded
      confirm prompt sits between the two, so apply-time gates must not
      trust this snapshot.

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
        except LockfileError as exc:
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
        dest = project_root / ".memtomem" / asset_type / name
        flat_path, flat_dirty = (
            _flat_layout_probe(dest, asset_type, name, lock_entry)
            if report.reason in ("missing_dest", "never_installed")
            else (None, False)
        )
        if report.reason == "dirty":
            state: Literal["update", "unchanged", "refuse", "error"] = "refuse"
            reason: str | None = f"{report.summary()} since install"
        elif flat_path is not None and flat_dirty:
            # Preview parity with _apply_update's flat-layout guard
            # (#1247 id 0): the batch table must show the refusal the
            # execute phase would raise.
            state = "refuse"
            reason = (
                f"flat layout with local edits; run "
                f"`mm context migrate {asset_type.removesuffix('s')} {name}` first"
            )
        elif report.reason == "never_installed" and dest.is_dir():
            # Unprovable install record over an existing dest — preview
            # parity with _apply_update's unprovable gate (#1247 impl
            # gate): nothing is provably clean, so the batch must refuse
            # rather than silently overwrite.
            state = "refuse"
            reason = (
                "install record unusable (malformed installed_at); local edits cannot be ruled out"
            )
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
    - ``"refuse"`` — the restore can't be proven safe: dest exists with
      local edits, the entry's ``installed_at`` is unusable over an
      existing dest, or a live flat-layout sibling would be shadowed
      (#1247). Without ``--force`` the entire batch refuses (no writes).
      With ``--force`` the at-risk dest files (dirty subset, or every
      current file when unprovable) are preserved as ``<file>.bak`` then
      the pin is re-extracted; flat siblings are kept in place.
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
    ran (state ∈ {``skip``, ``refuse``}) and is **preview-only** (#1247
    id 13): :func:`_apply_pinned_install` re-classifies at apply time
    because ``--all``'s confirm prompt sits between classify and execute.
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
    3. Asset-path presence at the pin via
       :meth:`WikiStore.asset_files_at_commit` (the extractor's own
       ls-tree shape); absent → state=``error`` (#1247 id 5 —
       preview/execute parity with the extract phase's
       ``AssetNotFoundError``).
    4. Dest existence check:

       - missing → probe the flat-layout sibling
         (:func:`_flat_layout_probe`, #1247 id 0): dirty/unprovable flat
         → state=``refuse``; else state=``install`` (no dirty walk
         needed).
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

        # Preview/execute parity (#1247 id 5): the state="error" docstring
        # promises "asset path missing at the pinned commit", but nothing
        # probed it — such rows previewed green as install/skip and only the
        # execute phase hit AssetNotFoundError from the extractor. Probe with
        # the extractor's own ls-tree helper so the two can't drift. Sits
        # before the dest check: a skip/refuse row at an unrestorable pin is
        # just as unrestorable (--force re-extracts at the pin).
        try:
            wiki.asset_files_at_commit(pin, asset_type, name)
        except AssetNotFoundError:
            out.append(
                ProjectInstallClassification(
                    asset_type=asset_type,  # type: ignore[arg-type]
                    name=name,
                    pin_commit=pin,
                    state="error",
                    reason=f"{asset_type}/{name} not present at pin {pin[:12]}",
                    lock_entry=entry,
                    dirty_report=None,
                )
            )
            continue
        except CommitNotFoundError:
            # Race: reachable in the check above, gone by the ls-tree probe.
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
            flat_path, flat_dirty = _flat_layout_probe(dest, asset_type, name, entry)
            if flat_path is not None and flat_dirty:
                # Flat-layout guard (#1247 id 0): extracting the dir layout
                # next to a dirty (or unprovable) flat file would silently
                # stop the user's flat bytes being served. dirty_report is
                # attached (a missing_dest/never_installed report) so
                # _apply_pinned_install's refuse-state invariants hold; its
                # --force .bak loop iterates dirty_files=() — nothing is
                # overwritten, the flat file stays on disk.
                out.append(
                    ProjectInstallClassification(
                        asset_type=asset_type,  # type: ignore[arg-type]
                        name=name,
                        pin_commit=pin,
                        state="refuse",
                        reason=(
                            f"flat layout with local edits; run `mm context migrate "
                            f"{asset_type.removesuffix('s')} {name}` first"
                        ),
                        lock_entry=entry,
                        dirty_report=is_asset_dirty(
                            project_root, asset_type, name, lock_entry=entry
                        ),
                    )
                )
                continue
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
            reason: str | None = report.summary()
        elif report.reason == "never_installed":
            # Dest exists here, so this is exactly "entry present but
            # unusable installed_at" — unprovable. Collapsing it to "skip"
            # would let --force re-extract over possible local edits with
            # no .bak (#1247 impl gate); mirror _apply_update's refuse.
            state = "refuse"
            reason = (
                "install record unusable (malformed installed_at); local edits cannot be ruled out"
            )
        else:
            # clean / missing_dest collapse to "skip" here: missing_dest is
            # impossible (we already saw dest.exists()), so this is
            # effectively the clean path.
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
    surface: str = "cli_context_install_all",
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
    - ``state="refuse"`` + ``force=False``: raise
      :class:`StaleInstallError` (caller already aborted batch; defense
      in depth).
    - The dest tree is then **re-classified here** (#1247 id 13): the
      classify-phase ``dirty_report`` predates ``--all``'s unbounded
      confirm prompt and is preview-only. Fresh ``dirty`` — or an
      unusable install record over an existing dest — without ``force``
      raises :class:`StaleInstallError` (covers install/skip rows that
      went dirty during the prompt; the CLI loop degrades it to a red
      row). With ``force`` the FRESH at-risk set (dirty subset, or
      EVERY current file when unprovable — mirroring ``_apply_update``)
      is ``shutil.copy2``'d to ``<file>.bak`` before extraction, so a
      clean-at-classify row edited mid-prompt still gets its ``.bak``.
    - A dirty flat-layout sibling is re-probed the same way (design-gate
      fold): one that APPEARED during the prompt refuses without
      ``force``; with ``force`` the flat file stays on disk (shadowed,
      no ``.bak``) exactly like the classify-time flat-refuse rows.
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

    # Apply-time re-classification (#1247 id 13): the classify-phase report
    # predates --all's unbounded confirm prompt, so every gate and the .bak
    # set below use FRESH evidence — mirroring _apply_update. A row that
    # classified install/skip and went dirty during the prompt refuses loud
    # here (the CLI loop catches → red row, file intact).
    report = is_asset_dirty(project_root, asset_type, name, lock_entry=classification.lock_entry)
    if report.walk_failed:
        # Unenumerable dest tree (unreadable subtree): refuse before any
        # mutation even with --force — the at-risk files can't be backed up.
        # Mirrors _apply_update; the --all CLI loop catches → red row, file
        # intact.
        raise StaleInstallError(
            f"{asset_type}/{name}: {report.summary()} under {dest} — refusing to "
            f"update even with --force, because the at-risk files can't be "
            f"enumerated to back up; fix permissions and retry"
        )
    unprovable = report.reason == "never_installed" and dest.is_dir()
    if report.reason == "dirty" and not force:
        raise StaleInstallError(
            f"{asset_type}/{name}: {report.summary()} since classification; "
            f"pass --force to overwrite (each modified file gets a .bak sibling)"
        )
    if unprovable and not force:
        raise StaleInstallError(
            f"{asset_type}/{name}: install record unusable (missing or malformed "
            f"installed_at) — local edits cannot be ruled out; pass --force to "
            f"overwrite (every current file gets a .bak sibling)"
        )

    # Flat-layout re-probe (#1247 id 13 design gate): a dirty flat sibling
    # that appeared during the prompt while dest is absent would be silently
    # shadowed by the extraction below — classify probed too early to see
    # it. The probe self-short-circuits when dest is a dir or the asset
    # type has no flat layout, so classify-time flat-refuse rows re-probe
    # to the same verdict here.
    if report.reason in ("missing_dest", "never_installed"):
        flat_path, flat_dirty = _flat_layout_probe(
            dest, asset_type, name, classification.lock_entry
        )
        if flat_path is not None:
            kind = asset_type.removesuffix("s")
            if flat_dirty and not force:
                raise StaleInstallError(
                    f"{asset_type}/{name}: flat-layout file {flat_path.name} has local "
                    f"edits (or no provable install time) and the dir-layout install "
                    f"would stop it being served; run `mm context migrate {kind} {name}` "
                    f"first, or pass --force to write the dir layout anyway (the flat "
                    f"file is kept on disk but stops being served)"
                )
            logger.warning(
                "%s/%s: dir layout will shadow flat file %s (dir wins at fan-out); "
                "run `mm context migrate %s %s` to consolidate",
                asset_type,
                name,
                flat_path,
                kind,
                name,
            )

    # Gate A (ADR-0011 §5, #1247): scan the pin's git objects (the exact
    # bytes the extractor would write) plus any dirty files a --force run
    # would .bak — both BEFORE the .bak loop, the first dest mutation.
    _gate_a_scan_pinned_asset(
        wiki,
        pin,
        asset_type,
        name,
        surface=surface,
        project_root=project_root,
    )
    files_to_bak: tuple[Path, ...] = ()
    if force and report.reason == "dirty":
        files_to_bak = report.dirty_files
    elif force and unprovable:
        # Unprovable install record over an existing dest (#1247 impl
        # gate): no epoch to subset by, so preserve EVERY current file
        # before the pin re-extraction — mirrors _apply_update. (The
        # flat-refuse rows carry never_installed/missing_dest reports,
        # but their dest is absent → no bak set, the flat file is
        # preserved in place.)
        files_to_bak = tuple(iter_installed_files(dest))
    if files_to_bak:
        _gate_a_scan_dirty_files(
            files_to_bak,
            surface=surface,
            project_root=project_root,
            asset_type=asset_type,
            name=name,
        )

    bak_paths: list[Path] = []
    for f in files_to_bak:
        bak = f.with_suffix(f.suffix + ".bak")
        shutil.copy2(f, bak)
        bak_paths.append(bak)

    digest_map = wiki.copy_asset_at_commit(pin, asset_type, name, dest)

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
            baked=frozenset(files_to_bak),
            manifest=manifest_from_entry(entry),
            old_digests=digests_from_entry(entry),
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
        files=sorted(digest_map),
        files_commit=pin,
        digests=digest_map,
    )

    return InstallResult(
        asset_type=cast('Literal["skills", "agents", "commands"]', asset_type),
        name=name,
        wiki_commit=pin,
        installed_at=installed_at,
        dest=dest,
        files_written=len(digest_map),
        files_removed=files_removed,
    )
