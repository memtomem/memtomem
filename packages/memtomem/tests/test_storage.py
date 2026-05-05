"""Tests for storage backend operations."""

import unicodedata
from pathlib import Path
from uuid import uuid4

import pytest

from helpers import make_chunk as _make_chunk
from memtomem.models import Chunk, ChunkMetadata
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
