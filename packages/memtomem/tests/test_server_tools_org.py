"""Tests for server tool organization functions: namespace, tags, session, scratch."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from helpers import make_chunk
from memtomem.errors import NamespaceConflictError, StorageError


# ---------------------------------------------------------------------------
# Namespace tools
# ---------------------------------------------------------------------------


class TestNamespace:
    async def test_list_namespaces_empty(self, storage):
        result = await storage.list_namespaces()
        assert result == []

    async def test_list_namespaces_counts(self, storage):
        chunks = [
            make_chunk(content="a", namespace="proj-alpha"),
            make_chunk(content="b", namespace="proj-alpha"),
            make_chunk(content="c", namespace="proj-beta"),
        ]
        await storage.upsert_chunks(chunks)
        ns = dict(await storage.list_namespaces())
        assert ns["proj-alpha"] == 2
        assert ns["proj-beta"] == 1

    async def test_rename_namespace(self, storage):
        chunks = [
            make_chunk(content="one", namespace="old-ns"),
            make_chunk(content="two", namespace="old-ns"),
            make_chunk(content="other", namespace="keep-ns"),
        ]
        await storage.upsert_chunks(chunks)
        result = await storage.rename_namespace("old-ns", "new-ns")
        assert result.chunks_moved == 2
        ns = dict(await storage.list_namespaces())
        assert "old-ns" not in ns
        assert ns["new-ns"] == 2
        assert ns["keep-ns"] == 1

    async def test_rename_namespace_not_merged_when_target_free(self, storage):
        await storage.upsert_chunks([make_chunk(content="one", namespace="old-ns")])
        result = await storage.rename_namespace("old-ns", "new-ns")
        assert result.merged is False

    async def test_rename_nonexistent_namespace(self, storage):
        result = await storage.rename_namespace("ghost", "phantom")
        assert result.chunks_moved == 0

    async def test_delete_by_namespace(self, storage):
        chunks = [
            make_chunk(content="del1", namespace="doomed"),
            make_chunk(content="del2", namespace="doomed"),
            make_chunk(content="safe", namespace="keeper"),
        ]
        await storage.upsert_chunks(chunks)
        deleted = await storage.delete_by_namespace("doomed")
        assert deleted == 2
        ns = dict(await storage.list_namespaces())
        assert "doomed" not in ns
        assert ns["keeper"] == 1

    async def test_delete_namespace_empty(self, storage):
        deleted = await storage.delete_by_namespace("nonexistent")
        assert deleted == 0

    async def test_set_and_get_namespace_meta(self, storage):
        await storage.set_namespace_meta("proj-x", description="Project X docs", color="#ff0000")
        meta = await storage.get_namespace_meta("proj-x")
        assert meta is not None
        assert meta["description"] == "Project X docs"
        assert meta["color"] == "#ff0000"
        assert meta["namespace"] == "proj-x"

    async def test_update_namespace_meta(self, storage):
        await storage.set_namespace_meta("proj-y", description="Original")
        await storage.set_namespace_meta("proj-y", description="Updated")
        meta = await storage.get_namespace_meta("proj-y")
        assert meta["description"] == "Updated"

    async def test_get_namespace_meta_nonexistent(self, storage):
        meta = await storage.get_namespace_meta("no-such-ns")
        assert meta is None

    async def test_namespace_meta_partial_update(self, storage):
        await storage.set_namespace_meta("ns-partial", description="desc", color="#000")
        await storage.set_namespace_meta("ns-partial", color="#fff")
        meta = await storage.get_namespace_meta("ns-partial")
        assert meta["description"] == "desc"
        assert meta["color"] == "#fff"

    async def test_list_namespace_meta_includes_registered_empty_namespace(self, storage):
        """``mm agent register <id>`` followed by ``mm agent list`` must show
        the agent even before any chunks land in its namespace.

        Regression: ``list_namespace_meta`` previously sourced rows from
        ``chunks`` only (LEFT JOIN ``namespace_metadata``), so a registered
        namespace with zero chunks was invisible — the user-visible symptom
        was that ``mm agent register planner && mm agent list`` printed
        ``Agents: 0``. Both real-user testing on v0.1.28 and the
        scenario-1 walkthrough hit this. Existing CLI tests stubbed the
        storage so the SQL bug was never exercised
        (``feedback_storage_artifact_false_pass.md``).
        """
        await storage.set_namespace_meta("agent-runtime:planner", description="planner")
        await storage.set_namespace_meta("agent-runtime:coder")  # description default

        meta = await storage.list_namespace_meta()
        by_ns = {m["namespace"]: m for m in meta}

        assert "agent-runtime:planner" in by_ns
        assert by_ns["agent-runtime:planner"]["chunk_count"] == 0
        assert by_ns["agent-runtime:planner"]["description"] == "planner"
        assert "agent-runtime:coder" in by_ns
        assert by_ns["agent-runtime:coder"]["chunk_count"] == 0

    async def test_list_namespace_meta_unions_chunks_and_metadata(self, storage):
        """Namespace appearing in either side of the union must surface.

        Three states the listing must cover, all in one fixture:
        - **metadata only** — registered but no chunks yet (``empty-meta``)
        - **chunks only** — legacy / un-registered chunks (``chunks-only``)
        - **both** — registered AND has chunks (``both``)
        """
        await storage.set_namespace_meta("empty-meta", description="reg only")
        await storage.set_namespace_meta("both", description="reg + chunks", color="#abc")
        await storage.upsert_chunks(
            [
                make_chunk(content="legacy", namespace="chunks-only"),
                make_chunk(content="b1", namespace="both"),
                make_chunk(content="b2", namespace="both"),
            ]
        )

        meta = await storage.list_namespace_meta()
        by_ns = {m["namespace"]: m for m in meta}

        assert by_ns["empty-meta"]["chunk_count"] == 0
        assert by_ns["empty-meta"]["description"] == "reg only"
        assert by_ns["chunks-only"]["chunk_count"] == 1
        assert by_ns["chunks-only"]["description"] == ""  # no metadata row
        assert by_ns["chunks-only"]["color"] == ""  # COALESCE fallback symmetry
        assert by_ns["both"]["chunk_count"] == 2
        assert by_ns["both"]["description"] == "reg + chunks"
        assert by_ns["both"]["color"] == "#abc"

    async def test_namespace_assign_via_upsert(self, storage):
        """Verify chunks in different namespaces are tracked independently."""
        c1 = make_chunk(content="alpha chunk", namespace="ns-a")
        c2 = make_chunk(content="beta chunk", namespace="ns-b")
        c3 = make_chunk(content="another alpha", namespace="ns-a")
        await storage.upsert_chunks([c1, c2, c3])
        ns = dict(await storage.list_namespaces())
        assert ns["ns-a"] == 2
        assert ns["ns-b"] == 1

    @pytest.mark.parametrize(
        "bad",
        ["bad name!", "no\ttab", 'quote"here', "a" * 256, "agent/legacy"],
    )
    async def test_set_namespace_meta_rejects_invalid(self, storage, bad):
        with pytest.raises(StorageError, match="Invalid namespace"):
            await storage.set_namespace_meta(bad, description="x")

    async def test_set_namespace_meta_accepts_agent_runtime_form(self, storage):
        """``agent-runtime:{id}`` is the canonical multi-agent namespace format (#318)."""
        await storage.set_namespace_meta("agent-runtime:alpha", description="multi-agent ns")
        meta = await storage.get_namespace_meta("agent-runtime:alpha")
        assert meta is not None
        assert meta["namespace"] == "agent-runtime:alpha"

    async def test_rename_namespace_rejects_invalid_target(self, storage):
        chunks = [make_chunk(content="x", namespace="source-ns")]
        await storage.upsert_chunks(chunks)
        with pytest.raises(StorageError, match="Invalid namespace"):
            await storage.rename_namespace("source-ns", "bad!target")

    async def test_assign_namespace_rejects_invalid_target(self, storage):
        chunks = [make_chunk(content="x", namespace="orig")]
        await storage.upsert_chunks(chunks)
        with pytest.raises(StorageError, match="Invalid namespace"):
            await storage.assign_namespace("bad\tname", old_namespace="orig")


# ---------------------------------------------------------------------------
# rename_namespace — atomicity and conflict semantics (#1874)
# ---------------------------------------------------------------------------


@pytest.fixture
def fail_on_metadata_update(storage):
    """Install a trigger that aborts the metadata rename of one namespace.

    Fault injection *after* the chunk rows are rewritten — the exact shape
    of the original bug (a PK collision on the metadata rename raised with
    the chunk UPDATE already applied and uncommitted). A test that only
    exercises the up-front refusal would pass even with the rollback
    missing. Installed by the test *after* seeding, and scoped to the
    source namespace so unrelated metadata writes still work — the
    "unrelated later commit" is the whole point of the first test.
    """
    db = storage._get_db()

    def install(namespace: str) -> None:
        db.execute(
            "CREATE TRIGGER _test_meta_boom BEFORE UPDATE ON namespace_metadata "
            f"WHEN OLD.namespace = '{namespace}' "
            "BEGIN SELECT RAISE(ABORT, 'boom'); END"
        )
        db.commit()

    yield install
    db.execute("DROP TRIGGER IF EXISTS _test_meta_boom")
    db.commit()


class TestRenameNamespaceAtomicity:
    async def test_failed_rename_does_not_leak_into_a_later_commit(
        self, storage, fail_on_metadata_update
    ):
        """The reported bug: chunk rows rewritten, metadata rename fails, no rollback.

        The pending UPDATE used to sit on the shared connection until some
        unrelated ``commit()`` flushed it — persisting a rename the caller
        was told had failed.
        """
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        await storage.set_namespace_meta("src-ns", description="d")
        fail_on_metadata_update("src-ns")

        with pytest.raises(StorageError):
            await storage.rename_namespace("src-ns", "dst-ns")

        # An unrelated write that commits the shared connection.
        await storage.set_namespace_meta("third-ns", description="unrelated")

        ns = dict(await storage.list_namespaces())
        assert ns.get("src-ns") == 1
        assert "dst-ns" not in ns

    async def test_failed_rename_inside_outer_transaction_is_undone(
        self, storage, fail_on_metadata_update
    ):
        """Caller swallows the error inside ``transaction()`` — our writes still vanish.

        The savepoint, not the connection-level rollback, is what makes this
        hold: rename does not own the outer transaction and must not tear it
        down, but it also must not leave half its work behind for the outer
        commit to pick up.
        """
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        await storage.set_namespace_meta("src-ns", description="d")
        fail_on_metadata_update("src-ns")

        async with storage.transaction():
            with pytest.raises(StorageError):
                await storage.rename_namespace("src-ns", "dst-ns")
            # …and the caller carries on, committing the outer transaction.

        ns = dict(await storage.list_namespaces())
        assert ns.get("src-ns") == 1
        assert "dst-ns" not in ns

    async def test_failed_rename_keeps_the_callers_earlier_writes(
        self, storage, fail_on_metadata_update
    ):
        """Undo *our* writes only — a connection-level rollback would take the caller's too."""
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        await storage.set_namespace_meta("src-ns", description="d")
        fail_on_metadata_update("src-ns")

        async with storage.transaction():
            await storage.upsert_chunks([make_chunk(content="earlier", namespace="caller-ns")])
            with pytest.raises(StorageError):
                await storage.rename_namespace("src-ns", "dst-ns")

        assert dict(await storage.list_namespaces()).get("caller-ns") == 1

    async def test_successful_rename_is_undone_when_the_caller_aborts(self, storage):
        """Rename must not commit inside a transaction it does not own."""
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])

        with pytest.raises(RuntimeError):
            async with storage.transaction():
                await storage.rename_namespace("src-ns", "dst-ns")
                raise RuntimeError("caller aborts")

        ns = dict(await storage.list_namespaces())
        assert ns.get("src-ns") == 1
        assert "dst-ns" not in ns

    async def test_write_lock_is_taken_before_the_existence_checks(self, storage):
        """``BEGIN IMMEDIATE`` precedes the preflight SELECTs.

        Without the ordering, a concurrent writer could create the target
        between the check and the UPDATE — and every other test here would
        still pass. Mirrors the trace assertion in test_context_memory_migrate.
        """
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        db = storage._get_db()
        trace: list[str] = []
        db.set_trace_callback(lambda sql: trace.append(sql.strip().split("\n", 1)[0]))
        try:
            await storage.rename_namespace("src-ns", "dst-ns")
        finally:
            db.set_trace_callback(None)

        begin_idx = next(i for i, s in enumerate(trace) if "BEGIN IMMEDIATE" in s.upper())
        select_idx = next(i for i, s in enumerate(trace) if s.upper().startswith("SELECT"))
        assert begin_idx < select_idx, f"lock must precede the preflight, trace: {trace}"

    async def test_write_lock_is_taken_inside_an_outer_transaction(self, storage):
        """First statement inside ``transaction()`` still needs its own BEGIN IMMEDIATE.

        ``transaction()`` only flips the backend flag; it does not open a
        SQLite transaction, so gating the lock on *ownership* would silently
        drop it here.
        """
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        db = storage._get_db()
        trace: list[str] = []
        async with storage.transaction():
            db.set_trace_callback(lambda sql: trace.append(sql.strip().split("\n", 1)[0]))
            try:
                await storage.rename_namespace("src-ns", "dst-ns")
            finally:
                db.set_trace_callback(None)

        begin_idx = next(
            (i for i, s in enumerate(trace) if "BEGIN IMMEDIATE" in s.upper()),
            None,
        )
        select_idx = next((i for i, s in enumerate(trace) if s.upper().startswith("SELECT")), None)
        assert begin_idx is not None, (
            f"rename must take the write lock even inside transaction(), trace: {trace}"
        )
        assert select_idx is None or begin_idx < select_idx, (
            f"lock must still precede the preflight, trace: {trace}"
        )

    async def test_no_op_rename_leaves_no_open_transaction(self, storage):
        """A source that holds nothing must not strand the RESERVED lock."""
        await storage.rename_namespace("ghost", "phantom")
        assert storage._get_db().in_transaction is False


