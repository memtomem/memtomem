"""CLI: mm reset — drop all data and reinitialize the database.

Destructive by intent, so it carries the same operational gates as
``mm uninstall`` (#1574 item 7, hardened by #1945):

* **Liveness gates** — refuse while the MCP server or ``mm web`` is
  running (``cli/_liveness.py`` flock probes). A live writer racing
  ``reset_all`` can lose concurrent writes or observe a half-wiped DB.
* **Write-lock probe** — ``BEGIN IMMEDIATE`` via ``cli/_db_lock.py``
  catches writers the pid files don't know about (ad-hoc scripts,
  watchdog). Runs BEFORE the storage backend is constructed so reset's
  own migrations never write to an un-gated DB.
* **Instance-registry gate** (#1935, #1945) — refuses on LIVE, UNKNOWN,
  and UNTRUSTED registry evidence, closing the probes' blind spot: a
  *secondary* server owns no ``server.pid`` and an *idle* server holds
  no write lock. Deliberately **user-wide**, like uninstall's: the probe
  and the lifecycle barrier below are per-user, not per-store, so a live
  server on an *unrelated* custom store also refuses reset. Fail-closed
  is the accepted trade-off — no store-scoped fail-closed probe exists.
* **Lifecycle barrier** (#1936, #1945) — probes are snapshots, so each
  of reset's two write boundaries re-probes *under* an exclusive hold of
  ``runtime_dir()/lifecycle.lock``: once around ``initialize()`` (which
  may migrate, i.e. write), once around backup + ``reset_all()``. A
  starting server takes the barrier shared before opening storage, so it
  either blocks or is already visible to the re-probe. The barrier is
  never held across the confirmation prompt — that would fail-close
  legitimate server startups for as long as the user sits on it, the
  shape #1936 explicitly rejected for uninstall.
* ``--force`` bypasses only the pid/web/db-lock heuristics, for
  stale-pid recovery (uninstall parity). It never overrides registry
  evidence or a held barrier — those are positive liveness, not
  heuristics. ``--yes`` skips only the confirmation prompt, never the
  gates.
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
import stat
import sys
from datetime import datetime
from pathlib import Path

import click

from memtomem._instance_registry import (
    BarrierTimeout,
    HeldBarrier,
    UninstallProbeResult,
)
from memtomem._instance_registry import (
    acquire_uninstall_lifecycle_barrier as _acquire_lifecycle_barrier,
)
from memtomem._instance_registry import (
    probe_all_for_uninstall as _probe_registry_liveness,
)
from memtomem._settlement import settle_shielded_value
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
    help="Bypass the stale-pid/db-lock safety heuristics (use only if you know "
    "the pid or lock is stale). Does NOT override instance-registry evidence "
    "of a live server or a held lifecycle barrier — those are positive "
    "liveness, not heuristics.",
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


def _refuse(message: str, hint: str, *, cause: str | None = None, as_json: bool = False) -> None:
    if as_json:
        # Write-command JSON error shape (CONTRIBUTING): keep stdout
        # machine-readable while signaling the handled failure via exit 1.
        parts = [message, f"Cause: {cause}" if cause else None, hint]
        reason = " ".join(p for p in parts if p)
        click.echo(json.dumps({"ok": False, "reason": reason}))
        sys.exit(1)
    click.secho(message, fg="red")
    if cause:
        click.secho(f"  Cause: {cause}", fg="red")
    click.secho(f"  {hint}", fg="red")
    sys.exit(2)


def _refuse_registry(probe: UninstallProbeResult, *, as_json: bool = False) -> None:
    """Refuse on registry evidence — never ``--force``-overridable.

    Mirrors ``mm uninstall``'s gate (#1935, #1942) in reset's own
    vocabulary. The three causes prescribe different remediations and
    must not drift into each other's advice: only UNKNOWN (transient)
    may advise retrying; UNTRUSTED (persistent) names the offending path
    — printed verbatim by design, this is the CLI surface where the
    canonical-path redaction rule (#1385, #1550) does not apply. No
    ``pass --force`` hint anywhere: advertising an override that does
    not apply would be false remediation.
    """
    if probe.state == "LIVE":
        _refuse(
            "A live memtomem-server instance is registered for this user. "
            "Refusing to reset — an active server holds the store open, and "
            "wiping it underneath can lose writes or corrupt the WAL.",
            "Stop every memtomem-server (close editor sessions using "
            "memtomem) and retry. --force does not override this check.",
            as_json=as_json,
        )
    elif probe.state == "UNTRUSTED":
        where = str(probe.untrusted_path) if probe.untrusted_path is not None else "its path"
        # Two flavors, keyed on the remediation vocabulary like uninstall's
        # ``_refuse_untrusted_registry`` (#1938): a redirected probe path
        # vs. a real-but-unprobeable entry (stray subdirectory,
        # permission-denied path, unlistable ``instances/``).
        if probe.untrusted_kind == "unprobeable":
            message = (
                f"The instance registry cannot be trusted: {where} cannot be "
                "probed — it is a stray subdirectory, link, or permission-denied "
                "path in the sentinel registry. Refusing to reset — liveness "
                "cannot be judged while any part of the registry is unreadable."
            )
        else:
            message = (
                f"The instance registry cannot be trusted: {where} is a symlink, "
                "junction, or otherwise not a private real directory. Refusing "
                "to reset — liveness cannot be judged through a redirected path."
            )
        _refuse(
            message,
            "Remove or repair that path, then retry — retrying without "
            "fixing it cannot succeed. --force does not override this check.",
            cause=probe.detail,
            as_json=as_json,
        )
    else:
        _refuse(
            "Could not determine whether a memtomem-server instance is still "
            "running (instance-registry probe did not complete). Refusing to "
            "reset.",
            "Retry in a moment. --force does not override this check.",
            as_json=as_json,
        )


def _check_gates(db_path: Path, *, force: bool = False, as_json: bool = False) -> None:
    """Refuse (exit 2; exit 1 + ``ok: false`` under ``--json``) on any
    evidence of a live writer.

    The registry gate runs first and unconditionally — ``--force``
    covers only the pid/web/db-lock heuristics below it (#1945). Called
    up to three times per run: once un-barriered (rich per-cause
    messages, without making the common live-server case wait out a
    barrier timeout first) and once under each barrier hold, because a
    probe outside the hold is only a snapshot.
    """
    registry = _probe_registry_liveness()
    if registry.state != "NONE":
        _refuse_registry(registry, as_json=as_json)
    if force:
        return
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


async def _acquire_barrier_settled() -> HeldBarrier:
    """Exclusive lifecycle-barrier acquire, offloaded and cancellation-settled.

    Same contract as the server's shared acquire
    (``AppContext._acquire_lifecycle_barrier``): the blocking flock wait
    is forbidden directly on the event loop, and the settlement must be
    :func:`settle_shielded_value`, not ``settle_shielded_result`` — that
    variant swallows worker failures, which here would let a
    ``BarrierTimeout`` vanish and the wipe proceed unbarriered. A handle
    acquired just as a cancellation lands is handed back, released, and
    the cancellation re-raised — never dropped on the floor as a hold
    nothing can release.
    """
    future = asyncio.ensure_future(asyncio.to_thread(_acquire_lifecycle_barrier))
    result, cancelled = await settle_shielded_value(future, what="lifecycle barrier")
    if not isinstance(result, HeldBarrier):
        # Fail closed on a contract change — the settlement helper erases
        # the result type to ``object``.
        raise RuntimeError(f"lifecycle barrier returned {type(result).__name__}, not HeldBarrier")
    if cancelled is not None:
        result.release()
        raise cancelled
    return result


async def _acquire_barrier_or_refuse(*, as_json: bool = False) -> HeldBarrier:
    """Acquire the barrier or refuse the way the gates do.

    Deliberately no ``--force`` hint in either refusal: a held flock is
    never stale — the kernel releases it when its holder dies — and an
    unusable barrier path is infrastructure, not a heuristic. The two
    causes prescribe different remediations (#1870): contention is fixed
    by stopping the holder, a direct ``OSError`` (unusable runtime dir,
    barrier-file permissions) only by repairing the reported path —
    "stop it" would send that user hunting for a process that does not
    exist.
    """
    try:
        return await _acquire_barrier_settled()
    except BarrierTimeout as exc:
        _refuse(
            f"A memtomem process is starting or holding the lifecycle "
            f"barrier ({exc}). Refusing to reset.",
            "Stop it and re-run mm reset.",
            as_json=as_json,
        )
        raise  # unreachable — _refuse exits; keeps the return type honest
    except OSError as exc:
        _refuse(
            f"The lifecycle barrier could not be used ({exc}). Refusing to reset.",
            "Repair the reported path, then retry — this is an infrastructure "
            "failure, not a running process.",
            as_json=as_json,
        )
        raise  # unreachable — _refuse exits; keeps the return type honest


def _release_or_retain(barrier: HeldBarrier, close_confirmed: bool) -> None:
    """Release the barrier, or retain it on an unconfirmed storage close.

    The server-side polarity (#1936, ``AppContext._release_lifecycle_barrier``):
    a ``close()`` that raised leaves a possibly-open store, which must
    keep blocking servers and uninstalls — the kernel frees the flock
    when this process exits. The propagating close exception explains
    itself; this only surfaces the retention.
    """
    if close_confirmed:
        barrier.release()
        return
    click.secho(
        f"Warning: storage close unconfirmed — retaining the lifecycle "
        f"barrier at {barrier.path} until process exit.",
        fg="yellow",
        err=True,
    )


def _store_fingerprint(db_path: Path) -> tuple[int, int, int, int] | None:
    """A consent-integrity fingerprint of the DB file, or ``None`` if it
    is absent / not a regular file.

    Richer than ``store_digest_for``'s ``(st_dev, st_ino)`` on purpose:
    a same-path replacement created after this process unlinks the
    original commonly *reuses* the just-freed inode, so identity alone
    would accept the swap. Adding ``st_size`` and ``st_mtime_ns`` catches
    that — an independently written replacement differs in at least one.
    Still best-effort defense-in-depth behind the barrier and the
    registry gate; a durable handle is not an option here because holding
    one across the confirmation prompt is the very shape #1936 rejected.
    """
    try:
        st = os.stat(db_path)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return None
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


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

    def _make_backend() -> SqliteBackend:
        return SqliteBackend(
            cfg.storage,
            dimension=cfg.embedding.dimension,
            embedding_provider=cfg.embedding.provider,
            embedding_model=cfg.embedding.model,
        )

    # Un-barriered pass first: rich per-cause messages without making the
    # common live-server case wait out a barrier timeout (uninstall
    # precedent). --yes must never skip any gate (uninstall -y precedent).
    _check_gates(db_path, force=force, as_json=as_json)

    # Phase A (#1945): initialize() may run migrations, i.e. write to a DB
    # we have only *probed* to be unowned — and a probe is a snapshot.
    # Re-probe under an exclusive barrier hold and keep it across the
    # write. The backend is opened AND closed inside the hold: carrying an
    # open handle past the release would leave the store unprotected
    # across the prompt, where an uninstall could acquire the barrier,
    # re-probe past our idle connection, and stage the live DB file out
    # from under us.
    barrier = await _acquire_barrier_or_refuse(as_json=as_json)
    close_confirmed = True
    try:
        _check_gates(db_path, force=force, as_json=as_json)
        storage = _make_backend()
        # From construction on, the store may be open: release only after
        # a *confirmed* close (#1936 polarity — see _release_or_retain).
        close_confirmed = False
        try:
            await storage.initialize()
            stats = await storage.get_stats()
            total = stats.get("total_chunks", 0)
        finally:
            # ``close`` is partial-init tolerant; every gate refusal above
            # raises ``SystemExit`` before construction, releasing normally.
            await storage.close()
            close_confirmed = True
        # Fingerprint the store as it was when we counted (and the user
        # will consent). ``None`` here means the file vanished between the
        # close above and this stat — already the removed-during-window
        # case; the Phase B check below fails it closed.
        store_fp = _store_fingerprint(db_path)
    finally:
        _release_or_retain(barrier, close_confirmed)

    if total == 0:
        if as_json:
            click.echo(json.dumps({"ok": True, "deleted": {}, "backup": None}))
        else:
            click.echo("Database is already empty — nothing to reset.")
        return

    if not yes:
        # err=as_json keeps stdout pure JSON under --json — the prompt is
        # interactive chrome, and `mm reset --json | jq` must not choke on it.
        # _prompts.confirm bypasses click's Windows stdout leak (#1640).
        # Nothing of ours is open and nothing is held while the user
        # decides: a prompt can be sat on for minutes.
        if not _confirm(
            f"This will permanently delete ALL data ({total} chunks, sessions, "
            f"history, etc.) from the database. Continue?",
            default=False,
            err=as_json,
        ):
            if as_json:
                click.echo(json.dumps({"ok": False, "reason": "cancelled at confirmation prompt"}))
                raise click.exceptions.Exit(1)
            click.echo("Cancelled.")
            return

    # Phase B (#1945): the destructive boundary. Re-acquire, re-probe
    # under the hold, and keep holding across snapshot + wipe so nothing
    # can open the store in between. A server that started during the
    # prompt holds the barrier shared for its process lifetime, so it
    # surfaces here as a timeout refusal — the mechanism working, not a
    # degraded message.
    barrier = await _acquire_barrier_or_refuse(as_json=as_json)
    close_confirmed = True  # nothing open until the wipe backend below
    try:
        _check_gates(db_path, force=force, as_json=as_json)

        # Consent was given for the file we counted in Phase A. If it
        # vanished (a racing ``mm uninstall`` during the prompt) or was
        # swapped for a different file at the same path, the ``total`` in
        # the confirmation no longer describes what is on disk — wiping
        # now would destroy a database the user never saw, and
        # ``initialize()`` below would resurrect one uninstall just
        # removed. Fail closed unless BOTH fingerprints are present and
        # equal: a ``None`` on either side cannot confirm same-file, so a
        # match by absence (``None == None``) must not proceed. Not
        # ``--force``-overridable: consent integrity is not a heuristic.
        current_fp = _store_fingerprint(db_path)
        if current_fp is None or store_fp is None or current_fp != store_fp:
            gone = current_fp is None
            _refuse(
                "The database changed while you were deciding — it was "
                + ("removed" if gone else "replaced")
                + f" at {db_path}. Refusing to reset: the confirmation you "
                "gave was for a different database.",
                "Re-run mm reset to reset the database that is there now.",
                as_json=as_json,
            )

        backup_path: Path | None = None
        if backup:
            # Inside the hold on purpose: a short-lived writer between
            # snapshot and wipe would have its writes wiped yet absent
            # from the backup. After the confirm so a cancelled run
            # leaves no backup litter; abort without wiping if the
            # snapshot fails.
            try:
                backup_path = _backup_db(db_path)
            except (sqlite3.Error, OSError) as exc:
                if as_json:
                    click.echo(
                        json.dumps({"ok": False, "reason": f"backup failed ({exc}); nothing wiped"})
                    )
                    sys.exit(1)
                click.secho(f"Backup failed ({exc}); aborting without wiping.", fg="red")
                sys.exit(1)
            if not as_json:
                click.echo(f"Backup written to {backup_path}")

        # A fresh backend for the wipe — Phase A's was closed before the
        # prompt, precisely so nothing of ours outlived that hold.
        storage = _make_backend()
        close_confirmed = False
        try:
            await storage.initialize()
            deleted = await storage.reset_all()
        finally:
            await storage.close()
            close_confirmed = True
    finally:
        _release_or_retain(barrier, close_confirmed)

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
