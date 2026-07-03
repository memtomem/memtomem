"""Tests for mem_do meta-tool and tool_registry."""

import json

from memtomem.server.tool_registry import ACTIONS
from memtomem.server.tools.meta import _ALIASES, _help, mem_do
from memtomem.server.tools.status_config import mem_version


class TestToolRegistry:
    def test_actions_registered(self):
        """All non-core tools should be registered."""
        assert len(ACTIONS) >= 50

    def test_all_categories_present(self):
        categories = {info.category for info in ACTIONS.values()}
        expected = {
            "crud",
            "namespace",
            "tags",
            "sessions",
            "scratch",
            "relations",
            "analytics",
            "maintenance",
            "policy",
            "entity",
            "multi_agent",
            "importers",
            "ingest",
            "procedures",
            "advanced",
            "context",
            "search",
            "schedule",
        }
        assert categories == expected

    def test_action_has_description(self):
        for name, info in ACTIONS.items():
            assert info.description, f"Action '{name}' missing description"

    def test_action_fn_is_callable(self):
        for name, info in ACTIONS.items():
            assert callable(info.fn), f"Action '{name}' fn is not callable"

    def test_idempotency_key_param_documented(self):
        """The idempotency_key param (issue #1573) surfaces in mem_do detail
        for the @register-ed write tools — the registry parses per-param docs
        from the Args: docstring, so a missing/undocumented param would drop
        it from the meta-tool help."""
        for name in ("batch_add", "agent_share"):
            info = ACTIONS.get(name)
            assert info is not None, f"expected '{name}' registered"
            doc = info.param_docs.get("idempotency_key")
            assert doc, f"'{name}' missing idempotency_key param doc"

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


class TestAliasInvariants:
    """Guards for the ``_ALIASES`` map (ADR-0022 PR2 review)."""

    def test_no_alias_shadows_a_registered_action(self):
        """An alias key MUST NOT equal a real action name.

        ``mem_do`` resolves aliases FIRST (``resolved = _ALIASES.get(action,
        action)``) before the registry lookup, so an alias whose key matches a
        registered action silently HIJACKS that action. A "version" alias for
        ``context_version`` did exactly this to ``mem_version`` (the
        memtomem-stm protocol-negotiation action) and was caught in review.
        This fails loudly if any future alias re-introduces the collision.
        """
        collisions = sorted(set(_ALIASES) & set(ACTIONS))
        assert not collisions, f"alias keys shadow real actions: {collisions}"

    def test_every_alias_target_is_a_real_action(self):
        """Each alias must resolve to an action that actually exists."""
        dangling = sorted(t for t in _ALIASES.values() if t not in ACTIONS)
        assert not dangling, f"aliases point at non-existent actions: {dangling}"


class TestMemDoProtocolNegotiation:
    """``mem_do('version')`` must reach ``mem_version``, not a context tool.

    Pins the contract memtomem-stm depends on. The existing ``TestMemVersion``
    only calls ``mem_version()`` directly, so it stayed green when a "version"
    alias hijacked the ``mem_do`` route — this exercises the route end-to-end.
    """

    async def test_mem_do_version_returns_protocol_json(self):
        result = await mem_do("version")
        parsed = json.loads(result)
        assert "version" in parsed and "capabilities" in parsed


class TestScheduleActions:
    """P2 Phase A: schedule_register/list/run_now/delete via mem_do registry."""

    def test_schedule_actions_registered(self):
        for name in ("schedule_register", "schedule_list", "schedule_run_now", "schedule_delete"):
            assert name in ACTIONS, f"{name} missing from ACTIONS"
            assert ACTIONS[name].category == "schedule"

    def test_schedule_register_param_shape(self):
        info = ACTIONS["schedule_register"]
        # The plumbing-level contract: callers must provide cron + job_kind
        # by keyword, params is optional. Pin so Phase B doesn't silently
        # drop cron= when the spec= field is added.
        assert "cron" in info.params
        assert "job_kind" in info.params
        assert "params" in info.params


class TestContextSurfacePosture:
    """#1510: context install/update and the projects registry are intentionally
    CLI + dev-tier-web only, with NO ``mem_context_*`` MCP verb (ADR-0008
    "Surface coverage" section).

    The web routes exist (``context_mutations.install_asset``/``update_asset``,
    ``context_projects.{add,delete,update}_known_project`` — see
    ``test_web_invariants_registry.py``); the CLI verbs exist
    (``mm context install|update|projects``). Only the MCP surface omits them.
    This pins both the exact registered-context action set and the deliberate
    absence, so adding a verb later must consciously flip this test rather than
    slip in unnoticed."""

    # The 10 @register("context") actions. mem_context_migrate is @mcp.tool()
    # only (a deprecated alias, not @register-ed), so it is not in ACTIONS.
    _EXPECTED_CONTEXT_ACTIONS = frozenset(
        {
            "context_init",
            "context_detect",
            "context_generate",
            "context_diff",
            "context_sync",
            "context_memory_migrate",
            "context_artifact_migrate",
            "context_artifact_transfer",
            "context_version",
            "context_promote",
        }
    )

    def test_registered_context_actions_are_exactly_the_expected_set(self):
        registered = {name for name, info in ACTIONS.items() if info.category == "context"}
        assert registered == self._EXPECTED_CONTEXT_ACTIONS

    def test_no_install_update_or_projects_mcp_verb(self):
        """The posture pin: these three surfaces write into a project tree or
        mutate cross-project enrollment, so they stay CLI/web-only. If you are
        adding one, update _EXPECTED_CONTEXT_ACTIONS, ADR-0008, and
        docs/guides/reference.md in the same change — and replicate the
        sync-eligibility gate (today a web-route-layer check) at the MCP
        boundary."""
        forbidden = {
            name
            for name in ACTIONS
            if name.startswith(("context_install", "context_update", "context_projects"))
        }
        assert forbidden == set(), (
            f"unexpected CLI/web-only context verb(s) exposed on MCP: {sorted(forbidden)}"
        )


class TestMemVersion:
    """Tests for mem_version (mem_do action='version')."""

    def test_version_registered(self):
        """version action should be in the ACTIONS registry."""
        assert "version" in ACTIONS
        assert ACTIONS["version"].category == "advanced"

    async def test_version_returns_valid_json(self):
        result = await mem_version()
        parsed = json.loads(result)
        assert "version" in parsed
        assert "capabilities" in parsed

    async def test_version_matches_package(self):
        from memtomem import __version__

        result = await mem_version()
        parsed = json.loads(result)
        assert parsed["version"] == __version__

    async def test_capabilities_search_formats(self):
        result = await mem_version()
        parsed = json.loads(result)
        formats = parsed["capabilities"]["search_formats"]
        assert "compact" in formats
        assert "verbose" in formats
        assert "structured" in formats
