"""Path-classifier tests for ADR-0011 scope hierarchy.

Pins the contract for ``classify_scope`` at the path-pattern level — the
helper is the single source of truth that the indexer (``_resolve_scope``)
and the future read/write surfaces (PR-C / PR-D) consume to decide which
scope a file belongs to. Drift in this contract silently re-tags chunks
on the next reindex, so the matrix here is exhaustive across the three
scopes plus the registration-required guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.config import classify_scope


class TestUserScope:
    def test_arbitrary_path_classifies_as_user(self):
        scope, root = classify_scope("/tmp/foo/bar.md")
        assert scope == "user"
        assert root is None

    def test_user_memtomem_dir_classifies_as_user(self):
        # ~/.memtomem/memories is the user-tier default; classify_scope
        # only flips to project_* when the path lives under a *project*
        # ``<X>/.memtomem/...`` ancestor (with X != $HOME). Without the
        # registered-dirs filter the regex would match — verify the
        # filter prevents that misclassification when called by callers
        # that pass project_memory_dirs.
        path = "/Users/x/.memtomem/memories/note.md"
        scope, _root = classify_scope(path, project_memory_dirs=[])
        assert scope == "user"

    def test_no_memtomem_segment_classifies_as_user(self):
        scope, root = classify_scope("/Users/x/Documents/notes/foo.md")
        assert scope == "user"
        assert root is None


class TestProjectSharedScope:
    def test_project_shared_exact_dir(self, tmp_path):
        proj = tmp_path / "myproj"
        memdir = proj / ".memtomem" / "memories"
        memdir.mkdir(parents=True)
        f = memdir / "note.md"
        f.write_text("hi")
        scope, root = classify_scope(f, project_memory_dirs=[memdir])
        assert scope == "project_shared"
        assert root == proj

    def test_project_shared_nested_file(self, tmp_path):
        proj = tmp_path / "myproj"
        memdir = proj / ".memtomem" / "memories"
        nested = memdir / "subdir" / "deep.md"
        nested.parent.mkdir(parents=True)
        nested.write_text("hi")
        scope, root = classify_scope(nested, project_memory_dirs=[memdir])
        assert scope == "project_shared"
        assert root == proj


class TestProjectLocalScope:
    def test_project_local_exact_dir(self, tmp_path):
        proj = tmp_path / "myproj"
        memdir = proj / ".memtomem" / "memories.local"
        memdir.mkdir(parents=True)
        f = memdir / "draft.md"
        f.write_text("hi")
        scope, root = classify_scope(f, project_memory_dirs=[memdir])
        assert scope == "project_local"
        assert root == proj

    def test_project_local_pattern_specificity(self, tmp_path):
        # The regex tuple lists project_local *first* so ``memories.local``
        # is matched as a unit, not as ``memories`` followed by the
        # ``.local`` suffix. Without that ordering the project_shared
        # pattern would steal project_local files.
        proj = tmp_path / "myproj"
        local = proj / ".memtomem" / "memories.local"
        local.mkdir(parents=True)
        f = local / "x.md"
        f.write_text("hi")
        scope, _ = classify_scope(f, project_memory_dirs=[local])
        assert scope == "project_local"


class TestRegistrationGuard:
    def test_unregistered_project_path_falls_back_to_user(self, tmp_path):
        # A file under <X>/.memtomem/memories/ that the user has NOT
        # registered as a project_memory_dir must NOT be silently
        # classified as project_shared — that would let an unregistered
        # tree pollute project-scope filters.
        proj = tmp_path / "rogue"
        memdir = proj / ".memtomem" / "memories"
        memdir.mkdir(parents=True)
        f = memdir / "x.md"
        f.write_text("hi")
        scope, root = classify_scope(f, project_memory_dirs=[])  # NOT registered
        assert scope == "user"
        assert root is None

    def test_none_project_memory_dirs_skips_registration_guard(self, tmp_path):
        # ``None`` means "don't enforce registration" — used by callers
        # that already gate on the config field separately. This is the
        # only way to classify a path that pattern-matches but is not in
        # any explicit registry.
        proj = tmp_path / "anywhere"
        memdir = proj / ".memtomem" / "memories"
        memdir.mkdir(parents=True)
        f = memdir / "x.md"
        f.write_text("hi")
        scope, root = classify_scope(f, project_memory_dirs=None)
        assert scope == "project_shared"
        assert root == proj


class TestEdgeCases:
    @pytest.mark.parametrize(
        "windows_path",
        [
            r"C:\projects\myproj\.memtomem\memories\file.md",
            r"C:\projects\myproj\.memtomem\memories.local\file.md",
        ],
    )
    def test_windows_separators_match(self, windows_path):
        # The classifier normalises separators before pattern match so
        # POSIX, Windows, UNC, and mixed-separator strings hit the same
        # patterns (mirrors ``categorize_memory_dir`` policy). With
        # ``project_memory_dirs=None`` (skip registration) we just verify
        # the regex side handles Windows separators.
        scope, _ = classify_scope(windows_path, project_memory_dirs=None)
        assert scope in ("project_shared", "project_local")

    def test_memtomem_at_root_classifies_as_user(self):
        # ``/.memtomem/memories/x`` has empty project_root (the regex
        # match starts at index 0), which the classifier treats as
        # invalid — falls back to user scope rather than producing a
        # bogus empty project_root path.
        scope, root = classify_scope("/.memtomem/memories/x.md", project_memory_dirs=None)
        assert scope == "user"
        assert root is None
