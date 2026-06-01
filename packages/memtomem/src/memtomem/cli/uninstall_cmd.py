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

import errno
import json
import os
import shutil
import sqlite3
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import click

from memtomem._runtime_paths import runtime_dir, server_pid_path
from memtomem.cli._liveness import check_server_liveness as _check_server_liveness
from memtomem.cli._liveness import probe_pid_file as _probe_pid_file  # noqa: F401  (test seam)
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
#
# Liveness helpers live in ``memtomem.cli._liveness`` so ``mm upgrade`` can
# share them. They're re-imported above as ``_check_server_liveness`` and
# ``_probe_pid_file`` to keep the existing test seams; the ``ServerState``
# dataclass is consumed via duck-typing so no alias is needed.


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
        home / ".kimi" / "mcp.json",
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

        # ── parsed mcpServers.memtomem key check ──────────────────────────
        # JSON files: check ``mcpServers.memtomem`` via json.loads.
        # Codex TOML: check ``mcp_servers.memtomem`` via tomllib (TOML uses
        #   snake_case dotted keys resolved to nested dicts by tomllib).
        # Unparseable files are silently skipped — uninstall is a recovery
        #   path and must not crash on malformed configs.
        suffix = path.suffix.lower()
        matched = False

        if suffix == ".json":
            try:
                data = json.loads(text)
            except (json.JSONDecodeError, TypeError):
                pass
            else:
                if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
                    if "memtomem" in data["mcpServers"]:
                        matched = True
        elif suffix == ".toml":
            try:
                data = tomllib.loads(text)
            except (tomllib.TOMLDecodeError, TypeError, ValueError):
                pass
            else:
                # tomllib resolves TOML dotted keys into nested dicts, e.g.
                # ``[mcp_servers.memtomem]`` becomes ``{"mcp_servers": {"memtomem": {...}}}``
                # and ``[mcp_servers]`` with ``memtomem = {...}`` also nests.
                if isinstance(data, dict) and isinstance(data.get("mcp_servers"), dict):
                    if "memtomem" in data["mcp_servers"]:
                        matched = True

        if matched:
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


# ---- transactional staging ----------------------------------------------
#
# The wipe is staged-then-finalized so a mid-run failure can't leave the
# user's state dir half-gone (#757). Every to-be-deleted path is moved
# via ``os.replace`` into a sibling ``.uninstall-staging-<pid>/`` dir
# under its anchor (``state_dir`` / custom DB dir / runtime dir);
# ``rename(2)`` is atomic on the same FS so each move either completes
# or doesn't. Any failure rolls back the moves done so far, restoring
# the original layout. After every group is staged, ``shutil.rmtree``
# wipes the staging dirs — failures there leave only orphan staging
# trash, not user-visible data.


_STAGING_PREFIX = ".uninstall-staging-"


@dataclass(frozen=True)
class _StagedMove:
    """A single original→staging move recorded for rollback."""

    original: Path
    staged: Path


class _UninstallStagingError(Exception):
    """A stage move failed; rollback was attempted.

    ``rollback_errors`` is empty when rollback fully restored the
    original layout. When non-empty, the user is left with files split
    between original locations and staging — ``staging_roots`` lists
    the dirs they need to inspect to recover.
    """

    def __init__(
        self,
        failing_path: Path,
        original: BaseException,
        rollback_errors: list[tuple[Path, BaseException]],
        staging_roots: list[Path],
    ) -> None:
        super().__init__(f"failed at {failing_path}: {original}")
        self.failing_path = failing_path
        self.original = original
        self.rollback_errors = rollback_errors
        self.staging_roots = staging_roots


class _UninstallCrossFsError(Exception):
    """Layout spans filesystems — atomic staging via ``os.replace`` not
    possible (raises ``EXDEV``). Surfaced as a clean refusal rather
    than a fall-back to copy+delete: the whole point of staging is
    atomicity, and copy+delete would just reintroduce the half-state
    risk this module is designed to eliminate."""

    def __init__(self, src: Path, anchor: Path) -> None:
        super().__init__(f"cross-FS: {src} not on same FS as {anchor}")
        self.src = src
        self.anchor = anchor


