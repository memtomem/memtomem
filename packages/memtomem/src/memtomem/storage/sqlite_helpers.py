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
