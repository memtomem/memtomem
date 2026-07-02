"""Shared pytest fixtures for wiki-related tests.

Imported by ``test_wiki_store.py``, ``test_wiki_cmd.py``, and
``test_context_install.py``. ``conftest.py`` already inserts
``packages/memtomem/tests/`` onto ``sys.path``, so a plain
``from _wiki_fixtures import git_identity, wiki_root`` works in every
test file. Mark such imports with ``# noqa: F401`` — pytest binds the
fixture into the test module's namespace.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set ``GIT_AUTHOR_*`` / ``GIT_COMMITTER_*`` so subprocess git commits
    succeed in tmp dirs that have no inherited ``.gitconfig``."""
    monkeypatch.setenv("GIT_AUTHOR_NAME", "test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


@pytest.fixture
def wiki_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    git_identity: None,
) -> Path:
    """Point ``WikiStore.at_default()`` at a tmp dir; bring the git identity along."""
    target = tmp_path / "wiki"
    monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(target))
    return target


@pytest.fixture
def unborn_wiki(wiki_root: Path, tmp_path: Path) -> Path:
    """A clone of an EMPTY bare remote: ``.git`` exists but HEAD is unborn.

    The state ``mm wiki init --from <url>`` leaves behind when the backup
    remote has no commits yet. One working-tree skill is seeded so listing
    surfaces have rows to render — the asset dirs exist, only HEAD is
    missing (``rev-parse HEAD`` has nothing to resolve).
    """
    import subprocess

    from memtomem.wiki.store import WikiStore

    remote = tmp_path / "empty-remote.git"
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    WikiStore.at_default().init_from_url(str(remote))
    skill = wiki_root / "skills" / "alpha"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_bytes(b"# Alpha\n")
    return wiki_root
