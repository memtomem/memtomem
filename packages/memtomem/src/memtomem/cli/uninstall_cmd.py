"""CLI: ``mm uninstall`` — wipe local user state separate from the binary.

Removes files under ``~/.memtomem/`` (db, config, fragments, memories, etc.)
and tells the user the exact package-manager command to remove the binary
itself for their detected install context. Does NOT touch external editor
configs (claude.json etc.) — only detects them and reports the paths the
user must clean up manually.

Design notes:

* State vs binary are separated on purpose. Different install contexts
  (uv-tool / uvx / venv-relative / system / unknown) need different
  uninstall commands; we print the right one but never execute, since the
  package manager owns its own permissions and lifecycle.
* If the MCP server is still running (``.server.pid``) OR any other
  process holds a SQLite write lock on the DB (``mm web``, ``mm
  watchdog``, ad-hoc connections) we refuse — deleting an open SQLite
  file under WAL mode risks corruption. ``--force`` overrides. The
  write-lock probe uses ``BEGIN IMMEDIATE`` to catch writers the pid
  file doesn't know about (see ``_check_db_lock``).
* If config.json is corrupted we fall back to defaults rather than
  aborting — uninstall is itself a recovery path, so it cannot depend on
  a valid config.
"""

from __future__ import annotations

import fcntl
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import click

from memtomem._runtime_paths import legacy_server_pid_path, runtime_dir, server_pid_path
from memtomem.cli.init_cmd import RuntimeProfile, _runtime_profile

_DEFAULT_STATE_DIR = Path.home() / ".memtomem"
_DEFAULT_DB_NAME = "memtomem.db"
_DB_SIBLING_SUFFIXES = ("", "-wal", "-shm", "-journal")
# Subdirectories under the state dir that we own and should rmdir after
# their contents are wiped (otherwise the state dir's empty-prune check
# at the end fails and we leave behind dead skeleton dirs).
_OWNED_SUBDIRS = ("config.d", "memories", "uploads")


def _isatty() -> bool:
    """Indirection so tests can monkeypatch the TTY check.

    ``CliRunner`` substitutes its own ``sys.stdin`` (a ``StringIO``) whose
    ``isatty()`` returns ``False``, so a direct call inside the command
    can't be flipped from the test side without this seam.
    """
    return sys.stdin.isatty()


@dataclass(frozen=True)
class _Group:
    """One row in the printed inventory."""

    label: str
    paths: list[Path]
    bytes_total: int


@dataclass(frozen=True)
class _Inventory:
    """All deletable groups + the resolved state dir, in display order."""

    state_dir: Path
    db_path: Path
    db_files: _Group
    config_files: _Group
    fragment_files: _Group
    backup_files: _Group
    memory_files: _Group
    upload_files: _Group
    other_files: _Group  # session, pid — always wiped unless --keep-config (no, see flag table)


@dataclass(frozen=True)
class _ServerState:
    alive: bool
    pid: int | None
    pid_file: Path | None


@dataclass(frozen=True)
class _DbLockState:
    """Result of probing the SQLite DB for an active writer.

    ``locked`` is True only when another connection holds a RESERVED /
    PENDING / EXCLUSIVE lock at probe time — i.e. an active writer.
    Pure readers (SHARED locks only) are not detected; that's an
    accepted tradeoff (see ``_check_db_lock``).
    """

    locked: bool
    probe_error: str | None


@dataclass(frozen=True)
class _External:
    path: Path
    reason: str


# ---- safe config load ----------------------------------------------------


def _load_config_safely() -> tuple[Path, str | None]:
    """Return ``(db_path, error_or_none)``.

    On any failure (malformed JSON, missing fields, permission error)
    falls back to the default DB path and returns the error message so
    the caller can surface it as a yellow warning. Uninstall is a
    recovery scenario — it must work when config is broken.
    """
    try:
        from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

        cfg = Mem2MemConfig()
        load_config_d(cfg, quiet=True)
        load_config_overrides(cfg)
        db_path = Path(cfg.storage.sqlite_path).expanduser()
        return db_path, None
    except Exception as exc:  # noqa: BLE001 — uninstall must never abort on config
        return _DEFAULT_STATE_DIR / _DEFAULT_DB_NAME, f"{type(exc).__name__}: {exc}"


