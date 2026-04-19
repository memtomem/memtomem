"""Tests for ``mm ingest`` subcommands (claude-memory, gemini-memory, codex-memory).

Split into two layers per source:

* **Unit** — pure functions (discover / slug / namespace / tags). No
  fixtures, fast, always runs in CI.
* **Integration** — full index_engine + storage loop via ``components``
  fixture; marked ``@pytest.mark.ollama`` because indexing calls embedders.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.cli.ingest_cmd import (
    _CODEX_NAMESPACE_PREFIX,
    _GEMINI_NAMESPACE_PREFIX,
    _NAMESPACE_PREFIX,
    _build_namespace,
    _codex_derive_slug,
    _codex_discover_files,
    _codex_tags_for_file,
    _derive_slug,
    _discover_claude_slug_dirs,
    _discover_files,
    _gemini_derive_slug,
    _gemini_discover_files,
    _gemini_tags_for_file,
    _ingest_files_with_components,
    _tags_for_file,
)


# ── Unit tests ───────────────────────────────────────────────────────


class TestDiscoverFiles:
    def test_returns_sorted_markdown_files(self, tmp_path):
        (tmp_path / "project_b.md").write_text("b", encoding="utf-8")
        (tmp_path / "feedback_a.md").write_text("a", encoding="utf-8")
        (tmp_path / "user_c.md").write_text("c", encoding="utf-8")

        files = _discover_files(tmp_path)
        assert [f.name for f in files] == [
            "feedback_a.md",
            "project_b.md",
            "user_c.md",
        ]

    def test_excludes_memory_md_and_readme(self, tmp_path):
        """MEMORY.md and README.md are indexes / docs, not memory content."""
        (tmp_path / "feedback_a.md").write_text("keep", encoding="utf-8")
        (tmp_path / "MEMORY.md").write_text("- [a](feedback_a.md)", encoding="utf-8")
        (tmp_path / "README.md").write_text("# how to read", encoding="utf-8")

        files = _discover_files(tmp_path)
        names = [f.name for f in files]
        assert names == ["feedback_a.md"]

    def test_excludes_hidden_and_non_markdown(self, tmp_path):
        (tmp_path / "project_a.md").write_text("keep", encoding="utf-8")
        (tmp_path / ".DS_Store").write_text("mac", encoding="utf-8")
        (tmp_path / ".hidden.md").write_text("hidden", encoding="utf-8")
        (tmp_path / "notes.txt").write_text("wrong ext", encoding="utf-8")
        (tmp_path / "script.py").write_text("code", encoding="utf-8")

        files = _discover_files(tmp_path)
        assert [f.name for f in files] == ["project_a.md"]

    def test_non_recursive(self, tmp_path):
        """Claude memory dirs are flat — don't walk subdirectories."""
        (tmp_path / "project_a.md").write_text("top", encoding="utf-8")
        sub = tmp_path / "subdir"
        sub.mkdir()
        (sub / "project_b.md").write_text("nested", encoding="utf-8")

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


