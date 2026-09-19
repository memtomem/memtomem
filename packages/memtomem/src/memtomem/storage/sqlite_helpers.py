"""Shared utility functions for the SQLite backend."""

from __future__ import annotations

import os
import struct
import unicodedata
import hashlib
from datetime import datetime, timezone
from fnmatch import fnmatch
from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

from memtomem.models import NamespaceFilter


def serialize_f32(vector: list[float]) -> bytes:
    """Pack a float vector into raw bytes for sqlite-vec."""
    return struct.pack(f"{len(vector)}f", *vector)


def deserialize_f32(data: bytes) -> list[float]:
    """Unpack raw bytes back to a float vector."""
    n = len(data) // 4
    return list(struct.unpack(f"{n}f", data))


def norm_path(p: Path) -> str:
    """Normalize path to a canonical string.

    Resolves symlinks (``/tmp`` → ``/private/tmp`` on macOS) and applies
    Unicode NFC normalization so NFD (typically produced by macOS/APFS) and
    NFC (typed by users or emitted by some cloud clients) forms of the same
    path compare equal. Without NFC here, non-ASCII paths such as
    ``~/Library/CloudStorage/GoogleDrive-.../내 드라이브/...`` can fail the
    equality check used by the web routes (see issue #235).
    """
    try:
        resolved = str(p.resolve())
    except OSError:
        resolved = str(p)
    return unicodedata.normalize("NFC", resolved)


def norm_dir_prefix(d: str | Path) -> str:
    """Return the directory path normalized for ``str.startswith`` matching.

    Adds a trailing ``os.sep`` (platform-native separator) so a configured
    dir does not falsely claim files under a sibling sharing the same
    prefix (e.g. ``/foo`` should not match ``/foo-bar/...``). Always runs
    through :func:`norm_path` (which resolves symlinks and applies Unicode
    NFC) so the prefix shape matches the source-side normalisation
    regardless of whether the dir currently exists on disk — the chunks
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


def _swap_case_component(name: str) -> str | None:
    """Return ``name`` with its case flipped, or ``None`` if it carries none."""
    swapped = name.swapcase()
    return swapped if swapped != name else None


_CASE_PROBE_CACHE: dict[str, bool] = {}

#: Prefix of the file the writing arm creates. Carries no ``.md`` suffix so an
#: indexed directory does not treat it as content, and is removed immediately.
_CASE_PROBE_PREFIX = ".memtomem-case-probe-"


def _same_directory(base: Path, alias: Path) -> bool | None:
    """Whether ``alias`` names the same directory as ``base``, writing nothing.

    Sound for *directories* in a way it is not for files: a directory cannot be
    hard-linked (measured: ``EPERM`` on macOS and Linux), so two directory names
    that resolve to one inode are the same entry rather than a forged pair. A
    **symlink** could still forge it, so either side being one is inconclusive.

    ``samefile`` raises :class:`FileNotFoundError` both when ``alias`` is absent
    — the conclusive *case-sensitive* — and when ``base`` has gone since it was
    seen, which answers nothing; the caller caches what it is told, so ``base``
    is asked again rather than recording a transient failure as a property.
    """
    try:
        if os.path.islink(base) or os.path.islink(alias):
            return None
    except OSError:
        return None
    try:
        return base.samefile(alias)
    except FileNotFoundError:
        try:
            if base.exists():
                return False
        except OSError:
            return None
        return None
    except OSError:
        return None


def _probe_by_spelling(directory: Path) -> bool | None:
    """Re-spell ``directory``'s own name. Reads only — safe inside a protected root."""
    swapped_name = _swap_case_component(directory.name)
    if swapped_name is None:
        return None
    return _same_directory(directory, directory.parent / swapped_name)


def _probe_by_writing(directory: Path) -> bool | None:
    """Create one file in ``directory`` and ask whether a re-spelled name finds it.

    The fallback for a directory whose name cannot be flipped. Asking about a
    name **this function just created** is what makes it trustworthy where
    inspecting existing entries is not: among files, a hard link or two symlinks
    to one target make ``Note.md`` and ``nOTE.MD`` the same file on a
    case-sensitive filesystem while the directory keeps the names apart.

    Never called on a read-only root — see :func:`_probe_case_insensitive`.
    """
    name = f"{_CASE_PROBE_PREFIX}{uuid4().hex}-Probe"
    swapped_name = _swap_case_component(name)
    if swapped_name is None:  # pragma: no cover - the literal suffix has a case
        return None
    probe = directory / name
    try:
        fd = os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        return None
    try:
        os.close(fd)
        return os.path.exists(directory / swapped_name)
    except OSError:
        return None
    finally:
        try:
            os.unlink(probe)
        except OSError:
            pass