# ---- server liveness probe -----------------------------------------------


def _probe_pid_file(pid_file: Path) -> _ServerState:
    """Probe a single pid file via ``fcntl.flock``.

    ``server/__init__.py:main`` opens this file and holds an exclusive
    flock for the entire server lifetime. If we can acquire
    ``LOCK_EX | LOCK_NB`` on it, no live writer is holding it (the file
    is a stale leftover, or fresh and unowned). If we cannot, a writer
    is alive — *regardless* of whether the recorded PID is still valid,
    has been recycled to an unrelated process, or was never set at all.

    This replaces the previous ``os.kill(pid, 0)`` probe, which was
    correct for stale-pid cases but produced false positives once the
    kernel recycled the recorded PID to an unrelated process (issue
    #387). The PID inside the file is now read for display only.
    """
    if not pid_file.exists():
        return _ServerState(alive=False, pid=None, pid_file=None)

    pid: int | None
    try:
        pid_text = pid_file.read_text().strip()
        pid = int(pid_text)
    except (OSError, ValueError):
        # Unreadable / non-int — leave pid=None for the message; the lock
        # probe below still decides alive vs. dead independently.
        pid = None

    try:
        # Read-mode is enough; advisory flock works regardless of fd mode.
        # Probing ``rb`` rather than ``rb+`` avoids any chance of accidental
        # truncation on an exotic filesystem if we later abort.
        fp = open(pid_file, "rb")
    except OSError:
        # Conservative — couldn't even open the file (permissions, race
        # with deletion). Treat as alive so the user explicitly --forces.
        return _ServerState(alive=True, pid=pid, pid_file=pid_file)

    try:
        try:
            fcntl.flock(fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Another process is holding the lock → live writer.
            return _ServerState(alive=True, pid=pid, pid_file=pid_file)
        except OSError:
            # Unsupported filesystem (some NFS configurations, FUSE) or
            # other unknown error — be conservative.
            return _ServerState(alive=True, pid=pid, pid_file=pid_file)
        # Got the lock. Release immediately — we don't want to hold it,
        # only probe; holding would block a server starting in the gap
        # between this probe and the eventual unlink.
        fcntl.flock(fp, fcntl.LOCK_UN)
        return _ServerState(alive=False, pid=pid, pid_file=pid_file)
    finally:
        fp.close()


def _check_server_liveness() -> _ServerState:
    """Probe the server pid file at both the new and legacy locations.

    As of #412 the server writes its pid / lock file under
    ``$XDG_RUNTIME_DIR/memtomem/server.pid`` (see ``_runtime_paths``).
    During the transition window we also probe the legacy
    ``~/.memtomem/.server.pid`` so a mixed-version upgrade (pre-#412
    server still running, new uninstall CLI) refuses correctly. First
    live holder wins; if neither is held the state is dead.

    No ``state_dir`` parameter — both probes use canonical absolute
    paths from :mod:`memtomem._runtime_paths`. When #384 expands the
    scheme to cover other writers (``mm web``, ``mm watchdog``) the
    glob will still live inside :func:`_runtime_paths.runtime_dir`,
    not under the persistent state dir.
    """
    for pid_file in (server_pid_path(), legacy_server_pid_path()):
        state = _probe_pid_file(pid_file)
        if state.alive:
            return state
    return _ServerState(alive=False, pid=None, pid_file=None)


def _check_db_lock(db_path: Path) -> _DbLockState:
    """Probe whether another connection holds a write lock on ``db_path``.

    Motivation: the ``.server.pid`` check only catches the MCP
    ``memtomem-server`` entrypoint. ``mm web``, ``mm watchdog``, and any
    user-run sqlite3 connection are invisible to that scheme, so
    uninstall could silently proceed while a live writer was holding the
    WAL (observed in issue #384).

    Mechanism: open a short-timeout connection and attempt
    ``BEGIN IMMEDIATE`` — that tries to acquire a RESERVED lock and
    raises ``SQLITE_BUSY`` (``sqlite3.OperationalError`` whose message
    contains "locked"/"busy") if any other connection holds
    RESERVED/PENDING/EXCLUSIVE. On success we ``ROLLBACK`` immediately;
    the probe never modifies data.

    Tradeoff: a process that only reads (SHARED lock) does NOT block
    ``BEGIN IMMEDIATE`` in WAL mode, so a quiet-at-probe-time reader
    slips through. That's an accepted tradeoff here — the WAL-corruption
    path (active writer) is the severe case and is what this probe is
    meant to guard. Complete reader-detection would need an ``lsof``
    fallback or an extended pid-file scheme (see issue #384 discussion).

    Error handling: if the probe can't run (file missing, corrupt,
    permission denied, sqlite unavailable), returns ``locked=False`` with
    ``probe_error`` set. Uninstall is itself a recovery path and must not
    be blocked by unrelated DB integrity issues.
    """
    if not db_path.exists():
        return _DbLockState(locked=False, probe_error=None)

    # Header gate: only probe real SQLite files. Opening a corrupt /
    # non-SQLite file with ``mode=rw`` can still trigger side effects on
    # sibling ``-wal`` / ``-shm`` files (observed: a fake-content WAL
    # got unlinked when SQLite tried to verify it). Stay out of that
    # code path unless the file is actually a SQLite database.
    try:
        with db_path.open("rb") as fh:
            header = fh.read(16)
    except OSError as exc:
        return _DbLockState(locked=False, probe_error=f"{type(exc).__name__}: {exc}")
    if header != b"SQLite format 3\x00":
        return _DbLockState(locked=False, probe_error="not a SQLite database")

    conn: sqlite3.Connection | None = None
    try:
        # mode=rw: don't auto-create if the file vanishes between stat
        # and connect (paranoia for concurrent deletions).
        conn = sqlite3.connect(
            f"file:{db_path}?mode=rw",
            uri=True,
            timeout=0.25,
        )
        conn.execute("BEGIN IMMEDIATE")
        conn.rollback()
        return _DbLockState(locked=False, probe_error=None)
    except sqlite3.OperationalError as exc:
        msg = str(exc).lower()
        if "locked" in msg or "busy" in msg:
            return _DbLockState(locked=True, probe_error=None)
        # Other OperationalError (not-a-database, read-only, etc.) — skip
        # probe, let uninstall proceed.
        return _DbLockState(locked=False, probe_error=f"{type(exc).__name__}: {exc}")
    except (sqlite3.Error, OSError) as exc:
        return _DbLockState(locked=False, probe_error=f"{type(exc).__name__}: {exc}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except sqlite3.Error:
                pass


# ---- inventory -----------------------------------------------------------


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _dir_total(path: Path) -> tuple[list[Path], int]:
    if not path.is_dir():
        return [], 0
    files = sorted(p for p in path.rglob("*") if p.is_file())
    return files, sum(_file_size(p) for p in files)


def _make_group(label: str, paths: Iterable[Path]) -> _Group:
    paths_list = sorted(paths)
    return _Group(label=label, paths=paths_list, bytes_total=sum(_file_size(p) for p in paths_list))


def _collect_inventory(db_path: Path) -> _Inventory:
    state_dir = _DEFAULT_STATE_DIR

    # Database + WAL/SHM/journal siblings (handles custom storage path).
    db_paths: list[Path] = []
    for suffix in _DB_SIBLING_SUFFIXES:
        candidate = db_path.with_name(db_path.name + suffix) if suffix else db_path
        if candidate.exists():
            db_paths.append(candidate)

    config_json = state_dir / "config.json"
    config_files = [config_json] if config_json.exists() else []

    fragment_dir = state_dir / "config.d"
    fragments, _ = _dir_total(fragment_dir)

    backups = sorted(state_dir.glob("config.json.bak-*")) if state_dir.exists() else []

    memory_dir = state_dir / "memories"
    memories, _ = _dir_total(memory_dir)

    upload_dir = state_dir / "uploads"
    uploads, _ = _dir_total(upload_dir)

    other: list[Path] = []
    for name in (".current_session", ".server.pid"):
        candidate = state_dir / name
        if candidate.exists():
            other.append(candidate)
    # New-location pid file lives outside state_dir (#412: on
    # ``$XDG_RUNTIME_DIR/memtomem/`` or a per-user temp subdir). Include
    # it in the transient "other" group so it's cleaned with the legacy
    # ``.server.pid`` and the user sees a single row per file.
    runtime_pid = server_pid_path()
    if runtime_pid.exists():
        other.append(runtime_pid)

    return _Inventory(
        state_dir=state_dir,
        db_path=db_path,
        db_files=_make_group("Database", db_paths),
        config_files=_make_group("Config", config_files),
        fragment_files=_make_group("Fragments", fragments),
        backup_files=_make_group("Backups", backups),
        memory_files=_make_group("Memories", memories),
        upload_files=_make_group("Uploads", uploads),
        other_files=_make_group("Other", other),
    )


# ---- external integrations ----------------------------------------------


def _probe_external_integrations() -> list[_External]:
    home = Path.home()
    candidates: list[Path] = [
        home / ".claude.json",
        home / ".codex" / "config.toml",
        home / ".cursor" / "mcp.json",
        home / ".codeium" / "windsurf" / "mcp_config.json",
        home / ".gemini" / "settings.json",
        home / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json",
    ]
    cwd_local = Path.cwd() / ".mcp.json"
    if cwd_local.exists():
        candidates.append(cwd_local)

    found: list[_External] = []
    for path in candidates:
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        # Substring detection — first-cut, false-positive-tolerant since
        # this is detect-and-report. LOW 7 in the plan upgrades to a
        # parsed mcpServers.memtomem key check in a follow-up.
        if "memtomem" in text:
            found.append(_External(path=path, reason="contains memtomem MCP entry"))
    return found


# ---- binary uninstall hint ----------------------------------------------


def _binary_uninstall_hint(profile: RuntimeProfile) -> tuple[str, list[str]]:
    """Return ``(label, command_lines)`` for the detected install context."""
    origin = profile.mm_binary_origin
    if origin == "uv-tool":
        return "uv-tool (global)", ["uv tool uninstall memtomem"]
    if origin == "uvx":
        return (
            "uvx (ephemeral — auto-cleaned on process exit)",
            ["No binary uninstall needed for the uvx caller."],
        )
    if origin == "venv-relative":
        venv_str = (
            str(profile.workspace_venv_path) if profile.workspace_venv_path else "<workspace>/.venv"
        )
        return (
            f"workspace venv ({venv_str})",
            [
                "uv pip uninstall memtomem",
                f"  # or: rm -rf {venv_str}",
            ],
        )
    if origin == "system":
        return (
            f"system Python ({profile.runtime_interpreter})",
            [
                "pip uninstall memtomem",
                "  # or: pipx uninstall memtomem",
                "  # (sudo may be required if the interpreter is system-owned)",
            ],
        )
    # unknown
    return (
        "unknown — could not classify install context",
        [
            "Check `which mm` and use the matching uninstall command:",
            "  uv tool uninstall memtomem      # if uv tool",
            "  pipx uninstall memtomem         # if pipx",
            "  pip uninstall memtomem          # if pip into a venv",
        ],
    )


# ---- printing -----------------------------------------------------------


def _human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{n} {unit}"
        n /= 1024  # type: ignore[assignment]
    return f"{n} GB"


def _format_path(p: Path) -> str:
    home = str(Path.home())
    s = str(p)
    return s.replace(home, "~", 1) if s.startswith(home) else s


def _print_group(group: _Group) -> None:
    if not group.paths:
        return
    if len(group.paths) <= 3:
        for path in group.paths:
            click.echo(
                f"  {group.label:<11}  {_format_path(path):<54}  {_human_size(_file_size(path))}"
            )
    else:
        click.echo(
            f"  {group.label:<11}  {_format_path(group.paths[0].parent) + '/'}"
            f" ({len(group.paths)} files)  {_human_size(group.bytes_total)}"
        )


def _print_inventory(inv: _Inventory, *, keep_config: bool, keep_data: bool) -> int:
    """Print inventory grouped + return total bytes that will be deleted."""
    click.echo("memtomem state inventory:")
    if inv.db_path.parent != _DEFAULT_STATE_DIR:
        click.echo(f"  (custom storage path: {_format_path(inv.db_path.parent)})")

    will_delete_total = 0

    def emit(group: _Group, will_delete: bool) -> None:
        nonlocal will_delete_total
        if not group.paths:
            return
        _print_group(group)
        if will_delete:
            will_delete_total += group.bytes_total

    emit(inv.db_files, not keep_data)
    emit(inv.config_files, not keep_config)
    emit(inv.fragment_files, not keep_config)
    emit(inv.backup_files, not keep_config)
    emit(inv.memory_files, not keep_data)
    emit(inv.upload_files, True)
    emit(inv.other_files, True)

    has_any = any(
        g.paths
        for g in (
            inv.db_files,
            inv.config_files,
            inv.fragment_files,
            inv.backup_files,
            inv.memory_files,
            inv.upload_files,
            inv.other_files,
        )
    )
    if not has_any:
        click.echo("  (nothing found)")
    else:
        click.echo(f"\nTotal to delete: ~{_human_size(will_delete_total)}")
    return will_delete_total


def _print_externals(externals: list[_External]) -> None:
    if not externals:
        return
    click.echo("\nExternal integrations (NOT touched — clean up manually if desired):")
    for ext in externals:
        click.echo(f"  {_format_path(ext.path):<54} {ext.reason}")


def _print_binary_hint(label: str, lines: list[str]) -> None:
    click.echo(f"\nBinary install detected: {label}")
    click.echo("After this completes, also run:")
    for line in lines:
        click.echo(f"  {line}")


# ---- ordered deletion ----------------------------------------------------


class _UninstallPartialError(Exception):
    """Raised when deletion fails mid-flight, after some groups succeeded."""

    def __init__(self, last_completed: str, failing_path: Path, original: BaseException) -> None:
        super().__init__(f"failed at {failing_path} after completing: {last_completed}")
        self.last_completed = last_completed
        self.failing_path = failing_path
        self.original = original


def _delete_paths(paths: list[Path]) -> None:
    for path in paths:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)


def _delete_inventory(inv: _Inventory, *, keep_config: bool, keep_data: bool) -> str:
    """Delete in low→high value order. Returns a summary of what was removed.

    Order: pid/session → fragments → backups → config → memories → uploads
    → DB+WAL/SHM/journal. Each group is logged before moving on so partial
    failures leave a recoverable trail.
    """
    completed: list[str] = []

    def step(group_label: str, paths: list[Path]) -> None:
        if not paths:
            return
        try:
            _delete_paths(paths)
        except (OSError, PermissionError) as exc:
            failing = paths[0] if len(paths) == 1 else _DEFAULT_STATE_DIR
            raise _UninstallPartialError(
                last_completed=", ".join(completed) or "nothing yet",
                failing_path=failing,
                original=exc,
            ) from exc
        completed.append(group_label)

    # transient first (always wiped — pid/session are runtime ephemera)
    step("session/pid", inv.other_files.paths)
    # config surface — fragments and backups are config too, so --keep-config
    # preserves them along with config.json (matches the flag table in the plan)
    if not keep_config:
        step("fragments", inv.fragment_files.paths)
        step("backups", inv.backup_files.paths)
        step("config.json", inv.config_files.paths)
    # data
    if not keep_data:
        step("memories", inv.memory_files.paths)
    step("uploads", inv.upload_files.paths)
    if not keep_data:
        step("database", inv.db_files.paths)

    # Prune now-empty subdirs we own (config.d, memories, uploads). _delete_paths
    # only removes individual files within these, so we have to remove the
    # skeleton dirs ourselves before the state-dir prune below can succeed.
    for subdir in _OWNED_SUBDIRS:
        candidate = _DEFAULT_STATE_DIR / subdir
        if candidate.exists() and candidate.is_dir() and not any(candidate.iterdir()):
            try:
                candidate.rmdir()
            except OSError:
                pass

    # If state dir is now empty (no custom storage outside it), prune it.
    if (
        _DEFAULT_STATE_DIR.exists()
        and not any(_DEFAULT_STATE_DIR.iterdir())
        and inv.db_path.parent == _DEFAULT_STATE_DIR
    ):
        try:
            _DEFAULT_STATE_DIR.rmdir()
            completed.append("state dir")
        except OSError:
            pass

    # Prune the runtime subdir (``$XDG_RUNTIME_DIR/memtomem`` or
    # ``$TMPDIR/memtomem-{uid}``) if we emptied it. We don't own the
    # parent (the kernel / OS does), so we only rmdir our own subdir.
    rt = runtime_dir()
    if rt.exists() and rt.is_dir() and not any(rt.iterdir()):
        try:
            rt.rmdir()
        except OSError:
            pass

    return ", ".join(completed) if completed else "nothing"


# ---- click command ------------------------------------------------------


@click.command("uninstall")
@click.option("--keep-config", is_flag=True, help="Preserve config.json + config.d/* + backups.")
@click.option("--keep-data", is_flag=True, help="Preserve the SQLite DB and ~/.memtomem/memories/.")
@click.option(
    "--force",
    is_flag=True,
    help="Bypass the running-server safety check (use only if you know the pid is stale).",
)
@click.option("-y", "--yes", is_flag=True, help="Skip the confirmation prompt.")
def uninstall(keep_config: bool, keep_data: bool, force: bool, yes: bool) -> None:
    """Remove memtomem user state. The binary itself stays — use your package
    manager to uninstall it (the command is printed at the end for your
    install context).
    """
    profile = _runtime_profile()
    db_path, config_error = _load_config_safely()
    state_dir = _DEFAULT_STATE_DIR

    server = _check_server_liveness()
    db_lock = _check_db_lock(db_path)

    # Empty-state fast path.
    if not state_dir.exists() and not db_path.exists():
        click.echo("No memtomem state to remove (~/.memtomem/ does not exist).")
        label, lines = _binary_uninstall_hint(profile)
        _print_binary_hint(label, lines)
        return

    if config_error is not None:
        click.secho(f"  Warning: config unreadable, using defaults: {config_error}", fg="yellow")

    inv = _collect_inventory(db_path)
    externals = _probe_external_integrations()

    will_delete_bytes = _print_inventory(inv, keep_config=keep_config, keep_data=keep_data)
    _print_externals(externals)
    label, lines = _binary_uninstall_hint(profile)
    _print_binary_hint(label, lines)

    if (server.alive or db_lock.locked) and not force:
        click.echo("")
        if server.alive:
            click.secho(
                f"Server still running (pid {server.pid}). Refusing to delete state — "
                "an active server holds the SQLite WAL and deleting it risks corruption.",
                fg="red",
            )
            click.secho("  Stop the server first, or pass --force to override.", fg="red")
        else:
            # db_lock.locked only — writer without .server.pid (mm web,
            # mm watchdog, ad-hoc script, ...). Point the user at lsof /
            # ps so they can find it without another round-trip.
            click.secho(
                f"Another process holds a write lock on {db_path}. Refusing to delete "
                "state — an active writer can corrupt the WAL.",
                fg="red",
            )
            click.secho(
                f"  Find it with `lsof {db_path}` (or `ps aux | grep memtomem`), "
                "stop it, or pass --force to override.",
                fg="red",
            )
        sys.exit(2)

    if will_delete_bytes == 0 and not inv.other_files.paths:
        click.echo("\nNothing to delete with the current flags.")
        return

    # Confirmation. Non-TTY without -y → Abort (mirrors
    # feedback_click_prompt_needs_isatty_gate.md).
    if not yes:
        if not _isatty():
            click.secho(
                "Refusing to delete without confirmation in a non-interactive shell. "
                "Pass -y to proceed.",
                fg="red",
            )
            raise click.Abort()
        if not click.confirm("\nProceed with state deletion?", default=False):
            click.echo("Cancelled — no files were touched.")
            sys.exit(1)

    try:
        summary = _delete_inventory(inv, keep_config=keep_config, keep_data=keep_data)
    except _UninstallPartialError as exc:
        click.secho(
            f"\nDeletion failed at {_format_path(exc.failing_path)}: {exc.original}",
            fg="red",
        )
        click.secho(
            f"  Successfully removed up to: {exc.last_completed}",
            fg="yellow",
        )
        sys.exit(2)

    click.secho(f"\nRemoved: {summary}.", fg="green")
    click.echo("Run the binary uninstall command above to complete removal.")
