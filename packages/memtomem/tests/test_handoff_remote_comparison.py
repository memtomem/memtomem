"""The Git behaviour handoff resume's remote comparison relies on (#2930).

Resume used to compare a record with the local checkout only. A record that
matched a ``main`` one commit behind ``origin/main`` passed, and a fix that had
already been merged was implemented again. Resume step 6 now reads the
upstream's tip from the remote and compares it with ``HEAD``.

``_compare_remote`` below runs the commands step 6 names, in its order, against
real repositories. It checks what the prose assumes about Git: which commands
pick the right upstream, which exit statuses mean what, and that nothing is
written. It is not proof that the prose matches it. ``test_plugin_assets`` pins
each outcome clause of the prose for that, and the parity test here only keeps
the two from naming different commands.

Each rejected design left a case here:

* ``git fetch``: under a negative refspec or ``remote.<name>.skipFetchAll`` it
  exits 0 and leaves the tracking ref stale, and ``--prune`` with
  ``fetch.pruneTags`` deletes local-only tags.
* ``git merge-base --is-ancestor``: lazily fetches the remote tip's objects in a
  partial clone, and reads an annotated tag's object id as "ahead".
* ``git config --get``: returns the last of several ``merge`` values; Git uses
  the first.
* ``git rev-parse --abbrev-ref HEAD``: prints ``heads/main`` when a tag is also
  named ``main``, which selects another branch's config.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[3]

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

# The commands of resume step 6, with the prose's placeholders. The model fills
# them in; the parity test finds each one verbatim in the workflow.
_BRANCH = "git rev-parse --symbolic-full-name HEAD"
_REMOTE = "git config --get-all branch.<branch>.remote"
_MERGE = "git config --get-all branch.<branch>.merge"
_LS_REMOTE = "git ls-remote --exit-code -- <remote> <merge>"
_HEAD = "git rev-parse HEAD"

_ENV = {
    **os.environ,
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


def _run(cwd: Path, argv: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, cwd=cwd, env=_ENV, capture_output=True, text=True, check=False, timeout=60
    )


def _git(cwd: Path, *args: str) -> str:
    done = _run(cwd, ["git", *args])
    assert done.returncode == 0, (args, done.stderr)
    return done.stdout.strip()


def _argv(template: str, **values: str) -> list[str]:
    """Split a command template, then substitute whole placeholder words.

    Substituting after the split keeps a value with spaces or a leading ``-``
    one argument, as the workflow passes it.
    """
    words = template.split()
    out = []
    for word in words:
        for key, value in values.items():
            word = word.replace(f"<{key}>", value)
        out.append(word)
    return out


def _compare_remote(repo: Path) -> str:
    """Resume step 6, as an outcome code."""
    branch = _run(repo, _argv(_BRANCH)).stdout.strip()
    if not branch.startswith("refs/heads/"):
        return "detached"
    name = branch.removeprefix("refs/heads/")
    remotes = _run(repo, _argv(_REMOTE, branch=name)).stdout.splitlines()
    merges = _run(repo, _argv(_MERGE, branch=name)).stdout.splitlines()
    if len(remotes) != 1 or len(merges) != 1:
        return "no-single-upstream"
    remote, merge = remotes[0], merges[0]
    if remote == ".":
        return "local-upstream"
    if not merge.startswith("refs/heads/"):
        return "unsupported-ref"
    listed = _run(repo, _argv(_LS_REMOTE, remote=remote, merge=merge))
    if listed.returncode == 2:
        return "not-advertised"
    if listed.returncode != 0:
        return "not-compared"
    tips = [
        line.split("\t", 1)[0]
        for line in listed.stdout.splitlines()
        if line.split("\t", 1)[1:] == [merge]
    ]
    if len(tips) != 1:
        return "ambiguous"
    head = _run(repo, _argv(_HEAD)).stdout.strip()
    return "in-sync" if tips[0] == head else "differs"


def _snapshot(repo: Path) -> tuple[str, tuple[str, ...], bytes | None]:
    """Every ref, every object file, and ``FETCH_HEAD``'s bytes (``None`` when absent).

    The bytes, not only presence: a fixture that fetched first already has a
    ``FETCH_HEAD``, and a comparison that rewrote it would otherwise pass.
    """
    git_dir = Path(_git(repo, "rev-parse", "--absolute-git-dir"))
    objects = tuple(sorted(str(p.relative_to(git_dir)) for p in (git_dir / "objects").rglob("*")))
    refs = _git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    fetch_head = git_dir / "FETCH_HEAD"
    return refs, objects, fetch_head.read_bytes() if fetch_head.exists() else None


def _compare_without_writes(repo: Path) -> str:
    before = _snapshot(repo)
    outcome = _compare_remote(repo)
    assert _snapshot(repo) == before, "the remote comparison wrote to the repository"
    return outcome


def _commit(repo: Path, name: str) -> None:
    (repo / name).write_text(name, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-q", "-m", name)


@pytest.fixture
def repos(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A bare remote, the checkout that resumes, and another clone that pushes."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    _git(remote, "config", "uploadpack.allowFilter", "true")
    seed = tmp_path / "seed"
    _git(tmp_path, "init", "-q", "-b", "main", str(seed))
    _commit(seed, "one")
    _git(seed, "remote", "add", "origin", str(remote))
    _git(seed, "push", "-q", "origin", "main")
    here = tmp_path / "here"
    other = tmp_path / "other"
    _git(tmp_path, "clone", "-q", str(remote), str(here))
    _git(tmp_path, "clone", "-q", str(remote), str(other))
    return remote, here, other


def test_in_sync(repos) -> None:
    _remote, here, _other = repos
    assert _compare_without_writes(here) == "in-sync"


def test_the_incident_a_remote_that_moved_is_reported(repos) -> None:
    """The record matched a local ``main`` behind ``origin/main``; resume said nothing."""
    _remote, here, other = repos
    _commit(other, "fix")
    _git(other, "push", "-q", "origin", "main")
    # The local view is stale, exactly as in the incident.
    assert _git(here, "status", "--porcelain=v1", "--branch") == "## main...origin/main"

    assert _compare_without_writes(here) == "differs"


def test_unpushed_local_commits_are_reported_too(repos) -> None:
    _remote, here, _other = repos
    _commit(here, "local")
    assert _compare_without_writes(here) == "differs"


def test_fetch_configuration_does_not_matter(repos) -> None:
    """The configs that left a fetch-based check stale, or let it delete a tag."""
    _remote, here, other = repos
    _git(here, "config", "--add", "remote.origin.fetch", "^refs/heads/main")
    _git(here, "config", "remote.origin.skipFetchAll", "true")
    _git(here, "config", "fetch.all", "true")
    _git(here, "config", "fetch.pruneTags", "true")
    _git(here, "tag", "local-only")
    assert _compare_without_writes(here) == "in-sync"

    _commit(other, "fix")
    _git(other, "push", "-q", "origin", "main")
    assert _compare_without_writes(here) == "differs"
    assert _git(here, "tag", "-l", "local-only") == "local-only"


def test_several_merge_values_are_not_compared(repos) -> None:
    """``--get`` would return ``other``, while Git's upstream is the first value."""
    _remote, here, other = repos
    _git(other, "push", "-q", "origin", "main:other")
    _commit(other, "fix")
    _git(other, "push", "-q", "origin", "main")
    _git(here, "config", "--add", "branch.main.merge", "refs/heads/other")
    assert _git(here, "config", "--get", "branch.main.merge") == "refs/heads/other"

    assert _compare_without_writes(here) == "no-single-upstream"


