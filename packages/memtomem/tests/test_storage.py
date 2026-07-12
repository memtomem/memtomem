"""Tests for storage backend operations."""

import dataclasses
import unicodedata
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from helpers import make_chunk as _make_chunk
from memtomem.models import Chunk, ChunkMetadata, ChunkType
from memtomem.storage.base import SearchMetadataFilter
from memtomem.storage.sqlite_backend import _classify_startup_error
from memtomem.storage.sqlite_helpers import norm_path


class TestChunkCRUD:
    @pytest.mark.asyncio
    async def test_upsert_and_get(self, storage):
        chunk = _make_chunk("hello world")
        await storage.upsert_chunks([chunk])
        result = await storage.get_chunk(chunk.id)
        assert result is not None
        assert result.content == "hello world"

    @pytest.mark.asyncio
    async def test_delete_chunks(self, storage):
        chunk = _make_chunk()
        await storage.upsert_chunks([chunk])
        deleted = await storage.delete_chunks([chunk.id])
        assert deleted == 1
        assert await storage.get_chunk(chunk.id) is None

    @pytest.mark.asyncio
    async def test_get_nonexistent(self, storage):
        result = await storage.get_chunk(uuid4())
        assert result is None


class TestSearchMetadataFilters:
    @pytest.mark.asyncio
    async def test_bm25_applies_exact_source_type_and_date_before_limit(self, storage):
        now = datetime.now(UTC)
        excluded = _make_chunk("shared marker", source="outside,comma.md")
        included = _make_chunk("shared marker", source="inside.md")
        excluded.metadata = dataclasses.replace(excluded.metadata, chunk_type=ChunkType.RAW_TEXT)
        included.metadata = dataclasses.replace(
            included.metadata, chunk_type=ChunkType.MARKDOWN_SECTION
        )
        excluded.created_at = now - timedelta(days=30)
        included.created_at = now
        await storage.upsert_chunks([excluded, included])

        results = await storage.bm25_search(
            "shared",
            top_k=1,
            metadata_filter=SearchMetadataFilter(
                source_exact=(str(included.metadata.source_file),),
                chunk_types=(ChunkType.MARKDOWN_SECTION.value,),
                created_from=now - timedelta(days=1),
                created_before=now + timedelta(days=1),
            ),
        )

        assert [result.chunk.id for result in results] == [included.id]


class TestStorageStartupClassification:
    @pytest.mark.parametrize(
        ("error", "stage", "reason", "retryable"),
        [
            (PermissionError("denied"), "parent", "storage_permission_denied", False),
            (RuntimeError("database is locked"), "journal", "storage_locked", True),
            (
                RuntimeError("unable to open database file"),
                "open",
                "storage_path_unavailable",
                False,
            ),
            (RuntimeError("boom"), "schema", "storage_unavailable", False),
        ],
    )
    def test_classifies_without_leaking_original_detail(self, error, stage, reason, retryable):
        classified = _classify_startup_error(error, stage)
        assert classified.reason_code == reason
        assert classified.stage == stage
        assert classified.retryable is retryable
        assert "boom" not in str(classified)


