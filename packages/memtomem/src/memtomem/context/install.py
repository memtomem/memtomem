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

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from memtomem.context._atomic import copy_tree_atomic
from memtomem.context._names import validate_name
from memtomem.context.lockfile import Lockfile, utcnow_iso8601_z
from memtomem.wiki.store import WikiStore

__all__ = [
    "AlreadyInstalledError",
    "AssetNotFoundError",
    "InstallResult",
    "install_agent",
    "install_command",
    "install_skill",
]


class AssetNotFoundError(RuntimeError):
    """Raised when the requested asset directory does not exist in the wiki."""


class AlreadyInstalledError(RuntimeError):
    """Raised when install would overwrite an existing lockfile entry or dest."""


@dataclass(frozen=True)
class InstallResult:
    """Outcome of a successful install. Display-oriented; not persisted."""

    asset_type: Literal["skills", "agents", "commands"]
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

    installed_at = utcnow_iso8601_z()
    lock.upsert_entry(
        asset_type,
        validated,
        wiki_commit=wiki_commit,
        installed_at=installed_at,
    )

    return InstallResult(
        asset_type=cast('Literal["skills", "agents", "commands"]', asset_type),
        name=validated,
        wiki_commit=wiki_commit,
        installed_at=installed_at,
        dest=dest,
        files_written=files_written,
    )
