"""Multi-project discovery for the context-gateway UI.

PR2 of the multi-project context UI series — see
``memtomem-docs/memtomem/planning/multi-project-context-ui-rfc.md``.

This module is read-only. It enumerates project scopes from three sources
(server cwd, the user-registered ``known_projects.json``, and an opt-in
scan of ``~/.claude/projects/``), deduplicates them by resolved path, and
returns ``ProjectScope`` records keyed by a stable ``scope_id``.

Mutating routes that target a specific scope (`POST /api/context/skills`
etc.) ride on top of this in PR3; PR2 only ships the discovery + GET
contract plus the ``known_projects.json`` POST/DELETE endpoints.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import logging
import os
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from memtomem.context._atomic import atomic_write_bytes

logger = logging.getLogger(__name__)

__all__ = [
    "ProjectScope",
    "KnownProjectsStore",
    "compute_scope_id",
    "discover_project_scopes",
]


# ── Scope id ─────────────────────────────────────────────────────────────


def _normalize_for_scope_id(path: Path) -> str:
    """Produce the stable string used as input to ``compute_scope_id``.

    ``Path.resolve()`` collapses symlinks and trailing-slash variants.
    ``os.path.normcase`` lowercases on Windows but is a no-op on POSIX,
    so we force-lowercase on macOS too: APFS is case-insensitive but
    case-preserving, and Python's ``realpath`` does not canonicalize
    casing — without explicit folding, ``/Users/Foo`` and ``/Users/foo``
    would hash to distinct scope_ids on the same FS inode (RFC
    §Decision 4 promises case-insensitive ids on macOS / Windows).
    Linux paths stay case-sensitive — `/users/foo` and `/Users/foo` are
    genuinely different dirs there.
    """
    s = os.path.normcase(str(Path(path).resolve()))
    if sys.platform == "darwin":
        s = s.lower()
    return s


def compute_scope_id(path: Path) -> str:
    """Derive the stable ``p-<sha12>`` id for a project root.

    12 hex chars = 48 bits. Birthday-bound 50% collision lands at ~16M
    projects — effectively zero in a single-user ``mm web`` deployment.
    """
    digest = hashlib.sha256(_normalize_for_scope_id(path).encode("utf-8")).hexdigest()
    return f"p-{digest[:12]}"


# ── ProjectScope ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProjectScope:
    """One row in the project-discovery list.

    The shape mirrors the JSON payload returned by ``GET /api/context/projects``;
    the route serializes ``ProjectScope`` instances directly via dataclass
    conversion. Adding a field here updates the wire schema — match the
    response in ``test_routes_context_projects.py``.
    """

    scope_id: str
    label: str
    root: Path | None
    tier: Literal["user", "project"]
    sources: tuple[str, ...]
    missing: bool = False
    experimental: bool = False


# ── known_projects.json store ───────────────────────────────────────────


_KNOWN_PROJECTS_VERSION = 1


@contextmanager
def _file_lock(lock_path: Path) -> Iterator[None]:
    """Cross-process exclusive lock on a sidecar lockfile.

    Locking the data file directly does **not** survive ``os.replace`` —
    the lock is on the inode, and the rename swaps the inode mid-operation
    so concurrent writers race on stale fds. The fix is to lock a sibling
    (`feedback_sidecar_lockfile_for_replaced_files.md` PR #548). The
    lockfile itself is never renamed, so its inode is stable.
    """
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _lock_path_for(data_path: Path) -> Path:
    return data_path.parent / f".{data_path.name}.lock"


@dataclass(frozen=True)
class _KnownProjectEntry:
    root: Path
    added_at: str
    label: str | None


class KnownProjectsStore:
    """Read / append / delete entries in ``known_projects.json``.

    All mutations hold an exclusive sidecar lock and write atomically via
    ``tmp + os.replace``. Two concurrent writers are last-write-wins, but
    the file never becomes invalid JSON and never disappears mid-rename.
    """

    def __init__(self, path: Path):
        # ``Path.expanduser`` so users can configure ``~/.memtomem/...``
        # in pydantic settings without explicit expansion at the call site.
        self._path = Path(path).expanduser()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> list[_KnownProjectEntry]:
        """Return entries in registration order; ``[]`` if file missing or unreadable."""
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            logger.warning("known_projects: read failed: %s", exc)
            return []

        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("known_projects: invalid JSON, ignoring file: %s", exc)
            return []

        if not isinstance(doc, dict) or doc.get("version") != _KNOWN_PROJECTS_VERSION:
            logger.warning(
                "known_projects: unexpected version %r, ignoring",
                doc.get("version") if isinstance(doc, dict) else None,
            )
            return []

        entries: list[_KnownProjectEntry] = []
        for item in doc.get("projects", []):
            if not isinstance(item, dict):
                continue
            root = item.get("root")
            if not isinstance(root, str) or not root:
                continue
            entries.append(
                _KnownProjectEntry(
                    root=Path(root),
                    added_at=str(item.get("added_at") or ""),
                    label=item.get("label") if isinstance(item.get("label"), str) else None,
                )
            )
        return entries

    def add(self, root: Path, label: str | None = None) -> _KnownProjectEntry:
        """Register *root*. Idempotent — re-registering an existing root is a no-op
        (returns the existing entry).
        """
        normalized = Path(root).expanduser()
        with _file_lock(_lock_path_for(self._path)):
            entries = self.load()
            for existing in entries:
                if _normalize_for_scope_id(existing.root) == _normalize_for_scope_id(normalized):
                    return existing
            new_entry = _KnownProjectEntry(
                root=normalized,
                added_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                label=label,
            )
            self._write(entries + [new_entry])
            return new_entry

    def remove_by_scope_id(self, scope_id: str) -> bool:
        """Drop the entry whose computed scope_id matches. Returns True if removed.

        Stale entries (root no longer a directory) are removable — matching
        is on ``compute_scope_id(entry.root)`` which is path-derived, not
        existence-derived.
        """
        with _file_lock(_lock_path_for(self._path)):
            entries = self.load()
            kept = [e for e in entries if compute_scope_id(e.root) != scope_id]
            if len(kept) == len(entries):
                return False
            self._write(kept)
            return True

    def _write(self, entries: list[_KnownProjectEntry]) -> None:
        doc = {
            "version": _KNOWN_PROJECTS_VERSION,
            "projects": [
                {
                    "root": str(e.root),
                    "added_at": e.added_at,
                    "label": e.label,
                }
                for e in entries
            ],
        }
        atomic_write_bytes(
            self._path,
            json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8"),
        )


# ── Discovery ────────────────────────────────────────────────────────────


_CLAUDE_PROJECTS_DIR = Path("~/.claude/projects").expanduser()


def _decode_claude_project_dirname(name: str) -> Path | None:
    """Decode a ``~/.claude/projects/<encoded>`` directory name into a path.

    The encoding is "every ``/`` becomes ``-``", so paths with literal
    dashes round-trip to the wrong spelling. The caller filters via
    ``Path.is_dir()`` to drop misdecoded entries (RFC §Decision 2).
    """
    if not name.startswith("-"):
        return None
    decoded = name.replace("-", "/")
    return Path(decoded)


def _discover_claude_projects() -> list[Path]:
    if not _CLAUDE_PROJECTS_DIR.is_dir():
        return []
    candidates: list[Path] = []
    for child in _CLAUDE_PROJECTS_DIR.iterdir():
        if not child.is_dir():
            continue
        decoded = _decode_claude_project_dirname(child.name)
        if decoded is None:
            continue
        # Filter: only paths that actually resolve to a directory.
        # The decoder is fragile around dashes so most non-cwd hits drop here.
        if not decoded.is_dir():
            continue
        candidates.append(decoded)
    return candidates


def _label_for(root: Path) -> str:
    name = root.name or str(root)
    return name


def discover_project_scopes(
    cwd: Path,
    known_projects_file: Path,
    *,
    experimental_claude_projects_scan: bool,
) -> list[ProjectScope]:
    """Enumerate all project scopes the UI should render, in display order.

    Server cwd is always first (so the user's primary working tree is
    visible even before Add Project is used). Entries with the same
    resolved path coalesce; ``sources`` then carries the union of every
    place each scope was discovered.
    """
    # Resolved-path → (display_path, sources, missing)
    by_resolved: dict[str, tuple[Path, set[str], bool]] = {}
    order: list[str] = []

    def _add(display: Path, source: str, *, missing: bool) -> None:
        # Resolve aggressively — strict=False so a stale known-project root
        # that no longer exists still gets a scope_id (so the user can
        # DELETE it via the UI).
        try:
            resolved = display.resolve(strict=False)
        except OSError:
            resolved = display
        key = _normalize_for_scope_id(resolved)
        if key in by_resolved:
            _, sources, was_missing = by_resolved[key]
            sources.add(source)
            # `missing` only stays true if every source flagged it missing.
            by_resolved[key] = (resolved, sources, was_missing and missing)
        else:
            by_resolved[key] = (resolved, {source}, missing)
            order.append(key)

    # 1. Server cwd — always first, never missing (the process is running there).
    _add(cwd, "server-cwd", missing=False)

    # 2. User-registered roots from known_projects.json.
    store = KnownProjectsStore(known_projects_file)
    for entry in store.load():
        _add(entry.root, "known-projects", missing=not entry.root.is_dir())

    # 3. Opt-in scan of ~/.claude/projects/ — silently skipped when the flag is
    #    False so the default discovery path stays cheap.
    if experimental_claude_projects_scan:
        for decoded in _discover_claude_projects():
            _add(decoded, "claude-projects", missing=False)

    scopes: list[ProjectScope] = []
    for key in order:
        resolved, sources, missing = by_resolved[key]
        # ``experimental`` is true iff the *only* source is the opt-in scan.
        # cwd / known-projects unions clear the flag so the most-trusted
        # source wins for display purposes.
        experimental = sources == {"claude-projects"}
        scopes.append(
            ProjectScope(
                scope_id=compute_scope_id(resolved),
                label="Server CWD" if "server-cwd" in sources else _label_for(resolved),
                root=resolved,
                tier="project",
                sources=tuple(sorted(sources)),
                missing=missing,
                experimental=experimental,
            )
        )
    return scopes


# ── Validation helpers ──────────────────────────────────────────────────


_MARKER_DIRS = (".claude", ".gemini", ".agents", ".memtomem")


def has_runtime_marker(root: Path) -> bool:
    """Return True if *root* contains any recognized runtime marker directory.

    Used by ``POST /api/context/known-projects`` to decide whether to
    emit a warning ("looks like a non-project directory") without rejecting
    the registration outright. Empty parents are valid — the user might be
    setting up a fresh checkout.
    """
    return any((root / m).is_dir() for m in _MARKER_DIRS)
