"""Tests for the IndexEngine: discovery, namespace, merging, overlap, and full indexing flow."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from memtomem.config import NamespaceConfig, NamespacePolicyRule
from memtomem.indexing.engine import (
    IndexEngine,
    _merge_short_chunks,
    _add_overlap,
    _estimate_tokens,
)
from memtomem.models import Chunk, ChunkMetadata


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_chunk_with(
    content: str,
    source: str = "/tmp/test.md",
    heading: tuple[str, ...] = (),
    namespace: str = "default",
) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(source),
            heading_hierarchy=heading,
            namespace=namespace,
        ),
    )


# ===========================================================================
# 1. _discover_files
# ===========================================================================


class TestDiscoverFiles:
    """Tests for IndexEngine._discover_files."""

    async def test_finds_supported_extensions(self, components, memory_dir):
        """Should discover .md, .json, .py files but not .txt."""
        (memory_dir / "notes.md").write_text("# Notes", encoding="utf-8")
        (memory_dir / "data.json").write_text('{"key": "val"}', encoding="utf-8")
        (memory_dir / "script.py").write_text("print('hello')", encoding="utf-8")
        (memory_dir / "readme.txt").write_text("ignored", encoding="utf-8")

        engine = components.index_engine
        files = engine._discover_files(memory_dir, recursive=True)

        names = {f.name for f in files}
        assert "notes.md" in names
        assert "data.json" in names
        assert "script.py" in names
        assert "readme.txt" not in names

    async def test_recursive_finds_nested(self, components, memory_dir):
        """Recursive mode should find files in subdirectories."""
        sub = memory_dir / "sub" / "deep"
        sub.mkdir(parents=True)
        (sub / "nested.md").write_text("# Nested", encoding="utf-8")
        (memory_dir / "top.md").write_text("# Top", encoding="utf-8")

        engine = components.index_engine
        files = engine._discover_files(memory_dir, recursive=True)

        names = {f.name for f in files}
        assert "top.md" in names
        assert "nested.md" in names

    async def test_non_recursive_only_top_level(self, components, memory_dir):
        """Non-recursive mode should only find top-level files."""
        sub = memory_dir / "subdir"
        sub.mkdir()
        (sub / "deep.md").write_text("# Deep", encoding="utf-8")
        (memory_dir / "surface.md").write_text("# Surface", encoding="utf-8")

        engine = components.index_engine
        files = engine._discover_files(memory_dir, recursive=False)

        names = {f.name for f in files}
        assert "surface.md" in names
        assert "deep.md" not in names

    async def test_excludes_git_and_node_modules(self, components, memory_dir):
        """Should skip .git/ and node_modules/ directories."""
        git_dir = memory_dir / ".git"
        git_dir.mkdir()
        (git_dir / "config.md").write_text("# Git config", encoding="utf-8")

        nm_dir = memory_dir / "node_modules"
        nm_dir.mkdir()
        (nm_dir / "package.json").write_text('{"name": "pkg"}', encoding="utf-8")

        pycache = memory_dir / "__pycache__"
        pycache.mkdir()
        (pycache / "cached.py").write_text("# cached", encoding="utf-8")

        (memory_dir / "real.md").write_text("# Real", encoding="utf-8")

        engine = components.index_engine
        files = engine._discover_files(memory_dir, recursive=True)

        names = {f.name for f in files}
        assert "real.md" in names
        assert "config.md" not in names
        assert "package.json" not in names
        assert "cached.py" not in names

    async def test_excludes_egg_info_suffix(self, components, memory_dir):
        """Directories ending with .egg-info should be excluded."""
        egg = memory_dir / "mypackage.egg-info"
        egg.mkdir()
        (egg / "PKG-INFO.md").write_text("# Info", encoding="utf-8")
        (memory_dir / "keep.md").write_text("# Keep", encoding="utf-8")

        engine = components.index_engine
        files = engine._discover_files(memory_dir, recursive=True)

        names = {f.name for f in files}
        assert "keep.md" in names
        assert "PKG-INFO.md" not in names

    async def test_returns_sorted_paths(self, components, memory_dir):
        """Discovered files should be sorted by path."""
        (memory_dir / "b.md").write_text("B", encoding="utf-8")
        (memory_dir / "a.md").write_text("A", encoding="utf-8")
        (memory_dir / "c.md").write_text("C", encoding="utf-8")

        engine = components.index_engine
        files = engine._discover_files(memory_dir, recursive=True)

        names = [f.name for f in files]
        assert names == sorted(names)

    async def test_empty_directory(self, components, memory_dir):
        """Empty directory returns empty list."""
        engine = components.index_engine
        files = engine._discover_files(memory_dir, recursive=True)
        assert files == []


# ===========================================================================
# 1b. exclude_patterns (built-in denylist + user-configurable patterns)
# ===========================================================================


class TestExcludePatterns:
    """Tests for built-in _BUILTIN_EXCLUDE_SPEC + user exclude_patterns."""

    async def test_builtin_secret_pattern_blocks_oauth_creds(self, components, memory_dir):
        """Files matching built-in secret patterns are never indexed."""
        (memory_dir / "oauth_creds.json").write_text('{"token": "x"}')
        (memory_dir / "keep.md").write_text("# Keep")

        files = components.index_engine._discover_files(memory_dir, recursive=True)
        names = {f.name for f in files}
        assert "keep.md" in names
        assert "oauth_creds.json" not in names

    async def test_builtin_noise_pattern_blocks_claude_meta(self, components, memory_dir):
        """Claude Code subagent .meta.json files are treated as noise."""
        sub = memory_dir / ".claude" / "projects" / "abc" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-x.meta.json").write_text('{"id": "x"}')
        (memory_dir / "keep.md").write_text("# Keep")

        files = components.index_engine._discover_files(memory_dir, recursive=True)
        names = {f.name for f in files}
        assert "keep.md" in names
        assert "agent-x.meta.json" not in names

    async def test_user_pattern_excludes_file(self, components, memory_dir):
        """User exclude_patterns are respected for positive matches."""
        (memory_dir / "draft.md").write_text("# Draft")
        (memory_dir / "final.md").write_text("# Final")

        engine = components.index_engine
        engine._config.exclude_patterns = ["**/draft.md"]

        files = engine._discover_files(memory_dir, recursive=True)
        names = {f.name for f in files}
        assert "final.md" in names
        assert "draft.md" not in names

    async def test_user_negation_restores_file(self, components, memory_dir):
        """User can negate their own earlier pattern with '!'."""
        (memory_dir / "draft.md").write_text("# Draft")
        (memory_dir / "important-draft.md").write_text("# Important")

        engine = components.index_engine
        engine._config.exclude_patterns = ["**/*draft*.md", "!**/important-draft.md"]

        files = engine._discover_files(memory_dir, recursive=True)
        names = {f.name for f in files}
        assert "important-draft.md" in names
        assert "draft.md" not in names

    async def test_user_negation_cannot_override_builtin_secret(self, components, memory_dir):
        """SECURITY REGRESSION: user '!' patterns cannot unset built-in secret denylist.

        Built-in and user specs are evaluated independently; a file is excluded
        if either matches. User negation affects only the user spec.
        """
        (memory_dir / "oauth_creds.json").write_text('{"token": "x"}')

        engine = components.index_engine
        engine._config.exclude_patterns = ["!**/oauth_creds.json"]

        files = engine._discover_files(memory_dir, recursive=True)
        names = {f.name for f in files}
        assert "oauth_creds.json" not in names

    async def test_case_insensitive_matching(self, components, memory_dir):
        """Built-in patterns match regardless of filesystem case."""
        (memory_dir / "OAuth_Creds.JSON").write_text('{"token": "x"}')
        (memory_dir / "keep.md").write_text("# Keep")

        files = components.index_engine._discover_files(memory_dir, recursive=True)
        names = {f.name for f in files}
        assert "keep.md" in names
        assert "OAuth_Creds.JSON" not in names

    async def test_aws_directory_excluded(self, components, memory_dir):
        """Directory-level secret stores (.aws) are never traversed."""
        aws = memory_dir / ".aws"
        aws.mkdir()
        (aws / "config.toml").write_text("[profile]")
        (memory_dir / "keep.md").write_text("# Keep")

        files = components.index_engine._discover_files(memory_dir, recursive=True)
        names = {f.name for f in files}
        assert "keep.md" in names
        assert "config.toml" not in names

    async def test_builtin_blocks_subagent_meta_when_root_is_claude_projects(
        self, components, memory_dir
    ):
        """REGRESSION: when ``~/.claude/projects`` itself is the memory_dir root,
        the rel path drops the ``.claude/`` token. The built-in noise pattern
        must still match — either via the ``**/subagents/*.meta.json`` rel form
        or via the absolute-path key. Previously these files were silently
        indexed because only the rel path was checked against the
        ``.claude/``-prefixed pattern.
        """
        sub = memory_dir / "abc123-uuid" / "subagents"
        sub.mkdir(parents=True)
        (sub / "agent-x.meta.json").write_text('{"agentType": "Explore"}')
        (memory_dir / "keep.md").write_text("# Keep")

        files = components.index_engine._discover_files(memory_dir, recursive=True)
        names = {f.name for f in files}
        assert "keep.md" in names
        assert "agent-x.meta.json" not in names

    async def test_builtin_blocks_oauth_at_memory_dir_root(self, components, memory_dir):
        """REGRESSION: a credential file sitting directly at the memory_dir root
        must still be caught. Previously this depended on pathspec matching
        ``**/oauth_creds.json`` against a zero-directory rel key (``oauth_creds.json``);
        the absolute-path key path closes that gap.
        """
        (memory_dir / "oauth_creds.json").write_text('{"token": "x"}')
        (memory_dir / "keep.md").write_text("# Keep")

        files = components.index_engine._discover_files(memory_dir, recursive=True)
        names = {f.name for f in files}
        assert "keep.md" in names
        assert "oauth_creds.json" not in names

    async def test_index_file_entry_point_blocks_excluded(self, components, memory_dir):
        """REGRESSION: ``index_file`` is the file-watcher entry point and was
        previously bypassing exclude checks entirely (only ``_discover_files``
        was guarded). A watcher event for an OAuth credential file would have
        indexed it. The fix applies the same exclude policy at this entry
        point — the call returns ``total_files=0`` without touching storage.
        """
        creds_path = memory_dir / "oauth_creds.json"
        creds_path.write_text('{"token": "secret"}')

        stats = await components.index_engine.index_file(creds_path)

        assert stats.total_files == 0
        assert stats.total_chunks == 0
        assert stats.indexed_chunks == 0

    async def test_index_path_stream_single_file_blocks_excluded(self, components, memory_dir):
        """REGRESSION: ``index_path_stream`` has a single-file branch
        (``if path.is_file(): files = [path]``) that skips ``_discover_files``
        and calls guard-free ``_index_file`` directly. Exposed via the Web UI
        ``GET /api/index/stream?path=...`` endpoint. The primary guard at
        ``_index_file`` closes this entry point; a chunker spy proves the
        guard fires *before* parsing so ``stored==0`` alone is not relied on
        (NoopEmbedder's sqlite-vec insert can roll back and mask a bypass).
        """
        creds_path = memory_dir / "oauth_creds.json"
        creds_path.write_text('{"token": "secret"}')

        engine = components.index_engine
        chunker_calls: list[Path] = []
        orig = engine._registry.chunk_file

        def spy(fp: Path, content: str):
            chunker_calls.append(fp)
            return orig(fp, content)

        engine._registry.chunk_file = spy  # type: ignore[method-assign]
        try:
            events = [ev async for ev in engine.index_path_stream(creds_path, recursive=False)]
        finally:
            engine._registry.chunk_file = orig  # type: ignore[method-assign]

        assert all("oauth_creds" not in str(c) for c in chunker_calls)
        complete = next(e for e in events if e.get("type") == "complete")
        assert complete["indexed_chunks"] == 0
        assert complete["total_chunks"] == 0

    async def test_index_path_stream_dir_still_filters_excluded(self, components, memory_dir):
        """REGRESSION: the directory-walk path of ``index_path_stream``
        still funnels filtered files through ``_index_file``. Ensure
        legitimate files are indexed while the excluded sibling is skipped.
        """
        (memory_dir / "oauth_creds.json").write_text('{"token": "x"}')
        (memory_dir / "notes.md").write_text("# Keep\n\nContent to index.")

        engine = components.index_engine
        chunker_calls: list[str] = []
        orig = engine._registry.chunk_file

        def spy(fp: Path, content: str):
            chunker_calls.append(fp.name)
            return orig(fp, content)

        engine._registry.chunk_file = spy  # type: ignore[method-assign]
        try:
            events = [ev async for ev in engine.index_path_stream(memory_dir, recursive=True)]
        finally:
            engine._registry.chunk_file = orig  # type: ignore[method-assign]

        assert "oauth_creds.json" not in chunker_calls
        assert "notes.md" in chunker_calls
        complete = next(e for e in events if e.get("type") == "complete")
        assert complete["indexed_chunks"] >= 1

    async def test_index_path_stream_namespace_propagates(self, components, memory_dir):
        """``index_path_stream`` must accept ``namespace`` and apply it to all
        indexed chunks — same contract as ``index_path``. Issue #590: the
        streaming endpoint previously dropped this argument silently, so
        Web UI / CLI / MCP callers that streamed an index ignored the user's
        namespace selection.
        """
        notes = memory_dir / "notes.md"
        notes.write_text("# Heading\n\nBody to index for namespace check.")

        engine = components.index_engine
        events = [
            ev
            async for ev in engine.index_path_stream(memory_dir, recursive=True, namespace="ns590")
        ]
        complete = next(e for e in events if e.get("type") == "complete")
        assert complete["indexed_chunks"] >= 1

        chunks = await components.storage.list_chunks_by_source(notes)
        assert chunks, "expected indexed chunks for notes.md"
        assert all(c.metadata.namespace == "ns590" for c in chunks)

    async def test_index_path_stream_complete_errors_no_silent_drop(self, components, memory_dir):
        """Per-file ``_index_file`` errors must surface in the ``complete``
        event's ``errors`` field. Issue #590 regression guard: previously the
        stream loop summed only chunk counters and silently dropped any
        ``result["errors"]``, so partial-failure runs (binary file, file too
        large, embedding failure on a subset) appeared successful.

        Uses a binary file (engine.py:621-628 path) as the cleanest
        deterministic per-file error trigger — ``_index_file`` returns
        ``errors=[f"{name}: binary file detected, skipping"]`` without raising.
        """
        binary = memory_dir / "blob.md"
        # NUL byte in the first 8 KiB → ``_index_file`` short-circuits with a
        # binary-detected error entry rather than chunking + embedding.
        binary.write_bytes(b"# Title\n\nbody\x00more body\n")

        engine = components.index_engine
        events = [ev async for ev in engine.index_path_stream(memory_dir, recursive=True)]
        complete = next(e for e in events if e.get("type") == "complete")

        assert complete["errors"], (
            "complete.errors must be non-empty when a file fails — silent "
            "drop is a contract violation (#590)"
        )
        assert any("blob.md" in err for err in complete["errors"])


# ===========================================================================
# 2. _resolve_namespace
# ===========================================================================


class TestResolveNamespace:
    """Tests for IndexEngine._resolve_namespace."""

    async def test_explicit_namespace_wins(self, components, memory_dir):
        """Explicit namespace parameter should override everything."""
        engine = components.index_engine
        fp = memory_dir / "file.md"
        result = engine._resolve_namespace(fp, "my-ns")
        assert result == "my-ns"

    async def test_auto_ns_folder_based(self, components, memory_dir):
        """With auto_ns enabled, derives namespace from parent folder."""
        engine = components.index_engine
        engine._ns_config = NamespaceConfig(enable_auto_ns=True)

        sub = memory_dir / "project-x"
        sub.mkdir()
        fp = sub / "notes.md"
        fp.write_text("# Notes", encoding="utf-8")

        result = engine._resolve_namespace(fp, None)
        assert result == "project-x"

    async def test_auto_ns_skips_memory_root(self, components, memory_dir):
        """Auto-ns should NOT use the memory_dir root itself as namespace."""
        engine = components.index_engine
        engine._ns_config = NamespaceConfig(enable_auto_ns=True)

        fp = memory_dir / "notes.md"
        fp.write_text("# Notes", encoding="utf-8")

        result = engine._resolve_namespace(fp, None)
        # Should fall back to default, not use memory_dir folder name
        assert result is None  # "default" is treated as no namespace

    async def test_no_auto_no_explicit_returns_none(self, components, memory_dir):
        """Without auto_ns and no explicit ns, returns None."""
        engine = components.index_engine
        engine._ns_config = NamespaceConfig(enable_auto_ns=False, default_namespace="default")

        fp = memory_dir / "file.md"
        result = engine._resolve_namespace(fp, None)
        assert result is None

    async def test_custom_default_namespace(self, components, memory_dir):
        """Non-'default' default_namespace should be returned."""
        engine = components.index_engine
        engine._ns_config = NamespaceConfig(enable_auto_ns=False, default_namespace="work")

        fp = memory_dir / "file.md"
        result = engine._resolve_namespace(fp, None)
        assert result == "work"


# ===========================================================================
# 2b. _resolve_namespace with NamespacePolicyRule
# ===========================================================================


def _install_rules(engine, rules, *, enable_auto_ns=False, default_namespace="default"):
    """Swap an engine's namespace config + rebuild its pre-compiled rule specs.

    Mirrors the constructor wiring in IndexEngine.__init__ so tests can exercise
    different rule sets without standing up a fresh component stack.
    """
    from memtomem.indexing.engine import _build_exclude_spec

    engine._ns_config = NamespaceConfig(
        enable_auto_ns=enable_auto_ns,
        default_namespace=default_namespace,
        rules=rules,
    )
    engine._ns_rule_specs = [(_build_exclude_spec([rule.path_glob]), rule) for rule in rules]
    engine._warned_empty_parent_rules = set()


class TestNamespacePolicyRules:
    """Tests for IndexEngine._resolve_namespace with rule-based policy."""

    async def test_no_rules_preserves_existing_behavior(self, components, memory_dir):
        """rules=[] → priority chain behaves exactly like the pre-rules path."""
        engine = components.index_engine
        _install_rules(engine, [])

        fp = memory_dir / "notes.md"
        assert engine._resolve_namespace(fp, None) is None
        assert engine._resolve_namespace(fp, "explicit") == "explicit"

    async def test_rule_match_returns_namespace(self, components, memory_dir):
        """A matching rule returns its namespace."""
        engine = components.index_engine
        rule_glob = f"{memory_dir.as_posix()}/**"
        _install_rules(
            engine,
            [NamespacePolicyRule(path_glob=rule_glob, namespace="matched")],
        )

        fp = memory_dir / "sub" / "notes.md"
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text("# Notes")

        assert engine._resolve_namespace(fp, None) == "matched"

    async def test_first_match_wins(self, components, memory_dir):
        """When multiple rules match, the earliest one wins."""
        engine = components.index_engine
        _install_rules(
            engine,
            [
                NamespacePolicyRule(path_glob=f"{memory_dir.as_posix()}/**", namespace="first"),
                NamespacePolicyRule(path_glob=f"{memory_dir.as_posix()}/**", namespace="second"),
            ],
        )

        fp = memory_dir / "notes.md"
        fp.write_text("# Notes")

        assert engine._resolve_namespace(fp, None) == "first"

    async def test_explicit_ns_beats_rules(self, components, memory_dir):
        """Explicit namespace argument takes priority over any rule match."""
        engine = components.index_engine
        _install_rules(
            engine,
            [
                NamespacePolicyRule(path_glob=f"{memory_dir.as_posix()}/**", namespace="ruled"),
            ],
        )

        fp = memory_dir / "notes.md"
        assert engine._resolve_namespace(fp, "explicit") == "explicit"

    async def test_rules_beat_auto_ns(self, components, memory_dir):
        """A rule match wins over enable_auto_ns folder derivation."""
        engine = components.index_engine
        _install_rules(
            engine,
            [
                NamespacePolicyRule(path_glob=f"{memory_dir.as_posix()}/**", namespace="ruled"),
            ],
            enable_auto_ns=True,
        )

        sub = memory_dir / "project-x"
        sub.mkdir()
        fp = sub / "notes.md"
        fp.write_text("# Notes")

        # Without rules this would have returned "project-x" (see
        # TestResolveNamespace.test_auto_ns_folder_based).
        assert engine._resolve_namespace(fp, None) == "ruled"

    async def test_parent_placeholder_substitution(self, components, memory_dir):
        """``{parent}`` expands to the matched file's parent folder name."""
        engine = components.index_engine
        _install_rules(
            engine,
            [
                NamespacePolicyRule(
                    path_glob=f"{memory_dir.as_posix()}/**",
                    namespace="gdrive:{parent}",
                ),
            ],
        )

        sub = memory_dir / "team-alpha"
        sub.mkdir()
        fp = sub / "notes.md"
        fp.write_text("# Notes")

        assert engine._resolve_namespace(fp, None) == "gdrive:team-alpha"

    async def test_home_tilde_in_path_glob_expanded(self):
        """Leading ``~/`` in path_glob expands at load time."""
        rule = NamespacePolicyRule(path_glob="~/some/path/**", namespace="home")
        assert not rule.path_glob.startswith("~"), rule.path_glob
        assert rule.path_glob.endswith("/some/path/**")

    async def test_parent_placeholder_empty_parent_falls_through(
        self, components, memory_dir, caplog
    ):
        """When ``{parent}`` expands to an empty string the rule is skipped and
        the next fallback (here: default_namespace) is returned. Also verifies
        the skip is logged once per rule index.
        """
        import logging

        engine = components.index_engine
        _install_rules(
            engine,
            [
                NamespacePolicyRule(path_glob="**", namespace="prefix:{parent}"),
            ],
            default_namespace="fallback",
        )

        # A path whose parent is "/" — parent.name == "" on POSIX.
        fp = Path("/root-level.md")

        with caplog.at_level(logging.WARNING, logger="memtomem.indexing.engine"):
            assert engine._resolve_namespace(fp, None) == "fallback"
            # Second call on the same rule index must not re-log.
            assert engine._resolve_namespace(fp, None) == "fallback"

        skip_warnings = [r for r in caplog.records if "parent name empty" in r.getMessage()]
        assert len(skip_warnings) == 1, [r.getMessage() for r in skip_warnings]

    async def test_case_insensitive_matching(self, components, memory_dir):
        """Glob matching is case-insensitive — same semantics as exclude_patterns."""
        engine = components.index_engine
        _install_rules(
            engine,
            [
                NamespacePolicyRule(
                    path_glob=f"{memory_dir.as_posix()}/**/.CLAUDE/**",
                    namespace="claude",
                ),
            ],
        )

        sub = memory_dir / "proj" / ".claude"
        sub.mkdir(parents=True)
        fp = sub / "notes.md"
        fp.write_text("# Notes")

        assert engine._resolve_namespace(fp, None) == "claude"

    async def test_literal_namespace_no_placeholder(self, components, memory_dir):
        """A namespace template without ``{parent}`` is returned verbatim."""
        engine = components.index_engine
        _install_rules(
            engine,
            [
                NamespacePolicyRule(
                    path_glob=f"{memory_dir.as_posix()}/**",
                    namespace="literal-label",
                ),
            ],
        )

        fp = memory_dir / "notes.md"
        fp.write_text("# Notes")

        assert engine._resolve_namespace(fp, None) == "literal-label"

    def test_unknown_placeholder_rejected_at_load(self):
        """``{unknown}`` in namespace raises a ValidationError at construction."""
        with pytest.raises(ValidationError) as excinfo:
            NamespacePolicyRule(path_glob="**", namespace="foo:{unknown}")
        assert "unknown placeholder" in str(excinfo.value).lower()

    def test_namespace_length_limit_rejected_at_load(self):
        """A namespace over 128 chars raises a ValidationError."""
        with pytest.raises(ValidationError) as excinfo:
            NamespacePolicyRule(path_glob="**", namespace="x" * 129)
        assert "128" in str(excinfo.value)

    # ---- {ancestor:N} placeholder (issue #296) -----------------------------

    async def test_ancestor_placeholder_grandparent(self, components, memory_dir):
        """``{ancestor:1}`` picks the folder above the immediate parent —
        the canonical #296 shape (``.../<project-id>/memory/<file>``)."""
        engine = components.index_engine
        _install_rules(
            engine,
            [
                NamespacePolicyRule(
                    path_glob=f"{memory_dir.as_posix()}/**",
                    namespace="claude:{ancestor:1}",
                ),
            ],
        )

        proj = memory_dir / "FOO" / "memory"
        proj.mkdir(parents=True)
        fp = proj / "notes.md"
        fp.write_text("# Notes")

        assert engine._resolve_namespace(fp, None) == "claude:FOO"

    async def test_ancestor_zero_equals_parent(self, components, memory_dir):
        """``{ancestor:0}`` is equivalent to ``{parent}`` — the immediate
        parent folder name. Codified so the two placeholders stay in sync."""
        engine = components.index_engine
        _install_rules(
            engine,
            [
                NamespacePolicyRule(
                    path_glob=f"{memory_dir.as_posix()}/**",
                    namespace="a0:{ancestor:0}",
                ),
            ],
        )

        sub = memory_dir / "team-alpha"
        sub.mkdir()
        fp = sub / "notes.md"
        fp.write_text("# Notes")

        assert engine._resolve_namespace(fp, None) == "a0:team-alpha"

    async def test_ancestor_out_of_range_falls_through(self, components, memory_dir, caplog):
        """An out-of-range ``{ancestor:N}`` skips the rule and falls through,
        logging once per rule index (same shape as the empty-parent skip)."""
        import logging

        engine = components.index_engine
        _install_rules(
            engine,
            [
                NamespacePolicyRule(
                    path_glob=f"{memory_dir.as_posix()}/**",
                    namespace="deep:{ancestor:99}",
                ),
            ],
            default_namespace="fallback",
        )

        fp = memory_dir / "notes.md"
        fp.write_text("# Notes")

        with caplog.at_level(logging.WARNING, logger="memtomem.indexing.engine"):
            assert engine._resolve_namespace(fp, None) == "fallback"
            # Second call on the same rule index must not re-log.
            assert engine._resolve_namespace(fp, None) == "fallback"

        skip_warnings = [r for r in caplog.records if "ancestor:99" in r.getMessage()]
        assert len(skip_warnings) == 1, [r.getMessage() for r in skip_warnings]

    async def test_ancestor_combined_with_literal(self, components, memory_dir):
        """Ancestor placeholder composes with literal prefixes/suffixes like
        ``{parent}`` does — template parsing is position-preserving."""
        engine = components.index_engine
        _install_rules(
            engine,
            [
                NamespacePolicyRule(
                    path_glob=f"{memory_dir.as_posix()}/**",
                    namespace="{ancestor:1}/sub",
                ),
            ],
        )

        proj = memory_dir / "BAR" / "memory"
        proj.mkdir(parents=True)
        fp = proj / "notes.md"
        fp.write_text("# Notes")

        assert engine._resolve_namespace(fp, None) == "BAR/sub"

    def test_ancestor_without_index_rejected_at_load(self):
        """Bare ``{ancestor}`` without an integer spec is a configuration
        error — the caller must say which level."""
        with pytest.raises(ValidationError) as excinfo:
            NamespacePolicyRule(path_glob="**", namespace="bad:{ancestor}")
        assert "requires an integer index" in str(excinfo.value)

    def test_ancestor_non_integer_spec_rejected_at_load(self):
        """``{ancestor:abc}`` is rejected — spec must parse as int."""
        with pytest.raises(ValidationError) as excinfo:
            NamespacePolicyRule(path_glob="**", namespace="bad:{ancestor:abc}")
        assert "non-negative integer" in str(excinfo.value)

    def test_ancestor_negative_index_rejected_at_load(self):
        """Negative indices would wrap into ``parents[-1]`` (filesystem
        root) — reject explicitly instead of silently doing the wrong thing."""
        with pytest.raises(ValidationError) as excinfo:
            NamespacePolicyRule(path_glob="**", namespace="bad:{ancestor:-1}")
        assert "non-negative" in str(excinfo.value)

    def test_parent_with_format_spec_rejected_at_load(self):
        """``{parent:N}`` is ambiguous — force users to switch to the
        ``{ancestor:N}`` spelling rather than silently padding/truncating."""
        with pytest.raises(ValidationError) as excinfo:
            NamespacePolicyRule(path_glob="**", namespace="bad:{parent:1}")
        assert "ancestor" in str(excinfo.value).lower()


# ===========================================================================
# 2c. discover_indexable_files + resolve_namespaces_for (preview-namespace)
# ===========================================================================


class TestPreviewHelpers:
    """Public helpers powering both ``trigger_index`` and the
    ``preview-namespace`` route. The two paths must walk the same file
    set and resolve the same namespaces, otherwise the UI's preview echo
    silently disagrees with the actually-applied result."""

    async def test_discover_indexable_files_handles_single_file(self, components, memory_dir):
        engine = components.index_engine
        fp = memory_dir / "single.md"
        fp.write_text("# Single", encoding="utf-8")

        files = engine.discover_indexable_files(fp)
        assert files == [fp.resolve()]

    async def test_discover_indexable_files_handles_directory(self, components, memory_dir):
        engine = components.index_engine
        (memory_dir / "a.md").write_text("# A", encoding="utf-8")
        (memory_dir / "b.md").write_text("# B", encoding="utf-8")

        files = engine.discover_indexable_files(memory_dir)
        names = {f.name for f in files}
        assert names == {"a.md", "b.md"}

    async def test_discover_indexable_files_outside_memory_dirs(self, components, tmp_path):
        """Out-of-memory_dirs returns empty — same trust gate as
        ``index_path``. The route layer turns this into a 403; the engine
        helper must not leak file enumeration outside the boundary."""
        engine = components.index_engine
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "leak.md").write_text("# Leak", encoding="utf-8")

        assert engine.discover_indexable_files(outside) == []

    async def test_resolve_namespaces_for_uniform(self, components, memory_dir):
        """All files under the memory_dir root with default config →
        single ``None`` entry (untagged, ``default_namespace == 'default'``
        carve-out)."""
        engine = components.index_engine
        (memory_dir / "a.md").write_text("# A", encoding="utf-8")
        (memory_dir / "b.md").write_text("# B", encoding="utf-8")

        files = engine.discover_indexable_files(memory_dir)
        assert engine.resolve_namespaces_for(files) == [None]

    async def test_resolve_namespaces_for_rule_variance(self, components, memory_dir):
        """The motivating case for the list shape: a folder where rules
        split files into two namespaces. A scalar response would silently
        lie about which namespace was applied."""
        engine = components.index_engine
        (memory_dir / "alpha").mkdir()
        (memory_dir / "beta").mkdir()
        (memory_dir / "alpha" / "a.md").write_text("# A", encoding="utf-8")
        (memory_dir / "beta" / "b.md").write_text("# B", encoding="utf-8")
        _install_rules(
            engine,
            [
                NamespacePolicyRule(
                    path_glob=f"{memory_dir.as_posix()}/alpha/**",
                    namespace="ns-alpha",
                ),
                NamespacePolicyRule(
                    path_glob=f"{memory_dir.as_posix()}/beta/**",
                    namespace="ns-beta",
                ),
            ],
        )

        files = engine.discover_indexable_files(memory_dir)
        result = engine.resolve_namespaces_for(files)
        assert result == ["ns-alpha", "ns-beta"]

    async def test_resolve_namespaces_for_sorts_with_none_last(self, components, memory_dir):
        """``None`` (untagged) sorts after named namespaces — UI renders
        named NSes first, then the untagged sentinel, in stable order."""
        engine = components.index_engine
        (memory_dir / "ruled").mkdir()
        (memory_dir / "ruled" / "r.md").write_text("# R", encoding="utf-8")
        (memory_dir / "untagged.md").write_text("# U", encoding="utf-8")
        _install_rules(
            engine,
            [
                NamespacePolicyRule(
                    path_glob=f"{memory_dir.as_posix()}/ruled/**",
                    namespace="zeta-ruled",
                ),
            ],
        )

        files = engine.discover_indexable_files(memory_dir)
        # zeta-ruled is alphabetically *after* a hypothetical None — confirm
        # None doesn't naively win the sort.
        result = engine.resolve_namespaces_for(files)
        assert result == ["zeta-ruled", None]

    async def test_walk_matches_index_walk(self, components, memory_dir):
        """File-set parity contract: ``discover_indexable_files`` returns
        exactly the files ``index_path`` would visit, so the preview
        cannot disagree with the actually-applied namespace set."""
        engine = components.index_engine
        (memory_dir / "a.md").write_text("# A", encoding="utf-8")
        (memory_dir / "sub").mkdir()
        (memory_dir / "sub" / "nested.md").write_text("# Nested", encoding="utf-8")
        # Hidden / excluded files must be filtered out by both paths.
        (memory_dir / ".hidden.md").write_text("# Hidden", encoding="utf-8")

        # Public helper.
        public = set(engine.discover_indexable_files(memory_dir, recursive=True))
        # Internal walker — what ``_index_path_inner`` calls today.
        internal = set(engine._discover_files(memory_dir, recursive=True))
        assert public == internal


