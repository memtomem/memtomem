"""A group a summary already covers is neither listed nor applied again (#2802).

``mem_consolidate`` built its groups without looking at ``consolidated_into``
edges, and ``mem_consolidate_apply`` linked a group without checking for one,
so a scan taken after an apply listed the same originals again and a second
apply appended a second summary for them. The scan now leaves out chunks a
summary covers, in the statement and before its limit; the apply refuses a
group whose originals are covered, and checks again under the write lock that
links them, so a concurrent apply of the same preview cannot link them twice.

Only an original's *outgoing* edge counts. The summary is the edge's target and
stays an ordinary chunk of its file.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID

import pytest

from memtomem.models import Chunk, ChunkMetadata
from test_storage_read_routing import pool_reader_mid_statement

pytestmark = pytest.mark.asyncio

_KEY = "consolidation_groups"


def _chunk(text: str, source: Path) -> Chunk:
    return Chunk(content=text, metadata=ChunkMetadata(source_file=source), embedding=[0.1] * 1024)


async def _originals(components, path: Path, n: int, start: int = 0) -> list[Chunk]:
    chunks = [_chunk(f"original note {i}", path) for i in range(start, start + n)]
    await components.storage.upsert_chunks(chunks)
    return chunks


async def _preview_ids(components) -> list[list[str]]:
    import json

    entry = await components.storage.scratch_get(_KEY)
    return [g["chunk_ids"] for g in json.loads(entry["value"])["groups"]]


def _ctx(components):
    from tests.test_tools_logic import _fake_ctx

    return _fake_ctx(components)


def _written(memory_dir: Path, text: str) -> int:
    """Occurrences of *text* in the memory files under *memory_dir*.

    Counted by the summary body rather than the ``Consolidated: <name>``
    heading, so the count does not depend on how the heading is formed.
    """
    return sum(f.read_text(encoding="utf-8").count(text) for f in memory_dir.rglob("*.md"))


def _edge_from_another_connection(db_path: Path, source_id: UUID, target_id: UUID) -> str | None:
    """Commit an edge the way a concurrent apply would; return the error, if any."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = sqlite3.connect(db_path, timeout=0)
    try:
        conn.execute(
            "INSERT INTO chunk_relations (source_id, target_id, relation_type, created_at) "
            "VALUES (?, ?, 'consolidated_into', ?)",
            (str(source_id), str(target_id), now),
        )
        conn.commit()
        return None
    except sqlite3.OperationalError as exc:
        return str(exc)
    finally:
        conn.close()