class TestRenameNamespaceConflicts:
    async def test_refuses_target_with_metadata_row(self, storage):
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        await storage.set_namespace_meta("dst-ns", description="existing")

        with pytest.raises(NamespaceConflictError, match="target already exists"):
            await storage.rename_namespace("src-ns", "dst-ns")

    async def test_refusal_writes_nothing(self, storage):
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        await storage.set_namespace_meta("dst-ns", description="existing")

        with pytest.raises(NamespaceConflictError):
            await storage.rename_namespace("src-ns", "dst-ns")

        assert dict(await storage.list_namespaces()).get("src-ns") == 1

    async def test_refuses_target_with_chunks_but_no_metadata(self, storage):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        with pytest.raises(NamespaceConflictError):
            await storage.rename_namespace("src-ns", "dst-ns")

    async def test_refuses_rename_onto_itself(self, storage):
        await storage.set_namespace_meta("same-ns", description="keep me")
        with pytest.raises(NamespaceConflictError, match="onto itself"):
            await storage.rename_namespace("same-ns", "same-ns")

    async def test_refuses_rename_onto_itself_even_with_merge(self, storage):
        """The merge branch would delete the sole metadata row (target-wins + drop source)."""
        await storage.set_namespace_meta("same-ns", description="keep me")
        with pytest.raises(NamespaceConflictError):
            await storage.rename_namespace("same-ns", "same-ns", merge=True)
        meta = await storage.get_namespace_meta("same-ns")
        assert meta["description"] == "keep me"

    async def test_missing_source_is_a_no_op_even_when_target_exists(self, storage):
        """Nothing to move is not a conflict — keeps the pinned zero no-op."""
        await storage.upsert_chunks([make_chunk(content="one", namespace="dst-ns")])
        result = await storage.rename_namespace("ghost", "dst-ns")
        assert result.chunks_moved == 0

    async def test_missing_source_leaves_target_untouched(self, storage):
        await storage.upsert_chunks([make_chunk(content="one", namespace="dst-ns")])
        await storage.rename_namespace("ghost", "dst-ns")
        assert dict(await storage.list_namespaces())["dst-ns"] == 1


