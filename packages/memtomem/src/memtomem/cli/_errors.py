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

import json
import sqlite3
from typing import Literal, NoReturn

import click


def raise_cli_error(e: Exception) -> NoReturn:
    """Re-raise ``e`` as a ``ClickException``, appending a next-step hint
    for recognized failure classes.

    ``ClickException`` passes through unchanged so already-tailored
    messages keep their wording and exit semantics; everything else
    falls back to the plain ``str(e)`` the call sites used before.

    Click's own control-flow exceptions (``Exit``, ``Abort``) are
    ``RuntimeError`` subclasses, so a wrapper's ``except Exception`` catches
    them; they are re-raised untouched rather than re-worded as an error
    (``mm index`` interrupted keeps exit 130, not ``Error: 130``).
    """
    if isinstance(e, (click.exceptions.Exit, click.Abort)):
        raise e
    converted = to_cli_error(e)
    if converted is e:
        raise e
    raise converted from e


def to_cli_error(e: Exception) -> click.ClickException:
    """Build the ``ClickException`` that ``raise_cli_error`` would raise,
    without raising it — for callers that render the message themselves,
    such as a ``--json`` error envelope (#2575)."""
    if isinstance(e, click.ClickException):
        return e
    message = str(e) or type(e).__name__
    hint = _hint_for(e)
    if hint:
        return click.ClickException(f"{message}\n  Hint: {hint}")
    return click.ClickException(message)


def exit_json_failure(e: Exception, *, shape: Literal["error", "ok"]) -> NoReturn:
    """``--json`` counterpart of ``raise_cli_error`` (#2589).

    A handled failure — a ``ClickException`` or anything
    ``is_handled_failure`` accepts — is printed as one JSON object and exits
    with the converted error's exit code: ``{"error": ...}`` for commands
    whose success payload carries no ``ok`` flag, ``{"ok": false, "reason":
    ...}`` for those whose success does (CONTRIBUTING "JSON error shape").
    Anything else goes through ``raise_cli_error``, so a defect stays a
    plain error with nothing on stdout. So does a usage error (exit 2):
    Click raises most of those while parsing options, before any wrapper
    runs, so the ones a command raises itself stay on stderr alongside them.
    """
    if isinstance(e, (click.exceptions.Exit, click.Abort)):
        raise e
    if not isinstance(e, click.UsageError) and (
        isinstance(e, click.ClickException) or is_handled_failure(e)
    ):
        converted = to_cli_error(e)
        message = converted.format_message()
        payload = {"error": message} if shape == "error" else {"ok": False, "reason": message}
        click.echo(json.dumps(payload))
        raise click.exceptions.Exit(converted.exit_code)
    raise_cli_error(e)


def is_handled_failure(e: Exception) -> bool:
    """Whether a JSON caller gets the error envelope for ``e``.

    Classified failures — a storage open refused by the schema fence, a
    locked or unreachable database — are exactly when a script needs the
    structured answer (#2575). Everything else, including a SQL error in
    memtomem's queries once the database is open, stays a plain Click error
    so a defect is not dressed up as a handled failure.

    A failure while opening storage is the exception: the storage layer
    already wraps whatever went wrong, defects included, as a path-safe
    ``StorageStartupError``. Text mode prints that same wrapped message, so
    the envelope carries nothing text mode would not.
    """
    from memtomem.errors import Mem2MemError

    if isinstance(e, Mem2MemError):
        return True
    if isinstance(e, sqlite3.OperationalError):
        # SQLite raises OperationalError for environmental failures and for
        # query defects (``no such column``, syntax errors: SQLITE_ERROR)
        # alike, so decide by result code, never by message text — a defect
        # can name a column ``readonly``. No code means no classification.
        code = getattr(e, "sqlite_errorcode", None)
        # Extended codes (SQLITE_IOERR_READ, ...) keep the primary code in
        # the low byte.
        return isinstance(code, int) and code & 0xFF in _ENVIRONMENTAL_SQLITE_CODES
    return False


# Primary SQLite result codes for failures of the database's surroundings
# (another writer, permissions, the filesystem, the disk), as opposed to
# memtomem's own SQL.
_ENVIRONMENTAL_SQLITE_CODES = frozenset(
    {
        sqlite3.SQLITE_PERM,
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
        sqlite3.SQLITE_READONLY,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_FULL,
        sqlite3.SQLITE_CANTOPEN,
    }
)

_LOCKED_HINT = (
    "another process is writing to the database (MCP server, mm web, "
    "or the watchdog) — stop it and retry. On POSIX, "
    "`ps aux | grep memtomem` finds live writers."
)


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
        RetryableError,
        SchemaDowngradeError,
        SchemaMigrationBlockedError,
        StorageError,
        StorageStartupError,
    )

    if isinstance(e, sqlite3.OperationalError):
        text = str(e).lower()
        if "database is locked" in text:
            return _LOCKED_HINT
        if "no such table" in text:
            return "the database looks uninitialized — run `mm init`, then `mm index <path>`."
        return None
    if isinstance(e, EmbeddingDimensionMismatchError):
        return (
            "stored and configured embedding settings disagree — `mm status` shows "
            "the mismatch; `mm embedding-reset --mode apply-current` repairs it."
        )
    if isinstance(e, SchemaMigrationBlockedError):
        # Ahead of its base class: nothing newer wrote this database — this
        # binary declined to migrate it under running older servers (#2564).
        return "an older memtomem server may still have this database open — stop it, then retry."
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
            "check the active configuration with `mm config show`, then edit "
            "~/.memtomem/config.json or re-run `mm init`."
        )
    if isinstance(e, StorageStartupError) and e.reason_code == "storage_locked":
        # The open-time shape of "database is locked": the storage layer
        # classified it before it reached the CLI, so the generic
        # ``StorageError`` hint below ("run `mm status`") would be the only
        # advice — even inside ``mm status`` (#2589).
        return _LOCKED_HINT
    if isinstance(e, RetryableError):
        # Deliberately ahead of ``StorageError``: a retryable failure that is
        # *also* a storage failure (``NamespaceMutationBusyError``) is more
        # usefully described by what the operator should do — retry — than by
        # which subsystem raised. This is also the only path that keeps the
        # retryable classification for failures raised *before* any
        # ``IndexingStats`` exists: the engine's pre-write namespace prepass
        # (issue #2005) raises ``NamespaceResolutionError`` rather than
        # returning per-file errors, so ``print_index_errors`` never sees it.
        return "transient failure — retry the same command once the chunk store is reachable."
    if isinstance(e, StorageError):
        return "storage backend error — run `mm status` to check the DB path and health."
    return None