# ===========================================================================
# 3. _apply_namespace
# ===========================================================================


class TestApplyNamespace:
    """Tests for IndexEngine._apply_namespace."""

    def test_applies_namespace_to_all_chunks(self):
        """All chunks should get the specified namespace."""
        chunks = [
            _make_chunk_with("content A", namespace="old"),
            _make_chunk_with("content B", namespace="old"),
        ]
        result = IndexEngine._apply_namespace(chunks, "new-ns")
        assert all(c.metadata.namespace == "new-ns" for c in result)

    def test_preserves_other_metadata(self):
        """Other metadata fields should be preserved."""
        chunk = _make_chunk_with(
            "content",
            source="/tmp/src.md",
            heading=("H1", "H2"),
        )
        chunk_list = [chunk]
        result = IndexEngine._apply_namespace(chunk_list, "project")

        r = result[0]
        assert r.metadata.source_file == Path("/tmp/src.md")
        assert r.metadata.heading_hierarchy == ("H1", "H2")
        assert r.content == "content"
        assert r.metadata.namespace == "project"

    def test_empty_list(self):
        """Empty chunk list should return empty."""
        assert IndexEngine._apply_namespace([], "ns") == []


# ===========================================================================
# 4. _merge_short_chunks
# ===========================================================================


class TestMergeShortChunks:
    """Tests for the _merge_short_chunks post-processing function."""

    def test_merge_two_short_same_heading(self):
        """Two short chunks from the same file+heading should merge."""
        c1 = _make_chunk_with("short", heading=("H1",))
        c2 = _make_chunk_with("also short", heading=("H1",))

        # min_tokens high enough to trigger merge
        result = _merge_short_chunks([c1, c2], min_tokens=50, max_tokens=2000)
        assert len(result) == 1
        assert "short" in result[0].content
        assert "also short" in result[0].content

    def test_distinct_root_short_chunks_stay_separate(self):
        """Short chunks under distinct top-level roots (e.g. mem_add entries
        ``## Cache Decision`` vs ``## Database Decision``) must not merge,
        even when both are below min_tokens.
        """
        c1 = _make_chunk_with("section one", heading=("H1",))
        c2 = _make_chunk_with("section two", heading=("H2",))

        result = _merge_short_chunks([c1, c2], min_tokens=50, max_tokens=2000)
        assert len(result) == 2

    def test_short_cross_subsection_same_root_merges(self):
        """Short orphan in a subsection merges with the next subsection
        when both share the same top-level root (rescues audit-doc-style
        cross-``##`` micro-chunks).
        """
        summary = _make_chunk_with("short summary", heading=("# Root", "## Summary"))
        first = _make_chunk_with(
            "section body " * 40,
            heading=("# Root", "## 1. Findings", "### Critical Files"),
        )

        result = _merge_short_chunks([summary, first], min_tokens=128, max_tokens=2000)
        assert len(result) == 1

    def test_no_merge_different_sources(self):
        """Chunks from different source files should NOT merge."""
        c1 = _make_chunk_with("file A", source="/tmp/a.md", heading=("H1",))
        c2 = _make_chunk_with("file B", source="/tmp/b.md", heading=("H1",))

        result = _merge_short_chunks([c1, c2], min_tokens=50, max_tokens=2000)
        assert len(result) == 2

    def test_already_long_enough(self):
        """Chunks already above min_tokens should not be merged."""
        long_content = "word " * 200  # ~200 tokens
        c1 = _make_chunk_with(long_content, heading=("H1",))
        c2 = _make_chunk_with(long_content, heading=("H1",))

        result = _merge_short_chunks([c1, c2], min_tokens=10, max_tokens=2000)
        assert len(result) == 2

    def test_max_tokens_prevents_overmerge(self):
        """Merging should stop when max_tokens would be exceeded."""
        # Each chunk is ~50 tokens (150 chars / 3)
        content = "x" * 150
        c1 = _make_chunk_with(content, heading=("H1",))
        c2 = _make_chunk_with(content, heading=("H1",))
        c3 = _make_chunk_with(content, heading=("H1",))

        # min_tokens=60 forces merge, max_tokens=110 caps at 2 chunks merged
        result = _merge_short_chunks([c1, c2, c3], min_tokens=60, max_tokens=110)
        assert len(result) >= 2  # should NOT merge all three

    def test_min_tokens_zero_noop(self):
        """min_tokens=0 should skip merging entirely."""
        c1 = _make_chunk_with("a")
        c2 = _make_chunk_with("b")
        result = _merge_short_chunks([c1, c2], min_tokens=0)
        assert len(result) == 2

    def test_single_chunk_noop(self):
        """Single chunk list should be returned as-is."""
        c1 = _make_chunk_with("only one")
        result = _merge_short_chunks([c1], min_tokens=100)
        assert len(result) == 1

    def test_merged_preserves_start_end_lines(self):
        """Merged chunk should span from first start_line to last end_line."""
        c1 = Chunk(
            content="first",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/t.md"),
                heading_hierarchy=("H1",),
                start_line=1,
                end_line=5,
            ),
        )
        c2 = Chunk(
            content="second",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/t.md"),
                heading_hierarchy=("H1",),
                start_line=6,
                end_line=10,
            ),
        )
        result = _merge_short_chunks([c1, c2], min_tokens=100, max_tokens=2000)
        assert len(result) == 1
        assert result[0].metadata.start_line == 1
        assert result[0].metadata.end_line == 10

    def test_headingless_chunk_merges_into_next_section(self):
        """Headingless chunk (e.g. frontmatter) should merge into the next heading section."""
        frontmatter = _make_chunk_with("---\ntags: [redis]\n---", heading=())
        section = _make_chunk_with("Redis LRU eviction policy", heading=("## Cache",))

        result = _merge_short_chunks([frontmatter, section], min_tokens=50, max_tokens=2000)
        assert len(result) == 1
        assert "tags:" in result[0].content
        assert "Redis LRU" in result[0].content
        # Adopts the heading hierarchy of the section
        assert result[0].metadata.heading_hierarchy == ("## Cache",)

    def test_headingless_chunk_alone_stays(self):
        """Headingless chunk with no following section stays as-is (already >= min_tokens)."""
        big = _make_chunk_with("x" * 600, heading=())  # ~200 tokens
        result = _merge_short_chunks([big], min_tokens=50, max_tokens=2000)
        assert len(result) == 1

    def test_long_different_headings_stay_separate(self):
        """Long chunks (above min) with different headings stay separate.

        Pass 2 greedy packing respects the hierarchy gate — cross-heading
        merges are only allowed while a chunk is below min_tokens.
        """
        c1 = _make_chunk_with("x" * 600, heading=("## A",))  # ~200 tokens
        c2 = _make_chunk_with("y" * 600, heading=("## B",))  # ~200 tokens
        result = _merge_short_chunks([c1, c2], min_tokens=128, max_tokens=512, target_tokens=384)
        assert len(result) == 2

    def test_headingless_respects_max_tokens(self):
        """Headingless merge should still respect max_tokens."""
        frontmatter = _make_chunk_with("x" * 300, heading=())  # ~100 tokens
        section = _make_chunk_with("y" * 300, heading=("## Big",))  # ~100 tokens

        result = _merge_short_chunks([frontmatter, section], min_tokens=50, max_tokens=110)
        assert len(result) == 2  # would exceed max_tokens

    def test_headingless_chain_merges(self):
        """Multiple headingless chunks before a heading all merge into it."""
        c1 = _make_chunk_with("meta1", heading=())
        c2 = _make_chunk_with("meta2", heading=())
        c3 = _make_chunk_with("actual content", heading=("## Section",))

        result = _merge_short_chunks([c1, c2, c3], min_tokens=50, max_tokens=2000)
        assert len(result) == 1
        assert "meta1" in result[0].content
        assert "meta2" in result[0].content
        assert "actual content" in result[0].content
        assert result[0].metadata.heading_hierarchy == ("## Section",)


