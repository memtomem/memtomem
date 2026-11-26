#!/usr/bin/env python3
"""MCP launcher for the agent-eval plugin: one isolated memtomem store per eval run.

``claude plugin eval`` starts this as the plugin's MCP server (``--mocks off``) with the run's
temporary HOME and an empty workspace inside it. The launcher:

1. refuses to start unless the host's ``CLAUDE_CODE_EVAL_CONFINED`` marker is set, HOME is
   not the user's real home (nor inside or above it), and the working directory is inside HOME.
   This is a layout check, not proof of an eval run. If the host stops setting the marker, the
   launcher refuses rather than guesses;
2. keeps the server away from real stores by construction: the environment is built from
   scratch (no inherited ``MEMTOMEM_*``, dotenv off), the runtime dir and database live in a
   state directory created fresh inside HOME, and the only memory root is ``<cwd>/mm-notes``,
   where file graders can read the notes. Existing paths are refused and files are written
   without following links;
3. seeds the notes root from ``seeds/<EVAL_MM_SEED>/`` when the case names one, and indexes it.
   It also creates today's (UTC) day file empty and links ``<cwd>/mm-today.md`` to it: mem_add
   appends to that file by default, and a grader needs a fixed path to read what was persisted;
4. runs the import check, the indexer and the server with one interpreter, one environment and
   ``-P`` (no script or working directory on ``sys.path``), from the state directory, after
   checking that this interpreter imports ``memtomem`` from this checkout.

Standard library only: it runs under whatever ``python3`` the host finds.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
import pwd
import re
import stat
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent
REPO = PLUGIN_ROOT.parents[2]
SEEDS = PLUGIN_ROOT / "seeds"
SEED_NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
EVAL_MARKER = "CLAUDE_CODE_EVAL_CONFINED"
_BASE_PATH = "/usr/bin:/bin"
_INDEX = "import sys; from memtomem.cli import cli; cli(prog_name='mm')"
_SERVE = "from memtomem._server_entry import main; main([])"


class LaunchError(Exception):
    """The launcher refuses to start; the message says why."""


def check_sandbox(environ: Mapping[str, str], home: Path, cwd: Path, real_home: Path) -> None:
    if EVAL_MARKER not in environ:
        raise LaunchError(f"{EVAL_MARKER} is not set: not inside a claude plugin eval run")
    home, cwd, real = home.resolve(), cwd.resolve(), real_home.resolve()
    if home == real or real in home.parents or home in real.parents:
        raise LaunchError(f"HOME {home} is the real home, inside it, or above it")
    if home not in cwd.parents:
        raise LaunchError(f"working directory {cwd} is not inside HOME {home}")


def _write_new(path: Path, text: str) -> None:
    """Create ``path``; refuse an existing file or a symlink."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def prepare(home: Path, cwd: Path) -> dict[str, Path]:
    """Create the private state dir and the notes root; both must not exist yet."""
    box = home / ".mm-eval"
    try:
        box.mkdir(mode=0o700)
        (box / "home").mkdir(mode=0o700)
        (box / "home" / ".memtomem").mkdir(mode=0o700)
        (box / "runtime").mkdir(mode=0o700)
        (cwd / "mm-notes").mkdir(mode=0o700)
    except FileExistsError as exc:
        raise LaunchError(f"{exc.filename} already exists") from exc
    return {
        "box": box,
        "home": box / "home",
        "runtime": box / "runtime",
        "db": box / "memtomem.db",
        "notes": cwd / "mm-notes",
    }


def write_config(paths: dict[str, Path]) -> None:
    config = {
        "storage": {"sqlite_path": str(paths["db"])},
        "indexing": {"memory_dirs": [str(paths["notes"])]},
        "embedding": {"provider": "none", "dimension": 1024},
        "search": {"enable_dense": False},
    }
    _write_new(paths["home"] / ".memtomem" / "config.json", json.dumps(config))


def seed_files(name: str, seeds: Path | None = None) -> list[Path]:
    """The seed's Markdown files: a known name, no symlinks, regular files only."""
    seeds = SEEDS if seeds is None else seeds
    if not SEED_NAME.fullmatch(name):
        raise LaunchError(f"invalid seed name {name!r}")
    source = seeds / name
    if source.is_symlink() or not source.is_dir() or source.resolve().parent != seeds.resolve():
        raise LaunchError(f"unknown seed {name!r}")
    files = sorted(source.glob("*.md"))
    for item in files:
        if not stat.S_ISREG(item.lstat().st_mode):
            raise LaunchError(f"seed file {item.name} is not a regular file")
    if not files:
        raise LaunchError(f"seed {name!r} has no Markdown files")
    return files