class TestChunkUniqueness:
    """Regression for #691: duplicate chunks within a single source.

    Real-world cause: ``mm web`` watcher + ``mm`` MCP / CLI indexing the same
    file from separate processes. Each process holds its own asyncio.Lock
    and SQLite is the only shared coordination point — so without a UNIQUE
    constraint plus INSERT OR IGNORE, both inserts succeed and produce
    rows that share (namespace, source_file, content_hash, start_line) but
    differ only in id. Once present, those rows survive subsequent
    re-indexing because the differ never sees their hash as "stale".
    """

    @staticmethod
    def _twin_chunks() -> tuple[Chunk, Chunk]:
        # ``content_hash`` is filled by ``Chunk.__post_init__`` from the
        # NFC-normalised content, so two chunks built from the same string
        # produce the same hash with different uuid4 ids — exactly the
        # shape the multi-process race produces in the wild.
        def _mk() -> Chunk:
            return Chunk(
                content="duplicate body",
                metadata=ChunkMetadata(
                    source_file=Path("/tmp/dup.md"),
                    start_line=10,
                    end_line=20,
                    namespace="default",
                ),
                embedding=[0.1] * 1024,
            )

        return _mk(), _mk()

    @pytest.mark.asyncio
    async def test_second_upsert_with_same_hash_is_silently_ignored(self, storage):
        a, b = self._twin_chunks()
        assert a.content_hash == b.content_hash
        assert a.id != b.id

        await storage.upsert_chunks([a])
        await storage.upsert_chunks([b])

        rows = (
            storage._get_db()
            .execute(
                "SELECT id FROM chunks WHERE content_hash=? AND source_file=? "
                "AND namespace=? AND start_line=?",
                (a.content_hash, norm_path(Path("/tmp/dup.md")), "default", 10),
            )
            .fetchall()
        )
        assert len(rows) == 1, (
            f"expected 1 row after dup upsert, got {len(rows)} — "
            f"UNIQUE(namespace, source_file, content_hash, start_line) "
            f"missing or INSERT not using OR IGNORE"
        )

    @pytest.mark.asyncio
    async def test_within_batch_dup_collapses_to_one_row(self, storage):
        # Same race shape, single ``upsert_chunks`` call: a ``new_chunks``
        # batch with two identical-hash entries (e.g. a chunker bug emitting
        # the same section twice within one run) must not produce two rows.
        a, b = self._twin_chunks()

        await storage.upsert_chunks([a, b])

        rows = (
            storage._get_db()
            .execute("SELECT id FROM chunks WHERE content_hash=?", (a.content_hash,))
            .fetchall()
        )
        assert len(rows) == 1