def _same_device(path: Path, device: int) -> bool:
    """Whether ``path`` is on ``device``. A mount boundary ends every walk here:
    another filesystem's case semantics are not this one's."""
    try:
        return path.stat().st_dev == device
    except OSError:
        return False


def _probe_case_insensitive(root: str) -> bool | None:
    """Ask the filesystem holding ``root``, or ``None`` when it cannot answer.

    ``root`` may not have been created yet, so the question goes to the nearest
    **existing** directory — the one the root will be created inside, hence the
    same filesystem.

    Two arms, and the order is the point. ``root`` is a directory memtomem has
    promised not to write into, and that promise is about *this application*,
    not about the mode bits: a read-only root is very often OS-writable. So the
    read-only arm goes first and is the only one allowed to touch the root
    itself. The writing arm is the fallback for a name with no case to flip, and
    it starts at the root's **parent** so nothing is ever created inside the
    protected directory.

    Both walks stop at a mount boundary, and every unanswerable case returns
    ``None`` rather than a guess, so the caller can decline to remember it.
    """
    probe_dir = Path(root)
    while True:
        try:
            if probe_dir.is_dir():
                break
        except OSError:
            # Permission and other stat failures propagate out of ``is_dir()``;
            # an unreadable root must not break checks for unrelated targets.
            return None
        parent = probe_dir.parent
        if parent == probe_dir:
            return None
        probe_dir = parent

    try:
        device = probe_dir.stat().st_dev
    except OSError:
        return None

    cursor = probe_dir
    while True:
        answer = _probe_by_spelling(cursor)
        if answer is not None:
            return answer
        parent = cursor.parent
        if parent == cursor or not _same_device(parent, device):
            break
        cursor = parent

    cursor = probe_dir.parent
    while cursor != probe_dir and _same_device(cursor, device):
        answer = _probe_by_writing(cursor)
        if answer is not None:
            return answer
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    return None


def _root_is_case_insensitive(root: str) -> bool:
    """Whether the filesystem holding ``root`` treats case as insignificant.

    Probed rather than inferred from the platform: macOS ships case-insensitive
    APFS by default but can mount case-sensitive volumes, Windows can expose
    case-sensitive directories, and Linux can mount either. Asking the
    filesystem is the only answer that is right on all of them.

    Anything unanswerable is reported as case-*sensitive*, the answer that adds
    no refusals — but that answer is **not** remembered. A conclusive probe
    describes a mount and is cached, because this runs on every write-target
    check; an inconclusive one describes only the moment it ran. Caching it
    would leave a read-only root that is configured before it is created
    unprotected against differently cased spellings for the life of the
    process, which is precisely the window in which a provider directory is
    declared and then synced into place.

    The cache still assumes a given path keeps its case semantics once the
    answer is conclusive; a volume remounted with the opposite semantics over
    the same path mid-process is not tracked.
    """
    cached = _CASE_PROBE_CACHE.get(root)
    if cached is not None:
        return cached
    answer = _probe_case_insensitive(root)
    if answer is None:
        return False
    _CASE_PROBE_CACHE[root] = answer
    return answer


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

    On a case-insensitive filesystem the comparison also folds case, decided
    per root by :func:`_root_is_case_insensitive` rather than by platform: the
    same machine can host both kinds of volume. Folding is deliberately not
    unconditional — on a case-sensitive filesystem ``/vault`` and ``/Vault``
    really are two directories, and refusing writes to the unprotected one
    would be a wrong answer, not a cautious one.
    """
    if not roots:
        return False
    path = Path(target).expanduser()
    resolved = norm_path(path)
    # ``norm_path`` would resolve the leaf too, which is exactly what this
    # second spelling must not do.
    entry = norm_path(path.parent).rstrip(os.sep) + os.sep + path.name
    spellings = (resolved, entry)
    for root in roots:
        prefix = norm_dir_prefix(root)
        bare = prefix.rstrip(os.sep)
        if any(s == bare or s.startswith(prefix) for s in spellings):
            return True
        if _root_is_case_insensitive(bare):
            folded_prefix, folded_bare = prefix.casefold(), bare.casefold()
            if any(
                s.casefold() == folded_bare or s.casefold().startswith(folded_prefix)
                for s in spellings
            ):
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