class TestScan:
    async def test_a_scan_after_an_apply_lists_nothing(self, components, memory_dir):
        """The issue's case: scan, apply, scan."""
        from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply

        await _originals(components, memory_dir / "notes.md", 3)
        ctx = _ctx(components)
        assert "original note" in await mem_consolidate(min_group_size=2, ctx=ctx)
        out = await mem_consolidate_apply(group_id=0, summary="summary number 1", ctx=ctx)
        assert "Originals linked: 3/3" in out

        again = await mem_consolidate(min_group_size=2, ctx=ctx)
        assert "notes.md" not in again
        assert "No consolidation candidates found." in again

    async def test_every_listed_chunk_is_in_the_group_not_only_the_previews(
        self, components, memory_dir
    ):
        """An apply links the ids it is given; an unlinked rest would be listed again."""
        from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply

        originals = await _originals(components, memory_dir / "notes.md", 8)
        ctx = _ctx(components)
        out = await mem_consolidate(min_group_size=3, ctx=ctx)
        assert out.count("original note") == 5  # five previews
        assert await _preview_ids(components) == [[str(c.id) for c in originals]]

        applied = await mem_consolidate_apply(group_id=0, summary="the summary", ctx=ctx)
        assert "Originals linked: 8/8" in applied
        assert "No consolidation candidates found." in await mem_consolidate(
            min_group_size=3, ctx=ctx
        )

    async def test_chunks_added_after_an_apply_form_a_group_of_their_own(
        self, components, memory_dir
    ):
        from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply

        path = memory_dir / "notes.md"
        await _originals(components, path, 3)
        ctx = _ctx(components)
        await mem_consolidate(min_group_size=2, ctx=ctx)
        await mem_consolidate_apply(group_id=0, summary="the summary", ctx=ctx)

        added = await _originals(components, path, 3, start=3)
        assert "notes.md" in await mem_consolidate(min_group_size=2, ctx=ctx)
        assert await _preview_ids(components) == [[str(c.id) for c in added]]

    async def test_deleting_the_summary_lists_its_originals_again(self, components, memory_dir):
        """The edge is the marker, and it goes with the summary it points at."""
        from memtomem.server.tools.consolidation import mem_consolidate

        storage = components.storage
        originals = await _originals(components, memory_dir / "notes.md", 3)
        summary = _chunk("a summary", memory_dir / "summaries.md")
        await storage.upsert_chunks([summary])
        for c in originals:
            await storage.add_relation(c.id, summary.id, "consolidated_into")
        ctx = _ctx(components)
        assert "notes.md" not in await mem_consolidate(min_group_size=2, ctx=ctx)

        assert await storage.delete_chunks([summary.id]) == 1
        assert "notes.md" in await mem_consolidate(min_group_size=2, ctx=ctx)
        assert await _preview_ids(components) == [[str(c.id) for c in originals]]

    async def test_a_covered_head_does_not_hide_the_rest_of_the_file(self, components, memory_dir):
        """The scan lists 20 rows; covered rows are left out before that limit."""
        from memtomem.server.tools.consolidation import mem_consolidate

        storage = components.storage
        path = memory_dir / "notes.md"
        originals = await _originals(components, path, 25)
        summary = _chunk("a summary", memory_dir / "summaries.md")
        await storage.upsert_chunks([summary])
        for c in originals[:20]:
            await storage.add_relation(c.id, summary.id, "consolidated_into")

        assert "notes.md" in await mem_consolidate(
            min_group_size=3, source_filter="notes.md", ctx=_ctx(components)
        )
        assert await _preview_ids(components) == [[str(c.id) for c in originals[20:]]]

    async def test_a_summary_and_an_unrelated_edge_do_not_count(self, components, memory_dir):
        """The summary is an edge's target, and a ``related`` edge marks nothing."""
        from memtomem.server.tools.consolidation import mem_consolidate

        storage = components.storage
        path = memory_dir / "notes.md"
        chunks = await _originals(components, path, 3)
        elsewhere = await _originals(components, memory_dir / "other.md", 1)
        await storage.add_relation(elsewhere[0].id, chunks[0].id, "consolidated_into")
        await storage.add_relation(chunks[1].id, elsewhere[0].id, "related")

        assert "notes.md" in await mem_consolidate(
            min_group_size=3, source_filter="notes.md", ctx=_ctx(components)
        )
        assert await _preview_ids(components) == [[str(c.id) for c in chunks]]