class TestTags:
    @pytest.mark.asyncio
    async def test_tag_counts(self, storage):
        c1 = _make_chunk(tags=("python", "debug"))
        c2 = _make_chunk(tags=("python", "web"))
        await storage.upsert_chunks([c1, c2])
        counts = await storage.get_tag_counts()
        tag_dict = dict(counts)
        assert tag_dict.get("python") == 2
        assert tag_dict.get("debug") == 1

    @pytest.mark.asyncio
    async def test_rename_tag(self, storage):
        chunk = _make_chunk(tags=("old_tag",))
        await storage.upsert_chunks([chunk])
        updated = await storage.rename_tag("old_tag", "new_tag")
        assert updated == 1
        counts = dict(await storage.get_tag_counts())
        assert "new_tag" in counts
        assert "old_tag" not in counts

    @pytest.mark.asyncio
    async def test_delete_tag(self, storage):
        chunk = _make_chunk(tags=("remove_me", "keep_me"))
        await storage.upsert_chunks([chunk])
        await storage.delete_tag("remove_me")
        counts = dict(await storage.get_tag_counts())
        assert "remove_me" not in counts
        assert "keep_me" in counts

    @pytest.mark.asyncio
    async def test_list_chunks_by_tag(self, storage):
        c1 = _make_chunk(content="a", tags=("python",))
        c2 = _make_chunk(content="b", tags=("python", "web"))
        c3 = _make_chunk(content="c", tags=("rust",))
        await storage.upsert_chunks([c1, c2, c3])
        rows = await storage.list_chunks_by_tag("python", limit=10)
        ids = {r.id for r in rows}
        assert ids == {c1.id, c2.id}
        assert all("python" in r.metadata.tags for r in rows)

    @pytest.mark.asyncio
    async def test_list_chunks_by_tag_respects_limit(self, storage):
        chunks = [_make_chunk(content=f"x{i}", tags=("shared",)) for i in range(5)]
        await storage.upsert_chunks(chunks)
        rows = await storage.list_chunks_by_tag("shared", limit=2)
        assert len(rows) == 2

    @pytest.mark.asyncio
    async def test_count_chunks_by_tag(self, storage):
        c1 = _make_chunk(content="a", tags=("python",))
        c2 = _make_chunk(content="b", tags=("python", "web"))
        c3 = _make_chunk(content="c", tags=("rust",))
        await storage.upsert_chunks([c1, c2, c3])
        assert await storage.count_chunks_by_tag("python") == 2
        assert await storage.count_chunks_by_tag("rust") == 1
        assert await storage.count_chunks_by_tag("absent") == 0

    @pytest.mark.asyncio
    async def test_list_chunks_by_tag_samples_globally_across_scopes(self, storage):
        # Regression (#688): the dry-run sample must draw from the same global
        # row set that count/apply use. It previously routed through
        # ``recall_chunks``, which appends the ADR-0011 scope fragment — with no
        # project context pinned that narrowed samples to ``scope='user'``, so a
        # tag living only on project-tier chunks previewed as "N affected, 0
        # samples" while the global rename/delete/merge still mutated all N rows.
        proj_root = Path("/tmp/proj-x")
        user_chunk = _make_chunk(content="u", tags=("curate",))
        base_local = _make_chunk(content="p1", source="p1.md", tags=("curate",))
        proj_local = dataclasses.replace(
            base_local,
            metadata=dataclasses.replace(
                base_local.metadata, scope="project_local", project_root=proj_root
            ),
        )
        base_shared = _make_chunk(content="p2", source="p2.md", tags=("curate",))
        proj_shared = dataclasses.replace(
            base_shared,
            metadata=dataclasses.replace(
                base_shared.metadata, scope="project_shared", project_root=proj_root
            ),
        )
        await storage.upsert_chunks([user_chunk, proj_local, proj_shared])

        count = await storage.count_chunks_by_tag("curate")
        sample = await storage.list_chunks_by_tag("curate", limit=10)
        sample_ids = {c.id for c in sample}

        # Sample membership must match the global count — no scope narrowing.
        assert count == 3
        assert len(sample) == count
        # The project-tier rows the global apply would mutate must show up in
        # the preview; these are exactly the rows the old user-only sample
        # dropped, leaving a destructive op with a misleadingly empty preview.
        assert {user_chunk.id, proj_local.id, proj_shared.id} == sample_ids

    @pytest.mark.asyncio
    async def test_merge_tags(self, storage):
        c1 = _make_chunk(content="a", tags=("py", "code"))
        c2 = _make_chunk(content="b", tags=("python3",))
        c3 = _make_chunk(content="c", tags=("py", "python3"))  # collapses
        c4 = _make_chunk(content="d", tags=("rust",))  # untouched
        await storage.upsert_chunks([c1, c2, c3, c4])
        affected = await storage.merge_tags(["py", "python3"], "python")
        assert affected == 3
        # Reload and verify
        r1 = await storage.get_chunk(c1.id)
        r2 = await storage.get_chunk(c2.id)
        r3 = await storage.get_chunk(c3.id)
        r4 = await storage.get_chunk(c4.id)
        assert r1 is not None and set(r1.metadata.tags) == {"code", "python"}
        assert r2 is not None and set(r2.metadata.tags) == {"python"}
        # collapse: both source tags + dedup → single "python"
        assert r3 is not None and set(r3.metadata.tags) == {"python"}
        assert r4 is not None and set(r4.metadata.tags) == {"rust"}

    @pytest.mark.asyncio
    async def test_merge_tags_target_in_sources_is_noop_for_target(self, storage):
        c1 = _make_chunk(content="a", tags=("py", "python"))
        await storage.upsert_chunks([c1])
        # target appearing in sources is treated as "leave target alone"
        affected = await storage.merge_tags(["py", "python"], "python")
        assert affected == 1
        r1 = await storage.get_chunk(c1.id)
        assert r1 is not None and set(r1.metadata.tags) == {"python"}

    @pytest.mark.asyncio
    async def test_merge_tags_empty_sources_no_writes(self, storage):
        c1 = _make_chunk(content="a", tags=("py",))
        await storage.upsert_chunks([c1])
        assert await storage.merge_tags([], "python") == 0
        assert await storage.merge_tags(["python"], "python") == 0
        r1 = await storage.get_chunk(c1.id)
        assert r1 is not None and set(r1.metadata.tags) == {"py"}

    @staticmethod
    def _force_old_updated_at(storage, chunk_id, iso="2026-01-01T00:00:00+00:00"):
        """Pin a chunk's updated_at to a known-old value via direct SQL.

        Avoids the asyncio.sleep-1-second hack the isoformat(timespec="seconds")
        precision would otherwise force on the test.
        """
        db = storage._get_db()
        db.execute("UPDATE chunks SET updated_at = ? WHERE id = ?", (iso, str(chunk_id)))
        db.commit()

    @pytest.mark.asyncio
    async def test_rename_tag_bumps_updated_at_symmetric(self, storage):
        """Symmetric pin: positive (renamed chunk's ``updated_at`` moves
        forward) + negative (untouched chunk's ``updated_at`` stays put).

        ``decay.py`` reads ``updated_at`` for age, so the bump is what
        propagates the rename to decay scoring downstream. A negative-only
        assertion would false-pass against a no-op implementation; both
        halves are required (feedback_pin_invert_symmetric_assertion).
        """
        renamed = _make_chunk(content="a", tags=("old_tag",))
        untouched = _make_chunk(content="b", tags=("other",))
        await storage.upsert_chunks([renamed, untouched])
        self._force_old_updated_at(storage, renamed.id)
        self._force_old_updated_at(storage, untouched.id)

        before_renamed = await storage.get_chunk(renamed.id)
        before_untouched = await storage.get_chunk(untouched.id)
        assert before_renamed is not None and before_untouched is not None

        affected = await storage.rename_tag("old_tag", "new_tag")
        assert affected == 1

        after_renamed = await storage.get_chunk(renamed.id)
        after_untouched = await storage.get_chunk(untouched.id)
        assert after_renamed is not None and after_untouched is not None

        # Positive: renamed chunk's updated_at moved forward
        assert after_renamed.updated_at > before_renamed.updated_at
        # Negative: untouched chunk stayed anchored
        assert after_untouched.updated_at == before_untouched.updated_at

    @pytest.mark.asyncio
    async def test_delete_tag_bumps_updated_at_symmetric(self, storage):
        deleted = _make_chunk(content="a", tags=("remove_me", "keep"))
        untouched = _make_chunk(content="b", tags=("other",))
        await storage.upsert_chunks([deleted, untouched])
        self._force_old_updated_at(storage, deleted.id)
        self._force_old_updated_at(storage, untouched.id)

        before_deleted = await storage.get_chunk(deleted.id)
        before_untouched = await storage.get_chunk(untouched.id)
        assert before_deleted is not None and before_untouched is not None

        await storage.delete_tag("remove_me")

        after_deleted = await storage.get_chunk(deleted.id)
        after_untouched = await storage.get_chunk(untouched.id)
        assert after_deleted is not None and after_untouched is not None

        assert after_deleted.updated_at > before_deleted.updated_at
        assert after_untouched.updated_at == before_untouched.updated_at

    @pytest.mark.asyncio
    async def test_merge_tags_bumps_updated_at_symmetric(self, storage):
        merged = _make_chunk(content="a", tags=("py",))
        collapsed = _make_chunk(content="b", tags=("py", "python"))
        untouched = _make_chunk(content="c", tags=("rust",))
        await storage.upsert_chunks([merged, collapsed, untouched])
        for c in (merged, collapsed, untouched):
            self._force_old_updated_at(storage, c.id)

        before_merged = await storage.get_chunk(merged.id)
        before_collapsed = await storage.get_chunk(collapsed.id)
        before_untouched = await storage.get_chunk(untouched.id)
        assert before_merged and before_collapsed and before_untouched

        affected = await storage.merge_tags(["py"], "python")
        assert affected == 2

        after_merged = await storage.get_chunk(merged.id)
        after_collapsed = await storage.get_chunk(collapsed.id)
        after_untouched = await storage.get_chunk(untouched.id)
        assert after_merged and after_collapsed and after_untouched

        assert after_merged.updated_at > before_merged.updated_at
        assert after_collapsed.updated_at > before_collapsed.updated_at
        assert after_untouched.updated_at == before_untouched.updated_at