@pytest.mark.parametrize("merge", ["refs/tags/annotated", "main"], ids=["annotated-tag", "short"])
def test_a_merge_value_that_is_not_a_remote_branch_is_not_compared(repos, merge) -> None:
    """An annotated tag's advertised id is the tag object, never ``HEAD``."""
    _remote, here, _other = repos
    _git(here, "tag", "-a", "-m", "t", "annotated")
    _git(here, "push", "-q", "origin", "annotated")
    _git(here, "config", "branch.main.merge", merge)
    assert _compare_without_writes(here) == "unsupported-ref"


def test_a_local_upstream_is_not_compared(repos) -> None:
    _remote, here, _other = repos
    _git(here, "config", "branch.main.remote", ".")
    _git(here, "config", "branch.main.merge", "refs/remotes/origin/main")
    assert _compare_without_writes(here) == "local-upstream"


def test_a_detached_head_is_not_compared(repos) -> None:
    _remote, here, _other = repos
    _git(here, "switch", "-q", "--detach")
    assert _compare_without_writes(here) == "detached"


def test_a_branch_without_an_upstream_is_not_compared(repos) -> None:
    _remote, here, _other = repos
    _git(here, "switch", "-q", "-c", "lonely")
    assert _compare_without_writes(here) == "no-single-upstream"


