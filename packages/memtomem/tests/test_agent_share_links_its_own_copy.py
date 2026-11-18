"""``mem_agent_share`` links the source to the chunk its copy became (#3315).

The ``chunk_links`` ``shared`` row used to target ``new_chunk_ids[0]``. That
list covers the whole re-index of the target day file: a pending edit earlier
in the file comes first in it, and a copy whose chunk kept an existing id is
not in it at all. The share now reads the chunk the indexer attributes to its
append (``appended_chunk_ids``, #3292) and links only an ``exact`` attribution:
a copy merged into the section before it is not a copy of the source, and a
link on that chunk would block the neighbouring entry's next edit (#3324).

Every test asserts the link's target id (or its absence) against a real store.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from helpers import StubCtx, make_chunk
from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE
from memtomem.models import Chunk
from memtomem.server.context import AppContext
from memtomem.server.tools import _append_target, memory_crud
from memtomem.server.tools.memory_crud import _REPLAY_MARKER
from memtomem.server.tools.multi_agent import mem_agent_register, mem_agent_share

_LOGGER = "memtomem.server.tools.multi_agent"
_UNATTRIBUTED = "could not tell which chunk holds the share copy"


@pytest.fixture
async def stack(bm25_only_components):
    comp, mem_dir = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    await mem_agent_register(agent_id="alpha", ctx=ctx)
    return comp, mem_dir, ctx


async def _source(comp, content: str) -> Chunk:
    chunk = make_chunk(content, namespace=f"{AGENT_NAMESPACE_PREFIX}alpha")
    await comp.storage.upsert_chunks([chunk])
    return chunk


async def _share(ctx, source: Chunk, **kwargs) -> str:
    result = await mem_agent_share(
        chunk_id=str(source.id), target=SHARED_NAMESPACE, ctx=ctx, **kwargs
    )
    assert result.startswith("Shared to namespace"), result
    return result


async def _add(ctx, content: str, title: str, tags: list[str] | None = None) -> Path:
    """A plain entry in the shared day file, written and indexed the real way."""
    seen: list[str] = []
    result, stats = await memory_crud._mem_add_core(
        content=content,
        title=title,
        tags=tags,
        file=None,
        namespace=SHARED_NAMESPACE,
        template=None,
        ctx=ctx,
        event_type="mem_add",
        on_appended=seen.append,
    )
    assert stats is not None, result
    return _day_file(ctx)


def _day_file(ctx) -> Path:
    mem_dir = Path(ctx.request_context.lifespan_context.config.indexing.memory_dirs[0])
    (path,) = mem_dir.rglob(f"*--{SHARED_NAMESPACE}-*.md")
    return path


async def _shared_chunks(comp) -> list[Chunk]:
    db = comp.storage._get_db()
    ids = [
        r[0]
        for r in db.execute(
            "SELECT id FROM chunks WHERE namespace = ?", (SHARED_NAMESPACE,)
        ).fetchall()
    ]
    from uuid import UUID

    found = await comp.storage.get_chunks_batch([UUID(i) for i in ids])
    return list(found.values())


async def _chunk_holding(comp, token: str) -> Chunk:
    (chunk,) = [c for c in await _shared_chunks(comp) if token in c.content]
    return chunk


async def _link_targets(comp, source: Chunk) -> list:
    return [link.target_id for link in await comp.storage.get_chunks_shared_from(source.id)]


def _record_stats(monkeypatch, *, after_append=None) -> list:
    """Capture the share's ``IndexingStats``; optionally act right after the append."""
    seen: list = []
    real = memory_crud._mem_add_core

    async def record(*args, **kwargs):
        if after_append is not None:
            collect = kwargs["on_appended"]

            def on_appended(text: str) -> None:
                collect(text)
                after_append(text)

            kwargs["on_appended"] = on_appended
        out = await real(*args, **kwargs)
        seen.append(out[1])
        return out

    monkeypatch.setattr(memory_crud, "_mem_add_core", record)
    return seen


# ---------------------------------------------------------------------------
# Linked to the copy
# ---------------------------------------------------------------------------