class TestAccess:
    @pytest.mark.asyncio
    async def test_increment_and_get(self, storage):
        chunk = _make_chunk()
        await storage.upsert_chunks([chunk])
        await storage.increment_access([chunk.id])
        await storage.increment_access([chunk.id])
        counts = await storage.get_access_counts([chunk.id])
        assert counts[str(chunk.id)] == 2

    @pytest.mark.asyncio
    async def test_empty_access_counts(self, storage):
        counts = await storage.get_access_counts([])
        assert counts == {}


class TestRelations:
    @pytest.mark.asyncio
    async def test_add_and_get_related(self, storage):
        c1, c2 = _make_chunk(), _make_chunk()
        await storage.upsert_chunks([c1, c2])
        await storage.add_relation(c1.id, c2.id, "related")
        related = await storage.get_related(c1.id)
        assert len(related) == 1
        assert related[0][0] == c2.id

    @pytest.mark.asyncio
    async def test_bidirectional(self, storage):
        c1, c2 = _make_chunk(), _make_chunk()
        await storage.upsert_chunks([c1, c2])
        await storage.add_relation(c1.id, c2.id)
        # Query from either direction
        assert len(await storage.get_related(c1.id)) == 1
        assert len(await storage.get_related(c2.id)) == 1

    @pytest.mark.asyncio
    async def test_delete_relation(self, storage):
        c1, c2 = _make_chunk(), _make_chunk()
        await storage.upsert_chunks([c1, c2])
        await storage.add_relation(c1.id, c2.id)
        removed = await storage.delete_relation(c1.id, c2.id)
        assert removed is True
        assert len(await storage.get_related(c1.id)) == 0


