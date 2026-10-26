"""Bundles carry the edges between their chunks, and a restore puts them back (#2809).

``export_chunks`` wrote chunks and nothing of ``chunk_relations``, so a store
restored from a bundle had every summary and original but no
``consolidated_into`` edge between them: ``mem_consolidate`` listed every
consolidated group again, and an apply wrote a second summary over the same
originals. A bundle now lists the edges whose both ends it holds, signed with
its chunks, and import maps each end to the row its record landed on — import
mints fresh ids unless ``preserve_ids`` is set, and a hash conflict lands on the
existing row.

A foreign bundle restores only edges between rows it newly adds. Its relation
types are scanned by the same redaction gate as its records.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from memtomem import provenance
from memtomem.storage.base import ChunkKeyRow
from memtomem.storage.sqlite_backend import SqliteBackend
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.tools.export_import import (
    ImportPrivacyError,
    _destination_key,
    _match_conflicts,
    export_chunks,
    import_chunks,
)
from test_import_provenance_gate import (
    _CLEAN,
    _SECRET,
    _FakeEmbedder,
    _make_chunk,
    _make_storage,
    _record,
)

pytestmark = pytest.mark.asyncio


def _edges(storage: SqliteBackend) -> set[tuple[str, str, str]]:
    """Every edge in *storage*, by the content of its ends."""
    db = storage._get_db()
    rows = db.execute(
        "SELECT s.content, t.content, r.relation_type FROM chunk_relations r "
        "JOIN chunks s ON s.id = r.source_id JOIN chunks t ON t.id = r.target_id"
    ).fetchall()
    return {(s, t, rel) for s, t, rel in rows}


def _ids(storage: SqliteBackend) -> dict[str, UUID]:
    db = storage._get_db()
    return {content: UUID(cid) for cid, content in db.execute("SELECT id, content FROM chunks")}


async def _consolidated_store(tmp_path: Path, name: str = "src") -> SqliteBackend:
    """Three originals in ``notes.md`` covered by one summary, plus a user link."""
    storage = await _make_storage(tmp_path, name)
    originals = [_make_chunk(f"original note {i}", source="notes.md") for i in range(3)]
    summary = _make_chunk("summary of the notes", source="summary.md")
    await storage.upsert_chunks([*originals, summary])
    for chunk in originals:
        await storage.add_relation(chunk.id, summary.id, "consolidated_into")
    await storage.add_relation(originals[0].id, originals[1].id, "related")
    return storage


_EXPECTED_EDGES = {
    ("original note 0", "summary of the notes", "consolidated_into"),
    ("original note 1", "summary of the notes", "consolidated_into"),
    ("original note 2", "summary of the notes", "consolidated_into"),
    ("original note 0", "original note 1", "related"),
}


def _signed_bundle(path: Path, key_path: Path, records: list[dict], relations: list[dict]) -> Path:
    """A bundle this install's key signs, as a self-export of it would be."""
    key = provenance.load_or_create_key_for_export(key_path)
    marker = provenance.make_marker(records, key, relations)
    path.write_text(
        json.dumps(
            {"version": "3", "chunks": records, "relations": relations, "provenance": marker}
        ),
        encoding="utf-8",
    )
    return path


def _foreign_bundle(path: Path, records: list[dict], relations: object) -> Path:
    path.write_text(
        json.dumps({"version": "3", "chunks": records, "relations": relations}), encoding="utf-8"
    )
    return path


def _rel(source: dict, target: dict, relation_type: str = "related") -> dict:
    return {
        "source_id": source["chunk_id"],
        "target_id": target["chunk_id"],
        "relation_type": relation_type,
    }