class TestLinksItsOwnCopy:
    @pytest.mark.asyncio
    async def test_a_share_links_the_chunk_holding_the_copy(self, stack):
        comp, _, ctx = stack
        source = await _source(comp, "zqplain cache tuning strategy for the query layer")
        await _share(ctx, source)

        copy = await _chunk_holding(comp, "zqplain")
        assert await _link_targets(comp, source) == [copy.id]

    @pytest.mark.asyncio
    async def test_a_pending_edit_indexed_with_the_copy(self, stack, monkeypatch):
        # The re-index upserts the edited entry too, and it comes first in
        # ``new_chunk_ids``; it took the link before #3315.
        comp, _, ctx = stack
        comp.index_engine._config.min_chunk_tokens = 0
        path = await _add(ctx, "zqseed body that is already indexed.", "Seed")
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("zqseed body", "zqedited body"), encoding="utf-8")
        seen = _record_stats(monkeypatch)

        source = await _source(comp, "zqpending copy body for the link.")
        await _share(ctx, source)

        copy = await _chunk_holding(comp, "zqpending")
        edited = await _chunk_holding(comp, "zqedited")
        assert set(seen[0].new_chunk_ids) == {edited.id, copy.id}
        assert seen[0].appended_chunk_ids == (copy.id,)
        assert await _link_targets(comp, source) == [copy.id]

    @pytest.mark.asyncio
    async def test_a_copy_that_keeps_an_existing_id(self, stack, monkeypatch):
        # The copy matches a stored chunk by content hash and keeps that id, so
        # it is not in ``new_chunk_ids``; a pending edit leaves another chunk
        # holding the copy's text as the only new id. That chunk took the link
        # before #3315 (or nothing was linked when there was no new id).
        comp, _, ctx = stack
        comp.index_engine._config.min_chunk_tokens = 0
        # Tagged, so the entry's metadata blockquote (with its timestamp) is not
        # part of the chunk, as for the share copy.
        await _add(ctx, "repeat", "Shared: memory", tags=["seed"])
        path = await _add(ctx, "prefix", "Other", tags=["seed"])
        (reused_id,) = [c.id for c in await _shared_chunks(comp) if c.content == "repeat"]
        # Unindexed: drops the first entry and makes Other hold the copy's text.
        path.write_text("## Other\n\nprefix repeat\n", encoding="utf-8")
        seen = _record_stats(monkeypatch)

        source = await _source(comp, "repeat")
        await _share(ctx, source)

        rewritten = await _chunk_holding(comp, "prefix repeat")
        assert reused_id not in seen[0].new_chunk_ids
        assert seen[0].appended_chunk_ids == (reused_id,)
        assert await _link_targets(comp, source) == [reused_id]
        assert rewritten.id != reused_id


# ---------------------------------------------------------------------------
# Not linked
# ---------------------------------------------------------------------------


class TestUnsureLinksNothing:
    @pytest.mark.asyncio
    async def test_a_copy_merged_into_the_entry_before_it(self, stack, monkeypatch, caplog):
        # A short copy folds into the preceding plain entry under the same
        # heading. The merged chunk is not a copy of the source, and linking it
        # would make the plain entry's next edit fail the linked-identity guard
        # (#3324).
        comp, _, ctx = stack
        path = await _add(ctx, "zqneighbour short entry.", "Shared: memory", tags=["seed"])
        seen = _record_stats(monkeypatch)

        source = await _source(comp, "zqmerged short copy.")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            await _share(ctx, source)

        merged = await _chunk_holding(comp, "zqmerged")
        assert "zqneighbour" in merged.content
        assert seen[0].appended_attribution == "merged"
        assert seen[0].appended_chunk_ids == (merged.id,)
        assert await _link_targets(comp, source) == []
        assert _UNATTRIBUTED in caplog.text

        # The neighbour stays editable.
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("zqneighbour", "zqrevised"), encoding="utf-8")
        await comp.index_engine.index_file(path, ingress_guarded=True)
        assert "zqrevised" in (await _chunk_holding(comp, "zqmerged")).content

    @pytest.mark.asyncio
    async def test_another_writer_between_append_and_re_index(self, stack, monkeypatch, caplog):
        comp, _, ctx = stack

        def intrude(_text: str) -> None:
            path = _day_file(ctx)
            with open(path, "a", encoding="utf-8") as f:
                f.write("\n## Intruder\n\nzqintruder written by another agent.\n")

        seen = _record_stats(monkeypatch, after_append=intrude)
        source = await _source(comp, "zqraced copy body for the link.")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            await _share(ctx, source)

        assert seen[0].appended_attribution == "unverified"
        assert seen[0].appended_chunk_ids == ()
        assert await _link_targets(comp, source) == []
        assert _UNATTRIBUTED in caplog.text

    @pytest.mark.asyncio
    async def test_the_target_file_excluded_after_the_append(self, stack, monkeypatch, caplog):
        comp, _, ctx = stack

        def exclude(_text: str) -> None:
            comp.config.indexing.exclude_patterns = [f"**/*--{SHARED_NAMESPACE}-*.md"]

        seen = _record_stats(monkeypatch, after_append=exclude)
        source = await _source(comp, "zqexcluded copy body.")
        with caplog.at_level(logging.WARNING, logger=_LOGGER):
            await mem_agent_share(chunk_id=str(source.id), target=SHARED_NAMESPACE, ctx=ctx)

        assert seen[0].excluded_files
        assert await _link_targets(comp, source) == []
        assert _UNATTRIBUTED in caplog.text

    @pytest.mark.asyncio
    async def test_an_attribution_error_completes_the_keyed_share(self, stack, monkeypatch):
        # The copy is on disk and indexed by then; an error result, or a
        # released claim, would let a retry append it a second time.
        comp, _, ctx = stack

        async def boom(app, chunk_id):
            raise RuntimeError("store read failed")

        monkeypatch.setattr(_append_target, "resolve_chunk", boom)
        source = await _source(comp, "zqboom copy body for the link.")
        first = await _share(ctx, source, idempotency_key="k-share-boom")
        replay = await mem_agent_share(
            chunk_id=str(source.id),
            target=SHARED_NAMESPACE,
            ctx=ctx,
            idempotency_key="k-share-boom",
        )

        assert replay == first + _REPLAY_MARKER
        assert _day_file(ctx).read_text(encoding="utf-8").count("zqboom") == 1
        assert await _link_targets(comp, source) == []
