"""``is_writer_busy`` tells a held writer from every other storage failure."""

from __future__ import annotations

import sqlite3

import pytest

from memtomem.errors import StorageError, TransactionOwnedError
from memtomem.storage.sqlite_errors import is_writer_busy


def _wrapped(message: str, cause: BaseException) -> StorageError:
    try:
        raise StorageError(message) from cause
    except StorageError as exc:
        return exc


@pytest.mark.parametrize(
    "exc",
    [
        TransactionOwnedError("SQLite transaction is owned by another task"),
        _wrapped("write failed", sqlite3.OperationalError("database is locked")),
        _wrapped("write failed", sqlite3.OperationalError("database table is busy")),
    ],
)
def test_a_held_writer_is_busy(exc):
    assert is_writer_busy(exc)


@pytest.mark.parametrize(
    "exc",
    [
        StorageError("Database is closing"),
        _wrapped("write failed", sqlite3.OperationalError("disk I/O error")),
        _wrapped("write failed", sqlite3.IntegrityError("UNIQUE constraint failed")),
        _wrapped("write failed", ValueError("database is locked")),
    ],
)
def test_other_storage_failures_are_not_busy(exc):
    assert not is_writer_busy(exc)