class TestRestore:
    async def test_a_restore_into_an_empty_store_keeps_coverage(self, tmp_path):
        """The issue's case, with the default fresh ids."""
        key_path = tmp_path / "k.provenance_key"
        src = await _consolidated_store(tmp_path)
        bundle_path = tmp_path / "backup.json"
        bundle = await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)
        assert len(bundle.relations) == 4

        dst = await _make_storage(tmp_path, "dst")
        stats = await import_chunks(dst, _FakeEmbedder(), bundle_path, provenance_key_path=key_path)

        assert (stats.restored_relations, stats.skipped_relations) == (4, 0)
        assert _edges(dst) == _EXPECTED_EDGES
        # Fresh ids: the edges were remapped, not copied.
        assert set(_ids(dst).values()).isdisjoint(_ids(src).values())
        # What ``mem_consolidate`` reads: no original of the group is listed.
        listed = await dst.list_chunks_by_source(
            Path("notes.md"), limit=20, as_stored=True, exclude_consolidated=True
        )
        assert listed == []

    async def test_preserve_ids_restores_the_same_edges(self, tmp_path):
        key_path = tmp_path / "k.provenance_key"
        src = await _consolidated_store(tmp_path)
        bundle_path = tmp_path / "backup.json"
        await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)

        dst = await _make_storage(tmp_path, "dst")
        await import_chunks(
            dst, _FakeEmbedder(), bundle_path, preserve_ids=True, provenance_key_path=key_path
        )

        def by_id(storage):
            return set(
                storage._get_db()
                .execute("SELECT source_id, target_id, relation_type FROM chunk_relations")
                .fetchall()
            )

        assert by_id(dst) == by_id(src)

    async def test_a_skip_reimport_puts_lost_edges_back(self, tmp_path):
        """Every record conflicts, so there is nothing to upsert — only edges."""
        key_path = tmp_path / "k.provenance_key"
        src = await _consolidated_store(tmp_path)
        bundle_path = tmp_path / "backup.json"
        await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)
        src._get_db().execute("DELETE FROM chunk_relations")
        src._get_db().commit()

        stats = await import_chunks(src, _FakeEmbedder(), bundle_path, provenance_key_path=key_path)

        assert stats.conflict_skipped_chunks == 4 and stats.imported_chunks == 0
        assert stats.restored_relations == 4
        assert _edges(src) == _EXPECTED_EDGES

    async def test_an_update_reimport_lands_edges_on_the_existing_rows(self, tmp_path):
        key_path = tmp_path / "k.provenance_key"
        src = await _consolidated_store(tmp_path)
        before = _ids(src)
        bundle_path = tmp_path / "backup.json"
        await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)
        src._get_db().execute("DELETE FROM chunk_relations")
        src._get_db().commit()

        stats = await import_chunks(
            src, _FakeEmbedder(), bundle_path, on_conflict="update", provenance_key_path=key_path
        )

        assert stats.updated_chunks == 4 and stats.restored_relations == 4
        assert _ids(src) == before
        assert _edges(src) == _EXPECTED_EDGES

    async def test_a_reminted_preserved_id_still_gets_its_edges(self, tmp_path):
        """``preserve_ids`` re-mints an id another row already holds."""
        key_path = tmp_path / "k.provenance_key"
        records = [_record("first new note"), _record("second new note")]
        relations = [_rel(records[0], records[1])]
        bundle_path = _signed_bundle(tmp_path / "b.json", key_path, records, relations)

        dst = await _make_storage(tmp_path, "dst")
        squatter = _make_chunk("unrelated row")
        squatter.id = UUID(records[0]["chunk_id"])
        await dst.upsert_chunks([squatter])

        stats = await import_chunks(
            dst, _FakeEmbedder(), bundle_path, preserve_ids=True, provenance_key_path=key_path
        )

        assert stats.restored_relations == 1
        assert _edges(dst) == {("first new note", "second new note", "related")}
        ids = _ids(dst)
        assert ids["unrelated row"] == squatter.id
        assert ids["first new note"] != squatter.id

    async def test_an_existing_edge_on_the_pair_is_kept(self, tmp_path):
        key_path = tmp_path / "k.provenance_key"
        src = await _consolidated_store(tmp_path)
        bundle_path = tmp_path / "backup.json"
        await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)
        ids = _ids(src)
        await src.delete_relation(ids["original note 0"], ids["summary of the notes"])
        await src.add_relation(ids["original note 0"], ids["summary of the notes"], "mine")

        stats = await import_chunks(src, _FakeEmbedder(), bundle_path, provenance_key_path=key_path)

        assert stats.restored_relations == 0 and stats.skipped_relations == 4
        assert ("original note 0", "summary of the notes", "mine") in _edges(src)
        assert ("original note 0", "summary of the notes", "consolidated_into") not in _edges(src)

    async def test_any_relation_type_round_trips(self, tmp_path):
        """``mem_link`` writes any string, empty and long ones included."""
        key_path = tmp_path / "k.provenance_key"
        src = await _make_storage(tmp_path, "src")
        a, b, c = (_make_chunk(f"typed note {i}", source=f"t{i}.md") for i in range(3))
        await src.upsert_chunks([a, b, c])
        await src.add_relation(a.id, b.id, "")
        await src.add_relation(b.id, c.id, "x" * 500)
        bundle_path = tmp_path / "backup.json"
        await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)

        dst = await _make_storage(tmp_path, "dst")
        await import_chunks(dst, _FakeEmbedder(), bundle_path, provenance_key_path=key_path)

        assert _edges(dst) == _edges(src)

    async def test_duplicate_mode_with_an_ignored_row_does_not_abort(self, tmp_path):
        """Exact occupied keys land on real rows rather than phantom duplicate ids.

        The self-export can restore the missing row and its edge to an existing
        summary without violating foreign keys or rewriting the occupied rows.
        """
        key_path = tmp_path / "k.provenance_key"
        src = await _consolidated_store(tmp_path)
        bundle_path = tmp_path / "backup.json"
        await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)
        src._get_db().execute("DELETE FROM chunks WHERE content = 'original note 2'")
        src._get_db().commit()

        stats = await import_chunks(
            src, _FakeEmbedder(), bundle_path, on_conflict="duplicate", provenance_key_path=key_path
        )

        assert stats.failed_chunks == 0 and stats.failed_relations == 0
        assert (stats.restored_relations, stats.skipped_relations) == (1, 3)
        assert "original note 2" in _ids(src)


