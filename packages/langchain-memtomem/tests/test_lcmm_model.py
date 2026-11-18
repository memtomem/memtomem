"""Random operation sequences checked against a model of what the store holds.

The provenance unit (which rows may answer a query) was broken four times by
sequences a reviewer had to think of: a re-created key, a cached hit from
another instance, a heading-only edit, a repair interrupted between its index
and its marker. This test generates the sequences instead.

Two store instances share one root, each with its own result cache. Each step
is one of: a put (new text, a heading-only edit, or ``index=False``), a
delete, either of them interrupted at one of its step boundaries and followed
by a restart of that instance, or the loss of ``index.db`` with both
restarted, sometimes after a first reopen that dies between a repair's index
and its marker. After every step:

- **no stale answer**: every item a query search returns, from either
  instance, holds the model's current value, and that value contains the
  queried word (a hit ranked by text the item no longer holds is a stale
  score);
- **get is the record**: ``aget`` from either instance returns the model's
  value.

Every few steps, and at the end, a freshly opened third instance (repair on
open, empty cache) must also be **complete**: each indexed item is found by
each of its words, words no current value holds find nothing, and the
query-less listing is exactly the model's keys.

Seeds are fixed, so a failure names a reproducible seed.
``LANGCHAIN_MEMTOMEM_MODEL_SEEDS`` runs more of them.
"""

from __future__ import annotations

import os
import random
import re
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from langchain_memtomem import MemtomemPipelineStore

PREFIX = ("u",)
NAMESPACES = [("u", "a"), ("u", "b")]
KEYS = ["k0", "k1", "k2"]
STEPS = 40
SETTLE_EVERY = 8
SEEDS = int(os.environ.get("LANGCHAIN_MEMTOMEM_MODEL_SEEDS", "20"))

PUT_BOUNDARIES = [
    "put.before_record",
    "put.after_record",
    "put.after_projection",
    "put.after_index",
    "put.after_marker",
]
PUT_OFF_BOUNDARIES = ["put.before_record", "put.after_record", "put.between_unlinks"]
DELETE_BOUNDARIES = [
    "delete.after_marker",
    "delete.after_projection",
    "delete.after_rows",
    "delete.after_record",
]
_WORD = re.compile(r"\w+")


class Crash(Exception):
    pass


@dataclass
class Entry:
    heading: str | None
    body: str
    indexed: bool

    @property
    def text(self) -> str:
        return (f"# {self.heading}\n\n" if self.heading else "") + f"{self.body} filler"

    @property
    def words(self) -> set[str]:
        return set(_WORD.findall(self.text)) - {"filler"}


@dataclass
class World:
    root: Path
    rng: random.Random
    stores: list[MemtomemPipelineStore] = field(default_factory=list)
    model: dict[tuple[tuple[str, ...], str], Entry] = field(default_factory=dict)
    ever: set[str] = field(default_factory=set)
    counter: int = 0
    log: list[str] = field(default_factory=list)

    def word(self, kind: str) -> str:
        self.counter += 1
        token = f"{kind}q{self.counter}x"
        self.ever.add(token)
        return token

    async def open(self, index: int) -> None:
        self.stores[index] = await MemtomemPipelineStore.open(self.root)

    async def restart(self, index: int) -> None:
        await self.stores[index].aclose()
        await self.open(index)


def _crash_at(store: MemtomemPipelineStore, boundary: str) -> None:
    async def at(name: str) -> None:
        if name == boundary:
            raise Crash(name)

    store._steps.at = at  # type: ignore[method-assign]