# ===========================================================================
# 5. _add_overlap
# ===========================================================================


class TestAddOverlap:
    """Tests for the _add_overlap post-processing function."""

    def test_overlap_between_adjacent_same_source(self):
        """Adjacent chunks from the same file should get overlap content."""
        c1 = _make_chunk_with("First chunk content here")
        c2 = _make_chunk_with("Second chunk content here")

        result = _add_overlap([c1, c2], overlap_tokens=5)

        # c1 should have overlap_after > 0
        assert result[0].metadata.overlap_after > 0
        assert result[0].metadata.overlap_before == 0

        # c2 should have overlap_before > 0
        assert result[1].metadata.overlap_before > 0
        assert result[1].metadata.overlap_after == 0

    def test_single_chunk_no_overlap(self):
        """Single chunk should have no overlap."""
        c = _make_chunk_with("Only chunk")
        result = _add_overlap([c], overlap_tokens=10)
        assert len(result) == 1
        assert result[0].metadata.overlap_before == 0
        assert result[0].metadata.overlap_after == 0

    def test_different_sources_no_overlap(self):
        """Chunks from different files should NOT get overlap."""
        c1 = _make_chunk_with("File A", source="/tmp/a.md")
        c2 = _make_chunk_with("File B", source="/tmp/b.md")

        result = _add_overlap([c1, c2], overlap_tokens=10)
        assert result[0].metadata.overlap_after == 0
        assert result[1].metadata.overlap_before == 0

    def test_zero_overlap_noop(self):
        """overlap_tokens=0 should not change chunks."""
        c1 = _make_chunk_with("chunk 1")
        c2 = _make_chunk_with("chunk 2")
        result = _add_overlap([c1, c2], overlap_tokens=0)
        assert result[0].content == "chunk 1"
        assert result[1].content == "chunk 2"

    def test_overlap_content_is_borrowed(self):
        """Overlapped chunk content should contain text from neighbor."""
        c1 = _make_chunk_with("ALPHA content here")
        c2 = _make_chunk_with("BETA content here")

        # overlap_chars = overlap_tokens * 3; use 10 so chars=30 > len of each content
        result = _add_overlap([c1, c2], overlap_tokens=10)

        # c1's content should contain borrowed text from c2
        assert "BETA" in result[0].content
        # c2's content should contain borrowed text from c1
        assert "ALPHA" in result[1].content


