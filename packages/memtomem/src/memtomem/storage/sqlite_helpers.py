"""Shared utility functions for the SQLite backend."""

from __future__ import annotations

import os
import struct
import sys
import unicodedata
import hashlib
from datetime import datetime, timezone
from fnmatch import fnmatch
from collections.abc import Iterable
from pathlib import Path

from memtomem.models import NamespaceFilter


def serialize_f32(vector: list[float]) -> bytes:
    """Pack a float vector into raw bytes for sqlite-vec."""
    return struct.pack(f"{len(vector)}f", *vector)


def deserialize_f32(data: bytes) -> list[float]:
    """Unpack raw bytes back to a float vector."""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


# Whether a path key folds Unicode normalisation forms. Read at call time, so
# tests can force either answer on any platform. See :func:`fold_path_form`.
FOLDS_UNICODE_FORMS = sys.platform == "darwin"


def fold_path_form(path: str) -> str:
    """Fold a path string to NFC where the filesystem treats NFC and NFD as one.

    macOS volumes (APFS, HFS+) do not distinguish the forms, so ``café`` typed
    in NFC and ``café`` stored in NFD name one directory there, and the key has
    to fold them or the two spellings of one file never compare equal (#235).

    Linux and Windows filesystems (ext4, NTFS) keep the forms apart: they can be
    two directories, and the NFD one is reachable only by its own bytes. Folding
    there makes the key the address of a different path. A file under an NFD
    directory was stored under an NFC path that does not exist, so the orphan
    scan confirmed it missing and cleanup deleted live chunks; an NFC and an NFD
    sibling shared one key, so indexing either replaced the other's chunks
    (#2544). There the resolved spelling is already the one on disk, and it is
    returned unchanged.

    The answer is per platform, not per volume: a volume that keeps the forms
    apart mounted on macOS (NFS, FUSE ext4) is still folded, as before, and a
    folding one mounted on Linux (ext4 ``casefold``, SMB from a Mac) is not, so
    two spellings of one file can be keyed twice there. Asking each volume
    would mean probing directories memtomem may not write to.
    """
    return unicodedata.normalize("NFC", path) if FOLDS_UNICODE_FORMS else path


def norm_path(p: Path) -> str:
    """Normalize path to a canonical string.

    Resolves symlinks (``/tmp`` → ``/private/tmp`` on macOS) and folds the
    Unicode normalisation form where the filesystem does (macOS), so NFD and
    NFC spellings of the same path compare equal there. Without that,
    non-ASCII paths such as
    ``~/Library/CloudStorage/GoogleDrive-.../내 드라이브/...`` can fail the
    equality check used by the web routes (see issue #235). Elsewhere the two
    forms are two paths and are kept apart — see :func:`fold_path_form`.
    """
    try:
        resolved = str(p.resolve())
    except OSError:
        resolved = str(p)
    return fold_path_form(resolved)


def norm_dir_prefix(d: str | Path) -> str:
    """Return the directory path normalized for ``str.startswith`` matching.

    Adds a trailing ``os.sep`` (platform-native separator) so a configured
    dir does not falsely claim files under a sibling sharing the same
    prefix (e.g. ``/foo`` should not match ``/foo-bar/...``). Always runs
    through :func:`norm_path` (which resolves symlinks and folds the
    Unicode form where the filesystem does) so the prefix shape matches the
    source-side normalisation regardless of whether the dir currently exists
    on disk — the chunks
    table holds resolved paths, and a configured-but-missing dir would
    otherwise compare in raw ``/tmp`` form against resolved
    ``/private/tmp`` source paths on macOS.

    The trailing-separator step uses ``os.sep`` rather than a hardcoded
    ``"/"`` so the prefix is consistent with ``norm_path``'s output on
    Windows, where ``Path.resolve()`` returns backslash-separated strings
    (``C:\\Users\\foo``) — a hardcoded ``"/"`` would yield a mixed-form
    prefix that never matches a native source path under
    ``startswith`` (#647). On POSIX, ``os.sep == "/"`` so behaviour is
    unchanged.

    Used by :func:`memory_dir_stats` (which buckets chunks per configured
    dir), :func:`resolve_owning_memory_dir` (which goes the other way —
    given a source, find the owning dir) and the read-only-root predicate
    on both the config and the engine. Keeping the normalisation in one
    place ensures those views stay consistent when the prefix rules
    evolve. Lives here rather than in ``indexing.engine`` because
    ``config`` validates read-only roots with it and must not import the
    engine; ``indexing.engine`` re-exports the name for its own callers.
    """
    p = Path(d).expanduser()
    base = norm_path(p)
    if not base.endswith(os.sep):
        base += os.sep
    return base


