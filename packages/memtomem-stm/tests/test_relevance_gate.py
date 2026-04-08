"""Tests for RelevanceGate — surfacing eligibility decisions."""

from __future__ import annotations

from memtomem_stm.surfacing.config import SurfacingConfig, ToolSurfacingConfig
from memtomem_stm.surfacing.relevance import RelevanceGate


def _gate(**kwargs) -> RelevanceGate:
    return RelevanceGate(SurfacingConfig(**kwargs))


class TestRelevanceGateBasic:
    def test_returns_false_when_disabled(self):
        gate = _gate(enabled=False)
        assert not gate.should_surface("s", "read_file", "query")

    def test_returns_false_when_query_is_none(self):
        gate = _gate()
        assert not gate.should_surface("s", "read_file", None)

    def test_returns_true_for_normal_read_tool(self):
        gate = _gate()
        assert gate.should_surface("s", "read_file", "some query here")


class TestRelevanceGateExclusions:
    def test_excluded_by_pattern(self):
        gate = _gate(exclude_tools=["*search*"])
        assert not gate.should_surface("s", "search_code", "query text")

    def test_excluded_by_prefixed_name(self):
        gate = _gate(exclude_tools=["gh__*"])
        assert not gate.should_surface("gh", "list_repos", "query text")

    def test_write_tool_heuristic(self):
        gate = _gate()
        for tool in ["write_file", "create_issue", "delete_branch", "push_commit", "send_message", "remove_label"]:
            assert not gate.should_surface("s", tool, "query text"), f"Should skip {tool}"

    def test_read_tool_passes(self):
        gate = _gate(cooldown_seconds=0.0)
        assert gate.should_surface("s", "read_file", "query about reading files")
        assert gate.should_surface("s", "list_repos", "query about listing repos")
        assert gate.should_surface("s", "get_issue", "query about getting issues")


class TestRelevanceGatePerTool:
    def test_per_tool_disabled(self):
        gate = _gate(context_tools={"read_file": ToolSurfacingConfig(enabled=False)})
        assert not gate.should_surface("s", "read_file", "query text")

    def test_per_tool_enabled(self):
        gate = _gate(context_tools={"read_file": ToolSurfacingConfig(enabled=True)})
        assert gate.should_surface("s", "read_file", "query text")


class TestRelevanceGateRateLimit:
    def test_rate_limit_exceeded(self):
        gate = _gate(max_surfacings_per_minute=3)
        for i in range(3):
            assert gate.should_surface("s", f"tool_{i}", f"different query {i}")
        # 4th should be rejected
        assert not gate.should_surface("s", "tool_x", "another different query")


class TestRelevanceGateCooldown:
    def test_duplicate_query_rejected(self):
        gate = _gate(cooldown_seconds=10.0)
        assert gate.should_surface("s", "t1", "exact same query text")
        assert not gate.should_surface("s", "t2", "exact same query text")

    def test_different_query_accepted(self):
        gate = _gate(cooldown_seconds=10.0)
        assert gate.should_surface("s", "t1", "first query about topic A")
        assert gate.should_surface("s", "t2", "completely different query about topic B")


class TestJaccardSimilarity:
    def test_identical_strings(self):
        assert RelevanceGate._jaccard_similarity("hello world", "hello world") == 1.0

    def test_disjoint_strings(self):
        assert RelevanceGate._jaccard_similarity("hello world", "foo bar") == 0.0

    def test_partial_overlap(self):
        sim = RelevanceGate._jaccard_similarity("hello world foo", "hello world bar")
        assert 0.3 < sim < 0.8

    def test_empty_string(self):
        assert RelevanceGate._jaccard_similarity("", "hello") == 0.0
        assert RelevanceGate._jaccard_similarity("hello", "") == 0.0