# ===========================================================================
# 6. _estimate_tokens
# ===========================================================================


class TestEstimateTokens:
    """Tests for the rough token estimator."""

    def test_empty_string(self):
        assert _estimate_tokens("") == 1  # max(1, 0)

    def test_short_string(self):
        assert _estimate_tokens("hello") >= 1

    def test_longer_string_english(self):
        # 300 ASCII chars -> 300//4 = 75 tokens (English ratio)
        text = "a" * 300
        assert _estimate_tokens(text) == 75

    def test_korean_text(self):
        # Korean text uses ratio=2 when > 30% Korean chars
        text = "안녕하세요 " * 30  # ~180 chars, mostly Korean
        result = _estimate_tokens(text)
        assert result == len(text) // 2


# ===========================================================================
# 7. index_file — full flow with mocked embedder
# ===========================================================================


class TestIndexFile:
    """Integration tests for index_file with mocked embedder."""

    async def test_index_markdown_file(self, components, memory_dir):
        """Index a markdown file with two headings; verify chunks stored."""
        md_content = (
            "# Section A\n\nContent for section A.\n\n# Section B\n\nContent for section B.\n"
        )
        md_path = memory_dir / "test_doc.md"
        md_path.write_text(md_content, encoding="utf-8")

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        stats = await components.index_engine.index_file(md_path)

        assert stats.total_files == 1
        assert stats.total_chunks > 0
        assert stats.indexed_chunks > 0
        assert mock_embedder.embed_texts.called

    async def test_unchanged_file_skips_reembedding(self, components, memory_dir):
        """Re-indexing an unchanged file should skip embedding."""
        md_path = memory_dir / "stable.md"
        md_path.write_text("# Title\n\nStable content here.\n", encoding="utf-8")

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        # First indexing
        stats1 = await components.index_engine.index_file(md_path)
        assert stats1.indexed_chunks > 0

        call_count_after_first = mock_embedder.embed_texts.call_count

        # Second indexing — no changes
        stats2 = await components.index_engine.index_file(md_path)
        assert stats2.skipped_chunks > 0
        assert stats2.indexed_chunks == 0
        # embed_texts should NOT be called again
        assert mock_embedder.embed_texts.call_count == call_count_after_first

    async def test_force_reindex_reembeds(self, components, memory_dir):
        """force=True should re-embed even unchanged content."""
        md_path = memory_dir / "forced.md"
        md_path.write_text("# Force\n\nForce reindex content.\n", encoding="utf-8")

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        await components.index_engine.index_file(md_path)
        call_count_first = mock_embedder.embed_texts.call_count

        stats2 = await components.index_engine.index_file(md_path, force=True)
        assert stats2.indexed_chunks > 0
        assert mock_embedder.embed_texts.call_count > call_count_first

    async def test_unsupported_extension_ignored(self, components, memory_dir):
        """Files with unsupported extensions should return zero chunks."""
        txt_path = memory_dir / "readme.txt"
        txt_path.write_text("Plain text content", encoding="utf-8")

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        stats = await components.index_engine.index_file(txt_path)
        assert stats.total_chunks == 0
        assert stats.indexed_chunks == 0

    async def test_nonexistent_file(self, components, memory_dir):
        """Non-existent file should return zero counts, not raise."""
        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        fake_path = memory_dir / "does_not_exist.md"
        stats = await components.index_engine.index_file(fake_path)
        assert stats.total_chunks == 0
        assert stats.indexed_chunks == 0

    async def test_namespace_applied_to_indexed_chunks(self, components, memory_dir):
        """Explicit namespace should be applied to stored chunks."""
        md_path = memory_dir / "ns_test.md"
        md_path.write_text("# NS Test\n\nNamespaced content.\n", encoding="utf-8")

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        await components.index_engine.index_file(md_path, namespace="project-alpha")

        # Verify through storage that chunks have the namespace
        hashes = await components.storage.get_chunk_hashes(md_path)
        assert len(hashes) > 0  # chunks were stored

    async def test_force_reindex_preserves_chunk_id_for_unchanged(self, components, memory_dir):
        """force=True must keep the same UUID for chunks whose content_hash is unchanged.

        Regression guard for ADR-0005: pre-fix the force path called
        ``delete_by_source`` + fresh ``upsert_chunks``, which generated new
        UUIDs for chunks the caller had no intent to modify.
        """
        md_path = memory_dir / "two_chunks.md"
        md_path.write_text(
            "# Section A\n\n" + ("Body for section A.\n" * 12) + "\n"
            "# Section B\n\n" + ("Body for section B.\n" * 12) + "\n",
            encoding="utf-8",
        )

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        await components.index_engine.index_file(md_path)
        # Bind id to content so an ID swap (assigning A's id to B and vice
        # versa) fails the test even though the set of ids would still match.
        binding_before = {
            c.content: str(c.id) for c in await components.storage.list_chunks_by_source(md_path)
        }
        assert len(binding_before) >= 2

        await components.index_engine.index_file(md_path, force=True)
        binding_after = {
            c.content: str(c.id) for c in await components.storage.list_chunks_by_source(md_path)
        }

        assert binding_after == binding_before

    async def test_force_reindex_preserves_access_count(self, components, memory_dir):
        """force=True must keep ``access_count`` for chunks whose content is unchanged.

        Regression guard for ADR-0005: pre-fix the force path reset access
        stats to schema defaults because ``delete_by_source`` removed the
        row before fresh insertion.
        """
        md_path = memory_dir / "two_chunks_access.md"
        md_path.write_text(
            "# Section A\n\n" + ("Body for section A.\n" * 12) + "\n"
            "# Section B\n\n" + ("Body for section B.\n" * 12) + "\n",
            encoding="utf-8",
        )

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        await components.index_engine.index_file(md_path)
        chunks_before = await components.storage.list_chunks_by_source(md_path)
        assert len(chunks_before) >= 2
        # Bump access_count on the second chunk only
        target_id = chunks_before[1].id
        await components.storage.increment_access([target_id])

        ac_before = (await components.storage.get_access_counts([target_id]))[str(target_id)]
        assert ac_before == 1

        await components.index_engine.index_file(md_path, force=True)

        ac_after = (await components.storage.get_access_counts([target_id]))[str(target_id)]
        assert ac_after == 1, (
            f"force-reindex should preserve access_count for unchanged chunks, "
            f"got {ac_after} (pre-fix would be 0)"
        )

    async def test_force_reindex_refreshes_line_ranges_for_unchanged(self, components, memory_dir):
        """When a sibling chunk's body shifts line numbers, the unchanged chunk's
        ``start_line`` / ``end_line`` columns must catch up — even with force=True
        (which routes hash-matched chunks through the upsert UPDATE path).
        """
        md_path = memory_dir / "shift.md"
        md_path.write_text(
            "# Section A\n\n" + ("Body for section A.\n" * 5) + "\n"
            "# Section B\n\n" + ("Body for section B.\n" * 5) + "\n",
            encoding="utf-8",
        )

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        await components.index_engine.index_file(md_path)
        chunks_before = sorted(
            await components.storage.list_chunks_by_source(md_path),
            key=lambda c: c.metadata.start_line,
        )
        assert len(chunks_before) >= 2
        b_chunk_before = chunks_before[1]
        b_id = b_chunk_before.id
        b_start_before = b_chunk_before.metadata.start_line

        # Insert extra lines in section A so B's start_line shifts.
        md_path.write_text(
            "# Section A\n\n" + ("Body for section A.\n" * 15) + "\n"
            "# Section B\n\n" + ("Body for section B.\n" * 5) + "\n",
            encoding="utf-8",
        )
        await components.index_engine.index_file(md_path, force=True)

        b_chunk_after = await components.storage.get_chunk(b_id)
        assert b_chunk_after is not None, "B chunk lost identity across force-reindex"
        assert b_chunk_after.metadata.start_line > b_start_before, (
            f"B.start_line should reflect the upward shift; "
            f"before={b_start_before}, after={b_chunk_after.metadata.start_line}"
        )

    async def test_mem_edit_preserves_sibling_chunk_metadata(self, components, memory_dir):
        """End-to-end regression: mem_edit on chunk A leaves chunk B's
        ``access_count`` and ``id`` intact (the spike scenario from ADR-0005).

        Uses the same call pattern as ``server.tools.memory_crud.mem_edit``:
        mutate the source file, then ``index_file(force=True)``.
        """
        md_path = memory_dir / "edit_pair.md"
        md_path.write_text(
            "# Section A\n\n" + ("Body for section A.\n" * 8) + "\n"
            "# Section B\n\n" + ("Body for section B.\n" * 8) + "\n",
            encoding="utf-8",
        )

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        await components.index_engine.index_file(md_path)
        chunks = sorted(
            await components.storage.list_chunks_by_source(md_path),
            key=lambda c: c.metadata.start_line,
        )
        assert len(chunks) >= 2
        b_id = chunks[1].id
        await components.storage.increment_access([b_id])

        # Simulate mem_edit's body-only edit on chunk A: rewrite section A,
        # leave section B byte-identical, then index_file(force=True).
        md_path.write_text(
            "# Section A\n\n" + ("Body for section A. EDITED.\n" * 8) + "\n"
            "# Section B\n\n" + ("Body for section B.\n" * 8) + "\n",
            encoding="utf-8",
        )
        await components.index_engine.index_file(md_path, force=True)

        b_after = await components.storage.get_chunk(b_id)
        assert b_after is not None, "Sibling B should keep its UUID across mem_edit"
        ac = (await components.storage.get_access_counts([b_id]))[str(b_id)]
        assert ac == 1, f"Sibling B.access_count should be preserved; got {ac}"

    async def test_force_reindex_deletes_vanished_chunks(self, components, memory_dir):
        """force=True must drop rows whose hash no longer appears in the file.

        Pre-fix this was guaranteed by ``delete_by_source`` (blanket DELETE);
        post-fix the same end state must hold via ``compute_diff``'s
        ``to_delete`` set + ``delete_chunks``.
        """
        md_path = memory_dir / "vanish.md"
        md_path.write_text(
            "# Section A\n\n" + ("Body for section A.\n" * 8) + "\n"
            "# Section B\n\n" + ("Body for section B.\n" * 8) + "\n",
            encoding="utf-8",
        )

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        await components.index_engine.index_file(md_path)
        chunks_before = await components.storage.list_chunks_by_source(md_path)
        assert len(chunks_before) >= 2

        # Remove Section B from the file entirely.
        md_path.write_text(
            "# Section A\n\n" + ("Body for section A.\n" * 8) + "\n",
            encoding="utf-8",
        )
        stats = await components.index_engine.index_file(md_path, force=True)

        chunks_after = await components.storage.list_chunks_by_source(md_path)
        assert len(chunks_after) == 1, (
            f"force-reindex should drop vanished chunks; "
            f"got {len(chunks_after)} rows after section B was removed"
        )
        assert stats.deleted_chunks >= 1, (
            f"IndexingStats.deleted_chunks should report removals under force; "
            f"got {stats.deleted_chunks}"
        )


