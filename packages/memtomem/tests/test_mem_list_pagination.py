"""``mem_list`` pages through source files without listing the whole store.

The tool used to aggregate every source and filter the result in Python. Now
``SqliteBackend.get_source_files_page`` filters in SQL before ``LIMIT``, so the
oracles below are the pre-pagination Python rules, applied to the unpaged
``get_source_files_with_counts`` — a page must keep exactly what they kept.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from uuid import uuid4

import pytest

from helpers import StubCtx
from memtomem.models import (
    CONSOLIDATED_SUFFIX,
    ORIGIN_CONSOLIDATION_POLICY,
    Chunk,
    ChunkMetadata,
    has_namespace_prefix,
)
from memtomem.search.pipeline import match_source_filter
from memtomem.server.context import AppContext
from memtomem.server.formatters import _display_path
from memtomem.server.tools.browse import mem_list

_SYSTEM = ("archive:", "agent-runtime:")


def _chunk(source: Path, namespace: str = "default", origin: str | None = None) -> Chunk:
    return Chunk(
        content=f"{source.name} in {namespace}",
        metadata=ChunkMetadata(source_file=source, namespace=namespace, origin=origin),
        content_hash=f"hash-{uuid4().hex}",
        embedding=[0.1] * 1024,
    )


def _ctx(comp) -> StubCtx:
    return StubCtx(AppContext.from_components(comp))


def _listed(output: str) -> list[str]:
    """File names on the tool's per-file rows, in order."""
    return [
        Path(line.strip().split("  — ")[0]).name
        for line in output.splitlines()
        if line.startswith("  ") and "  — " in line
    ]


_MORE = re.compile(r"^More: mem_list\((.*)\)$", re.MULTILINE)
_ARG = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\d+)')


def _more_kwargs(output: str) -> dict | None:
    """The keyword arguments of the ``More:`` call, decoded, or ``None``."""
    match = _MORE.search(output)
    if match is None:
        return None
    return {key: json.loads(value) for key, value in _ARG.findall(match.group(1))}


async def _traverse(ctx, **kwargs) -> list[str]:
    names: list[str] = []
    for _ in range(100):
        out = await mem_list(ctx=ctx, **kwargs)  # type: ignore[arg-type]
        names += _listed(out)
        more = _more_kwargs(out)
        if more is None:
            return names
        kwargs = more
    raise AssertionError("traversal did not end")


def _oracle(rows, hidden, source_filter=None, namespace=None, prefixes=_SYSTEM):
    """The pre-pagination ``mem_list`` filters, verbatim."""
    rows = [r for r in rows if str(r[0]) not in {str(p) for p in hidden}]
    if source_filter:
        rows = [r for r in rows if match_source_filter(source_filter, str(r[0]))]
    if namespace:
        rows = [r for r in rows if r[3] and namespace in r[3].split(",")]
    elif prefixes:
        rows = [
            r
            for r in rows
            if not r[3] or any(not has_namespace_prefix(ns, prefixes) for ns in r[3].split(","))
        ]
    return rows


async def _page_all(storage, **kwargs) -> list[tuple]:
    rows, cursor = await storage.get_source_files_page(after=None, limit=500, **kwargs)
    assert cursor is None
    return rows


@pytest.fixture
async def matrix(bm25_only_components):
    """Sources covering every visibility and namespace rule the listing applies."""
    comp, mem_dir = bm25_only_components
    p = {
        name: mem_dir / f"{name}.md"
        for name in (
            "plain",
            "held",
            "pending",
            "parent_held",
            "parent_open",
            "archived",
            "agent",
            "mixed",
            "comma",
            "comma_sys",
            "empty_ns",
        )
    }
    summary_of_held = mem_dir / f"parent_held.md{CONSOLIDATED_SUFFIX}"
    plain_summary_of_held = mem_dir / f"parent_held2.md{CONSOLIDATED_SUFFIX}"
    mixed_summary_of_held = mem_dir / f"parent_held3.md{CONSOLIDATED_SUFFIX}"
    summary_of_open = mem_dir / f"parent_open.md{CONSOLIDATED_SUFFIX}"
    await comp.storage.upsert_chunks(
        [
            _chunk(p["plain"]),
            _chunk(p["held"]),
            _chunk(p["pending"]),
            _chunk(p["parent_held"]),
            _chunk(mem_dir / "parent_held2.md"),
            _chunk(mem_dir / "parent_held3.md"),
            _chunk(p["parent_open"]),
            _chunk(p["archived"], "archive:2026"),
            _chunk(p["agent"], "agent-runtime:planner"),
            _chunk(p["mixed"]),
            _chunk(p["mixed"], "archive:2026"),
            # Namespace values are not comma-free in storage (mem_add stores
            # them verbatim); the listing has always split the aggregate.
            _chunk(p["comma"], "work,personal"),
            _chunk(p["comma_sys"], "archive:x,work"),
            _chunk(p["empty_ns"], ""),
            _chunk(summary_of_held, "default", ORIGIN_CONSOLIDATION_POLICY),
            # Not a policy chunk, so its held parent does not hide it.
            _chunk(plain_summary_of_held),
            # One policy chunk in another namespace hides the whole file, even
            # under a namespace= that selects only the other chunk.
            _chunk(mixed_summary_of_held, "work"),
            _chunk(mixed_summary_of_held, "archive:2026", ORIGIN_CONSOLIDATION_POLICY),
            _chunk(summary_of_open, "default", ORIGIN_CONSOLIDATION_POLICY),
        ]
    )
    # A hold applies only to an indexed path, so each parent has a chunk above.
    for parent in ("parent_held", "parent_held2", "parent_held3"):
        assert await comp.storage.hold_source(mem_dir / f"{parent}.md", "test_hold")
    assert await comp.storage.hold_source(p["held"], "test_hold")
    assert comp.storage.queue_source_check_sync(p["pending"])
    return comp


