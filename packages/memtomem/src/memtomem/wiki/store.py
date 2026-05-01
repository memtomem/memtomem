"""``~/.memtomem-wiki/`` git repository abstraction.

Provides :class:`WikiStore` with scratch ``init``, ``init --from <git-url>``
clone, asset listing, and HEAD commit lookup. Snapshot install, override
resolution, lockfile, and staleness lint live in sibling modules per
ADR-0008's roadmap.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

DEFAULT_WIKI_PATH: Path = Path.home() / ".memtomem-wiki"
"""Default wiki location — overridable via ``MEMTOMEM_WIKI_PATH`` env."""

WIKI_ASSET_TYPES: tuple[str, ...] = ("skills", "agents", "commands")
"""Asset directory names at the wiki root. Order is significant for listing."""

_INITIAL_COMMIT_MESSAGE = "Initialize memtomem wiki"

_README_TEMPLATE = """# memtomem wiki

Personal wiki for AI agent skills, agents, and commands.

This is a git repository containing canonical (vendor-neutral) artifacts.

## Layout

- `skills/<name>/SKILL.md` — Anthropic Agent Skills spec, byte-identical
  across Claude Code, Gemini CLI, and Codex CLI.
- `agents/<name>/agent.md` — sub-agent definition (canonical MD + YAML).
- `commands/<name>/command.md` — slash command (canonical, `$ARGUMENTS`).
- `<type>/<name>/overrides/<vendor>.<ext>` — optional vendor-specific
  file; bypasses auto-conversion when present.

## Available commands

Run `mm wiki --help` for the current set of subcommands available in your
installed version. See <https://github.com/memtomem/memtomem> for the
project README and ADR-0008 (the wiki layer design document).
"""


class WikiNotFoundError(RuntimeError):
    """Raised when a wiki operation runs on a path that is not a wiki."""


class WikiAlreadyExistsError(RuntimeError):
    """Raised when ``init`` or ``init_from_url`` would overwrite existing data."""


class CommitNotFoundError(RuntimeError):
    """Raised when a wiki operation references an unreachable commit.

    Surfaces from :meth:`WikiStore.copy_asset_at_commit` when the target
    SHA is not present in the wiki repo's object database (typical
    cause: ``git push --force`` or local rebase past the lockfile pin).
    Distinct from :class:`memtomem.context.install.AssetNotFoundError`
    — that fires when the commit *exists* but the asset path is missing
    at that commit (history rewrite that dropped the path).
    """


@dataclass(frozen=True)
class WikiAsset:
    """An entry in the wiki — a skill, agent, or command directory."""

    type: str
    name: str
    path: Path


def _wiki_path_from_env() -> Path:
    env = os.environ.get("MEMTOMEM_WIKI_PATH")
    if env:
        return Path(env).expanduser()
    return DEFAULT_WIKI_PATH


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd``; raise with stderr on failure."""
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}") from exc


