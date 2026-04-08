"""Pending selection storage backends for SelectiveCompressor."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from typing import Protocol

from memtomem_stm.proxy.compression import PendingSelection


class PendingStore(Protocol):
    """Protocol for pending TOC selection storage."""

    def put(self, key: str, selection: PendingSelection) -> None: ...
    def get(self, key: str) -> PendingSelection | None: ...
    def touch(self, key: str) -> None: ...
    def delete(self, key: str) -> None: ...
    def evict_expired(self, ttl: float) -> None: ...
    def evict_oldest(self, max_size: int) -> None: ...
    def __len__(self) -> int: ...


class InMemoryPendingStore:
    """In-memory pending store (default, single-instance)."""

    def __init__(self) -> None:
        self._data: dict[str, PendingSelection] = {}
        self._order: deque[str] = deque()

    def put(self, key: str, selection: PendingSelection) -> None:
        self._data[key] = selection
        self._order.append(key)

    def get(self, key: str) -> PendingSelection | None:
        return self._data.get(key)

    def touch(self, key: str) -> None:
        sel = self._data.get(key)
        if sel is not None:
            sel.created_at = time.monotonic()

    def delete(self, key: str) -> None:
        self._data.pop(key, None)

    def evict_expired(self, ttl: float) -> None:
        now = time.monotonic()
        expired = {k for k, v in self._data.items() if (now - v.created_at) > ttl}
        for k in expired:
            self._data.pop(k, None)
        if expired:
            self._order = deque(k for k in self._order if k not in expired)

    def evict_oldest(self, max_size: int) -> None:
        while len(self._data) > max_size and self._order:
            oldest = self._order.popleft()
            self._data.pop(oldest, None)

    def __len__(self) -> int:
        return len(self._data)


class SQLitePendingStore:
    """SQLite-backed pending store for multi-instance sharing."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path
        self._db: sqlite3.Connection | None = None
        self._lock = threading.Lock()

    def initialize(self) -> None:
        self._db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._db = sqlite3.connect(str(self._db_path), check_same_thread=False, timeout=5.0)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(
            """CREATE TABLE IF NOT EXISTS pending_selections (
                key TEXT PRIMARY KEY,
                chunks_json TEXT NOT NULL,
                format TEXT NOT NULL,
                created_at REAL NOT NULL,
                total_chars INTEGER NOT NULL
            )"""
        )
        self._db.commit()

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    def _get_db(self) -> sqlite3.Connection:
        if self._db is None:
            raise RuntimeError("SQLitePendingStore not initialized")
        return self._db

    def put(self, key: str, selection: PendingSelection) -> None:
        with self._lock:
            self._get_db().execute(
                "INSERT OR REPLACE INTO pending_selections VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    json.dumps(selection.chunks, ensure_ascii=False),
                    selection.format,
                    time.time(),
                    selection.total_chars,
                ),
            )
            self._get_db().commit()

    def get(self, key: str) -> PendingSelection | None:
        with self._lock:
            row = (
                self._get_db()
                .execute(
                    "SELECT chunks_json, format, created_at, total_chars "
                    "FROM pending_selections WHERE key = ?",
                    (key,),
                )
                .fetchone()
            )
        if row is None:
            return None
        return PendingSelection(
            chunks=json.loads(row[0]),
            format=row[1],
            created_at=row[2],
            total_chars=row[3],
        )

    def touch(self, key: str) -> None:
        with self._lock:
            self._get_db().execute(
                "UPDATE pending_selections SET created_at = ? WHERE key = ?",
                (time.time(), key),
            )
            self._get_db().commit()

    def delete(self, key: str) -> None:
        with self._lock:
            self._get_db().execute("DELETE FROM pending_selections WHERE key = ?", (key,))
            self._get_db().commit()

    def evict_expired(self, ttl: float) -> None:
        cutoff = time.time() - ttl
        with self._lock:
            self._get_db().execute("DELETE FROM pending_selections WHERE created_at < ?", (cutoff,))
            self._get_db().commit()

    def evict_oldest(self, max_size: int) -> None:
        with self._lock:
            self._get_db().execute(
                "DELETE FROM pending_selections WHERE key NOT IN "
                "(SELECT key FROM pending_selections ORDER BY created_at DESC LIMIT ?)",
                (max_size,),
            )
            self._get_db().commit()

    def __len__(self) -> int:
        with self._lock:
            row = self._get_db().execute("SELECT COUNT(*) FROM pending_selections").fetchone()
        return row[0] if row else 0
