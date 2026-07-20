"""``mem_ns_rename`` tool surface: outcome wording and the merge consent gate.

The storage contract is pinned in ``test_server_tools_org.py``; this file
covers what the *tool* says and accepts. Two things matter here:

* The reported chunk count is not the whole outcome — a namespace that
  exists only as metadata renames with ``0 chunks moved``, and a merge
  moves rows into a namespace that already had some. The message has to
  say which happened, or "0" reads as "nothing happened" (issue #1874).
* ``merge=True`` consolidates two namespaces and drops the source's
  metadata row. That is consent-shaped, so it must be a literal boolean
  on both doors into this function — the FastMCP model (``StrictBool``)
  and the ``mem_do`` dispatcher, which forwards ``params`` unvalidated.
"""

from __future__ import annotations

import pytest

from memtomem.server.context import AppContext
from memtomem.server.tools.namespace import mem_ns_rename

from helpers import make_chunk
from test_validate_namespace import _StubCtx


@pytest.fixture
def ctx(components):
    return _StubCtx(AppContext.from_components(components))


class TestRenameMessage:
    async def test_reports_moved_chunks(self, ctx, storage):
        await storage.upsert_chunks([make_chunk(content="one", namespace="src-ns")])
        out = await mem_ns_rename(old="src-ns", new="dst-ns", ctx=ctx)
        assert "1 chunks moved" in out

    async def test_metadata_only_rename_says_so(self, ctx, storage):
        """``0 chunks moved`` alone would read as "nothing changed"."""
        await storage.set_namespace_meta("src-ns", description="registered only")
        out = await mem_ns_rename(old="src-ns", new="dst-ns", ctx=ctx)
        assert "0 chunks moved, metadata row renamed" in out

    async def test_merge_is_named_in_the_message(self, ctx, storage):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        out = await mem_ns_rename(old="src-ns", new="dst-ns", merge=True, ctx=ctx)
        assert "merged into existing 'dst-ns'" in out


class TestRenameConflict:
    async def test_existing_target_is_refused(self, ctx, storage):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        out = await mem_ns_rename(old="src-ns", new="dst-ns", ctx=ctx)
        assert out.startswith("Error:") and "target already exists" in out

    async def test_refusal_leaves_the_source_alone(self, ctx, storage):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        await mem_ns_rename(old="src-ns", new="dst-ns", ctx=ctx)
        assert dict(await storage.list_namespaces())["src-ns"] == 1

    async def test_refusal_names_the_way_forward(self, ctx, storage):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        out = await mem_ns_rename(old="src-ns", new="dst-ns", ctx=ctx)
        assert "merge=True" in out


class TestMergeConsentGate:
    """``mem_do(action="ns_rename", params={...})`` reaches the body raw."""

    @pytest.mark.parametrize("value", ["true", "True", 1, "false", 0])
    async def test_non_literal_merge_is_refused(self, ctx, storage, value):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        out = await mem_ns_rename(old="src-ns", new="dst-ns", merge=value, ctx=ctx)
        assert out.startswith("Error:") and "literal boolean" in out

    async def test_a_refused_merge_flag_does_not_consolidate(self, ctx, storage):
        await storage.upsert_chunks(
            [
                make_chunk(content="one", namespace="src-ns"),
                make_chunk(content="two", namespace="dst-ns"),
            ]
        )
        await mem_ns_rename(old="src-ns", new="dst-ns", merge="true", ctx=ctx)
        assert dict(await storage.list_namespaces())["src-ns"] == 1