class TestExport:
    async def test_an_edge_to_a_chunk_left_out_is_not_exported(self, tmp_path):
        storage = await _make_storage(tmp_path, "src")
        work = _make_chunk("work note", source="work.md", ns="work")
        other = _make_chunk("other note", source="other.md", ns="other")
        work2 = _make_chunk("second work note", source="work2.md", ns="work")
        await storage.upsert_chunks([work, other, work2])
        await storage.add_relation(work.id, other.id, "related")
        await storage.add_relation(work.id, work2.id, "related")

        bundle = await export_chunks(storage, namespace_filter="work", stamp_provenance=False)

        assert bundle.relations == [
            {"source_id": str(work.id), "target_id": str(work2.id), "relation_type": "related"}
        ]

    async def test_an_edge_to_a_held_source_is_not_exported(self, tmp_path):
        storage = await _make_storage(tmp_path, "src")
        visible = _make_chunk("visible note", source="visible.md")
        held = _make_chunk("held note", source="held.md")
        await storage.upsert_chunks([visible, held])
        await storage.add_relation(visible.id, held.id, "related")
        await storage.hold_source(held.metadata.source_file, "missing")

        bundle = await export_chunks(storage, stamp_provenance=False)

        assert bundle.total_chunks == 1 and bundle.relations == []


class TestSignature:
    async def test_a_bundle_without_relations_signs_as_before(self):
        """v2 self-exports, signed over chunks alone, still verify."""
        import hashlib
        import hmac

        key = b"k" * 32
        records = [_record(_CLEAN)]
        before = hmac.new(
            key,
            provenance.SCHEME.encode("ascii") + b"\n" + provenance._canonical_chunks(records),
            hashlib.sha256,
        ).hexdigest()

        assert provenance.sign_chunks(records, key) == before
        assert provenance.sign_chunks(records, key, []) == before
        assert provenance.verify_marker(records, provenance.make_marker(records, key), key, [])

    @pytest.mark.parametrize("edit", ["append", "remove", "endpoint", "type"])
    async def test_editing_the_relations_breaks_the_marker(self, edit):
        key = b"k" * 32
        records = [_record("a note"), _record("b note")]
        relations = [_rel(records[0], records[1])]
        marker = provenance.make_marker(records, key, relations)
        edited = [dict(r) for r in relations]
        if edit == "append":
            edited.append(_rel(records[1], records[0]))
        elif edit == "remove":
            edited = []
        elif edit == "endpoint":
            edited[0]["target_id"] = records[0]["chunk_id"]
        else:
            edited[0]["relation_type"] = "consolidated_into"

        assert provenance.verify_marker(records, marker, key, relations)
        assert not provenance.verify_marker(records, marker, key, edited)


class TestForeignBundle:
    async def test_only_edges_between_rows_it_adds_are_restored(self, tmp_path):
        dst = await _make_storage(tmp_path, "dst")
        await dst.upsert_chunks([_make_chunk("a note the store already had")])
        new_a, new_b = _record("new note a"), _record("new note b")
        known = _record("a note the store already had")
        relations = [_rel(new_a, new_b), _rel(new_a, known, "consolidated_into")]
        bundle_path = _foreign_bundle(tmp_path / "f.json", [new_a, new_b, known], relations)

        stats = await import_chunks(
            dst, _FakeEmbedder(), bundle_path, provenance_key_path=tmp_path / "none.key"
        )

        assert (stats.restored_relations, stats.skipped_relations) == (1, 1)
        assert _edges(dst) == {("new note a", "new note b", "related")}

    async def test_a_tampered_self_export_restores_as_foreign(self, tmp_path):
        """An edge appended after signing makes the bundle foreign, guard and all."""
        key_path = tmp_path / "k.provenance_key"
        src = await _consolidated_store(tmp_path)
        bundle_path = tmp_path / "backup.json"
        await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)
        src._get_db().execute("DELETE FROM chunk_relations")
        src._get_db().commit()
        data = json.loads(bundle_path.read_text(encoding="utf-8"))
        data["relations"].append(dict(data["relations"][0], relation_type="extra"))
        bundle_path.write_text(json.dumps(data), encoding="utf-8")

        stats = await import_chunks(src, _FakeEmbedder(), bundle_path, provenance_key_path=key_path)

        # Every end hash-matched a row the store had, which a foreign bundle may not link.
        assert stats.restored_relations == 0 and _edges(src) == set()


