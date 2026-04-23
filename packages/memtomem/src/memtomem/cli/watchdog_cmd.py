"""CLI: mm watchdog — health monitoring commands."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import click


@click.group()
def watchdog() -> None:
    """Health watchdog — check system status and run health checks."""


@watchdog.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def watchdog_status(as_json: bool) -> None:
    """Show latest health check results."""
    try:
        asyncio.run(_watchdog_status(as_json))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


@watchdog.command("run")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def watchdog_run(as_json: bool) -> None:
    """Run all health checks immediately and show results."""
    try:
        asyncio.run(_watchdog_run(as_json))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


@watchdog.command("history")
@click.argument("check_name")
@click.option("--hours", default=24.0, help="Hours to look back (default: 24)")
def watchdog_history(check_name: str, hours: float) -> None:
    """Show historical results for a specific check."""
    try:
        asyncio.run(_watchdog_history(check_name, hours))
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e)) from e


# ── Async implementations ──────────────────────────────────────────


async def _watchdog_status(as_json: bool) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.server.health_store import HealthStore

    async with cli_components() as comp:
        db_path = Path(comp.config.storage.sqlite_path).expanduser().resolve()
        store = HealthStore(db_path, max_snapshots=1000)
        store.initialize()
        try:
            summary = store.get_summary()
        finally:
            store.close()

    if not summary:
        click.echo("No health check data found. Enable the watchdog or run: mm watchdog run")
        return

    if as_json:
        click.echo(json.dumps(summary, indent=2, default=str))
        return

    _print_summary(summary)


async def _watchdog_run(as_json: bool) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.config import HealthWatchdogConfig
    from memtomem.server.context import AppContext
    from memtomem.server.health_watchdog import HealthWatchdog

    async with cli_components() as comp:
        ctx = AppContext.from_components(comp)
        config = HealthWatchdogConfig(enabled=True)
        wd = HealthWatchdog(ctx, config)
        await wd.start()
        try:
            results = await wd.run_now()
        finally:
            await wd.stop()

    if as_json:
        click.echo(json.dumps(results, indent=2, default=str))
        return

    _print_results(results)


async def _watchdog_history(check_name: str, hours: float) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.server.health_store import HealthStore

    async with cli_components() as comp:
        db_path = Path(comp.config.storage.sqlite_path).expanduser().resolve()
        store = HealthStore(db_path, max_snapshots=1000)
        store.initialize()
        try:
            trend = store.get_trend(check_name, hours)
        finally:
            store.close()

    if not trend:
        click.echo(f"No history for '{check_name}' in the last {hours}h.")
        return

    import datetime

    click.echo(f"History for '{check_name}' (last {hours}h): {len(trend)} snapshots\n")
    for snap in trend[-30:]:
        ts = datetime.datetime.fromtimestamp(snap.created_at).strftime("%Y-%m-%d %H:%M:%S")
        marker = _status_marker(snap.status)
        vals = " ".join(f"{k}={v}" for k, v in snap.value.items())
        click.echo(f"  {marker} {ts}  {vals}")


# ── Formatting helpers ─────────────────────────────────────────────


_MARKERS = {
    "ok": click.style("OK", fg="green"),
    "warning": click.style("WARN", fg="yellow"),
    "critical": click.style("CRIT", fg="red"),
}


def _status_marker(status: str) -> str:
    return _MARKERS.get(status, status)


def _print_summary(summary: dict) -> None:
    click.echo("Health Watchdog Status\n")
    for name in sorted(summary):
        info = summary[name]
        marker = _status_marker(info["status"])
        vals = " ".join(f"{k}={v}" for k, v in info["value"].items())
        click.echo(f"  {marker}  {name} [{info['tier']}]  {vals}")
    click.echo()


def _print_results(results: dict) -> None:
    click.echo("Health Check Results\n")
    criticals = warnings = 0
    for name in sorted(results):
        info = results[name]
        marker = _status_marker(info["status"])
        vals = " ".join(f"{k}={v}" for k, v in info.get("value", {}).items())
        click.echo(f"  {marker}  {name}  {vals}")
        if info["status"] == "critical":
            criticals += 1
        elif info["status"] == "warning":
            warnings += 1

    click.echo()
    if criticals:
        click.echo(click.style(f"  {criticals} critical, {warnings} warning", fg="red"))
    elif warnings:
        click.echo(click.style(f"  {warnings} warning", fg="yellow"))
    else:
        click.echo(click.style("  All checks passed", fg="green"))
