"""Tests for the tag-management service.

Covers the service-layer contracts on top of the storage helpers:
- dry_run returns count + sample without writing
- apply path mutates and triggers cache invalidation
- input validation rejects empty names
- merge edge cases (empty sources, target-only sources, target in sources)
"""

from __future__ import annotations

import pytest

from helpers import make_chunk as _make_chunk
from memtomem.services import tag_management as svc


class _SearchPipelineSpy:
    """Minimal stand-in for ``SearchPipeline`` that just counts cache flushes."""

    def __init__(self) -> None:
        self.invalidate_count = 0

    def invalidate_cache(self) -> None:
        self.invalidate_count += 1


# ---------------------------------------------------------------------------
# rename
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_dry_run_returns_count_and_samples_without_writing(storage):
    c1 = _make_chunk(content="alpha", tags=("old_tag",))
    c2 = _make_chunk(content="beta", tags=("old_tag", "extra"))
    await storage.upsert_chunks([c1, c2])

    spy = _SearchPipelineSpy()
    result = await svc.rename_tag(storage, "old_tag", "new_tag", dry_run=True, search_pipeline=spy)

    assert result.dry_run is True
    assert result.affected_chunks == 2
    assert result.tag == "new_tag"
    assert {s.chunk_id for s in result.samples} == {c1.id, c2.id}
    # Storage untouched
    counts = dict(await storage.get_tag_counts())
    assert counts.get("old_tag") == 2
    assert "new_tag" not in counts
    # No cache invalidation on dry-run
    assert spy.invalidate_count == 0


@pytest.mark.asyncio
async def test_rename_apply_mutates_and_invalidates_cache(storage):
    c1 = _make_chunk(content="alpha", tags=("old_tag",))
    await storage.upsert_chunks([c1])

    spy = _SearchPipelineSpy()
    result = await svc.rename_tag(storage, "old_tag", "new_tag", dry_run=False, search_pipeline=spy)

    assert result.dry_run is False
    assert result.affected_chunks == 1
    assert result.samples == ()  # apply does not return samples
    counts = dict(await storage.get_tag_counts())
    assert counts.get("new_tag") == 1
    assert "old_tag" not in counts
    assert spy.invalidate_count == 1


@pytest.mark.asyncio
async def test_rename_apply_no_match_skips_invalidate(storage):
    """Cache invalidation only fires when something actually changed."""
    spy = _SearchPipelineSpy()
    result = await svc.rename_tag(
        storage, "absent", "still_absent", dry_run=False, search_pipeline=spy
    )
    assert result.affected_chunks == 0
    assert spy.invalidate_count == 0


@pytest.mark.asyncio
async def test_rename_rejects_empty_names(storage):
    with pytest.raises(ValueError):
        await svc.rename_tag(storage, "", "new")
    with pytest.raises(ValueError):
        await svc.rename_tag(storage, "old", "")


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_dry_run_returns_count_without_writing(storage):
    c1 = _make_chunk(content="alpha", tags=("doomed", "keep"))
    await storage.upsert_chunks([c1])

    spy = _SearchPipelineSpy()
    result = await svc.delete_tag(storage, "doomed", dry_run=True, search_pipeline=spy)

    assert result.dry_run is True
    assert result.affected_chunks == 1
    assert {s.chunk_id for s in result.samples} == {c1.id}
    counts = dict(await storage.get_tag_counts())
    assert counts.get("doomed") == 1  # not actually deleted
    assert spy.invalidate_count == 0


@pytest.mark.asyncio
async def test_delete_apply_mutates_and_invalidates(storage):
    c1 = _make_chunk(content="alpha", tags=("doomed", "keep"))
    await storage.upsert_chunks([c1])

    spy = _SearchPipelineSpy()
    result = await svc.delete_tag(storage, "doomed", dry_run=False, search_pipeline=spy)

    assert result.affected_chunks == 1
    counts = dict(await storage.get_tag_counts())
    assert "doomed" not in counts
    assert counts.get("keep") == 1
    assert spy.invalidate_count == 1


@pytest.mark.asyncio
async def test_delete_rejects_empty_name(storage):
    with pytest.raises(ValueError):
        await svc.delete_tag(storage, "")


