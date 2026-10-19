"""An apply deletes only the consolidation preview it read (#2798).

``mem_consolidate`` stores its groups under one scratch key, and a second scan
replaces the row. ``mem_consolidate_apply`` used to delete that key whenever
it finished with the preview, so a scan that ran while an apply was in flight
lost its preview, and its caller was later told to run ``mem_consolidate``
first. At four exits a real rescan replaces the preview mid-apply; the expiry
exit returns right after its read, so there the replacement is written between
the read and the delete. Each test checks that the newer preview survives.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memtomem.models import Chunk, ChunkMetadata, IndexingStats

pytestmark = pytest.mark.asyncio

_KEY = "consolidation_groups"


def _chunk(text: str, source: Path) -> Chunk:
    return Chunk(content=text, metadata=ChunkMetadata(source_file=source), embedding=[0.1] * 1024)


def _groups(entry: dict) -> list[dict]:
    return json.loads(entry["value"])["groups"]


async def _scanned(components, memory_dir):
    """Two indexed originals and a preview of them; returns ``(ctx, read_entry)``."""
    from memtomem.server.tools.consolidation import mem_consolidate
    from tests.test_tools_logic import _fake_ctx

    path = memory_dir / "notes.md"
    originals = [_chunk(f"original note {i}", path) for i in range(2)]
    await components.storage.upsert_chunks(originals)
    ctx = _fake_ctx(components)
    assert "original note" in await mem_consolidate(min_group_size=2, ctx=ctx)
    return ctx, path, await components.storage.scratch_get(_KEY)


async def _rescan(components, ctx) -> dict:
    """The newer preview a concurrent ``mem_consolidate`` writes."""
    from memtomem.server.tools.consolidation import mem_consolidate

    assert "original note" in await mem_consolidate(min_group_size=2, ctx=ctx)
    return await components.storage.scratch_get(_KEY)


def _publish_then(monkeypatch, after):
    from memtomem.server.tools import memory_crud

    real = memory_crud._mem_add_core

    async def publish_then(*args, **kwargs):
        result, stats = await real(*args, **kwargs)
        await after()
        return result, stats

    monkeypatch.setattr(memory_crud, "_mem_add_core", publish_then)


async def _assert_newer_survives(components, read: dict, newer: dict) -> None:
    assert newer["value"] != read["value"]
    kept = await components.storage.scratch_get(_KEY)
    assert kept is not None, "the apply deleted a preview it never read"
    assert kept["value"] == newer["value"]


async def test_success_keeps_a_rescan_of_the_same_groups(components, memory_dir, monkeypatch):
    """The issue's own case: a rescan mid-apply finds the very same groups."""
    from memtomem.server.tools.consolidation import mem_consolidate_apply

    ctx, _, read = await _scanned(components, memory_dir)
    newer: dict = {}

    async def rescan():
        newer.update(await _rescan(components, ctx))

    _publish_then(monkeypatch, rescan)
    out = await mem_consolidate_apply(group_id=0, summary="the summary", ctx=ctx)

    assert out.startswith("Consolidation applied for group 0."), out
    # Same groups, so only the preview id tells the two scans apart.
    assert _groups(newer) == _groups(read)
    await _assert_newer_survives(components, read, newer)


async def test_success_without_a_rescan_consumes_the_preview(components, memory_dir):
    from memtomem.server.tools.consolidation import mem_consolidate_apply

    ctx, _, _ = await _scanned(components, memory_dir)

    out = await mem_consolidate_apply(group_id=0, summary="the summary", ctx=ctx)

    assert out.startswith("Consolidation applied for group 0."), out
    assert await components.storage.scratch_get(_KEY) is None


async def test_unlinked_keeps_a_rescan(components, memory_dir, monkeypatch):
    from memtomem.server.tools import memory_crud
    from memtomem.server.tools.consolidation import mem_consolidate_apply

    ctx, _, read = await _scanned(components, memory_dir)
    newer: dict = {}

    async def landed_without_new_ids(*args, **kwargs):
        newer.update(await _rescan(components, ctx))
        return "Memory added", IndexingStats(0, 1, 0, 1, 0, 0.0)

    monkeypatch.setattr(memory_crud, "_mem_add_core", landed_without_new_ids)
    out = await mem_consolidate_apply(group_id=0, summary="the summary", ctx=ctx)

    assert "(unlinked)" in out
    await _assert_newer_survives(components, read, newer)


async def test_source_unavailable_keeps_a_rescan(components, memory_dir, monkeypatch):
    from memtomem.server.tools.consolidation import mem_consolidate_apply

    ctx, path, read = await _scanned(components, memory_dir)
    newer: dict = {}

    async def rescan_then_hold():
        # Rescan first: a scan skips a hidden source.
        newer.update(await _rescan(components, ctx))
        await components.storage.hold_source(path, "missing")

    _publish_then(monkeypatch, rescan_then_hold)
    out = await mem_consolidate_apply(group_id=0, summary="the summary", ctx=ctx)

    assert "became unavailable after the summary was written" in out
    await _assert_newer_survives(components, read, newer)


async def test_appended_refusal_keeps_a_rescan_and_says_so(components, memory_dir, monkeypatch):
    from memtomem.indexing.engine import PrivacyRejection
    from memtomem.server.tools import memory_crud
    from memtomem.server.tools.consolidation import mem_consolidate_apply

    ctx, _, read = await _scanned(components, memory_dir)
    newer: dict = {}

    async def appended_then_refused(*args, **kwargs):
        newer.update(await _rescan(components, ctx))
        exc = PrivacyRejection(path=Path("day.md"), hit_count=1, scope="user", decision="blocked")
        exc.mutation_outcome = "appended"
        raise exc

    monkeypatch.setattr(memory_crud, "_mem_add_core", appended_then_refused)
    out = await mem_consolidate_apply(group_id=0, summary="the summary", ctx=ctx)

    assert "was discarded" not in out
    assert "replaced or removed meanwhile" in out
    assert "do not apply it" in out
    await _assert_newer_survives(components, read, newer)


async def test_expiry_keeps_a_preview_written_after_the_read(components, monkeypatch):
    from memtomem.server.tools.consolidation import mem_consolidate_apply
    from tests.test_tools_logic import _fake_ctx

    storage = components.storage
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    await storage.scratch_set(
        _KEY, json.dumps({"preview_id": "old", "groups": []}), expires_at=past
    )
    newer_value = json.dumps({"preview_id": "new", "groups": []})
    real_get = storage.scratch_get

    async def read_then_replaced(key):
        entry = await real_get(key)
        await storage.scratch_set(_KEY, newer_value)
        return entry

    monkeypatch.setattr(storage, "scratch_get", read_then_replaced)
    out = await mem_consolidate_apply(group_id=0, summary="x", ctx=_fake_ctx(components))
    monkeypatch.undo()

    assert "stale" in out
    assert (await storage.scratch_get(_KEY))["value"] == newer_value


async def test_expiry_drops_the_stale_preview_it_read(components):
    from memtomem.server.tools.consolidation import mem_consolidate_apply
    from tests.test_tools_logic import _fake_ctx

    storage = components.storage
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    await storage.scratch_set(_KEY, json.dumps([]), expires_at=past)

    out = await mem_consolidate_apply(group_id=0, summary="x", ctx=_fake_ctx(components))

    assert "stale" in out
    assert await storage.scratch_get(_KEY) is None


async def test_every_scan_writes_a_distinct_preview(components, memory_dir):
    ctx, _, first = await _scanned(components, memory_dir)
    second = await _rescan(components, ctx)

    assert _groups(first) == _groups(second)
    assert first["value"] != second["value"]
