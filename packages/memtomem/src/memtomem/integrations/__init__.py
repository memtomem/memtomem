"""Framework integrations for memtomem.

Adapters that let a host process — a LangGraph or deepagents agent, a
notebook, any embedder — drive memtomem in-process. They build components
through :mod:`memtomem.runtime` and never import the MCP server or the web
stack; ``tests/test_runtime_import_hygiene.py`` enforces that.

Both names resolve lazily so importing this package stays cheap and does not
require optional dependencies:

- :class:`~memtomem.integrations.langgraph.MemtomemStore` needs nothing beyond
  memtomem itself. It reads and writes the real long-term memory through the
  search pipeline, so its ``agent-runtime:<id>`` / ``shared`` namespaces are
  the same ones the ``mem_*`` tools and ``mm`` see.
- :class:`~memtomem.integrations.langgraph_store.MemtomemBaseStore` implements
  LangGraph's tuple-namespace ``BaseStore`` (so it drops into ``create_agent``
  or a deepagents ``StoreBackend``) and requires ``memtomem[langgraph]``. It
  keeps its records as inspectable JSON under its own root — a separate corpus
  from the markdown memories ``MemtomemStore`` searches.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Re-exported for type checkers, which cannot follow ``__getattr__``. The
    # redundant aliases mark them as intentional re-exports; ``MemtomemBaseStore``
    # needs one because it is deliberately out of ``__all__`` (see below).
    from memtomem.integrations.langgraph import MemtomemStore as MemtomemStore
    from memtomem.integrations.langgraph_store import MemtomemBaseStore as MemtomemBaseStore

# ``MemtomemBaseStore`` is deliberately absent: ``__all__`` drives
# ``from memtomem.integrations import *``, which would then import the
# BaseStore adapter and fail on a minimal install. It stays importable by
# name — ``from memtomem.integrations import MemtomemBaseStore`` — which is
# the honest place for the optional dependency to surface.
__all__ = ["MemtomemStore"]

_LAZY = {
    "MemtomemStore": "memtomem.integrations.langgraph",
    "MemtomemBaseStore": "memtomem.integrations.langgraph_store",
}


def __getattr__(name: str) -> Any:
    module_path = _LAZY.get(name)
    if module_path is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    return getattr(import_module(module_path), name)


def __dir__() -> list[str]:
    # Advertise the optional adapter too, even though it is out of ``__all__``:
    # discoverability should not depend on whether star-import is safe.
    return sorted({*globals(), *__all__, *_LAZY})
