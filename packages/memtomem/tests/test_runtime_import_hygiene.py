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

import os
import subprocess
import sys
from pathlib import Path

_TRANSPORT_ROOTS = ("mcp", "memtomem.server", "memtomem.web")

_ASSERT_CLEAN = f"""
import sys

roots = {_TRANSPORT_ROOTS!r}
leaked = sorted(
    name
    for name in sys.modules
    if any(name == root or name.startswith(root + ".") for root in roots)
)
assert not leaked, "transport modules leaked into the runtime import graph: " + repr(leaked)
"""


def _run(body: str, *, home: Path | None = None) -> None:
    env = None
    if home is not None:
        # Isolate ambient config: MemtomemStore resolves ``config.d`` and
        # persisted overrides from the real home directory otherwise.
        env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home)}
    subprocess.run([sys.executable, "-c", body + _ASSERT_CLEAN], check=True, env=env)


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


def test_memtomem_store_init_and_close_stay_off_the_transport_stack(tmp_path) -> None:
    """The named consumer, not just the layer it is supposed to use.

    The checks above import ``memtomem.runtime`` directly, so they would stay
    green if ``MemtomemStore`` quietly went back to
    ``memtomem.server.component_factory`` — the regression this whole move
    exists to prevent. Drive the adapter itself instead.

    Scope note: only the lazy-init and close path is asserted. ``search`` is
    not clean yet — it lazy-imports ``_resolve_project_context_root`` from
    ``memtomem.server.tools.search``, the last core→server edge in this
    adapter. Extend this case to cover ``search``/``add`` in the change that
    relocates that helper.
    """
    home = tmp_path / "home"
    home.mkdir()
    db_path = tmp_path / "store.db"
    _run(
        f"""
import asyncio

from memtomem.integrations.langgraph import MemtomemStore

store = MemtomemStore(
    config_overrides={{
        "storage": {{"sqlite_path": {str(db_path)!r}}},
        "embedding": {{"provider": "none", "dimension": 0}},
    }}
)

async def main():
    comp = await store._ensure_init()
    assert comp.search_pipeline is not None
    await store.close()
    assert store._components is None

asyncio.run(main())
""",
        home=home,
    )
