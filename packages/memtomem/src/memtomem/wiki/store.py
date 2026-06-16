"""``~/.memtomem-wiki/`` git repository abstraction.

Provides :class:`WikiStore` with scratch ``init``, ``init --from <git-url>``
clone, asset listing, and HEAD commit lookup. Snapshot install, override
resolution, lockfile, and staleness lint live in sibling modules per
ADR-0008's roadmap.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

logger = logging.getLogger(__name__)

DEFAULT_WIKI_PATH: Path = Path.home() / ".memtomem-wiki"
"""Default wiki location — overridable via ``MEMTOMEM_WIKI_PATH`` env."""

WIKI_ASSET_TYPES: tuple[str, ...] = ("skills", "agents", "commands")
"""Asset directory names at the wiki root. Order is significant for listing."""

_INITIAL_COMMIT_MESSAGE = "Initialize memtomem wiki"

_FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
"""Canonical lockfile-pin shape — full 40-hex lowercase object id."""

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


class WikiHeadMovedError(RuntimeError):
    """Raised when the wiki HEAD advanced under an in-flight commit.

    The web Commit affordance (ADR-0027 §3) passes the ``expected_head``
    the client last saw. If HEAD no longer matches — caught either by the
    upfront check or by the atomic compare-and-swap on the ref update — this
    is raised so the route returns 409 instead of clobbering the moved HEAD.
    """


class WikiNothingToCommitError(RuntimeError):
    """Raised when an isolated commit would reproduce HEAD's existing tree.

    The saved bytes are byte-identical to what is already committed, so
    :meth:`WikiStore.commit_paths` makes no commit. The route maps this to a
    benign "nothing to commit" response (no new history is written).
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