def _build_stage_plan(
    inv: _Inventory, *, keep_config: bool, keep_data: bool
) -> list[tuple[str, list[Path]]]:
    """Return ordered ``[(group_label, paths_to_stage), ...]``.

    Owned subdirs (``config.d``, ``memories``, ``uploads``) move as
    whole directories — one ``rename`` instead of N — which is also
    why ``_OWNED_SUBDIRS`` no longer needs a post-deletion empty-prune
    pass for the no-keep-flag path.
    """
    state_dir = _DEFAULT_STATE_DIR
    plan: list[tuple[str, list[Path]]] = []

    plan.append(("session/pid", list(inv.other_files.paths)))

    if not keep_config:
        fragment_dir = state_dir / "config.d"
        if fragment_dir.is_dir():
            plan.append(("fragments", [fragment_dir]))
        plan.append(("backups", list(inv.backup_files.paths)))
        plan.append(("config.json", list(inv.config_files.paths)))

    if not keep_data:
        memory_dir = state_dir / "memories"
        if memory_dir.is_dir():
            plan.append(("memories", [memory_dir]))

    upload_dir = state_dir / "uploads"
    if upload_dir.is_dir():
        plan.append(("uploads", [upload_dir]))

    if not keep_data:
        plan.append(("database", list(inv.db_files.paths)))

    return plan


def _stage_inventory(
    inv: _Inventory, *, keep_config: bool, keep_data: bool
) -> tuple[list[Path], list[str]]:
    """Atomically move every to-be-deleted path into a staging sibling.

    Returns ``(staging_roots, completed_labels)`` so the caller can
    finalize via ``shutil.rmtree`` per root and report what was wiped.
    On any failure, rolls back and raises ``_UninstallStagingError``;
    on cross-FS layout, raises ``_UninstallCrossFsError`` instead.
    """
    state_dir = _DEFAULT_STATE_DIR
    custom_db_dir = inv.db_path.parent if inv.db_path.parent != state_dir else None
    rt = runtime_dir()

    staging_name = f"{_STAGING_PREFIX}{os.getpid()}"
    plan = _build_stage_plan(inv, keep_config=keep_config, keep_data=keep_data)

    def _anchor_for(path: Path) -> Path:
        for anchor in (state_dir, custom_db_dir, rt):
            if anchor is None:
                continue
            try:
                path.relative_to(anchor)
                return anchor
            except ValueError:
                continue
        raise ValueError(f"unanchored path: {path}")

    # Cross-FS pre-check. ``os.replace`` raises ``EXDEV`` for cross-FS,
    # so a same-``st_dev`` probe upfront keeps us from staging half the
    # plan before the doomed move surfaces. The late-detection branch
    # below still catches anything the pre-check misses (stat race,
    # transient FS errors, etc.).
    for _, paths in plan:
        for src in paths:
            if not src.exists():
                continue
            try:
                anchor = _anchor_for(src)
            except ValueError:
                continue
            try:
                if src.stat().st_dev != anchor.stat().st_dev:
                    raise _UninstallCrossFsError(src=src, anchor=anchor)
            except OSError:
                # Stat failure: defer to ``os.replace``'s own diagnostics.
                continue

    roots: dict[Path, Path] = {}
    moves: list[_StagedMove] = []

    def _staging_root_for(anchor: Path) -> Path:
        if anchor not in roots:
            root = anchor / staging_name
            # exist_ok=False — the pid-suffixed name is per-process, so
            # collision means somebody else parked a directory with our
            # exact name and we should refuse rather than scribble in it.
            root.mkdir(parents=True, exist_ok=False)
            roots[anchor] = root
        return roots[anchor]

    def _rollback() -> list[tuple[Path, BaseException]]:
        errors: list[tuple[Path, BaseException]] = []
        for move in reversed(moves):
            try:
                os.replace(move.staged, move.original)
            except OSError as exc:
                errors.append((move.staged, exc))
        # Best-effort cleanup of empty staging trees. If rollback
        # partially failed, the not-rolled-back content survives — and
        # the user is told the staging root path so they can recover.
        for root in list(roots.values()):
            if not root.exists():
                continue
            for sub in sorted(root.rglob("*"), reverse=True):
                if sub.is_dir():
                    try:
                        sub.rmdir()
                    except OSError:
                        pass
            try:
                root.rmdir()
            except OSError:
                pass
        return errors

    completed_labels: list[str] = []
    for label, paths in plan:
        any_staged = False
        for src in paths:
            if not src.exists():
                continue
            try:
                anchor = _anchor_for(src)
                staging_root = _staging_root_for(anchor)
                rel = src.relative_to(anchor)
                dst = staging_root / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                os.replace(src, dst)
            except OSError as exc:
                rollback_errors = _rollback()
                if isinstance(exc, OSError) and exc.errno == errno.EXDEV:
                    # Late-detected cross-FS — surface the dedicated
                    # error so the user gets the layout-specific message
                    # instead of a generic stage-failed report.
                    raise _UninstallCrossFsError(src=src, anchor=anchor) from exc
                raise _UninstallStagingError(
                    failing_path=src,
                    original=exc,
                    rollback_errors=rollback_errors,
                    staging_roots=list(roots.values()),
                ) from exc
            except ValueError as exc:
                rollback_errors = _rollback()
                raise _UninstallStagingError(
                    failing_path=src,
                    original=exc,
                    rollback_errors=rollback_errors,
                    staging_roots=list(roots.values()),
                ) from exc
            moves.append(_StagedMove(original=src, staged=dst))
            any_staged = True
        if any_staged:
            completed_labels.append(label)

    return list(roots.values()), completed_labels


