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
# Pure path comparison against a fixed list of cloud-sync mounts under
# ``$HOME`` (RFC §Phase 2). No fs probing. ``Path.relative_to`` keeps the
# match separator-safe on Windows (``\\``) as well as POSIX (``/``).
#
# Matched prefixes (display form):
#   ~/Library/CloudStorage/                          (macOS sync clients)
#   ~/Library/Mobile Documents/com~apple~CloudDocs/  (iCloud Drive on macOS)
#   ~/Dropbox/                                       (Dropbox legacy fallback)
#   ~/OneDrive*/                                     (OneDrive, any suffix variant)


def cloud_mount_prefix(path: Path, *, home: Path | None = None) -> str | None:
    """Return matched cloud-mount prefix (display form), or ``None`` if none.

    ``path`` should already be expanded (``Path.expanduser()``). ``home`` is
    overridable for tests; defaults to ``Path.home()``. Comparison goes through
    ``Path.relative_to`` so the result is separator-safe on Windows (``\\`` vs
    ``/``).
    """
    home_p = home or Path.home()
    fixed = (
        ("~/Library/CloudStorage/", home_p / "Library" / "CloudStorage"),
        (
            "~/Library/Mobile Documents/com~apple~CloudDocs/",
            home_p / "Library" / "Mobile Documents" / "com~apple~CloudDocs",
        ),
        ("~/Dropbox/", home_p / "Dropbox"),
    )
    for label, root in fixed:
        try:
            path.relative_to(root)
        except ValueError:
            continue
        return label
    # OneDrive variants: matches ``OneDrive``, ``OneDrive-Personal``,
    # ``OneDrive - Acme``; rejects substring false positives like ``OneDriveX``.
    try:
        rel = path.relative_to(home_p)
    except ValueError:
        return None
    if not rel.parts:
        return None
    head = rel.parts[0]
    if head == "OneDrive" or (head.startswith("OneDrive") and len(head) > 8 and head[8] in "-_ "):
        return "~/OneDrive*/"
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


