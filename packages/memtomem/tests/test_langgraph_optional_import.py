"""Minimal-install contract for the dependency-free LangGraph adapter."""

from __future__ import annotations

import subprocess
import sys

_BLOCK_LANGGRAPH = r"""
import builtins

real_import = builtins.__import__

def blocked_import(name, *args, **kwargs):
    if name == "langgraph" or name.startswith("langgraph."):
        raise ImportError("langgraph intentionally unavailable")
    return real_import(name, *args, **kwargs)

builtins.__import__ = blocked_import
"""


def _run_without_langgraph(body: str) -> None:
    subprocess.run([sys.executable, "-c", _BLOCK_LANGGRAPH + body], check=True)


def test_memtomem_store_imports_without_langgraph() -> None:
    _run_without_langgraph(
        """
from memtomem.integrations.langgraph import MemtomemStore
assert MemtomemStore.__name__ == "MemtomemStore"
"""
    )


def test_package_level_export_does_not_pull_in_the_basestore_adapter() -> None:
    """The lazy re-export must not drag the optional adapter in with it.

    ``from memtomem.integrations import MemtomemStore`` is the documented
    entry point, so it has to hold on a minimal install. A plain eager
    ``from .langgraph_store import MemtomemBaseStore`` in the package
    ``__init__`` would satisfy the submodule test above and still break here.
    """
    _run_without_langgraph(
        """
import sys

from memtomem.integrations import MemtomemStore
assert MemtomemStore.__name__ == "MemtomemStore"
assert "memtomem.integrations.langgraph_store" not in sys.modules
"""
    )


def test_star_import_stays_safe_on_a_minimal_install() -> None:
    """``__all__`` drives star-import, so it must list only what always works."""
    _run_without_langgraph(
        """
ns = {}
exec("from memtomem.integrations import *", ns)
assert "MemtomemStore" in ns
assert "MemtomemBaseStore" not in ns
"""
    )


def test_basestore_is_discoverable_even_though_it_is_not_in_all() -> None:
    """Keeping it out of ``__all__`` must not make it invisible.

    Runs with langgraph available (no import block) so ``dir()`` reflects the
    normal install.
    """
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import memtomem.integrations as integrations

assert "MemtomemBaseStore" in dir(integrations)
assert "MemtomemStore" in dir(integrations)
""",
        ],
        check=True,
    )
