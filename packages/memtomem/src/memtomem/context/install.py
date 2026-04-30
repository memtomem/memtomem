"""Install a single wiki asset into ``<project>/.memtomem/<type>/<name>/``.

Implements PR-B of ADR-0008. The wiki at ``~/.memtomem-wiki/`` is the
source of truth; an "install" is a copytree snapshot pinned to the wiki's
HEAD commit, recorded in :class:`memtomem.context.lockfile.Lockfile`.

PR-B exposes only :func:`install_skill`. Skill fan-out works end-to-end
through the existing :mod:`memtomem.context.skills` generators, so the
user can install a wiki skill and immediately have it appear under
``.claude/skills/`` etc. Agent and command install land in PR-C alongside
override resolution — without override-aware extraction the snapshot
exists on disk but does not flow through fan-out, which would surprise
users into thinking install is broken.

Install is intentionally non-destructive: if either a lockfile entry OR
the destination directory already exists, install refuses with a
classified error (see step 6 of the install pipeline). This forward-
protects ADR-0008 Invariant 2 ("manual edits are detected, not silently
clobbered") without depending on PR-D's mtime/dirty detection. PR-D's
``mm context update`` is the supported way to refresh an installed asset.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from memtomem.context._atomic import copy_tree_atomic
from memtomem.context._names import validate_name
from memtomem.context.lockfile import Lockfile, utcnow_iso8601_z
from memtomem.wiki.store import WikiStore

__all__ = [
    "AlreadyInstalledError",
    "AssetNotFoundError",
    "InstallResult",
    "install_skill",
]


class AssetNotFoundError(RuntimeError):
    """Raised when the requested asset directory does not exist in the wiki."""


class AlreadyInstalledError(RuntimeError):
    """Raised when install would overwrite an existing lockfile entry or dest."""


@dataclass(frozen=True)
class InstallResult:
    """Outcome of a successful install. Display-oriented; not persisted."""

    asset_type: str
    name: str
    wiki_commit: str
    installed_at: str
    dest: Path
    files_written: int


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


def _install_asset(
    project_root: Path | str,
    asset_type: str,
    name: str,
    *,
    wiki: WikiStore | None,
) -> InstallResult:
    validated = validate_name(name, kind=f"{asset_type.rstrip('s')} name")
    project_root = Path(project_root).expanduser()

    wiki = wiki if wiki is not None else WikiStore.at_default()
    wiki.require_exists()

    src = wiki.root / asset_type / validated
    if not src.is_dir():
        raise AssetNotFoundError(f"{asset_type}/{validated} not in wiki at {wiki.root}")

    wiki_commit = wiki.current_commit()
    installed_at = utcnow_iso8601_z()

    dest = project_root / ".memtomem" / asset_type / validated
    lock = Lockfile.at(project_root)
    existing = lock.read_entry(asset_type, validated)
    has_lock = existing is not None
    has_dest = dest.exists()
    if has_lock or has_dest:
        raise AlreadyInstalledError(
            f"{asset_type}/{validated}: "
            f"lockfile_entry={'yes' if has_lock else 'no'}, "
            f"dest={'yes' if has_dest else 'no'}; "
            f"`mm context update` is reserved for PR-D — "
            f"to reinstall now, remove BOTH .memtomem/{asset_type}/{validated}/ "
            f"AND the `{asset_type}.{validated}` entry from .memtomem/lock.json"
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    files_written = copy_tree_atomic(src, dest)

    lock.upsert_entry(
        asset_type,
        validated,
        wiki_commit=wiki_commit,
        installed_at=installed_at,
    )

    return InstallResult(
        asset_type=asset_type,
        name=validated,
        wiki_commit=wiki_commit,
        installed_at=installed_at,
        dest=dest,
        files_written=files_written,
    )
