"""Import-time isolation contract for :mod:`memtomem.runtime`.

In-process embedders (LangGraph / deepagents hosts, notebooks, the quality
harness) build components through ``memtomem.runtime``. That path must not
drag in the MCP server or its SDK: doing so costs import time, pulls a
transport dependency into library use, and re-couples the engine to the
server package the runtime layer was extracted from.

Each check runs in a subprocess because ``sys.modules`` is process-global —
an earlier test importing ``memtomem.server`` would mask the regression
(the pattern in ``test_langgraph_optional_import.py``).
"""

from __future__ import annotations

import subprocess
import sys

_ASSERT_CLEAN = """
import sys

leaked = sorted(
    name
    for name in sys.modules
    if name == "mcp" or name.startswith("mcp.")
    or name == "memtomem.server" or name.startswith("memtomem.server.")
)
assert not leaked, "transport modules leaked into the runtime import graph: " + repr(leaked)
"""


def _run(body: str) -> None:
    subprocess.run([sys.executable, "-c", body + _ASSERT_CLEAN], check=True)


def test_runtime_import_does_not_pull_in_the_mcp_server() -> None:
    _run("import memtomem.runtime\n")


def test_runtime_factory_names_resolve_without_the_mcp_server() -> None:
    _run(
        "from memtomem.runtime import Components, TeardownResult, "
        "create_components, close_components\n"
    )


def test_components_can_be_built_and_closed_without_the_mcp_server(tmp_path) -> None:
    """The full build/teardown cycle, not just the import.

    A lazy ``import memtomem.server`` inside ``create_components`` would pass
    the import-only checks above, so exercise the real path against an
    isolated on-disk store with embeddings disabled.
    """
    db_path = tmp_path / "hygiene.db"
    _run(
        f"""
import asyncio

from memtomem.config import Mem2MemConfig
from memtomem.runtime import close_components, create_components

config = Mem2MemConfig()
config.storage.sqlite_path = {str(db_path)!r}
config.embedding.provider = "none"
config.embedding.dimension = 0

async def main():
    comp = await create_components(config, load_ambient_config=False)
    try:
        assert comp.search_pipeline is not None
        assert comp.index_engine is not None
    finally:
        await close_components(comp)

asyncio.run(main())
"""
    )