class TestRenameNamespaceMetadataOnly:
    async def test_metadata_only_source_reports_zero_chunks(self, storage):
        await storage.set_namespace_meta("meta-ns", description="registered only")
        result = await storage.rename_namespace("meta-ns", "renamed-ns")
        assert result.chunks_moved == 0

    async def test_metadata_only_source_moves_the_row(self, storage):
        await storage.set_namespace_meta("meta-ns", description="registered only")
        result = await storage.rename_namespace("meta-ns", "renamed-ns")
        assert result.metadata_renamed is True

    async def test_metadata_only_source_clears_the_old_name(self, storage):
        await storage.set_namespace_meta("meta-ns", description="registered only")
        await storage.rename_namespace("meta-ns", "renamed-ns")
        assert await storage.get_namespace_meta("meta-ns") is None

    async def test_metadata_only_source_keeps_its_description(self, storage):
        await storage.set_namespace_meta("meta-ns", description="registered only")
        await storage.rename_namespace("meta-ns", "renamed-ns")
        meta = await storage.get_namespace_meta("renamed-ns")
        assert meta["description"] == "registered only"


class TestRenameNamespaceMerge:
    @pytest.fixture
    async def merged(self, storage):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="src-ns"),
                make_chunk(content="three", namespace="dst-ns"),
            ]
        )
        await storage.set_namespace_meta("src-ns", description="source", color="#111111")
        await storage.set_namespace_meta("dst-ns", description="target", color="#222222")
        return await storage.rename_namespace("src-ns", "dst-ns", merge=True)

    async def test_reports_moved_chunks(self, merged):
        assert merged.chunks_moved == 2

    async def test_reports_merged(self, merged):
        assert merged.merged is True

    async def test_does_not_report_a_metadata_rename(self, merged):
        """The source row was dropped, not moved — the target's row survives."""
        assert merged.metadata_renamed is False

    async def test_chunks_are_consolidated(self, merged, storage):
        ns = dict(await storage.list_namespaces())
        assert ns["dst-ns"] == 3

    async def test_source_namespace_is_gone(self, merged, storage):
        assert "src-ns" not in dict(await storage.list_namespaces())

    async def test_target_metadata_wins(self, merged, storage):
        meta = await storage.get_namespace_meta("dst-ns")
        assert (meta["description"], meta["color"]) == ("target", "#222222")

    async def test_source_metadata_row_is_dropped(self, merged, storage):
        assert await storage.get_namespace_meta("src-ns") is None

    @pytest.fixture
    async def overlapping(self, storage):
        """Both namespaces hold the same indexed chunk (same file + hash + line).

        ``chunks`` is UNIQUE on that key, so a naive namespace UPDATE would
        fail the merge outright. This is not exotic: it is what
        ``mm agent migrate`` hits when one agent's memory was indexed under
        both the legacy and the canonical namespace.
        """
        src = make_chunk(content="same", namespace="src-ns", source="shared.md")
        dup = make_chunk(content="same", namespace="dst-ns", source="shared.md")
        dup.content_hash = src.content_hash
        only_in_source = make_chunk(content="unique", namespace="src-ns", source="other.md")
        await storage.upsert_chunks([src, dup, only_in_source])
        return await storage.rename_namespace("src-ns", "dst-ns", merge=True)

    async def test_overlapping_merge_succeeds(self, overlapping):
        assert overlapping.merged is True

    async def test_overlapping_merge_reports_dropped_duplicates(self, overlapping):
        assert overlapping.duplicates_dropped == 1

    async def test_overlapping_merge_moves_only_the_new_chunk(self, overlapping):
        assert overlapping.chunks_moved == 1

    async def test_overlapping_merge_leaves_one_copy(self, overlapping, storage):
        assert dict(await storage.list_namespaces())["dst-ns"] == 2

    async def test_overlapping_merge_empties_the_source(self, overlapping, storage):
        assert "src-ns" not in dict(await storage.list_namespaces())

    async def test_overlapping_merge_keeps_the_targets_copy(self, storage):
        """Target wins, matching the metadata rule — its row (and counters) survive."""
        src = make_chunk(content="same", namespace="src-ns", source="shared.md")
        dup = make_chunk(content="same", namespace="dst-ns", source="shared.md")
        dup.content_hash = src.content_hash
        await storage.upsert_chunks([src, dup])

        await storage.rename_namespace("src-ns", "dst-ns", merge=True)

        assert await storage.get_chunk(src.id) is None
        assert await storage.get_chunk(dup.id) is not None

    async def test_no_duplicates_dropped_on_a_plain_rename(self, storage):
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        result = await storage.rename_namespace("src-ns", "dst-ns")
        assert result.duplicates_dropped == 0

    async def test_metadata_only_source_merges_into_metadata_target(self, storage):
        await storage.set_namespace_meta("src-ns", description="source")
        await storage.set_namespace_meta("dst-ns", description="target")
        result = await storage.rename_namespace("src-ns", "dst-ns", merge=True)
        assert (result.chunks_moved, result.merged) == (0, True)

    async def test_metadata_only_merge_drops_the_source_row(self, storage):
        await storage.set_namespace_meta("src-ns", description="source")
        await storage.set_namespace_meta("dst-ns", description="target")
        await storage.rename_namespace("src-ns", "dst-ns", merge=True)
        assert await storage.get_namespace_meta("src-ns") is None


