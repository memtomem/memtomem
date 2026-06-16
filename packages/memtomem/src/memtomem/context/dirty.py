"""Detect drift between installed asset bytes and their lockfile snapshot.

Pure classifier used by ``mm context update`` to decide whether the
on-disk tree at ``<project>/.memtomem/<type>/<name>/`` still matches the
wiki state recorded in :class:`memtomem.context.lockfile.Lockfile`.

When the entry carries a valid per-file digest map
(:func:`memtomem.context.lockfile.digests_from_entry`, #1247 id 15), the
compare rule is **byte equality**: a file is dirty iff its current
SHA-256 differs from the digest recorded for the bytes the installing
operation wrote — mtime is not consulted at all on this branch (mixing
it in would re-import its false positives for zero detection gain). This
closes the during-install absorption window: a concurrent edit landing
at any point after a file's write leaves current bytes ≠ recorded digest
→ dirty, where the scalar ``installed_at`` capture absorbed it →
permanently clean → silent clobber on the next update. A file that
cannot be **read** on this branch classifies dirty with a warning —
"cannot prove clean" must protect, never crash the whole status walk and
never silently pass; the underlying OSError then surfaces loudly at
mutation time, before the first dest write (Gate A scans ``--force``'s
``.bak`` set and ``privacy_scan`` raises on unreadable files).

For **legacy entries** (no/invalid/stale digests) the rule remains
strict ``mtime > installed_at_epoch`` — only files whose modification
time is *strictly* later than the lockfile's ``installed_at`` are
flagged dirty. Equality is clean. ``installed_at`` is captured from the
filesystem itself by
:func:`memtomem.context._atomic.installed_at_from_dest` (``max
st_mtime_ns`` ceiled to microsecond), so on every platform the install's
own writes round-trip to a value ``<= installed_at_epoch`` and the
classifier can't false-positive on a fresh install — including Windows,
where NTFS ``FILETIME`` is a different timer from Python's wall clock
(#634).

Skip rules live in :func:`memtomem.context._atomic.iter_installed_files`
(shared with the capture helper above): ``.git``, ``.DS_Store``,
``__pycache__``, ``.bak`` and symlinks are not part of the canonical
install surface, so user edits to such entries don't make the asset
dirty and don't perturb the captured ``installed_at`` either.

This module is read-only. The execute path (``mm context update``)
consumes a :class:`DirtyReport` and writes ``.bak`` files / overwrites
the dest tree separately.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from memtomem.context._atomic import iter_installed_files
from memtomem.context.lockfile import Lockfile, digests_from_entry, manifest_from_entry

logger = logging.getLogger(__name__)

__all__ = [
    "DirtyReason",
    "DirtyReport",
    "is_asset_dirty",
]


DirtyReason = Literal["clean", "dirty", "never_installed", "missing_dest"]


@dataclass(frozen=True)
class DirtyReport:
    """Outcome of a dirty check on a single installed asset.

    - ``reason="clean"`` — dest exists and no checked file diverged:
      on digest entries every file's current SHA-256 equals its recorded
      digest, on legacy entries every file's mtime is
      ``<= installed_at_epoch``; and no recorded file is missing.
    - ``reason="dirty"`` — at least one checked file diverged (digest
      mismatch / unreadable / unrecorded addition on digest entries,
      ``mtime > installed_at_epoch`` on legacy entries; paths in
      ``dirty_files``) and/or at least one recorded file is gone from
      disk (paths in ``missing_files``) — a user deletion is a local
      edit too (#1247).
    - ``reason="never_installed"`` — no usable lockfile entry;
      ``installed_at`` is ``None`` and ``dirty_files`` / ``checked_files``
      are empty. Returned for "no entry at all" and "entry exists but
      missing / non-string / unparseable ``installed_at``" — all are
      unrecoverable states (#1247: an unparseable ISO string previously
      crashed with ``ValueError`` instead of degrading like its
      non-string siblings). Deliberately NOT widened to "digests valid
      but installed_at malformed" — unprovable-record semantics stay
      uniform, and the ``digests_installed_at == installed_at`` pairing
      makes such an entry degrade anyway.
    - ``reason="missing_dest"`` — lockfile entry exists but
      ``<project>/.memtomem/<type>/<name>/`` was deleted; ``installed_at``
      is the lockfile value, ``dirty_files`` empty.

    ``missing_files`` derives from the digest map's keys on digest
    entries, and from the file manifest (see
    :func:`memtomem.context.lockfile.manifest_from_entry`) on legacy
    entries that carry one; pre-manifest entries keep deletions
    invisible.

    ``walk_failed`` is set with ``reason="dirty"`` when the dest tree could
    not be fully enumerated (an unreadable subtree, or a path vanishing
    mid-walk). The "dirty" verdict is then a FAIL-SAFE ("cannot prove
    clean"), not an enumerated diff: ``dirty_files`` / ``missing_files`` are
    incomplete because the at-risk set is unknown. Read-only callers (``mm
    context status``, the update/install previews) treat it as plain dirty;
    MUTATION callers must REFUSE on it even with ``--force`` — they cannot
    back up files they could not enumerate.
    """

    reason: DirtyReason
    installed_at: str | None
    dirty_files: tuple[Path, ...]
    checked_files: int
    missing_files: tuple[Path, ...] = ()
    walk_failed: bool = False

    def summary(self) -> str:
        """Human-readable local-edit summary for messages and status rows.

        Covers both edit classes so a missing-only report can't render as
        "0 file(s) modified locally" (#1247 design-gate M4).
        """
        parts: list[str] = []
        if self.dirty_files:
            parts.append(f"{len(self.dirty_files)} file(s) modified locally")
        if self.missing_files:
            parts.append(f"{len(self.missing_files)} file(s) deleted locally")
        if self.walk_failed:
            parts.append("tree could not be fully read (unreadable file or directory)")
        return " and ".join(parts) if parts else "0 file(s) changed"


def is_asset_dirty(
    project_root: Path | str,
    asset_type: str,
    name: str,
    *,
    lock_entry: dict[str, Any] | None = None,
) -> DirtyReport:
    """Classify an installed asset as clean / dirty / never_installed / missing_dest.

    Pure: walks the dest tree, reads file mtimes, returns a
    :class:`DirtyReport`. No writes, no lockfile mutations.

    ``lock_entry`` is optional caller injection — pass it when the caller
    already loaded the lockfile (e.g. ``mm context update --all``
    classification reuses one read across N projects). When omitted,
    we read the entry from ``<project>/.memtomem/lock.json``.
    """
    project_root_path = Path(project_root).expanduser()

    if lock_entry is None:
        lock_entry = Lockfile.at(project_root_path).read_entry(asset_type, name)

    if lock_entry is None:
        return DirtyReport(
            reason="never_installed",
            installed_at=None,
            dirty_files=(),
            checked_files=0,
        )

    # Parse before the dest probe so a malformed string degrades to
    # never_installed in EVERY branch, exactly like its non-string
    # siblings — previously a malformed string returned missing_dest when
    # the dest was gone but crashed with ValueError when it existed (#1247).
    installed_at = lock_entry.get("installed_at")
    installed_at_epoch: float | None = None
    if isinstance(installed_at, str):
        try:
            installed_at_epoch = datetime.fromisoformat(installed_at).timestamp()
        except ValueError:
            logger.warning(
                "%s/%s: lockfile installed_at %r is not ISO-8601; treating as never installed",
                asset_type,
                name,
                installed_at,
            )
    if installed_at_epoch is None:
        return DirtyReport(
            reason="never_installed",
            installed_at=None,
            dirty_files=(),
            checked_files=0,
        )

    dest = project_root_path / ".memtomem" / asset_type / name
    if not dest.is_dir():
        return DirtyReport(
            reason="missing_dest",
            installed_at=installed_at,
            dirty_files=(),
            checked_files=0,
        )

    dirty: list[Path] = []
    checked = 0
    present_rels: set[str] = set()
    missing: list[Path] = []
    # An unreadable subtree / entry that aborts the enumeration. The walker is
    # fail-closed (it raises), which is correct for the privacy-gate source
    # scan but would crash this read-only status walk over N projects. Catch it
    # here and degrade to "dirty" — "cannot prove clean" protects, and unlike
    # `missing` this fires on BOTH branches (a pre-manifest legacy entry has no
    # recorded set to surface a skipped subtree, so silently skipping would
    # report clean). Never push the skip down into the walker: that would relax
    # the gate's fail-closed contract too.
    walk_failed = False
    digests = digests_from_entry(lock_entry)
    try:
        if digests is not None:
            # Digest branch (#1247 id 15): byte equality against the SHA-256
            # recorded for the bytes the install wrote. mtime is deliberately
            # not consulted — it would re-import touch-only false positives
            # for zero detection gain. Deltas vs legacy: touch-only edit →
            # clean, backdated edit/addition → dirty, unreadable → dirty+warn.
            for file_path in iter_installed_files(dest):
                checked += 1
                rel = file_path.relative_to(dest).as_posix()
                present_rels.add(rel)
                recorded = digests.get(rel)
                if recorded is None:
                    dirty.append(file_path)  # local addition — never recorded
                    continue
                try:
                    blob = file_path.read_bytes()
                except OSError as exc:
                    # Fail-safe: cannot prove clean. The read error itself
                    # surfaces loudly pre-mutation (Gate A / copy2), not here —
                    # `mm context status` over N projects must stay usable.
                    logger.warning(
                        "%s/%s: cannot read %s for digest check (%s); classifying dirty",
                        asset_type,
                        name,
                        file_path,
                        exc,
                    )
                    dirty.append(file_path)
                    continue
                if hashlib.sha256(blob).hexdigest() != recorded:
                    dirty.append(file_path)
            # Deletion detection from the digest map's own keys — when digests
            # validate, `files` was written by the same upsert from the same
            # set; if a hand-edit makes them diverge, the digest map is the
            # single record we trust (mirrored on the reconcile side).
            missing = [dest / rel for rel in sorted(digests.keys() - present_rels)]
        else:
            # Legacy branch — pre-digest behavior verbatim.
            for file_path in iter_installed_files(dest):
                checked += 1
                present_rels.add(file_path.relative_to(dest).as_posix())
                if file_path.stat().st_mtime > installed_at_epoch:
                    dirty.append(file_path)

            # Deletion detection (#1247): a manifest-recorded file gone from
            # disk is a local edit. Valid-manifest entries only —
            # legacy/stale/malformed manifests degrade to the pre-manifest
            # behavior (deletions invisible).
            manifest = manifest_from_entry(lock_entry)
            if manifest is not None:
                missing = [dest / rel for rel in sorted(manifest - present_rels)]
    except OSError as exc:
        # An unreadable directory (search bit removed) or a path vanishing
        # mid-walk aborts the enumeration: we cannot enumerate the installed
        # tree, so we cannot prove the asset clean. Degrade to dirty instead of
        # crashing the status walk. Also covers the legacy branch's unguarded
        # `file_path.stat()` racing-delete.
        logger.warning(
            "%s/%s: cannot enumerate %s for dirty check (%s); classifying dirty",
            asset_type,
            name,
            dest,
            exc,
        )
        walk_failed = True

    return DirtyReport(
        reason="dirty" if (dirty or missing or walk_failed) else "clean",
        installed_at=installed_at,
        dirty_files=tuple(dirty),
        checked_files=checked,
        missing_files=tuple(missing),
        walk_failed=walk_failed,
    )