class TestPageMatchesUnpagedFilters:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "kwargs",
        [
            {},
            {"namespace": "work"},
            {"namespace": "personal"},
            {"namespace": "archive:2026"},
            {"namespace": "work,personal"},
            {"namespace": ""},
            {"source_filter": "*held*"},
            {"source_filter": "consolidated"},
            {"source_filter": "plain", "namespace": "default"},
            {"prefixes": ()},
            {"prefixes": ("",)},
        ],
        ids=lambda kw: json.dumps(kw),
    )
    async def test_page_keeps_what_the_python_filters_kept(self, matrix, kwargs):
        storage = matrix.storage
        prefixes = kwargs.pop("prefixes", _SYSTEM)
        expected = _oracle(
            await storage.get_source_files_with_counts(),
            await storage.get_hidden_source_files(),
            prefixes=prefixes,
            **kwargs,
        )
        got = await _page_all(storage, system_prefixes=prefixes, **kwargs)
        assert got == expected

    @pytest.mark.asyncio
    async def test_visibility_twin_matches_the_hidden_set(self, matrix):
        storage = matrix.storage
        hidden = {str(p) for p in await storage.get_hidden_source_files()}
        every = {str(r[0]) for r in await storage.get_source_files_with_counts()}
        listed = {str(r[0]) for r in await _page_all(storage)}
        assert listed == every - hidden
        # The matrix exercises each branch of the twin, not just the direct holds.
        names = {Path(p).name for p in hidden}
        assert f"parent_held.md{CONSOLIDATED_SUFFIX}" in names
        assert f"parent_held3.md{CONSOLIDATED_SUFFIX}" in names
        assert f"parent_held2.md{CONSOLIDATED_SUFFIX}" not in names
        assert f"parent_open.md{CONSOLIDATED_SUFFIX}" not in names