class TestRenameNamespaceOtherTables:
    """Namespace identity is ``chunks`` ∪ ``namespace_metadata`` — nothing else."""

    async def test_session_namespace_follows_the_rename(self, storage):
        """A live session filters chunks by its stored namespace."""
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        await storage.create_session("s1", "alpha", "src-ns")
        await storage.rename_namespace("src-ns", "dst-ns")
        row = await storage.get_session("s1")
        assert row["namespace"] == "dst-ns"

    async def test_session_only_source_is_a_no_op(self, storage):
        """A namespace that exists only on a session row is not renameable."""
        await storage.create_session("s1", "alpha", "session-only-ns")
        result = await storage.rename_namespace("session-only-ns", "dst-ns")
        assert (result.chunks_moved, result.metadata_renamed) == (0, False)

    async def test_share_lineage_keeps_the_historical_namespace(self, storage):
        """``chunk_links.namespace_target`` records the name at share time."""
        src = make_chunk(content="one", namespace="src-ns")
        dst = make_chunk(content="copy", namespace="src-ns")
        await storage.upsert_chunks([src, dst])
        await storage.add_chunk_link(src.id, dst.id, "shared", "src-ns")

        await storage.rename_namespace("src-ns", "dst-ns")

        link = await storage.get_chunk_link(dst.id, "shared")
        assert link.namespace_target == "src-ns"

    async def test_session_only_target_is_not_a_conflict(self, storage):
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        await storage.create_session("s1", "alpha", "dst-ns")
        result = await storage.rename_namespace("src-ns", "dst-ns")
        assert result.merged is False


