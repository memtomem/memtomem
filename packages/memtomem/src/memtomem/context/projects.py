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

import hashlib
import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Literal

from memtomem.context._atomic import _file_lock, _lock_path_for, atomic_write_bytes

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
    # ``discover_project_scopes`` only ever emits "project"; the "user" arm was
    # never constructed (#1123 B7-5). Narrowed to the real domain so the type
    # stops advertising a tier the producer can't return.
    tier: Literal["project"]
    sources: tuple[str, ...]
    missing: bool = False
    experimental: bool = False


# ── known_projects.json store ───────────────────────────────────────────


_KNOWN_PROJECTS_VERSION = 1


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


# Frontier cap: a runaway backstop so a pathologically dashed name cannot
# explode the reconstruction. With per-step dedup, real names collapse to a
# handful of FS-confirmed branches far under this; the cap only fires on
# adversarial inputs, and hitting it raises ``_DecodeBudgetError`` (NOT a silent
# no-match) so the caller reports it distinctly.
_MAX_DECODE_CANDIDATES = 512


class _DecodeBudgetError(Exception):
    """FS-guided reconstruction exceeded the frontier budget.

    The encoded name is too ambiguous to reconstruct safely. Kept distinct from
    an empty (no-match) result so the caller does not misreport overflow as
    "no matching directory" and can point the user at ``known_projects.json``.
    """