def test_a_tag_named_like_the_branch_does_not_redirect_the_config(repos) -> None:
    """``--abbrev-ref`` prints ``heads/main`` here and would read ``branch.heads/main.*``."""
    _remote, here, other = repos
    _git(other, "push", "-q", "origin", "main:same-as-here")
    _commit(other, "fix")
    _git(other, "push", "-q", "origin", "main")
    _git(here, "tag", "main")
    _git(here, "config", "branch.heads/main.remote", "origin")
    _git(here, "config", "branch.heads/main.merge", "refs/heads/same-as-here")
    assert _git(here, "rev-parse", "--abbrev-ref", "HEAD") == "heads/main"

    assert _compare_without_writes(here) == "differs"


def test_a_ref_the_remote_does_not_advertise_is_a_divergence(repos) -> None:
    _remote, here, other = repos
    _git(other, "push", "-q", "origin", "main:feature")
    _git(here, "fetch", "-q", "origin")
    _git(here, "switch", "-q", "-c", "feature", "--track", "origin/feature")
    _git(other, "push", "-q", "origin", "--delete", "feature")

    assert _compare_without_writes(here) == "not-advertised"


def test_an_unreachable_remote_is_not_compared(repos, tmp_path) -> None:
    _remote, here, _other = repos
    _git(here, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    assert _compare_without_writes(here) == "not-compared"


def test_an_option_shaped_remote_name_stays_an_operand(repos, tmp_path) -> None:
    """``--`` keeps a hostile config value from becoming ``--upload-pack``."""
    _remote, here, _other = repos
    marker = tmp_path / "upload-pack-ran"
    _git(here, "config", "branch.main.remote", f"--upload-pack=touch {marker}")

    assert _compare_without_writes(here) == "not-compared"
    assert not marker.exists()


def test_a_partial_clone_fetches_no_objects(repos, tmp_path) -> None:
    """An ancestry check would fetch the missing tip here; equality reads ids only."""
    remote, _here, other = repos
    partial = tmp_path / "partial"
    _git(tmp_path, "clone", "-q", "--filter=blob:none", remote.as_uri(), str(partial))
    assert _git(partial, "config", "remote.origin.partialclonefilter") == "blob:none"
    _commit(other, "fix")
    _git(other, "push", "-q", "origin", "main")

    assert _compare_without_writes(partial) == "differs"


def test_the_model_runs_the_commands_the_workflow_names() -> None:
    """Parity of commands only; the outcome clauses are pinned in ``test_plugin_assets``."""
    body = (_ROOT / "packages/memtomem-plugin-assets/workflows/handoff.md").read_text(
        encoding="utf-8"
    )
    flat = " ".join(body.split())
    for command in (_BRANCH, _REMOTE, _MERGE, _LS_REMOTE, _HEAD):
        assert f"`{command}`" in flat, command
