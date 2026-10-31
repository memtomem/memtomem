"""``mem_ns_rename`` tool surface: outcome wording and the merge consent gate.

The storage contract is pinned in ``test_server_tools_org.py``; this file
covers what the *tool* says and accepts. Two things matter here:

* The reported chunk count is not the whole outcome — a namespace that
  exists only as metadata renames with ``0 chunks moved``, and a merge
  moves rows into a namespace that already had some. The message has to
  say which happened, or "0" reads as "nothing happened" (issue #1874).
* ``merge=True`` consolidates two namespaces and drops the source's
  metadata row. That is consent-shaped, so it must be a literal boolean
  on both doors into this function — the FastMCP model (``StrictBool``)
  and the ``mem_do`` dispatcher, which forwards ``params`` unvalidated.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from memtomem.errors import NamespaceMutationBusyError
from memtomem.server.context import AppContext
from memtomem.server.tools.meta import mem_do
from memtomem.server.tools.namespace import mem_ns_rename

from helpers import make_chunk
from test_validate_namespace import _StubCtx


@pytest.fixture
def ctx(components):
    return _StubCtx(AppContext.from_components(components))


class TestRenameMessage:
    async def test_reports_moved_chunks(self, ctx, storage):
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        out = await mem_ns_rename(old="src-ns", new="dst-ns", ctx=ctx)
        assert "1 chunks moved" in out

    async def test_metadata_only_rename_says_so(self, ctx, storage):
        """``0 chunks moved`` alone would read as "nothing changed"."""
        await storage.set_namespace_meta("src-ns", description="registered only")
        out = await mem_ns_rename(old="src-ns", new="dst-ns", ctx=ctx)
        assert "0 chunks moved, metadata row renamed" in out

    async def test_merge_is_named_in_the_message(self, ctx, storage):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        out = await mem_ns_rename(old="src-ns", new="dst-ns", merge=True, ctx=ctx)
        assert "merged into existing 'dst-ns'" in out

    async def test_dropped_duplicates_are_reported(self, ctx, storage):
        """A merge that deletes rows must say so — silent deletion is the bug class."""
        src = make_chunk(content="same", namespace="src-ns", source="shared.md")
        dup = make_chunk(content="same", namespace="dst-ns", source="shared.md")
        dup.content_hash = src.content_hash
        await storage.upsert_chunks([src, dup])

        out = await mem_ns_rename(old="src-ns", new="dst-ns", merge=True, ctx=ctx)
        assert "1 duplicate chunk(s) dropped" in out

    async def test_missing_source_does_not_claim_a_rename(self, ctx, storage):
        out = await mem_ns_rename(old="ghost", new="phantom", ctx=ctx)
        assert "not found" in out and "Renamed" not in out


class TestRenameConflict:
    async def test_unstable_snapshot_is_labeled_retryable_and_claims_no_change(
        self, ctx, storage, monkeypatch
    ):
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        raw_writer = AsyncMock(
            side_effect=NamespaceMutationBusyError("snapshot changed; nothing was changed. Retry.")
        )
        monkeypatch.setattr(storage, "rename_namespace", raw_writer)

        out = await mem_ns_rename(old="src-ns", new="dst-ns", ctx=ctx)

        assert out.startswith("Error (retryable):")
        assert "nothing was changed" in out
        assert raw_writer.await_count == 3

    async def test_existing_target_is_refused(self, ctx, storage):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        out = await mem_ns_rename(old="src-ns", new="dst-ns", ctx=ctx)
        assert out.startswith("Error:") and "target already exists" in out

    async def test_refusal_leaves_the_source_alone(self, ctx, storage):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        await mem_ns_rename(old="src-ns", new="dst-ns", ctx=ctx)
        assert dict(await storage.list_namespaces())["src-ns"] == 1

    async def test_refusal_names_the_way_forward(self, ctx, storage):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        out = await mem_ns_rename(old="src-ns", new="dst-ns", ctx=ctx)
        assert "merge=True" in out


class TestMergeConsentGate:
    """``mem_do(action="ns_rename", params={...})`` reaches the body raw."""

    @pytest.mark.parametrize("value", ["true", "True", 1, "false", 0])
    async def test_non_literal_merge_is_refused(self, ctx, storage, value):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        out = await mem_ns_rename(old="src-ns", new="dst-ns", merge=value, ctx=ctx)
        assert out.startswith("Error:") and "literal boolean" in out

    async def test_a_refused_merge_flag_does_not_consolidate(self, ctx, storage):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        await mem_ns_rename(old="src-ns", new="dst-ns", merge="true", ctx=ctx)
        assert dict(await storage.list_namespaces())["src-ns"] == 1


class TestStoredLegacyName:
    """A stored name that fails ``validate_namespace`` can be renamed *out*.

    Writers that never validated (``mm index --namespace``, namespace rules,
    raw MCP ``mem_add``) could store names such as a literal ``!system``,
    which since 0.7.1 is the every-non-system-namespace selector on search
    and recall. Renaming is the way out, so ``old`` gets one exception to the
    #500 gate: the exact name is stored. ``new`` keeps the full gate.
    """

    async def test_literal_system_selector_name_moves_out(self, ctx, storage):
        await storage.upsert_chunks([make_chunk(content="legacy", namespace="!system")])

        out = await mem_ns_rename(old="!system", new="legacy-system", ctx=ctx)

        assert out.startswith("Renamed namespace '!system' -> 'legacy-system' (1 chunks moved")
        stored = dict(await storage.list_namespaces())
        assert stored.get("legacy-system") == 1
        assert "!system" not in stored

    async def test_metadata_only_legacy_name_moves_out(self, ctx, storage):
        # Storage accepts a space in a metadata name; validate_namespace does not.
        await storage.set_namespace_meta("team notes", description="pre-validation")

        out = await mem_ns_rename(old="team notes", new="team-notes", ctx=ctx)

        assert "0 chunks moved, metadata row renamed" in out
        assert await storage.get_namespace_meta("team notes") is None
        assert await storage.get_namespace_meta("team-notes") is not None

    async def test_only_the_exact_stored_name_is_excepted(self, ctx, storage):
        """A *different* invalid name still gets the #500 refusal."""
        await storage.upsert_chunks([make_chunk(content="legacy", namespace="!system")])

        for old in ("!System", "!system ", "x*y"):
            out = await mem_ns_rename(old=old, new="cleanup", ctx=ctx)
            assert out.startswith("Error: invalid namespace"), (old, out)
        assert dict(await storage.list_namespaces()).get("!system") == 1

    async def test_invalid_new_is_refused_even_from_a_stored_legacy_name(self, ctx, storage):
        await storage.upsert_chunks([make_chunk(content="legacy", namespace="!system")])

        out = await mem_ns_rename(old="!system", new="x*y", ctx=ctx)

        assert out.startswith("Error: invalid namespace")
        assert dict(await storage.list_namespaces()).get("!system") == 1

    async def test_stored_empty_name_is_refused_and_force_reindex_moves_it(
        self, ctx, components, tmp_path
    ):
        """``""`` reads as "no filter" to the candidate loader, so it stays refused.

        0.7.0 could store it (``mm index --namespace ''``); the documented way
        out is a forced re-index of the file with an explicit namespace.
        """
        source = tmp_path / "memories" / "empty-ns.md"
        source.write_text("# Empty\n\nindexed with an empty namespace\n")
        assert not (await components.index_engine.index_file(source, namespace="")).errors
        assert "" in dict(await components.storage.list_namespaces())

        out = await mem_ns_rename(old="", new="cleanup", ctx=ctx)
        assert out.startswith("Error: invalid namespace"), out

        stats = await components.index_engine.index_file(source, force=True, namespace="cleanup")
        assert not stats.errors
        rows = await components.storage.list_chunks_by_source(source, limit=None)
        assert rows and {r.metadata.namespace for r in rows} == {"cleanup"}

    async def test_str_subclass_cannot_borrow_a_stored_name(self, ctx, storage):
        class Lookalike(str):
            def __eq__(self, other):
                return other == "!system"

            def __hash__(self):
                return hash("!system")

        await storage.upsert_chunks([make_chunk(content="legacy", namespace="!system")])

        out = await mem_ns_rename(old=Lookalike("!absent"), new="cleanup", ctx=ctx)

        assert out.startswith("Error: invalid namespace"), out
        assert dict(await storage.list_namespaces()).get("!system") == 1

    @pytest.mark.parametrize("old", [123, [], {}, ["!system"]])
    async def test_non_string_old_via_mem_do_is_refused(self, ctx, storage, old):
        """``mem_do`` forwards params raw; SQLite would match ``123`` to TEXT ``"123"``."""
        await storage.set_namespace_meta("123", description="numeric name")
        await storage.upsert_chunks([make_chunk(content="legacy", namespace="!system")])

        out = await mem_do("ns_rename", params={"old": old, "new": "cleanup"}, ctx=ctx)

        assert out.startswith("Error: invalid namespace"), out
        assert await storage.get_namespace_meta("123") is not None
        assert await storage.get_namespace_meta("cleanup") is None

    async def test_string_legacy_name_via_mem_do_moves_out(self, ctx, storage):
        await storage.upsert_chunks([make_chunk(content="legacy", namespace="!system")])

        out = await mem_do("ns_rename", params={"old": "!system", "new": "legacy-system"}, ctx=ctx)

        assert out.startswith("Renamed namespace '!system'"), out

    @pytest.mark.parametrize("force", [False, True])
    async def test_reindex_keeps_the_renamed_namespace(self, ctx, components, tmp_path, force):
        source = tmp_path / "memories" / "legacy.md"
        source.write_text("# Legacy\n\nwritten under a pre-validation namespace\n")
        stats = await components.index_engine.index_file(source, namespace="!system")
        assert not stats.errors

        await mem_ns_rename(old="!system", new="legacy-system", ctx=ctx)
        source.write_text("# Legacy\n\nwritten under a pre-validation namespace, edited\n")
        stats = await components.index_engine.index_file(source, force=force)

        assert not stats.errors
        rows = await components.storage.list_chunks_by_source(source, limit=None)
        assert rows and {r.metadata.namespace for r in rows} == {"legacy-system"}
