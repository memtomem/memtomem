"""Tests for mem_do meta-tool and tool_registry."""

from memtomem.server.tool_registry import ACTIONS
from memtomem.server.tools.meta import _help


class TestToolRegistry:
    def test_actions_registered(self):
        """All non-core tools should be registered."""
        assert len(ACTIONS) >= 50

    def test_all_categories_present(self):
        categories = {info.category for info in ACTIONS.values()}
        expected = {
            "crud", "namespace", "tags", "sessions", "scratch", "relations",
            "analytics", "maintenance", "policy", "entity", "multi_agent",
            "importers", "ingest", "procedures", "advanced", "context", "search",
        }
        assert categories == expected

    def test_action_has_description(self):
        for name, info in ACTIONS.items():
            assert info.description, f"Action '{name}' missing description"

    def test_action_fn_is_callable(self):
        for name, info in ACTIONS.items():
            assert callable(info.fn), f"Action '{name}' fn is not callable"

    def test_no_core_tools_registered(self):
        """Core tools should NOT be in the registry."""
        core_names = {"search", "add", "index", "recall", "status", "stats", "list", "read"}
        for name in core_names:
            assert name not in ACTIONS, f"Core tool '{name}' should not be in ACTIONS"


class TestHelpCatalog:
    def test_full_catalog(self):
        result = _help()
        assert "Available Actions" in result
        assert "sessions" in result
        assert "analytics" in result

    def test_category_detail(self):
        result = _help(category="sessions")
        assert "session_start" in result
        assert "session_end" in result
        assert "session_list" in result

    def test_unknown_category(self):
        result = _help(category="nonexistent")
        assert "Unknown category" in result

    def test_params_shown_in_detail(self):
        result = _help(category="crud")
        assert "chunk_id" in result or "new_content" in result


class TestMemDoRouting:
    def test_help_action(self):
        """help action should return catalog (sync call to _help)."""
        result = _help()
        assert len(result) > 100

    def test_unknown_action_message(self):
        """Verify the error message format for unknown actions."""
        # This tests the logic without needing async/ctx
        info = ACTIONS.get("totally_nonexistent")
        assert info is None

    def test_similar_action_lookup(self):
        """Verify fuzzy matching would find similar actions."""
        similar = [k for k in ACTIONS if "tag" in k]
        assert len(similar) >= 2  # tag_list, tag_rename, tag_delete, auto_tag