class TestTraversal:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [1, 2, 3])
    async def test_pages_cover_the_unpaged_listing_once_in_order(self, matrix, limit):
        expected = _listed(await mem_list(limit=500, ctx=_ctx(matrix)))  # type: ignore[arg-type]
        assert len(expected) > 3
        names = await _traverse(_ctx(matrix), limit=limit)
        assert names == expected

    @pytest.mark.asyncio
    async def test_continuation_carries_the_filters(self, matrix):
        ctx = _ctx(matrix)
        first = await mem_list(source_filter="*.md", namespace="work", limit=1, ctx=ctx)  # type: ignore[arg-type]
        more = _more_kwargs(first)
        assert more is not None
        assert more["source_filter"] == "*.md"
        assert more["namespace"] == "work"
        assert more["limit"] == 1
        names = await _traverse(ctx, source_filter="*.md", namespace="work", limit=1)
        assert names == ["comma.md", "comma_sys.md"]

    @pytest.mark.asyncio
    async def test_hidden_files_take_no_page_slot(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        await comp.storage.upsert_chunks(
            [_chunk(mem_dir / "a_held.md"), _chunk(mem_dir / "b_archived.md", "archive:x")]
            + [_chunk(mem_dir / "c_plain.md")]
        )
        assert await comp.storage.hold_source(mem_dir / "a_held.md", "test_hold")
        out = await mem_list(limit=1, ctx=_ctx(comp))  # type: ignore[arg-type]
        assert _listed(out) == ["c_plain.md"]
        assert "More:" not in out


class TestPageBoundary:
    @pytest.fixture
    async def three(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        await comp.storage.upsert_chunks([_chunk(mem_dir / f"{n}.md") for n in "abc"])
        return comp

    @pytest.mark.asyncio
    async def test_one_full_page_reads_as_the_unpaged_listing(self, three):
        out = await mem_list(limit=3, ctx=_ctx(three))  # type: ignore[arg-type]
        assert out.startswith("Indexed files: 3\n")
        assert out.endswith("\nTotal: 3 files, 3 chunks")
        assert "More:" not in out and "Page:" not in out

    @pytest.mark.asyncio
    async def test_one_more_file_than_the_page_continues(self, three):
        ctx = _ctx(three)
        first = await mem_list(limit=2, ctx=ctx)  # type: ignore[arg-type]
        assert first.startswith("Indexed files (page): 2\n")
        assert "\nPage: 2 files, 2 chunks\nMore: mem_list(cursor=" in first
        more = _more_kwargs(first)
        assert more is not None and Path(more["cursor"]).name == "b.md"
        second = await mem_list(ctx=ctx, **more)  # type: ignore[arg-type]
        assert _listed(second) == ["c.md"]
        assert second.endswith("\nPage: 1 files, 1 chunks\nEnd of list.")

    @pytest.mark.asyncio
    async def test_default_page_is_fifty_files(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        await comp.storage.upsert_chunks([_chunk(mem_dir / f"{i:02d}.md") for i in range(51)])
        out = await mem_list(ctx=_ctx(comp))  # type: ignore[arg-type]
        assert len(_listed(out)) == 50
        more = _more_kwargs(out)
        assert more is not None and set(more) == {"cursor"}  # the default is not repeated
        assert Path(more["cursor"]).name == "49.md"

    @pytest.mark.asyncio
    async def test_cursor_past_the_end(self, three):
        out = await mem_list(cursor="\U0010ffff", ctx=_ctx(three))  # type: ignore[arg-type]
        assert out == "No more files."

    @pytest.mark.asyncio
    async def test_empty_cursor_is_a_cursor(self, bm25_only_components):
        """``""`` sorts first, so it lists from the start, labelled as a page."""
        comp, mem_dir = bm25_only_components
        ctx = _ctx(comp)
        assert await mem_list(cursor="", ctx=ctx) == "No more files."  # type: ignore[arg-type]
        await comp.storage.upsert_chunks([_chunk(mem_dir / "a.md")])
        out = await mem_list(cursor="", ctx=ctx)  # type: ignore[arg-type]
        assert _listed(out) == ["a.md"] and out.endswith("End of list.")


class TestMessages:
    @pytest.mark.asyncio
    async def test_empty_store(self, bm25_only_components):
        comp, _ = bm25_only_components
        assert await mem_list(ctx=_ctx(comp)) == "No indexed files."  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_filter_that_matches_nothing(self, matrix):
        out = await mem_list(source_filter="*.txt", ctx=_ctx(matrix))  # type: ignore[arg-type]
        assert out == "No files match the filter."

    @pytest.mark.asyncio
    async def test_store_of_system_only_files_is_a_filter_miss(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        await comp.storage.upsert_chunks([_chunk(mem_dir / "a.md", "archive:x")])
        assert await mem_list(ctx=_ctx(comp)) == "No files match the filter."  # type: ignore[arg-type]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit", [0, 501])
    async def test_limit_bounds(self, matrix, limit):
        out = await mem_list(limit=limit, ctx=_ctx(matrix))  # type: ignore[arg-type]
        assert out == f"Error: limit must be between 1 and 500, got {limit}."

    @pytest.mark.asyncio
    async def test_small_store_output_is_unchanged(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        chunk = _chunk(mem_dir / "only.md")
        await comp.storage.upsert_chunks([chunk])
        (path, *_rest) = (await comp.storage.get_source_files_with_counts())[0]
        tok = len(chunk.content) // 3
        assert await mem_list(ctx=_ctx(comp)) == (  # type: ignore[arg-type]
            f"Indexed files: 1\n\n  {_display_path(path)}  — 1 chunks, ~{tok} tok/chunk"
            " [default]\n\nTotal: 1 files, 1 chunks"
        )


class TestCursorIsTheStoredKey:
    @pytest.mark.asyncio
    async def test_resume_from_a_key_path_would_respell(self, bm25_only_components):
        """``Path`` respells some stored keys — ``//`` here, ``/`` → ``\\`` on Windows.

        Resuming from the respelt key would skip ``//b.md``, which sorts between
        ``//a.md`` and ``/a.md``; the cursor must be the stored string itself.
        """
        comp, mem_dir = bm25_only_components
        chunks = {name: _chunk(mem_dir / name) for name in ("a.md", "b.md")}
        await comp.storage.upsert_chunks(list(chunks.values()))
        db = comp.storage._get_db()
        for name, chunk in chunks.items():
            updated = db.execute(
                "UPDATE chunks SET source_file=? WHERE id=?", (f"/notes//{name}", str(chunk.id))
            ).rowcount
            assert updated == 1
        db.commit()

        rows, cursor = await comp.storage.get_source_files_page(after=None, limit=1)
        assert cursor == "/notes//a.md"
        assert str(rows[0][0]) != cursor  # the row's Path is the respelt form
        rows, cursor = await comp.storage.get_source_files_page(after=cursor, limit=1)
        assert [Path(r[0]).name for r in rows] == ["b.md"] and cursor is None
