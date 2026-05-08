"""Tests for wiki/store.py — ``~/.memtomem-wiki/`` git repository abstraction."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from memtomem.wiki.store import (
    DEFAULT_WIKI_PATH,
    WIKI_ASSET_TYPES,
    CommitNotFoundError,
    WikiAlreadyExistsError,
    WikiNotFoundError,
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


class TestInitFromUrl:
    def test_clone_from_local_file_url(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        git_identity: None,  # noqa: ARG002
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


def _seed_skill(wiki_root_path: Path, name: str, files: dict[str, bytes]) -> str:
    """Add ``skills/<name>/`` to wiki + commit. Returns the commit SHA."""
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    for relpath, data in files.items():
        target = skill_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"add {name}"],
        check=True,
        capture_output=True,
    )
    return WikiStore.at_default().current_commit()


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
        files_written = store.copy_asset_at_commit(old_pin, "skills", "foo", dest)

        assert files_written == 1
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
        files_written = store.copy_asset_at_commit(pin, "skills", "foo", dest)

        assert files_written == 3
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
