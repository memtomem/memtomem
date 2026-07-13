"""Minimal-install contract for the dependency-free LangGraph adapter."""

from __future__ import annotations

import subprocess
import sys


def test_memtomem_store_imports_without_langgraph() -> None:
    script = r"""
import builtins

real_import = builtins.__import__

def blocked_import(name, *args, **kwargs):
    if name == "langgraph" or name.startswith("langgraph."):
        raise ImportError("langgraph intentionally unavailable")
    return real_import(name, *args, **kwargs)

builtins.__import__ = blocked_import
from memtomem.integrations.langgraph import MemtomemStore
assert MemtomemStore.__name__ == "MemtomemStore"
"""
    subprocess.run([sys.executable, "-c", script], check=True)
