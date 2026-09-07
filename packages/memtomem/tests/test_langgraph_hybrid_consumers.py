"""Optional pinned consumer contract tests; no model or network calls.

Run in an isolated environment with tools/hybrid-store-consumers.txt installed.
"""

import pytest

pytest.importorskip("langgraph")
from memtomem.integrations import MemtomemHybridStore


def test_deepagents_store_backend(tmp_path):
    pytest.importorskip("deepagents")
    from deepagents.backends import StoreBackend

    with MemtomemHybridStore(tmp_path / "deep.db") as store:
        backend = StoreBackend(store=store, namespace=lambda runtime: ("agent", "filesystem"))
        assert backend.write("/notes.md", "Remember apples").error is None
        read = backend.read("/notes.md")
        assert read.error is None
        assert "Remember apples" in str(read)
        assert backend.edit("/notes.md", "apples", "pears").error is None
        assert "pears" in str(backend.read("/notes.md"))
        listing = backend.ls("/")
        assert listing.error is None
        assert "/notes.md" in str(listing)
        assert store.search(("agent",), query="pears")


@pytest.mark.asyncio
async def test_langmem_tools(tmp_path):
    pytest.importorskip("langmem")
    from langmem import create_manage_memory_tool, create_search_memory_tool

    async with MemtomemHybridStore(tmp_path / "langmem.db") as store:
        manage = create_manage_memory_tool(("user", "memories"), store=store)
        search = create_search_memory_tool(("user", "memories"), store=store)
        await manage.ainvoke({"content": "User likes apples", "action": "create"})
        records = await store.asearch(("user", "memories"))
        assert len(records) == 1
        assert "apples" in str(await search.ainvoke({"query": "apples"}))
        await manage.ainvoke(
            {"id": records[0].key, "content": "User likes pears", "action": "update"}
        )
        assert not await store.asearch((), query="apples")
        assert "pears" in str(await search.ainvoke({"query": "pears"}))
        await manage.ainvoke({"id": records[0].key, "action": "delete"})
        assert not await store.asearch(())
