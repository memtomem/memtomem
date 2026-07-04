"""Shared CLI error presentation (#1617).

Several commands end in ``except Exception as e: raise
click.ClickException(str(e)) from e`` — no traceback (good) but also no
next-step guidance (bad): a locked DB or an unreadable config surfaces
as a terse upstream message. ``raise_cli_error`` keeps the catch-all
shape but appends an actionable hint when the failure class is one the
CLI knows how to remediate, modeled on the tailored messages in
``reset``/``uninstall``/``upgrade`` and the server-side
``error_handler._KNOWN_EXCEPTIONS`` split.

Usage — replace the bare re-wrap tail:

    except click.ClickException:
        raise
    except Exception as e:
        raise_cli_error(e)
"""

from __future__ import annotations

import sqlite3
from typing import NoReturn

import click


def raise_cli_error(e: Exception) -> NoReturn:
    """Re-raise ``e`` as a ``ClickException``, appending a next-step hint
    for recognized failure classes.

    ``ClickException`` passes through unchanged so already-tailored
    messages keep their wording and exit semantics; everything else
    falls back to the plain ``str(e)`` the call sites used before.
    """
    if isinstance(e, click.ClickException):
        raise e
    message = str(e) or type(e).__name__
    hint = _hint_for(e)
    if hint:
        raise click.ClickException(f"{message}\n  Hint: {hint}") from e
    raise click.ClickException(message) from e


def _hint_for(e: Exception) -> str | None:
    """Map a failure class to a one-line remediation hint (``None`` = no
    mapping). Subclass checks come before their bases (e.g. the embedding
    mismatch before ``StorageError``)."""
    # Lazy import: this module is on the error path of every wrapped
    # command, and cli/ modules keep import time lean.
    from memtomem.errors import (
        ConfigError,
        EmbeddingDimensionMismatchError,
        EmbeddingError,
        SchemaDowngradeError,
        StorageError,
    )

    if isinstance(e, sqlite3.OperationalError):
        text = str(e).lower()
        if "database is locked" in text:
            return (
                "another process is writing to the database (MCP server, mm web, "
                "or the watchdog) — stop it and retry. On POSIX, "
                "`ps aux | grep memtomem` finds live writers."
            )
        if "no such table" in text:
            return "the database looks uninitialized — run `mm init`, then `mm index <path>`."
        return None
    if isinstance(e, EmbeddingDimensionMismatchError):
        return (
            "stored and configured embedding settings disagree — `mm status` shows "
            "the mismatch; `mm embedding-reset --mode apply-current` repairs it."
        )
    if isinstance(e, SchemaDowngradeError):
        return (
            "the database was written by a newer memtomem — upgrade this binary "
            "(`mm upgrade`) instead of downgrading the data."
        )
    if isinstance(e, EmbeddingError):
        return (
            "the embedding backend failed — check provider/model with `mm status` "
            "and see docs/guides/embeddings.md for setup."
        )
    if isinstance(e, ConfigError):
        return (
            "configuration failed to load — inspect it with `mm config show`, check "
            "~/.memtomem/config.json permissions, or re-run `mm init`."
        )
    if isinstance(e, StorageError):
        return "storage backend error — run `mm status` to check the DB path and health."
    return None