class TestDiscoverClaudeSlugDirs:
    """Unit tests for multi-slug discovery."""

    def test_finds_slug_memory_dirs(self, tmp_path):
        """Standard ~/.claude/projects/ layout with two slugs."""
        for slug in ("-Users-me-Work-alpha", "-Users-me-Work-beta"):
            mem = tmp_path / slug / "memory"
            mem.mkdir(parents=True)
            (mem / "project_x.md").write_text("x", encoding="utf-8")

        dirs = _discover_claude_slug_dirs(tmp_path)
        assert len(dirs) == 2
        assert all(d.name == "memory" for d in dirs)
        slugs = sorted(d.parent.name for d in dirs)
        assert slugs == ["-Users-me-Work-alpha", "-Users-me-Work-beta"]

    def test_skips_dirs_without_memory_subdir(self, tmp_path):
        """Slug directories without a memory/ child are ignored."""
        good = tmp_path / "has-memory" / "memory"
        good.mkdir(parents=True)
        (good / "note.md").write_text("ok", encoding="utf-8")

        bad = tmp_path / "no-memory"
        bad.mkdir()
        (bad / "stray.md").write_text("stray", encoding="utf-8")

        dirs = _discover_claude_slug_dirs(tmp_path)
        assert len(dirs) == 1
        assert dirs[0].parent.name == "has-memory"

    def test_skips_hidden_dirs(self, tmp_path):
        hidden = tmp_path / ".hidden" / "memory"
        hidden.mkdir(parents=True)
        (hidden / "secret.md").write_text("hidden", encoding="utf-8")

        assert _discover_claude_slug_dirs(tmp_path) == []

    def test_sorted_output(self, tmp_path):
        for name in ("charlie", "alpha", "bravo"):
            mem = tmp_path / name / "memory"
            mem.mkdir(parents=True)
        dirs = _discover_claude_slug_dirs(tmp_path)
        assert [d.parent.name for d in dirs] == ["alpha", "bravo", "charlie"]

    def test_empty_parent(self, tmp_path):
        assert _discover_claude_slug_dirs(tmp_path) == []

    def test_file_not_dir(self, tmp_path):
        f = tmp_path / "not-a-dir.txt"
        f.write_text("file", encoding="utf-8")
        assert _discover_claude_slug_dirs(f) == []


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
            "because the vocabulary coverage is wider than bge-small.\n",
            encoding="utf-8",
        )
        (slug_dir / "project_b.md").write_text(
            "# Project B\n\nPhase B adds a claude-memory ingestion path "
            "that treats the source directory as a read-only snapshot.\n",
            encoding="utf-8",
        )
        # MEMORY.md must be ignored even though it lives in the same dir.
        (slug_dir / "MEMORY.md").write_text(
            "- [Feedback A](feedback_a.md)\n- [Project B](project_b.md)\n", encoding="utf-8"
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
            "and the English quality is comparable.\n",
            encoding="utf-8",
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


# =====================================================================
# Gemini memory unit tests
# =====================================================================


class TestGeminiDiscoverFiles:
    def test_file_path_returns_single_element(self, tmp_path):
        md = tmp_path / "GEMINI.md"
        md.write_text("## Gemini Added Memories\n\n- fact one\n", encoding="utf-8")
        assert _gemini_discover_files(md) == [md]

    def test_directory_finds_gemini_md(self, tmp_path):
        md = tmp_path / "GEMINI.md"
        md.write_text("memories", encoding="utf-8")
        files = _gemini_discover_files(tmp_path)
        assert files == [md]

    def test_directory_without_gemini_md_returns_empty(self, tmp_path):
        (tmp_path / "other.md").write_text("not gemini", encoding="utf-8")
        assert _gemini_discover_files(tmp_path) == []

    def test_non_md_file_returns_empty(self, tmp_path):
        txt = tmp_path / "GEMINI.txt"
        txt.write_text("wrong extension", encoding="utf-8")
        assert _gemini_discover_files(txt) == []

    def test_empty_file_still_discovered(self, tmp_path):
        md = tmp_path / "GEMINI.md"
        md.write_text("", encoding="utf-8")
        assert _gemini_discover_files(md) == [md]


class TestGeminiDeriveSlug:
    def test_global_gemini_dir(self, tmp_path):
        gemini_dir = tmp_path / ".gemini"
        gemini_dir.mkdir()
        md = gemini_dir / "GEMINI.md"
        md.write_text("", encoding="utf-8")
        assert _gemini_derive_slug(md) == "global"

    def test_project_root(self, tmp_path):
        project = tmp_path / "my-cool-project"
        project.mkdir()
        md = project / "GEMINI.md"
        md.write_text("", encoding="utf-8")
        assert _gemini_derive_slug(md) == "my-cool-project"

    def test_root_path_defaults(self):
        assert _gemini_derive_slug(Path("/GEMINI.md")) == "global"


class TestGeminiNamespace:
    def test_builds_with_gemini_prefix(self):
        ns = _build_namespace("my-project", prefix=_GEMINI_NAMESPACE_PREFIX)
        assert ns == "gemini-memory:my-project"

    def test_sanitizes_unsafe_chars(self):
        ns = _build_namespace("weird/path$here", prefix=_GEMINI_NAMESPACE_PREFIX)
        assert ns == "gemini-memory:weird_path_here"


class TestGeminiTagsForFile:
    def test_always_returns_gemini_memory(self):
        assert _gemini_tags_for_file(Path("GEMINI.md")) == {"gemini-memory"}

    def test_no_type_tags_regardless_of_name(self):
        assert _gemini_tags_for_file(Path("feedback_a.md")) == {"gemini-memory"}


# =====================================================================
# Codex memory unit tests
# =====================================================================


class TestCodexDiscoverFiles:
    def test_returns_sorted_md_files(self, tmp_path):
        (tmp_path / "note_b.md").write_text("b", encoding="utf-8")
        (tmp_path / "fact_a.md").write_text("a", encoding="utf-8")
        files = _codex_discover_files(tmp_path)
        assert [f.name for f in files] == ["fact_a.md", "note_b.md"]

    def test_excludes_readme(self, tmp_path):
        (tmp_path / "fact.md").write_text("keep", encoding="utf-8")
        (tmp_path / "README.md").write_text("docs", encoding="utf-8")
        files = _codex_discover_files(tmp_path)
        assert [f.name for f in files] == ["fact.md"]

    def test_excludes_hidden_and_non_markdown(self, tmp_path):
        (tmp_path / "fact.md").write_text("keep", encoding="utf-8")
        (tmp_path / ".hidden.md").write_text("skip", encoding="utf-8")
        (tmp_path / "data.json").write_text("{}", encoding="utf-8")
        files = _codex_discover_files(tmp_path)
        assert [f.name for f in files] == ["fact.md"]

    def test_non_recursive(self, tmp_path):
        (tmp_path / "fact.md").write_text("top", encoding="utf-8")
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "nested.md").write_text("nested", encoding="utf-8")
        files = _codex_discover_files(tmp_path)
        assert [f.name for f in files] == ["fact.md"]

    def test_empty_directory(self, tmp_path):
        assert _codex_discover_files(tmp_path) == []