# ===========================================================================
# 8. index_path — directory indexing
# ===========================================================================


class TestIndexPath:
    """Integration tests for index_path with mocked embedder."""

    async def test_index_multiple_files(self, components, memory_dir):
        """Indexing a directory should process all supported files."""
        (memory_dir / "file1.md").write_text("# File 1\n\nContent one.\n", encoding="utf-8")
        (memory_dir / "file2.md").write_text("# File 2\n\nContent two.\n", encoding="utf-8")
        (memory_dir / "file3.json").write_text(
            '{"key": "value", "nested": {"a": 1}}', encoding="utf-8"
        )

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        stats = await components.index_engine.index_path(memory_dir, recursive=True)

        assert stats.total_files >= 2  # at least md files
        assert stats.total_chunks > 0
        assert stats.indexed_chunks > 0

    async def test_index_path_nonexistent(self, components, tmp_path):
        """Non-existent path returns zeroed stats."""
        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        fake = tmp_path / "nonexistent"
        stats = await components.index_engine.index_path(fake)
        assert stats.total_files == 0
        assert stats.total_chunks == 0

    async def test_index_path_stats_correct(self, components, memory_dir):
        """Stats should accurately reflect total files and chunks."""
        (memory_dir / "only.md").write_text("# Only\n\nSingle file.\n", encoding="utf-8")

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        stats = await components.index_engine.index_path(memory_dir)
        assert stats.total_files == 1
        assert stats.indexed_chunks == stats.total_chunks
        assert stats.skipped_chunks == 0
        assert stats.duration_ms > 0


