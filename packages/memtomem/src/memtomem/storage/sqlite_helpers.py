"""Shared utility functions for the SQLite backend."""

from __future__ import annotations

import struct
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
    """Normalize path to a canonical string (resolves symlinks like /tmp -> /private/tmp on macOS)."""
    try:
        return str(p.resolve())
    except OSError:
        return str(p)


def placeholders(n: int) -> str:
    """Return ``n`` comma-separated SQL ``?`` placeholders."""
    return ",".join("?" * n)


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def namespace_sql(ns: NamespaceFilter) -> tuple[str, list]:
    """Build SQL WHERE fragment + params for a NamespaceFilter."""
    if ns.namespaces:
        ph = ",".join("?" * len(ns.namespaces))
        return f"namespace IN ({ph})", list(ns.namespaces)
    if ns.pattern:
        escaped = ns.pattern.replace("_", r"\_").replace("*", "%")
        return "namespace LIKE ? ESCAPE '\\'", [escaped]
    return "", []
