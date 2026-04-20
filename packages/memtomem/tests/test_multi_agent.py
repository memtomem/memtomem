"""Tests for multi-agent namespace helpers (``mem_agent_*``).

The sanitizer used by ``mem_agent_register`` / ``mem_agent_search`` lives in
``storage/sqlite_namespace.py`` as ``sanitize_namespace_segment`` — shared
with the ingest pipeline. These tests pin the behavior the multi-agent tool
relies on (single-segment `agent_id` sanitization so the generated
``agent-runtime:{id}`` namespace stays at depth 1).
"""

from __future__ import annotations

import pytest

from memtomem.storage.sqlite_namespace import sanitize_namespace_segment


class TestSanitizeNamespaceSegment:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("alpha", "alpha"),
            ("  spaced  ", "spaced"),
            ("foo/bar", "foo_bar"),
            ("a/b/c", "a_b_c"),
            ("name!with?specials", "name_with_specials"),
            ("ok.chars-allowed:1@host", "ok.chars-allowed:1@host"),
            ("with space", "with space"),
            ("한글도허용", "한글도허용"),
        ],
    )
    def test_sanitize_replaces_disallowed(self, raw, expected):
        assert sanitize_namespace_segment(raw) == expected

    def test_sanitize_preserves_allowed_chars(self):
        allowed = "abc_123-xyz.foo:bar@host"
        assert sanitize_namespace_segment(allowed) == allowed

    def test_sanitize_pure_slash_collapses_to_underscore(self):
        """``agent_id="/"`` must not produce ``agent-runtime://`` (double separator)."""
        assert sanitize_namespace_segment("/") == "_"