# ---------------------------------------------------------------------------
# Tag management tools
# ---------------------------------------------------------------------------


class TestTagManagement:
    async def test_get_tag_counts_empty(self, storage):
        counts = await storage.get_tag_counts()
        assert counts == []

    async def test_get_tag_counts(self, storage):
        c1 = make_chunk(content="a", tags=("python", "async"))
        c2 = make_chunk(content="b", tags=("python", "web"))
        c3 = make_chunk(content="c", tags=("rust",))
        await storage.upsert_chunks([c1, c2, c3])
        tag_dict = dict(await storage.get_tag_counts())
        assert tag_dict["python"] == 2
        assert tag_dict["async"] == 1
        assert tag_dict["web"] == 1
        assert tag_dict["rust"] == 1

    async def test_rename_tag(self, storage):
        c1 = make_chunk(content="a", tags=("legacy-tag", "other"))
        c2 = make_chunk(content="b", tags=("legacy-tag",))
        c3 = make_chunk(content="c", tags=("unrelated",))
        await storage.upsert_chunks([c1, c2, c3])
        renamed = await storage.rename_tag("legacy-tag", "modern-tag")
        assert renamed == 2
        tag_dict = dict(await storage.get_tag_counts())
        assert "legacy-tag" not in tag_dict
        assert tag_dict["modern-tag"] == 2
        assert tag_dict["other"] == 1
        assert tag_dict["unrelated"] == 1

    async def test_rename_tag_nonexistent(self, storage):
        renamed = await storage.rename_tag("ghost-tag", "new-tag")
        assert renamed == 0

    async def test_delete_tag(self, storage):
        c1 = make_chunk(content="a", tags=("remove-me", "keep-me"))
        c2 = make_chunk(content="b", tags=("remove-me",))
        await storage.upsert_chunks([c1, c2])
        deleted = await storage.delete_tag("remove-me")
        assert deleted == 2
        tag_dict = dict(await storage.get_tag_counts())
        assert "remove-me" not in tag_dict
        assert "keep-me" in tag_dict

    async def test_delete_tag_preserves_chunks(self, storage):
        """Deleting a tag should not delete the chunks themselves."""
        chunk = make_chunk(content="important content", tags=("disposable-tag",))
        await storage.upsert_chunks([chunk])
        await storage.delete_tag("disposable-tag")
        result = await storage.get_chunk(chunk.id)
        assert result is not None
        assert result.content == "important content"

    async def test_delete_tag_nonexistent(self, storage):
        deleted = await storage.delete_tag("no-such-tag")
        assert deleted == 0

    async def test_rename_tag_deduplicates(self, storage):
        """Renaming a tag that merges with an existing tag deduplicates."""
        chunk = make_chunk(content="a", tags=("tag-a", "tag-b"))
        await storage.upsert_chunks([chunk])
        await storage.rename_tag("tag-a", "tag-b")
        tag_dict = dict(await storage.get_tag_counts())
        assert tag_dict.get("tag-b") == 1
        assert "tag-a" not in tag_dict


