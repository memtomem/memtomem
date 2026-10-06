#!/usr/bin/env python3
"""Publish the Claude plugin folder to the branch the plugin directory follows.

The Claude plugin directory validates everything above the plugin folder on the
ref it tracks, and refuses this repository's root ``pyproject.toml`` because
its ``[tool.uv.sources]`` workspace entry reads as a package-source redirect.
So the directory tracks a branch whose commits hold the plugin folder and
nothing else, at the same path it has on ``main``. This tool builds the next
commit of that branch from a commit on ``main`` and pushes it.

It refuses rather than repairs, and every check runs before the push: a branch
tip that is not shaped like this tool's own output, a content change without a
higher plugin version, a lookup that failed for any reason other than the
branch not existing. The push is a plain one: a branch that was moved to
another commit in the meantime rejects it.

A refusal means nothing was pushed. A failed push is reported separately,
because a failure reported by the remote does not prove the branch is unchanged.

The tip check is a consistency check, not authentication. Anyone who can push
to the repository can hand-craft a commit that passes it.

Credentials are git's business, not this tool's: it runs whatever ``git push``
the checkout is configured for.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path


PLUGIN_PATH = "packages/memtomem-claude-plugin"
_MANIFEST = f"{PLUGIN_PATH}/.claude-plugin/plugin.json"
_TRAILER = "Source-Commit"
_VERSION_RE = re.compile(r"\d+\.\d+\.\d+")
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_IDENTITY = ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL")


class PublishError(RuntimeError):
    """The branch cannot be published as asked; nothing was pushed."""


class PushFailed(RuntimeError):
    """The push was attempted and did not report success."""


class _Repo:
    def __init__(self, root: Path) -> None:
        self._root = root

    def git(self, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        """Run git and hand back the result; callers decide what each exit code means."""
        try:
            return subprocess.run(
                ["git", "-C", str(self._root), *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                # A commit message is bytes in whatever encoding its author used.
                errors="replace",
                input=stdin,
                env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PublishError(f"cannot run git {args[0]}: {exc}") from exc

    def ok(self, *args: str, stdin: str | None = None) -> str:
        result = self.git(*args, stdin=stdin)
        if result.returncode != 0:
            raise PublishError(
                f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip()}"
            )
        return result.stdout


def _is_commit(repo: _Repo, object_id: str) -> bool:
    # The object's own type. ``<id>^{commit}`` would also accept a tag that
    # points at a commit, and a branch must not hold one.
    result = repo.git("cat-file", "-t", object_id)
    return result.returncode == 0 and result.stdout.strip() == "commit"


def _entries(repo: _Repo, tree: str) -> list[tuple[str, str, str]]:
    """``(type, id, name)`` for each entry directly inside ``tree``."""
    entries = []
    for record in repo.ok("ls-tree", "-z", tree).split("\0"):
        if record:
            meta, name = record.split("\t", 1)
            _mode, kind, object_id = meta.split(" ")
            entries.append((kind, object_id, name))
    return entries


def _plugin_tree(repo: _Repo, commit: str, label: str) -> str:
    result = repo.git("rev-parse", "--verify", "--quiet", f"{commit}:{PLUGIN_PATH}")
    tree = result.stdout.strip()
    if result.returncode != 0 or repo.git("cat-file", "-t", tree).stdout.strip() != "tree":
        raise PublishError(f"{label} has no directory at {PLUGIN_PATH}")
    return tree


def _version(repo: _Repo, commit: str, label: str) -> tuple[int, ...]:
    result = repo.git("show", f"{commit}:{_MANIFEST}")
    if result.returncode != 0:
        raise PublishError(f"{label} has no {_MANIFEST}")
    try:
        version = json.loads(result.stdout).get("version")
    except (ValueError, AttributeError) as exc:
        raise PublishError(f"{_MANIFEST} at {label} is not a JSON object") from exc
    if not isinstance(version, str) or not _VERSION_RE.fullmatch(version):
        raise PublishError(f"{_MANIFEST} at {label} has version {version!r}; expected X.Y.Z")
    return tuple(int(part) for part in version.split("."))


def _dotted(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


def _tip_source(repo: _Repo, tip: str) -> str:
    """The ``main`` commit the tip says it was built from."""
    raw = repo.ok("cat-file", "commit", tip)
    message = raw.split("\n\n", 1)[1] if "\n\n" in raw else ""
    lines = [line for line in message.splitlines() if line.startswith(f"{_TRAILER}:")]
    if len(lines) != 1:
        raise PublishError(
            f"branch tip {tip} carries {len(lines)} {_TRAILER} trailers; expected exactly one"
        )
    value = lines[0].removeprefix(f"{_TRAILER}:").strip()
    if not _SHA_RE.fullmatch(value) or not _is_commit(repo, value):
        raise PublishError(
            f"branch tip {tip} names {_TRAILER} {value!r}, which is not a commit here"
        )
    return value


def _require_wrapped(repo: _Repo, tip: str, expected: str) -> None:
    """The tip's whole tree must be the plugin path wrapped around ``expected``, nothing else."""
    tree = repo.ok("rev-parse", "--verify", f"{tip}^{{tree}}").strip()
    walked = ""
    for segment in PLUGIN_PATH.split("/"):
        entries = _entries(repo, tree)
        if len(entries) != 1 or entries[0][0] != "tree" or entries[0][2] != segment:
            names = sorted(name for _kind, _id, name in entries)
            raise PublishError(
                f"branch tip {tip} holds {names} at {walked or 'its root'}; "
                f"expected only the directory {segment!r}"
            )
        tree = entries[0][1]
        walked = f"{walked}/{segment}" if walked else segment
    if tree != expected:
        raise PublishError(
            f"branch tip {tip} does not hold the plugin folder of the commit it names"
        )


