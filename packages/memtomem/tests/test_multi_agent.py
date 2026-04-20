"""Tests for multi-agent tool helpers (``mem_agent_*``)."""

from __future__ import annotations

import pytest

from memtomem.server.tools.multi_agent import _sanitize_agent_id


class TestSanitizeAgentId:
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
        assert _sanitize_agent_id(raw) == expected

    def test_sanitize_preserves_allowed_chars(self):
        allowed = "abc_123-xyz.foo:bar@host"
        assert _sanitize_agent_id(allowed) == allowed

    def test_sanitize_pure_separator_collapses_to_underscore(self):
        """``agent_id="/"`` must not produce ``agent//`` (double separator)."""
        assert _sanitize_agent_id("/") == "_"