class TestCodexDeriveSlug:
    def test_memories_under_codex(self, tmp_path):
        """~/.codex/memories/ → global."""
        codex = tmp_path / ".codex" / "memories"
        codex.mkdir(parents=True)
        assert _codex_derive_slug(codex) == "global"

    def test_custom_directory(self, tmp_path):
        custom = tmp_path / "my-codex-notes"
        custom.mkdir()
        assert _codex_derive_slug(custom) == "my-codex-notes"

    def test_memories_under_custom_parent(self, tmp_path):
        """<parent>/memories/ where parent is not .codex."""
        parent = tmp_path / "workspace-a" / "memories"
        parent.mkdir(parents=True)
        assert _codex_derive_slug(parent) == "workspace-a"

    def test_root_path_defaults(self):
        assert _codex_derive_slug(Path("/")) == "global"


class TestCodexNamespace:
    def test_builds_with_codex_prefix(self):
        ns = _build_namespace("global", prefix=_CODEX_NAMESPACE_PREFIX)
        assert ns == "codex-memory:global"

    def test_sanitizes_unsafe_chars(self):
        ns = _build_namespace("has/slash", prefix=_CODEX_NAMESPACE_PREFIX)
        assert ns == "codex-memory:has_slash"


class TestCodexTagsForFile:
    def test_always_returns_codex_memory(self):
        assert _codex_tags_for_file(Path("fact.md")) == {"codex-memory"}

    def test_no_type_tags(self):
        assert _codex_tags_for_file(Path("project_x.md")) == {"codex-memory"}


# ── Integration tests (Gemini) ──────────────────────────────────────


@pytest.mark.ollama
class TestGeminiIngestIntegration:
    async def test_happy_path_indexes_gemini_md(self, components, tmp_path):
        project = tmp_path / "demo-project"
        project.mkdir()
        md = project / "GEMINI.md"
        md.write_text(
            "## Gemini Added Memories\n\n"
            "- User prefers Korean for discussion but English for code.\n"
            "- The project uses uv for dependency management.\n"
            "- Always run ruff check before committing.\n",
            encoding="utf-8",
        )

        files = _gemini_discover_files(md)
        assert files == [md]

        summary = await _ingest_files_with_components(
            components,
            files,
            namespace="gemini-memory:demo-project",
            tag_fn=_gemini_tags_for_file,
        )
        assert summary.indexed >= 1, summary
        assert summary.errors == ()

        chunks = await components.storage.list_chunks_by_source(md)
        assert chunks
        for c in chunks:
            assert c.metadata.namespace == "gemini-memory:demo-project"
            assert "gemini-memory" in c.metadata.tags

    async def test_rerun_skips_unchanged(self, components, tmp_path):
        project = tmp_path / "demo"
        project.mkdir()
        md = project / "GEMINI.md"
        md.write_text("## Memories\n\n- Fact alpha\n- Fact beta\n", encoding="utf-8")

        files = _gemini_discover_files(md)
        ns = "gemini-memory:demo"

        first = await _ingest_files_with_components(
            components, files, ns, tag_fn=_gemini_tags_for_file
        )
        assert first.indexed >= 1

        second = await _ingest_files_with_components(
            components, files, ns, tag_fn=_gemini_tags_for_file
        )
        assert second.indexed == 0
        assert second.skipped >= 1


# ── Integration tests (Codex) ───────────────────────────────────────