def _wrap(repo: _Repo, tree: str) -> str:
    for segment in reversed(PLUGIN_PATH.split("/")):
        tree = repo.ok("mktree", stdin=f"040000 tree {tree}\t{segment}\n").strip()
    return tree


def _build(repo: _Repo, source: str, tip: str | None) -> str:
    """The commit the branch should point at: ``tip`` itself when nothing changed."""
    resolved = repo.git("rev-parse", "--verify", "--quiet", f"{source}^{{commit}}")
    if resolved.returncode != 0:
        raise PublishError(f"source {source!r} is not a commit here")
    source = resolved.stdout.strip()
    plugin = _plugin_tree(repo, source, f"source {source}")
    version = _version(repo, source, f"source {source}")

    parents: list[str] = []
    if tip is not None:
        if not _is_commit(repo, tip):
            raise PublishError(f"branch tip {tip} is not a commit")
        tip_source = _tip_source(repo, tip)
        tip_plugin = _plugin_tree(repo, tip_source, f"{_TRAILER} {tip_source}")
        _require_wrapped(repo, tip, tip_plugin)
        ancestry = repo.git("merge-base", "--is-ancestor", tip_source, source)
        if ancestry.returncode == 1:
            raise PublishError(
                f"source {source} does not descend from {tip_source}, "
                "which the branch was last published from"
            )
        if ancestry.returncode != 0:
            raise PublishError(
                f"cannot compare {tip_source} and {source}: {ancestry.stderr.strip()}"
            )
        if plugin == tip_plugin:
            return tip
        published = _version(repo, tip, f"branch tip {tip}")
        if version <= published:
            raise PublishError(
                f"the plugin folder changed but its version {_dotted(version)} is not higher "
                f"than the published {_dotted(published)}; bump the version before releasing"
            )
        parents = ["-p", tip]

    missing = [name for name in _IDENTITY if not os.environ.get(name)]
    if missing:
        raise PublishError(f"commit identity is not set: {', '.join(missing)}")

    return repo.ok(
        "commit-tree",
        "--no-gpg-sign",
        _wrap(repo, plugin),
        *parents,
        "-m",
        f"publish: memtomem-claude-plugin {_dotted(version)} from {source[:8]}",
        "-m",
        f"{_TRAILER}: {source}",
    ).strip()


def _remote_tip(repo: _Repo, remote: str, branch: str) -> str | None:
    """The branch's commit on ``remote``, fetched; ``None`` only when it does not exist."""
    ref = f"refs/heads/{branch}"
    rows = [line.split("\t", 1) for line in repo.ok("ls-remote", remote, ref).splitlines()]
    if any(len(row) != 2 for row in rows):
        raise PublishError(f"cannot read the answer of git ls-remote {remote} {ref}")
    # ls-remote patterns match a trailing part of the name, so the answer can
    # include refs/heads/<anything>/<ref>. Only the row for the ref itself counts.
    exact = [object_id for object_id, name in rows if name == ref]
    if not exact:
        return None
    if len(exact) != 1 or not _SHA_RE.fullmatch(exact[0]):
        raise PublishError(f"git ls-remote {remote} {ref} gave {len(exact)} answers for one ref")
    repo.ok("fetch", "--no-tags", remote, ref)
    fetched = repo.ok("rev-parse", "--verify", "FETCH_HEAD").strip()
    if fetched != exact[0]:
        raise PublishError(
            f"{remote} moved {branch} from {exact[0]} to {fetched} while it was being read"
        )
    return fetched


def _push_report(commit: str, branch: str, detail: str) -> str:
    return (
        f"git push of {commit} to {branch} failed {detail}. "
        "The branch may or may not have been updated; look at it before running this again"
    )


def publish(repo: _Repo, *, source: str, remote: str, branch: str, dry_run: bool) -> str:
    tip = _remote_tip(repo, remote, branch)
    commit = _build(repo, source, tip)
    if commit != tip and not dry_run:
        # A plain push: the remote refuses anything that is not a fast-forward,
        # which is the answer wanted when the branch moved on since the lookup.
        try:
            result = repo.git("push", remote, f"{commit}:refs/heads/{branch}")
        except PublishError as exc:
            # git did not finish (a timeout, say); the remote may still have acted.
            raise PushFailed(_push_report(commit, branch, str(exc))) from exc
        if result.returncode != 0:
            detail = f"({result.returncode}): {result.stderr.strip()}"
            raise PushFailed(_push_report(commit, branch, detail))
    return commit


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0], allow_abbrev=False)
    parser.add_argument("--source", required=True, help="commit on main to publish from")
    parser.add_argument("--remote", required=True, help="remote name, URL or path")
    parser.add_argument("--branch", required=True, help="branch the directory follows")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--dry-run", action="store_true", help="build the commit but do not push it"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        commit = publish(
            _Repo(args.repo_root.resolve()),
            source=args.source,
            remote=args.remote,
            branch=args.branch,
            dry_run=args.dry_run,
        )
    except PublishError as exc:
        print(f"publish refused: {exc}", file=sys.stderr)
        return 1
    except PushFailed as exc:
        print(f"publish failed: {exc}", file=sys.stderr)
        return 1
    print(commit)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
