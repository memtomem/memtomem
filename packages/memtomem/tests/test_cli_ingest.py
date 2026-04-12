"""Tests for ``mm ingest claude-memory`` (Phase B: one-way Claude ingestion).

Split into two layers:

* **Unit** — pure functions (_discover_files / _derive_slug / _build_namespace /
  _tags_for_file). No fixtures, fast, always runs in CI.
* **Integration** — full index_engine + storage loop via ``components``
  fixture; marked ``@pytest.mark.ollama`` because indexing calls embedders.
  Tests that ingest is read-only (source files untouched), delta re-runs
  skip unchanged content, edits are picked up, and namespace/tags land on
  the real chunks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.cli.ingest_cmd import (
    _NAMESPACE_PREFIX,
    _build_namespace,
    _derive_slug,
    _discover_files,
    _ingest_files_with_components,
    _tags_for_file,
)


# ── Unit tests ───────────────────────────────────────────────────────


class TestDiscoverFiles:
    def test_returns_sorted_markdown_files(self, tmp_path):
        (tmp_path / "project_b.md").write_text("b")
        (tmp_path / "feedback_a.md").write_text("a")
        (tmp_path / "user_c.md").write_text("c")

        files = _discover_files(tmp_path)
        assert [f.name for f in files] == [
            "feedback_a.md",
            "project_b.md",
            "user_c.md",
        ]

    def test_excludes_memory_md_and_readme(self, tmp_path):
        """MEMORY.md and README.md are indexes / docs, not memory content."""
        (tmp_path / "feedback_a.md").write_text("keep")
        (tmp_path / "MEMORY.md").write_text("- [a](feedback_a.md)")
        (tmp_path / "README.md").write_text("# how to read")

        files = _discover_files(tmp_path)
        names = [f.name for f in files]
        assert names == ["feedback_a.md"]

    def test_excludes_hidden_and_non_markdown(self, tmp_path):
        (tmp_path / "project_a.md").write_text("keep")
        (tmp_path / ".DS_Store").write_text("mac")
        (tmp_path / ".hidden.md").write_text("hidden")
        (tmp_path / "notes.txt").write_text("wrong ext")
        (tmp_path / "script.py").write_text("code")

        files = _discover_files(tmp_path)
        assert [f.name for f in files] == ["project_a.md"]

    def test_non_recursive(self, tmp_path):
        """Claude memory dirs are flat — don't walk subdirectories."""
        (tmp_path / "project_a.md").write_text("top")
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "project_b.md").write_text("nested")

        files = _discover_files(tmp_path)
        assert [f.name for f in files] == ["project_a.md"]

    def test_empty_directory(self, tmp_path):
        assert _discover_files(tmp_path) == []


class TestDeriveSlug:
    def test_memory_subdir_returns_parent_name(self, tmp_path):
        """Canonical ~/.claude/projects/<slug>/memory/ layout."""
        slug_dir = tmp_path / "-Users-me-Work-foo" / "memory"
        slug_dir.mkdir(parents=True)
        assert _derive_slug(slug_dir) == "-Users-me-Work-foo"

    def test_non_memory_leaf_falls_back_to_leaf_name(self, tmp_path):
        """When the user points at a non-canonical path, at least stay
        deterministic — slug is the leaf directory name."""
        leaf = tmp_path / "custom-project-notes"
        leaf.mkdir()
        assert _derive_slug(leaf) == "custom-project-notes"

    def test_empty_name_defaults(self, tmp_path):
        """Guard against a bare root path producing an empty slug."""
        # Path("/") has name == ""; _derive_slug must degrade gracefully.
        assert _derive_slug(Path("/")) == "default"


class TestBuildNamespace:
    def test_simple_slug_passes_through(self):
        assert _build_namespace("my-project") == f"{_NAMESPACE_PREFIX}my-project"

    def test_real_claude_slug_passes_through(self):
        """Real Claude project slugs start with '-' and use hyphens as
        the flattened path separator — must stay intact."""
        slug = "-Users-me-Work-agent-harness-memtomem"
        assert _build_namespace(slug) == f"{_NAMESPACE_PREFIX}{slug}"

    def test_unsafe_chars_replaced_with_underscore(self):
        """Anything outside _NS_NAME_RE gets sanitized so storage accepts it."""
        ns = _build_namespace("weird/slug$with!chars")
        assert ns == f"{_NAMESPACE_PREFIX}weird_slug_with_chars"

    def test_safe_punctuation_kept(self):
        """Word chars, dot, colon, @, hyphen, underscore, space are allowed."""
        ns = _build_namespace("ok.slug_1:v2@host")
        assert ns == f"{_NAMESPACE_PREFIX}ok.slug_1:v2@host"


class TestTagsForFile:
    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("feedback_docs_as_tests.md", {"claude-memory", "feedback"}),
            ("project_ltm_manager_roadmap.md", {"claude-memory", "project"}),
            ("user_language.md", {"claude-memory", "user"}),
            ("reference_memtomem_ssh.md", {"claude-memory", "reference"}),
        ],
    )
    def test_known_prefixes_get_type_tag(self, filename, expected):
        assert _tags_for_file(Path(filename)) == expected

    def test_unknown_prefix_still_gets_claude_memory_tag(self):
        """Files without a recognized prefix are still ingested under the
        ``claude-memory`` source tag — just without a type classifier."""
        assert _tags_for_file(Path("mystery.md")) == {"claude-memory"}

    def test_substring_does_not_match_prefix(self):
        """``feedbackXYZ.md`` starts with 'feedback' but is not a feedback
        note — the trailing underscore guard in _TAG_PREFIXES enforces this."""
        assert _tags_for_file(Path("feedbackXYZ.md")) == {"claude-memory"}