def _git(
    args: list[str],
    cwd: Path,
    *,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd``; raise with stderr on failure.

    ``env`` keys are *merged into* the parent environment (not replacing it)
    — used only to thread ``GIT_INDEX_FILE`` for the out-of-worktree
    temp-index commit (:meth:`WikiStore.commit_paths`) without disturbing
    git's config discovery, identity, or other inherited environment.
    """
    run_env = {**os.environ, **env} if env else None
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            env=run_env,
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}") from exc


def _require_clean_rel(rel: str) -> None:
    """Reject anything that isn't a clean wiki-relative POSIX path.

    Defense-in-depth for :meth:`WikiStore.commit_paths`: the route already
    server-resolves targets, but a path with ``..``, a leading ``/``, or
    redundant components must never reach ``update-index --cacheinfo`` (it
    would name an arbitrary entry in the committed tree).
    """
    p = PurePosixPath(rel)
    if not rel or rel != str(p) or p.is_absolute() or ".." in p.parts:
        raise ValueError(f"unsafe wiki-relative path: {rel!r}")


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

        # The guard above leaves the root either absent or pre-existing-empty;
        # remember which so rollback never deletes a dir the user already had.
        root_preexisted = self.root.exists()
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            for asset_type in WIKI_ASSET_TYPES:
                asset_dir = self.root / asset_type
                asset_dir.mkdir(exist_ok=True)
                (asset_dir / ".gitkeep").write_text("", encoding="utf-8")

            (self.root / "README.md").write_text(_README_TEMPLATE, encoding="utf-8")

            _git(["init", "-b", "main"], cwd=self.root)
            _git(["add", "."], cwd=self.root)
            _git(["commit", "-m", _INITIAL_COMMIT_MESSAGE], cwd=self.root)
        except RuntimeError:
            # Bootstrap failed — most plausibly ``git commit`` with no resolvable
            # user identity (a minimal/rootless container, ``user.useConfigOnly``).
            # Without rollback, ``.git/`` survives → ``exists()`` returns True, so
            # re-running ``init`` / ``init_from_url`` is refused AND every read op
            # (``current_commit``, ``is_dirty``, …) fails on the HEAD-less repo —
            # manual ``rm -rf`` the only escape. Remove exactly what we created
            # (never a pre-existing root) and re-raise so the caller still
            # surfaces the failure. We add no fallback identity: the "memtomem
            # injects no git identity" invariant is preserved.
            if root_preexisted:
                shutil.rmtree(self.root / ".git", ignore_errors=True)
                for asset_type in WIKI_ASSET_TYPES:
                    shutil.rmtree(self.root / asset_type, ignore_errors=True)
                (self.root / "README.md").unlink(missing_ok=True)
            else:
                shutil.rmtree(self.root, ignore_errors=True)
            raise

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
        is malformed — the caller should not need to validate the SHA
        shape upfront. "Malformed" is enforced as anything that is not
        a full 40-hex object id: a symbolic ref (``main``) or an
        abbreviated SHA would otherwise satisfy ``cat-file`` while
        breaking the pin contract — refs move, so a hand-edited
        lockfile pin of ``main`` would let the scanned bytes diverge
        from the extracted bytes and make ``install --all`` restores
        non-reproducible (#1247 Gate A review). Pins written by mm are
        always full-length (:meth:`current_commit`).

        Raises :class:`WikiNotFoundError` if the wiki itself is missing
        — the caller decides whether that's a hard error or a
        graceful-degradation path (status renders rows without
        reachability info; install --all refuses).
        """
        self.require_exists()
        if not commit or _FULL_SHA_RE.fullmatch(commit) is None:
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

    def commit_paths(
        self,
        files: dict[str, bytes],
        *,
        message: str,
        expected_head: str,
    ) -> str:
        """Commit *files* in isolation onto HEAD; return the new commit SHA.

        ``files`` maps wiki-**relative** POSIX paths to the exact bytes to
        commit. The caller has already read+verified them under a lock — the
        "saved blob", not a fresh working-tree ``add`` — so an external
        same-path edit that slipped in cannot be swept in. The commit
        contains **only** these paths layered onto HEAD's tree, independent
        of whatever else is staged in the real index, and the branch ref is
        advanced by an atomic compare-and-swap against *expected_head*
        (ADR-0027 §3 / D-G).

        Mechanics — out-of-worktree temp index → ``commit-tree`` → ref CAS:

        1. The temp index is seeded from HEAD's tree (``read-tree``), so the
           real index — including any unrelated staged changes — is never
           consulted and never swept into the commit.
        2. Each target's bytes are stored byte-exact (``hash-object -w
           --no-filters``, so override Invariant 4 holds despite any repo
           ``.gitattributes`` eol/clean filters) and staged via
           ``update-index --add --cacheinfo``.
        3. ``write-tree`` + ``commit-tree`` build the commit using the wiki
           repo's own git identity (memtomem injects none).
        4. ``update-ref <branch> <new> <expected_head>`` is a compare-and-swap:
           if HEAD moved underneath us — an external ``$EDITOR``+git that
           neither the in-process nor the cross-process lock can bind — it
           fails and we raise :class:`WikiHeadMovedError` rather than
           clobber. This is the binding cross-process guard.
        5. The real index is reconciled for **only** the committed paths from
           the new HEAD — best-effort, since the commit has already landed.

        Raises :class:`WikiHeadMovedError` (HEAD advanced — caller → 409),
        :class:`WikiNothingToCommitError` (bytes identical to HEAD), or
        :class:`RuntimeError` (git failure — the caller MUST surface a fixed
        message; the raw stderr embeds the absolute repo path).
        """
        self.require_exists()
        head = self.current_commit()
        expected = expected_head.strip().lower()
        if _FULL_SHA_RE.fullmatch(expected) is None or head != expected:
            raise WikiHeadMovedError(
                f"wiki HEAD {head[:12]} does not match expected {expected_head[:12]}"
            )

        # Resolve the actual branch ref dynamically — never hardcode
        # ``refs/heads/main`` (a clone may be on another branch); a detached
        # HEAD has no symbolic ref and cannot be safely CAS-advanced.
        branch_ref = _git(["symbolic-ref", "HEAD"], cwd=self.root).stdout.strip()

        tmpdir = Path(tempfile.mkdtemp(prefix="mm-wiki-commit-"))
        try:
            index_file = tmpdir / "index"
            env = {"GIT_INDEX_FILE": str(index_file)}
            _git(["read-tree", head], cwd=self.root, env=env)

            blob_file = tmpdir / "blob"
            for rel, data in files.items():
                _require_clean_rel(rel)
                blob_file.write_bytes(data)
                blob = _git(
                    ["hash-object", "-w", "--no-filters", str(blob_file)],
                    cwd=self.root,
                ).stdout.strip()
                _git(
                    ["update-index", "--add", "--cacheinfo", f"100644,{blob},{rel}"],
                    cwd=self.root,
                    env=env,
                )

            tree = _git(["write-tree"], cwd=self.root, env=env).stdout.strip()
            head_tree = _git(["rev-parse", f"{head}^{{tree}}"], cwd=self.root).stdout.strip()
            if tree == head_tree:
                raise WikiNothingToCommitError("saved bytes match HEAD; nothing to commit")

            new = _git(
                ["commit-tree", tree, "-p", head, "-m", message],
                cwd=self.root,
            ).stdout.strip()

            try:
                _git(["update-ref", branch_ref, new, expected], cwd=self.root)
            except RuntimeError as exc:
                raise WikiHeadMovedError(f"wiki {branch_ref} advanced during commit") from exc

            # Reconcile the REAL index for only the committed paths from the
            # new HEAD. Best-effort: the commit has already landed at the ref,
            # so a failure here (e.g. an external ``index.lock``) must never be
            # raised — that would tell the caller the commit failed when it
            # succeeded. A stale real index is cosmetic and self-heals on the
            # next commit (step 1 reads from HEAD, not the real index).
            try:
                _git(["reset", "-q", new, "--", *files.keys()], cwd=self.root)
            except RuntimeError:
                logger.warning("wiki commit %s landed but real-index reconcile failed", new[:12])
            return new
        finally:
            shutil.rmtree(tmpdir, ignore_errors=True)

    def copy_asset_at_commit(
        self,
        commit: str,
        asset_type: str,
        name: str,
        dest: Path,
    ) -> dict[str, str]:
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
        the rel→SHA-256 map of the files written (``copy_tree_atomic``'s
        return, #1247 id 15): the tmpdir bytes it hashes are the
        git-object bytes just materialized, so for dest purposes each
        digest describes exactly what was written to *dest*.
        """
        # Deferred import: install.py imports wiki.store at module load;
        # the reverse import here would cycle. Local resolves at call time.
        from memtomem.context._atomic import (
            DIRTY_SKIP_SUFFIXES,
            copy_tree_atomic,
            is_copy_skipped_rel,
        )

        inner_relpaths = self.asset_files_at_commit(commit, asset_type, name)

        dest.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(dir=dest.parent) as tmpdir:
            tmpdir_path = Path(tmpdir)
            for inner in inner_relpaths:
                # Filter BEFORE reading: the final copy would skip these
                # anyway, but materializing them first would park unscanned
                # wiki bytes inside the git-tracked ``.memtomem/`` tree —
                # surviving a crash/SIGKILL as commit-able residue, and
                # invisible to the pinned-install Gate A scan, which uses
                # the same predicate (#1247).
                if is_copy_skipped_rel(inner):
                    continue
                target = tmpdir_path / inner
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(self.read_asset_file_at_commit(commit, asset_type, name, inner))
            return copy_tree_atomic(tmpdir_path, dest, skip_suffixes=DIRTY_SKIP_SUFFIXES)

    def read_asset_file_at_commit(self, commit: str, asset_type: str, name: str, rel: str) -> bytes:
        """Read one asset file's bytes at *commit* straight from git objects.

        ``git show <commit>:<asset_type>/<name>/<rel>`` — the wiki working
        tree is never consulted, so the bytes are immutable for a given
        commit. Shared by :meth:`copy_asset_at_commit` (extraction) and the
        pinned-install Gate A privacy scan (#1247), which must observe
        exactly the bytes the extractor would write.
        """
        result = subprocess.run(
            ["git", "show", f"{commit}:{asset_type}/{name}/{rel}"],
            cwd=self.root,
            check=True,
            capture_output=True,
        )
        return result.stdout

    def asset_files_at_commit(self, commit: str, asset_type: str, name: str) -> list[str]:
        """List the asset's file relpaths (relative to the asset dir) at *commit*.

        ``git ls-tree -r --name-only`` against the commit's objects — the
        wiki working tree is never consulted. Raises
        :class:`CommitNotFoundError` for an unreachable SHA and
        :class:`memtomem.context.install.AssetNotFoundError` when the asset
        path has no files at that revision. Used by
        :meth:`copy_asset_at_commit` for extraction and by
        ``mm context install --all`` reconciliation, which needs the
        expected file set without re-extracting (#1247).
        """
        from memtomem.context.install import AssetNotFoundError

        self.require_exists()
        if not self.commit_is_reachable(commit):
            raise CommitNotFoundError(f"commit {commit[:12]} is not reachable in {self.root}")

        src_prefix = f"{asset_type}/{name}/"
        ls_result = _git(
            ["ls-tree", "-r", "--name-only", commit, "--", src_prefix],
            cwd=self.root,
        )
        inner_relpaths = [
            line[len(src_prefix) :]
            for line in ls_result.stdout.splitlines()
            # The startswith filter guards against odd git path output; the
            # truthiness check drops a bare prefix row (ls-tree -r shouldn't
            # yield one, but guard against odd git versions).
            if line.startswith(src_prefix) and line[len(src_prefix) :]
        ]
        if not inner_relpaths:
            raise AssetNotFoundError(
                f"{asset_type}/{name} not present at commit {commit[:12]} in {self.root}"
            )
        return inner_relpaths

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
