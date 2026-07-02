"""Tests for wiki/store.py — ``~/.memtomem-wiki/`` git repository abstraction."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

import pytest

from memtomem.wiki.store import (
    DEFAULT_WIKI_PATH,
    WIKI_ASSET_TYPES,
    CommitNotFoundError,
    WikiAlreadyExistsError,
    WikiHeadMovedError,
    WikiNotFoundError,
    WikiNothingToCommitError,
    WikiStore,
)


class TestDefaultPath:
    def test_default_path_is_home_relative(self) -> None:
        assert DEFAULT_WIKI_PATH == Path.home() / ".memtomem-wiki"

    def test_at_default_uses_env_override(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        target = tmp_path / "custom-wiki"
        monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(target))
        store = WikiStore.at_default()
        assert store.root == target

    def test_at_default_falls_back_to_home(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("MEMTOMEM_WIKI_PATH", raising=False)
        store = WikiStore.at_default()
        assert store.root == Path.home() / ".memtomem-wiki"


class TestInitScratch:
    def test_init_creates_layout(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        assert not store.exists()
        store.init()
        assert store.exists()
        for asset_type in WIKI_ASSET_TYPES:
            assert (wiki_root / asset_type).is_dir()
            assert (wiki_root / asset_type / ".gitkeep").is_file()
        assert (wiki_root / "README.md").is_file()
        assert (wiki_root / ".git").is_dir()

    def test_init_makes_initial_commit(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        result = subprocess.run(
            ["git", "log", "--oneline"],
            cwd=wiki_root,
            capture_output=True,
            text=True,
            check=True,
        )
        assert "Initialize memtomem wiki" in result.stdout

    def test_current_commit_after_init(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        sha = store.current_commit()
        assert len(sha) == 40
        assert all(c in "0123456789abcdef" for c in sha)

    def test_init_refuses_when_already_a_wiki(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        with pytest.raises(WikiAlreadyExistsError):
            store.init()

    def test_init_refuses_non_empty_directory(self, wiki_root: Path) -> None:
        wiki_root.mkdir(parents=True)
        (wiki_root / "stray.txt").write_text("hello", encoding="utf-8")
        store = WikiStore.at_default()
        with pytest.raises(WikiAlreadyExistsError, match="not empty"):
            store.init()

    def test_init_rolls_back_when_commit_has_no_identity(
        self, wiki_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #1385 finding 5: a bootstrap ``git commit`` with no resolvable identity
        # (minimal / rootless container) must NOT leave a wedged half-wiki — a
        # surviving ``.git/`` makes ``exists()`` return True (re-init refused) and
        # every read op fail on the HEAD-less repo. init() rolls back exactly what
        # it created and re-raises; NO fallback identity is injected (invariant).
        import os

        # Strip every identity source. ``user.useConfigOnly`` defeats git's
        # auto-derive-from-system fallback, so ``commit`` fails on every git build.
        for var in (
            "GIT_AUTHOR_NAME",
            "GIT_AUTHOR_EMAIL",
            "GIT_COMMITTER_NAME",
            "GIT_COMMITTER_EMAIL",
        ):
            monkeypatch.delenv(var, raising=False)
        no_identity = tmp_path / "no-identity.gitconfig"
        no_identity.write_text("[user]\n\tuseConfigOnly = true\n", encoding="utf-8")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(no_identity))
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)

        store = WikiStore.at_default()
        with pytest.raises(RuntimeError, match="git commit"):
            store.init()

        # Rollback removed everything init() created — no wedge left behind.
        assert not store.exists()
        assert not (wiki_root / ".git").exists()
        assert not (wiki_root / "README.md").exists()
        for asset_type in WIKI_ASSET_TYPES:
            assert not (wiki_root / asset_type).exists()

        # An env identity bypasses ``useConfigOnly``, so a retry now succeeds —
        # proving the directory was not left wedged.
        monkeypatch.setenv("GIT_AUTHOR_NAME", "test")
        monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
        monkeypatch.setenv("GIT_COMMITTER_NAME", "test")
        monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
        store.init()
        assert store.exists()


class TestInitFromUrl:
    def test_clone_from_local_file_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        git_identity: None,
    ) -> None:
        # Set up a source wiki at one path…
        source = tmp_path / "source-wiki"
        monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(source))
        WikiStore.at_default().init()

        # …clone from it into another path.
        target = tmp_path / "target-wiki"
        monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(target))
        clone = WikiStore.at_default()
        clone.init_from_url(f"file://{source}")

        assert clone.exists()
        for asset_type in WIKI_ASSET_TYPES:
            assert (target / asset_type).is_dir()
        # HEAD should match the source HEAD.
        source_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=source,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert clone.current_commit() == source_head

    def test_init_from_url_refuses_existing_wiki(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        with pytest.raises(WikiAlreadyExistsError):
            store.init_from_url("file:///nonexistent")

    def test_init_from_url_propagates_git_failure(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        with pytest.raises(RuntimeError, match="git clone"):
            store.init_from_url("file:///definitely/not/a/repo")


class TestListAssets:
    def test_list_empty_after_init(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        assert store.list_assets() == []

    def test_list_finds_assets(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        (wiki_root / "skills" / "code-review").mkdir()
        (wiki_root / "skills" / "code-review" / "SKILL.md").write_text("x", encoding="utf-8")
        (wiki_root / "agents" / "reviewer").mkdir()
        (wiki_root / "commands" / "lint").mkdir()

        assets = store.list_assets()
        names = [(a.type, a.name) for a in assets]
        assert ("skills", "code-review") in names
        assert ("agents", "reviewer") in names
        assert ("commands", "lint") in names
        assert len(assets) == 3

    def test_list_filters_by_type(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        (wiki_root / "skills" / "alpha").mkdir()
        (wiki_root / "agents" / "beta").mkdir()

        skills = store.list_assets("skills")
        assert [a.name for a in skills] == ["alpha"]

    def test_list_sorts_alphabetically(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        (wiki_root / "skills" / "zeta").mkdir()
        (wiki_root / "skills" / "alpha").mkdir()
        names = [a.name for a in store.list_assets("skills")]
        assert names == ["alpha", "zeta"]

    def test_list_skips_hidden_entries(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        # .gitkeep exists from init; should not appear.
        (wiki_root / "skills" / ".secret").mkdir()
        assert store.list_assets("skills") == []

    def test_list_rejects_unknown_type(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        with pytest.raises(ValueError, match="unknown asset type"):
            store.list_assets("widgets")

    def test_list_requires_existing_wiki(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        with pytest.raises(WikiNotFoundError):
            store.list_assets()


class TestRequireExists:
    def test_raises_when_absent(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        with pytest.raises(WikiNotFoundError, match="run `mm wiki init`"):
            store.require_exists()

    def test_silent_when_present(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        store.require_exists()  # no exception


class TestIsDirty:
    def test_clean_after_init(self, wiki_root: Path) -> None:
        """Fresh ``init`` leaves the working tree clean — no dirty marker."""
        store = WikiStore.at_default()
        store.init()
        assert store.is_dirty() is False

    def test_dirty_with_untracked_file(self, wiki_root: Path) -> None:
        """An untracked file inside the wiki flips ``is_dirty`` to True."""
        store = WikiStore.at_default()
        store.init()
        (wiki_root / "untracked_marker.txt").write_text("wip", encoding="utf-8")
        assert store.is_dirty() is True

    def test_dirty_with_modified_tracked_file(self, wiki_root: Path) -> None:
        """A modification to a tracked file flips ``is_dirty`` to True."""
        store = WikiStore.at_default()
        store.init()
        readme = wiki_root / "README.md"
        readme.write_text(readme.read_text(encoding="utf-8") + "\nlocal note\n", encoding="utf-8")
        assert store.is_dirty() is True

    def test_raises_when_wiki_absent(self, wiki_root: Path) -> None:
        """``is_dirty`` calls ``require_exists`` first, surfacing the
        usual ``WikiNotFoundError`` when the wiki itself is missing."""
        store = WikiStore.at_default()
        with pytest.raises(WikiNotFoundError):
            store.is_dirty()


# ── helpers for commit-bound tests ──────────────────────────────────────


def _seed_skill(
    wiki_root_path: Path,
    name: str,
    files: dict[str, bytes],
    *,
    force_add: bool = False,
) -> str:
    """Add ``skills/<name>/`` to wiki + commit. Returns the commit SHA.

    ``force_add`` uses ``git add -f`` so names a developer's global gitignore
    commonly covers (``*.bak``, ``__pycache__``) reliably land in the commit."""
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    for relpath, data in files.items():
        target = skill_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    add_cmd = ["git", "-C", str(wiki_root_path), "add"]
    if force_add:
        add_cmd.append("-f")
    add_cmd.append(".")
    subprocess.run(add_cmd, check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"add {name}"],
        check=True,
        capture_output=True,
    )
    return WikiStore.at_default().current_commit()


def _record_store_git_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Spy every argv ``wiki.store`` hands to ``subprocess.run`` (delegating to
    the real one) — pins which git objects ``copy_asset_at_commit`` reads."""
    import memtomem.wiki.store as store_module

    recorded: list[list[str]] = []
    real_run = subprocess.run

    def _recording_run(args: object, *pargs: object, **kwargs: object) -> object:
        if isinstance(args, list | tuple):
            recorded.append([str(a) for a in args])
        return real_run(args, *pargs, **kwargs)

    monkeypatch.setattr(store_module.subprocess, "run", _recording_run)
    return recorded