@dataclass(frozen=True)
class WikiStore:
    """View into a wiki repository at ``root``.

    Construct via :meth:`at_default` (uses ``~/.memtomem-wiki/`` or the
    ``MEMTOMEM_WIKI_PATH`` env override) or :meth:`at` for an explicit
    path. The class is frozen — operations that touch disk delegate to
    git via subprocess.
    """

    root: Path

    @classmethod
    def at_default(cls) -> WikiStore:
        return cls(_wiki_path_from_env())

    @classmethod
    def at(cls, path: Path | str) -> WikiStore:
        return cls(Path(path).expanduser())

    def exists(self) -> bool:
        return (self.root / ".git").is_dir()

    def require_exists(self) -> None:
        if not self.exists():
            raise WikiNotFoundError(f"wiki not found at {self.root}, run `mm wiki init`")

    def init(self) -> None:
        """Initialize a new empty wiki at ``root``."""
        if self.exists():
            raise WikiAlreadyExistsError(f"wiki already initialized at {self.root}")
        if self.root.exists() and any(self.root.iterdir()):
            raise WikiAlreadyExistsError(
                f"directory {self.root} is not empty and is not a wiki — refusing to init"
            )

        self.root.mkdir(parents=True, exist_ok=True)
        for asset_type in WIKI_ASSET_TYPES:
            asset_dir = self.root / asset_type
            asset_dir.mkdir(exist_ok=True)
            (asset_dir / ".gitkeep").write_text("", encoding="utf-8")

        (self.root / "README.md").write_text(_README_TEMPLATE, encoding="utf-8")

        _git(["init", "-b", "main"], cwd=self.root)
        _git(["add", "."], cwd=self.root)
        _git(["commit", "-m", _INITIAL_COMMIT_MESSAGE], cwd=self.root)

    def init_from_url(self, url: str) -> None:
        """Clone an existing wiki from ``url`` into ``root``."""
        if self.exists():
            raise WikiAlreadyExistsError(f"wiki already initialized at {self.root}")
        if self.root.exists() and any(self.root.iterdir()):
            raise WikiAlreadyExistsError(f"directory {self.root} is not empty — refusing to clone")

        self.root.parent.mkdir(parents=True, exist_ok=True)
        # ``git clone`` creates the target directory; if root exists empty,
        # remove it first so clone owns the layout.
        if self.root.exists():
            self.root.rmdir()
        _git(["clone", url, str(self.root)], cwd=self.root.parent)

    def current_commit(self) -> str:
        """Return the wiki HEAD commit SHA as the full 40-character hex string.

        Display surfaces (e.g. ``mm wiki list``) may abbreviate when
        rendering, but the canonical value is always full-length to
        avoid abbreviation collisions in stored references such as
        the project lockfile (see ADR-0008).
        """
        self.require_exists()
        result = _git(["rev-parse", "HEAD"], cwd=self.root)
        return result.stdout.strip()

    def commit_is_reachable(self, commit: str) -> bool:
        """``True`` when *commit* resolves to an object in this wiki repo.

        Uses ``git cat-file -e <commit>^{commit}`` and inspects the exit
        code only — no exception is raised for an unreachable commit.
        Used by ``mm context status`` to flag entries whose lockfile pin
        was rebased / force-pushed away (the ``stale-pin`` state) and
        by ``mm context install --all`` to classify orphan entries
        before attempting per-file extraction.

        Returns ``False`` when the commit is missing OR when *commit*
        is malformed (empty string, non-hex chars, etc.) — the caller
        should not need to validate the SHA shape upfront.

        Raises :class:`WikiNotFoundError` if the wiki itself is missing
        — the caller decides whether that's a hard error or a
        graceful-degradation path (status renders rows without
        reachability info; install --all refuses).
        """
        self.require_exists()
        if not commit:
            return False
        result = subprocess.run(
            ["git", "cat-file", "-e", f"{commit}^{{commit}}"],
            cwd=self.root,
            check=False,
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

    def is_dirty(self) -> bool:
        """``True`` when the wiki working tree has uncommitted modifications.

        Wraps ``git status --porcelain`` — true on any combination of
        modified-tracked, staged, or untracked files. Used by ``mm
        context update`` to warn the user that the install/update will
        reflect HEAD only, not the dirty working tree.

        Returns ``False`` if the wiki is clean. Raises ``WikiNotFoundError``
        if the wiki itself is missing (no ``.git`` directory) — caller
        should ``require_exists()`` first if the missing-wiki path is
        not desired.
        """
        self.require_exists()
        result = _git(["status", "--porcelain"], cwd=self.root)
        return bool(result.stdout.strip())

    def copy_asset_at_commit(
        self,
        commit: str,
        asset_type: str,
        name: str,
        dest: Path,
    ) -> int:
        """Materialize ``<wiki>/<asset_type>/<name>/`` at *commit* into *dest*.

        Implementation flow:

        1. ``commit_is_reachable`` precheck — :class:`CommitNotFoundError`
           if the SHA is not in the object database.
        2. ``git ls-tree -r --name-only <commit> -- <asset_type>/<name>/``
           enumerates the files at that commit. An empty result means
           the asset path didn't exist at that revision — raises
           :class:`memtomem.context.install.AssetNotFoundError` (deferred
           import to avoid the install ↔ wiki cycle).
        3. Per-file ``git show <commit>:<relpath>`` reads the bytes.
        4. The bytes are written into a tmpdir adjacent to *dest* (same
           filesystem, so subsequent ``copy_tree_atomic`` rename steps
           don't span devices).
        5. ``copy_tree_atomic(tmpdir, dest)`` mirrors the structure into
           *dest* using the same per-file atomic semantics as
           :func:`memtomem.context.install._install_asset`.

        The wiki working tree is **never touched** — every read uses the
        commit's git objects directly, so a concurrent ``git checkout``
        / edit in ``~/.memtomem-wiki/`` cannot bleed through. Returns
        the count of files written.
        """
        # Deferred import: install.py imports wiki.store at module load;
        # the reverse import here would cycle. Local resolves at call time.
        from memtomem.context._atomic import copy_tree_atomic
        from memtomem.context.install import AssetNotFoundError

        self.require_exists()
        if not self.commit_is_reachable(commit):
            raise CommitNotFoundError(f"commit {commit[:12]} is not reachable in {self.root}")

        src_prefix = f"{asset_type}/{name}/"
        ls_result = _git(
            ["ls-tree", "-r", "--name-only", commit, "--", src_prefix],
            cwd=self.root,
        )
        relpaths = [line for line in ls_result.stdout.splitlines() if line.startswith(src_prefix)]
        if not relpaths:
            raise AssetNotFoundError(
                f"{asset_type}/{name} not present at commit {commit[:12]} in {self.root}"
            )

        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmpdir:
            tmpdir_path = Path(tmpdir)
            for relpath in relpaths:
                inner = relpath[len(src_prefix) :]
                if not inner:
                    # The prefix itself; ls-tree -r shouldn't yield this
                    # but guard against odd git versions.
                    continue
                target = tmpdir_path / inner
                target.parent.mkdir(parents=True, exist_ok=True)
                content = subprocess.run(
                    ["git", "show", f"{commit}:{relpath}"],
                    cwd=self.root,
                    check=True,
                    capture_output=True,
                )
                target.write_bytes(content.stdout)
            return copy_tree_atomic(tmpdir_path, dest)

    def list_assets(self, asset_type: str | None = None) -> list[WikiAsset]:
        """Enumerate asset directories under the wiki.

        ``asset_type`` filters to one of :data:`WIKI_ASSET_TYPES`.
        Hidden entries (``.gitkeep``, ``.git``) are excluded.
        """
        self.require_exists()

        if asset_type is not None and asset_type not in WIKI_ASSET_TYPES:
            raise ValueError(
                f"unknown asset type {asset_type!r}; expected one of {WIKI_ASSET_TYPES}"
            )

        types = (asset_type,) if asset_type else WIKI_ASSET_TYPES
        out: list[WikiAsset] = []
        for t in types:
            tdir = self.root / t
            if not tdir.is_dir():
                continue
            for entry in sorted(tdir.iterdir()):
                if entry.is_dir() and not entry.name.startswith("."):
                    out.append(WikiAsset(type=t, name=entry.name, path=entry))
        return out