# ---------------------------------------------------------------------------
# Session (episodic memory) tools
# ---------------------------------------------------------------------------


class TestSessions:
    async def test_create_and_list(self, storage):
        await storage.create_session("sess-1", "agent-a", "default")
        sessions = await storage.list_sessions(agent_id="agent-a")
        assert len(sessions) == 1
        assert sessions[0]["id"] == "sess-1"
        assert sessions[0]["agent_id"] == "agent-a"
        assert sessions[0]["namespace"] == "default"
        assert sessions[0]["ended_at"] is None

    async def test_end_session(self, storage):
        await storage.create_session("sess-2", "agent-b", "work")
        await storage.end_session("sess-2", "Completed analysis", {"queries": 5})
        sessions = await storage.list_sessions(agent_id="agent-b")
        assert sessions[0]["ended_at"] is not None
        assert sessions[0]["summary"] == "Completed analysis"

    async def test_add_and_get_session_events(self, storage):
        await storage.create_session("sess-3", "agent-c", "default")
        await storage.add_session_event("sess-3", "query", "search for X")
        await storage.add_session_event("sess-3", "add", "added chunk Y", ["chunk-1", "chunk-2"])
        await storage.add_session_event("sess-3", "note", "observation Z")
        events = await storage.get_session_events("sess-3")
        assert len(events) == 3
        assert events[0]["event_type"] == "query"
        assert events[0]["content"] == "search for X"
        assert events[0]["chunk_ids"] == []
        assert events[1]["event_type"] == "add"
        assert events[1]["chunk_ids"] == ["chunk-1", "chunk-2"]
        assert events[2]["event_type"] == "note"

    async def test_duplicate_session_ignored(self, storage):
        await storage.create_session("dup-id", "agent-1", "ns-a")
        await storage.create_session("dup-id", "agent-2", "ns-b")
        sessions = await storage.list_sessions()
        dup = [s for s in sessions if s["id"] == "dup-id"]
        assert len(dup) == 1
        assert dup[0]["agent_id"] == "agent-1"

    async def test_list_sessions_with_limit(self, storage):
        for i in range(5):
            await storage.create_session(f"lim-{i}", "agent", "default")
        sessions = await storage.list_sessions(agent_id="agent", limit=3)
        assert len(sessions) == 3

    async def test_list_sessions_with_since_filter(self, storage):
        await storage.create_session("old-s", "agent", "default")
        sessions = await storage.list_sessions(since="2099-01-01T00:00:00+00:00")
        assert len(sessions) == 0

    async def test_session_with_metadata(self, storage):
        meta = {"title": "Debug session", "tags": ["bug", "urgent"]}
        await storage.create_session("meta-s", "agent-d", "default", metadata=meta)
        sessions = await storage.list_sessions(agent_id="agent-d")
        assert len(sessions) == 1
        assert sessions[0]["id"] == "meta-s"

    async def test_get_events_empty_session(self, storage):
        await storage.create_session("empty-s", "agent", "default")
        events = await storage.get_session_events("empty-s")
        assert events == []

    async def test_multiple_agents(self, storage):
        await storage.create_session("s-a1", "agent-x", "default")
        await storage.create_session("s-a2", "agent-x", "default")
        await storage.create_session("s-b1", "agent-y", "default")
        x_sessions = await storage.list_sessions(agent_id="agent-x")
        y_sessions = await storage.list_sessions(agent_id="agent-y")
        assert len(x_sessions) == 2
        assert len(y_sessions) == 1