# ── Integration tests ────────────────────────────────────────────────


@pytest.mark.ollama
class TestIngestFilesWithComponents:
    """End-to-end coverage via the real index_engine + storage.

    Uses the ``components`` fixture from conftest.py — same pattern as the
    other ``_mem_*_core`` integration tests in test_tools_logic.py.
    """

    async def _make_claude_memory_dir(self, tmp_path: Path) -> Path:
        """Build a fake ~/.claude/projects/<slug>/memory/ layout outside
        the configured memtomem memory_dirs, so we exercise the
        read-only ingestion path (no file copy)."""
        claude_root = tmp_path / "fake_home" / ".claude" / "projects"
        slug_dir = claude_root / "-Users-test-Work-demo-project" / "memory"
        slug_dir.mkdir(parents=True)

        (slug_dir / "feedback_a.md").write_text(
            "# Feedback A\n\nAlways use bge-m3 for Korean text embeddings "
            "because the vocabulary coverage is wider than bge-small.\n"
        )
        (slug_dir / "project_b.md").write_text(
            "# Project B\n\nPhase B adds a claude-memory ingestion path "
            "that treats the source directory as a read-only snapshot.\n"
        )
        # MEMORY.md must be ignored even though it lives in the same dir.
        (slug_dir / "MEMORY.md").write_text(
            "- [Feedback A](feedback_a.md)\n- [Project B](project_b.md)\n"
        )
        return slug_dir

    async def test_happy_path_indexes_with_namespace_and_tags(self, components, tmp_path):
        slug_dir = await self._make_claude_memory_dir(tmp_path)
        files = _discover_files(slug_dir)
        # Sanity: discovery already drops MEMORY.md.
        assert {f.name for f in files} == {"feedback_a.md", "project_b.md"}

        summary = await _ingest_files_with_components(
            components,
            files,
            namespace="claude-memory:-Users-test-Work-demo-project",
        )

        assert summary.indexed >= 2, summary
        assert summary.errors == ()

        # Every chunk lives under the expected namespace and has
        # both the source tag and its per-file type tag.
        for f in files:
            chunks = await components.storage.list_chunks_by_source(f)
            assert chunks, f"no chunks indexed for {f.name}"
            for c in chunks:
                assert c.metadata.namespace == ("claude-memory:-Users-test-Work-demo-project")
                assert "claude-memory" in c.metadata.tags
                if f.name.startswith("feedback_"):
                    assert "feedback" in c.metadata.tags
                elif f.name.startswith("project_"):
                    assert "project" in c.metadata.tags

    async def test_ingest_is_read_only_source_files_untouched(self, components, tmp_path):
        """After ingestion the source files must still exist at their
        original absolute path and have unchanged content — no copy, no
        move, no rewrite."""
        slug_dir = await self._make_claude_memory_dir(tmp_path)
        files = _discover_files(slug_dir)
        original_bytes = {f: f.read_bytes() for f in files}

        await _ingest_files_with_components(
            components,
            files,
            namespace="claude-memory:-Users-test-Work-demo-project",
        )

        for f, data in original_bytes.items():
            assert f.exists(), f"{f} disappeared after ingest"
            assert f.read_bytes() == data, f"{f} content was mutated"

        # And chunk source_file must point at the original absolute path,
        # not some copy under memtomem's memory_dirs.
        for f in files:
            chunks = await components.storage.list_chunks_by_source(f)
            assert chunks
            for c in chunks:
                assert Path(c.metadata.source_file).resolve() == f.resolve()

    async def test_rerun_skips_unchanged_files(self, components, tmp_path):
        """Content-hash delta: a second identical ingest indexes 0 new
        chunks and marks the existing ones as skipped."""
        slug_dir = await self._make_claude_memory_dir(tmp_path)
        files = _discover_files(slug_dir)

        first = await _ingest_files_with_components(
            components,
            files,
            namespace="claude-memory:-Users-test-Work-demo-project",
        )
        assert first.indexed >= 2

        second = await _ingest_files_with_components(
            components,
            files,
            namespace="claude-memory:-Users-test-Work-demo-project",
        )
        assert second.indexed == 0, second
        assert second.skipped >= 2, second
        assert second.errors == ()

    async def test_edited_file_is_reindexed_on_rerun(self, components, tmp_path):
        """When a single file's content changes, the next ingest
        re-indexes that file while leaving the others on skip."""
        slug_dir = await self._make_claude_memory_dir(tmp_path)
        files = _discover_files(slug_dir)

        await _ingest_files_with_components(
            components,
            files,
            namespace="claude-memory:-Users-test-Work-demo-project",
        )

        edited = slug_dir / "feedback_a.md"
        edited.write_text(
            "# Feedback A\n\nRevised guidance: prefer bge-m3 for multilingual "
            "corpora — the Korean coverage is measurably better than bge-small, "
            "and the English quality is comparable.\n"
        )

        summary = await _ingest_files_with_components(
            components,
            files,
            namespace="claude-memory:-Users-test-Work-demo-project",
        )
        # At least one chunk from feedback_a.md should be re-upserted; the
        # untouched project_b.md should show up as skipped.
        assert summary.indexed >= 1, summary
        assert summary.skipped >= 1, summary
