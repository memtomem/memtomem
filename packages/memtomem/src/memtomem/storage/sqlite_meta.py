"""Embedding metadata management for the SQLite backend."""

from __future__ import annotations

import sqlite3
from typing import Callable


class MetaManager:
    """Manages the ``_memtomem_meta`` key-value table."""

    def __init__(self, get_db: Callable[[], sqlite3.Connection]) -> None:
        self._get_db = get_db

    # ---- generic meta helpers ------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        db = self._get_db()
        row = db.execute("SELECT value FROM _memtomem_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        db = self._get_db()
        db.execute(
            "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES (?, ?)",
            (key, value),
        )
        db.commit()

    # ---- dimension helpers ---------------------------------------------------

    def get_stored_dimension(self) -> int | None:
        v = self.get_meta("embedding_dimension")
        return int(v) if v is not None else None

    def store_dimension(self, dim: int) -> None:
        self.set_meta("embedding_dimension", str(dim))

    # ---- embedding info property builders ------------------------------------

    def stored_embedding_info(
        self,
        dimension: int,
        provider: str,
        model: str,
    ) -> dict:
        """Return the embedding config actually stored in the DB."""
        return {
            "dimension": dimension,
            "provider": self.get_meta("embedding_provider") or provider,
            "model": self.get_meta("embedding_model") or model,
        }

    # ---- reset ---------------------------------------------------------------

    def reset_embedding_meta(
        self,
        dimension: int,
        provider: str,
        model: str,
    ) -> None:
        """Update all embedding-related meta rows.

        The caller is responsible for dropping/recreating ``chunks_vec``
        and committing the transaction.
        """
        self.store_dimension(dimension)
        if provider:
            self.set_meta("embedding_provider", provider)
        if model:
            self.set_meta("embedding_model", model)