def _encode_claude_project_path(root: PurePath) -> str:
    """Encode an absolute path the way Claude Code names ``~/.claude/projects/<dir>``.

    Claude Code replaces **every character outside ASCII** ``[A-Za-z0-9]`` in the
    absolute path with a single ``-`` — equivalent to
    ``re.sub(r"[^A-Za-z0-9]", "-", …)`` (anthropics/claude-code issue #19972).
    This is platform-agnostic and broader than it looks:

    * POSIX ``/`` and ``.`` collapse to ``-`` — AND so does ``_`` (verified
      empirically against a live ``~/.claude/projects/``; e.g. ``/a/.config`` →
      ``-a--config`` and a ``foo_bar`` segment → ``foo-bar``).
    * On Windows the ``\\`` separators and the drive ``:`` also become ``-``
      (``C:\\dev\\repo`` → ``C--dev-repo``); the drive colon is not special-cased.
    * Non-ASCII characters (Korean / CJK / accented) each become ``-`` too — so a
      Unicode letter, which ``str.isalnum()`` would count as alphanumeric, is NOT
      preserved.

    Literal dashes are also ``-`` — exactly the many-to-one lossiness that
    :func:`_decode_claude_project_dirname` reconstructs around. Accepts any
    ``PurePath`` (only ``str(root)`` is used) so a ``PureWindowsPath`` can be
    encoded for testing on a POSIX host.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(root))


# A Windows drive-rooted slug: the drive letter survives (ASCII-alnum) and the
# ``:`` + ``\`` of ``<letter>:\`` both collapse to ``-``, so ``C:\dev`` encodes to
# ``C--dev``. Anchored at the start; a POSIX absolute slug always leads with ``-``
# (the ``/`` root) so it never matches here. UNC slugs (``\\host\share`` →
# ``--host-share``) DO lead with ``-`` and fall through to the POSIX branch — they
# are out of scope for the drive-root walk and simply fail closed there.
_WIN_DRIVE_SLUG_RE = re.compile(r"([A-Za-z])--")


def _decode_seed(name: str) -> tuple[Path, str] | None:
    """Return the ``(root, body)`` seed for the FS-walk, or ``None`` if *name* is
    not a walkable absolute slug.

    Two absolute-root encodings are recognized:

    * **POSIX** — a leading ``-`` is the ``/`` root (``-home-foo`` ← ``/home/foo``).
    * **Windows** — a drive-rooted slug ``<letter>--…`` ← ``<letter>:\\…`` (the
      drive ``:`` and the ``\\`` separator both collapsed to ``-``). The walk is
      seeded at the drive root and the drive prefix is consumed from the body.

    ``Path(...)`` is the native flavor, so on a Windows host the drive seed is a
    ``WindowsPath`` whose ``os.scandir`` / ``/`` join / ``is_dir`` all hit the real
    drive. On a non-Windows host ``Path("C:\\")`` is a *relative* ``PosixPath`` (a
    one-component name, ``is_absolute()`` is False), not a drive root — so the drive
    branch is gated on ``is_absolute()`` and returns ``None`` there, failing closed
    by construction rather than depending on the cwd not happening to contain a
    literal ``C:\\`` child. This is harmless either way: Claude Code never emits a
    drive-rooted slug for a POSIX path (and vice versa).
    """
    if name.startswith("-"):
        return Path("/"), name[1:]  # the leading "-" is the root "/"
    m = _WIN_DRIVE_SLUG_RE.match(name)
    if m:
        # "C--rest" ← "C:\rest": seed at the drive root, walk "rest". Only honor
        # it when the seed is a genuine absolute root on THIS host — a real
        # ``WindowsPath`` drive on Windows; on POSIX ``Path("C:\\")`` is a relative
        # name, so we fail closed instead of walking it relative to cwd.
        seed_root = Path(f"{m.group(1)}:\\")
        if seed_root.is_absolute():
            return seed_root, name[m.end() :]
    return None


def _decode_claude_project_dirname(name: str, anchors: tuple[Path, ...] = ()) -> list[Path]:
    """Reconstruct candidate absolute paths from a ``~/.claude/projects/<name>``.

    The on-disk encoding collapses *every* non-ASCII-alphanumeric character to
    ``-`` (see :func:`_encode_claude_project_path`), so it is lossy / many-to-one.
    We reconstruct **FS-guided**: at each encoded ``-`` a branch may either treat
    it as a path separator (commit the pending component if it exists as a real
    child) or keep building the component — and the non-separator case reads the
    *actual* next character from each existing child, so a ``-`` is reversed to
    whatever the real path holds there (``.``, ``_``, a literal ``-``, a space, a
    non-ASCII char, …). Only branches whose committed directory prefix actually
    exists survive, so the filesystem prunes the otherwise exponential partition
    space down to the paths that really exist. The anchor fast-path below is exact
    for any character because it re-encodes candidates through the same encoder.

    Returns the FS-confirmed candidate directories (``[]`` if none). The caller
    applies the accept-one-match rule. ``anchors`` (the ``known_projects.json``
    roots, plus the server cwd) are tried first: any whose encoding equals
    ``name`` are authoritative and returned directly — but *all* matches are
    returned (the encoding is many-to-one, so two distinct roots can collide),
    leaving the accept-one decision to the caller. Only when no anchor matches
    do we walk the filesystem.

    The slow-path FS walk seeds from :func:`_decode_seed`, which recognizes both a
    POSIX absolute root (leading ``-`` → ``/``) and a Windows drive-rooted slug
    (``C--…`` ← ``C:\\…``). The walk itself is platform-agnostic; the drive seed
    only resolves on a Windows host (where the real drive exists) and fails closed
    elsewhere, mirroring the anchor fast-path which already resolves registered
    Windows roots on any host. The feature is experimental / off by default. (The
    *encoder* is platform-agnostic too — see :func:`_encode_claude_project_path`.)
    """
    # Anchors first — authoritative, cheap, and platform-agnostic: they
    # re-encode through the same encoder, so a Windows ``C--…`` slug resolves
    # here even though the POSIX FS walk below cannot. Tried BEFORE the
    # leading-``-`` gate so registered Windows roots are not dropped. Collect
    # ALL matches (not first-match): the encoding is many-to-one so distinct
    # roots can collide. Dedup by resolved path so the same root reached via cwd
    # AND a known-project entry is not mistaken for two ambiguous candidates.
    anchor_hits: list[Path] = []
    seen_anchors: set[str] = set()
    for a in anchors:
        if _encode_claude_project_path(a) != name:
            continue
        key = _normalize_for_scope_id(a)
        if key not in seen_anchors:
            seen_anchors.add(key)
            anchor_hits.append(a)
    if anchor_hits:
        return anchor_hits

    # FS walk slow path. The seed is the absolute root the body is relative to:
    # "/" for a POSIX slug, the drive root for a Windows "C--…" slug. A slug that
    # is neither (no leading "-" and no drive prefix) is not a walkable absolute
    # path → fail closed.
    seed = _decode_seed(name)
    if seed is None:
        return []
    seed_root, body = seed
    children_cache: dict[Path, set[str]] = {}

    def children(d: Path) -> set[str]:
        cached = children_cache.get(d)
        if cached is None:
            names: set[str] = set()
            try:
                with os.scandir(d) as it:
                    for e in it:
                        # Skip a single unreadable entry rather than dropping the
                        # whole directory's listing — the Windows drive-root walk
                        # scans busy system dirs (``C:\``, ``C:\Users`` with its
                        # ``Default User`` / ``All Users`` junctions) where one
                        # entry's ``is_dir()`` can raise while its siblings (the
                        # real project ancestors) are fine.
                        try:
                            if e.is_dir():
                                names.add(e.name)
                        except OSError:
                            continue
            except OSError:
                pass  # the directory itself is unreadable / gone
            cached = names
            children_cache[d] = cached
        return cached

    # Frontier of (committed_dir, pending_segment): committed_dir is the
    # deepest confirmed-existing directory; pending_segment is the partial
    # component built since the last separator.
    frontier: list[tuple[Path, str]] = [(seed_root, "")]
    for ch in body:
        if ch != "-":
            frontier = [(committed, pending + ch) for committed, pending in frontier]
        else:
            nxt: list[tuple[Path, str]] = []
            for committed, pending in frontier:
                # Separator: pending is a complete component that must exist.
                if pending and pending in children(committed):
                    nxt.append((committed / pending, ""))
                # Non-separator: the encoded "-" stands for some character the
                # encoder collapsed (".", "_", a literal "-", a space, a
                # non-ASCII char, …). FS-guided: read the actual next character
                # from each existing child that extends ``pending`` — it must be
                # a char the encoder maps to "-" (i.e. NOT ASCII-alphanumeric).
                # Bounded by the child count, so the branch factor cannot explode.
                for child_name in children(committed):
                    if len(child_name) > len(pending) and child_name.startswith(pending):
                        real_char = child_name[len(pending)]
                        if not (real_char.isascii() and real_char.isalnum()):
                            nxt.append((committed, pending + real_char))
            frontier = nxt
        # Collapse convergent states so the budget reflects DISTINCT
        # reconstructions, not duplicate (committed, pending) paths.
        frontier = list(dict.fromkeys(frontier))
        if not frontier:
            return []
        if len(frontier) > _MAX_DECODE_CANDIDATES:
            # Genuine overflow — raise rather than return [] so the caller
            # does not misreport it as "no matching directory".
            raise _DecodeBudgetError(name)

    results: list[Path] = []
    seen: set[str] = set()
    for committed, pending in frontier:
        if not pending:
            continue
        full = committed / pending
        key = _normalize_for_scope_id(full)
        if key in seen:
            continue
        if full.is_dir():
            seen.add(key)
            results.append(full)
    return results


def _discover_claude_projects(anchors: tuple[Path, ...] = ()) -> list[Path]:
    if not _CLAUDE_PROJECTS_DIR.is_dir():
        return []
    found: list[Path] = []
    for child in _CLAUDE_PROJECTS_DIR.iterdir():
        if not child.is_dir():
            continue
        try:
            candidates = _decode_claude_project_dirname(child.name, anchors)
        except _DecodeBudgetError:
            logger.warning(
                "claude-projects: skip %r: too ambiguous to reconstruct safely "
                "(exceeded decode budget); register the project in "
                "known_projects.json to resolve it.",
                child.name,
            )
            continue
        # Drop stale candidates BEFORE the accept-one decision. Anchor hits are
        # not is_dir()-checked inside the decoder, so a stale known-project root
        # whose lossy encoding collides with a live cwd/project root would
        # otherwise make a valid live match look ambiguous and get skipped. (FS
        # walk candidates are already is_dir()-confirmed; this also preserves
        # the fail-closed guarantee.) Stale known-projects still surface through
        # the known-projects source in discover_project_scopes.
        candidates = [c for c in candidates if c.is_dir()]
        if not candidates:
            logger.warning(
                "claude-projects: skip %r: no matching directory on disk; "
                "register the project in known_projects.json to surface it.",
                child.name,
            )
            continue
        if len(candidates) > 1:
            logger.warning(
                "claude-projects: skip %r: ambiguous decode (%d matches: %s); "
                "register the project in known_projects.json to disambiguate.",
                child.name,
                len(candidates),
                ", ".join(str(c) for c in candidates),
            )
            continue
        found.append(candidates[0])
    return found


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
    known_entries = store.load()
    for entry in known_entries:
        _add(entry.root, "known-projects", missing=not entry.root.is_dir())

    # 3. Opt-in scan of ~/.claude/projects/ — silently skipped when the flag is
    #    False so the default discovery path stays cheap. The cwd and the
    #    user-registered roots are passed as authoritative decode anchors so a
    #    kebab-case project resolves unambiguously even where the FS walk would
    #    flag it ambiguous.
    if experimental_claude_projects_scan:
        anchors = (cwd, *(entry.root for entry in known_entries))
        for decoded in _discover_claude_projects(anchors):
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
