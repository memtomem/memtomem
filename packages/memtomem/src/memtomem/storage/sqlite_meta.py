"""Embedding metadata management for the SQLite backend."""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from typing import Callable, ContextManager, Iterator

logger = logging.getLogger(__name__)


def _standalone_commit(db: sqlite3.Connection) -> None:
    db.commit()


@contextmanager
def _standalone_write_guard(db: sqlite3.Connection) -> Iterator[None]:
    """Roll back a failed standalone write instead of stranding its transaction.

    A rollback that itself fails must not replace the failure the caller is
    about to see (the same preservation contract as the backend's
    ``_rolls_back_if_standalone``), so it is logged at ERROR instead.
    """
    try:
        yield
    except BaseException:
        try:
            db.rollback()
        except Exception:
            logger.error(
                "rollback after a failed meta write raised; the writer's transaction "
                "may still be open on the connection (#2167)",
                exc_info=True,
            )
        raise


class MetaManager:
    """Manages the ``_memtomem_meta`` key-value table.

    ``commit`` and ``write_guard`` let the owning backend route this
    manager's writes through its transaction-ownership machinery
    (``_commit_if_standalone`` / ``_rolls_back_if_standalone``), so a
    ``set_meta`` inside an owned ``transaction()`` neither ends the
    owner's transaction early (#2158) nor strands a failed one (#2167).
    Standalone constructions (schema helpers, tests) keep the plain
    commit/rollback defaults.
    """

    def __init__(
        self,
        get_db: Callable[[], sqlite3.Connection],
        *,
        commit: Callable[[sqlite3.Connection], None] = _standalone_commit,
        write_guard: Callable[[sqlite3.Connection], ContextManager[None]] = _standalone_write_guard,
    ) -> None:
        self._get_db = get_db
        self._commit = commit
        self._write_guard = write_guard

    # ---- generic meta helpers ------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        db = self._get_db()
        row = db.execute("SELECT value FROM _memtomem_meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        db = self._get_db()
        with self._write_guard(db):
            db.execute(
                "INSERT OR REPLACE INTO _memtomem_meta(key, value) VALUES (?, ?)",
                (key, value),
            )
            self._commit(db)

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
        policy_fingerprint: str = "",
        max_sequence_tokens: int | None = None,
    ) -> dict:
        """Return the embedding config actually stored in the DB."""
        stored_max = self.get_meta("embedding_max_sequence_tokens")
        return {
            "dimension": dimension,
            "provider": self.get_meta("embedding_provider") or provider,
            "model": self.get_meta("embedding_model") or model,
            "policy_fingerprint": self.get_meta("embedding_policy_fingerprint")
            or policy_fingerprint,
            "max_sequence_tokens": int(stored_max)
            if stored_max is not None
            else max_sequence_tokens,
        }

    # ---- reset ---------------------------------------------------------------

    def reset_embedding_meta(
        self,
        dimension: int,
        provider: str,
        model: str,
        policy_fingerprint: str = "",
        max_sequence_tokens: int | None = None,
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
        if policy_fingerprint:
            self.set_meta("embedding_policy_fingerprint", policy_fingerprint)
        if max_sequence_tokens is not None:
            self.set_meta("embedding_max_sequence_tokens", str(max_sequence_tokens))
