"""Storage contract of the Hermes memory mirror: a last-writer-wins element set.

Every operation the Hermes provider sends is a set of row writes keyed by the
sha256 of an entry's text. The guarantees pinned here are the ones the
provider's retries and cross-process delivery rely on: a write lands only when
newer, a live row beats a tombstone on a tie, any delivery order of the same
writes converges, a purge fences out older writes, and a render's
acknowledgement cannot clear a newer revision's pending flag.
"""

from __future__ import annotations

import itertools

import pytest

from memtomem.storage.mixins.hermes_mirror import MirrorWrite

OWNER = "a" * 32
TARGET = "memory"


def add(key: str, ts: int, content: str | None = None) -> MirrorWrite:
    return MirrorWrite(key, content if content is not None else f"text-{key}", ts, False)


def tomb(key: str, ts: int) -> MirrorWrite:
    return MirrorWrite(key, "ignored", ts, True)


async def live(storage, owner: str = OWNER, target: str = TARGET) -> list[tuple[str, str, int]]:
    _, entries = await storage.hermes_mirror_snapshot(owner, target)
    return [(e.entry_key, e.content, e.ts) for e in entries]


@pytest.fixture
async def storage(bm25_only_components):
    comp, _ = bm25_only_components
    return comp.storage


async def test_an_add_creates_a_live_row_and_moves_the_revision(storage):
    out = await storage.hermes_mirror_apply(OWNER, TARGET, [add("k1", 10)])
    assert out == {"fenced": False, "changed": True, "previous_matched": None, "revision": 1}
    assert await live(storage) == [("k1", "text-k1", 10)]
    state = await storage.hermes_mirror_target_state(OWNER, TARGET)
    assert state == {
        "revision": 1,
        "rendered_revision": 0,
        "render_pending": True,
        "purged_at": None,
    }


async def test_a_replayed_write_changes_nothing(storage):
    await storage.hermes_mirror_apply(OWNER, TARGET, [add("k1", 10)])
    out = await storage.hermes_mirror_apply(OWNER, TARGET, [add("k1", 10)])
    assert out["changed"] is False
    assert out["revision"] == 1


async def test_an_older_write_loses(storage):
    await storage.hermes_mirror_apply(OWNER, TARGET, [tomb("k1", 20)])
    out = await storage.hermes_mirror_apply(OWNER, TARGET, [add("k1", 10)])
    assert out["changed"] is False
    assert await live(storage) == []


async def test_a_live_row_wins_a_tie_from_either_side(storage):
    await storage.hermes_mirror_apply(OWNER, TARGET, [tomb("k1", 10)])
    await storage.hermes_mirror_apply(OWNER, TARGET, [add("k1", 10)])
    assert [k for k, _, _ in await live(storage)] == ["k1"]
    await storage.hermes_mirror_apply(OWNER, TARGET, [tomb("k1", 10)])
    assert [k for k, _, _ in await live(storage)] == ["k1"]


async def test_a_tombstone_keeps_no_text(storage):
    await storage.hermes_mirror_apply(OWNER, TARGET, [add("k1", 10, "secret-ish entry")])
    await storage.hermes_mirror_apply(OWNER, TARGET, [tomb("k1", 11)])
    db = storage._get_db()
    row = db.execute(
        "SELECT content, tombstone FROM hermes_mirror_entries WHERE entry_key = 'k1'"
    ).fetchone()
    assert tuple(row) == ("", 1)


async def test_previous_matched_reports_whether_the_key_was_live(storage):
    await storage.hermes_mirror_apply(OWNER, TARGET, [add("k1", 10)])
    hit = await storage.hermes_mirror_apply(OWNER, TARGET, [tomb("k1", 11)], previous_key="k1")
    miss = await storage.hermes_mirror_apply(OWNER, TARGET, [tomb("k9", 12)], previous_key="k9")
    assert hit["previous_matched"] is True
    assert miss["previous_matched"] is False


async def test_every_delivery_order_of_the_same_operations_converges(storage):
    # add A, replace A -> B, remove B, add C, each with its own ts: one native history.
    ops = [
        [add("A", 1)],
        [tomb("A", 2), add("B", 2)],
        [tomb("B", 3)],
        [add("C", 4)],
    ]
    results = set()
    for i, order in enumerate(itertools.permutations(ops)):
        owner = f"{i:032x}"
        for op in order:
            await storage.hermes_mirror_apply(owner, TARGET, list(op))
        results.add(tuple(await live(storage, owner)))
    assert results == {(("C", "text-C", 4),)}


async def test_owners_and_targets_do_not_share_rows(storage):
    await storage.hermes_mirror_apply(OWNER, "memory", [add("k1", 10)])
    await storage.hermes_mirror_apply(OWNER, "user", [add("k2", 10)])
    await storage.hermes_mirror_apply("b" * 32, "memory", [tomb("k1", 99)])
    assert [k for k, _, _ in await live(storage, OWNER, "memory")] == ["k1"]
    assert [k for k, _, _ in await live(storage, OWNER, "user")] == ["k2"]