class TestMalformed:
    async def test_malformed_relations_reject_the_whole_bundle(self, tmp_path):
        key_path = tmp_path / "k.provenance_key"
        a, b = _record("note a"), _record("note b")
        for broken in [
            "not a dict",
            {"source_id": a["chunk_id"]},
            {"source_id": a["chunk_id"], "target_id": b["chunk_id"], "relation_type": 7},
            {"source_id": a["chunk_id"], "target_id": str(uuid4()), "relation_type": "related"},
        ]:
            bundle_path = _signed_bundle(
                tmp_path / "b.json", key_path, [a, b], [_rel(a, b), broken]
            )
            dst = await _make_storage(tmp_path, str(uuid4()))
            try:
                with pytest.raises(ValueError, match="relation"):
                    await import_chunks(
                        dst, _FakeEmbedder(), bundle_path, provenance_key_path=key_path
                    )
                assert (await dst.get_stats())["total_chunks"] == 0
                assert _edges(dst) == set()
            finally:
                await dst.close()

    async def test_two_records_landing_on_one_row_drop_their_edge(self, tmp_path):
        key_path = tmp_path / "k.provenance_key"
        a = _record("same text", source="one.md")
        b = _record("same text", source="two.md")
        bundle_path = _signed_bundle(tmp_path / "b.json", key_path, [a, b], [_rel(a, b)])
        dst = await _make_storage(tmp_path, "dst")
        await dst.upsert_chunks([_make_chunk("same text")])

        stats = await import_chunks(dst, _FakeEmbedder(), bundle_path, provenance_key_path=key_path)

        assert stats.restored_relations == 0 and _edges(dst) == set()

    async def test_relations_that_are_not_a_list_reject_the_import(self, tmp_path):
        bundle_path = _foreign_bundle(tmp_path / "f.json", [_record(_CLEAN)], {"a": "b"})
        dst = await _make_storage(tmp_path, "dst")

        with pytest.raises(ValueError, match="relations"):
            await import_chunks(dst, _FakeEmbedder(), bundle_path)

        assert await dst.recall_chunks(limit=10) == []

    async def test_a_bundle_without_chunks_counts_its_relations_skipped(self, tmp_path):
        bundle_path = _foreign_bundle(tmp_path / "f.json", [], [{"x": 1}, {"y": 2}])
        dst = await _make_storage(tmp_path, "dst")
        with pytest.raises(ValueError, match="relation"):
            await import_chunks(dst, _FakeEmbedder(), bundle_path)
        assert (await dst.get_stats())["total_chunks"] == 0


class TestPrivacy:
    @pytest.fixture
    def counters(self, monkeypatch):
        from memtomem import privacy

        recorded: list[str] = []
        audits: list[dict] = []
        monkeypatch.setattr(privacy, "record", lambda outcome, tool: recorded.append(outcome))
        monkeypatch.setattr(
            privacy,
            "emit_bypass_audit",
            lambda *, surface, content_chars, hits, audit_context=None: audits.append(
                {"content_chars": content_chars, "hits": hits}
            ),
        )
        return recorded, audits

    async def test_a_secret_relation_type_blocks_a_clean_bundle(self, tmp_path, counters):
        recorded, _ = counters
        a, b = _record(_CLEAN, source="a.md"), _record(_CLEAN + "b", source="b.md")
        bundle_path = _foreign_bundle(tmp_path / "f.json", [a, b], [_rel(a, b, _SECRET)])
        dst = await _make_storage(tmp_path, "dst")

        with pytest.raises(ImportPrivacyError) as excinfo:
            await import_chunks(dst, _FakeEmbedder(), bundle_path)

        assert excinfo.value.blocked_records == 1
        assert recorded == ["blocked"]
        assert await dst.recall_chunks(limit=10) == []

    async def test_blocked_records_and_relations_count_together(self, tmp_path, counters):
        a, b = _record(_SECRET, source="a.md"), _record(_CLEAN, source="b.md")
        bundle_path = _foreign_bundle(tmp_path / "f.json", [a, b], [_rel(a, b, _SECRET)])
        dst = await _make_storage(tmp_path, "dst")

        with pytest.raises(ImportPrivacyError) as excinfo:
            await import_chunks(dst, _FakeEmbedder(), bundle_path)

        assert excinfo.value.blocked_records == 2

    async def test_a_relation_that_would_not_land_still_blocks(self, tmp_path, counters):
        a = _record(_CLEAN)
        relations = [{"source_id": "nowhere", "target_id": "nothing", "relation_type": _SECRET}]
        bundle_path = _foreign_bundle(tmp_path / "f.json", [a], relations)
        dst = await _make_storage(tmp_path, "dst")
        with pytest.raises(ValueError, match="relation"):
            await import_chunks(dst, _FakeEmbedder(), bundle_path)
        assert (await dst.get_stats())["total_chunks"] == 0

    async def test_force_unsafe_audits_each_relation_without_its_bytes(self, tmp_path, counters):
        recorded, audits = counters
        a, b = _record(_CLEAN, source="a.md"), _record(_CLEAN + "b", source="b.md")
        bundle_path = _foreign_bundle(tmp_path / "f.json", [a, b], [_rel(a, b, _SECRET)])
        dst = await _make_storage(tmp_path, "dst")

        stats = await import_chunks(dst, _FakeEmbedder(), bundle_path, force_unsafe=True)

        assert stats.restored_relations == 1
        assert recorded == ["pass", "pass", "bypassed"]
        assert audits == [{"content_chars": len(_SECRET), "hits": audits[0]["hits"]}]
        assert audits[0]["hits"] >= 1
        assert _SECRET not in json.dumps(audits)


