"""Tests for the shared CLI error helper (#1617).

Two layers: the class→hint mapping in ``cli/_errors.py``, and an
integration pin through a wrapped command (``mm status``) proving the
hint actually reaches the user instead of the bare ``str(e)`` the
catch-all sites used to emit.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import click
import pytest
from click.testing import CliRunner

import memtomem.cli
from memtomem.cli import cli
from memtomem.cli._errors import (
    exit_json_failure,
    is_handled_failure,
    raise_cli_error,
    to_cli_error,
)
from memtomem.errors import (
    ConfigError,
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    NamespaceMutationBusyError,
    NamespaceResolutionError,
    SchemaDowngradeError,
    SchemaMigrationBlockedError,
    StorageError,
    StorageStartupError,
)


def _message_of(exc: Exception) -> str:
    with pytest.raises(click.ClickException) as info:
        raise_cli_error(exc)
    return info.value.format_message()


_HINT_CASES = [
    (sqlite3.OperationalError("database is locked"), "another process is writing"),
    (sqlite3.OperationalError("no such table: chunks"), "run `mm init`"),
    (EmbeddingDimensionMismatchError("dim 0 vs 1024"), "mm embedding-reset"),
    (SchemaDowngradeError("schema 2 > 1"), "`mm upgrade`"),
    # Subclass of SchemaDowngradeError with its own hint (#2564).
    (SchemaMigrationBlockedError("older servers are running"), "older memtomem server"),
    (EmbeddingError("model not found"), "docs/guides/embeddings.md"),
    (ConfigError("bad json"), "mm config show"),
    (StorageError("disk I/O error"), "run `mm status`"),
    # The open-time lock gets the lock hint, not "run `mm status`" — which
    # would be the advice inside `mm status` itself (#2589).
    (StorageStartupError(reason_code="storage_locked", stage="open"), "another process is writing"),
    (StorageStartupError(reason_code="storage_unavailable", stage="open"), "run `mm status`"),
    # The engine's pre-write namespace prepass (issue #2005) raises
    # instead of returning per-file errors, so this is the only place
    # a retryable indexing failure keeps its classification on the
    # direct-CLI path — print_index_errors never sees it.
    (NamespaceResolutionError("store unreachable"), "retry the same command"),
]


class TestHintMapping:
    @pytest.mark.parametrize(("exc", "fragment"), _HINT_CASES)
    def test_known_classes_get_hints(self, exc: Exception, fragment: str) -> None:
        message = _message_of(exc)
        assert str(exc) in message, "original message must be preserved"
        assert "Hint:" in message
        assert fragment in message

    def test_retryable_wins_over_storage_for_the_dual_inheritance_case(self) -> None:
        """``NamespaceMutationBusyError`` is both ``StorageError`` and
        ``RetryableError``. What the operator should do (retry) is more
        actionable than which subsystem raised, so the retryable branch must
        sit ahead of the storage one — this pins the ordering rather than
        leaving it to import/branch accident."""
        message = _message_of(NamespaceMutationBusyError("snapshot changed"))

        assert "retry the same command" in message
        assert "storage backend error" not in message

    def test_unknown_exception_falls_back_to_plain_message(self) -> None:
        message = _message_of(RuntimeError("boom"))
        assert message == "boom"

    def test_unknown_operational_error_gets_no_hint(self) -> None:
        # Only the recognized sqlite messages map; others stay bare so we
        # never attach a misleading remediation.
        message = _message_of(sqlite3.OperationalError("disk I/O error"))
        assert message == "disk I/O error"
        assert "Hint:" not in message

    def test_empty_message_falls_back_to_class_name(self) -> None:
        message = _message_of(RuntimeError())
        assert message == "RuntimeError"

    def test_click_exception_passes_through_unwrapped(self) -> None:
        original = click.ClickException("already tailored")
        with pytest.raises(click.ClickException) as info:
            raise_cli_error(original)
        assert info.value is original

    def test_chains_original_exception(self) -> None:
        exc = StorageError("disk I/O error")
        with pytest.raises(click.ClickException) as info:
            raise_cli_error(exc)
        assert info.value.__cause__ is exc


class TestToCliError:
    """``to_cli_error`` is the non-raising half of ``raise_cli_error``, for
    callers that render the error themselves (``mm status --json``, #2575)."""

    @pytest.mark.parametrize(
        "exc", [exc for exc, _ in _HINT_CASES] + [RuntimeError("boom"), RuntimeError()]
    )
    def test_same_message_as_the_raised_form(self, exc: Exception) -> None:
        assert to_cli_error(exc).format_message() == _message_of(exc)

    def test_click_exception_is_returned_unchanged(self) -> None:
        original = click.ClickException("already tailored")
        assert to_cli_error(original) is original


def _operational_error(code: int | None) -> sqlite3.OperationalError:
    exc = sqlite3.OperationalError("stamped")
    if code is not None:
        exc.sqlite_errorcode = code  # type: ignore[attr-defined]
    return exc


def _real_query_defect() -> sqlite3.OperationalError:
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("SELECT no_such_column")
    except sqlite3.OperationalError as exc:
        return exc
    finally:
        conn.close()
    raise AssertionError("expected SQLITE_ERROR")


class TestIsHandledFailure:
    """The classification every ``--json`` envelope shares (#2575, #2589):
    SQLite errors by result code, never by message text."""

    @pytest.mark.parametrize(
        "code",
        [
            sqlite3.SQLITE_PERM,
            sqlite3.SQLITE_BUSY,
            sqlite3.SQLITE_LOCKED,
            sqlite3.SQLITE_READONLY,
            sqlite3.SQLITE_IOERR,
            sqlite3.SQLITE_FULL,
            sqlite3.SQLITE_CANTOPEN,
            # Extended code: the primary code sits in the low byte.
            sqlite3.SQLITE_IOERR_READ,
        ],
    )
    def test_environmental_codes_are_handled(self, code: int) -> None:
        assert is_handled_failure(_operational_error(code))

    def test_mem2mem_errors_are_handled(self) -> None:
        assert is_handled_failure(StorageStartupError(reason_code="storage_locked", stage="open"))
        assert is_handled_failure(ConfigError("bad json"))

    @pytest.mark.parametrize(
        "exc",
        [
            _real_query_defect(),
            _operational_error(sqlite3.SQLITE_ERROR),
            # No code, even with environmental wording: no classification.
            sqlite3.OperationalError("database is locked"),
            sqlite3.ProgrammingError("closed database"),
            RuntimeError("boom"),
        ],
    )
    def test_everything_else_is_not(self, exc: Exception) -> None:
        assert not is_handled_failure(exc)


class TestExitJsonFailure:
    """``exit_json_failure`` is ``raise_cli_error``'s ``--json`` twin (#2589)."""

    @staticmethod
    def _run(exc: Exception, shape: str) -> tuple[int, str]:
        @click.command()
        def cmd() -> None:
            try:
                raise exc
            except Exception as e:
                exit_json_failure(e, shape=shape)  # type: ignore[arg-type]

        result = CliRunner().invoke(cmd, [])
        return result.exit_code, result.stdout

    def test_error_shape(self) -> None:
        code, out = self._run(ConfigError("bad json"), "error")
        assert code == 1
        assert json.loads(out) == {"error": to_cli_error(ConfigError("bad json")).format_message()}

    def test_ok_shape(self) -> None:
        code, out = self._run(_operational_error(sqlite3.SQLITE_FULL), "ok")
        assert code == 1
        assert json.loads(out) == {"ok": False, "reason": "stamped"}

    def test_click_exception_is_enveloped(self) -> None:
        code, out = self._run(click.ClickException("refused"), "error")
        assert code == 1
        assert json.loads(out) == {"error": "refused"}

    def test_usage_error_stays_plain(self) -> None:
        """Like the usage errors Click raises while parsing, before any wrapper."""
        code, out = self._run(click.UsageError("bad flag"), "error")
        assert code == 2
        assert out == ""

    def test_unhandled_failure_stays_plain(self) -> None:
        code, out = self._run(RuntimeError("boom"), "error")
        assert code == 1
        assert out == ""

    @pytest.mark.parametrize("exc", [click.exceptions.Exit(130), click.Abort()])
    def test_click_control_flow_passes_through(self, exc: Exception) -> None:
        with pytest.raises(type(exc)) as info:
            exit_json_failure(exc, shape="error")
        assert info.value is exc


class TestRaiseCliErrorControlFlow:
    """``Exit`` and ``Abort`` are ``RuntimeError`` subclasses, so every
    ``except Exception: raise_cli_error(e)`` wrapper caught them and printed
    ``Error: 130`` / ``Error: Abort`` with exit 1 (#2589)."""

    @pytest.mark.parametrize("exc", [click.exceptions.Exit(130), click.Abort()])
    def test_passes_through_untouched(self, exc: Exception) -> None:
        with pytest.raises(type(exc)) as info:
            raise_cli_error(exc)
        assert info.value is exc


class TestNoBareCatchAllRegression:
    """#1617 sweep pin: no ``except Exception`` in ``cli/`` may re-wrap
    as a bare ``ClickException(str(e))`` — that's ``raise_cli_error``'s
    job now. Typed conversions (``except WikiNotFoundError`` etc.) stay
    allowed: their messages are already the actionable text."""

    _PATTERN = re.compile(
        r"except Exception as (\w+):\n"  # catch-all only
        r"(?:[^\n]*\n){0,2}?"  # tolerate up to 2 bookkeeping lines (trace_ctx etc.)
        r"\s*raise click\.ClickException\(str\(\1\)\)",
    )

    def test_no_catch_all_rewrap_in_cli(self) -> None:
        cli_dir = Path(memtomem.cli.__file__).parent
        offenders = []
        for path in sorted(cli_dir.glob("*.py")):
            if path.name == "_errors.py":  # docstring shows the old shape
                continue
            for match in self._PATTERN.finditer(path.read_text(encoding="utf-8")):
                # Windows-safe: report by file name only.
                offenders.append(f"{path.name}: {match.group(0).splitlines()[0]}")
        assert not offenders, (
            "bare `except Exception -> ClickException(str(e))` re-wraps found; "
            f"route them through cli._errors.raise_cli_error instead: {offenders}"
        )


class TestWrappedCommandIntegration:
    """A locked DB surfacing through ``mm status`` carries the hint."""

    def test_status_locked_db_shows_hint(self, monkeypatch: pytest.MonkeyPatch) -> None:
        storage = SimpleNamespace(
            get_stats=AsyncMock(side_effect=sqlite3.OperationalError("database is locked")),
        )
        comp = SimpleNamespace(config=None, storage=storage, embedder=SimpleNamespace())

        @asynccontextmanager
        async def fake():
            yield comp

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

        result = CliRunner().invoke(cli, ["status"])

        assert result.exit_code != 0
        assert "database is locked" in result.output
        assert "Hint: another process is writing" in result.output


class TestClickControlFlowThroughCommands:
    """The passthrough reaches the command wrappers that raise these (#2589)."""

    def test_interrupted_index_exits_130(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        async def interrupted(*args: object, **kwargs: object) -> None:
            raise click.exceptions.Exit(130)

        monkeypatch.setattr("memtomem.cli.indexing._index", interrupted)

        result = CliRunner().invoke(cli, ["index", str(tmp_path)])

        assert result.exit_code == 130
        assert "Error:" not in result.stderr

    def test_declined_add_prints_aborted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def declined(*args: object, **kwargs: object) -> None:
            raise click.Abort()

        monkeypatch.setattr("memtomem.cli.memory._add", declined)

        result = CliRunner().invoke(cli, ["add", "a note"])

        assert result.exit_code == 1
        assert "Aborted!" in result.stderr
        assert "Error:" not in result.stderr
