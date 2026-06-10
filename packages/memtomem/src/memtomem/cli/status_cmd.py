"""CLI: mm status — terminal mirror of the MCP ``mem_status`` tool (#382)."""

from __future__ import annotations

import asyncio
import os
import re

import click


_DENSE_RE = re.compile(r"^(Dense vectors: )(\d+)/(\d+)( \([^)]+\).*)?$")
_KEY_RE = re.compile(r"^([A-Za-z][A-Za-z .-]*:)(\s+.*)$")
_IMMUTABLE_KEY_RE = re.compile(r"^([a-z.]+:)(\s+.*)$")


@click.command("status")
def status() -> None:
    """Show indexing statistics and current configuration summary.

    Mirrors the MCP ``mem_status`` tool — same output, callable from a
    terminal without an MCP client. Useful as a post-install sanity
    check that the binary works, the config is readable, and the DB is
    reachable, without having to run a search.
    """
    try:
        asyncio.run(_status())
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


async def _status() -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.server.context import AppContext
    from memtomem.server.tools.status_config import format_status_report

    async with cli_components() as comp:
        ctx = AppContext.from_components(comp)
        output = await format_status_report(ctx)

    click.echo(_style_status_report(output))


def _style_status_report(output: str) -> str:
    """Add terminal-only scanability hints without changing report text."""

    if "NO_COLOR" in os.environ:
        return output

    lines = output.splitlines()
    styled: list[str] = []
    immutable_section = False

    for line in lines:
        if line == "memtomem Status":
            styled.append(click.style(line, fg="cyan", bold=True))
        elif line == "==============":
            styled.append(click.style(line, fg="cyan", bold=True))
        elif line == "Index stats":
            immutable_section = False
            styled.append(click.style(line, bold=True))
        elif line == "-----------":
            styled.append(click.style(line, bold=True))
        elif line == "Immutable fields (set once at init)":
            immutable_section = True
            styled.append(click.style(line, fg="yellow", bold=True))
        elif line == "------------------------------------":
            styled.append(click.style(line, fg="yellow", bold=True))
        elif line == "Warnings":
            immutable_section = False
            styled.append(click.style(line, fg="yellow", bold=True))
        elif line == "--------":
            styled.append(click.style(line, fg="yellow", bold=True))
        elif line.startswith("Dense vectors: "):
            styled.append(_style_dense_vectors(line))
        elif line.startswith("  ->"):
            styled.append(_style_guidance_line(line))
        elif immutable_section:
            styled.append(_style_key_value(line, _IMMUTABLE_KEY_RE))
        else:
            styled.append(_style_key_value(line, _KEY_RE))

    return "\n".join(styled)


def _style_key_value(line: str, pattern: re.Pattern[str]) -> str:
    match = pattern.match(line)
    if not match:
        return line

    key, value = match.groups()
    if key == "DB path:":
        return click.style(key, bold=True) + click.style(value, fg="cyan")
    return click.style(key, bold=True) + value


def _style_dense_vectors(line: str) -> str:
    match = _DENSE_RE.match(line)
    if not match:
        return click.style(line, bold=True)

    label, with_dense_text, total_text, suffix = match.groups()
    suffix = suffix or ""
    with_dense = int(with_dense_text)
    total = int(total_text)

    if total == 0:
        color = "yellow"
    elif with_dense == total:
        color = "green"
    elif with_dense == 0:
        color = "red"
    else:
        color = "yellow"

    return click.style(label, bold=True) + click.style(
        f"{with_dense_text}/{total_text}{suffix}", fg=color, bold=True
    )


def _style_guidance_line(line: str) -> str:
    line = line.replace("  ->", click.style("  ->", fg="yellow", bold=True), 1)
    for command in ("`mm init`", "`mm embedding-reset`"):
        line = line.replace(command, click.style(command, fg="cyan", bold=True))
    return line