def _identity_match(spellings: tuple[str, ...], root_norm: str) -> bool:
    """Whether either spelling passes through ``root`` itself, by inode identity.

    This replaces asking whether the filesystem folds case. That question had no
    trustworthy answer — a probe either inspected entries another tool owns, or
    created one inside a directory memtomem promised not to write into — and one
    answer was then applied to a whole path whose components can sit on
    different filesystems.

    Identity needs none of that. ``norm_path`` has already resolved the
    spelling, so slicing it to the root's own depth names the directory that
    occupies the root's position in that path, whatever case it was typed in and
    whatever links were traversed to get there. ``(st_dev, st_ino)`` then says
    whether that is the protected directory. A case-insensitive filesystem
    answers yes for ``VAULT`` because it really is the same directory; a
    case-sensitive one answers no because it really is not. Nothing is written,
    nothing is cached, and no property is extrapolated across a mount boundary.

    ``stat`` follows links deliberately. A path that reaches the protected
    directory reaches it however it is spelled; the one over-refusal this adds
    is a link *outside* the root pointing at it, where replacing the link would
    not touch the root. For a protection rule that is the safe direction.

    The root must exist to have an identity. When it does not, only the literal
    prefix rule applies — see :func:`is_under_any_root`.

    ``root_norm`` is the **already normalized** root — the same string the
    literal prefix was built from, minus its trailing separator. Re-deriving it
    here is what broke this once: ``norm_dir_prefix`` expands ``~`` and this did
    not, so a ``~/Vault`` root had its prefix built from the home directory
    while the identity arm stat'ed ``<cwd>/~/Vault`` and matched nothing. Two
    arms of one rule must be told which path they are about, not each work it
    out.
    """
    root_path = Path(root_norm)
    try:
        root_stat = root_path.stat()
    except OSError:
        return False
    key = (root_stat.st_dev, root_stat.st_ino)
    depth = len(root_path.parts)
    for spelling in spellings:
        parts = Path(spelling).parts
        if len(parts) < depth:
            continue
        try:
            at_depth = Path(*parts[:depth]).stat()
        except OSError:
            continue
        if (at_depth.st_dev, at_depth.st_ino) == key:
            return True
    return False