# ---------------------------------------------------------------------------
# merge
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merge_dry_run_unions_candidate_chunks(storage):
    c1 = _make_chunk(content="a", tags=("py",))
    c2 = _make_chunk(content="b", tags=("python3",))
    c3 = _make_chunk(content="c", tags=("py", "python3"))  # in both
    c4 = _make_chunk(content="d", tags=("rust",))  # untouched
    await storage.upsert_chunks([c1, c2, c3, c4])

    spy = _SearchPipelineSpy()
    result = await svc.merge_tags(
        storage, ["py", "python3"], "python", dry_run=True, search_pipeline=spy
    )

    assert result.dry_run is True
    # Union dedupes c3, so 3 candidates total (c1, c2, c3)
    assert result.affected_chunks == 3
    sample_ids = {s.chunk_id for s in result.samples}
    assert sample_ids == {c1.id, c2.id, c3.id}
    # Storage untouched
    counts = dict(await storage.get_tag_counts())
    assert counts.get("py") == 2
    assert counts.get("python3") == 2
    assert "python" not in counts
    assert spy.invalidate_count == 0


@pytest.mark.asyncio
async def test_merge_apply_mutates_and_invalidates(storage):
    c1 = _make_chunk(content="a", tags=("py", "code"))
    c2 = _make_chunk(content="b", tags=("python3",))
    await storage.upsert_chunks([c1, c2])

    spy = _SearchPipelineSpy()
    result = await svc.merge_tags(
        storage, ["py", "python3"], "python", dry_run=False, search_pipeline=spy
    )

    assert result.affected_chunks == 2
    r1 = await storage.get_chunk(c1.id)
    r2 = await storage.get_chunk(c2.id)
    assert r1 is not None and set(r1.metadata.tags) == {"code", "python"}
    assert r2 is not None and set(r2.metadata.tags) == {"python"}
    assert spy.invalidate_count == 1


@pytest.mark.asyncio
async def test_merge_target_in_sources_does_not_count_target_only_chunks(storage):
    """Chunks that only have ``target`` are not affected even when target is
    listed in ``sources`` (target is filtered out of the source set)."""
    c1 = _make_chunk(content="a", tags=("py",))
    c2 = _make_chunk(content="b", tags=("python",))  # already at target
    await storage.upsert_chunks([c1, c2])

    result = await svc.merge_tags(storage, ["py", "python"], "python", dry_run=True)
    # Only c1 is a candidate; c2 holds only target, which is filtered out
    assert result.affected_chunks == 1
    assert {s.chunk_id for s in result.samples} == {c1.id}


@pytest.mark.asyncio
async def test_merge_empty_sources_returns_zero(storage):
    spy = _SearchPipelineSpy()
    result = await svc.merge_tags(storage, [], "python", search_pipeline=spy)
    assert result.affected_chunks == 0
    assert spy.invalidate_count == 0

    result_only_target = await svc.merge_tags(storage, ["python"], "python", search_pipeline=spy)
    assert result_only_target.affected_chunks == 0
    assert spy.invalidate_count == 0


@pytest.mark.asyncio
async def test_merge_rejects_empty_target(storage):
    with pytest.raises(ValueError):
        await svc.merge_tags(storage, ["foo"], "")


# ---------------------------------------------------------------------------
# lock acquisition
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_service_acquires_tag_write_lock(storage):
    """Lock is held during the read-modify-write window. Holding it from
    the outside blocks the service call until released — proves the
    service is actually using it (regression: a future refactor that
    drops ``async with storage._tag_write_lock:`` would surface here)."""
    import asyncio

    c1 = _make_chunk(content="a", tags=("old",))
    await storage.upsert_chunks([c1])

    held = asyncio.Event()
    can_release = asyncio.Event()

    async def hold_lock():
        async with storage._tag_write_lock:
            held.set()
            await can_release.wait()

    holder = asyncio.create_task(hold_lock())
    await held.wait()

    # Service call should be blocked while the lock is held externally.
    svc_task = asyncio.create_task(svc.rename_tag(storage, "old", "new"))
    try:
        await asyncio.wait_for(asyncio.shield(svc_task), timeout=0.05)
    except asyncio.TimeoutError:
        pass  # expected — the lock is held
    else:
        raise AssertionError("service did not acquire the lock; rename completed too early")

    can_release.set()
    await holder
    result = await svc_task
    assert result.affected_chunks == 1