@pytest.mark.ollama
class TestCodexIngestIntegration:
    async def test_happy_path_indexes_codex_memories(self, components, tmp_path):
        mem_dir = tmp_path / "codex_memories"
        mem_dir.mkdir()
        (mem_dir / "fact_a.md").write_text(
            "# Workspace preference\n\nAlways use workspace-write sandbox mode.\n", encoding="utf-8"
        )
        (mem_dir / "fact_b.md").write_text(
            "# Git convention\n\nCommit messages should start with a verb.\n", encoding="utf-8"
        )
        (mem_dir / "README.md").write_text("# Index\nDo not index this.\n", encoding="utf-8")

        files = _codex_discover_files(mem_dir)
        assert {f.name for f in files} == {"fact_a.md", "fact_b.md"}

        summary = await _ingest_files_with_components(
            components,
            files,
            namespace="codex-memory:global",
            tag_fn=_codex_tags_for_file,
        )
        assert summary.indexed >= 2, summary
        assert summary.errors == ()

        for f in files:
            chunks = await components.storage.list_chunks_by_source(f)
            assert chunks, f"no chunks for {f.name}"
            for c in chunks:
                assert c.metadata.namespace == "codex-memory:global"
                assert "codex-memory" in c.metadata.tags

    async def test_rerun_skips_unchanged(self, components, tmp_path):
        mem_dir = tmp_path / "codex_memories"
        mem_dir.mkdir()
        (mem_dir / "fact.md").write_text("# Fact\n\nSome important fact.\n", encoding="utf-8")

        files = _codex_discover_files(mem_dir)
        ns = "codex-memory:global"

        first = await _ingest_files_with_components(
            components, files, ns, tag_fn=_codex_tags_for_file
        )
        assert first.indexed >= 1

        second = await _ingest_files_with_components(
            components, files, ns, tag_fn=_codex_tags_for_file
        )
        assert second.indexed == 0
        assert second.skipped >= 1


# ── Multi-slug integration tests ──────────────────────────────────────


@pytest.mark.ollama
class TestMultiSlugIngestIntegration:
    """End-to-end multi-slug discovery + ingest."""

    @staticmethod
    def _make_projects_dir(tmp_path: Path) -> Path:
        """Build a fake ~/.claude/projects/ layout with two slugs."""
        projects = tmp_path / "fake_home" / ".claude" / "projects"
        for slug in ("-Users-test-Work-alpha", "-Users-test-Work-beta"):
            mem = projects / slug / "memory"
            mem.mkdir(parents=True)
            (mem / "feedback_x.md").write_text(
                f"# Feedback\n\nAlways lint before commit in {slug}.\n", encoding="utf-8"
            )
            (mem / "project_y.md").write_text(
                f"# Project\n\nPhase 1 for {slug} is complete.\n", encoding="utf-8"
            )
            (mem / "MEMORY.md").write_text("- [x](feedback_x.md)\n", encoding="utf-8")
        return projects

    async def test_multi_slug_indexes_all_slugs(self, components, tmp_path):
        projects = self._make_projects_dir(tmp_path)
        slug_dirs = _discover_claude_slug_dirs(projects)
        assert len(slug_dirs) == 2

        for mem_dir in slug_dirs:
            slug = _derive_slug(mem_dir)
            ns = _build_namespace(slug)
            files = _discover_files(mem_dir)
            assert len(files) == 2  # feedback_x.md + project_y.md

            summary = await _ingest_files_with_components(components, files, ns)
            assert summary.indexed >= 2
            assert summary.errors == ()

            for f in files:
                chunks = await components.storage.list_chunks_by_source(f)
                assert chunks
                for c in chunks:
                    assert c.metadata.namespace == ns

    async def test_multi_slug_rerun_skips_all(self, components, tmp_path):
        projects = self._make_projects_dir(tmp_path)
        slug_dirs = _discover_claude_slug_dirs(projects)

        # First pass
        for mem_dir in slug_dirs:
            files = _discover_files(mem_dir)
            ns = _build_namespace(_derive_slug(mem_dir))
            await _ingest_files_with_components(components, files, ns)

        # Second pass — all skipped
        for mem_dir in slug_dirs:
            files = _discover_files(mem_dir)
            ns = _build_namespace(_derive_slug(mem_dir))
            summary = await _ingest_files_with_components(components, files, ns)
            assert summary.indexed == 0
            assert summary.skipped >= 2


# ── MCP ingest tool tests ─────────────────────────────────────────────


class TestMemIngestValidation:
    """Unit tests for mem_ingest input validation (no fixtures)."""

    async def test_invalid_source_type(self):
        from memtomem.server.tools.ingest import mem_ingest

        result = await mem_ingest(source="/tmp", source_type="notion")
        assert "unknown source_type" in result

    async def test_nonexistent_path(self, tmp_path):
        from memtomem.server.tools.ingest import mem_ingest

        result = await mem_ingest(source=str(tmp_path / "nope"), source_type="claude")
        assert "not found" in result

    async def test_empty_dir_returns_message(self, tmp_path):
        from memtomem.server.tools.ingest import mem_ingest

        result = await mem_ingest(source=str(tmp_path), source_type="claude")
        assert "No indexable" in result

    async def test_empty_codex_dir_returns_message(self, tmp_path):
        from memtomem.server.tools.ingest import mem_ingest

        result = await mem_ingest(source=str(tmp_path), source_type="codex")
        assert "No indexable" in result

    async def test_empty_gemini_dir_returns_message(self, tmp_path):
        from memtomem.server.tools.ingest import mem_ingest

        result = await mem_ingest(source=str(tmp_path), source_type="gemini")
        assert "No indexable" in result
