"""Classify storage failures that mean "the writer is held, retry"."""

from __future__ import annotations

import sqlite3

from memtomem.errors import TransactionOwnedError


def is_writer_busy(exc: BaseException) -> bool:
    """Whether a storage error is the writer being held, here or by another process.

    ``TransactionOwnedError`` is another task in this process owning
    ``transaction()`` past its wait. A ``StorageError`` raised from SQLite's
    "database is locked" / "busy" ``OperationalError`` is another process
    holding the write lock past SQLite's own busy timeout.
    """
    if isinstance(exc, TransactionOwnedError):
        return True
    cause = exc.__cause__
    text = f"{exc} {cause}".lower()
    return isinstance(cause, sqlite3.OperationalError) and ("locked" in text or "busy" in text)
