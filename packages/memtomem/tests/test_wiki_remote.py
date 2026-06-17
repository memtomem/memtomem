"""Tests for the wiki remote/backup verbs — ``WikiStore.remote_url`` /
``set_remote`` / ``current_branch`` / ``push`` / ``pull`` and the
credential-redaction helper (ADR-0008 "git remotes", issue #1416).

These are thin git wrappers: the tests assert the round-trip works, that the
memtomem-level precondition (no remote) is friendly, and that git's own
errors (non-fast-forward, conflict, dirty tree) surface verbatim while
memtomem leaves the working tree for the user to resolve.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from memtomem.wiki.store import (
    WikiDetachedHeadError,
    WikiNotFoundError,
    WikiStore,
    _git,
    _redact_url_userinfo,
)


def _bare_origin(path: Path) -> Path:
    """Create a bare git repo to act as ``origin`` (no working tree)."""
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def _init_wiki(path: Path) -> WikiStore:
    store = WikiStore.at(path)
    store.init()
    return store


def _commit_file(root: Path, rel: str, content: str, msg: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", rel], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", msg], check=True, capture_output=True)


# ── remote_url / set_remote ─────────────────────────────────────────────


class TestRemoteUrl:
    def test_none_before_set(self, tmp_path: Path, git_identity: None) -> None:
        store = _init_wiki(tmp_path / "wiki")
        assert store.remote_url() is None

    def test_returns_url_after_set(self, tmp_path: Path, git_identity: None) -> None:
        store = _init_wiki(tmp_path / "wiki")
        origin = _bare_origin(tmp_path / "origin.git")
        store.set_remote(str(origin))
        assert store.remote_url() == str(origin)

    def test_requires_existing_wiki(self, tmp_path: Path) -> None:
        store = WikiStore.at(tmp_path / "missing")
        with pytest.raises(WikiNotFoundError):
            store.remote_url()

    def test_oserror_normalized_to_runtimeerror(
        self, tmp_path: Path, git_identity: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A missing git binary (or vanished cwd) must surface as a RuntimeError,
        # not a raw OSError that escapes the CLI's `except RuntimeError`.
        import memtomem.wiki.store as store_module

        store = _init_wiki(tmp_path / "wiki")

        def _boom(*_a: object, **_k: object) -> object:
            raise FileNotFoundError("git")

        monkeypatch.setattr(store_module.subprocess, "run", _boom)
        with pytest.raises(RuntimeError):
            store.remote_url()

    def test_corrupt_config_raises_not_none(self, tmp_path: Path, git_identity: None) -> None:
        # A broken .git/config makes `git config --get` exit 128 — that is a real
        # failure and must surface, NOT be masked as "no remote configured"
        # (which only exit 1 == key-absent means).
        root = tmp_path / "wiki"
        store = _init_wiki(root)
        (root / ".git" / "config").write_text("not a valid git config\n", encoding="utf-8")
        with pytest.raises(RuntimeError):
            store.remote_url()


class TestSetRemote:
    def test_added_then_updated(self, tmp_path: Path, git_identity: None) -> None:
        store = _init_wiki(tmp_path / "wiki")
        first = _bare_origin(tmp_path / "first.git")
        second = _bare_origin(tmp_path / "second.git")
        assert store.set_remote(str(first)) == "added"
        assert store.set_remote(str(second)) == "updated"
        assert store.remote_url() == str(second)

    def test_requires_existing_wiki(self, tmp_path: Path) -> None:
        store = WikiStore.at(tmp_path / "missing")
        with pytest.raises(WikiNotFoundError):
            store.set_remote("file:///x")


# ── current_branch ──────────────────────────────────────────────────────


class TestCurrentBranch:
    def test_returns_main(self, tmp_path: Path, git_identity: None) -> None:
        store = _init_wiki(tmp_path / "wiki")
        assert store.current_branch() == "main"

    def test_raises_when_detached(self, tmp_path: Path, git_identity: None) -> None:
        root = tmp_path / "wiki"
        store = _init_wiki(root)
        head = store.current_commit()
        # Checking out the commit SHA directly detaches HEAD.
        subprocess.run(["git", "-C", str(root), "checkout", head], check=True, capture_output=True)
        with pytest.raises(WikiDetachedHeadError, match="detached HEAD"):
            store.current_branch()

    def test_oserror_normalized_to_runtimeerror(
        self, tmp_path: Path, git_identity: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import memtomem.wiki.store as store_module

        store = _init_wiki(tmp_path / "wiki")

        def _boom(*_a: object, **_k: object) -> object:
            raise FileNotFoundError("git")

        monkeypatch.setattr(store_module.subprocess, "run", _boom)
        with pytest.raises(RuntimeError):
            store.current_branch()

    def test_unborn_branch_returns_name(self, tmp_path: Path, git_identity: None) -> None:
        # Cloning an EMPTY remote leaves an unborn HEAD (no commits). The branch
        # name still resolves (symbolic-ref) — current_branch must NOT crash on
        # rev-parse of a missing HEAD; push then surfaces git's own refspec error.
        origin = _bare_origin(tmp_path / "origin.git")
        store = WikiStore.at(tmp_path / "wiki")
        store.init_from_url(str(origin))
        assert store.current_branch() == "main"


# ── push / pull round-trip ──────────────────────────────────────────────


class TestPushPullRoundTrip:
    def test_push_creates_branch_on_origin(self, tmp_path: Path, git_identity: None) -> None:
        store = _init_wiki(tmp_path / "wiki")
        origin = _bare_origin(tmp_path / "origin.git")
        store.set_remote(str(origin))
        store.push()
        # origin now has main pointing at the wiki HEAD.
        remote_head = _git(["ls-remote", str(origin), "main"], cwd=store.root).stdout.split()[0]
        assert remote_head == store.current_commit()

    def test_pull_fast_forwards_a_remote_commit(self, tmp_path: Path, git_identity: None) -> None:
        # wiki A seeds origin; wiki B clones it, adds a commit, pushes.
        a = _init_wiki(tmp_path / "a")
        origin = _bare_origin(tmp_path / "origin.git")
        a.set_remote(str(origin))
        a.push()

        b = WikiStore.at(tmp_path / "b")
        b.init_from_url(str(origin))
        _commit_file(b.root, "skills/code-review/SKILL.md", "# review\n", "add skill")
        b.push()

        # A pulls B's commit; the new file fast-forwards into A's tree.
        out = a.pull()
        assert (a.root / "skills" / "code-review" / "SKILL.md").read_text() == "# review\n"
        assert isinstance(out, str)


# ── error surfacing (thin pass-through) ─────────────────────────────────


class TestPushPullErrors:
    def test_push_without_remote_is_friendly(self, tmp_path: Path, git_identity: None) -> None:
        store = _init_wiki(tmp_path / "wiki")
        with pytest.raises(RuntimeError, match="mm wiki remote"):
            store.push()

    def test_pull_without_remote_is_friendly(self, tmp_path: Path, git_identity: None) -> None:
        store = _init_wiki(tmp_path / "wiki")
        with pytest.raises(RuntimeError, match="mm wiki remote"):
            store.pull()

    def test_push_non_fast_forward_surfaces_git_message(
        self, tmp_path: Path, git_identity: None
    ) -> None:
        a = _init_wiki(tmp_path / "a")
        origin = _bare_origin(tmp_path / "origin.git")
        a.set_remote(str(origin))
        a.push()

        b = WikiStore.at(tmp_path / "b")
        b.init_from_url(str(origin))
        _commit_file(b.root, "README.md", "from B\n", "B edit")
        b.push()

        # A diverges locally without pulling → its push is rejected by git.
        _commit_file(a.root, "README.md", "from A\n", "A edit")
        with pytest.raises(RuntimeError) as exc:
            a.push()
        msg = str(exc.value).lower()
        assert "rejected" in msg or "fast-forward" in msg or "fast forward" in msg

    def test_pull_merge_conflict_left_for_user(self, tmp_path: Path, git_identity: None) -> None:
        a = _init_wiki(tmp_path / "a")
        origin = _bare_origin(tmp_path / "origin.git")
        a.set_remote(str(origin))
        a.push()

        b = WikiStore.at(tmp_path / "b")
        b.init_from_url(str(origin))
        _commit_file(b.root, "README.md", "B line\n", "B edit")
        b.push()

        # A commits a conflicting change to the same file. Force a merge
        # strategy so the divergent pull actually merges (modern git otherwise
        # fatals "need to specify how to reconcile divergent branches" — itself
        # a faithfully-surfaced thin-pass-through error, just not a conflict).
        _commit_file(a.root, "README.md", "A line\n", "A edit")
        subprocess.run(
            ["git", "-C", str(a.root), "config", "pull.rebase", "false"],
            check=True,
            capture_output=True,
        )
        with pytest.raises(RuntimeError) as exc:
            a.pull()
        msg = str(exc.value).lower()
        assert "conflict" in msg or "merge" in msg
        # memtomem owns no resolution: the merge is left in progress for the user.
        assert a.is_dirty() is True

    def test_pull_dirty_tree_surfaces_git_message(self, tmp_path: Path, git_identity: None) -> None:
        a = _init_wiki(tmp_path / "a")
        origin = _bare_origin(tmp_path / "origin.git")
        a.set_remote(str(origin))
        a.push()

        b = WikiStore.at(tmp_path / "b")
        b.init_from_url(str(origin))
        _commit_file(b.root, "README.md", "from B\n", "B edit")
        b.push()

        # A has an uncommitted local edit to the same file git wants to update.
        (a.root / "README.md").write_text("uncommitted local\n", encoding="utf-8")
        with pytest.raises(RuntimeError):
            a.pull()

    def test_push_detached_head_raises(self, tmp_path: Path, git_identity: None) -> None:
        root = tmp_path / "wiki"
        store = _init_wiki(root)
        origin = _bare_origin(tmp_path / "origin.git")
        store.set_remote(str(origin))
        head = store.current_commit()
        subprocess.run(["git", "-C", str(root), "checkout", head], check=True, capture_output=True)
        with pytest.raises(WikiDetachedHeadError):
            store.push()

    def test_pull_detached_head_raises(self, tmp_path: Path, git_identity: None) -> None:
        root = tmp_path / "wiki"
        store = _init_wiki(root)
        origin = _bare_origin(tmp_path / "origin.git")
        store.set_remote(str(origin))
        head = store.current_commit()
        subprocess.run(["git", "-C", str(root), "checkout", head], check=True, capture_output=True)
        with pytest.raises(WikiDetachedHeadError):
            store.pull()

    def test_push_unborn_branch_surfaces_git_message(
        self, tmp_path: Path, git_identity: None
    ) -> None:
        # Clone of an empty remote → unborn HEAD, nothing to push. current_branch
        # resolves "main"; git push then fails with its own refspec error, which
        # is surfaced verbatim (thin pass-through) rather than crashing earlier.
        origin = _bare_origin(tmp_path / "origin.git")
        store = WikiStore.at(tmp_path / "wiki")
        store.init_from_url(str(origin))  # clone sets origin
        with pytest.raises(RuntimeError) as exc:
            store.push()
        assert "src refspec" in str(exc.value).lower() or "does not match" in str(exc.value).lower()

    def test_push_requires_existing_wiki(self, tmp_path: Path) -> None:
        store = WikiStore.at(tmp_path / "missing")
        with pytest.raises(WikiNotFoundError):
            store.push()

    def test_pull_requires_existing_wiki(self, tmp_path: Path) -> None:
        store = WikiStore.at(tmp_path / "missing")
        with pytest.raises(WikiNotFoundError):
            store.pull()


# ── credential redaction ────────────────────────────────────────────────


class TestRedactUrlUserinfo:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://user:pass@github.com/o/r.git", "https://github.com/o/r.git"),
            ("https://ghp_TOKEN@github.com/o/r.git", "https://github.com/o/r.git"),
            ("ssh://git@host/o/r.git", "ssh://host/o/r.git"),
            # @ inside the password (greedy strips up to the LAST @ in authority).
            ("https://us:p@ss@github.com/o/r.git", "https://github.com/o/r.git"),
            # scp-like SSH has no scheme + no password syntax → untouched.
            ("git@github.com:o/r.git", "git@github.com:o/r.git"),
            # no userinfo → untouched.
            ("https://github.com/o/r.git", "https://github.com/o/r.git"),
            ("file:///tmp/wiki", "file:///tmp/wiki"),
            # ``@`` in a query/fragment is NOT userinfo → must stay intact.
            ("https://example.com/p?email=a@b", "https://example.com/p?email=a@b"),
            ("https://user@example.com/p?x=a@b", "https://example.com/p?x=a@b"),
            ("https://us:p@ss@host?q=a@b", "https://host?q=a@b"),
        ],
    )
    def test_redacts(self, raw: str, expected: str) -> None:
        assert _redact_url_userinfo(raw) == expected

    def test_redacts_within_free_text(self) -> None:
        text = "fatal: unable to access 'https://tok@github.com/r.git/': Could not resolve host"
        out = _redact_url_userinfo(text)
        assert "tok@" not in out
        assert "https://github.com/r.git" in out

    def test_git_error_redacts_credential_url(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``_git`` boundary redacts credentials in BOTH the echoed argv and
        git's stderr before raising — so no caller can leak them."""
        import memtomem.wiki.store as store_module

        def _boom(*_args: object, **_kwargs: object) -> object:
            raise subprocess.CalledProcessError(
                returncode=128,
                cmd=["git", "push"],
                stderr="fatal: Authentication failed for 'https://user:secret@host/r.git'",
            )

        monkeypatch.setattr(store_module.subprocess, "run", _boom)
        with pytest.raises(RuntimeError) as exc:
            _git(["push", "https://user:secret@host/r.git", "main"], cwd=tmp_path)
        msg = str(exc.value)
        assert "secret" not in msg
        assert "host/r.git" in msg
