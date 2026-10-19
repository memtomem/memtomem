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


def _summary_entries(memory_dir: Path) -> int:
    return sum(
        f.read_text(encoding="utf-8").count("Consolidated: notes.md")
        for f in memory_dir.rglob("*.md")
        if f.name != "notes.md"
    )


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
        assert _summary_entries(memory_dir) == 1
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