class TestNormPathUnicode:
    """Regression for #235: ``norm_path`` must collapse NFD and NFC into one form."""

    def test_nfd_and_nfc_korean_paths_compare_equal(self, tmp_path):
        nfd_path = tmp_path / unicodedata.normalize("NFD", "내 드라이브") / "file.md"
        nfc_path = tmp_path / unicodedata.normalize("NFC", "내 드라이브") / "file.md"
        # Sanity: the raw Path strings differ before normalization, so the
        # equality below actually depends on the NFC step inside norm_path.
        assert str(nfd_path) != str(nfc_path)
        assert norm_path(nfd_path) == norm_path(nfc_path)

    def test_norm_path_output_is_nfc(self, tmp_path):
        nfd_path = tmp_path / unicodedata.normalize("NFD", "내 드라이브") / "file.md"
        result = norm_path(nfd_path)
        assert result == unicodedata.normalize("NFC", result)

    def test_norm_path_osError_fallback_still_normalizes(self, monkeypatch):
        # If ``Path.resolve`` raises, ``norm_path`` falls back to the input
        # string — it must still NFC-normalize that fallback. Comparison goes
        # through ``as_posix()`` so the assertion is portable: norm_path uses
        # ``str(p)`` on fallback which is platform-separator-dependent
        # (backslash on Windows), but the NFC property under test is not.
        nfd = unicodedata.normalize("NFD", "/tmp/내 드라이브/file.md")

        def _boom(self, strict=False):
            raise OSError("boom")

        monkeypatch.setattr(Path, "resolve", _boom)

        out = norm_path(Path(nfd))
        assert Path(out).as_posix() == unicodedata.normalize("NFC", nfd)