def is_under_any_root(target: str | Path, roots: Iterable[str | Path]) -> bool:
    """Whether ``target`` lies under any directory in ``roots``.

    The single containment rule for read-only index roots. It lives beside
    :func:`norm_dir_prefix` rather than on ``IndexEngine`` because three
    callers need it and only one of them has an engine: the engine's
    ``is_read_only_source``, ``mm memory doctor --fix`` (which refuses to
    rewrite a protected provider index), and ``PinnedContextStore``'s
    mutating methods. Each hand-rolling the same ``norm_path`` /
    ``norm_dir_prefix`` pair is how the three would drift apart, and a
    protection rule that means something slightly different at each surface
    is the bug this feature exists to avoid.

    Two spellings of the path are checked, because a symlink puts the file and
    its directory entry in different places and different operations act on
    different ones:

    * **Fully resolved.** Right for a path whose *parent* is a symlink into a
      protected root — ``/writable/link/note.md`` pointing into ``/vault`` is
      under ``/vault`` even though neither configured root contains the other,
      and an ``open()`` write follows the link to the protected bytes.
    * **Entry location** — parent resolved, leaf name left literal. Right for
      the mirror case: a link that *lives* inside a protected root and points
      out. ``os.replace`` and ``unlink`` act on that directory entry, so they
      would replace or remove something inside the vault while the resolved
      path says the file is elsewhere. This is the same reasoning
      ``refuse_replace_target``'s symlink arm carries, applied to the callers
      that do not go through it.

    Either one matching refuses. For an ordinary path the two spellings are
    identical, so this adds no refusals that were not already there — only the
    symlink cases differ, and for those the safe answer is the protected one.

    The root **itself** counts as under itself. ``norm_dir_prefix`` ends in a
    separator so it cannot match the bare root path, and callers do ask about
    the directory (the upload route checks its whole destination before
    creating anything inside it) — without this, protecting exactly that
    directory protected everything in it except the act of preparing it.

    A spelling that matches neither prefix literally can still *be* the root —
    ``VAULT/note.md`` for ``/Vault`` on a case-insensitive volume names the
    protected file. That is settled by :func:`_identity_match`, which asks the
    kernel whether the directory at the root's depth is the root, rather than
    asking the filesystem whether it folds case and applying that answer to the
    whole path. On a case-sensitive volume ``/vault`` and ``/Vault`` are two
    directories with two inodes, so nothing is refused that nobody protected.

    **A root that does not exist yet has no identity**, so only the literal
    prefix rule covers it: a differently cased spelling of an uncreated root is
    not refused. The same spelling still is. This is narrower than the probe
    this replaced, which guessed the answer from a neighbouring directory --
    and guessed it by writing into one.
    """
    if not roots:
        return False
    path = Path(target).expanduser()
    resolved = norm_path(path)
    # ``norm_path`` would resolve the leaf too, which is exactly what this
    # second spelling must not do.
    entry = norm_path(path.parent).rstrip(os.sep) + os.sep + path.name
    # For a path with no symlink in it the two spellings are identical, which is
    # the common case; checking it twice doubles the stat calls per root.
    spellings = (resolved,) if entry == resolved else (resolved, entry)
    for root in roots:
        prefix = norm_dir_prefix(root)
        bare = prefix.rstrip(os.sep)
        if any(s == bare or s.startswith(prefix) for s in spellings):
            return True
        if _identity_match(spellings, bare):
            return True
    return False


def project_boundary_key(project_context_root: Path | str | None) -> str:
    """Return the non-reversible key used to isolate project-owned records.

    ``None`` is a real boundary: it represents a user-only invocation outside
    a registered project.  Absolute roots are never persisted in history rows
    or returned through diagnostics.
    """
    if project_context_root is None:
        return "user"
    canonical = norm_path(Path(project_context_root))
    return hashlib.sha256(f"project\0{canonical}".encode("utf-8")).hexdigest()


def match_source_filter_value(filter_str: str, source_path: str) -> bool:
    """Canonical substring-or-glob source matcher, separator portable."""
    norm_filter = filter_str.replace("\\", "/")
    norm_source = source_path.replace("\\", "/")
    if any(char in norm_filter for char in ("*", "?", "[")):
        return fnmatch(norm_source, norm_filter)
    return norm_filter in norm_source


def placeholders(n: int) -> str:
    """Return ``n`` comma-separated SQL ``?`` placeholders."""
    if n <= 0:
        raise ValueError(f"placeholders() requires n > 0, got {n}")
    return ",".join("?" * n)


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def utc_bound(value: datetime) -> str:
    """Render a datetime as a bound comparable against a stored timestamp.

    The ``created_at`` / ``updated_at`` columns hold UTC ISO-8601 strings and
    every filter on them compares **lexically** — SQLite has no datetime type,
    so ``created_at >= ?`` orders by the printed digits. That is temporal
    ordering only while both sides are UTC: a bound left at ``+09:00`` sorts by
    its own wall-clock reading, so ``2026-01-01T00:00:00+09:00``
    (= ``2025-12-31T15:00Z``) compares as *later* than a row written at
    ``2025-12-31T16:00Z``, which actually follows it.

    A naive value is read as UTC, matching how the rest of the storage layer
    treats one.

    Use this for every datetime that becomes a bound on those columns; do not
    call ``.isoformat()`` directly at the call site.
    """
    return utc_stamp(value, timespec="auto")


