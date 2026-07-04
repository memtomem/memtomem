"""Tests for the shared CLI error helper (#1617).

Two layers: the class→hint mapping in ``cli/_errors.py``, and an
integration pin through a wrapped command (``mm status``) proving the
hint actually reaches the user instead of the bare ``str(e)`` the
catch-all sites used to emit.
"""

from __future__ import annotations

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
from memtomem.cli._errors import raise_cli_error
from memtomem.errors import (
    ConfigError,
    EmbeddingDimensionMismatchError,
    EmbeddingError,
    SchemaDowngradeError,
    StorageError,
)


def _message_of(exc: Exception) -> str:
    with pytest.raises(click.ClickException) as info:
        raise_cli_error(exc)
    return info.value.format_message()


class TestHintMapping:
    @pytest.mark.parametrize(
        ("exc", "fragment"),
        [
            (sqlite3.OperationalError("database is locked"), "another process is writing"),
            (sqlite3.OperationalError("no such table: chunks"), "run `mm init`"),
            (EmbeddingDimensionMismatchError("dim 0 vs 1024"), "mm embedding-reset"),
            (SchemaDowngradeError("schema 2 > 1"), "`mm upgrade`"),
            (EmbeddingError("model not found"), "docs/guides/embeddings.md"),
            (ConfigError("bad json"), "mm config show"),
            (StorageError("disk I/O error"), "run `mm status`"),
        ],
    )
    def test_known_classes_get_hints(self, exc: Exception, fragment: str) -> None:
        message = _message_of(exc)
        assert str(exc) in message, "original message must be preserved"
        assert "Hint:" in message
        assert fragment in message

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
