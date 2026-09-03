"""Tests for ``mm agent`` (migrate / register / list / search / share / debug-resolve)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.errors import NamespaceConflictError, NamespaceMutationBusyError
from memtomem.storage.base import NamespaceRenameResult


def _mock_components(legacy_namespaces, existing_new_namespaces=(), chunk_counts=None):
    """Storage stub: ``list_namespace_meta`` returns the given namespace set.

    Each row mirrors the real ``NamespaceOps.list_namespace_meta`` shape
    (chunks ∪ namespace_metadata, so a registered-but-empty namespace is
    listed with ``chunk_count`` 0). ``chunk_counts`` overrides the per-
    namespace count — pass 0 to model a metadata-only legacy namespace.
    ``rename_namespace`` returns a fixed result so the CLI sees non-zero
    chunk updates.
    """
    counts = chunk_counts or {}
    rows = [
        {"namespace": ns, "chunk_count": counts.get(ns, 2), "description": "", "color": ""}
        for ns in legacy_namespaces
    ]
    rows.extend(
        {"namespace": ns, "chunk_count": counts.get(ns, 1), "description": "", "color": ""}
        for ns in existing_new_namespaces
    )
    storage = SimpleNamespace(
        list_namespace_meta=AsyncMock(return_value=rows),
        list_namespace_chunk_candidates=AsyncMock(return_value=[]),
        rename_namespace=AsyncMock(
            return_value=NamespaceRenameResult(chunks_moved=2, metadata_renamed=True, merged=False)
        ),
    )
    return SimpleNamespace(storage=storage)


def _patched_cli_components(comp):
    @asynccontextmanager
    async def fake():
        yield comp

    return fake


class TestAgentMigrate:
    def test_no_legacy_namespaces_nothing_to_do(self, monkeypatch):
        comp = _mock_components([])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"])
        assert result.exit_code == 0
        assert "Nothing to migrate" in result.output
        comp.storage.rename_namespace.assert_not_awaited()

    def test_dry_run_lists_without_renaming(self, monkeypatch):
        comp = _mock_components(["agent/alpha", "agent/beta"])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate", "--dry-run"])
        assert result.exit_code == 0
        assert "agent/alpha  ->  agent-runtime:alpha" in result.output
        assert "agent/beta  ->  agent-runtime:beta" in result.output
        assert "dry-run" in result.output
        comp.storage.rename_namespace.assert_not_awaited()

    def test_apply_renames_each_namespace(self, monkeypatch):
        comp = _mock_components(["agent/alpha", "agent/beta"])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"])
        assert result.exit_code == 0
        assert comp.storage.rename_namespace.await_count == 2
        # No collisions in the listing, so nothing to consolidate into and
        # no consent was asked for — these take the plain rename.
        comp.storage.rename_namespace.assert_any_await(
            "agent/alpha", "agent-runtime:alpha", merge=False, candidates=[]
        )
        comp.storage.rename_namespace.assert_any_await(
            "agent/beta", "agent-runtime:beta", merge=False, candidates=[]
        )
        assert "Migration complete" in result.output

    def test_metadata_only_legacy_namespace_is_migrated(self, monkeypatch):
        """A registered-but-empty ``agent/{id}`` has no chunks — still migrates.

        Discovery used to run off ``list_namespaces`` (chunks only), which
        left such a namespace stranded on the legacy prefix.
        """
        comp = _mock_components(["agent/ghost"], chunk_counts={"agent/ghost": 0})
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"])
        assert result.exit_code == 0
        comp.storage.rename_namespace.assert_awaited_once_with(
            "agent/ghost", "agent-runtime:ghost", merge=False, candidates=[]
        )

    def test_dropped_duplicates_are_reported(self, monkeypatch):
        """The migration deletes chunks the destination already had — say so."""
        comp = _mock_components(["agent/alpha"], existing_new_namespaces=["agent-runtime:alpha"])
        comp.storage.rename_namespace = AsyncMock(
            return_value=NamespaceRenameResult(
                chunks_moved=1, metadata_renamed=False, merged=True, duplicates_dropped=3
            )
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate", "--yes"])
        assert result.exit_code == 0
        assert "3 duplicate(s) dropped" in result.output

    def test_existing_target_asks_before_consolidating(self, monkeypatch):
        """Every other surface demands explicit consent to merge; so does this one."""
        comp = _mock_components(["agent/alpha"], existing_new_namespaces=["agent-runtime:alpha"])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"], input="n\n")
        assert result.exit_code == 0
        comp.storage.rename_namespace.assert_not_awaited()

    def test_declining_the_merge_says_nothing_changed(self, monkeypatch):
        comp = _mock_components(["agent/alpha"], existing_new_namespaces=["agent-runtime:alpha"])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"], input="n\n")
        assert "Aborted" in result.output

    def test_accepting_the_prompt_migrates(self, monkeypatch):
        comp = _mock_components(["agent/alpha"], existing_new_namespaces=["agent-runtime:alpha"])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"], input="y\n")
        assert result.exit_code == 0
        comp.storage.rename_namespace.assert_awaited_once_with(
            "agent/alpha", "agent-runtime:alpha", merge=True, candidates=[]
        )

    def test_the_prompt_says_what_it_is_agreeing_to(self, monkeypatch):
        comp = _mock_components(["agent/alpha"], existing_new_namespaces=["agent-runtime:alpha"])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"], input="y\n")
        assert "duplicate chunks are dropped" in result.output

    def test_a_target_that_appeared_since_the_listing_is_not_merged(self, monkeypatch):
        """Consent covered the targets the user was shown, not later arrivals."""
        comp = _mock_components(["agent/alpha"])  # target absent from the listing
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        CliRunner().invoke(cli, ["agent", "migrate"])
        comp.storage.rename_namespace.assert_awaited_once_with(
            "agent/alpha", "agent-runtime:alpha", merge=False, candidates=[]
        )

    def test_busy_pair_stops_nonzero_and_reports_earlier_partial_progress(self, monkeypatch):
        comp = _mock_components(["agent/alpha", "agent/beta"])
        success = NamespaceRenameResult(
            chunks_moved=2,
            metadata_renamed=True,
            merged=False,
        )
        comp.storage.rename_namespace = AsyncMock(
            side_effect=[
                success,
                NamespaceMutationBusyError("snapshot changed"),
                NamespaceMutationBusyError("snapshot changed"),
                NamespaceMutationBusyError("snapshot changed"),
            ]
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["agent", "migrate"])

        assert result.exit_code != 0
        assert "Renamed: agent/alpha" in result.output
        assert "stopped after 1 namespace(s)" in result.output
        assert "current pair agent/beta -> agent-runtime:beta was not changed" in result.output
        assert "Earlier reported renames remain applied" in result.output

    def test_a_late_conflict_is_skipped_and_named(self, monkeypatch):
        comp = _mock_components(["agent/alpha"])
        comp.storage.rename_namespace = AsyncMock(
            side_effect=NamespaceConflictError("target already exists", reason_code="target_exists")
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"])
        assert result.exit_code == 0
        assert "Skipped: agent/alpha" in result.output

    def test_yes_skips_the_merge_confirmation(self, monkeypatch):
        comp = _mock_components(["agent/alpha"], existing_new_namespaces=["agent-runtime:alpha"])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate", "--yes"])
        assert result.exit_code == 0
        comp.storage.rename_namespace.assert_awaited_once()

    def test_no_confirmation_when_nothing_is_merged(self, monkeypatch):
        """A collision-free migration stays non-interactive."""
        comp = _mock_components(["agent/alpha"])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"])
        assert result.exit_code == 0
        comp.storage.rename_namespace.assert_awaited_once()

    def test_dry_run_flags_existing_merge_target(self, monkeypatch):
        """A target that already exists is consolidated — say so before applying."""
        comp = _mock_components(["agent/alpha"], existing_new_namespaces=["agent-runtime:alpha"])
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate", "--dry-run"])
        assert result.exit_code == 0
        assert "merges into existing namespace" in result.output

    def test_ignores_already_migrated_namespaces(self, monkeypatch):
        comp = _mock_components(
            legacy_namespaces=[],
            existing_new_namespaces=["agent-runtime:alpha", "claude-memory:x"],
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = CliRunner().invoke(cli, ["agent", "migrate"])
        assert result.exit_code == 0
        assert "Nothing to migrate" in result.output
        comp.storage.rename_namespace.assert_not_awaited()


def _registry_components(
    namespaces: list[tuple[str, int]] | None = None,
    namespace_meta: list[dict] | None = None,
    shared_meta: dict | None = None,
):
    """Storage stub for the register/list path.

    ``list_namespaces`` returns ``(namespace, count)`` pairs and
    ``list_namespace_meta`` returns the agent meta records. ``get_namespace_meta``
    returns ``shared_meta`` when queried for ``"shared"`` (the only key the
    list command looks up explicitly), and ``None`` otherwise.
    """
    namespaces = namespaces or []
    namespace_meta = namespace_meta or []

    async def _get_meta(ns: str) -> dict | None:
        if ns == "shared":
            return shared_meta
        return None

    storage = SimpleNamespace(
        list_namespaces=AsyncMock(return_value=namespaces),
        list_namespace_meta=AsyncMock(return_value=namespace_meta),
        get_namespace_meta=AsyncMock(side_effect=_get_meta),
        set_namespace_meta=AsyncMock(return_value=None),
    )
    return SimpleNamespace(storage=storage)


class TestAgentRegister:
    def test_register_creates_namespace_and_shared(self, monkeypatch):
        comp = _registry_components(shared_meta=None)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(
            cli, ["agent", "register", "planner", "--description", "the planning agent"]
        )

        assert result.exit_code == 0, result.output
        assert "Agent registered: planner" in result.output
        assert "agent-runtime:planner" in result.output
        # Both the agent NS and shared NS were upserted (shared was missing)
        assert comp.storage.set_namespace_meta.await_count == 2
        first_call = comp.storage.set_namespace_meta.await_args_list[0]
        assert first_call.args[0] == "agent-runtime:planner"
        assert first_call.kwargs["description"] == "the planning agent"

    def test_register_skips_shared_when_already_exists(self, monkeypatch):
        comp = _registry_components(shared_meta={"namespace": "shared"})
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["agent", "register", "coder"])

        assert result.exit_code == 0, result.output
        # Only the agent NS was upserted; shared was left alone
        assert comp.storage.set_namespace_meta.await_count == 1
        assert comp.storage.set_namespace_meta.await_args_list[0].args[0] == "agent-runtime:coder"

    def test_register_rejects_empty_agent_id(self, monkeypatch):
        # Whitespace-only ids fail at ``validate_agent_id`` with the
        # same shared error vocabulary as ``mem_session_start`` and
        # ``mm session start`` (issue #493 — read/write parity).
        comp = _registry_components()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["agent", "register", "   "])

        assert result.exit_code != 0
        assert "invalid agent-id" in result.output
        comp.storage.set_namespace_meta.assert_not_called()


class TestAgentList:
    def test_list_table_groups_agents_and_shared(self, monkeypatch):
        comp = _registry_components(
            namespaces=[
                ("agent-runtime:planner", 5),
                ("agent-runtime:coder", 2),
                ("shared", 3),
                ("default", 8),
            ],
            namespace_meta=[
                {
                    "namespace": "agent-runtime:planner",
                    "description": "planner role",
                    "color": None,
                },
                {"namespace": "agent-runtime:coder", "description": None, "color": "#abcdef"},
                {"namespace": "default", "description": None, "color": None},
            ],
            shared_meta={"namespace": "shared", "description": "shared knowledge"},
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["agent", "list"])

        assert result.exit_code == 0, result.output
        assert "Agents: 2" in result.output
        assert "planner" in result.output
        assert "agent-runtime:planner" in result.output
        assert "planner role" in result.output
        assert "coder" in result.output
        assert "Shared: shared" in result.output
        assert "shared knowledge" in result.output
        # Non-agent namespaces are not surfaced in the table
        assert "default" not in result.output.split("Shared:")[0]

    def test_list_json_machine_readable(self, monkeypatch):
        comp = _registry_components(
            namespaces=[("agent-runtime:planner", 7), ("shared", 2)],
            namespace_meta=[
                {"namespace": "agent-runtime:planner", "description": None, "color": None},
            ],
            shared_meta={"namespace": "shared", "description": None},
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["agent", "list", "--json"])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert {a["agent_id"] for a in payload["agents"]} == {"planner"}
        assert payload["agents"][0]["chunks"] == 7
        assert payload["shared"]["chunks"] == 2

    def test_list_empty_state_message(self, monkeypatch):
        comp = _registry_components()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = CliRunner().invoke(cli, ["agent", "list"])

        assert result.exit_code == 0, result.output
        assert "No agents registered" in result.output


class TestAgentDebugResolve:
    """``mm agent debug-resolve`` is the hidden e2e helper — JSON-only output
    so integration scripts can assert resolved namespaces without standing up
    an MCP client.
    """

    @pytest.mark.parametrize("agent_flag", ("--agent-id", "-a"))
    def test_explicit_agent_id_with_shared(self, agent_flag):
        result = CliRunner().invoke(cli, ["agent", "debug-resolve", agent_flag, "planner"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["agent_namespace"] == "agent-runtime:planner"
        assert payload["resolved_namespace_filter"] == "agent-runtime:planner,shared"

    def test_falls_back_to_current_agent_id(self):
        result = CliRunner().invoke(
            cli,
            [
                "agent",
                "debug-resolve",
                "--current-agent-id",
                "planner",
                "--no-include-shared",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["agent_namespace"] == "agent-runtime:planner"
        assert payload["resolved_namespace_filter"] == "agent-runtime:planner"

    def test_legacy_current_namespace_fallback(self):
        result = CliRunner().invoke(
            cli,
            [
                "agent",
                "debug-resolve",
                "--current-namespace",
                "legacy:project",
                "--no-include-shared",
            ],
        )
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["agent_namespace"] == "legacy:project"
        assert payload["resolved_namespace_filter"] == "legacy:project"

    def test_no_inputs_returns_null_filter(self):
        result = CliRunner().invoke(cli, ["agent", "debug-resolve"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload["agent_namespace"] is None
        assert payload["resolved_namespace_filter"] is None


def _search_components(results=None, namespaces=None):
    """Components stub for ``mm agent search``: a pipeline that records how it
    was called, plus the config the empty-result explainer reads.

    ``namespaces`` is what ``list_namespaces`` reports. It defaults to empty,
    which short-circuits the explainer to the index hint — so a test that
    wants the *explanation* has to say the store holds something.
    """
    from memtomem.search.pipeline import RetrievalStats

    storage = SimpleNamespace(
        get_current_session=AsyncMock(return_value=None),
        count_chunks=AsyncMock(return_value=0),
        list_namespaces=AsyncMock(return_value=list(namespaces or [])),
        list_namespace_meta=AsyncMock(return_value=[]),
    )
    return SimpleNamespace(
        storage=storage,
        search_pipeline=SimpleNamespace(
            search=AsyncMock(return_value=(results or [], RetrievalStats()))
        ),
        config=SimpleNamespace(indexing=SimpleNamespace(project_memory_dirs=[])),
    )


class TestAgentSearch:
    """``mm agent search`` resolves the same two buckets ``mem_agent_search``
    does. The pins are on the namespace filter that reaches retrieval, because
    that filter *is* the feature — everything else is ``mm search``'s.
    """

    def _run(self, monkeypatch, argv, comp=None, session_ns=None):
        comp = comp or _search_components()
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr(
            "memtomem.cli._session_state.resolve_session_write_namespace",
            AsyncMock(return_value=session_ns),
        )
        result = CliRunner().invoke(cli, argv)
        return result, comp

    def _namespace(self, comp):
        return comp.search_pipeline.search.await_args.kwargs["namespace"]

    @pytest.mark.parametrize("agent_flag", ("--agent-id", "-a"))
    def test_explicit_agent_id_merges_the_shared_bucket(self, monkeypatch, agent_flag):
        result, comp = self._run(monkeypatch, ["agent", "search", "deploy", agent_flag, "planner"])
        assert result.exit_code == 0, result.output
        assert self._namespace(comp) == "agent-runtime:planner,shared"

    def test_no_include_shared_searches_the_private_bucket_alone(self, monkeypatch):
        result, comp = self._run(
            monkeypatch,
            ["agent", "search", "deploy", "-a", "planner", "--no-include-shared"],
        )
        assert result.exit_code == 0, result.output
        assert self._namespace(comp) == "agent-runtime:planner"

    def test_shared_namespace_repoints_only_the_shared_leg(self, monkeypatch):
        """ADR-0028: a per-project team merges its own shared bucket, and the
        agent's private scope is untouched by that choice."""
        result, comp = self._run(
            monkeypatch,
            [
                "agent",
                "search",
                "deploy",
                "-a",
                "planner",
                "--shared-namespace",
                "shared:myproj",
            ],
        )
        assert result.exit_code == 0, result.output
        assert self._namespace(comp) == "agent-runtime:planner,shared:myproj"

    def test_without_a_flag_the_active_session_supplies_the_agent(self, monkeypatch):
        result, comp = self._run(
            monkeypatch,
            ["agent", "search", "deploy"],
            session_ns="agent-runtime:coder",
        )
        assert result.exit_code == 0, result.output
        assert self._namespace(comp) == "agent-runtime:coder,shared"

    def test_no_flag_and_no_session_searches_everything(self, monkeypatch):
        """An unresolved agent means "no filter", not "the shared bucket" —
        the same rule ``_resolve_agent_namespace`` returning ``None`` sets on
        the MCP side."""
        result, comp = self._run(monkeypatch, ["agent", "search", "deploy"])
        assert result.exit_code == 0, result.output
        assert self._namespace(comp) is None

    def test_explicit_agent_id_beats_the_session_binding(self, monkeypatch):
        result, comp = self._run(
            monkeypatch,
            ["agent", "search", "deploy", "-a", "planner"],
            session_ns="agent-runtime:coder",
        )
        assert result.exit_code == 0, result.output
        assert self._namespace(comp) == "agent-runtime:planner,shared"

    @pytest.mark.parametrize(
        "argv",
        [
            ["agent", "search", "deploy", "-a", "../etc"],
            ["agent", "search", "deploy", "--shared-namespace", "bad space"],
        ],
    )
    def test_hostile_names_are_refused_before_the_store_opens(self, monkeypatch, argv):
        """The components block must never be *entered*, not merely never
        searched — validation moved inside an already-open store would still
        leave the process paying to open one for an argument it rejects."""

        @asynccontextmanager
        async def exploding():
            raise AssertionError("components opened for an invalid argument")
            yield  # pragma: no cover - unreachable, satisfies the CM protocol

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", exploding)
        result = CliRunner().invoke(cli, argv)
        assert result.exit_code != 0
        assert "components opened" not in result.output

    def test_output_is_written_after_the_components_close(self, monkeypatch):
        """Rendering holds stdout, and a consumer that stops reading a pipe
        blocks the writer. Holding the SQLite connection, the embedder and the
        reranker behind that block is a cost the finished query should not
        still be paying, so retrieval releases them first."""
        comp = _search_components()
        state = {"open": False}
        # Every render, not just the last: a stray render inside the block
        # followed by the real one outside would look correct to a check that
        # only reads the final observation.
        open_at_render: list[bool] = []

        @asynccontextmanager
        async def tracking():
            state["open"] = True
            try:
                yield comp
            finally:
                state["open"] = False

        def fake_render(query, fmt, payload):
            open_at_render.append(state["open"])

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", tracking)
        monkeypatch.setattr("memtomem.cli.search.render_search_results", fake_render)
        monkeypatch.setattr(
            "memtomem.cli._session_state.resolve_session_write_namespace",
            AsyncMock(return_value=None),
        )

        result = CliRunner().invoke(cli, ["agent", "search", "deploy", "-a", "planner"])

        assert result.exit_code == 0, result.output
        assert open_at_render == [False]

    def test_an_unresolved_agent_says_the_search_was_widened(self, monkeypatch):
        """No session and no flag resolves the same way a session whose
        binding could not be read does, and both return results from
        namespaces the caller asked to be scoped away from. Silence there is
        the one outcome nobody thinks to check."""
        result, comp = self._run(monkeypatch, ["agent", "search", "deploy"])

        assert result.exit_code == 0, result.output
        assert self._namespace(comp) is None
        assert "no agent resolved" in result.stderr

    def test_a_resolved_agent_says_nothing(self, monkeypatch):
        result, _comp = self._run(monkeypatch, ["agent", "search", "deploy", "-a", "planner"])

        assert result.exit_code == 0, result.output
        assert "no agent resolved" not in result.stderr

    def test_an_empty_result_names_the_resolved_namespace_not_a_flag(self, monkeypatch):
        """This verb has no ``--namespace``.

        The namespace it searches is merged here out of ``--agent-id`` and the
        two shared-bucket options, so reporting it as ``--namespace`` — right
        for ``mm search``, whose helper this shares — answers an empty result
        by naming an option the command rejects. A remediation the reader
        cannot carry out is worse than none.
        """
        comp = _search_components(namespaces=[("default", 7), ("work", 2)])
        result, _comp = self._run(
            monkeypatch, ["agent", "search", "deploy", "-a", "planner"], comp=comp
        )

        assert result.exit_code == 0, result.output
        assert (
            "the resolved agent namespace 'agent-runtime:planner,shared' "
            "matches none of the namespaces" in result.stderr
        )
        assert "--namespace" not in result.stderr

    def test_the_reported_options_are_this_verb_s_own(self, monkeypatch):
        """The inventory branch reports the invocation, so every flag it names
        has to be one ``mm agent search`` accepts."""
        comp = _search_components(namespaces=[("agent-runtime:planner", 3), ("shared", 1)])
        result, _comp = self._run(
            monkeypatch,
            [
                "agent",
                "search",
                "deploy",
                "-a",
                "planner",
                "--shared-namespace",
                "shared",
            ],
            comp=comp,
        )

        assert result.exit_code == 0, result.output
        assert "This query included: --agent-id 'planner', --shared-namespace 'shared'" in (
            result.stderr
        )
        assert "--namespace" not in result.stderr

    def test_a_session_supplied_agent_is_not_reported_as_a_typed_option(self, monkeypatch):
        """``filters`` is the command line, and a session binding was not on
        it. The scope it produced is disclosed separately, below."""
        comp = _search_components(namespaces=[("agent-runtime:coder", 3), ("shared", 1)])
        result, _comp = self._run(
            monkeypatch,
            ["agent", "search", "deploy"],
            comp=comp,
            session_ns="agent-runtime:coder",
        )

        assert result.exit_code == 0, result.output
        assert "This query included" not in result.stderr
        assert "--agent-id 'coder'" not in result.stderr

    def test_a_session_derived_scope_is_disclosed_on_an_empty_result(self, monkeypatch):
        """Dropping the wrong label must not drop the fact it carried.

        Nothing on this command line narrowed anything, so the inventory branch
        reports a healthy index and no options — and says nothing about the
        session binding that scoped the query to two namespaces out of three.
        Before the vocabulary split this leaked out as a mislabelled
        ``--namespace``; the label was wrong, the fact was not.
        """
        comp = _search_components(
            namespaces=[("agent-runtime:coder", 3), ("shared", 1), ("default", 9)]
        )
        result, _comp = self._run(
            monkeypatch,
            ["agent", "search", "deploy"],
            comp=comp,
            session_ns="agent-runtime:coder",
        )

        assert result.exit_code == 0, result.output
        assert (
            "This search was scoped to namespace 'agent-runtime:coder,shared', of which "
            "'agent-runtime:coder' was resolved from the active session and the rest is "
            "the shared bucket" in result.stderr
        )

    def test_the_scope_note_does_not_credit_the_session_with_the_shared_leg(self, monkeypatch):
        """Only the private leg came from the binding. ``--include-shared``'s
        default put ``shared`` there, and ``--shared-namespace`` can put
        something else — crediting the whole merge to the session is the same
        misattribution this note exists to repair."""
        comp = _search_components(namespaces=[("agent-runtime:coder", 3), ("shared", 1)])
        result, _comp = self._run(
            monkeypatch,
            ["agent", "search", "deploy", "--shared-namespace", "shared:myproj"],
            comp=comp,
            session_ns="agent-runtime:coder",
        )

        assert result.exit_code == 0, result.output
        assert "'agent-runtime:coder' was resolved from the active session" in result.stderr
        assert "'agent-runtime:coder,shared:myproj', resolved from" not in result.stderr

    def test_a_private_only_scope_note_claims_no_shared_leg(self, monkeypatch):
        """With ``--no-include-shared`` the merge is the session's namespace
        and nothing else, so the note must not invent a shared half."""
        comp = _search_components(namespaces=[("agent-runtime:coder", 3), ("default", 9)])
        result, _comp = self._run(
            monkeypatch,
            ["agent", "search", "deploy", "--no-include-shared"],
            comp=comp,
            session_ns="agent-runtime:coder",
        )

        assert result.exit_code == 0, result.output
        assert (
            "This search was scoped to namespace 'agent-runtime:coder', resolved from the "
            "active session" in result.stderr
        )
        assert "shared bucket" not in result.stderr

    def test_an_explicit_agent_id_is_not_restated_as_a_scope_note(self, monkeypatch):
        """``--agent-id`` is already in the reported filters. Repeating the
        merge would restate what the reader typed."""
        comp = _search_components(namespaces=[("agent-runtime:planner", 3), ("shared", 1)])
        result, _comp = self._run(
            monkeypatch, ["agent", "search", "deploy", "-a", "planner"], comp=comp
        )

        assert result.exit_code == 0, result.output
        assert "resolved from the active session" not in result.stderr

    def test_an_unresolved_agent_gets_no_scope_note(self, monkeypatch):
        """Nothing was scoped away, so there is nothing to disclose — and the
        "no agent resolved" note already owns that case."""
        comp = _search_components(namespaces=[("default", 9)])
        result, _comp = self._run(monkeypatch, ["agent", "search", "deploy"], comp=comp)

        assert result.exit_code == 0, result.output
        assert "This search was scoped to" not in result.stderr
        assert "no agent resolved" in result.stderr

    def test_no_include_shared_is_reported_without_an_argument(self, monkeypatch):
        comp = _search_components(namespaces=[("agent-runtime:planner", 3)])
        result, _comp = self._run(
            monkeypatch,
            ["agent", "search", "deploy", "-a", "planner", "--no-include-shared"],
            comp=comp,
        )

        assert result.exit_code == 0, result.output
        assert "--no-include-shared" in result.stderr
        assert "--no-include-shared '" not in result.stderr

    @pytest.mark.parametrize(
        ("argv", "reason"),
        [
            (
                ["agent", "search", "deploy", "-a", "planner", "--no-include-shared"],
                "--no-include-shared drops the shared leg it re-points",
            ),
            (
                ["agent", "search", "deploy"],
                "there is no agent scope to merge it with",
            ),
        ],
    )
    def test_an_ignored_shared_namespace_says_which_case_swallowed_it(
        self, monkeypatch, argv, reason
    ):
        """``--shared-namespace`` re-points only the shared leg of the merge,
        so both of these accept it, validate it and then have no leg to give
        it to. Accepting is right; doing it silently reads as "it worked"."""
        result, _comp = self._run(monkeypatch, [*argv, "--shared-namespace", "shared:myproj"])

        assert result.exit_code == 0, result.output
        assert f"--shared-namespace 'shared:myproj' was ignored: {reason}." in result.stderr

    def test_an_unresolved_agent_says_no_include_shared_was_disregarded(self, monkeypatch):
        """``--no-include-shared`` drops the shared leg *of the merge*, and with
        no agent there is no merge — the helper answers "no filter" before it
        looks at the flag. Default visibility then hides ``agent-runtime:`` and
        ``archive:`` but not ``shared``, so a query that asked to exclude the
        shared bucket can return rows from it. The widening note above does not
        say which of their options it took with it.
        """
        result, comp = self._run(monkeypatch, ["agent", "search", "deploy", "--no-include-shared"])

        assert result.exit_code == 0, result.output
        assert self._namespace(comp) is None
        assert "--no-include-shared was disregarded" in result.stderr
        assert "an unpinned search still reaches the shared bucket" in result.stderr

    def test_a_resolved_agent_honours_no_include_shared_silently(self, monkeypatch):
        """With an agent the flag does exactly what it says, so there is
        nothing to disclose."""
        result, comp = self._run(
            monkeypatch,
            ["agent", "search", "deploy", "-a", "planner", "--no-include-shared"],
        )

        assert result.exit_code == 0, result.output
        assert self._namespace(comp) == "agent-runtime:planner"
        assert "disregarded" not in result.stderr

    def test_an_honoured_shared_namespace_says_nothing(self, monkeypatch):
        result, comp = self._run(
            monkeypatch,
            ["agent", "search", "deploy", "-a", "planner", "--shared-namespace", "shared:myproj"],
        )

        assert result.exit_code == 0, result.output
        assert self._namespace(comp) == "agent-runtime:planner,shared:myproj"
        assert "was ignored" not in result.stderr

    def test_the_hidden_rows_hint_is_retargeted_to_this_verb(self, monkeypatch):
        """The shared service ends that hint with ``pass namespace="…"``.

        Right on ``mem_search``; unfollowable here, since this verb takes no
        namespace argument — and the glob it suggests reaches every agent's
        private scope, not the one the caller asked about. Same defect as
        naming a ``--namespace`` filter, one layer further out.
        """
        from memtomem.search.pipeline import RetrievalStats

        comp = _search_components(namespaces=[("default", 9)])
        comp.search_pipeline.search = AsyncMock(
            return_value=(
                [],
                RetrievalStats(
                    hidden_system_ns=3,
                    hidden_by_prefix={"agent-runtime:": 2, "archive:": 1},
                ),
            )
        )
        result, _comp = self._run(monkeypatch, ["agent", "search", "deploy"], comp=comp)

        assert result.exit_code == 0, result.output
        assert "3 result(s) hidden in system namespaces" in result.stderr
        assert "reach one agent's own scope with --agent-id" in result.stderr
        assert 'namespace="agent-runtime:*"' not in result.stderr

    def test_an_unrelated_hint_survives_the_swap(self, monkeypatch):
        """The swap matches the upstream string by value, rebuilt from the same
        producer and stats, so it replaces one hint and carries the rest.

        Both hints are present here on purpose: with only the one being
        rewritten, "the other hint was not swallowed" is a claim about a list
        of length one, which any implementation satisfies.
        """
        from memtomem.search.pipeline import RetrievalStats

        comp = _search_components(namespaces=[("default", 9)])
        comp.search_pipeline.search = AsyncMock(
            return_value=(
                [],
                RetrievalStats(
                    dense_candidates=0,
                    dense_suppressed_mismatch=True,
                    mismatch_detail={
                        "dimension_mismatch": True,
                        "stored": {"provider": "none", "model": "", "dimension": 0},
                        "configured": {
                            "provider": "onnx",
                            "model": "bge-small-en-v1.5",
                            "dimension": 384,
                        },
                    },
                    hidden_system_ns=3,
                    hidden_by_prefix={"agent-runtime:": 2, "archive:": 1},
                ),
            )
        )
        result, _comp = self._run(monkeypatch, ["agent", "search", "deploy"], comp=comp)

        assert result.exit_code == 0, result.output
        # The one that was rewritten.
        assert "reach one agent's own scope with --agent-id" in result.stderr
        assert 'namespace="agent-runtime:*"' not in result.stderr
        # The one that was not: still whatever the service said about it.
        assert "dense retrieval did not contribute to this query" in result.stderr

    def test_json_format_is_a_bare_list(self, monkeypatch):
        """``--format json`` has to stay pipeable into ``mm agent share``."""
        result, _comp = self._run(
            monkeypatch, ["agent", "search", "deploy", "-a", "planner", "--format", "json"]
        )
        assert result.exit_code == 0, result.output
        assert json.loads(result.stdout) == []
