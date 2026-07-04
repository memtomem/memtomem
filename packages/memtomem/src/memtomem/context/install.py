"""Install a single wiki asset into ``<project>/.memtomem/<type>/<name>/``.

Implements ADR-0008 PR-B (skills) and PR-C (agents, commands). The wiki at
``~/.memtomem-wiki/`` is the source of truth; install (#1643) and update
(#1652) both extract the asset's bytes **from git objects at the wiki's
HEAD commit** and record that commit in
:class:`memtomem.context.lockfile.Lockfile` — the pin therefore always
reproduces the written bytes. A wiki working tree that differs from HEAD
for the asset (modified/deleted tracked files, untracked files, or a
never-committed asset) refuses with :class:`UncommittedAssetError`; dirt
elsewhere in the wiki does not block. (Update's no-op path — pin already
at HEAD — succeeds without the gates: nothing is written.)

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
from pathlib import Path
from typing import Any, Literal, cast

from memtomem.context._atomic import (
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
    installed_at_epoch_from_entry,
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
    "UncommittedAssetError",
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


class UncommittedAssetError(RuntimeError):
    """Raised when install/update would record a HEAD pin that can't reproduce the write.

    Single-asset install (#1643) and update (#1652) are commit-true: bytes
    are extracted from git objects at HEAD and the lockfile pins that
    commit. When the asset's OWN worktree state differs from HEAD —
    modified/deleted tracked files, untracked files under the asset dir, or
    an asset that was never committed at all — proceeding would either
    write bytes the user doesn't see in the wiki (HEAD ≠ worktree) or
    record a pin that doesn't contain the asset. The message carries a
    runnable ``mm wiki <type> commit`` hint plus a raw-git fallback (the
    hinted command commits only the canonical file and vendor overrides —
    it cannot commit e.g. a skill's ``scripts/`` or deletions). Dirt
    outside the asset never raises this, and update's ``force`` never
    bypasses it (that flag overrides project-side edits, not wiki dirt).
    """


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

    ``was_no_op=True`` is set only by ``install --all``'s
    :func:`_apply_pinned_install` when a ``--force`` row re-classifies CLEAN
    AND its recorded digests differ from the pin's bytes (an entry written
    off a dirty wiki working tree by the pre-#1643 install or the
    pre-#1652 update — both verbs are commit-true now, so only legacy
    lockfiles still carry such rows): the row is left untouched rather
    than silently re-extracted, so nothing is written or removed
    (``files_written=0``). It is reachable only under ``--force`` — without
    it the caller skips clean rows before calling this helper.
    """

    asset_type: Literal["skills", "agents", "commands"]
    name: str
    wiki_commit: str
    installed_at: str
    dest: Path
    files_written: int
    files_removed: tuple[Path, ...] = ()
    was_no_op: bool = False


@dataclass(frozen=True)
class UpdateResult:
    """Outcome of an ``mm context update`` call. Display-oriented; not persisted.

    - ``was_no_op=True`` means the wiki HEAD already matched the
      lockfile pin — the lockfile bytes were *not* touched, so
      ``installed_at`` is the value previously recorded (echoed for
      display) and ``files_written``/``bak_files_written`` are empty.
    - ``was_no_op=False`` means a real refresh happened: the dest tree
      was mirrored to the pinned HEAD commit's bytes (#1652) — files
      written/overwritten AND dest files absent from the pin reconciled
      away (``files_removed``,
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
    surface: str = "cli_context_install",
) -> InstallResult:
    """Snapshot ``skills/<name>/`` **at wiki HEAD** into ``<project>/.memtomem/skills/<name>/``.

    Commit-true (#1643): bytes are extracted from git objects at the HEAD
    commit read at the start of the operation, so neither a concurrent
    ``git pull`` nor a worktree edit in the wiki can make the recorded
    ``wiki_commit`` drift from the bytes that were copied; an asset whose
    worktree state differs from HEAD refuses with
    :class:`UncommittedAssetError`. Also refuses if either the lockfile
    entry or the destination directory already exists — see module
    docstring.

    ``surface`` names the ingress in the Gate-A privacy audit log
    (#1246/#1248 real-ingress rule): the CLI keeps the default, the web
    route passes ``"web_context_install"`` so a browser-triggered block is
    not misattributed to a CLI event.
    """
    return _install_asset(
        project_root, "skills", name, wiki=wiki, lock_timeout=lock_timeout, surface=surface
    )


def install_agent(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
    lock_timeout: float | None = None,
    surface: str = "cli_context_install",
) -> InstallResult:
    """Snapshot ``agents/<name>/`` at wiki HEAD into ``<project>/.memtomem/agents/<name>/``."""
    return _install_asset(
        project_root, "agents", name, wiki=wiki, lock_timeout=lock_timeout, surface=surface
    )


def install_command(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
    lock_timeout: float | None = None,
    surface: str = "cli_context_install",
) -> InstallResult:
    """Snapshot ``commands/<name>/`` at wiki HEAD into ``<project>/.memtomem/commands/<name>/``."""
    return _install_asset(
        project_root, "commands", name, wiki=wiki, lock_timeout=lock_timeout, surface=surface
    )


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
    (:func:`installed_at_epoch_from_entry` → ``None``) counts as dirty-or-unprovable.
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
    epoch = installed_at_epoch_from_entry(lock_entry or {})
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
    remediation_hint: str | None = None,
) -> None:
    """Gate A over the bytes a pinned extraction would write.

    ``remediation_hint`` overrides the default restore-flavored hint ("…
    re-pin to a clean commit before restoring") — the commit-true single
    install (#1643) and update (#1652) scan HEAD here before extraction,
    where "restoring"/"re-pin" would misdirect; each passes a
    verb-flavored hint instead. ``{pin}``/``{rel}`` placeholders are not
    interpolated — pass a fully-rendered string.

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
                remediation_hint=remediation_hint
                or (
                    f"The pinned wiki commit {pin[:12]} ships a secret in "
                    f"{asset_type}/{name}/{rel}; fix the asset in the wiki and "
                    f"re-pin to a clean commit before restoring."
                ),
            )


def _commit_hint(wiki: WikiStore, asset_type: str, name: str) -> str:
    """Runnable remediation hint for :class:`UncommittedAssetError` messages.

    Shared by the install (#1643) and update (#1652) commit-true gates so
    both verbs point at the identical `mm wiki <type> commit` command (plus
    the raw-git fallback for paths that command can't commit).
    """
    singular = asset_type.removesuffix("s")
    return (
        f"commit it first: `mm wiki {singular} commit {name} --canonical` "
        f"(add `--vendor <vendor>` for override files; for scripts, deletions, "
        f"or other paths that command can't commit, use git directly in the "
        f"wiki at {wiki.root})"
    )


def _asset_dirty_rels(wiki: WikiStore, asset_type: str, name: str) -> list[str]:
    """Asset-inner rels whose worktree state differs from HEAD — gate input.

    Strips :meth:`WikiStore.asset_uncommitted_paths` repo-relative rows to
    asset-inner rels so ``is_copy_skipped_rel`` matches the same surface the
    extractor filters (a legacy untracked ``*.bak`` or a stray ``.DS_Store``
    is not dirt: it would never be extracted). Dirt in OTHER assets never
    appears here (the underlying ``git status`` call is pathspec-scoped).
    Non-empty ⇒ the commit-true install/update gates (#1643/#1652) refuse.
    """
    asset_prefix = f"{asset_type}/{name}/"
    return sorted(
        rel
        for row in wiki.asset_uncommitted_paths(asset_type, name)
        if (rel := row[len(asset_prefix) :]) and not is_copy_skipped_rel(rel)
    )


def _install_asset(
    project_root: Path | str,
    asset_type: str,
    name: str,
    *,
    wiki: WikiStore | None,
    lock_timeout: float | None = None,
    surface: str = "cli_context_install",
) -> InstallResult:
    """Internal: install a single asset of any type — commit-true (#1643).

    Bytes are extracted from git objects at HEAD
    (:meth:`WikiStore.copy_asset_at_commit`, the same mechanism as
    ``install --all``'s pinned restore), never from the wiki working tree,
    so the recorded ``wiki_commit`` pin always reproduces the installed
    bytes. Two gates precede extraction: the asset must exist at HEAD (an
    asset present only in the worktree — never committed — refuses with
    :class:`UncommittedAssetError` rather than pinning a commit that lacks
    it), and the asset's own worktree state must match HEAD (any
    modified/deleted/untracked path under the asset dir refuses the same
    way; dirt elsewhere in the wiki is ignored). All refusals fire before
    any dest mkdir / byte / lockfile write — zero residue. A crash DURING
    extraction can leave a partially-mirrored dest with no lockfile entry,
    the same residue class as the pre-#1643 copytree (the
    dest-without-entry hint below covers it).

    Concurrency contract: same-asset races accept last-write-wins on the
    lockfile entry. HEAD is read once per call and extraction reads that
    commit's immutable objects, so racers that observe the SAME HEAD write
    identical bytes regardless of interleaving — and a concurrent wiki
    *edit* can no longer bleed into the copy (strictly stronger than the
    worktree-copy era). Racers that observe DIFFERENT HEADs can still
    interleave files from two commits with the last lockfile write winning
    — the same class as before, reconciled by ``install --all``/update.
    Distinct-asset writers serialize cleanly on the lockfile sidecar lock
    and both entries survive.

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

    # Worktree presence is a boolean input to the HEAD-presence gate below,
    # not a raise site — "in the worktree but not at HEAD" must classify as
    # uncommitted, not absent (#1643).
    src = wiki.root / asset_type / validated
    worktree_present = src.is_dir()

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

    commit_hint = _commit_hint(wiki, asset_type, validated)

    # HEAD-presence gate (#1643): the pin must CONTAIN the asset. An asset
    # that exists only in the worktree would install bytes whose recorded
    # provenance points at a commit that lacks them entirely — the worst
    # shape of the unreproducible-pin bug (a `git clean` in the wiki then
    # destroys the only source while the lockfile claims a valid pin).
    try:
        wiki.asset_files_at_commit(wiki_commit, asset_type, validated)
    except AssetNotFoundError:
        if worktree_present:
            raise UncommittedAssetError(
                f"{asset_type}/{validated}: exists in the wiki working tree but "
                f"has never been committed, so the HEAD pin would not contain "
                f"it; {commit_hint}"
            ) from None
        raise AssetNotFoundError(f"{asset_type}/{validated} not in wiki at {wiki.root}") from None

    # Same-asset dirty gate (#1643): worktree state must equal HEAD for THIS
    # asset — otherwise the user-visible wiki bytes and the installed bytes
    # silently diverge. Skip-filter and scoping semantics live in
    # _asset_dirty_rels (shared with the update gate, #1652).
    dirty_rels = _asset_dirty_rels(wiki, asset_type, validated)
    if dirty_rels:
        shown = ", ".join(dirty_rels[:5])
        more = "" if len(dirty_rels) <= 5 else f", +{len(dirty_rels) - 5} more"
        raise UncommittedAssetError(
            f"{asset_type}/{validated}: wiki working tree differs from HEAD for "
            f"this asset ({len(dirty_rels)} file(s): {shown}{more}); install is "
            f"commit-true and records HEAD's bytes only — {commit_hint}"
        )

    # Gate A (ADR-0011 §5, #1247): wiki bytes are user-tier — secrets are
    # legal there — but dest is the git-tracked project_shared canonical.
    # The scan reads git objects at the pin (#1643): exactly the immutable
    # bytes the extractor below will write (scan set == write set, and no
    # scan→write TOCTOU). Runs before any mkdir so refusal leaves zero
    # residue (no empty type dir, no dest bytes, no lockfile entry).
    _gate_a_scan_pinned_asset(
        wiki,
        wiki_commit,
        asset_type,
        validated,
        surface=surface,
        project_root=project_root,
        remediation_hint=(
            f"The wiki HEAD commit {wiki_commit[:12]} ships a secret in "
            f"{asset_type}/{validated}; remove it from the asset, commit the "
            f"fix in the wiki, and re-run install."
        ),
    )

    # Commit-true extraction (#1643): bytes come from git objects at the
    # pinned commit, never the worktree, so the lockfile's digests/
    # files_commit claims below are literally true. copy_asset_at_commit
    # does its own dest.parent.mkdir + tmpdir-adjacent materialization and
    # applies the same skip predicate as the old copytree.
    digest_map = wiki.copy_asset_at_commit(wiki_commit, asset_type, validated, dest)

    # files= derives from the extractor's WRITTEN set (#1247 id 15) — and the
    # source is now an immutable commit, so no concurrent wiki addition can
    # be absorbed into the manifest as if wiki-shipped.
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
    surface: str = "cli_context_update",
) -> UpdateResult:
    """Refresh ``skills/<name>/`` **at wiki HEAD** into ``<project>/.memtomem/skills/<name>/``.

    Commit-true (#1652): bytes are extracted from git objects at HEAD, so
    an asset whose wiki working tree differs from HEAD refuses with
    :class:`UncommittedAssetError` (never force-able). No-op when wiki
    HEAD already matches the lockfile pin — returned BEFORE the wiki-side
    gates (nothing would be written). Refuses when local dest edits would
    be clobbered, unless ``force=True`` (which preserves each dirty file
    as ``<file>.bak`` before overwriting).

    ``surface`` names the ingress in the Gate-A privacy audit log
    (#1246/#1248 real-ingress rule): the CLI keeps the default, the web
    route passes ``"web_context_update"`` so a browser-triggered block is
    not misattributed to a CLI event.
    """
    return _update_asset(
        project_root,
        "skills",
        name,
        wiki=wiki,
        force=force,
        lock_timeout=lock_timeout,
        surface=surface,
    )


def update_agent(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
    force: bool = False,
    lock_timeout: float | None = None,
    surface: str = "cli_context_update",
) -> UpdateResult:
    """Refresh ``agents/<name>/`` at wiki HEAD into ``<project>/.memtomem/agents/<name>/``."""
    return _update_asset(
        project_root,
        "agents",
        name,
        wiki=wiki,
        force=force,
        lock_timeout=lock_timeout,
        surface=surface,
    )


def update_command(
    project_root: Path | str,
    name: str,
    *,
    wiki: WikiStore | None = None,
    force: bool = False,
    lock_timeout: float | None = None,
    surface: str = "cli_context_update",
) -> UpdateResult:
    """Refresh ``commands/<name>/`` at wiki HEAD into ``<project>/.memtomem/commands/<name>/``."""
    return _update_asset(
        project_root,
        "commands",
        name,
        wiki=wiki,
        force=force,
        lock_timeout=lock_timeout,
        surface=surface,
    )


def _update_asset(
    project_root: Path | str,
    asset_type: str,
    name: str,
    *,
    wiki: WikiStore | None,
    force: bool = False,
    lock_timeout: float | None = None,
    surface: str = "cli_context_update",
) -> UpdateResult:
    """Internal: refresh a single installed asset of any type — commit-true (#1652).

    Pipeline:

    1. Validate ``name`` and project root.
    2. Read the existing lockfile entry — ``NotInstalledError`` if absent.
    3. Pin wiki HEAD as ``new_commit`` once (concurrent ``git pull`` in the
       wiki cannot make the recorded commit drift mid-update).
    4. **True no-op short-circuit**: when ``new_commit`` matches the lockfile
       pin, return early *without touching the lockfile*. ``installed_at``
       is echoed from the existing entry; ``was_no_op=True``. The no-op
       runs BEFORE the wiki-side gates below: the gates protect a write,
       and a matching pin writes nothing — uncommitted wiki edits must not
       fail a refresh that would be a no-op anyway (the CLI surfaces an
       asset-scoped hint instead).
    5. **HEAD-presence gate** (#1652, mirrors #1643 install): the asset
       must exist at ``new_commit``. Present only in the worktree —
       committed history has dropped it — refuses with
       :class:`UncommittedAssetError` (the pin the update would record
       wouldn't contain the asset); absent everywhere raises
       :class:`AssetNotFoundError`.
    6. **Same-asset dirty gate** (#1652): any worktree divergence from HEAD
       under the asset dir (via :func:`_asset_dirty_rels`) refuses with
       :class:`UncommittedAssetError`. NOT force-able — ``force`` only
       overrides project-side (dest) edits; the remedy for wiki-side dirt
       is committing it. Dirt elsewhere in the wiki never blocks.
    7. Delegate to :func:`_apply_update`, which classifies the dest tree
       via :func:`is_asset_dirty` immediately before its gates (#1247
       id 13) and then refuses or writes — extracting at ``new_commit``.

    The split lets ``mm context update --all`` (commit 4) reuse step 7
    after rendering a preview classification across all known projects
    up front (its wiki-side gates run once per batch in the CLI, not per
    project — the wiki state is asset-global).
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

    new_commit = wiki.current_commit()
    dest = project_root / ".memtomem" / asset_type / validated

    # RESIDUAL (follow-up): update moves the pin to HEAD whenever it differs
    # from the recorded pin, WITHOUT checking that HEAD descends from it. After
    # a wiki reset / force-pull to older-or-divergent history HEAD is behind (or
    # off to the side of) the pin, so this would move the pin BACKWARD — a
    # silent downgrade, the same forward-only gap `classify_status` now guards
    # against via `WikiStore.commit_is_ancestor`. Fixing it here (and in
    # `_classify_for_all_update`'s preview parity) needs a new refuse verdict
    # threaded through both the single-asset and `--all` state machines, so it
    # is deliberately left out of the status-classification change.
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

    commit_hint = _commit_hint(wiki, asset_type, validated)

    # HEAD-presence gate (#1652, mirrors #1643 install): the pin the update
    # would record must CONTAIN the asset. Worktree presence is a boolean
    # input, not a raise site — "in the worktree but not at HEAD" must
    # classify as uncommitted, not absent.
    try:
        wiki.asset_files_at_commit(new_commit, asset_type, validated)
    except AssetNotFoundError:
        if (wiki.root / asset_type / validated).is_dir():
            raise UncommittedAssetError(
                f"{asset_type}/{validated}: exists in the wiki working tree but "
                f"is not present at HEAD, so the pin the update would record "
                f"would not contain it; {commit_hint}"
            ) from None
        raise AssetNotFoundError(f"{asset_type}/{validated} not in wiki at {wiki.root}") from None

    # Same-asset dirty gate (#1652): worktree state must equal HEAD for THIS
    # asset — otherwise the user-visible wiki bytes and the refreshed bytes
    # silently diverge. Deliberately ignores ``force``: that flag overrides
    # project-side (dest) edits, never wiki-side dirt (the remedy is
    # committing). Skip-filter/scoping semantics live in _asset_dirty_rels.
    dirty_rels = _asset_dirty_rels(wiki, asset_type, validated)
    if dirty_rels:
        shown = ", ".join(dirty_rels[:5])
        more = "" if len(dirty_rels) <= 5 else f", +{len(dirty_rels) - 5} more"
        raise UncommittedAssetError(
            f"{asset_type}/{validated}: wiki working tree differs from HEAD for "
            f"this asset ({len(dirty_rels)} file(s): {shown}{more}); update is "
            f"commit-true and records HEAD's bytes only — {commit_hint}"
        )

    return _apply_update(
        project_root,
        asset_type,
        validated,
        wiki=wiki,
        dest=dest,
        wiki_commit=new_commit,
        lock_entry=lock_entry,
        force=force,
        surface=surface,
        lock_timeout=lock_timeout,
    )


def _apply_update(
    project_root: Path,
    asset_type: str,
    name: str,
    *,
    wiki: WikiStore,
    dest: Path,
    wiki_commit: str,
    lock_entry: dict[str, Any],
    force: bool,
    surface: str = "cli_context_update",
    lock_timeout: float | None = None,
) -> UpdateResult:
    """Execute an update against apply-time dirty evidence — commit-true (#1652).

    Bytes are extracted from git objects at ``wiki_commit``
    (:meth:`WikiStore.copy_asset_at_commit`), never from the wiki working
    tree, so the lockfile's ``wiki_commit``/``digests`` claims below are
    literally true — pin==bytes holds by construction.

    Pre-conditions enforced by callers (``_update_asset`` for the single-
    asset path, ``mm context update --all`` orchestration for the batch
    path):

    - ``lock_entry`` is non-None (``NotInstalledError`` is raised earlier).
    - The no-op case (``lock_entry["wiki_commit"] == wiki_commit``) was
      already short-circuited; this helper unconditionally writes.
    - The wiki-side commit-true gates (HEAD presence, same-asset dirt —
      #1652) already ran in the caller; this helper only enforces
      dest-side gates. On the ``--all`` path those gates fire once per
      batch (before the confirm prompt), so wiki dirt appearing DURING the
      prompt is not re-checked here — deliberate: the extraction below
      reads immutable objects at ``wiki_commit``, so the written bytes
      stay pin-reproducible regardless; only the decide-time divergence
      check is prompt-stale, the same acceptance class as install's
      sub-second gate→extract window.

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
    each dirty file is preserved alongside the extracted bytes as
    ``<file>.bak`` before the copy. An existing dest whose entry has an
    unusable ``installed_at`` (``reason == "never_installed"``) refuses
    the same way — nothing is provably clean — and ``--force`` preserves
    EVERY current dest file as ``.bak`` (#1247). ``shutil.copy2`` is used
    so the user's edit-mtime survives onto the ``.bak``
    (atomic_write_bytes would lose it). After the copy, dest files absent
    from the pinned commit's extracted set are reconciled away
    (:func:`_reconcile_removed_files`,
    #1247) so the refreshed tree mirrors the pin instead of growing
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
            # The migrate CLI's asset-type argument is a plural Choice
            # ("agents", ...) — embed the plural verbatim so the hint is
            # runnable as-is (the singular trips Click's invalid-choice
            # error, same shape as the #895 privacy_scan fix).
            if flat_dirty and not force:
                raise StaleInstallError(
                    f"{asset_type}/{name}: flat-layout file {flat_path.name} has local "
                    f"edits (or no provable install time) and the dir-layout install "
                    f"would stop it being served; run `mm context migrate {asset_type} "
                    f"{name}` first, or pass --force to write the dir layout anyway "
                    f"(the flat file is kept on disk but stops being served)"
                )
            logger.warning(
                "%s/%s: dir layout will shadow flat file %s (dir wins at fan-out); "
                "run `mm context migrate %s %s` to consolidate",
                asset_type,
                name,
                flat_path,
                asset_type,
                name,
            )

    # Gate A (ADR-0011 §5, #1247): both scans precede the .bak loop — the
    # first dest mutation with no rollback — so a privacy refusal leaves
    # no .bak, no copied bytes, and (upserts trail copies) no lockfile drift.
    # The scan reads git objects at the pin (#1652): exactly the immutable
    # bytes the extractor below will write (scan set == write set, no
    # scan→write TOCTOU).
    _gate_a_scan_pinned_asset(
        wiki,
        wiki_commit,
        asset_type,
        name,
        surface=surface,
        project_root=project_root,
        remediation_hint=(
            f"The wiki commit {wiki_commit[:12]} ships a secret in "
            f"{asset_type}/{name}; remove it from the asset, commit the "
            f"fix in the wiki, and re-run update."
        ),
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
        # would lose it. Race window between copy2 and the pinned
        # extraction is sub-ms; acceptable for v1. Overwrite-if-exists
        # policy (prior .bak from earlier --force gets replaced).
        shutil.copy2(f, bak)
        bak_paths.append(bak)

    # Commit-true extraction (#1652): bytes come from git objects at the
    # pinned commit, never the worktree, so the lockfile's digests/
    # files_commit claims below are literally true. copy_asset_at_commit
    # does its own dest.parent.mkdir + tmpdir-adjacent materialization and
    # applies the same skip predicate as the old copytree.
    digest_map = wiki.copy_asset_at_commit(wiki_commit, asset_type, name, dest)

    # Mirror semantics (#1247): drop dest files the pin no longer ships.
    # Membership is the extractor's RETURNED written set, not a re-walk of
    # the wiki (#1247 id 15 impl gate; the source is now an immutable
    # commit — #1652 — so the walk-a-different-snapshot hazard the
    # worktree copy had is closed by construction, and the returned map
    # remains the single provenance surface).
    # The map shares the extractor's skip rules by construction, covering
    # B5's symlink concern (a wiki file replaced by a symlink is absent
    # from the map, so the old regular file in dest reconciles away). The
    # guard epoch is the PRE-update install timestamp (the same basis
    # ``dirty_report`` classified against) — the freshly captured value
    # below is >= every current mtime and would approve deleting
    # anything. ``old_digests`` likewise comes from the OLD entry.
    files_removed = _reconcile_removed_files(
        dest,
        src_has=lambda rel: rel in digest_map,
        old_installed_at_epoch=installed_at_epoch_from_entry(lock_entry),
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
    up front: ``wiki.current_commit()`` and the HEAD-presence gate
    (#1652) both happen before the per-project loop, so every project is
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

    The asset's presence at HEAD is checked once up front. If it's
    missing everywhere, callers get :class:`AssetNotFoundError` (or
    :class:`UncommittedAssetError` when it exists only in the worktree)
    here, *before* any project loop runs — preventing a confusing
    per-project error storm.

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

    # HEAD-presence gate (#1652, mirrors the single-asset update): the pin
    # every subsequent _apply_update would record must CONTAIN the asset.
    # Fires once, before the project loop (same storm-prevention rationale
    # as the old worktree existence check it replaces). Worktree-only
    # presence classifies as uncommitted, not absent. The same-asset dirty
    # gate is deliberately NOT here: its trigger — "at least one row would
    # write" — is a function of the classification output, so the CLI
    # applies it post-classify (unchanged-only batches just hint).
    try:
        wiki.asset_files_at_commit(new_commit, asset_type, name)
    except AssetNotFoundError:
        if (wiki.root / asset_type / name).is_dir():
            raise UncommittedAssetError(
                f"{asset_type}/{name}: exists in the wiki working tree but is "
                f"not present at HEAD, so the pin the update would record "
                f"would not contain it; {_commit_hint(wiki, asset_type, name)}"
            ) from None
        raise AssetNotFoundError(f"{asset_type}/{name} not in wiki at {wiki.root}") from None

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
                f"flat layout with local edits; run `mm context migrate {asset_type} {name}` first"
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
    - ``"skip"`` — dest exists and is clean (matches its recorded
      install). Default behavior is no-op. Under ``--force``,
      :func:`_apply_pinned_install` re-extracts the pin ONLY when the
      recorded digests equal the pin's bytes (a true no-op that also
      reconciles stale dest-only leftovers, #1247); if they DIFFER — a
      clean install taken off a dirty wiki working tree, whose bytes the
      pin never described (ADR-0008 ``wiki_commit`` provenance
      imprecision) — the row is left untouched (``was_no_op``) rather than
      silently swapped to the pin's bytes with no ``.bak``. (A row that
      goes dirty between classify and apply re-classifies ``refuse``/dirty,
      not ``skip``, so its ``.bak`` is still taken — #1247 id 13.)
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
                            f"{asset_type} {name}` first"
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
    - ``state="skip"`` + ``force=True``: re-classified below. If still
      CLEAN and the recorded digests DIFFER from the pin's bytes (a clean
      install taken off a dirty wiki working tree — ADR-0008 ``wiki_commit``
      provenance imprecision), returns a no-op :class:`InstallResult`
      (``was_no_op=True``) WITHOUT re-extracting, so the installed bytes
      are not silently swapped for the pin's with no ``.bak``. If the
      recorded digests EQUAL the pin's bytes, it falls through to a normal
      re-extraction (a content no-op that still reconciles stale dest-only
      leftovers, #1247).
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
      clean-at-classify row edited mid-prompt still gets its ``.bak``;
      a row that re-classifies clean is the ``was_no_op`` case above.
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

    # Data-safety no-op (ADR-0008 wiki_commit provenance imprecision): the
    # legacy single-install (pre-#1643) and update (pre-#1652) paths copied
    # the wiki WORKING TREE while pinning HEAD, so entries created by them off
    # a *dirty* wiki hold bytes the recorded pin never described (both verbs
    # are commit-true now and can no longer mint such entries — only
    # existing lockfiles keep them alive). Such a row
    # re-classifies CLEAN here (dest == its recorded
    # digests), and --force would then re-extract the pin over it — SILENTLY
    # swapping the installed bytes for the pin's, with no .bak (files_to_bak is
    # empty on a clean row). `install --all` reproduces the RECORDED install, so
    # leave that row untouched. The check is narrow on purpose: only when the
    # recorded digests (== the clean dest) differ from the bytes the pin would
    # extract. When they MATCH (the dest already equals the pin), fall through
    # so --force's re-extraction can still reconcile away stale dest-only
    # leftovers (#1247). A pre-digest entry (``digests_from_entry`` → None) has
    # no recorded map, so the CLEAN DEST is hashed in its place (#1512): the
    # row just classified clean, so the dest bytes ARE the bytes the install
    # wrote — the same identity a recorded map would carry — and a legacy row
    # gains the same no-op-on-divergence protection instead of the pre-#1479
    # silent, .bak-less re-extract. (Its dest==pin fall-through then re-extracts
    # identical bytes and upserts a digest-bearing entry, upgrading the row.)
    # A VALID EMPTY map (``{}`` — a digest-bearing install that copied zero
    # files) is honored, so a pin that would extract files over it counts as a
    # divergence rather than a fall-through (``is not None``, not truthiness —
    # Codex gate). (Reachable only under --force: without it the caller skips
    # clean rows; a row edited mid-prompt re-classifies "dirty" here, not
    # "clean", so the #1247 id 13 .bak guarantee is preserved.)
    if report.reason == "clean":
        recorded_digests = digests_from_entry(classification.lock_entry or {})
        pin_digests = {
            rel: hashlib.sha256(
                wiki.read_asset_file_at_commit(pin, asset_type, name, rel)
            ).hexdigest()
            for rel in wiki.asset_files_at_commit(pin, asset_type, name)
            if not is_copy_skipped_rel(rel)
        }
        if recorded_digests is not None:
            effective_digests = recorded_digests
        else:
            try:
                effective_digests = {
                    f.relative_to(dest).as_posix(): hashlib.sha256(f.read_bytes()).hexdigest()
                    for f in iter_installed_files(dest)
                }
            except OSError as exc:
                # Fail-closed like the walk_failed gate above: an unhashable
                # dest cannot be proven equal to the pin, and a wrong guess
                # either destroys bytes (fall through) or silently masks a
                # permissions problem (no-op) — refuse loud instead (the
                # --all CLI loop degrades this to a red row, file intact).
                raise StaleInstallError(
                    f"{asset_type}/{name}: cannot hash installed files under "
                    f"{dest} ({exc}) to compare against the pin — refusing to "
                    f"re-extract even with --force; fix permissions and retry"
                ) from exc
        if effective_digests != pin_digests:
            return InstallResult(
                asset_type=cast('Literal["skills", "agents", "commands"]', asset_type),
                name=name,
                wiki_commit=pin,
                installed_at=cast(str, (classification.lock_entry or {}).get("installed_at", "")),
                dest=dest,
                files_written=0,
                was_no_op=True,
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
            # The migrate CLI's asset-type argument is a plural Choice
            # ("agents", ...) — embed the plural verbatim so the hint is
            # runnable as-is (the singular trips Click's invalid-choice
            # error, same shape as the #895 privacy_scan fix).
            if flat_dirty and not force:
                raise StaleInstallError(
                    f"{asset_type}/{name}: flat-layout file {flat_path.name} has local "
                    f"edits (or no provable install time) and the dir-layout install "
                    f"would stop it being served; run `mm context migrate {asset_type} "
                    f"{name}` first, or pass --force to write the dir layout anyway "
                    f"(the flat file is kept on disk but stops being served)"
                )
            logger.warning(
                "%s/%s: dir layout will shadow flat file %s (dir wins at fan-out); "
                "run `mm context migrate %s %s` to consolidate",
                asset_type,
                name,
                flat_path,
                asset_type,
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
            old_installed_at_epoch=installed_at_epoch_from_entry(entry),
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