class TestCommitIsReachable:
    def test_head_is_reachable(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        head = store.current_commit()
        assert store.commit_is_reachable(head) is True

    def test_unknown_sha_is_not_reachable(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        assert store.commit_is_reachable("0" * 40) is False

    def test_empty_string_is_not_reachable(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        assert store.commit_is_reachable("") is False

    def test_symbolic_ref_is_not_reachable(self, wiki_root: Path) -> None:
        """``main`` resolves for ``git cat-file`` but is NOT a pin — refs
        move, so a hand-edited lockfile ref would let scanned bytes
        diverge from extracted bytes (#1247 Gate A review)."""
        store = WikiStore.at_default()
        store.init()
        assert store.commit_is_reachable("main") is False
        assert store.commit_is_reachable("HEAD") is False

    def test_abbreviated_sha_is_not_reachable(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        head = store.current_commit()
        assert store.commit_is_reachable(head[:12]) is False

    def test_raises_when_wiki_absent(self, wiki_root: Path) -> None:
        store = WikiStore.at_default()
        with pytest.raises(WikiNotFoundError):
            store.commit_is_reachable("0" * 40)


class TestCopyAssetAtCommit:
    def test_copies_files_at_pin(self, wiki_root: Path, tmp_path: Path) -> None:
        """Bytes at the pin land in dest, even after wiki HEAD advances."""
        store = WikiStore.at_default()
        store.init()
        old_pin = _seed_skill(wiki_root, "foo", {"SKILL.md": b"v1\n"})

        # Advance HEAD past the pin.
        (wiki_root / "skills" / "foo" / "SKILL.md").write_bytes(b"v2\n")
        subprocess.run(["git", "-C", str(wiki_root), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(wiki_root), "commit", "-m", "advance"],
            check=True,
            capture_output=True,
        )

        dest = tmp_path / "out" / "foo"
        digest_map = store.copy_asset_at_commit(old_pin, "skills", "foo", dest)

        # rel→digest map over the GIT-OBJECT bytes at the pin (#1247 id 15)
        # — exactly the bytes written to dest, not the v2 working tree/HEAD.
        assert digest_map == {"SKILL.md": hashlib.sha256(b"v1\n").hexdigest()}
        assert (dest / "SKILL.md").read_bytes() == b"v1\n"  # NOT the v2 HEAD

    def test_copies_nested_subdirs(self, wiki_root: Path, tmp_path: Path) -> None:
        """Per-file enumeration via ``ls-tree -r`` recovers nested layout."""
        store = WikiStore.at_default()
        store.init()
        pin = _seed_skill(
            wiki_root,
            "foo",
            {
                "SKILL.md": b"# foo\n",
                "scripts/run.sh": b"#!/bin/bash\necho hi\n",
                "references/a.md": b"a\n",
            },
        )

        dest = tmp_path / "foo"
        digest_map = store.copy_asset_at_commit(pin, "skills", "foo", dest)

        # Nested rels are POSIX, relative to dest (#1247 id 15).
        assert sorted(digest_map) == ["SKILL.md", "references/a.md", "scripts/run.sh"]
        assert digest_map["scripts/run.sh"] == hashlib.sha256(b"#!/bin/bash\necho hi\n").hexdigest()
        assert (dest / "SKILL.md").read_bytes() == b"# foo\n"
        assert (dest / "scripts" / "run.sh").read_bytes() == b"#!/bin/bash\necho hi\n"
        assert (dest / "references" / "a.md").read_bytes() == b"a\n"

    def test_unreachable_commit_raises(self, wiki_root: Path, tmp_path: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        with pytest.raises(CommitNotFoundError):
            store.copy_asset_at_commit("0" * 40, "skills", "foo", tmp_path / "foo")

    def test_asset_missing_at_pin_raises(self, wiki_root: Path, tmp_path: Path) -> None:
        """Pin reachable but the path didn't exist at that commit."""
        from memtomem.context.install import AssetNotFoundError

        store = WikiStore.at_default()
        store.init()
        head = store.current_commit()  # initial scaffold; no skills/foo yet
        with pytest.raises(AssetNotFoundError):
            store.copy_asset_at_commit(head, "skills", "foo", tmp_path / "foo")

    def test_dirty_wiki_does_not_bleed_through(self, wiki_root: Path, tmp_path: Path) -> None:
        """`git show <pin>:<path>` reads from objects, not the working tree."""
        store = WikiStore.at_default()
        store.init()
        pin = _seed_skill(wiki_root, "foo", {"SKILL.md": b"committed\n"})

        # Modify the working tree without committing.
        (wiki_root / "skills" / "foo" / "SKILL.md").write_bytes(b"uncommitted local\n")
        assert store.is_dirty() is True

        dest = tmp_path / "foo"
        store.copy_asset_at_commit(pin, "skills", "foo", dest)

        assert (dest / "SKILL.md").read_bytes() == b"committed\n"

    def test_pinned_bak_rel_never_read_or_materialized(
        self, wiki_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A wiki-shipped ``*.bak`` at the pin is filtered BEFORE ``git show`` —
        its bytes must never reach a tempdir inside the project tree (#1247)."""
        store = WikiStore.at_default()
        store.init()
        pin = _seed_skill(
            wiki_root,
            "foo",
            {
                "SKILL.md": b"# foo\n",
                "foo.md.bak": b"api_key=AKIA1234567890ABCDEF\n",
            },
            force_add=True,
        )
        # Guard: seeding really tracked the .bak (force-add beat any global
        # gitignore) — otherwise the spy assertions below pass vacuously.
        ls_result = subprocess.run(
            ["git", "-C", str(wiki_root), "ls-tree", "-r", "--name-only", pin],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "skills/foo/foo.md.bak" in ls_result.stdout.splitlines()

        recorded = _record_store_git_argv(monkeypatch)
        dest = tmp_path / "out" / "foo"
        digest_map = store.copy_asset_at_commit(pin, "skills", "foo", dest)

        show_args = [arg for argv in recorded if argv[:2] == ["git", "show"] for arg in argv]
        assert any(arg.endswith(":skills/foo/SKILL.md") for arg in show_args)  # spy is live
        assert not any("foo.md.bak" in arg for argv in recorded for arg in argv)
        assert sorted(digest_map) == ["SKILL.md"]  # skipped rel absent from the map too
        assert (dest / "SKILL.md").read_bytes() == b"# foo\n"
        assert not list(dest.rglob("*.bak"))

    def test_pinned_pycache_rel_never_read_or_materialized(
        self, wiki_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``COPY_SKIP_NAMES`` rels (``__pycache__``) get the same pre-``git
        show`` filter as ``.bak`` suffixes — one shared skip predicate (#1247)."""
        store = WikiStore.at_default()
        store.init()
        pin = _seed_skill(
            wiki_root,
            "foo",
            {
                "SKILL.md": b"# foo\n",
                "__pycache__/junk.pyc": b"\x00not real bytecode\n",
            },
            force_add=True,
        )
        ls_result = subprocess.run(
            ["git", "-C", str(wiki_root), "ls-tree", "-r", "--name-only", pin],
            check=True,
            capture_output=True,
            text=True,
        )
        assert "skills/foo/__pycache__/junk.pyc" in ls_result.stdout.splitlines()

        recorded = _record_store_git_argv(monkeypatch)
        dest = tmp_path / "out" / "foo"
        digest_map = store.copy_asset_at_commit(pin, "skills", "foo", dest)

        show_args = [arg for argv in recorded if argv[:2] == ["git", "show"] for arg in argv]
        assert any(arg.endswith(":skills/foo/SKILL.md") for arg in show_args)  # spy is live
        assert not any("__pycache__" in arg for argv in recorded for arg in argv)
        assert sorted(digest_map) == ["SKILL.md"]  # skipped rel absent from the map too
        assert (dest / "SKILL.md").read_bytes() == b"# foo\n"
        assert not (dest / "__pycache__").exists()


class TestAssetFilesNonAsciiPaths:
    """Non-ASCII (e.g. Korean) pathnames must survive ``ls-tree`` parsing.

    git's default ``core.quotePath=true`` C-quotes non-ASCII pathnames in
    line-oriented porcelain output (``"skills/\\355\\225\\234…"`` wrapped in
    double quotes), so a plain ``--name-only`` line parse fails the
    ``startswith`` prefix match and the file silently vanishes from
    extraction and the digest map — data loss on ``mm context install``."""

    def test_non_ascii_file_survives_extraction(self, wiki_root: Path, tmp_path: Path) -> None:
        store = WikiStore.at_default()
        store.init()
        pin = _seed_skill(
            wiki_root,
            "foo",
            {
                "SKILL.md": b"# foo\n",
                "references/설명.md": "# 설명\n".encode(),
            },
        )

        rels = store.asset_files_at_commit(pin, "skills", "foo")
        assert sorted(rels) == ["SKILL.md", "references/설명.md"]

        dest = tmp_path / "out" / "foo"
        digest_map = store.copy_asset_at_commit(pin, "skills", "foo", dest)
        assert sorted(digest_map) == ["SKILL.md", "references/설명.md"]
        assert (dest / "references" / "설명.md").read_bytes() == "# 설명\n".encode()

    def test_non_ascii_asset_name_is_found(self, wiki_root: Path) -> None:
        """An asset whose NAME is non-ASCII must not raise a spurious
        ``AssetNotFoundError`` (every quoted ls-tree line failed the prefix
        match, so the old parse saw zero files)."""
        store = WikiStore.at_default()
        store.init()
        pin = _seed_skill(wiki_root, "한글스킬", {"SKILL.md": b"# ko\n"})

        assert store.asset_files_at_commit(pin, "skills", "한글스킬") == ["SKILL.md"]


class TestCommitPaths:
    """``WikiStore.commit_paths`` — the isolated commit primitive (ADR-0027 §3)."""

    def _seed(self, wiki_root: Path) -> WikiStore:
        store = WikiStore.at_default()
        store.init()
        (wiki_root / "agents" / "beta").mkdir(parents=True)
        (wiki_root / "agents" / "beta" / "agent.md").write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(wiki_root), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(wiki_root), "commit", "-m", "seed"], check=True, capture_output=True
        )
        return store

    def _committed_files(self, wiki_root: Path, commit: str) -> list[str]:
        out = subprocess.run(
            ["git", "-C", str(wiki_root), "show", "--name-only", "--format=", commit],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        return out.split()

    def test_isolated_commit_excludes_unrelated_staged(self, wiki_root: Path) -> None:
        store = self._seed(wiki_root)
        head = store.current_commit()
        # an unrelated file staged in the real index must not be swept in
        (wiki_root / "unrelated.txt").write_text("u\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(wiki_root), "add", "unrelated.txt"],
            check=True,
            capture_output=True,
        )
        new = store.commit_paths(
            {"agents/beta/agent.md": b"v2\n"}, message="edit", expected_head=head
        )
        assert new != head
        assert self._committed_files(wiki_root, new) == ["agents/beta/agent.md"]
        # byte-exact + unrelated.txt still staged
        got = subprocess.run(
            ["git", "-C", str(wiki_root), "show", f"{new}:agents/beta/agent.md"],
            check=True,
            capture_output=True,
        ).stdout
        assert got == b"v2\n"
        staged = subprocess.run(
            ["git", "-C", str(wiki_root), "diff", "--cached", "--name-only"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "unrelated.txt" in staged

    def test_new_untracked_path_becomes_clean(self, wiki_root: Path) -> None:
        store = self._seed(wiki_root)
        head = store.current_commit()
        rel = "agents/beta/overrides/gemini.md"
        (wiki_root / "agents" / "beta" / "overrides").mkdir()
        (wiki_root / rel).write_text("ov\n", encoding="utf-8")
        new = store.commit_paths({rel: b"ov\n"}, message="add override", expected_head=head)
        assert self._committed_files(wiki_root, new) == [rel]
        # after the reconcile the path is clean (not staged-reverted, not untracked)
        status = subprocess.run(
            ["git", "-C", str(wiki_root), "status", "--porcelain", rel],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert status.strip() == ""

    def test_stale_expected_head_raises(self, wiki_root: Path) -> None:
        store = self._seed(wiki_root)
        head = store.current_commit()
        # advance HEAD so the passed expected_head is stale
        (wiki_root / "README.md").write_text("x\n", encoding="utf-8")
        subprocess.run(
            ["git", "-C", str(wiki_root), "commit", "-am", "adv"], check=True, capture_output=True
        )
        with pytest.raises(WikiHeadMovedError):
            store.commit_paths({"agents/beta/agent.md": b"v2\n"}, message="e", expected_head=head)

    def test_noop_when_bytes_match_head(self, wiki_root: Path) -> None:
        store = self._seed(wiki_root)
        head = store.current_commit()
        with pytest.raises(WikiNothingToCommitError):
            store.commit_paths({"agents/beta/agent.md": b"v1\n"}, message="e", expected_head=head)
        assert store.current_commit() == head  # no new commit

    def test_rejects_traversal_path(self, wiki_root: Path) -> None:
        store = self._seed(wiki_root)
        head = store.current_commit()
        with pytest.raises(ValueError):
            store.commit_paths({"../evil": b"x\n"}, message="e", expected_head=head)
