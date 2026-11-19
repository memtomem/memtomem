"""``mem_agent_share`` links the source to the chunk its copy became (#3315).

The ``chunk_links`` ``shared`` row used to target ``new_chunk_ids[0]``. That
list covers the whole re-index of the target day file: a pending edit earlier
in the file comes first in it, and a copy whose chunk kept an existing id is
not in it at all. The share now reads the chunk the indexer attributes to its
append (``appended_chunk_ids``, #3292) and links only an ``exact`` attribution:
a chunk holding more than the copy is not a copy of the source, and a link on it
would block the other text's next edit.

Each copy goes to a file of its own (#3324). In the namespace's day file a short
copy merged into the entry before it, and when that entry was an earlier copy,
its link made the re-index refuse: the copy stayed on disk unindexed and the
day file stopped indexing.

Every test asserts the link's target id (or its absence) against a real store.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from helpers import StubCtx, make_chunk
from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE
from memtomem.memory_scope import SHARE_COPY_DIR, day_file_name
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


def _mem_dir(ctx) -> Path:
    return Path(ctx.request_context.lifespan_context.config.indexing.memory_dirs[0])


def _day_file(ctx) -> Path:
    """The namespace's day file, where a plain ``mem_add`` lands."""
    default = ctx.request_context.lifespan_context.config.namespace.default_namespace
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return _mem_dir(ctx) / day_file_name(SHARED_NAMESPACE, default, date_str=today)


def _copy_files(ctx) -> list[Path]:
    """The share copies' files, one per copy."""
    return sorted((_mem_dir(ctx) / SHARE_COPY_DIR).glob("*.md"))


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
    async def test_a_pending_edit_in_the_day_file_is_left_alone(self, stack, monkeypatch):
        # When the copy went to the day file, the share's re-index took an
        # unindexed edit there too, and it came first in ``new_chunk_ids``; it
        # took the link before #3315. The copy's own file holds nothing else.
        comp, _, ctx = stack
        comp.index_engine._config.min_chunk_tokens = 0
        path = await _add(ctx, "zqseed body that is already indexed.", "Seed")
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("zqseed body", "zqedited body"), encoding="utf-8")
        seen = _record_stats(monkeypatch)

        source = await _source(comp, "zqpending copy body for the link.")
        await _share(ctx, source)

        copy = await _chunk_holding(comp, "zqpending")
        assert list(seen[0].new_chunk_ids) == [copy.id]
        assert seen[0].appended_chunk_ids == (copy.id,)
        assert await _link_targets(comp, source) == [copy.id]
        assert not [c for c in await _shared_chunks(comp) if "zqedited" in c.content]


# ---------------------------------------------------------------------------
# Each copy in a file of its own (#3324)
# ---------------------------------------------------------------------------


async def _cli_share(comp, monkeypatch, source: Chunk) -> str | None:
    """``mm agent share`` of ``source`` into ``shared``: ``None``, or its refusal."""
    from test_excluded_target_writes import _cli, _patch_cli_components

    from memtomem.cli.agent_cmd import _run_share

    _patch_cli_components(monkeypatch, comp)
    return await _cli(_run_share(str(source.id), SHARED_NAMESPACE, False))


class TestCopiesDoNotShareAFile:
    @pytest.mark.asyncio
    async def test_two_short_shares_are_both_indexed_and_linked(self, stack):
        # The #3324 reproduction: the second copy merged into the first's
        # linked chunk, and the re-index refused to drop that chunk's id.
        comp, _, ctx = stack
        first = await _source(comp, "zqa short")
        second = await _source(comp, "zqb short")
        await _share(ctx, first)
        await _share(ctx, second)

        assert len(_copy_files(ctx)) == 2
        assert await _link_targets(comp, first) == [(await _chunk_holding(comp, "zqa")).id]
        assert await _link_targets(comp, second) == [(await _chunk_holding(comp, "zqb")).id]

    @pytest.mark.asyncio
    async def test_the_same_source_shared_twice(self, stack):
        comp, _, ctx = stack
        source = await _source(comp, "zqtwice short")
        await _share(ctx, source)
        await _share(ctx, source)

        copies = [c for c in await _shared_chunks(comp) if "zqtwice" in c.content]
        assert len(copies) == 2
        assert sorted(await _link_targets(comp, source)) == sorted(c.id for c in copies)

    @pytest.mark.asyncio
    async def test_with_chunk_overlap(self, stack):
        # Overlap borrows text from a neighbouring chunk of the same file only.
        comp, _, ctx = stack
        comp.index_engine._config.chunk_overlap_tokens = 16
        first = await _source(comp, "zqoverlap first copy body.")
        second = await _source(comp, "zqoverlap second copy body.")
        await _share(ctx, first)
        await _share(ctx, second)

        assert len(await _link_targets(comp, first)) == 1
        assert len(await _link_targets(comp, second)) == 1

    @pytest.mark.asyncio
    async def test_an_entry_titled_like_the_copy_stays_editable(self, stack):
        # Same heading as the copy, written after it, then edited: in one day
        # file the entry merged into the linked copy and every write failed.
        comp, _, ctx = stack
        source = await _source(comp, "zqcopy short")
        await _share(ctx, source)
        path = await _add(ctx, "zqentry short.", "Shared: memory", tags=["seed"])
        text = path.read_text(encoding="utf-8")
        path.write_text(text.replace("zqentry", "zqedited"), encoding="utf-8")
        stats = await comp.index_engine.index_file(path, ingress_guarded=True)

        assert not stats.errors, stats.errors
        assert await _chunk_holding(comp, "zqedited")
        assert await _link_targets(comp, source) == [(await _chunk_holding(comp, "zqcopy")).id]

    @pytest.mark.asyncio
    async def test_mcp_and_cli_copies_each_get_a_file(self, stack, monkeypatch):
        comp, _, ctx = stack
        linked = await _source(comp, "zqmcp short")
        await _share(ctx, linked)
        assert await _cli_share(comp, monkeypatch, await _source(comp, "zqcli short")) is None

        assert len(_copy_files(ctx)) == 2
        assert await _link_targets(comp, linked) == [(await _chunk_holding(comp, "zqmcp")).id]
        assert await _chunk_holding(comp, "zqcli")