def utc_stamp(value: datetime, *, timespec: str = "microseconds") -> str:
    """Render a datetime as the canonical stored form of a timestamp column.

    The ROW side of the invariant :func:`utc_bound` describes: bounds compare
    lexically against stored values, so a *stored* timestamp must be UTC and
    canonically rendered just as much as a bound. A row written with its
    caller's offset intact (e.g. an imported bundle's ``+09:00``) sorts by its
    wall-clock digits and lands on the wrong side of every correct bound.

    A naive value is read as UTC, matching :func:`utc_bound`. The default
    ``timespec="microseconds"`` is the precision the ``chunks`` timestamp
    columns are written at; pass the target column's precision explicitly when
    it differs.

    Use this for every datetime that becomes a stored timestamp; do not call
    ``.isoformat()`` directly at the call site.
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec=timespec)


def utc_bound_from_iso(value: str, *, field: str, timespec: str = "auto") -> str:
    """Parse a caller-supplied ISO-8601 string into a UTC bound.

    The string counterpart of :func:`utc_bound`, for surfaces that take the
    bound as text and would otherwise bind it to SQL untouched.

    ``timespec`` must match the precision the *target column* is written at,
    because the comparison is lexical. ``query_history.created_at`` is stored
    with ``timespec="seconds"``, so a bound carrying fractional seconds sorts
    after every row inside its own second: a poll at ``12:00:00.500`` would
    miss a run recorded at ``12:00:00.800`` and stored as ``12:00:00+00:00``.
    Flooring the bound to the same precision keeps the whole second on the
    inclusive side. Leave it at ``"auto"`` for columns written at full
    precision, such as ``chunks.created_at``.

    Raises:
        ValueError: the value is not ISO-8601, named by ``field`` so the
            message points at the argument the caller passed.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{field} must be an ISO-8601 timestamp, got {value!r}") from None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec=timespec)


def escape_like(value: str) -> str:
    """Escape LIKE special characters (``%``, ``_``) in a user-supplied value."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def namespace_sql(ns: NamespaceFilter) -> tuple[str, list]:
    """Build SQL WHERE fragment + params for a NamespaceFilter.

    Explicit forms (``namespaces``, ``pattern``) take priority over the
    default-search ``exclude_prefixes`` fallback — the parse layer is
    responsible for never sending both at once, so this ordering is just
    defensive.
    """
    if ns.namespaces:
        ph = ",".join("?" * len(ns.namespaces))
        return f"namespace IN ({ph})", list(ns.namespaces)
    if ns.pattern:
        escaped = ns.pattern.replace("_", r"\_").replace("*", "%")
        return "namespace LIKE ? ESCAPE '\\'", [escaped]
    if ns.exclude_prefixes:
        # Belt-and-suspenders cap: the config validator already rejects
        # >10, but if a caller constructs NamespaceFilter directly we still
        # refuse to emit a pathologically long WHERE clause.
        assert len(ns.exclude_prefixes) <= 10, (
            f"namespace_sql: exclude_prefixes has {len(ns.exclude_prefixes)} entries, cap is 10"
        )
        clauses = " AND ".join("namespace NOT LIKE ? ESCAPE '\\'" for _ in ns.exclude_prefixes)
        params = [f"{escape_like(p)}%" for p in ns.exclude_prefixes]
        return clauses, params
    return "", []


def quote_ident(name: str) -> str:
    """Quote a SQLite identifier for interpolation into DDL/DML.

    ``reset_all`` interpolates table names discovered from ``sqlite_master``
    (including tables an older binary has never heard of), so bracket quoting
    (``[name]``) is unsafe — a valid identifier containing ``]`` would produce
    invalid SQL and abort a privacy reset. Double-quote with embedded ``"``
    doubled per the SQL standard.

    Lives here rather than in ``sqlite_backend`` so the delegated ops modules
    (which the backend imports) can use it without an import cycle.
    """
    return '"' + name.replace('"', '""') + '"'
