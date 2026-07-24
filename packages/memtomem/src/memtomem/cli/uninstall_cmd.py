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
* Exception to the ``--force`` override (#1935): the instance registry
  (``instances/`` under the runtime dir). A held sentinel is *positive*
  evidence of a live server — a secondary owns no ``server.pid``, and an
  idle server holds no SQLite write lock, so the two probes above can
  both miss it — and an inconclusive registry probe is fail-closed
  (a timeout never means "empty"). LIVE/UNKNOWN registry evidence refuses
  unconditionally; ``--force``'s contract covers the stale-*pid*
  heuristic, not positive liveness. The registry's mutation sidecar
  (``instances.registry.lock``) is retained infrastructure: never
  inventoried or deleted (unlinking a lock file re-opens the waiter
  race); it lives in the volatile runtime dir, which self-cleans.
* If config.json is corrupted we fall back to defaults rather than
  aborting — uninstall is itself a recovery path, so it cannot depend on
  a valid config.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import stat
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import click

from memtomem._instance_registry import instances_dir as _instances_dir
from memtomem._instance_registry import (
    probe_all_for_uninstall as _probe_registry_liveness,
)
from memtomem._runtime_paths import runtime_dir, server_pid_path
from memtomem.cli._db_lock import DbLockState as _DbLockState  # noqa: F401  (test seam)
from memtomem.cli._db_lock import check_db_lock as _check_db_lock
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


def _is_dir_link(path: Path) -> bool:
    """True when *path* redirects: a symlink or an NTFS junction.

    Both checks are needed. Windows tags junctions
    ``IO_REPARSE_TAG_MOUNT_POINT``, so ``is_symlink()`` — like ``lstat``
    + ``S_ISDIR`` — reports ``False`` for one while it stays
    directory-shaped. Unreadable counts as a redirect: every caller uses
    this to decide whether to *stop*, so the safe answer is "yes".
    """
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return True


def _real_registry_dir() -> Path | None:
    """The sentinel directory iff it is an actual directory.

    ``lstat`` semantics — a symlinked ``instances/`` is treated as
    absent here so the inventory and the prune below can never traverse
    or stage through it into unrelated files (the fail-closed refusal
    for that case comes from ``probe_all_for_uninstall`` returning
    ``UNKNOWN``; this guard keeps the *listing* side inert too).

    Junctions must be refused by name: on Windows they redirect like a
    symlink while ``lstat`` still reports ``S_IFDIR``, so without the
    explicit check ``_collect_inventory`` would list the *target's*
    files, stage them with ``os.replace``, and delete them with the
    staging tree — data loss outside the registry, not a read-only leak.

    The *anchor* is checked as well, not just the final component: a
    junctioned runtime dir leaves an ordinary ``instances/`` inside the
    target, which passes every check made on the leaf alone.
    ``ensure_runtime_dir`` refuses that anchor too, but this path must
    not depend on something else having run first.
    """
    d = _instances_dir()
    if _is_dir_link(d.parent):
        return None
    try:
        st = os.stat(d, follow_symlinks=False)
    except OSError:
        return None
    if not stat.S_ISDIR(st.st_mode) or _is_dir_link(d):
        return None
    return d


def _registry_has_sentinels() -> bool:
    """True when the instance-registry sentinel directory has any entries.

    Sidecar-only leftovers return ``False`` (the sidecar lives outside
    the directory and is retained infrastructure, #1935).
    """
    d = _real_registry_dir()
    if d is None:
        return False
    try:
        return any(d.iterdir())
    except OSError:
        return False


def _prune_if_empty(path: Path) -> bool:
    """``rmdir`` *path* when it is an existing, empty directory.

    Returns whether it was removed; never raises. The directory check,
    the emptiness listing, and the ``rmdir`` all sit inside one ``try``
    on purpose: these prunes run *after* the staging move, so the data is
    already out of the way and a directory that disappears underneath us
    (a concurrent cleanup, the volatile runtime dir, a racing second
    uninstall) must degrade to "not pruned" rather than raise and skip
    every prune that follows. Guarding only the ``rmdir`` — the shape
    this replaced — left the listing able to abort the sequence.

    Directory links are refused outright: ``is_dir()`` follows them, and
    while POSIX ``rmdir`` then fails with ``ENOTDIR``, Windows
    ``RemoveDirectoryW`` removes the reparse point and leaves the target,
    so an empty ``config.d`` or ``memories`` *link* would be deleted out
    from under ``--keep-config`` / ``--keep-data``. The check must come
    before the listing: below it, a link to a non-empty directory would
    refuse for the wrong reason and a link to an empty one would be
    pruned outright.
    """
    try:
        if _is_dir_link(path):
            return False
        if not path.is_dir() or any(path.iterdir()):
            return False
        path.rmdir()
        return True
    except OSError:
        return False


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
        # migrate=False: uninstall only reads the db path — it must not run the
        # auto_discover migration, which would rewrite config.json (and create
        # the .config.json.lock sidecar) right before we delete it. Read-only
        # diagnostic surface, per feedback_doctor_no_migration_loader.
        load_config_overrides(cfg, migrate=False)
        db_path = Path(cfg.storage.sqlite_path).expanduser()
        return db_path, None
    except Exception as exc:  # broad: uninstall must never abort on config
        return _DEFAULT_STATE_DIR / _DEFAULT_DB_NAME, f"{type(exc).__name__}: {exc}"


# ---- server liveness + DB-lock probes -------------------------------------
#
# Liveness helpers live in ``memtomem.cli._liveness`` so ``mm upgrade`` can
# share them, and the ``BEGIN IMMEDIATE`` write-lock probe lives in
# ``memtomem.cli._db_lock`` so ``mm reset`` can share it (#1574 item 7).
# They're re-imported above as ``_check_server_liveness`` / ``_probe_pid_file``
# / ``_check_db_lock`` / ``_DbLockState`` to keep the existing test seams; the
# ``ServerState`` dataclass is consumed via duck-typing so no alias is needed.


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

    # Per-install provenance HMAC key sidecar (ADR-0006 Axis F.3). It is a
    # persistent secret derived from the DB stem (``<db-stem>.provenance_key``),
    # not a ``<db-name><suffix>`` sibling, so the loop above misses it. Group it
    # with the database so ``keep_data=False`` wipes it — otherwise a reinstall
    # at the same path reuses the old key and keeps trusting prior self-export
    # markers (handles custom storage paths via ``key_path_for_db``).
    from memtomem import provenance

    key_path = provenance.key_path_for_db(db_path)
    if key_path.exists():
        db_paths.append(key_path)

    # ``mm reset --backup`` snapshots (``<db-name>.pre-reset-<ts>.bak``,
    # #1574 item 7). Like the provenance key they are DB-stem siblings the
    # suffix loop above misses; group them with the database so they're
    # data-gated (``--keep-data`` preserves, default wipes) and don't
    # silently survive uninstall keeping the state dir non-empty.
    db_paths.extend(sorted(db_path.parent.glob(db_path.name + ".pre-reset-*.bak")))

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
    # ``.config.json.lock`` is the sidecar lock for config.json read-modify-write
    # (issue #1567). ``_file_lock`` creates it on first write and never unlinks it
    # (deleting it would reintroduce the os.replace inode race for a waiting
    # writer), so it persists in state_dir and must be cleaned here or it would
    # keep the directory non-empty after uninstall.
    for name in (".current_session", ".server.pid", ".config.json.lock"):
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
    # #1935: instance-registry sentinels are transient runtime files like
    # the pid files above; the refusal gate guarantees none of them is
    # live by the time staging runs. The mutation sidecar
    # (``instances.registry.lock``, *outside* this directory) is
    # deliberately absent — retained infrastructure, see module docstring.
    reg_dir = _real_registry_dir()
    if reg_dir is not None:
        try:
            other.extend(sorted(p for p in reg_dir.iterdir() if p.is_file()))
        except OSError:
            pass

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
    kimi_share_dir = os.environ.get("KIMI_SHARE_DIR")
    if kimi_share_dir:
        kimi_share_mcp = Path(kimi_share_dir).expanduser() / "mcp.json"
        if kimi_share_mcp not in candidates:
            candidates.append(kimi_share_mcp)

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
        # That survivor is why this prunes through ``_prune_if_empty``
        # rather than a bare ``rmdir``: the tree being walked is then the
        # user's own data, and a directory *link* inside it answers
        # ``is_dir()`` while Windows ``RemoveDirectoryW`` deletes the
        # reparse point — destroying part of what the recovery message
        # just promised was still recoverable.
        for root in list(roots.values()):
            if not root.exists():
                continue
            for sub in sorted(root.rglob("*"), reverse=True):
                _prune_if_empty(sub)
            _prune_if_empty(root)
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
        _prune_if_empty(_DEFAULT_STATE_DIR / subdir)

    if inv.db_path.parent == _DEFAULT_STATE_DIR and _prune_if_empty(_DEFAULT_STATE_DIR):
        completed.append("state dir")

    # #1935: prune the sentinel directory once its contents are staged
    # away. The runtime-dir prune below then usually still finds the
    # retained ``instances.registry.lock`` sidecar and no-ops — expected;
    # the runtime dir is volatile and self-cleans. ``_real_registry_dir``
    # already refuses a symlinked ``instances/``; ``_prune_if_empty`` is
    # what keeps every prune here off directory links in general.
    reg_dir = _real_registry_dir()
    if reg_dir is not None:
        _prune_if_empty(reg_dir)

    _prune_if_empty(runtime_dir())

    return ", ".join(completed) if completed else "nothing"


# ---- click command ------------------------------------------------------


@click.command("uninstall")
@click.option("--keep-config", is_flag=True, help="Preserve config.json + config.d/* + backups.")
@click.option("--keep-data", is_flag=True, help="Preserve the SQLite DB and ~/.memtomem/memories/.")
@click.option(
    "--force",
    is_flag=True,
    help="Bypass the stale-pid/db-lock safety heuristics (use only if you know "
    "the pid is stale). Does NOT override instance-registry evidence of a "
    "live server — that check is positive liveness, not a heuristic.",
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
    registry_state = _probe_registry_liveness()

    # Empty-state fast path. Leftover registry *sentinels* count as state
    # (#1935 — they must be inventoried and offered for deletion); the
    # retained registry sidecar alone does not, so a post-uninstall rerun
    # with only ``instances.registry.lock`` remaining still lands here.
    if not state_dir.exists() and not db_path.exists() and not _registry_has_sentinels():
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

    # #1935: registry evidence is not operator-overridable — this block
    # runs before any ``--force`` handling on every platform. A held
    # sentinel is positive evidence of a live server even when it owns
    # neither ``server.pid`` (a secondary) nor a SQLite write lock (an
    # idle server); UNKNOWN (probe timeout / unreadable entry) is
    # fail-closed — a timeout never means "empty". No ``--force`` hint is
    # printed here: advertising an override that does not apply would be
    # false remediation.
    if registry_state != "NONE":
        click.echo("")
        if registry_state == "LIVE":
            click.secho(
                "A live memtomem-server instance is registered for this user. "
                "Refusing to delete state — an active server holds the store "
                "open and deleting it risks corruption.",
                fg="red",
            )
            click.secho(
                "  Stop every memtomem-server (close editor sessions using "
                "memtomem) and retry. --force does not override this check.",
                fg="red",
            )
        else:
            click.secho(
                "Could not determine whether a memtomem-server instance is "
                "still running (instance-registry probe did not complete). "
                "Refusing to delete state.",
                fg="red",
            )
            click.secho(
                "  Retry in a moment. --force does not override this check.",
                fg="red",
            )
        sys.exit(2)

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

    # #1935: liveness was sampled before the inventory print and — in the
    # interactive flow — a confirmation prompt the user can sit on for
    # minutes. Re-run every probe at the destructive boundary so a server
    # that started (or registered) meanwhile is refused instead of having
    # its live state staged. ``--force`` keeps exactly the authority it
    # had above: the POSIX stale-pid/db-lock heuristics, never registry
    # evidence and never Windows open-handle reality.
    server = _check_server_liveness()
    db_lock = _check_db_lock(db_path)
    registry_state = _probe_registry_liveness()
    heuristics_block = (server.alive or db_lock.locked) and (not force or is_windows)
    if registry_state != "NONE" or heuristics_block:
        click.echo("")
        click.secho(
            "A memtomem process became active while uninstall was waiting for "
            "confirmation. Refusing to delete state — stop it and re-run "
            "mm uninstall.",
            fg="red",
        )
        sys.exit(2)

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