class TestTheCopyFileIsNew:
    """The copy must be its file's only entry, so an existing file is refused."""

    @pytest.fixture
    def taken(self, monkeypatch, stack):
        _, mem_dir, _ = stack
        name = f"{SHARE_COPY_DIR}/taken-0123456789ab.md"
        monkeypatch.setattr("memtomem.memory_scope.share_copy_file_name", lambda *a, **k: name)
        path = mem_dir / name
        path.parent.mkdir()
        path.write_text("## Earlier\n\nzqearlier entry.\n", encoding="utf-8")
        return path

    @pytest.mark.asyncio
    async def test_mcp(self, stack, taken):
        comp, _, ctx = stack
        source = await _source(comp, "zqrefused short")
        result = await mem_agent_share(
            chunk_id=str(source.id), target=SHARED_NAMESPACE, ctx=ctx, idempotency_key="k-taken"
        )

        assert result.startswith("Error: ") and "already exists; nothing was written" in result
        assert taken.read_text(encoding="utf-8") == "## Earlier\n\nzqearlier entry.\n"
        assert await _link_targets(comp, source) == []
        # Nothing landed, so the key was released and a retry is not "in progress".
        retry = await mem_agent_share(
            chunk_id=str(source.id), target=SHARED_NAMESPACE, ctx=ctx, idempotency_key="k-taken"
        )
        assert "already exists" in retry, retry

    @pytest.mark.asyncio
    async def test_cli(self, stack, taken, monkeypatch):
        comp, _, _ = stack
        result = await _cli_share(comp, monkeypatch, await _source(comp, "zqrefused short"))

        assert result is not None and "already exists; nothing was written" in result
        assert taken.read_text(encoding="utf-8") == "## Earlier\n\nzqearlier entry.\n"


def _symlink_or_skip(link: Path, target: Path) -> None:
    import os

    try:
        os.symlink(target, link, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # Windows without the privilege
        pytest.skip(f"symlinks unavailable: {exc}")


class TestALinkedCopyDirectory:
    """The copies' directory is judged where it resolves, as a day file is (#2807)."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("surface", ["mcp", "cli"])
    @pytest.mark.parametrize("where", ["outside", "nested"])
    async def test_into_another_tier_is_refused(self, stack, tmp_path, monkeypatch, surface, where):
        comp, mem_dir, ctx = stack
        root = {"outside": tmp_path / "proj", "nested": mem_dir / "proj"}[where]
        tier = root / ".memtomem" / "memories"
        tier.mkdir(parents=True)
        comp.config.indexing.project_memory_dirs = [tier]
        _symlink_or_skip(mem_dir / SHARE_COPY_DIR, tier)
        source = await _source(comp, "zqtier short")

        if surface == "mcp":
            result = await mem_agent_share(
                chunk_id=str(source.id), target=SHARED_NAMESPACE, ctx=ctx
            )
            assert result.startswith("Error: ") and "nothing" in result, result
        else:
            result = await _cli_share(comp, monkeypatch, source)
            assert result is not None and "nothing" in result, result
        assert list(tier.iterdir()) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("surface", ["mcp", "cli"])
    async def test_inside_its_own_tier_still_writes(self, stack, monkeypatch, surface):
        comp, mem_dir, ctx = stack
        real = mem_dir / "elsewhere"
        real.mkdir()
        _symlink_or_skip(mem_dir / SHARE_COPY_DIR, real)
        source = await _source(comp, "zqlinked short")

        if surface == "mcp":
            await _share(ctx, source)
        else:
            assert await _cli_share(comp, monkeypatch, source) is None
        (written,) = real.glob("*.md")
        assert "zqlinked" in written.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Not linked
# ---------------------------------------------------------------------------


class TestUnsureLinksNothing:
    @pytest.mark.asyncio
    async def test_another_writer_between_append_and_re_index(self, stack, monkeypatch, caplog):
        comp, _, ctx = stack

        def intrude(_text: str) -> None:
            (path,) = _copy_files(ctx)
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
        (copy_file,) = _copy_files(ctx)
        assert copy_file.read_text(encoding="utf-8").count("zqboom") == 1
        assert await _link_targets(comp, source) == []