class TestApply:
    async def test_a_kept_preview_of_an_applied_group_is_refused(self, components, memory_dir):
        """A preview read before the apply still lists the group; applying it is refused."""
        from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply

        storage = components.storage
        await _originals(components, memory_dir / "notes.md", 3)
        ctx = _ctx(components)
        await mem_consolidate(min_group_size=2, ctx=ctx)
        kept = await storage.scratch_get(_KEY)
        first = await mem_consolidate_apply(group_id=0, summary="summary number 1", ctx=ctx)
        summary_id = first.split("Summary chunk id: ")[1].split("\n")[0]
        await storage.scratch_set(_KEY, kept["value"], expires_at=kept["expires_at"])

        out = await mem_consolidate_apply(group_id=0, summary="summary number 2", ctx=ctx)

        assert out.startswith("Error: group 0 is already consolidated: 3 of its 3 originals")
        assert summary_id not in out
        assert _written(memory_dir, "summary number 1") == 1
        assert _written(memory_dir, "summary number 2") == 0
        runs = await storage.maintenance_run_latest(kind="consolidate_apply", limit=10)
        assert len(runs) == 1
        assert (await storage.scratch_get(_KEY))["value"] == kept["value"]

    async def test_a_group_holding_a_summary_is_applied(self, components, memory_dir):
        """A member that is only an edge's target is not covered."""
        from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply

        storage = components.storage
        chunks = await _originals(components, memory_dir / "notes.md", 3)
        elsewhere = await _originals(components, memory_dir / "other.md", 1)
        await storage.add_relation(elsewhere[0].id, chunks[0].id, "consolidated_into")
        ctx = _ctx(components)
        await mem_consolidate(min_group_size=3, source_filter="notes.md", ctx=ctx)

        out = await mem_consolidate_apply(group_id=0, summary="the summary", ctx=ctx)

        assert "Originals linked: 3/3" in out

    async def test_a_summary_in_the_group_is_linked_but_not_decayed(self, components, memory_dir):
        """``keep_originals=False`` decays the ordinary notes only (#2811)."""
        from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply
        from memtomem.tools.consolidation_engine import DECAY_FACTOR, DECAY_FLOOR

        storage = components.storage
        chunks = await _originals(components, memory_dir / "notes.md", 3)
        elsewhere = await _originals(components, memory_dir / "other.md", 1)
        await storage.add_relation(elsewhere[0].id, chunks[0].id, "consolidated_into")
        await storage.update_importance_scores({str(c.id): 0.8 for c in chunks})
        ctx = _ctx(components)
        await mem_consolidate(min_group_size=3, source_filter="notes.md", ctx=ctx)

        out = await mem_consolidate_apply(
            group_id=0, summary="the summary", keep_originals=False, ctx=ctx
        )

        assert "Originals linked: 3/3" in out
        summary_id = UUID(out.split("Summary chunk id: ")[1].split("\n")[0])
        targets = await storage.get_consolidation_targets([c.id for c in chunks])
        assert targets == {c.id: [summary_id] for c in chunks}
        scores = await storage.get_importance_scores([str(c.id) for c in chunks])
        decayed = max(0.8 * DECAY_FACTOR, DECAY_FLOOR)
        assert scores == {
            str(chunks[0].id): pytest.approx(0.8),
            **{str(c.id): pytest.approx(decayed) for c in chunks[1:]},
        }
        [run] = await storage.maintenance_run_latest(kind="consolidate_apply")
        assert sorted(run["summary"]["decayed_ids"]) == sorted(str(c.id) for c in chunks[1:])

    @pytest.mark.parametrize(
        "bodies",
        [
            ("first summary", "second summary"),
            # ``format_entry_block`` keeps a body's own ``## `` heading and
            # drops the title, so these two share a heading whatever the title.
            ("## Summary\n\nfirst summary", "## Summary\n\nsecond summary"),
        ],
        ids=["plain", "own-heading"],
    )
    async def test_a_second_summary_of_the_same_source_keeps_the_first(
        self, components, memory_dir, bodies
    ):
        """Each summary keeps its own chunk and edges.

        In a shared day file, two entries under one heading were indexed as one
        chunk: the second write replaced the first summary's row, its edges
        cascaded, and its originals were listed again.
        """
        from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply

        storage = components.storage
        path = memory_dir / "notes.md"
        first = await _originals(components, path, 3)
        ctx = _ctx(components)
        await mem_consolidate(min_group_size=2, ctx=ctx)
        one = await mem_consolidate_apply(group_id=0, summary=bodies[0], ctx=ctx)
        second = await _originals(components, path, 3, start=3)
        await mem_consolidate(min_group_size=2, ctx=ctx)
        two = await mem_consolidate_apply(group_id=0, summary=bodies[1], ctx=ctx)

        ids = []
        for out in (one, two):
            summary_id = UUID(out.split("Summary chunk id: ")[1].split("\n")[0])
            assert await storage.get_chunk(summary_id) is not None
            ids.append(summary_id)
        targets = await storage.get_consolidation_targets([c.id for c in first + second])
        assert targets == {
            **{c.id: [ids[0]] for c in first},
            **{c.id: [ids[1]] for c in second},
        }
        assert "No consolidation candidates found." in await mem_consolidate(
            min_group_size=2, source_filter="notes.md", ctx=ctx
        )

    async def test_the_summary_is_the_only_entry_of_its_own_file(self, components, memory_dir):
        from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply

        storage = components.storage
        await _originals(components, memory_dir / "notes.md", 3)
        ctx = _ctx(components)
        await mem_consolidate(min_group_size=2, ctx=ctx)
        out = await mem_consolidate_apply(group_id=0, summary="the summary", ctx=ctx)

        summary = await storage.get_chunk(UUID(out.split("Summary chunk id: ")[1].split("\n")[0]))
        assert summary is not None
        written = Path(summary.metadata.source_file)
        run_id = out.split("Run id: ")[1].split("\n")[0]
        assert written.parent == (memory_dir / "consolidated").resolve()
        assert written.name.endswith(".md") and not written.name.endswith(".consolidated.md")
        assert f"-notes-run{run_id}-" in written.name
        text = written.read_text(encoding="utf-8")
        assert text.count("## Consolidated: notes.md") == 1 and text.count("the summary") == 1
        assert summary.metadata.heading_hierarchy == ("## Consolidated: notes.md",)
        assert [f.name for f in memory_dir.glob("*.md")] == []

    @pytest.mark.parametrize("into", ["project_shared", "nested_project_shared", "unregistered"])
    async def test_a_linked_summary_directory_is_refused(
        self, components, memory_dir, tmp_path, into
    ):
        """A link at ``consolidated/`` must not carry a user-scope summary elsewhere.

        Into a registered ``project_shared`` tier it would skip that tier's
        confirmation, also when that tier sits inside the user directory; into
        an unregistered directory it would land outside every memory directory.
        """
        from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply

        storage = components.storage
        elsewhere = {
            "project_shared": tmp_path / "proj" / ".memtomem" / "memories" / "sub",
            "nested_project_shared": memory_dir / "proj" / ".memtomem" / "memories" / "sub",
            "unregistered": tmp_path / "outside",
        }[into]
        elsewhere.mkdir(parents=True)
        if into != "unregistered":
            components.config.indexing.project_memory_dirs = [elsewhere.parent]
        try:
            (memory_dir / "consolidated").symlink_to(elsewhere, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
        await _originals(components, memory_dir / "notes.md", 3)
        ctx = _ctx(components)
        await mem_consolidate(min_group_size=2, ctx=ctx)
        kept = await storage.scratch_get(_KEY)

        out = await mem_consolidate_apply(group_id=0, summary="the summary", ctx=ctx)

        assert out.startswith("Error: ") and "nothing was written" in out
        assert list(elsewhere.iterdir()) == []
        [run] = await storage.maintenance_run_latest(kind="consolidate_apply")
        assert run["status"] == "error"
        assert (await storage.scratch_get(_KEY))["value"] == kept["value"]

    async def test_a_non_default_namespace_summary_stays_in_its_file(self, components, memory_dir):
        """The day file is re-targeted by namespace (#2005); a summary file is not."""
        from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply

        storage = components.storage
        path = memory_dir / "notes.md"
        originals = [
            Chunk(
                content=f"original note {i}",
                metadata=ChunkMetadata(source_file=path, namespace="team-a"),
                embedding=[0.1] * 1024,
            )
            for i in range(3)
        ]
        await storage.upsert_chunks(originals)
        ctx = _ctx(components)
        await mem_consolidate(namespace="team-a", min_group_size=2, ctx=ctx)
        out = await mem_consolidate_apply(group_id=0, summary="the summary", ctx=ctx)

        summary = await storage.get_chunk(UUID(out.split("Summary chunk id: ")[1].split("\n")[0]))
        assert summary is not None
        assert summary.metadata.namespace == "team-a"
        assert Path(summary.metadata.source_file).parent == (memory_dir / "consolidated").resolve()

    async def test_an_apply_finishing_while_another_writes_is_not_applied_twice(
        self, components, memory_dir, monkeypatch
    ):
        """One apply completes between another's check and its write: one link, one decay."""
        from memtomem.server.tools import memory_crud
        from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply

        storage = components.storage
        originals = await _originals(components, memory_dir / "notes.md", 2)
        await storage.update_importance_scores({str(c.id): 1.0 for c in originals})
        ctx = _ctx(components)
        await mem_consolidate(min_group_size=2, ctx=ctx)

        real = memory_crud._mem_add_core
        other: list[str] = []

        async def other_apply_first(*args, **kwargs):
            if not other:
                other.append("")
                other[0] = await mem_consolidate_apply(
                    group_id=0, summary="the other summary", keep_originals=False, ctx=ctx
                )
            return await real(*args, **kwargs)

        monkeypatch.setattr(memory_crud, "_mem_add_core", other_apply_first)

        out = await mem_consolidate_apply(
            group_id=0, summary="this summary", keep_originals=False, ctx=ctx
        )

        assert "Originals linked: 2/2" in other[0]
        assert "(unlinked)" in out and "another summary covered these originals" in out
        other_id = UUID(other[0].split("Summary chunk id: ")[1].split("\n")[0])
        targets = await storage.get_consolidation_targets([c.id for c in originals])
        assert targets == {c.id: [other_id] for c in originals}
        scores = await storage.get_importance_scores([str(c.id) for c in originals])
        assert set(scores.values()) == {0.5}

    async def _published_then(self, components, memory_dir, monkeypatch, after_publish):
        from memtomem.server.tools import memory_crud
        from memtomem.server.tools.consolidation import mem_consolidate

        storage = components.storage
        originals = await _originals(components, memory_dir / "notes.md", 2)
        await storage.update_importance_scores({str(c.id): 1.0 for c in originals})
        rival = _chunk("a rival summary", memory_dir / "rival.md")
        await storage.upsert_chunks([rival])
        ctx = _ctx(components)
        await mem_consolidate(min_group_size=2, source_filter="notes.md", ctx=ctx)

        real = memory_crud._mem_add_core

        async def publish_then(*args, **kwargs):
            result, stats = await real(*args, **kwargs)
            await after_publish(originals, rival)
            return result, stats

        monkeypatch.setattr(memory_crud, "_mem_add_core", publish_then)
        return ctx, originals, rival

    async def test_originals_covered_after_publish_get_no_edges_or_decay(
        self, components, memory_dir, monkeypatch
    ):
        from memtomem.server.tools.consolidation import mem_consolidate_apply

        storage = components.storage

        async def rival_apply(originals, rival):
            for c in originals:
                await storage.add_relation(c.id, rival.id, "consolidated_into")

        ctx, originals, rival = await self._published_then(
            components, memory_dir, monkeypatch, rival_apply
        )

        out = await mem_consolidate_apply(
            group_id=0, summary="the summary", keep_originals=False, ctx=ctx
        )

        assert "(unlinked)" in out and "another summary covered these originals" in out
        targets = await storage.get_consolidation_targets([c.id for c in originals])
        assert targets == {c.id: [rival.id] for c in originals}
        scores = await storage.get_importance_scores([str(c.id) for c in originals])
        assert set(scores.values()) == {1.0}
        [run] = await storage.maintenance_run_latest(kind="consolidate_apply")
        assert run["summary"]["warning"] == "already_consolidated"
        assert await storage.scratch_get(_KEY) is None

    async def test_a_rival_edge_after_the_final_check_cannot_commit_before_the_writes(
        self, components, memory_dir, monkeypatch
    ):
        """The re-check and the writes share the lock, so no edge lands between them."""
        from memtomem.server.tools.consolidation import mem_consolidate_apply

        storage = components.storage
        db_path = components.config.storage.sqlite_path
        outcomes: list[str | None] = []

        async def arm(originals, rival):
            # Armed only now: the check before publication must not trigger it.
            real = storage.get_consolidation_targets

            async def checked(chunk_ids):
                result = await real(chunk_ids)
                if not outcomes:
                    outcomes.append(
                        _edge_from_another_connection(db_path, originals[0].id, rival.id)
                    )
                return result

            monkeypatch.setattr(storage, "get_consolidation_targets", checked)

        ctx, originals, rival = await self._published_then(components, memory_dir, monkeypatch, arm)

        out = await mem_consolidate_apply(
            group_id=0, summary="the summary", keep_originals=False, ctx=ctx
        )

        [outcome] = outcomes
        assert outcome is not None and "locked" in outcome
        assert "Originals linked: 2/2" in out
        targets = await storage.get_consolidation_targets([c.id for c in originals])
        assert set(targets) == {c.id for c in originals}
        assert all(rival.id not in t for t in targets.values())


class TestStorage:
    async def test_targets_are_outgoing_typed_and_batched(self, storage, tmp_path, monkeypatch):
        from memtomem.storage import sqlite_backend

        monkeypatch.setattr(sqlite_backend, "_SQL_MAX_PARAMS", 2)
        originals = [_chunk(f"note {i}", tmp_path / "a.md") for i in range(5)]
        summary = _chunk("summary", tmp_path / "s.md")
        other = _chunk("other", tmp_path / "o.md")
        await storage.upsert_chunks([*originals, summary, other])
        for c in originals[:4]:
            await storage.add_relation(c.id, summary.id, "consolidated_into")
        await storage.add_relation(originals[4].id, other.id, "related")

        ids = [c.id for c in originals] + [summary.id]
        assert await storage.get_consolidation_targets(ids) == {
            c.id: [summary.id] for c in originals[:4]
        }

    async def test_summaries_are_incoming_typed_and_batched(self, storage, tmp_path, monkeypatch):
        from memtomem.storage import sqlite_backend

        monkeypatch.setattr(sqlite_backend, "_SQL_MAX_PARAMS", 2)
        originals = [_chunk(f"note {i}", tmp_path / "a.md") for i in range(3)]
        summaries = [_chunk(f"summary {i}", tmp_path / "s.md") for i in range(3)]
        linked = _chunk("linked", tmp_path / "o.md")
        await storage.upsert_chunks([*originals, *summaries, linked])
        for original, summary in zip(originals, summaries):
            await storage.add_relation(original.id, summary.id, "consolidated_into")
        await storage.add_relation(originals[0].id, linked.id, "related")

        ids = [c.id for c in originals] + [c.id for c in summaries] + [linked.id]
        assert await storage.get_consolidation_summaries(ids) == {c.id for c in summaries}

    async def test_targets_read_the_writer_inside_a_transaction(
        self, storage, tmp_path, monkeypatch
    ):
        original = _chunk("note", tmp_path / "a.md")
        summary = _chunk("summary", tmp_path / "s.md")
        await storage.upsert_chunks([original, summary])
        with pool_reader_mid_statement(storage, monkeypatch):
            await storage.add_relation(original.id, summary.id, "consolidated_into")
            async with storage.transaction():
                got = await storage.get_consolidation_targets([original.id])
        assert got == {original.id: [summary.id]}