def link_today(cwd: Path, notes: Path, now: datetime | None = None) -> Path:
    """Create today's empty day file and the fixed alias ``<cwd>/mm-today.md`` pointing at it.

    mem_add's default target is ``<memory root>/<UTC date>.md``. The alias misses a write that
    lands elsewhere (an explicit ``file=``, or a run that crosses UTC midnight). Graders that
    read it must not be the only evidence that nothing was written (tools/agent-eval/README.md).
    """
    day = f"{(now or datetime.now(timezone.utc)).strftime('%Y-%m-%d')}.md"
    _write_new(notes / day, "")
    alias = cwd / "mm-today.md"
    os.symlink(Path(notes.name) / day, alias)
    return alias


def copy_seed(files: list[Path], notes: Path) -> None:
    for item in files:
        _write_new(notes / item.name, item.read_text(encoding="utf-8"))


def server_env(paths: dict[str, Path], repo: Path = REPO) -> dict[str, str]:
    return {
        "PATH": _BASE_PATH,
        "LANG": "en_US.UTF-8",
        "HOME": str(paths["home"]),
        "USERPROFILE": str(paths["home"]),
        "XDG_CONFIG_HOME": str(paths["home"] / ".config"),
        "TMPDIR": str(paths["box"]),
        "PYTHON_DOTENV_DISABLED": "1",
        "PYTHONHASHSEED": "0",
        # Both are needed: the override is honoured only alongside PYTEST_CURRENT_TEST.
        "_MEMTOMEM_TEST_RUNTIME_DIR": str(paths["runtime"]),
        "PYTEST_CURRENT_TEST": "agent-eval",
        "MEMTOMEM_TOOL_MODE": "core",
        # This checkout's source first, whichever venv supplies the dependencies.
        "PYTHONPATH": str(repo / "packages" / "memtomem" / "src"),
    }


def select_python(repo: Path = REPO) -> Path:
    """This checkout's venv interpreter, else (a git worktree) the main checkout's.

    Which venv is chosen does not decide what code runs: check_import_origin does.
    """
    own = repo / ".venv" / "bin" / "python"
    if own.exists():
        return own
    common = subprocess.run(
        ["/usr/bin/git", "-C", str(repo), "rev-parse", "--path-format=absolute"]
        + ["--git-common-dir"],
        env={"PATH": _BASE_PATH},
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    ).stdout.strip()
    python = Path(common).parent / ".venv" / "bin" / "python" if common else own
    if not python.exists():
        raise LaunchError(f"no venv interpreter for {repo}")
    return python


def check_import_origin(python: Path, env: dict[str, str], repo: Path = REPO) -> None:
    """``python`` under ``env`` must import ``memtomem`` from the checkout under test."""
    probe = subprocess.run(
        [str(python), "-P", "-c", "import memtomem, sys; sys.stdout.write(memtomem.__path__[0])"],
        env=env,
        cwd=env.get("TMPDIR"),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if probe.returncode != 0 or not probe.stdout.strip():
        raise LaunchError(f"{python} cannot import memtomem: {probe.stderr.strip()[-300:]}")
    # Compare the package directory, not __init__.py: an __init__.py symlinked into this
    # checkout from a foreign package directory would still load that directory's modules.
    origin = Path(probe.stdout.strip()).resolve()
    expected = (repo / "packages" / "memtomem" / "src" / "memtomem").resolve()
    if origin != expected:
        raise LaunchError(f"memtomem imports from {origin}, not from {expected}")


def main() -> None:
    # Resolve once and use the resolved paths for everything after the check.
    home = Path(os.environ.get("HOME", "/nonexistent")).resolve()
    cwd = Path(os.getcwd()).resolve()
    check_sandbox(os.environ, home, cwd, Path(pwd.getpwuid(os.getuid()).pw_dir))
    seed = os.environ.get("EVAL_MM_SEED", "")
    files = seed_files(seed) if seed else []
    python = select_python()

    paths = prepare(home, cwd)
    write_config(paths)
    env = server_env(paths)
    check_import_origin(python, env)
    link_today(cwd, paths["notes"])
    if files:
        copy_seed(files, paths["notes"])
        indexed = subprocess.run(
            [str(python), "-P", "-c", _INDEX, "index", str(paths["notes"])],
            env=env,
            cwd=str(paths["box"]),
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if indexed.returncode != 0:
            raise LaunchError(f"seeding {seed!r} failed: {indexed.stderr.strip()[-500:]}")
    log = os.open(
        paths["box"] / "server.log", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
    )
    os.dup2(log, 2)
    os.chdir(paths["box"])
    os.execve(str(python), [str(python), "-P", "-c", _SERVE], env)


if __name__ == "__main__":
    try:
        main()
    except (LaunchError, OSError) as exc:
        sys.stderr.write(f"agent-eval launcher: {exc}\n")
        sys.exit(2)
