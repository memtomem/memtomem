"""Shared utility functions for the SQLite backend."""

from __future__ import annotations

import struct
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

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
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc).isoformat()
    return value.astimezone(timezone.utc).isoformat()


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