class TestWriteFailure:
    async def _failing(self, storage, monkeypatch):
        async def boom(edges):
            raise RuntimeError("edge write failed")

        monkeypatch.setattr(storage, "insert_relations", boom)

    async def test_a_failed_edge_write_rolls_the_chunks_back(self, tmp_path, monkeypatch):
        key_path = tmp_path / "k.provenance_key"
        src = await _consolidated_store(tmp_path)
        bundle_path = tmp_path / "backup.json"
        await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)
        dst = await _make_storage(tmp_path, "dst")
        await self._failing(dst, monkeypatch)

        stats = await import_chunks(dst, _FakeEmbedder(), bundle_path, provenance_key_path=key_path)

        assert (stats.imported_chunks, stats.failed_chunks) == (0, 4)
        assert (stats.restored_relations, stats.failed_relations) == (0, 4)
        assert await dst.recall_chunks(limit=10) == []

    async def test_a_failed_edge_only_repair_is_reported(self, tmp_path, monkeypatch):
        key_path = tmp_path / "k.provenance_key"
        src = await _consolidated_store(tmp_path)
        bundle_path = tmp_path / "backup.json"
        await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)
        src._get_db().execute("DELETE FROM chunk_relations")
        src._get_db().commit()
        await self._failing(src, monkeypatch)

        stats = await import_chunks(src, _FakeEmbedder(), bundle_path, provenance_key_path=key_path)

        assert stats.failed_chunks == 0
        assert (stats.restored_relations, stats.failed_relations) == (0, 4)
        assert _edges(src) == set()


class TestPolicySummary:
    """A policy summary's ``Source hash:`` is over chunk ids, so fresh ids make it stale."""

    async def _policy_store(self, tmp_path):
        from memtomem.tools.policy_engine import execute_auto_consolidate

        storage = await _make_storage(tmp_path, "src")
        source = str(tmp_path / "notes.md")
        notes = [_make_chunk(f"policy note {i}", source=source) for i in range(3)]
        await storage.upsert_chunks(notes)
        result = await execute_auto_consolidate(
            storage, {"min_group_size": 2}, None, dry_run=False, extract_entities=False
        )
        assert result.affected_count == 1
        return storage

    async def _restored(self, tmp_path, *, preserve_ids, import_key=None):
        from memtomem.tools.policy_engine import execute_auto_consolidate

        key_path = tmp_path / "k.provenance_key"
        src = await self._policy_store(tmp_path)
        bundle_path = tmp_path / "backup.json"
        await export_chunks(src, output_path=bundle_path, provenance_key_path=key_path)
        dst = await _make_storage(tmp_path, "dst")
        await import_chunks(
            dst,
            _FakeEmbedder(),
            bundle_path,
            preserve_ids=preserve_ids,
            provenance_key_path=import_key or key_path,
        )
        run = await execute_auto_consolidate(
            dst, {"min_group_size": 2}, None, dry_run=False, extract_entities=False
        )
        summaries = await dst.list_chunks_by_source(
            tmp_path / "notes.md.consolidated.md", limit=20, as_stored=True
        )
        return run, summaries

    async def test_a_preserved_id_restore_leaves_the_policy_idempotent(self, tmp_path):
        run, summaries = await self._restored(tmp_path, preserve_ids=True)

        assert (run.affected_count, run.error, run.outcome) == (0, None, {})
        assert len(summaries) == 1

    async def test_a_fresh_id_restore_regenerates_the_summary_in_place(self, tmp_path):
        run, summaries = await self._restored(tmp_path, preserve_ids=False)

        assert (run.affected_count, run.error) == (1, None)
        assert len(summaries) == 1

    @pytest.mark.parametrize("preserve_ids", [True, False])
    async def test_a_restore_without_the_key_leaves_the_summary_foreign(
        self, tmp_path, preserve_ids
    ):
        """Without the exporting install's key the bundle is foreign, and a
        foreign record never carries ``origin`` (#2161): the policy no longer
        owns the summary and refuses to touch its path, whatever the ids."""
        run, summaries = await self._restored(
            tmp_path, preserve_ids=preserve_ids, import_key=tmp_path / "other.key"
        )

        assert run.affected_count == 0
        assert "collision" in (run.error or "")
        assert len(summaries) == 1