# ===========================================================================
# 9. Incremental indexing — changed content
# ===========================================================================


class TestIncrementalIndexing:
    """Tests for incremental indexing: only changed content re-embedded."""

    async def test_modify_adds_new_chunks(self, components, memory_dir):
        """Modifying a file should re-embed only new/changed chunks."""
        md_path = memory_dir / "evolving.md"
        md_path.write_text("# Section 1\n\nOriginal content.\n", encoding="utf-8")

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        # First index
        await components.index_engine.index_file(md_path)
        first_embed_count = mock_embedder.embed_texts.call_count

        # Modify the file — add a new section
        md_path.write_text(
            "# Section 1\n\nOriginal content.\n\n# Section 2\n\nBrand new section.\n",
            encoding="utf-8",
        )

        stats2 = await components.index_engine.index_file(md_path)
        # Should have new indexed chunks for the changed/added content
        assert stats2.indexed_chunks > 0
        # embed_texts should have been called again
        assert mock_embedder.embed_texts.call_count > first_embed_count

    async def test_delete_section_removes_chunks(self, components, memory_dir):
        """Removing a section should delete its chunks."""
        md_path = memory_dir / "shrinking.md"
        md_path.write_text("# Keep\n\nKeep this.\n\n# Remove\n\nRemove this.\n", encoding="utf-8")

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        await components.index_engine.index_file(md_path)
        hashes_before = await components.storage.get_chunk_hashes(md_path)

        # Remove the second section
        md_path.write_text("# Keep\n\nKeep this.\n", encoding="utf-8")
        stats = await components.index_engine.index_file(md_path)

        hashes_after = await components.storage.get_chunk_hashes(md_path)
        # Fewer chunk hashes after removal
        assert len(hashes_after) <= len(hashes_before)
        assert stats.deleted_chunks > 0

    async def test_empty_file_clears_chunks(self, components, memory_dir):
        """Overwriting a file with empty content should delete all its chunks."""
        md_path = memory_dir / "clearme.md"
        md_path.write_text("# Content\n\nSome data.\n", encoding="utf-8")

        mock_embedder = AsyncMock()
        mock_embedder.embed_texts = AsyncMock(
            side_effect=lambda texts: [[0.1] * 1024 for _ in texts]
        )
        mock_embedder.dimension = 1024
        components.index_engine._embedder = mock_embedder

        await components.index_engine.index_file(md_path)
        hashes = await components.storage.get_chunk_hashes(md_path)
        assert len(hashes) > 0

        # Clear the file
        md_path.write_text("", encoding="utf-8")
        await components.index_engine.index_file(md_path)

        hashes_after = await components.storage.get_chunk_hashes(md_path)
        assert len(hashes_after) == 0


# ===========================================================================
# 10. FileWatcher — basic construction
# ===========================================================================


