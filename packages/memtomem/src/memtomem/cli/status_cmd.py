"""CLI: mm status — terminal mirror of the MCP ``mem_status`` tool (#382)."""

from __future__ import annotations

import asyncio
import json
import os
from typing import TYPE_CHECKING, Any

import click

from memtomem.cli._errors import raise_cli_error

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
            click.echo(json.dumps({"error": e.format_message()}))
            raise click.exceptions.Exit(1)
        raise
    except Exception as e:
        raise_cli_error(e)


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
        else:  # "warning_kv" | "blank" — plain on purpose
            styled.append(line.text)

    return "\n".join(styled)


def _style_guidance_line(line: str) -> str:
    line = line.replace("  ->", click.style("  ->", fg="yellow", bold=True), 1)
    for command in ("`mm init`", "`mm embedding-reset`"):
        line = line.replace(command, click.style(command, fg="cyan", bold=True))
    return line