class TestConsolidateAfterRestore:
    async def test_a_restored_group_is_not_listed_again(self, components, memory_dir, tmp_path):
        """The issue end to end: apply, back up, lose the store, restore, scan."""
        from memtomem.models import Chunk, ChunkMetadata
        from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply
        from tests.test_tools_logic import _fake_ctx

        source = memory_dir / "notes.md"
        await components.storage.upsert_chunks(
            [
                Chunk(
                    content=f"original note {i}",
                    metadata=ChunkMetadata(source_file=source),
                    embedding=[0.1] * 1024,
                )
                for i in range(3)
            ]
        )
        ctx = _fake_ctx(components)
        assert "original note" in await mem_consolidate(min_group_size=2, ctx=ctx)
        await mem_consolidate_apply(group_id=0, summary="summary of the notes", ctx=ctx)
        assert "No consolidation candidates" in await mem_consolidate(min_group_size=2, ctx=ctx)

        key_path = tmp_path / "k.provenance_key"
        bundle_path = tmp_path / "backup.json"
        await export_chunks(
            components.storage, output_path=bundle_path, provenance_key_path=key_path
        )
        all_ids = list(_ids(components.storage).values())
        await components.storage.delete_chunks(all_ids)
        assert _edges(components.storage) == set()

        await import_chunks(
            components.storage,
            _FakeEmbedder(dim=1024),
            bundle_path,
            provenance_key_path=key_path,
        )

        assert "No consolidation candidates" in await mem_consolidate(min_group_size=2, ctx=ctx)


def _rows(storage: SqliteBackend) -> dict[UUID, tuple[str, int, list[str]]]:
    """Every row's ``(namespace, start_line, tags)``, by id."""
    db = storage._get_db()
    return {
        UUID(cid): (ns, line, json.loads(tags))
        for cid, ns, line, tags in db.execute("SELECT id, namespace, start_line, tags FROM chunks")
    }


def _edge_ids(storage: SqliteBackend) -> set[tuple[UUID, UUID, str]]:
    db = storage._get_db()
    return {
        (UUID(s), UUID(t), rel)
        for s, t, rel in db.execute(
            "SELECT source_id, target_id, relation_type FROM chunk_relations"
        )
    }


def _at(content: str, line: int, *, source: str = "notes.md", ns: str = "default"):
    chunk = _make_chunk(content, source=source, ns=ns)
    chunk.metadata = replace(chunk.metadata, start_line=line, end_line=line)
    return chunk


async def _export_records(storage: SqliteBackend, tmp_path: Path, key_path: Path) -> list[dict]:
    bundle = await export_chunks(
        storage, output_path=tmp_path / "export.json", provenance_key_path=key_path
    )
    return bundle.chunks