def _repo_top_level(start: Path) -> Path | None:
    """Return the git top-level above ``start``, or ``None`` if not in a repo."""
    git = shutil.which("git")
    if git is None:
        return None
    try:
        proc = subprocess.run(
            [git, "rev-parse", "--show-toplevel"],
            cwd=str(start),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if proc.returncode != 0:
        return None
    top = proc.stdout.strip()
    return Path(top) if top else None


def _git_ls_files(start: Path) -> list[str] | None:
    """List every tracked path in the enclosing repo, or ``None`` if not a repo.

    Resolves the repository top-level first so the listing covers the whole
    private repo regardless of the subdirectory the doctor was invoked from.
    Plain ``git ls-files`` from a nested dir only yields paths *under* that
    dir, which would let the doctor miss a tracked root-level
    ``config.json`` or ``*.db``.
    """
    git = shutil.which("git")
    if git is None:
        return None
    repo_root = _repo_top_level(start)
    if repo_root is None:
        return None
    try:
        proc = subprocess.run(
            [git, "ls-files"],
            cwd=str(repo_root),
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
    """Verify ``~/.claude/projects/<slug>`` matches the current cwd.

    Skipped silently when ``~/.claude/projects/`` is absent (user doesn't run
    Claude Code). Two-tier match:

    1. **Fast path** — encode ``cwd`` (POSIX ``/`` → ``-``, Windows ``\\`` /
       drive ``:`` → ``-``) and look up the resulting directory directly. The
       previous one-shot ``str(cwd).replace("/", "-")`` only handled POSIX
       and produced an invalid slug on Windows (drive prefix + backslashes
       remained), causing false failures for valid Windows layouts.
    2. **Slow path** — iterate real entries and FS-guided-decode-then-
       ``samefile`` each against ``cwd`` via
       :func:`memtomem.context.projects._decode_claude_project_dirname`, which
       reconstructs ``/``, ``.`` and literal-``-`` segments (so kebab-case and
       dotted directories round-trip, unlike the old blind ``-`` → ``/``).
    """
    projects = (home or Path.home()) / ".claude" / "projects"
    if not projects.is_dir():
        return CheckResult("info", "~/.claude/projects/ absent — auto-memory check skipped")
    try:
        cwd_resolved = cwd.resolve()
    except OSError:
        return CheckResult("info", "cwd resolution failed — Claude slug check skipped")
    from memtomem.context.projects import (
        _DecodeBudgetError,
        _decode_claude_project_dirname,
        _encode_claude_project_path,
    )

    cwd_str = str(cwd_resolved)
    encoded_candidates = {
        # Authoritative POSIX encoding — Claude Code collapses BOTH "/" and "."
        # to "-" (so a dotted segment like ``.config-dir`` round-trips), which
        # the bare ``replace("/", "-")`` below misses.
        _encode_claude_project_path(cwd_resolved),
        cwd_str.replace("/", "-"),  # POSIX absolute (dot-less)
        cwd_str.replace("\\", "-").replace(":", ""),  # Windows: drop drive colon
        cwd_str.replace("\\", "-").replace(":", "-"),  # Windows: encode drive colon as "-"
    }
    for cand in encoded_candidates:
        # Skip candidates that still contain a path separator — the encoding
        # didn't fully strip them (wrong platform), and ``projects / cand``
        # would silently resolve to ``cand`` itself when it's absolute.
        if "/" in cand or "\\" in cand:
            continue
        if (projects / cand).is_dir():
            return CheckResult("pass", "~/.claude/projects/ slug matches synced layout")
    # Slow path: FS-guided decode of existing entries, then samefile-compare.
    # Shares ``_decode_claude_project_dirname`` (the 3-way reconstruction that
    # handles "/", "." and literal "-") so a dotted/dashed cwd the UI can
    # discover is not falsely failed here.
    for child in projects.iterdir():
        if not child.is_dir():
            continue
        try:
            decoded_candidates = _decode_claude_project_dirname(child.name)
        except _DecodeBudgetError:
            continue
        for decoded in decoded_candidates:
            try:
                if decoded.samefile(cwd):
                    return CheckResult("pass", "~/.claude/projects/ slug matches synced layout")
            except OSError:
                continue
    return CheckResult(
        "fail",
        "~/.claude/projects/ slug differs from synced layout — see doc",
        detail=f"no entry under {projects} matches cwd",
    )


# ---- Click entry point -----------------------------------------------------


def _apply_memory_dirs_override_no_write(cfg: object) -> None:
    """Read ``~/.memtomem/config.json``'s ``indexing.memory_dirs`` into ``cfg``.

    Read-only mirror of the relevant slice of ``load_config_overrides`` — the
    full loader also calls ``_migrate_auto_discover_once`` which rewrites
    ``config.json`` on legacy ``auto_discover=True`` installs. The doctor
    must not mutate config (RFC §Non-goals: read-only).

    Env precedence (``MEMTOMEM_INDEXING__MEMORY_DIRS``) is preserved by
    deferring to whatever ``Mem2MemConfig()`` already loaded.
    """
    import json
    import os

    from memtomem.config import _override_path

    path = _override_path()
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    indexing = data.get("indexing") if isinstance(data, dict) else None
    if not isinstance(indexing, dict):
        return
    md = indexing.get("memory_dirs")
    if not isinstance(md, list):
        return
    if "MEMTOMEM_INDEXING__MEMORY_DIRS" in os.environ:
        return  # env wins, mirroring load_config_overrides
    try:
        cfg.indexing.memory_dirs = [Path(p) for p in md if isinstance(p, str)]  # type: ignore[attr-defined]
    except (TypeError, ValueError):
        return


@click.command("sync-doctor")
def sync_doctor() -> None:
    """Validate the current working tree as a memtomem private-sync repo (read-only).

    Run from inside the private repo (the one tracking your synced ``memories/``
    + ``config.d/``). Reports six checks; exits non-zero on any failure. Warns
    don't fail the exit code (a future ``--strict`` may flip that).
    """
    from memtomem.config import Mem2MemConfig, _config_d_path, load_config_d

    cfg = Mem2MemConfig()
    load_config_d(cfg, quiet=True)
    _apply_memory_dirs_override_no_write(cfg)

    cwd = Path.cwd()
    # Slug check anchors at the repo top-level so subdir invocations still
    # match the project Claude Code recorded for the repo (which is the dir
    # ``claude`` was launched from — typically the repo root, not a subdir).
    repo_root = _repo_top_level(cwd) or cwd
    memory_dirs = list(cfg.indexing.memory_dirs)
    config_d = _config_d_path()

    results = [
        check_no_db_staged(cwd),
        check_config_json_absent(cwd),
        check_config_d_present(config_d),
        check_claude_slug(repo_root),
        check_memory_dirs_under_home(memory_dirs),
        check_cloud_mount(memory_dirs),
    ]

    for r in results:
        _emit(r)

    if any(r.status == "fail" for r in results):
        raise SystemExit(1)