class TestFileWatcher:
    """Basic tests for FileWatcher initialization."""

    def test_can_create_watcher(self, components):
        """FileWatcher can be instantiated without errors."""
        from memtomem.indexing.watcher import FileWatcher

        watcher = FileWatcher(
            index_engine=components.index_engine,
            config=components.config.indexing,
            debounce_ms=500,
        )
        assert watcher._debounce_s == 0.5
        assert watcher._observer is None
        assert watcher._task is None

    def test_watcher_custom_debounce(self, components):
        """FileWatcher accepts custom debounce_ms."""
        from memtomem.indexing.watcher import FileWatcher

        watcher = FileWatcher(
            index_engine=components.index_engine,
            config=components.config.indexing,
            debounce_ms=2000,
        )
        assert watcher._debounce_s == 2.0

    async def test_startup_backfill_default_off_skips_walk(self, components, memory_dir):
        """``startup_backfill`` defaults to False — guards against the
        PR #295 silent-CPU-block regression. An unset flag MUST NOT
        trigger a walk even when memory_dirs contain unindexed files;
        users opt in explicitly via the wizard or by editing the config.
        """
        from memtomem.indexing.watcher import FileWatcher

        (memory_dir / "unindexed.md").write_text("# never indexed\n", encoding="utf-8")
        assert components.config.indexing.startup_backfill is False, (
            "default must stay False; a True default reintroduces PR #295"
        )

        watcher = FileWatcher(
            index_engine=components.index_engine,
            config=components.config.indexing,
            debounce_ms=100,
        )
        try:
            await watcher.start()
            assert watcher._backfill_task is None
        finally:
            await watcher.stop()

    async def test_startup_backfill_indexes_preexisting_files_when_enabled(
        self, components, memory_dir
    ):
        """When the user opts in via ``startup_backfill=True``, pre-
        existing files get indexed. The watchdog observer only fires on
        change events from the moment it's scheduled, so without this
        opt-in path files that landed before ``start()`` stay invisible
        until manual reindex.
        """
        from memtomem.indexing.watcher import FileWatcher

        md = memory_dir / "preexisting.md"
        md.write_text(
            "# preexisting\n\ncontent that landed before the watcher started\n",
            encoding="utf-8",
        )
        components.config.indexing.startup_backfill = True

        watcher = FileWatcher(
            index_engine=components.index_engine,
            config=components.config.indexing,
            debounce_ms=100,
        )
        try:
            await watcher.start()
            assert watcher._backfill_task is not None
            await watcher._backfill_task
        finally:
            await watcher.stop()

        hashes = await components.storage.get_chunk_hashes(md)
        assert len(hashes) > 0, "opt-in backfill should have indexed the pre-existing file"

    async def test_startup_backfill_logs_walk_progress(self, components, memory_dir, caplog):
        """Opt-in backfill emits a 'walking N memory_dir(s)' line at
        start and a 'complete: M new chunks indexed' line at end.
        Without these the only backfill log was a per-dir summary that
        only fires when something was actually indexed — quiet logs
        on a slow embedder look identical to a hung server, exactly
        the UX failure mode the PR #295 silent startup scan caused.
        """
        import logging

        from memtomem.indexing.watcher import FileWatcher

        (memory_dir / "x.md").write_text("# x", encoding="utf-8")
        components.config.indexing.startup_backfill = True

        watcher = FileWatcher(
            index_engine=components.index_engine,
            config=components.config.indexing,
            debounce_ms=100,
        )
        try:
            with caplog.at_level(logging.INFO, logger="memtomem.indexing.watcher"):
                await watcher.start()
                assert watcher._backfill_task is not None
                await watcher._backfill_task
        finally:
            await watcher.stop()

        msgs = [r.message for r in caplog.records]
        assert any("Startup backfill: walking" in m for m in msgs), msgs
        assert any("Startup backfill complete" in m for m in msgs), msgs

    async def test_startup_backfill_idempotent_on_unchanged_files(self, components, memory_dir):
        """Opt-in backfill on every startup is bounded by changed-file
        count. Content-hash dedup in ``IndexEngine`` makes already-
        indexed files no-ops, so a second backfill produces the same
        chunk count rather than duplicating — users who flip the flag
        don't pay the full embed cost on every restart.
        """
        from memtomem.indexing.watcher import FileWatcher

        md = memory_dir / "stable.md"
        md.write_text("# stable\n\nno changes between runs\n", encoding="utf-8")
        components.config.indexing.startup_backfill = True

        async def _run_one_backfill() -> int:
            w = FileWatcher(
                index_engine=components.index_engine,
                config=components.config.indexing,
                debounce_ms=100,
            )
            try:
                await w.start()
                assert w._backfill_task is not None
                await w._backfill_task
            finally:
                await w.stop()
            return len(await components.storage.get_chunk_hashes(md))

        first = await _run_one_backfill()
        second = await _run_one_backfill()
        assert first > 0
        assert first == second, f"expected backfill idempotent, got {first} -> {second}"


# ===========================================================================
# 11. memory_dir_stats — per-dir index status for the web widget
# ===========================================================================


class _FakeStorageForStats:
    """Minimal storage stub — only ``get_source_files_with_counts`` is
    exercised by ``memory_dir_stats``. Keeps tests free of embedding
    setup while pinning the aggregation semantics."""

    def __init__(self, rows: list[tuple]) -> None:
        # Shape matches ``SqliteBackend.get_source_files_with_counts``:
        # (path, chunk_count, last_updated, namespaces, avg, min, max)
        self._rows = list(rows)

    async def get_source_files_with_counts(self) -> list[tuple]:
        return list(self._rows)


def _row(path: Path, chunks: int) -> tuple:
    return (path, chunks, "2026-04-19", "default", 100, 50, 200)