class TestSameTextOriginals:
    """Two rows holding one text: a record matches the row it is (#2829).

    ``_edges`` reads ends by content and cannot tell these rows apart, so
    everything here asserts by row id.
    """

    async def _store(self, tmp_path: Path):
        storage = await _make_storage(tmp_path, "src")
        a, b = _at("same text", 1), _at("same text", 10)
        summary = _at("summary of the notes", 1, source="summary.md")
        await storage.upsert_chunks([a, b, summary])
        for original in (a, b):
            await storage.add_relation(original.id, summary.id, "consolidated_into")
        return storage, a.id, b.id, summary.id

    async def test_a_skip_reimport_covers_both_originals(self, tmp_path):
        key_path = tmp_path / "k.provenance_key"
        storage, a, b, summary = await self._store(tmp_path)
        bundle_path = tmp_path / "backup.json"
        await export_chunks(storage, output_path=bundle_path, provenance_key_path=key_path)
        storage._get_db().execute("DELETE FROM chunk_relations")
        storage._get_db().commit()

        stats = await import_chunks(
            storage, _FakeEmbedder(), bundle_path, provenance_key_path=key_path
        )

        assert stats.restored_relations == 2
        assert _edge_ids(storage) == {
            (a, summary, "consolidated_into"),
            (b, summary, "consolidated_into"),
        }

    async def test_an_update_reimport_writes_each_record_onto_its_own_row(self, tmp_path):
        key_path = tmp_path / "k.provenance_key"
        storage, a, b, _ = await self._store(tmp_path)
        records = await _export_records(storage, tmp_path, key_path)
        for record in records:
            if record["chunk_id"] == str(b):
                record["tags"] = ["edited"]
        bundle_path = _signed_bundle(tmp_path / "b.json", key_path, records, [])

        stats = await import_chunks(
            storage,
            _FakeEmbedder(),
            bundle_path,
            on_conflict="update",
            provenance_key_path=key_path,
        )

        assert (stats.updated_chunks, stats.failed_chunks) == (3, 0)
        rows = _rows(storage)
        assert rows[a] == ("default", 1, [])
        assert rows[b] == ("default", 10, ["edited"])

    async def test_an_update_of_one_original_does_not_move_it_onto_its_sibling(self, tmp_path):
        """Matching any row of the text could move it onto the other row's key."""
        key_path = tmp_path / "k.provenance_key"
        storage, a, b, _ = await self._store(tmp_path)
        records = [r for r in await _export_records(storage, tmp_path, key_path)]
        only_a = [r for r in records if r["chunk_id"] == str(a)]
        only_a[0]["tags"] = ["edited"]
        bundle_path = _signed_bundle(tmp_path / "a.json", key_path, only_a, [])

        stats = await import_chunks(
            storage,
            _FakeEmbedder(),
            bundle_path,
            on_conflict="update",
            provenance_key_path=key_path,
        )

        assert (stats.updated_chunks, stats.failed_chunks) == (1, 0)
        rows = _rows(storage)
        assert rows[a] == ("default", 1, ["edited"])
        assert rows[b] == ("default", 10, [])

    async def test_a_foreign_bundle_matches_by_key_not_by_its_ids(self, tmp_path):
        key_path = tmp_path / "k.provenance_key"
        storage, a, b, summary = await self._store(tmp_path)
        records = await _export_records(storage, tmp_path, key_path)
        record_b = next(r for r in records if r["chunk_id"] == str(b))
        record_s = next(r for r in records if r["chunk_id"] == str(summary))
        # Names row a by id, but sits at row b's key.
        record_b["chunk_id"] = str(a)
        record_b["tags"] = ["edited"]
        bundle_path = _foreign_bundle(
            tmp_path / "f.json", [record_b, record_s], [_rel(record_b, record_s, "related")]
        )

        stats = await import_chunks(
            storage,
            _FakeEmbedder(),
            bundle_path,
            on_conflict="update",
            provenance_key_path=key_path,
        )

        assert stats.failed_chunks == 0
        rows = _rows(storage)
        assert rows[a] == ("default", 1, [])
        assert rows[b] == ("default", 10, ["edited"])
        # Both ends are rows the store already had: a foreign bundle links none.
        assert stats.restored_relations == 0

    async def test_a_foreign_id_does_not_choose_the_row_an_update_overwrites(self, tmp_path):
        """At a key no row holds, the record falls back to a row, not the one it names."""
        key_path = tmp_path / "k.provenance_key"
        storage, a, b, _ = await self._store(tmp_path)
        record = _record("same text")
        record["chunk_id"] = str(b)
        record["start_line"] = record["end_line"] = 50
        bundle_path = _foreign_bundle(tmp_path / "f.json", [record], [])

        stats = await import_chunks(
            storage,
            _FakeEmbedder(),
            bundle_path,
            on_conflict="update",
            provenance_key_path=key_path,
        )

        assert stats.updated_chunks == 1
        rows = _rows(storage)
        assert rows[a][:2] == ("default", 50)
        assert rows[b] == ("default", 10, [])

    async def test_a_namespace_override_keeps_each_record_on_its_own_row(self, tmp_path):
        """No row sits at the new key, so a self-export's ids decide."""
        key_path = tmp_path / "k.provenance_key"
        storage, a, b, _ = await self._store(tmp_path)
        records = await _export_records(storage, tmp_path, key_path)
        # Bundle order opposite to row order, so the fallback alone would swap them.
        records.sort(key=lambda r: r["start_line"], reverse=True)
        bundle_path = _signed_bundle(tmp_path / "o.json", key_path, records, [])

        stats = await import_chunks(
            storage,
            _FakeEmbedder(),
            bundle_path,
            namespace="other",
            on_conflict="update",
            provenance_key_path=key_path,
        )

        assert stats.failed_chunks == 0
        rows = _rows(storage)
        assert rows[a][:2] == ("other", 1)
        assert rows[b][:2] == ("other", 10)

    async def test_an_occupied_key_wins_over_the_signed_id(self, tmp_path):
        key_path = tmp_path / "k.provenance_key"
        storage = await _make_storage(tmp_path, "src")
        old, new = _at("same text", 1, ns="old"), _at("same text", 1, ns="new")
        await storage.upsert_chunks([old, new])
        records = [
            r for r in await _export_records(storage, tmp_path, key_path) if r["namespace"] == "old"
        ]
        records[0]["tags"] = ["edited"]
        bundle_path = _signed_bundle(tmp_path / "n.json", key_path, records, [])

        stats = await import_chunks(
            storage,
            _FakeEmbedder(),
            bundle_path,
            namespace="new",
            on_conflict="update",
            provenance_key_path=key_path,
        )

        assert (stats.updated_chunks, stats.failed_chunks) == (1, 0)
        rows = _rows(storage)
        assert rows[old.id] == ("old", 1, [])
        assert rows[new.id] == ("new", 1, ["edited"])

    async def test_two_records_for_one_key_write_one_row_once(self, tmp_path):
        """Both land on the one row; the vector table takes one write per row."""
        key_path = tmp_path / "k.provenance_key"
        storage = await _make_storage(tmp_path, "src")
        row = _at("same text", 5)
        await storage.upsert_chunks([row])
        first, second = _record("same text", ns="fresh"), _record("same text", ns="fresh")
        second["tags"] = ["second"]
        bundle_path = _foreign_bundle(tmp_path / "d.json", [first, second], [])

        stats = await import_chunks(
            storage,
            _FakeEmbedder(),
            bundle_path,
            on_conflict="update",
            provenance_key_path=key_path,
        )

        assert (stats.updated_chunks, stats.conflict_skipped_chunks) == (1, 1)
        assert stats.failed_chunks == 0
        assert _rows(storage) == {row.id: ("fresh", 1, [])}

    @pytest.mark.parametrize(("namespace", "stored"), [(1, "1"), (True, "1"), (1e20, "1.0e+20")])
    async def test_a_namespace_that_is_not_a_string_is_a_malformed_record(
        self, tmp_path, namespace, stored
    ):
        key_path = tmp_path / "k.provenance_key"
        storage = await _make_storage(tmp_path, "src")
        a, b = _at("same text", 2), _at("same text", 1, ns=stored)
        await storage.upsert_chunks([a, b])
        record = _record("same text")
        record["namespace"] = namespace
        bundle_path = _foreign_bundle(tmp_path / "i.json", [record], [])
        with pytest.raises(ValueError, match="namespace"):
            await import_chunks(
                storage,
                _FakeEmbedder(),
                bundle_path,
                on_conflict="update",
                provenance_key_path=key_path,
            )
        assert _rows(storage) == {a.id: ("default", 2, []), b.id: (stored, 1, [])}

    async def test_a_path_that_cannot_resolve_is_one_malformed_record(self, tmp_path):
        storage = await _make_storage(tmp_path, "src")
        row = _at("same text", 1)
        await storage.upsert_chunks([row])
        broken = _record("same text", source="no\x00tes.md")
        fresh = _record("a new note")
        bundle_path = _foreign_bundle(tmp_path / "z.json", [broken, fresh], [])
        with pytest.raises(ValueError, match="source_file"):
            await import_chunks(storage, _FakeEmbedder(), bundle_path)
        assert set(_ids(storage)) == {"same text"}

    @pytest.mark.requires_symlinks
    async def test_a_path_spelled_through_a_symlink_matches_its_row(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        (tmp_path / "link").symlink_to(real, target_is_directory=True)
        key_path = tmp_path / "k.provenance_key"
        storage = await _make_storage(tmp_path, "src")
        a = _at("same text", 1, source=str(real / "notes.md"))
        b = _at("same text", 10, source=str(real / "notes.md"))
        await storage.upsert_chunks([a, b])
        record = _record("same text", source=str(tmp_path / "link" / "notes.md"))
        record["start_line"] = record["end_line"] = 10
        record["tags"] = ["edited"]
        bundle_path = _foreign_bundle(tmp_path / "l.json", [record], [])

        await import_chunks(
            storage,
            _FakeEmbedder(),
            bundle_path,
            on_conflict="update",
            provenance_key_path=key_path,
        )

        rows = _rows(storage)
        assert rows[a.id] == ("default", 1, [])
        assert rows[b.id] == ("default", 10, ["edited"])


class TestMatchConflicts:
    """The pass order of ``_match_conflicts``, on rows built by hand."""

    @staticmethod
    def _row(line: int) -> ChunkKeyRow:
        return ChunkKeyRow(uuid4(), "default", norm_path(Path("notes.md")), line)

    @staticmethod
    def _parsed(line: int, chunk_id: str | None = None):
        return (_at("same text", line), chunk_id)

    @staticmethod
    def _match(parsed, rows, *, trust_ids: bool):
        keys = [_destination_key(chunk) for chunk, _ in parsed]
        return _match_conflicts(parsed, keys, rows, trust_ids=trust_ids)

    async def test_a_key_match_beats_an_earlier_records_fallback(self):
        r1, r10 = self._row(1), self._row(10)
        unplaced, placed = self._parsed(99), self._parsed(1)
        rows = {placed[0].content_hash: [r1, r10]}

        matches = self._match([unplaced, placed], rows, trust_ids=False)

        assert matches == [(r10.id, True), (r1.id, True)]

    async def test_an_unusable_id_falls_through_to_a_free_row(self):
        r1, r10 = self._row(1), self._row(10)
        other_text = self._row(99)  # free, but holding another text
        parsed = [self._parsed(98, "not-a-uuid"), self._parsed(99, str(other_text.id))]
        rows = {parsed[0][0].content_hash: [r1, r10], "other hash": [other_text]}

        matches = self._match(parsed, rows, trust_ids=True)

        assert matches == [(r1.id, True), (r10.id, True)]

    async def test_records_past_the_last_free_row_share_one_and_write_nothing(self):
        r1 = self._row(1)
        parsed = [self._parsed(1), self._parsed(50)]
        rows = {parsed[0][0].content_hash: [r1]}

        matches = self._match(parsed, rows, trust_ids=False)

        assert matches == [(r1.id, True), (r1.id, False)]
