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
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Literal

from memtomem.context._atomic import _file_lock, _lock_path_for, atomic_write_bytes

logger = logging.getLogger(__name__)

__all__ = [
    "ProjectScope",
    "ProjectHealth",
    "KnownProjectsCorruptError",
    "KnownProjectsStore",
    "UnknownProjectSelectorError",
    "compute_scope_id",
    "discover_project_scopes",
    "annotate_project_health",
    "resolve_project_selector",
    "sync_skip_reason",
]


class KnownProjectsCorruptError(RuntimeError):
    """``known_projects.json`` exists but cannot be read as the expected doc.

    Raised by :meth:`KnownProjectsStore.load` with ``strict=True`` (what every
    mutator uses) when the file is unreadable (``OSError`` other than missing),
    not valid JSON, not a dict, or carries an unknown ``version``. Mutators
    load-then-rewrite the whole doc, so the tolerant ``[]`` fallback would be
    *persisted* — ``add()`` would re-baseline the registered-project list to
    just the new entry (#1247 id 16). Read-only discovery keeps the tolerant
    default. Version mismatch is folded in (no separate version error like the
    lockfile's): ``_write`` re-renders at the current version, so mutating a
    future-version file is the same clobber hazard as mutating a corrupt one.
    """


class UnknownProjectSelectorError(ValueError):
    """A project selector matched neither a discovered scope_id nor an existing path.

    Raised by :func:`resolve_project_selector`. CLI surfaces translate this
    into a usage error; the message already names both interpretations tried.
    """


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
    # ``missing`` — the root is gone (registered but no longer a directory);
    # ``stale`` — the root exists but has no ``.memtomem/`` store (never
    # initialized as a memtomem project). Mutually exclusive: a missing root is
    # never also stale. See ``annotate_project_health``.
    missing: bool = False
    stale: bool = False
    experimental: bool = False
    # ``enabled`` — the known_projects entry's sync-enrollment flag (True for
    # cwd-only / scan-only scopes, which have no entry). ``sync_eligible`` —
    # derived: server-cwd OR (enrolled AND enabled). Only an enrolled+enabled
    # scope (or the always-eligible server cwd) participates in sync.
    enabled: bool = True
    sync_eligible: bool = False


# ── known_projects.json store ───────────────────────────────────────────


_KNOWN_PROJECTS_VERSION = 1


