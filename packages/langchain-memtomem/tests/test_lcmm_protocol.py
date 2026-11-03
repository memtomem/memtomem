"""The mutation protocol: every interruption boundary, repair on open, races.

Each row of the interruption matrix in ``docs/protocol.md`` is one case here.
A boundary is interrupted by replacing the store's ``Steps.at`` with one that
raises at that name; the lock is released by the context manager as the
exception propagates, exactly as a crash would release it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from langchain_memtomem import MemtomemPipelineStore
from langchain_memtomem._protocol import read_marker

NS = ("users", "alice")
KEY = "note"
A = {"text": "generation one is about apples", "n": 1}
B = {"text": "generation two is about bananas", "n": 2}


class Interrupted(Exception):
    pass


def interrupt_at(store: MemtomemPipelineStore, boundary: str) -> None:
    async def at(name: str) -> None:
        if name == boundary:
            raise Interrupted(name)

    store._steps.at = at  # type: ignore[method-assign]


async def _keys(store: MemtomemPipelineStore, query: str) -> set[str]:
    return {item.key for item in await store.asearch(NS, query=query, limit=10)}


async def _state(store: MemtomemPipelineStore):
    paths = store._paths(NS, KEY)
    state, _ = await store._read_state(NS, KEY, paths)
    return state


def _item_files(store: MemtomemPipelineStore) -> list[str]:
    paths = store._paths(NS, KEY)
    return [
        name
        for name, path in (
            ("record", paths.record),
            ("deleting", paths.deleting),
            ("projection", paths.projection),
            ("indexed", paths.indexed),
        )
        if path.exists()
    ]


PUT_BOUNDARIES = [
    "put.before_record",
    "put.after_record",
    "put.after_projection",
    "put.after_index",
    "put.after_marker",
]


@pytest.mark.parametrize("boundary", PUT_BOUNDARIES)
async def test_put_interrupted_then_repaired_on_open(root: Path, boundary: str) -> None:
    store = await MemtomemPipelineStore.open(root)
    await store.aput(NS, KEY, A)
    interrupt_at(store, boundary)
    with pytest.raises(Interrupted):
        await store.aput(NS, KEY, B)
    await store.aclose()

    reopened = await MemtomemPipelineStore.open(root)
    try:
        record_is_b = boundary != "put.before_record"
        expected = B if record_is_b else A
        assert (await reopened.aget(NS, KEY)).value == expected  # type: ignore[union-attr]
        assert await _keys(reopened, "bananas") == ({KEY} if record_is_b else set())
        assert await _keys(reopened, "apples") == (set() if record_is_b else {KEY})
        state = await _state(reopened)
        assert state.agrees, state
        assert state.record_gen == (2 if record_is_b else 1)
    finally:
        await reopened.aclose()


@pytest.mark.parametrize("boundary", ["put.after_record", "put.after_projection"])
async def test_put_index_false_interrupted_then_repaired(root: Path, boundary: str) -> None:
    store = await MemtomemPipelineStore.open(root)
    await store.aput(NS, KEY, A)
    interrupt_at(store, boundary)
    with pytest.raises(Interrupted):
        await store.aput(NS, KEY, B, index=False)
    await store.aclose()

    reopened = await MemtomemPipelineStore.open(root)
    try:
        assert (await reopened.aget(NS, KEY)).value == B  # type: ignore[union-attr]
        assert await _keys(reopened, "apples") == set()
        assert await _keys(reopened, "bananas") == set()
        assert _item_files(reopened) == ["record"]
    finally:
        await reopened.aclose()


DELETE_BOUNDARIES = [
    "delete.after_marker",
    "delete.after_projection",
    "delete.after_rows",
    "delete.after_record",
]


@pytest.mark.parametrize("boundary", DELETE_BOUNDARIES)
async def test_delete_interrupted_then_finished_on_open(root: Path, boundary: str) -> None:
    store = await MemtomemPipelineStore.open(root)
    await store.aput(NS, KEY, A)
    interrupt_at(store, boundary)
    with pytest.raises(Interrupted):
        await store.adelete(NS, KEY)
    await store.aclose()

    reopened = await MemtomemPipelineStore.open(root)
    try:
        assert await reopened.aget(NS, KEY) is None
        assert await _keys(reopened, "apples") == set()
        assert _item_files(reopened) == []
        assert await reopened._comp.storage.get_all_source_files() == set()  # type: ignore[union-attr]
    finally:
        await reopened.aclose()


async def test_delete_marker_is_superseded_by_a_later_put(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root)
    await store.aput(NS, KEY, A)
    interrupt_at(store, "delete.after_projection")
    with pytest.raises(Interrupted):
        await store.adelete(NS, KEY)
    store._steps.at = MemtomemPipelineStore._steps_cls().at if False else _noop  # type: ignore[attr-defined]
    await store.aput(NS, KEY, B)
    assert read_marker(store._paths(NS, KEY).deleting) is None
    assert await _keys(store, "bananas") == {KEY}
    await store.aclose()
    reopened = await MemtomemPipelineStore.open(root)
    try:
        assert (await reopened.aget(NS, KEY)).value == B  # type: ignore[union-attr]
        assert (await _state(reopened)).agrees
    finally:
        await reopened.aclose()


async def _noop(name: str) -> None:
    return None


async def test_stale_rows_are_not_served_by_another_instance(root: Path) -> None:
    """ADR-0042 B4: generation 2's record and projection, generation 1's rows."""
    writer = await MemtomemPipelineStore.open(root)
    reader = await MemtomemPipelineStore.open(root)
    try:
        await writer.aput(NS, KEY, A)
        assert await _keys(reader, "apples") == {KEY}
        interrupt_at(writer, "put.after_projection")
        with pytest.raises(Interrupted):
            await writer.aput(NS, KEY, B)
        # The reader's rows (and its result cache) still say apples. It must
        # not serve the item for apples, and must serve it for bananas.
        assert await _keys(reader, "apples") == set()
        assert await _keys(reader, "bananas") == {KEY}
        [item] = await reader.asearch(NS, query="bananas", limit=10)
        assert item.value == B
        assert (await _state(reader)).agrees
        # The writer's own cache held the apples hit too.
        assert await _keys(writer, "apples") == set()
    finally:
        await writer.aclose()
        await reader.aclose()


async def test_disagreeing_item_under_a_held_lock_is_skipped(root: Path) -> None:
    writer = await MemtomemPipelineStore.open(root)
    reader = await MemtomemPipelineStore.open(root)
    try:
        await writer.aput(NS, KEY, A)
        interrupt_at(writer, "put.after_projection")
        with pytest.raises(Interrupted):
            await writer.aput(NS, KEY, B)
        paths = reader._paths(NS, KEY)
        async with writer._item_lock(paths, 5.0):
            # Someone holds the item: the reader does without it this call.
            assert await _keys(reader, "apples") == set()
            assert await _keys(reader, "bananas") == set()
            assert not (await _state(reader)).agrees
        # Lock free again. Generation 2's text is not in any row yet, so a
        # query for it finds nothing to repair; a query that hits the stale
        # rows repairs the item, and the next query sees generation 2.
        assert await _keys(reader, "bananas") == set()
        assert await _keys(reader, "apples") == set()
        assert (await _state(reader)).agrees
        assert await _keys(reader, "bananas") == {KEY}
    finally:
        await writer.aclose()
        await reader.aclose()


async def test_two_instances_delete_races_put(root: Path) -> None:
    one = await MemtomemPipelineStore.open(root)
    two = await MemtomemPipelineStore.open(root)
    try:
        await one.aput(NS, KEY, A)
        await asyncio.gather(one.aput(NS, KEY, B), two.adelete(NS, KEY))
        for store in (one, two):
            item = await store.aget(NS, KEY)
            if item is None:
                assert _item_files(store) == []
                assert await _keys(store, "bananas") == set()
            else:
                assert item.value == B
                assert await _keys(store, "bananas") == {KEY}
                assert (await _state(store)).agrees
    finally:
        await one.aclose()
        await two.aclose()
    reopened = await MemtomemPipelineStore.open(root)
    try:
        item = await reopened.aget(NS, KEY)
        assert item is None or item.value == B
    finally:
        await reopened.aclose()


async def test_repair_is_idempotent_and_rebuilds_a_lost_index(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root)
    await store.aput(NS, KEY, A)
    await store.aput(("users", "bob"), "b", {"text": "bob and apples"})
    await store.aclose()
    for leftover in root.glob("index.db*"):
        leftover.unlink()
    for _ in range(2):
        reopened = await MemtomemPipelineStore.open(root)
        try:
            assert await _keys(reopened, "apples") == {KEY}
            assert (await _state(reopened)).agrees
        finally:
            await reopened.aclose()


async def test_stray_projection_and_orphan_rows_are_removed_on_open(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root)
    await store.aput(NS, KEY, A)
    paths = store._paths(NS, KEY)
    stray = store.projections_dir / "not-ours.md"
    stray.write_text("# stray\n\napples everywhere\n")
    await store._comp.index_engine.index_file(stray, namespace="sYQ")  # type: ignore[union-attr]
    # a projection whose record is gone
    paths.record.unlink()
    await store.aclose()
    reopened = await MemtomemPipelineStore.open(root)
    try:
        assert not stray.exists()
        assert not paths.projection.exists()
        assert await reopened._comp.storage.get_all_source_files() == set()  # type: ignore[union-attr]
        assert await _keys(reopened, "apples") == set()
    finally:
        await reopened.aclose()


# --- round 1 of the implementation review ------------------------------------


async def test_generation_continues_past_a_delete(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root)
    try:
        await store.aput(NS, KEY, A)
        await store.adelete(NS, KEY)
        assert read_marker(store._paths(NS, KEY).last) == 1
        await store.aput(NS, KEY, B)
        state = await _state(store)
        assert state.agrees and state.record_gen == 2
    finally:
        await store.aclose()


async def test_interrupted_delete_then_interrupted_put_keeps_the_new_record(root: Path) -> None:
    """Row 2 of the repair table: the new record outranks the old marker."""
    store = await MemtomemPipelineStore.open(root)
    await store.aput(NS, KEY, A)
    interrupt_at(store, "delete.after_record")
    with pytest.raises(Interrupted):
        await store.adelete(NS, KEY)
    interrupt_at(store, "put.after_record")
    with pytest.raises(Interrupted):
        await store.aput(NS, KEY, B)
    assert read_marker(store._paths(NS, KEY).deleting) == 1
    await store.aclose()
    reopened = await MemtomemPipelineStore.open(root)
    try:
        assert (await reopened.aget(NS, KEY)).value == B  # type: ignore[union-attr]
        assert await _keys(reopened, "bananas") == {KEY}
        state = await _state(reopened)
        assert state.agrees and state.record_gen == 2 and state.deleting_gen is None
    finally:
        await reopened.aclose()


async def test_recreated_key_is_not_served_from_a_hit_cached_before_the_delete(
    root: Path,
) -> None:
    writer = await MemtomemPipelineStore.open(root)
    reader = await MemtomemPipelineStore.open(root)
    try:
        await writer.aput(NS, KEY, A)
        assert await _keys(reader, "apples") == {KEY}  # cached in the reader
        await writer.adelete(NS, KEY)
        await writer.aput(NS, KEY, B)
        assert await _keys(reader, "apples") == set()
        [item] = await reader.asearch(NS, query="bananas", limit=10)
        assert item.value == B
    finally:
        await writer.aclose()
        await reader.aclose()


async def test_first_put_interrupted_after_its_record(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root)
    interrupt_at(store, "put.after_record")
    with pytest.raises(Interrupted):
        await store.aput(NS, KEY, A)
    assert _item_files(store) == ["record"]
    await store.aclose()
    reopened = await MemtomemPipelineStore.open(root)
    try:
        assert await _keys(reopened, "apples") == {KEY}
        assert (await _state(reopened)).agrees
    finally:
        await reopened.aclose()


async def test_put_index_false_interrupted_between_the_unlinks(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root)
    await store.aput(NS, KEY, A)
    interrupt_at(store, "put.between_unlinks")
    with pytest.raises(Interrupted):
        await store.aput(NS, KEY, B, index=False)
    assert _item_files(store) == ["record", "indexed"]
    await store.aclose()
    reopened = await MemtomemPipelineStore.open(root)
    try:
        assert (await reopened.aget(NS, KEY)).value == B  # type: ignore[union-attr]
        assert _item_files(reopened) == ["record"]
        assert await reopened._comp.storage.get_all_source_files() == set()  # type: ignore[union-attr]
    finally:
        await reopened.aclose()


async def test_open_time_orphan_sweep_does_not_race_a_put(root: Path) -> None:
    first = await MemtomemPipelineStore.open(root)
    await first.aput(NS, KEY, A)
    other = ("users", "bob")
    second = MemtomemPipelineStore(root)

    async def at(name: str) -> None:
        # Between the second store's file snapshot and its row listing, the
        # first store indexes a new item: its rows must survive the sweep.
        if name == "repair.after_snapshot":
            await first.aput(other, "b", {"text": "bob and apples"})

    second._steps.at = at  # type: ignore[method-assign]
    try:
        await second._open()
        assert {i.key for i in await second.asearch(other, query="apples", limit=10)} == {"b"}
        assert {i.key for i in await first.asearch(other, query="apples", limit=10)} == {"b"}
    finally:
        await first.aclose()
        await second.aclose()


async def test_nfd_spelled_root_searches_and_reopens(tmp_path: Path) -> None:
    import unicodedata

    root = tmp_path / unicodedata.normalize("NFD", "한글 메모리") / "app"
    store = await MemtomemPipelineStore.open(root)
    try:
        await store.aput(NS, KEY, A)
        assert await _keys(store, "apples") == {KEY}
    finally:
        await store.aclose()
    reopened = await MemtomemPipelineStore.open(root)
    try:
        assert await _keys(reopened, "apples") == {KEY}
        assert (await _state(reopened)).agrees
    finally:
        await reopened.aclose()


# --- round 2 of the implementation review ------------------------------------


async def test_cancelled_delete_cannot_remove_a_record_written_after_it(root: Path) -> None:
    """A task cancelled mid-delete releases the lock; nothing of it may run late."""
    one = await MemtomemPipelineStore.open(root)
    two = await MemtomemPipelineStore.open(root)
    try:
        await one.aput(NS, KEY, A)
        comp = one._comp
        assert comp is not None
        entered = asyncio.Event()
        release = asyncio.Event()
        real_delete = comp.storage.delete_by_source

        async def slow_delete(path, **kwargs):
            entered.set()
            await release.wait()
            return await real_delete(path, **kwargs)

        comp.storage.delete_by_source = slow_delete  # type: ignore[method-assign]
        task = asyncio.create_task(one.adelete(NS, KEY))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await two.aput(NS, KEY, B)
        await asyncio.sleep(0.05)
        assert (await two.aget(NS, KEY)).value == B  # type: ignore[union-attr]
        assert (await one.aget(NS, KEY)).value == B  # type: ignore[union-attr]
        assert await _keys(two, "bananas") == {KEY}
        assert (await _state(two)).agrees
    finally:
        await one.aclose()
        await two.aclose()


async def test_repeated_delete_keeps_a_legacy_marker_generation(root: Path) -> None:
    """No record, `.deleting`=1 and no `.last` (a pre-tombstone interruption)."""
    store = await MemtomemPipelineStore.open(root)
    try:
        await store.aput(NS, KEY, A)
        paths = store._paths(NS, KEY)
        paths.record.unlink()
        paths.last.unlink(missing_ok=True)
        paths.projection.unlink()
        paths.indexed.unlink()
        paths.deleting.write_text("1\n")
        await store.adelete(NS, KEY)
        assert read_marker(paths.last) == 1
        await store.aput(NS, KEY, B)
        state = await _state(store)
        assert state.agrees and state.record_gen == 2
    finally:
        await store.aclose()


# --- fresh-eyes review round -------------------------------------------------


async def test_an_undecodable_record_does_not_take_the_store_down(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root)
    await store.aput(NS, "good", A)
    await store.aput(NS, KEY, B)
    broken = store._paths(NS, KEY).record
    await store.aclose()
    broken.write_bytes(b"\xff")
    reopened = await MemtomemPipelineStore.open(root)
    try:
        assert (await reopened.aget(NS, "good")).value == A  # type: ignore[union-attr]
        assert {i.key for i in await reopened.asearch(NS)} == {"good"}
        assert await _keys(reopened, "bananas") == set()
        await reopened.adelete(NS, KEY)
        assert not broken.exists()
    finally:
        await reopened.aclose()


async def test_the_longest_accepted_key_can_be_written_and_deleted(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root)
    try:
        longest = "x" * 124
        await store.aput(NS, longest, A)
        assert await _keys(store, "apples") == {longest}
        await store.adelete(NS, longest)
        assert await store.aget(NS, longest) is None
        with pytest.raises(ValueError, match="at most"):
            await store.aput(NS, longest + "x", A)
        assert not any(store.records_dir.rglob("*.json"))
    finally:
        await store.aclose()


async def test_an_explicit_ttl_is_refused_as_the_readme_says(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root)
    try:
        with pytest.raises(NotImplementedError):
            await store.aput(NS, KEY, A, ttl=5)
    finally:
        await store.aclose()