def _delete_inventory(inv: _Inventory, *, keep_config: bool, keep_data: bool) -> str:
    """Stage every to-be-deleted path, then ``rmtree`` the staging dirs.

    The stage step is the atomic point — either every path is moved
    out of its original location, or none are. The ``rmtree`` step
    only operates on the staging dirs, so a failure there is benign
    leftover trash (warned, not failed) rather than user-visible
    half-state.
    """
    staging_roots, completed = _stage_inventory(inv, keep_config=keep_config, keep_data=keep_data)

    for root in staging_roots:
        if not root.exists():
            continue
        try:
            shutil.rmtree(root)
        except OSError as exc:
            click.secho(
                f"  Warning: could not remove staging dir {_format_path(root)}: {exc}. "
                "Original data is already moved out of the way; you may delete the "
                "staging dir manually.",
                fg="yellow",
            )

    # Owned-subdir prune — only matters when ``--keep-config`` /
    # ``--keep-data`` left a now-empty subdir behind (we whole-dir-stage
    # everything else, so those subdirs are gone via the rmtree above).
    for subdir in _OWNED_SUBDIRS:
        candidate = _DEFAULT_STATE_DIR / subdir
        if candidate.exists() and candidate.is_dir() and not any(candidate.iterdir()):
            try:
                candidate.rmdir()
            except OSError:
                pass

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

    is_windows = sys.platform == "win32"

    # Windows can't unlink files held by an open handle (WinError 32), so
    # `--force` cannot override a live writer — pretending it does would
    # leave the state dir half-wiped after `_delete_inventory` crashes
    # mid-run. Refuse cleanly with platform-specific guidance instead.
    # Tracked in #730.
    if force and is_windows and (server.alive or db_lock.locked):
        click.echo("")
        click.secho(
            "--force cannot wipe an open SQLite database on Windows; stop the writer first.",
            fg="red",
        )
        if server.alive and server.pid is not None:
            click.secho(
                f"  Stop the server (pid {server.pid}) and retry. Windows refuses to "
                "unlink files held by an open handle (WinError 32).",
                fg="red",
            )
        else:
            click.secho(
                "  Find the writer (Task Manager, `Get-Process`, or Sysinternals "
                "`handle.exe`), stop it, and retry. Windows refuses to unlink files "
                "held by an open handle (WinError 32).",
                fg="red",
            )
        sys.exit(2)

    if (server.alive or db_lock.locked) and not force:
        click.echo("")
        if server.alive:
            if server.pid is None:
                # Empty pid file but flock IS held — a live writer exists,
                # but the recorded pid was lost (typically a partial write
                # during startup, or a pre-fix concurrent server start that
                # truncated the file before bailing on the lock probe; see
                # server/__init__.py for the open(..., "a+") rationale).
                # ``_probe_pid_file`` only returns alive=True with the
                # ``pid_file`` field populated, so it is safe to dereference
                # here without a fallback.
                if is_windows:
                    click.secho(
                        "Server still running (pid unknown — an active writer holds "
                        "the lock, but the recorded pid is missing). Refusing to "
                        "delete state. Find the holder via Sysinternals `handle.exe` "
                        "or Resource Monitor.",
                        fg="red",
                    )
                else:
                    click.secho(
                        "Server still running (pid unknown — flock is held by an active "
                        "writer, but the recorded pid is missing). Refusing to delete "
                        f"state. Find the holder with `lsof {server.pid_file}`.",
                        fg="red",
                    )
            else:
                click.secho(
                    f"Server still running (pid {server.pid}). Refusing to delete state — "
                    "an active server holds the SQLite WAL and deleting it risks corruption.",
                    fg="red",
                )
            if is_windows:
                # On Windows --force cannot override (see #730 / branch above),
                # so don't suggest it.
                click.secho("  Stop the server first.", fg="red")
            else:
                click.secho("  Stop the server first, or pass --force to override.", fg="red")
        else:
            # db_lock.locked only — writer without .server.pid (mm web,
            # mm watchdog, ad-hoc script, ...). Point the user at the
            # platform-appropriate process inspection tool so they can
            # find it without another round-trip.
            click.secho(
                f"Another process holds a write lock on {db_path}. Refusing to delete "
                "state — an active writer can corrupt the WAL.",
                fg="red",
            )
            if is_windows:
                click.secho(
                    "  Find the writer (Task Manager, `Get-Process`, or Sysinternals "
                    "`handle.exe`) and stop it, then retry.",
                    fg="red",
                )
            else:
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
    except _UninstallCrossFsError as exc:
        click.echo("")
        click.secho(
            f"Refusing to proceed: {_format_path(exc.src)} is on a different "
            f"filesystem than {_format_path(exc.anchor)}.",
            fg="red",
        )
        click.secho(
            "  Transactional uninstall stages files via os.replace, which is "
            "atomic only within a single filesystem. Move the contents to one "
            "filesystem and retry, or remove the affected paths manually.",
            fg="red",
        )
        sys.exit(2)
    except _UninstallStagingError as exc:
        click.secho(
            f"\nDeletion failed at {_format_path(exc.failing_path)}: {exc.original}",
            fg="red",
        )
        if not exc.rollback_errors:
            click.secho(
                "  Rolled back — no files were deleted.",
                fg="yellow",
            )
        else:
            click.secho(
                "  Rollback also failed; state is split between original "
                "locations and staging dirs. Move the contents listed below "
                "back to their original locations to recover:",
                fg="red",
            )
            for staged_path, rb_exc in exc.rollback_errors:
                click.secho(
                    f"    - {_format_path(staged_path)}: {rb_exc}",
                    fg="red",
                )
            for root in exc.staging_roots:
                click.secho(f"  Staging dir: {_format_path(root)}", fg="red")
        sys.exit(2)

    click.secho(f"\nRemoved: {summary}.", fg="green")
    click.echo("Run the binary uninstall command above to complete removal.")