# ---------------------------------------------------------------------------
# Scratch (working memory) tools
# ---------------------------------------------------------------------------


class TestScratch:
    async def test_set_and_get(self, storage):
        await storage.scratch_set("my-key", "my-value")
        entry = await storage.scratch_get("my-key")
        assert entry is not None
        assert entry["key"] == "my-key"
        assert entry["value"] == "my-value"
        assert entry["promoted"] is False

    async def test_get_nonexistent(self, storage):
        entry = await storage.scratch_get("nonexistent-key")
        assert entry is None

    async def test_list_all(self, storage):
        await storage.scratch_set("k1", "v1")
        await storage.scratch_set("k2", "v2")
        await storage.scratch_set("k3", "v3")
        items = await storage.scratch_list()
        assert len(items) == 3
        keys = {item["key"] for item in items}
        assert keys == {"k1", "k2", "k3"}

    async def test_list_by_session(self, storage):
        await storage.scratch_set("s-key1", "val1", session_id="sess-a")
        await storage.scratch_set("s-key2", "val2", session_id="sess-a")
        await storage.scratch_set("global-key", "val3")
        session_items = await storage.scratch_list(session_id="sess-a")
        assert len(session_items) == 2
        all_items = await storage.scratch_list()
        assert len(all_items) == 3

    async def test_delete(self, storage):
        await storage.scratch_set("del-target", "data")
        removed = await storage.scratch_delete("del-target")
        assert removed is True
        assert await storage.scratch_get("del-target") is None

    async def test_delete_nonexistent(self, storage):
        removed = await storage.scratch_delete("no-such-key")
        assert removed is False

    async def test_overwrite_value(self, storage):
        await storage.scratch_set("mutable", "version-1")
        await storage.scratch_set("mutable", "version-2")
        entry = await storage.scratch_get("mutable")
        assert entry["value"] == "version-2"

    async def test_session_bound_cleanup(self, storage):
        await storage.scratch_set("bound", "data", session_id="sess-clean")
        await storage.scratch_set("free", "data")
        cleaned = await storage.scratch_cleanup(session_id="sess-clean")
        assert cleaned == 1
        assert await storage.scratch_get("bound") is None
        assert await storage.scratch_get("free") is not None

    async def test_ttl_expired_cleanup(self, storage):
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(timespec="seconds")
        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
        await storage.scratch_set("expired", "old", expires_at=past)
        await storage.scratch_set("still-valid", "new", expires_at=future)
        await storage.scratch_set("no-ttl", "permanent")
        cleaned = await storage.scratch_cleanup()
        assert cleaned == 1
        assert await storage.scratch_get("expired") is None
        assert await storage.scratch_get("still-valid") is not None
        assert await storage.scratch_get("no-ttl") is not None

    async def test_promoted_survives_session_cleanup(self, storage):
        await storage.scratch_set("important", "keep", session_id="sess-prom")
        promoted = await storage.scratch_promote("important")
        assert promoted is True
        cleaned = await storage.scratch_cleanup(session_id="sess-prom")
        assert cleaned == 0
        entry = await storage.scratch_get("important")
        assert entry is not None
        assert entry["promoted"] is True

    async def test_promoted_survives_ttl_cleanup(self, storage):
        past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        await storage.scratch_set("prom-ttl", "data", expires_at=past)
        await storage.scratch_promote("prom-ttl")
        cleaned = await storage.scratch_cleanup()
        assert cleaned == 0
        assert await storage.scratch_get("prom-ttl") is not None

    async def test_promote_nonexistent(self, storage):
        promoted = await storage.scratch_promote("no-key")
        assert promoted is False

    async def test_scratch_with_session_id(self, storage):
        await storage.scratch_set("ctx", "session context", session_id="sess-ctx")
        entry = await storage.scratch_get("ctx")
        assert entry["session_id"] == "sess-ctx"