async def test_render_order_is_total_on_equal_timestamps(storage):
    await storage.hermes_mirror_apply(OWNER, TARGET, [add("kb", 5), add("ka", 5), add("kc", 1)])
    assert [k for k, _, _ in await live(storage)] == ["kc", "ka", "kb"]


async def test_purge_deletes_rows_and_fences_older_writes(storage):
    await storage.hermes_mirror_apply(OWNER, TARGET, [add("k1", 10), add("k2", 11)])
    out = await storage.hermes_mirror_purge(OWNER, TARGET, 50)
    assert out["fenced"] is False and out["changed"] is True
    assert await live(storage) == []
    late = await storage.hermes_mirror_apply(OWNER, TARGET, [add("k1", 10)])
    assert late["fenced"] is True
    assert await live(storage) == []
    newer = await storage.hermes_mirror_apply(OWNER, TARGET, [add("k3", 51)])
    assert newer["fenced"] is False
    assert [k for k, _, _ in await live(storage)] == ["k3"]


async def test_an_older_purge_is_fenced(storage):
    await storage.hermes_mirror_purge(OWNER, TARGET, 50)
    await storage.hermes_mirror_apply(OWNER, TARGET, [add("k1", 60)])
    out = await storage.hermes_mirror_purge(OWNER, TARGET, 40)
    assert out["fenced"] is True
    assert [k for k, _, _ in await live(storage)] == ["k1"]


async def test_mark_rendered_clears_only_the_revision_it_rendered(storage):
    await storage.hermes_mirror_apply(OWNER, TARGET, [add("k1", 10)])
    rendered_revision, _ = await storage.hermes_mirror_snapshot(OWNER, TARGET)
    # A second operation commits while that render runs.
    await storage.hermes_mirror_apply(OWNER, TARGET, [add("k2", 11)])
    assert await storage.hermes_mirror_mark_rendered(OWNER, TARGET, rendered_revision) is False
    state = await storage.hermes_mirror_target_state(OWNER, TARGET)
    assert state["render_pending"] is True and state["rendered_revision"] == 0
    current, _ = await storage.hermes_mirror_snapshot(OWNER, TARGET)
    assert await storage.hermes_mirror_mark_rendered(OWNER, TARGET, current) is True
    state = await storage.hermes_mirror_target_state(OWNER, TARGET)
    assert state["render_pending"] is False and state["rendered_revision"] == current


async def test_writes_compose_inside_an_owner_transaction(storage):
    with pytest.raises(RuntimeError):
        async with storage.transaction():
            await storage.hermes_mirror_apply(OWNER, TARGET, [add("k1", 10)])
            raise RuntimeError("owner fails after the write")
    assert await live(storage) == []
    assert await storage.hermes_mirror_target_state(OWNER, TARGET) is None


async def test_snapshot_of_an_unknown_target_is_empty(storage):
    assert await storage.hermes_mirror_snapshot(OWNER, "user") == (0, [])


async def test_a_purge_keeps_rows_newer_than_itself_in_either_order(storage):
    await storage.hermes_mirror_apply(OWNER, TARGET, [add("A", 60)])
    await storage.hermes_mirror_purge(OWNER, TARGET, 50)
    first = await live(storage)
    other = "c" * 32
    await storage.hermes_mirror_purge(other, TARGET, 50)
    await storage.hermes_mirror_apply(other, TARGET, [add("A", 60)])
    second = await live(storage, other)
    assert first == second == [("A", "text-A", 60)]


async def test_a_purge_keeps_a_newer_tombstone_so_an_older_add_stays_dead(storage):
    await storage.hermes_mirror_apply(OWNER, TARGET, [tomb("A", 60)])
    await storage.hermes_mirror_purge(OWNER, TARGET, 50)
    out = await storage.hermes_mirror_apply(OWNER, TARGET, [add("A", 55)])
    assert out["fenced"] is False and out["changed"] is False
    assert await live(storage) == []


async def test_every_order_of_writes_and_a_purge_converges(storage):
    ops = [
        ("apply", [add("A", 10)]),
        ("apply", [add("B", 70)]),
        ("purge", 50),
        ("apply", [tomb("B", 80)]),
    ]
    results = set()
    for i, order in enumerate(itertools.permutations(ops)):
        owner = f"{i + 1000:032x}"
        for kind, arg in order:
            if kind == "purge":
                await storage.hermes_mirror_purge(owner, TARGET, arg)
            else:
                await storage.hermes_mirror_apply(owner, TARGET, list(arg))
        results.add(tuple(await live(storage, owner)))
    assert results == {()}
