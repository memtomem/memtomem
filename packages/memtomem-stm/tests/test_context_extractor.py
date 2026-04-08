"""Tests for ContextExtractor — query extraction from tool arguments."""

from __future__ import annotations

from memtomem_stm.surfacing.config import SurfacingConfig, ToolSurfacingConfig
from memtomem_stm.surfacing.context_extractor import ContextExtractor


def _extract(
    tool: str = "read_file",
    arguments: dict | None = None,
    **config_kwargs,
) -> str | None:
    ext = ContextExtractor()
    cfg = SurfacingConfig(**config_kwargs)
    return ext.extract_query("server", tool, arguments or {}, cfg)


class TestQueryTemplate:
    def test_template_substitution(self):
        cfg_kwargs = {
            "context_tools": {
                "read_file": ToolSurfacingConfig(query_template="file {arg.path}")
            }
        }
        result = _extract("read_file", {"path": "/src/main.py"}, **cfg_kwargs)
        assert result == "file /src/main.py"

    def test_template_with_server_and_tool(self):
        cfg_kwargs = {
            "context_tools": {
                "search": ToolSurfacingConfig(query_template="{server} {tool_name} {arg.q}")
            }
        }
        result = _extract("search", {"q": "hello"}, **cfg_kwargs)
        assert result == "server search hello"


class TestContextQuery:
    def test_context_query_argument(self):
        result = _extract("any_tool", {"_context_query": "find authentication code", "path": "x"})
        assert result == "find authentication code"

    def test_empty_context_query_falls_through(self):
        result = _extract("any_tool", {"_context_query": "", "path": "/src/main.py is a test file"})
        assert result is not None
        assert "/src/main.py" in result


class TestHeuristicExtraction:
    def test_semantic_string_args(self):
        result = _extract("tool", {"path": "/src/main.py", "query": "search term"})
        # Path is tokenized: /src/main.py → "src main py"
        assert "main" in result
        assert "search term" in result

    def test_skips_internal_args(self):
        result = _extract("tool", {"_internal": "skip", "path": "/src/main.py"})
        assert "_internal" not in (result or "")
        assert "skip" not in (result or "")
        # Path tokenized, so "main" should be present
        if result:
            assert "main" in result

    def test_skips_identifiers(self):
        result = _extract("tool", {
            "id": "550e8400-e29b-41d4-a716-446655440000",
            "name": "actual content with enough words",
        })
        assert "550e8400" not in (result or "")
        assert "actual content" in result

    def test_skips_hex_strings(self):
        result = _extract("tool", {"hash": "a" * 32, "title": "real query with enough words"})
        assert "real query" in result

    def test_falls_back_to_tool_name(self, surfacing_config):
        # With min_query_tokens=1 to avoid the 3-token minimum filter
        result = _extract("search_repositories", {}, min_query_tokens=1)
        assert result == "search repositories"

    def test_returns_none_for_short_query(self, surfacing_config):
        result = _extract("x", {"a": "ab"}, min_query_tokens=3)
        assert result is None


class TestIsIdentifier:
    def test_uuid(self):
        assert ContextExtractor._is_identifier("550e8400-e29b-41d4-a716-446655440000")

    def test_hex_string(self):
        assert ContextExtractor._is_identifier("a" * 24)

    def test_boolean_literals(self):
        assert ContextExtractor._is_identifier("true")
        assert ContextExtractor._is_identifier("False")
        assert ContextExtractor._is_identifier("null")
        assert ContextExtractor._is_identifier("None")

    def test_normal_string(self):
        assert not ContextExtractor._is_identifier("hello world")


class TestFirstSentence:
    def test_truncates_at_period(self):
        assert ContextExtractor._first_sentence("Hello. World.", max_chars=200) == "Hello."

    def test_respects_max_chars(self):
        result = ContextExtractor._first_sentence("A" * 300, max_chars=100)
        assert len(result) == 100
