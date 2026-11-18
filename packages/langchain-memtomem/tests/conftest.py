"""Every test runs under a scratch HOME with no ``MEMTOMEM_*`` variables.

The store reads the developer's project registry from ``~/.memtomem`` on
purpose (ADR-0042 B7); a test that wants a registered project writes that
registry itself.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from langchain_memtomem import MemtomemPipelineStore


@pytest.fixture(autouse=True)
def isolated_home(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Path:
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in list(os.environ):
        if name.startswith("MEMTOMEM_"):
            monkeypatch.delenv(name)
    return home


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "app-store"


@pytest.fixture
async def store(root: Path) -> AsyncIterator[MemtomemPipelineStore]:
    opened = await MemtomemPipelineStore.open(root)
    try:
        yield opened
    finally:
        await opened.aclose()


@pytest.fixture
def long_text():
    """A value text the chunker splits into several chunks."""

    def build(seed: str, sections: int = 6) -> str:
        filler = " ".join(f"{seed} filler word {i}" for i in range(60))
        return "\n\n".join(f"## {seed} section {n}\n\n{filler}" for n in range(sections))

    return build
