"""Prompt helpers that keep ``--json`` stdout byte-clean on every platform."""

from __future__ import annotations

import sys

import click


def confirm(text: str, *, default: bool = False, err: bool = False) -> bool:
    """``click.confirm`` with a stdout-safe stderr path.

    With ``err=False`` this defers to ``click.confirm`` unchanged. With
    ``err=True`` the prompt is written to stderr and the reply is read
    straight from ``sys.stdin``, bypassing click's prompt machinery: click
    8.4's ``_readline_prompt`` redirects the prompt function's stdout to
    stderr on POSIX but not on Windows, where the prompt tail (and, under
    ``CliRunner``, the echoed reply) leaks into stdout and corrupts the
    single-JSON-document contract of ``--json`` runs (#1640).
    """
    if not err:
        return click.confirm(text, default=default)
    suffix = " [Y/n]: " if default else " [y/N]: "
    while True:
        click.echo(f"{text}{suffix}", nl=False, err=True)
        line = sys.stdin.readline()
        if not line:
            # EOF — mirror click.confirm: nothing to answer with, abort.
            click.echo(err=True)
            raise click.Abort()
        value = line.strip().lower()
        if value in ("y", "yes"):
            return True
        if value in ("n", "no"):
            return False
        if not value:
            return default
        click.echo("Error: invalid input", err=True)