@dataclass(frozen=True)
class _KnownProjectEntry:
    root: Path
    added_at: str
    label: str | None
    # Per-project sync enrollment. Additive field — the schema stays version 1:
    # legacy rows without the key read back as ``True`` (see ``load``), so old
    # and new readers agree. Known downgrade hazard — an older writer that lacks
    # this field drops it on rewrite, silently resuming a paused project;
    # acceptable for single-user local use, revisit with unknown-field
    # preservation if paused state must survive older tools.
    enabled: bool = True


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

    def load(self, *, strict: bool = False) -> list[_KnownProjectEntry]:
        """Return entries in registration order.

        Missing file → ``[]`` in both modes (the normal pre-registration
        state). For an *existing* file that is unreadable, invalid JSON,
        not a dict, an unknown ``version``, a non-list ``projects`` member,
        or containing a row without a usable ``root``: ``strict=False``
        (default — read-only discovery) logs a warning and degrades to
        ``[]`` / skips the row; ``strict=True`` (mutators) raises
        :class:`KnownProjectsCorruptError` so the follow-up ``_write``
        cannot persist the degraded list over the user's registrations
        (#1247 id 16).
        """
        hint = "fix or remove it (e.g. restore it from version control), then retry"
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return []
        except OSError as exc:
            if strict:
                raise KnownProjectsCorruptError(
                    f"known_projects file at {self._path} is unreadable ({exc}); {hint}"
                ) from exc
            logger.warning("known_projects: read failed: %s", exc)
            return []

        try:
            doc = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError: json.loads(bytes) decodes before parsing,
            # so invalid UTF-8 raises it instead of JSONDecodeError — same
            # corrupt-file class, same handling (Codex design review).
            if strict:
                raise KnownProjectsCorruptError(
                    f"known_projects file at {self._path} is not valid JSON ({exc}); {hint}"
                ) from exc
            logger.warning("known_projects: invalid JSON, ignoring file: %s", exc)
            return []

        if not isinstance(doc, dict) or doc.get("version") != _KNOWN_PROJECTS_VERSION:
            version = doc.get("version") if isinstance(doc, dict) else None
            if strict:
                raise KnownProjectsCorruptError(
                    f"known_projects file at {self._path} has unexpected version "
                    f"{version!r} (this build writes version {_KNOWN_PROJECTS_VERSION}); {hint}"
                )
            logger.warning("known_projects: unexpected version %r, ignoring", version)
            return []

        # Shape guards below the version check: a row the parser drops here
        # is *destroyed* by the next mutation — unlike the lockfile store,
        # ``_write`` re-renders from parsed entries, so anything skipped does
        # not round-trip. Strict mode therefore refuses any unrepresentable
        # shape (Codex impl review: ``{"projects": {...}}`` was re-baselined
        # to one entry). Designed legacy defaults (missing ``added_at`` /
        # ``label`` / ``enabled``) are NOT corruption and parse normally.
        projects_member = doc.get("projects", [])
        if not isinstance(projects_member, list):
            if strict:
                raise KnownProjectsCorruptError(
                    f"known_projects file at {self._path} has a non-list 'projects' "
                    f"member ({type(projects_member).__name__}); {hint}"
                )
            logger.warning("known_projects: 'projects' is not a list, ignoring file")
            return []

        entries: list[_KnownProjectEntry] = []
        for item in projects_member:
            if not isinstance(item, dict):
                if strict:
                    raise KnownProjectsCorruptError(
                        f"known_projects file at {self._path} has a non-object project "
                        f"row ({type(item).__name__}); {hint}"
                    )
                continue
            root = item.get("root")
            if not isinstance(root, str) or not root:
                if strict:
                    raise KnownProjectsCorruptError(
                        f"known_projects file at {self._path} has a project row without "
                        f"a usable 'root'; {hint}"
                    )
                continue
            entries.append(
                _KnownProjectEntry(
                    # Canonicalize on load so legacy relative rows (pre-#1644) stop
                    # floating with the reader's cwd. In-memory only — the load path
                    # must not write (#1567); mutators persist the healed roots on
                    # their next locked load-then-write.
                    root=Path(root).expanduser().resolve(),
                    added_at=str(item.get("added_at") or ""),
                    label=item.get("label") if isinstance(item.get("label"), str) else None,
                    # Legacy rows (pre-``enabled`` schema) and any non-bool value
                    # default to True — enrolled-but-unflagged means "participating",
                    # matching ``add``'s default. Only an explicit JSON ``false`` disables.
                    enabled=item.get("enabled") is not False,
                )
            )
        return entries

    def add(self, root: Path, label: str | None = None) -> _KnownProjectEntry:
        """Register *root*. Idempotent — re-registering an existing root is a no-op
        (returns the existing entry).

        Raises :class:`KnownProjectsCorruptError` instead of re-baselining
        the list to ``[new_entry]`` when the file exists but is corrupt.

        Thin wrapper over :meth:`add_with_status` for callers that don't need to
        know whether the entry was freshly created.
        """
        entry, _created = self.add_with_status(root, label=label)
        return entry

    def add_with_status(
        self, root: Path, label: str | None = None
    ) -> tuple[_KnownProjectEntry, bool]:
        """Like :meth:`add`, but also report whether the entry was freshly created.

        Returns ``(entry, created)``: ``created`` is ``False`` when *root* was
        already registered (the existing entry is returned unchanged) and ``True``
        when a new entry was appended. The flag is decided INSIDE the same
        exclusive lock as the write, so it cannot disagree with the persisted state
        under a concurrent add of the same root — unlike a load-then-add check
        spread across two separate lock windows.

        Raises :class:`KnownProjectsCorruptError` instead of re-baselining
        the list to ``[new_entry]`` when the file exists but is corrupt.

        *root* is canonicalized (``expanduser().resolve()``) before dedup and
        persist — a relative root would otherwise be stored verbatim and mean
        a different directory per reader cwd (#1644).
        """
        normalized = Path(root).expanduser().resolve()
        with _file_lock(_lock_path_for(self._path)):
            entries = self.load(strict=True)
            for existing in entries:
                if _normalize_for_scope_id(existing.root) == _normalize_for_scope_id(normalized):
                    return existing, False
            new_entry = _KnownProjectEntry(
                root=normalized,
                added_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                label=label,
                enabled=True,
            )
            self._write(entries + [new_entry])
            return new_entry, True

    def remove_by_scope_id(self, scope_id: str) -> bool:
        """Drop the entry whose computed scope_id matches. Returns True if removed.

        Stale entries (root no longer a directory) are removable — matching
        is on ``compute_scope_id(entry.root)`` which is path-derived, not
        existence-derived.
        """
        with _file_lock(_lock_path_for(self._path)):
            entries = self.load(strict=True)
            kept = [e for e in entries if compute_scope_id(e.root) != scope_id]
            if len(kept) == len(entries):
                return False
            self._write(kept)
            return True

    def update_entry_by_scope_id(
        self,
        scope_id: str,
        *,
        label: str | None = None,
        set_label: bool = False,
        enabled: bool = True,
        set_enabled: bool = False,
    ) -> _KnownProjectEntry | None:
        """Atomically update ``label`` and/or ``enabled`` of the matching entry in
        a SINGLE locked load/write. Returns the first updated entry, or None if no
        entry matched.

        Each field is applied only when its ``set_*`` flag is True (an unset field
        is preserved). Applying both in one lock window keeps a combined PATCH
        atomic — a concurrent DELETE / PATCH cannot interleave between a label
        write and an enabled write, so the request never partially applies, loses
        the other field, nor 404s after a half-write. ``root`` / ``added_at`` are
        always preserved (identity is path-derived, so this never changes the
        scope_id). Holds the same exclusive sidecar lock + atomic
        ``tmp + os.replace`` as ``add`` / ``remove_by_scope_id``.

        Updates *every* entry whose scope_id matches — mirroring
        ``remove_by_scope_id``'s all-matching semantics — so a manually corrupted
        file with duplicate rows can't leave a stale row behind a "success".
        ``add`` canonicalizes roots before persisting and dedups on the same
        resolved form, so duplicates never arise via the API.
        """
        with _file_lock(_lock_path_for(self._path)):
            entries = self.load(strict=True)
            first_updated: _KnownProjectEntry | None = None
            new_entries: list[_KnownProjectEntry] = []
            for e in entries:
                if compute_scope_id(e.root) == scope_id:
                    replacement = _KnownProjectEntry(
                        root=e.root,
                        added_at=e.added_at,
                        label=label if set_label else e.label,
                        enabled=enabled if set_enabled else e.enabled,
                    )
                    new_entries.append(replacement)
                    if first_updated is None:
                        first_updated = replacement
                else:
                    new_entries.append(e)
            if first_updated is None:
                return None
            self._write(new_entries)
            return first_updated

    def update_label_by_scope_id(
        self, scope_id: str, label: str | None
    ) -> _KnownProjectEntry | None:
        """Set (or clear, when *label* is None) the label of the matching entry.

        Thin wrapper over :meth:`update_entry_by_scope_id` — ``enabled`` is
        preserved, so a rename never silently resumes a paused project.
        """
        return self.update_entry_by_scope_id(scope_id, label=label, set_label=True)

    def set_enabled_by_scope_id(self, scope_id: str, enabled: bool) -> _KnownProjectEntry | None:
        """Set the per-project sync-enrollment flag of the matching entry.

        Thin wrapper over :meth:`update_entry_by_scope_id` — the label is
        preserved. A disabled project stays discoverable but is excluded from sync
        (web sync button, Sync All, and CLI ``mm context update --all``).
        """
        return self.update_entry_by_scope_id(scope_id, enabled=enabled, set_enabled=True)

    def _write(self, entries: list[_KnownProjectEntry]) -> None:
        doc = {
            "version": _KNOWN_PROJECTS_VERSION,
            "projects": [
                {
                    "root": str(e.root),
                    "added_at": e.added_at,
                    "label": e.label,
                    "enabled": e.enabled,
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


# ── Project health ───────────────────────────────────────────────────────


# A scope is *stale* iff its root exists but lacks this directory — i.e. the
# tree is there but was never initialized as a memtomem project. It is the
# memtomem-specific subset of ``_MARKER_DIRS`` (which also recognizes runtime
# markers like ``.claude``); the Portal's "Initialize" affordance keys off
# *this* marker, not the broader set.
_MEMTOMEM_MARKER = ".memtomem"


@dataclass(frozen=True)
class ProjectHealth:
    """The (``missing``, ``stale``) pair the Portal renders for one scope.

    Mirrors the same-named ``ProjectScope`` fields; ``annotate_project_health``
    is the single source that computes both from a root on disk.
    """

    missing: bool
    stale: bool


def _root_stale(root: Path) -> bool:
    """True when *root* exists but has no ``.memtomem/`` store.

    Only meaningful for a present root; the caller gates on ``missing`` first.
    An unreadable root reads as stale (uninitialized from our vantage) rather
    than crashing discovery.
    """
    try:
        return not (root / _MEMTOMEM_MARKER).is_dir()
    except OSError:
        return True


def annotate_project_health(scope: ProjectScope) -> ProjectHealth:
    """Compute the Portal health pair (``missing``, ``stale``) for *scope*.

    ``missing`` — the root is None / absent / not a directory: the tree is gone,
    so the Portal greys the row out and offers only *unregister*. ``stale`` —
    the root exists but has no ``.memtomem/`` store: registered or discovered
    but never initialized, so the Portal offers *Initialize*. The two are
    mutually exclusive — a missing root is never reported stale (there is
    nothing to initialize until the tree returns).

    Re-derives ``missing`` from disk so the pair is internally coherent; the
    value agrees with the ``missing`` ``discover_project_scopes`` already sets
    (its source union flags a known-project root missing iff it is not a
    directory).
    """
    root = scope.root
    if root is None:
        return ProjectHealth(missing=True, stale=False)
    try:
        is_dir = root.is_dir()
    except OSError:
        is_dir = False
    if not is_dir:
        return ProjectHealth(missing=True, stale=False)
    return ProjectHealth(missing=False, stale=_root_stale(root))


def discover_project_scopes(
    cwd: Path,
    known_projects_file: Path,
    *,
    experimental_claude_projects_scan: bool,
    auto_display_configured_projects: bool = True,
) -> list[ProjectScope]:
    """Enumerate all project scopes the UI should render, in display order.

    Server cwd is always first (so the user's primary working tree is
    visible even before Add Project is used). Entries with the same
    resolved path coalesce; ``sources`` then carries the union of every
    place each scope was discovered.

    ``~/.claude/projects`` scan candidates are gated two ways:
    ``experimental_claude_projects_scan`` admits them *unfiltered* (the legacy
    escape hatch), while ``auto_display_configured_projects`` (on by default)
    admits only candidates whose root carries a runtime marker
    (``has_runtime_marker``). The filter applies *only* to scan rows — server
    cwd and known-projects entries are always shown, even when missing/stale.

    ``enabled`` / ``sync_eligible`` are read strictly from the matching
    known_projects entry (never from cwd / scan sources) so a paused project
    that also shows up in the scan cannot be silently re-enabled.
    """
    # Resolved-path → (display_path, sources, missing, stored_label)
    by_resolved: dict[str, tuple[Path, set[str], bool, str | None]] = {}
    order: list[str] = []

    def _add(display: Path, source: str, *, missing: bool, label: str | None = None) -> None:
        # Resolve aggressively — strict=False so a stale known-project root
        # that no longer exists still gets a scope_id (so the user can
        # DELETE it via the UI).
        try:
            resolved = display.resolve(strict=False)
        except OSError:
            resolved = display
        key = _normalize_for_scope_id(resolved)
        if key in by_resolved:
            _, sources, was_missing, prev_label = by_resolved[key]
            sources.add(source)
            # `missing` only stays true if every source flagged it missing.
            # The first non-empty stored label wins (only known-projects carries
            # one; cwd / claude-projects pass None).
            merged_label = prev_label if prev_label else label
            by_resolved[key] = (resolved, sources, was_missing and missing, merged_label)
        else:
            by_resolved[key] = (resolved, {source}, missing, label)
            order.append(key)

    # 1. Server cwd — always first, never missing (the process is running there).
    _add(cwd, "server-cwd", missing=False)

    # 2. User-registered roots from known_projects.json.
    store = KnownProjectsStore(known_projects_file)
    known_entries = store.load()
    # Sync enrollment (``enabled``) is read strictly from the known_projects
    # entry, keyed the same way ``_add`` keys ``by_resolved`` so the lookup at
    # emit time matches. cwd / scan sources never contribute enablement.
    known_by_key = {_normalize_for_scope_id(e.root): e for e in known_entries}
    for entry in known_entries:
        _add(
            entry.root,
            "known-projects",
            missing=not entry.root.is_dir(),
            label=entry.label,
        )

    # 3. Scan of ~/.claude/projects/. Admitted when EITHER gate is open:
    #    - experimental_claude_projects_scan: unfiltered (legacy escape hatch).
    #    - auto_display_configured_projects (default on): only candidates whose
    #      root carries a runtime marker, so auto-display surfaces configured
    #      projects only. The cwd and user-registered roots are passed as
    #      authoritative decode anchors so a kebab-case project resolves
    #      unambiguously even where the FS walk would flag it ambiguous.
    if experimental_claude_projects_scan or auto_display_configured_projects:
        anchors = (cwd, *(entry.root for entry in known_entries))
        for decoded in _discover_claude_projects(anchors):
            if not experimental_claude_projects_scan and not has_runtime_marker(decoded):
                continue
            _add(decoded, "claude-projects", missing=False)

    scopes: list[ProjectScope] = []
    for key in order:
        resolved, sources, missing, stored_label = by_resolved[key]
        # ``experimental`` flags a row present ONLY because the unfiltered opt-in
        # scan gate is open: a scan-only source AND no runtime marker. A
        # marker-bearing scan row is admitted by the default filtered auto-display
        # path (auto_display_configured_projects), so it is a normal configured
        # project — not opt-in/experimental — and must not carry the warning copy.
        # cwd / known-projects unions also clear the flag (most-trusted source wins).
        experimental = sources == {"claude-projects"} and not has_runtime_marker(resolved)
        # Label precedence: an explicit stored label (set via Add Project or the
        # rename PATCH) wins — it is the user's deliberate name, so it overrides
        # even the "Server CWD" auto-label when the cwd was also registered.
        # Otherwise the server cwd shows "Server CWD"; everything else falls back
        # to the directory basename.
        if stored_label:
            label = stored_label
        elif "server-cwd" in sources:
            label = "Server CWD"
        else:
            label = _label_for(resolved)
        # Sync enrollment is read from the known entry only (cwd / scan never
        # contribute), so a paused known project that is also scan-discovered
        # stays ineligible. server-cwd is always eligible — the directory the
        # server runs in cannot be "paused".
        known_entry = known_by_key.get(key)
        enabled = known_entry.enabled if known_entry is not None else True
        sync_eligible = ("server-cwd" in sources) or (
            known_entry is not None and known_entry.enabled
        )
        scopes.append(
            ProjectScope(
                scope_id=compute_scope_id(resolved),
                label=label,
                root=resolved,
                tier="project",
                sources=tuple(sorted(sources)),
                missing=missing,
                # A missing root cannot be inspected for a ``.memtomem/`` marker
                # and is never reported stale — the two flags are exclusive.
                stale=(not missing) and _root_stale(resolved),
                experimental=experimental,
                enabled=enabled,
                sync_eligible=sync_eligible,
            )
        )
    return scopes


# ── Validation helpers ──────────────────────────────────────────────────


# One marker per in-scope runtime (claude / antigravity-on-.gemini / codex /
# kimi) plus the canonical ``.memtomem`` store. ``.codex`` covers Codex agents
# (``.codex/agents``) and ``.agents`` covers Codex skills (``.agents/skills``) —
# see ``_runtime_targets.py``. Used both for the Add-Project "looks configured"
# warning and as the auto-display filter on ``~/.claude/projects`` scan rows.
_MARKER_DIRS = (".claude", ".gemini", ".codex", ".agents", ".kimi", ".memtomem")


def has_runtime_marker(root: Path) -> bool:
    """Return True if *root* contains any recognized runtime marker directory.

    Used by ``POST /api/context/known-projects`` to decide whether to
    emit a warning ("looks like a non-project directory") without rejecting
    the registration outright, and by ``discover_project_scopes`` to filter
    ``~/.claude/projects`` scan candidates to ones that actually carry runtime
    config. Empty parents are valid — the user might be setting up a fresh
    checkout.
    """
    return any((root / m).is_dir() for m in _MARKER_DIRS)


# ── Batch-sync eligibility (ADR-0025) ────────────────────────────────────


def sync_skip_reason(scope: ProjectScope) -> str | None:
    """Why *scope* is excluded from a batch sync run, or ``None`` if eligible.

    One derivation shared by ``mm context sync --all-projects`` and the web
    ``POST /api/context/sync-all-projects`` loop so the two surfaces cannot
    drift on WHICH scopes execute; each surface owns its remediation
    message. The paused / not-enrolled split mirrors the web
    ``resolve_writable_scope_root`` eligibility 409.

    Codes, in evaluation order:

    - ``missing_root`` — the root is gone; checked first because physical
      absence trumps enrollment state (a paused project whose tree was
      also deleted reports the physical problem).
    - ``sync_paused`` / ``sync_not_enrolled`` — ``sync_eligible`` is False:
      an enrolled known-project whose ``enabled`` flag is off, or a
      discovery-only scope never enrolled.
    - ``stale_project`` — root exists but has no ``.memtomem/`` store.
      Batch-only gate: bulk-syncing a tree the user never initialized
      would at best no-op every phase and at worst seed bookkeeping; the
      per-type single routes stay ungated on stale.
    """
    if scope.root is None or scope.missing:
        return "missing_root"
    if not scope.sync_eligible:
        return "sync_paused" if "known-projects" in scope.sources else "sync_not_enrolled"
    if scope.stale:
        return "stale_project"
    return None


# ── CLI / web project selector ───────────────────────────────────────────


_SCOPE_ID_SHAPE = re.compile(r"^p-[0-9a-f]{12}$")


def resolve_project_selector(
    selector: str,
    scopes: Sequence[ProjectScope],
) -> tuple[Path, ProjectScope | None]:
    """Resolve a user-supplied ``scope_id | path`` selector to a project root.

    Shared by the ``mm context projects`` subcommands and the cross-project
    flags that take a destination project (``--to-project``, ``--project``,
    ``--all-projects`` siblings) so every surface accepts the same two forms.

    Resolution is deterministic, not heuristic:

    1. A selector matching the ``p-<sha12>`` shape is ONLY treated as a
       scope_id. Match against *scopes* (case-sensitive, exact) → that
       scope's root. No match → :class:`UnknownProjectSelectorError` — it
       does NOT fall through to the path interpretation, so a directory that
       happens to be named like a scope_id cannot be selected ambiguously
       (spell it ``./p-...`` to force the path reading).
    2. Anything else is a filesystem path: ``expanduser`` + resolved, and it
       must be an existing directory. Returns the matching discovered scope
       when one shares the same ``compute_scope_id`` (so callers see
       enrollment state), else ``None`` — explicitly typing a path is consent
       to operate on an unregistered root; callers gate further (e.g. a
       paused *registered* destination still refuses).

    A scope_id match whose scope has no root (defensive — discovery always
    sets one today) raises :class:`UnknownProjectSelectorError` rather than
    returning a rootless scope.
    """
    if _SCOPE_ID_SHAPE.match(selector):
        for scope in scopes:
            if scope.scope_id == selector:
                if scope.root is None:
                    raise UnknownProjectSelectorError(
                        f"scope {selector} has no resolvable root; re-register it by path"
                    )
                return scope.root, scope
        raise UnknownProjectSelectorError(
            f"no discovered project has scope_id {selector!r} (run `mm context projects "
            f"list`); to select a directory literally named like a scope_id, "
            f"prefix it with ./"
        )

    candidate = Path(selector).expanduser()
    try:
        resolved = candidate.resolve(strict=False)
    except OSError:
        resolved = candidate
    if not resolved.is_dir():
        raise UnknownProjectSelectorError(
            f"{selector!r} is neither a discovered scope_id nor an existing directory"
        )
    wanted = compute_scope_id(resolved)
    for scope in scopes:
        if scope.scope_id == wanted:
            return resolved, scope
    return resolved, None