async def _step(world: World) -> None:
    rng = world.rng
    who = rng.randrange(2)
    store = world.stores[who]
    ident = (rng.choice(NAMESPACES), rng.choice(KEYS))
    current = world.model.get(ident)
    roll = rng.random()

    if roll < 0.08:
        crash_repair = rng.random() < 0.5
        world.log.append(f"lose index.db, crash the first repair={crash_repair}")
        for s in world.stores:
            await s.aclose()
        for leftover in world.root.glob("index.db*"):
            leftover.unlink()
        if crash_repair:
            # The first reopen dies between a repair's index and its marker
            # (if anything needs indexing); the next one must recover.
            doomed = MemtomemPipelineStore(world.root)
            _crash_at(doomed, "repair.after_index")
            try:
                await doomed._open()
            except Crash:
                pass
            else:
                await doomed.aclose()
        for index in range(2):
            await world.open(index)
        return

    if roll < 0.22 and current is not None:
        boundary = rng.choice(DELETE_BOUNDARIES) if rng.random() < 0.4 else None
        world.log.append(f"#{who} delete {ident} crash={boundary}")
        if boundary is None:
            await store.adelete(*ident)
        else:
            _crash_at(store, boundary)
            with pytest.raises(Crash):
                await store.adelete(*ident)
            await world.restart(who)  # repair finishes the delete
        del world.model[ident]
        return

    if roll < 0.32:
        new = Entry(None, world.word("off"), indexed=False)
        boundaries = PUT_OFF_BOUNDARIES
    elif current is not None and current.indexed and roll < 0.55:
        new = Entry(world.word("head"), current.body, indexed=True)  # heading-only edit
        boundaries = PUT_BOUNDARIES
    else:
        heading = world.word("head") if rng.random() < 0.5 else None
        new = Entry(heading, world.word("body"), indexed=True)
        boundaries = PUT_BOUNDARIES
    boundary = rng.choice(boundaries) if rng.random() < 0.35 else None
    world.log.append(f"#{who} put {ident} {new.text!r} indexed={new.indexed} crash={boundary}")
    kwargs = {"index": ["text"] if new.indexed else False}
    if boundary is None:
        await store.aput(*ident, {"text": new.text}, **kwargs)
        world.model[ident] = new
        return
    _crash_at(store, boundary)
    with pytest.raises(Crash):
        await store.aput(*ident, {"text": new.text}, **kwargs)
    await world.restart(who)
    if boundary != "put.before_record":
        world.model[ident] = new  # the record was written; repair derives the rest


async def _check_no_stale_answer(world: World) -> None:
    words = sorted(world.ever)
    sample = world.rng.sample(words, min(6, len(words)))
    for index, store in enumerate(world.stores):
        for word in sample:
            for hit in await store.asearch(PREFIX, query=word, limit=20):
                ident = (hit.namespace, hit.key)
                entry = world.model.get(ident)
                where = f"instance #{index}, query {word!r}, hit {ident}"
                assert entry is not None, f"{where}: a deleted item was served"
                assert entry.indexed, f"{where}: an index=False item was served"
                assert hit.value == {"text": entry.text}, f"{where}: stale value {hit.value}"
                assert word in entry.words, f"{where}: stale score, value is {entry.text!r}"


async def _check_get(world: World) -> None:
    for store in world.stores:
        for namespace in NAMESPACES:
            for key in KEYS:
                item = await store.aget(namespace, key)
                entry = world.model.get((namespace, key))
                expected = None if entry is None else {"text": entry.text}
                assert (item.value if item else None) == expected, ((namespace, key), item)


async def _check_complete(world: World) -> None:
    fresh = await MemtomemPipelineStore.open(world.root)
    try:
        live: set[str] = set()
        for ident, entry in world.model.items():
            if not entry.indexed:
                continue
            for word in entry.words:
                live.add(word)
                found = {(h.namespace, h.key) for h in await fresh.asearch(PREFIX, query=word)}
                assert ident in found, f"{ident} not found by {word!r}"
        for word in world.ever - live:
            assert await fresh.asearch(PREFIX, query=word) == [], f"dead word {word!r} found"
        listed = {(i.namespace, i.key) for i in await fresh.asearch(PREFIX, limit=100)}
        assert listed == set(world.model), listed
    finally:
        await fresh.aclose()


@pytest.mark.parametrize("seed", range(SEEDS))
async def test_random_sequences_keep_the_store_consistent(tmp_path: Path, seed: int) -> None:
    world = World(root=tmp_path / "store", rng=random.Random(seed), stores=[None, None])  # type: ignore[list-item]
    for index in range(2):
        await world.open(index)
    try:
        for step in range(STEPS):
            await _step(world)
            try:
                await _check_no_stale_answer(world)
                await _check_get(world)
                if step % SETTLE_EVERY == SETTLE_EVERY - 1:
                    await _check_complete(world)
            except AssertionError as exc:
                raise AssertionError(
                    f"seed {seed}, step {step}: {exc}\n" + "\n".join(world.log)
                ) from None
        await _check_complete(world)
    finally:
        for store in world.stores:
            if store is not None:
                await store.aclose()
