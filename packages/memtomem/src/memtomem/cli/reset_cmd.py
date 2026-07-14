"""CLI: mm reset — drop all data and reinitialize the database.

Destructive by intent, so it carries the same operational gates as
``mm uninstall`` (#1574 item 7):

* **Liveness gates** — refuse while the MCP server or ``mm web`` is
  running (``cli/_liveness.py`` flock probes). A live writer racing
  ``reset_all`` can lose concurrent writes or observe a half-wiped DB.
* **Write-lock probe** — ``BEGIN IMMEDIATE`` via ``cli/_db_lock.py``
  catches writers the pid files don't know about (ad-hoc scripts,
  watchdog). Runs BEFORE the storage backend is constructed so reset's
  own migrations never write to an un-gated DB.
* ``--force`` bypasses both gates for stale-pid recovery (uninstall
  parity). ``--yes`` skips only the confirmation prompt, never the gates.
* ``--backup`` snapshots the DB to a timestamped
  ``<db-name>.pre-reset-<ts>.bak`` sibling before wiping, via the stdlib
  ``sqlite3`` backup API — a plain file copy would silently lose
  WAL-resident commits. The snapshot is data-gated in ``mm uninstall``'s
  inventory.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import click

from memtomem.cli._db_lock import check_db_lock, sqlite_file_uri
from memtomem.cli._liveness import check_server_liveness, check_web_liveness
from memtomem.cli._prompts import confirm as _confirm


@click.command("reset")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option(
    "--backup",
    is_flag=True,
    help="Snapshot the DB to <db>.pre-reset-<timestamp>.bak before wiping.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Bypass the running-server / write-lock safety gates (use only if you "
    "know the pid or lock is stale).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit a machine-readable JSON ack instead of text output.",
)
def reset(yes: bool, backup: bool, force: bool, as_json: bool) -> None:
    """Delete ALL data (chunks, sessions, history, etc.) and reinitialize the DB.

    Embedding configuration is preserved — no need to re-configure after reset.
    A re-index is required to repopulate memory.
    """
    asyncio.run(_run(yes, backup=backup, force=force, as_json=as_json))


def _refuse(message: str, hint: str, *, as_json: bool = False) -> None:
    if as_json:
        # Write-command JSON error shape (CONTRIBUTING): keep stdout
        # machine-readable while signaling the handled failure via exit 1.
        click.echo(json.dumps({"ok": False, "reason": f"{message} {hint}"}))
        sys.exit(1)
    click.secho(message, fg="red")
    click.secho(f"  {hint}", fg="red")
    sys.exit(2)


def _check_gates(db_path: Path, *, as_json: bool = False) -> None:
    """Refuse (exit 2; exit 1 + ``ok: false`` under ``--json``) while any
    known writer is alive or holds the DB lock."""
    server = check_server_liveness()
    if server.alive:
        who = f"pid {server.pid}" if server.pid is not None else "pid unknown, flock held"
        _refuse(
            f"MCP server still running ({who}). Refusing to reset — a live "
            "writer racing the wipe can lose data or corrupt the WAL.",
            "Stop the server first, or pass --force to override.",
            as_json=as_json,
        )
    web = check_web_liveness()
    if web.alive:
        who = f"pid {web.pid}" if web.pid is not None else "pid unknown, flock held"
        port = f", port {web.port}" if web.port is not None else ""
        _refuse(
            f"mm web still running ({who}{port}). Refusing to reset — the web "
            "UI writes to this database.",
            "Stop it first (mm web is a foreground process), or pass --force to override.",
            as_json=as_json,
        )
    db_lock = check_db_lock(db_path)
    if db_lock.locked:
        _refuse(
            f"Another process holds a write lock on {db_path}. Refusing to reset.",
            f"Find it with `lsof {db_path}` (or `ps aux | grep memtomem`), stop "
            "it, or pass --force to override.",
            as_json=as_json,
        )


def _backup_db(db_path: Path) -> Path:
    """Snapshot ``db_path`` to a timestamped ``.bak`` sibling.

    Uses ``sqlite3.Connection.backup`` — the destination is a standalone,
    consistent database that includes WAL-resident commits (a ``shutil.copy``
    of the main file would silently drop them). Placed next to the actual DB
    so custom storage paths keep their backups co-located.

    The destination is reserved with ``O_CREAT | O_EXCL`` and mode ``0o600``
    (Codex review): ``sqlite3.connect`` alone would create the file with the
    process umask — leaking a private-memory DB as world-readable — and would
    happily open an *existing* backup, silently replacing an earlier snapshot
    on a timestamp collision. An existing backup is never overwritten.
    """
    for _ in range(10):
        ts = datetime.now().strftime("%Y%m%dT%H%M%S-%f")
        dest = db_path.with_name(db_path.name + f".pre-reset-{ts}.bak")
        try:
            fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            continue
        os.close(fd)
        break
    else:
        raise OSError(f"could not reserve a unique backup path next to {db_path}")

    try:
        src = sqlite3.connect(sqlite_file_uri(db_path, mode="ro"), uri=True)
        try:
            dst = sqlite3.connect(dest)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except BaseException:
        # Don't leave a zero-byte / partial reservation behind on failure —
        # the caller aborts the wipe, and a broken .bak would masquerade as
        # a usable snapshot in the uninstall inventory.
        dest.unlink(missing_ok=True)
        raise
    return dest


async def _run(
    yes: bool, *, backup: bool = False, force: bool = False, as_json: bool = False
) -> None:
    from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides
    from memtomem.storage.sqlite_backend import SqliteBackend

    cfg = Mem2MemConfig()
    load_config_d(cfg)
    # migrate=False: reset only needs the db path here — the auto-discover
    # migration would rewrite config.json (and create its lock sidecar) on a
    # read-only lookup, per feedback_doctor_no_migration_loader. The backend
    # construction below still applies the same storage config.
    load_config_overrides(cfg, migrate=False)
    db_path = Path(cfg.storage.sqlite_path).expanduser()

    # Gates run BEFORE SqliteBackend is constructed: initialize() may run
    # migrations, i.e. write to a DB we have not yet verified is unowned.
    # --yes must never skip these (uninstall -y precedent).
    if not force:
        _check_gates(db_path, as_json=as_json)

    storage = SqliteBackend(
        cfg.storage,
        dimension=cfg.embedding.dimension,
        embedding_provider=cfg.embedding.provider,
        embedding_model=cfg.embedding.model,
    )
    await storage.initialize()

    stats = await storage.get_stats()
    total = stats.get("total_chunks", 0)

    if total == 0:
        if as_json:
            click.echo(json.dumps({"ok": True, "deleted": {}, "backup": None}))
        else:
            click.echo("Database is already empty — nothing to reset.")
        await storage.close()
        return

    if not yes:
        # err=as_json keeps stdout pure JSON under --json — the prompt is
        # interactive chrome, and `mm reset --json | jq` must not choke on it.
        # _prompts.confirm bypasses click's Windows stdout leak (#1640).
        if not _confirm(
            f"This will permanently delete ALL data ({total} chunks, sessions, "
            f"history, etc.) from the database. Continue?",
            default=False,
            err=as_json,
        ):
            if as_json:
                click.echo(json.dumps({"ok": False, "reason": "cancelled at confirmation prompt"}))
            else:
                click.echo("Cancelled.")
            await storage.close()
            if as_json:
                raise click.exceptions.Exit(1)
            return

    backup_path: Path | None = None
    if backup:
        # After the confirm so a cancelled run leaves no backup litter;
        # abort without wiping if the snapshot fails.
        try:
            backup_path = _backup_db(db_path)
        except (sqlite3.Error, OSError) as exc:
            await storage.close()
            if as_json:
                click.echo(
                    json.dumps({"ok": False, "reason": f"backup failed ({exc}); nothing wiped"})
                )
                sys.exit(1)
            click.secho(f"Backup failed ({exc}); aborting without wiping.", fg="red")
            sys.exit(1)
        if not as_json:
            click.echo(f"Backup written to {backup_path}")

    deleted = await storage.reset_all()
    await storage.close()

    if as_json:
        click.echo(
            json.dumps(
                {
                    "ok": True,
                    "deleted": {table: count for table, count in deleted.items() if count > 0},
                    "backup": str(backup_path) if backup_path else None,
                }
            )
        )
        return
    click.secho("Database reset complete.", fg="green")
    for table, count in deleted.items():
        if count > 0:
            click.echo(f"  {table}: {count} rows deleted")
    click.echo("\nRun 'mm index <path>' to re-index your memories.")