class TestMemoryDirStats:
    """``memory_dir_stats`` feeds the web widget's per-dir "(N chunks)" /
    "(not indexed)" badges so users can see which ``memory_dirs`` still
    need a manual reindex."""

    async def test_empty_input_returns_empty(self, tmp_path):
        from memtomem.indexing.engine import memory_dir_stats

        storage = _FakeStorageForStats([])
        assert await memory_dir_stats(storage, []) == []

    async def test_missing_dir_reports_exists_false(self, tmp_path):
        from memtomem.indexing.engine import memory_dir_stats

        storage = _FakeStorageForStats([])
        nonexistent = tmp_path / "does-not-exist"
        result = await memory_dir_stats(storage, [nonexistent])
        assert len(result) == 1
        entry = result[0]
        assert entry["exists"] is False
        assert entry["chunk_count"] == 0
        assert entry["source_file_count"] == 0

    async def test_empty_dir_has_zero_counts(self, tmp_path):
        from memtomem.indexing.engine import memory_dir_stats

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()
        storage = _FakeStorageForStats([])
        result = await memory_dir_stats(storage, [empty_dir])
        assert result[0]["exists"] is True
        assert result[0]["chunk_count"] == 0
        assert result[0]["source_file_count"] == 0
        # ``category`` / ``provider`` are always present so the Web UI
        # never has to guess whether the server supplied them.
        assert result[0]["category"] == "user"
        assert result[0]["provider"] == "user"

    async def test_category_reflects_provider_layout(self, tmp_path):
        """A mix of provider-shaped and user paths produces the right
        ``category`` on each entry — the Web UI consumes this verbatim
        instead of running its own regex."""
        from memtomem.indexing.engine import memory_dir_stats

        user = tmp_path / "notes"
        codex = tmp_path / ".codex" / "memories"
        plans = tmp_path / ".claude" / "plans"
        claude_mem = tmp_path / ".claude" / "projects" / "demo" / "memory"
        for d in (user, codex, plans, claude_mem):
            d.mkdir(parents=True)

        storage = _FakeStorageForStats([])
        result = await memory_dir_stats(storage, [user, codex, plans, claude_mem])
        by_path = {r["path"]: r["category"] for r in result}
        assert by_path[str(user)] == "user"
        assert by_path[str(codex)] == "codex"
        assert by_path[str(plans)] == "claude-plans"
        assert by_path[str(claude_mem)] == "claude-memory"

    async def test_provider_tags_every_entry(self, tmp_path):
        """Every entry carries ``provider`` tagged per RFC #304 Phase 1:
        ``{user→user, claude-memory→claude, claude-plans→claude,
        codex→openai}``. Client-side vendor grouping consumes this field
        so it must be present on every response row."""
        from memtomem.indexing.engine import memory_dir_stats

        user = tmp_path / "notes"
        codex = tmp_path / ".codex" / "memories"
        plans = tmp_path / ".claude" / "plans"
        claude_mem = tmp_path / ".claude" / "projects" / "demo" / "memory"
        for d in (user, codex, plans, claude_mem):
            d.mkdir(parents=True)

        storage = _FakeStorageForStats([])
        result = await memory_dir_stats(storage, [user, codex, plans, claude_mem])
        by_path = {r["path"]: r["provider"] for r in result}
        assert by_path[str(user)] == "user"
        assert by_path[str(codex)] == "openai"
        assert by_path[str(plans)] == "claude"
        assert by_path[str(claude_mem)] == "claude"
        # Belt-and-suspenders: no entry should be missing ``provider``.
        assert all("provider" in r for r in result)

    async def test_kind_field_splits_memory_vs_general(self, tmp_path):
        """``kind`` is the single source of truth for the Web UI's
        Memory / General sub-toggle on the Sources page. Provider
        layouts and user-memory folders are ``"memory"``; arbitrary
        user-added RAG folders are ``"general"``. Without this field
        the frontend would have to re-derive the heuristic on its own
        and drift from the server."""
        from memtomem.indexing.engine import memory_dir_stats

        notes = tmp_path / "work" / "docs"
        memories = tmp_path / "memories"
        codex = tmp_path / ".codex" / "memories"
        claude_mem = tmp_path / ".claude" / "projects" / "demo" / "memory"
        for d in (notes, memories, codex, claude_mem):
            d.mkdir(parents=True)

        storage = _FakeStorageForStats([])
        result = await memory_dir_stats(storage, [notes, memories, codex, claude_mem])
        by_path = {r["path"]: r["kind"] for r in result}
        assert by_path[str(notes)] == "general"
        assert by_path[str(memories)] == "memory"
        assert by_path[str(codex)] == "memory"
        assert by_path[str(claude_mem)] == "memory"
        # Defensive: every row must carry ``kind`` so the Web UI never
        # has to guess.
        assert all("kind" in r for r in result)

    async def test_dir_with_indexed_files_is_aggregated(self, tmp_path):
        from memtomem.indexing.engine import memory_dir_stats

        indexed_dir = tmp_path / "indexed"
        indexed_dir.mkdir()
        file_a = indexed_dir / "a.md"
        file_b = indexed_dir / "b.md"
        file_a.write_text("# a")
        file_b.write_text("# b")

        storage = _FakeStorageForStats([_row(file_a, 3), _row(file_b, 5)])
        result = await memory_dir_stats(storage, [indexed_dir])
        assert result[0]["chunk_count"] == 8
        assert result[0]["source_file_count"] == 2

    async def test_per_dir_bucketing(self, tmp_path):
        """Rows under each dir should be attributed only to that dir,
        not leak into sibling entries."""
        from memtomem.indexing.engine import memory_dir_stats

        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        file_in_a = a / "x.md"
        file_in_b = b / "y.md"
        file_in_a.write_text("#")
        file_in_b.write_text("#")

        storage = _FakeStorageForStats([_row(file_in_a, 2), _row(file_in_b, 7)])
        result = await memory_dir_stats(storage, [a, b])
        by_path = {r["path"]: r for r in result}
        assert by_path[str(a)]["chunk_count"] == 2
        assert by_path[str(b)]["chunk_count"] == 7

    async def test_nested_files_count_toward_ancestor_dir(self, tmp_path):
        from memtomem.indexing.engine import memory_dir_stats

        parent = tmp_path / "parent"
        nested = parent / "sub" / "deep"
        nested.mkdir(parents=True)
        deep_file = nested / "note.md"
        deep_file.write_text("# deep")

        storage = _FakeStorageForStats([_row(deep_file, 4)])
        result = await memory_dir_stats(storage, [parent])
        assert result[0]["chunk_count"] == 4
        assert result[0]["source_file_count"] == 1

    async def test_preserves_input_order(self, tmp_path):
        from memtomem.indexing.engine import memory_dir_stats

        a = tmp_path / "a"
        b = tmp_path / "b"
        c = tmp_path / "c"
        for d in (a, b, c):
            d.mkdir()

        storage = _FakeStorageForStats([])
        result = await memory_dir_stats(storage, [c, a, b])
        assert [r["path"] for r in result] == [str(c), str(a), str(b)]

    async def test_path_field_is_expanded_form(self, tmp_path, monkeypatch):
        """Response ``path`` is always the expanded form, even when the
        input has a ``~`` prefix.

        Without this, ``~/memories`` came back raw in the response while
        peer endpoints (``/add``, ``/remove``, ``/open``) all returned
        ``str(Path(p).expanduser().resolve())`` — the web UI's per-row
        metadata lookup compared the two and silently dropped badges
        for any tilde-prefixed entry.
        """
        from memtomem.indexing.engine import memory_dir_stats

        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "memories").mkdir()
        storage = _FakeStorageForStats([])

        result = await memory_dir_stats(storage, ["~/memories"])
        assert result[0]["path"] == str(tmp_path / "memories")
        assert result[0]["path"] != "~/memories"

    async def test_created_at_is_iso_for_existing_dir(self, tmp_path):
        """``created_at`` is the OS filesystem creation time, ISO-8601 UTC.

        Drives the Web UI's "Newest first" / "Oldest first" sort options.
        """
        from memtomem.indexing.engine import memory_dir_stats

        d = tmp_path / "fresh"
        d.mkdir()
        storage = _FakeStorageForStats([])
        result = await memory_dir_stats(storage, [d])
        created = result[0]["created_at"]
        assert isinstance(created, str)
        # ISO-8601 with timezone (e.g., "2026-04-29T10:30:00+00:00").
        assert "T" in created and ("+" in created or created.endswith("Z"))

    async def test_created_at_none_when_dir_missing(self, tmp_path):
        """A registered ``memory_dir`` that doesn't exist on disk should
        not crash — it just reports ``created_at=None`` (and the existing
        ``exists=False``)."""
        from memtomem.indexing.engine import memory_dir_stats

        gone = tmp_path / "vanished"
        storage = _FakeStorageForStats([])
        result = await memory_dir_stats(storage, [gone])
        assert result[0]["exists"] is False
        assert result[0]["created_at"] is None

    async def test_last_indexed_is_max_updated_at_under_dir(self, tmp_path):
        """``last_indexed`` aggregates ``MAX(updated_at)`` over source
        files under the dir prefix — not just the latest row in the
        whole table."""
        from memtomem.indexing.engine import memory_dir_stats

        d = tmp_path / "active"
        d.mkdir()
        old = d / "old.md"
        new = d / "new.md"
        old.write_text("# old")
        new.write_text("# new")

        # Row shape: (path, chunk_count, last_updated, namespaces, avg, min, max)
        rows = [
            (old, 2, "2026-01-01T00:00:00+00:00", "default", 100, 50, 200),
            (new, 3, "2026-04-29T12:00:00+00:00", "default", 100, 50, 200),
        ]
        storage = _FakeStorageForStats(rows)
        result = await memory_dir_stats(storage, [d])
        assert result[0]["last_indexed"] == "2026-04-29T12:00:00+00:00"

    async def test_last_indexed_none_when_dir_has_no_chunks(self, tmp_path):
        """Empty / un-indexed dirs report ``last_indexed=None`` so the
        Web UI's "Recently indexed" sort can sink them to the bottom."""
        from memtomem.indexing.engine import memory_dir_stats

        d = tmp_path / "empty"
        d.mkdir()
        storage = _FakeStorageForStats([])
        result = await memory_dir_stats(storage, [d])
        assert result[0]["chunk_count"] == 0
        assert result[0]["last_indexed"] is None

    async def test_file_count_zero_when_extensions_not_passed(self, tmp_path):
        """Backward compat — fixtures that call the function without
        ``supported_extensions`` get ``file_count=0`` rather than a
        crash, even when the dir has files."""
        from memtomem.indexing.engine import memory_dir_stats

        d = tmp_path / "with-files"
        d.mkdir()
        (d / "a.md").write_text("# a")
        (d / "b.md").write_text("# b")

        storage = _FakeStorageForStats([])
        result = await memory_dir_stats(storage, [d])
        assert result[0]["file_count"] == 0

    async def test_file_count_walks_disk_when_extensions_passed(self, tmp_path):
        """``file_count`` reflects what's on disk so the Web UI can
        show "{N} files" on un-indexed dirs too — ``source_file_count``
        only counts files with chunks, which is 0 before the user hits
        Reindex."""
        from memtomem.indexing.engine import memory_dir_stats

        d = tmp_path / "fresh"
        d.mkdir()
        (d / "a.md").write_text("# a")
        (d / "sub").mkdir()
        (d / "sub" / "b.md").write_text("# b")
        # Unsupported suffix — should not be counted.
        (d / "ignore.txt").write_text("x")

        storage = _FakeStorageForStats([])
        result = await memory_dir_stats(storage, [d], supported_extensions=frozenset({".md"}))
        assert result[0]["file_count"] == 2
        # ``source_file_count`` stays 0 — no chunks indexed yet.
        assert result[0]["source_file_count"] == 0

    async def test_file_count_zero_for_missing_dir(self, tmp_path):
        """A registered dir that doesn't exist on disk returns
        ``file_count=0`` without crashing the gather."""
        from memtomem.indexing.engine import memory_dir_stats

        gone = tmp_path / "vanished"
        storage = _FakeStorageForStats([])
        result = await memory_dir_stats(storage, [gone], supported_extensions=frozenset({".md"}))
        assert result[0]["exists"] is False
        assert result[0]["file_count"] == 0


# ===========================================================================
# 12. resolve_owning_memory_dir — source → owning dir lookup
# ===========================================================================


class TestResolveOwningMemoryDir:
    """Inverse of :func:`memory_dir_stats`'s bucketing: given a single
    indexed source path, find which configured memory_dir owns it.
    Drives the Web UI's per-source ``memory_dir`` / ``kind`` fields on
    ``GET /api/sources``."""

    def test_source_under_single_dir(self, tmp_path):
        from memtomem.indexing.engine import resolve_owning_memory_dir

        d = tmp_path / "notes"
        d.mkdir()
        f = d / "a.md"
        f.write_text("#")
        owner = resolve_owning_memory_dir(f, [d])
        assert owner == d

    def test_orphan_returns_none(self, tmp_path):
        """A source that isn't under any configured dir is orphan —
        typical after the user removes a dir without purging chunks.
        ``None`` lets the route surface them in the General view rather
        than dropping them silently."""
        from memtomem.indexing.engine import resolve_owning_memory_dir

        d = tmp_path / "registered"
        d.mkdir()
        elsewhere = tmp_path / "unregistered" / "stray.md"
        elsewhere.parent.mkdir()
        elsewhere.write_text("#")
        assert resolve_owning_memory_dir(elsewhere, [d]) is None

    def test_longest_prefix_wins_for_nested_dirs(self, tmp_path):
        """When a parent dir and one of its children are both registered
        (uncommon but legal — e.g. broad RAG corpus + curated subset),
        the deeper match wins so the source is attributed to the more
        specific group the user explicitly opted into."""
        from memtomem.indexing.engine import resolve_owning_memory_dir

        parent = tmp_path / "corpus"
        child = parent / "curated"
        child.mkdir(parents=True)
        f = child / "doc.md"
        f.write_text("#")

        assert resolve_owning_memory_dir(f, [parent, child]) == child
        # Order in the configured list must not change the answer.
        assert resolve_owning_memory_dir(f, [child, parent]) == child

    def test_sibling_with_shared_prefix_does_not_match(self, tmp_path):
        """``/foo`` must not claim files under ``/foo-bar/...`` — the
        trailing-slash normalisation in :func:`norm_dir_prefix` is what
        prevents this. Pin the behaviour."""
        from memtomem.indexing.engine import resolve_owning_memory_dir

        foo = tmp_path / "foo"
        foo_bar = tmp_path / "foo-bar"
        foo.mkdir()
        foo_bar.mkdir()
        sibling_file = foo_bar / "x.md"
        sibling_file.write_text("#")
        assert resolve_owning_memory_dir(sibling_file, [foo]) is None

    def test_tilde_input_is_expanded(self, tmp_path, monkeypatch):
        """The route hands ``config.indexing.memory_dirs`` through
        verbatim (entries may be ``~/...`` form). The helper must
        expand both sides before comparing, otherwise tilde-prefixed
        configured dirs would never match concrete source paths."""
        from memtomem.indexing.engine import resolve_owning_memory_dir

        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "memories").mkdir()
        f = tmp_path / "memories" / "note.md"
        f.write_text("#")
        owner = resolve_owning_memory_dir(f, ["~/memories"])
        assert owner == (tmp_path / "memories")
