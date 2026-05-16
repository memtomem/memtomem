"""Storage-side coverage for temporal queries.

Pure-function coverage of `build_timeline`, `format_timeline`, and
`format_activity` lives in `test_tools_logic.py::TestTemporal`. This file
covers the storage method `Storage.get_activity_summary`, which needs a
real DB fixture and so does not fit alongside the pure-function tests.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.models import Chunk, ChunkMetadata


def _make_chunk(
    content="test",
    tags=(),
    namespace="default",
    source="test.md",
    created_at=None,
):
    chunk = Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(f"/tmp/{source}"),
            tags=tuple(tags),
            namespace=namespace,
        ),
        content_hash=f"hash-{uuid4().hex[:8]}",
        embedding=[0.1] * 1024,
    )
    if created_at is not None:
        chunk.created_at = created_at
    return chunk


class TestActivitySummaryStorage:
    @pytest.mark.asyncio
    async def test_counts(self, storage):
        # Pin created_at to a fixed instant so the test cannot straddle UTC
        # midnight between Chunk construction and the strftime() below.
        fixed_dt = datetime(2025, 3, 10, 12, 0, 0, tzinfo=timezone.utc)
        c1 = _make_chunk("chunk1", source="a.md", created_at=fixed_dt)
        c2 = _make_chunk("chunk2", source="b.md", created_at=fixed_dt)
        await storage.upsert_chunks([c1, c2])

        summary = await storage.get_activity_summary()
        target_day = fixed_dt.strftime("%Y-%m-%d")
        day_data = [d for d in summary if d["date"] == target_day]
        assert len(day_data) == 1
        assert day_data[0]["created"] == 2

    @pytest.mark.asyncio
    async def test_empty_range(self, storage):
        summary = await storage.get_activity_summary(since="2020-01-01", until="2020-01-02")
        assert len(summary) == 0
