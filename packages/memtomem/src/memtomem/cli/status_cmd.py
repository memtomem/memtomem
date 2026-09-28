"""CLI: mm status — terminal mirror of the MCP ``mem_status`` tool (#382)."""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from typing import TYPE_CHECKING, Any, NoReturn

import click

from memtomem.cli._errors import raise_cli_error, to_cli_error
from memtomem.errors import Mem2MemError

if TYPE_CHECKING:
    from memtomem.server.tools.status_config import StatusLine


@click.command("status")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.option("--json", "as_json", is_flag=True, help="Shortcut for --format json.")
def status(fmt: str, *, as_json: bool = False) -> None:
    """Show indexing statistics and current configuration summary.

    Mirrors the MCP ``mem_status`` tool — same output, callable from a
    terminal without an MCP client. Useful as a post-install sanity
    check that the binary works, the config is readable, and the DB is
    reachable, without having to run a search.
    """
    # --json is an alias for --format json (CONTRIBUTING "CLI output
    # convention"); if both are passed, --json wins since it's the more
    # specific intent.
    if as_json:
        fmt = "json"

    try:
        asyncio.run(_status(fmt))
    except click.ClickException as e:
        if fmt == "json":
            _exit_json_error(e)
        raise
    except Exception as e:
        if fmt == "json" and _is_handled_failure(e):
            _exit_json_error(to_cli_error(e))
        raise_cli_error(e)


def _is_handled_failure(e: Exception) -> bool:
    """Whether a JSON caller gets the ``{"error": ...}`` envelope for ``e``.

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


def _exit_json_error(e: click.ClickException) -> NoReturn:
    click.echo(json.dumps({"error": e.format_message()}))
    raise click.exceptions.Exit(1)


async def _status(fmt: str) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.server.context import AppContext
    from memtomem.server.tools.status_config import (
        collect_status_report,
        iter_status_lines,
        render_status_report,
    )

    async with cli_components() as comp:
        ctx = AppContext.from_components(comp)
        data = await collect_status_report(ctx)

    if fmt == "json":
        click.echo(json.dumps(data, indent=2, ensure_ascii=False, default=str))
    elif "NO_COLOR" in os.environ:
        click.echo(render_status_report(data))
    else:
        click.echo(_style_status_lines(iter_status_lines(data)))


_TONE_STYLES: dict[str, dict[str, Any]] = {
    "title": {"fg": "cyan", "bold": True},
    "plain": {"bold": True},
    "warn": {"fg": "yellow", "bold": True},
}

_DENSE_STATE_COLORS = {"full": "green", "partial": "yellow", "none": "red", "empty": "yellow"}


def _style_status_lines(lines: list[StatusLine]) -> str:
    """Add terminal-only scanability hints without changing report text."""
    styled: list[str] = []

    for line in lines:
        if line.role == "title":
            styled.append(click.style(line.text, fg="cyan", bold=True))
        elif line.role in ("rule", "section"):
            styled.append(click.style(line.text, **_TONE_STYLES[line.meta["tone"]]))
        elif line.role == "dense":
            styled.append(
                click.style(line.key, bold=True)
                + click.style(
                    line.value + line.suffix,
                    fg=_DENSE_STATE_COLORS[line.meta["state"]],
                    bold=True,
                )
            )
        elif line.role == "guidance":
            styled.append(_style_guidance_line(line.text))
        elif line.role in ("kv", "immutable_kv"):
            value_fg = line.meta.get("value_fg")
            value = click.style(line.value, fg=value_fg) if value_fg else line.value
            styled.append(click.style(line.key, bold=True) + value + line.suffix)
        else:  # source rows, warning rows, and blanks are plain on purpose
            styled.append(line.text)

    return "\n".join(styled)


def _style_guidance_line(line: str) -> str:
    line = line.replace("  ->", click.style("  ->", fg="yellow", bold=True), 1)
    for command in ("`mm init`", "`mm embedding-reset`"):
        line = line.replace(command, click.style(command, fg="cyan", bold=True))
    return line
