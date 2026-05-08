"""CLI: ``mm sync-doctor`` — read-only validator for private-repo multi-device sync.

Implements Phase 2 of the multi-device sync RFC (memtomem-docs#34). Catches the
common footguns the RFC documents:

- ``*.db`` files staged in the worktree (would propagate to other devices and
  corrupt SQLite under WAL).
- ``config.json`` staged (machine-local; only ``config.d/`` is portable).
- ``~/.memtomem/config.d/`` fragments missing on this machine (the synced
  ``config.d/`` was never bridged into the canonical location).
- ``memory_dirs`` paths under cloud-sync mounts where the fs watcher is
  unreliable; recommend ``startup_backfill=true``.
- ``~/.claude/projects/`` cwd slug doesn't match the current working tree (the
  per-project auto-memory layout is broken for this machine).

No ``push`` / ``pull`` / auto-fix. Read-only by design (RFC §Non-goals).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Literal, NamedTuple

import click


# ---- Cloud-mount path-prefix helper ----------------------------------------
#
# Pure path-prefix match against the user's expanded ``$HOME``. No fs probing,
# no platform branching — the prefix list is fixed (RFC §Phase 2). Intended to
# be applied to each entry in ``memory_dirs[]`` after ``Path.expanduser()``.

CLOUD_MOUNT_PREFIXES: tuple[str, ...] = (
    "~/Library/CloudStorage/",
    "~/Library/Mobile Documents/com~apple~CloudDocs/",
    "~/Dropbox/",
    "~/OneDrive",  # matches OneDrive, OneDrive-Personal, "OneDrive - Foo", etc.
)


def cloud_mount_prefix(path: Path, *, home: Path | None = None) -> str | None:
    """Return matched cloud-mount prefix (display form), or ``None`` if none.

    ``path`` should already be expanded (``Path.expanduser()``). ``home`` is
    overridable for tests; defaults to ``Path.home()``.
    """
    home_s = str(home or Path.home())
    s = str(path)
    for prefix in CLOUD_MOUNT_PREFIXES:
        expanded = prefix.replace("~", home_s, 1)
        if prefix == "~/OneDrive":
            if s.startswith(expanded):
                rest = s[len(expanded) :]
                if not rest or rest[0] in "-_ /":
                    return "~/OneDrive*/"
        elif s.startswith(expanded):
            return prefix
    return None


# ---- Check result model ----------------------------------------------------

Status = Literal["pass", "fail", "warn", "info"]

_GLYPH = {"pass": "✓", "fail": "✗", "warn": "!", "info": "·"}
_COLOR = {"pass": "green", "fail": "red", "warn": "yellow", "info": None}


class CheckResult(NamedTuple):
    status: Status
    message: str
    detail: str | None = None  # second-line elaboration, optional


def _emit(result: CheckResult) -> None:
    click.secho(f"{_GLYPH[result.status]} {result.message}", fg=_COLOR[result.status])
    if result.detail:
        click.echo(f"  {result.detail}")


# ---- Individual checks -----------------------------------------------------


def _git_ls_files(cwd: Path) -> list[str] | None:
    """Return ``git ls-files`` output split per line, or ``None`` if not a repo."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        proc = subprocess.run(
            [git, "ls-files"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    return [line for line in proc.stdout.splitlines() if line]


def check_no_db_staged(cwd: Path) -> CheckResult:
    files = _git_ls_files(cwd)
    if files is None:
        return CheckResult("warn", "not a git repo at cwd — skipping git checks")
    db_suffixes = (".db", ".db-wal", ".db-shm")
    matches = [f for f in files if f.endswith(db_suffixes)]
    if matches:
        sample = ", ".join(matches[:3]) + (" …" if len(matches) > 3 else "")
        return CheckResult("fail", f"{len(matches)} *.db file(s) staged", detail=sample)
    return CheckResult("pass", "no *.db files staged")


def check_config_json_absent(cwd: Path) -> CheckResult:
    files = _git_ls_files(cwd)
    if files is None:
        return CheckResult("warn", "config.json staging — not a git repo at cwd")
    if any(f == "config.json" or f.endswith("/config.json") for f in files):
        return CheckResult("fail", "config.json staged (machine-local — should not sync)")
    return CheckResult("pass", "config.json absent from worktree")


def check_config_d_present(config_d: Path) -> CheckResult:
    if not config_d.is_dir():
        return CheckResult(
            "warn",
            "~/.memtomem/config.d/ does not exist on this machine",
            detail="bridge synced fragments via symlink/copy per RFC §Design",
        )
    fragments = sorted(config_d.glob("*.json"))
    if not fragments:
        return CheckResult("warn", "config.d/ has no fragments — sync bridge may be missing")
    return CheckResult("pass", f"config.d/ fragments present ({len(fragments)} files)")


def check_memory_dirs_under_home(memory_dirs: list[Path]) -> CheckResult:
    home = Path.home().resolve()
    outside: list[str] = []
    for d in memory_dirs:
        try:
            resolved = Path(d).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        try:
            resolved.relative_to(home)
        except ValueError:
            outside.append(str(d))
    if outside:
        return CheckResult(
            "warn",
            f"{len(outside)} memory_dir(s) outside $HOME — not portable across users",
            detail=outside[0] + (" …" if len(outside) > 1 else ""),
        )
    return CheckResult("pass", "memory_dir paths resolve under $HOME")


def check_cloud_mount(memory_dirs: list[Path]) -> CheckResult:
    hits: list[tuple[str, str]] = []  # (display_path, prefix_label)
    for d in memory_dirs:
        expanded = Path(d).expanduser()
        prefix = cloud_mount_prefix(expanded)
        if prefix:
            hits.append((str(d), prefix))
    if not hits:
        return CheckResult("pass", "no memory_dir under known cloud-sync mounts")
    label = hits[0][1]
    return CheckResult(
        "warn",
        f"cloud-sync mount detected at {label} — fs watcher may miss events",
        detail="recommend startup_backfill=true",
    )


def check_claude_slug(cwd: Path, *, home: Path | None = None) -> CheckResult:
    """Verify ``~/.claude/projects/<slug>`` for the current cwd exists.

    Skipped silently when ``~/.claude/projects/`` is absent (user doesn't run
    Claude Code). When present, the cwd must round-trip to a real entry under
    that directory; otherwise the synced auto-memory layout is broken for this
    machine.
    """
    projects = (home or Path.home()) / ".claude" / "projects"
    if not projects.is_dir():
        return CheckResult("info", "~/.claude/projects/ absent — auto-memory check skipped")
    encoded = str(cwd.resolve()).replace("/", "-")
    if (projects / encoded).is_dir():
        return CheckResult("pass", "~/.claude/projects/ slug matches synced layout")
    return CheckResult(
        "fail",
        "~/.claude/projects/ slug differs from synced layout — see doc",
        detail=f"expected entry: {projects}/{encoded}",
    )


# ---- Click entry point -----------------------------------------------------


@click.command("sync-doctor")
def sync_doctor() -> None:
    """Validate the current working tree as a memtomem private-sync repo (read-only).

    Run from inside the private repo (the one tracking your synced ``memories/``
    + ``config.d/``). Reports six checks; exits non-zero on any failure. Warns
    don't fail the exit code (a future ``--strict`` may flip that).
    """
    from memtomem.config import Mem2MemConfig, _config_d_path, load_config_d, load_config_overrides

    cfg = Mem2MemConfig()
    load_config_d(cfg, quiet=True)
    load_config_overrides(cfg)

    cwd = Path.cwd()
    memory_dirs = list(cfg.indexing.memory_dirs)
    config_d = _config_d_path()

    results = [
        check_no_db_staged(cwd),
        check_config_json_absent(cwd),
        check_config_d_present(config_d),
        check_claude_slug(cwd),
        check_memory_dirs_under_home(memory_dirs),
        check_cloud_mount(memory_dirs),
    ]

    for r in results:
        _emit(r)

    if any(r.status == "fail" for r in results):
        raise SystemExit(1)
