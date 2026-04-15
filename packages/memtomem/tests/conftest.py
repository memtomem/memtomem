"""Shared fixtures for memtomem tests."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure tests/ directory is importable for helpers.py
sys.path.insert(0, str(Path(__file__).parent))

import httpx
import pytest

from memtomem.config import Mem2MemConfig
from memtomem.server.component_factory import Components, create_components, close_components


def _ollama_available() -> bool:
    """Check if Ollama is reachable at localhost:11434."""
    try:
        r = httpx.get("http://localhost:11434/api/tags", timeout=2)
        return r.status_code == 200
    except Exception:
        return False


_OLLAMA_UP = _ollama_available()


def pytest_collection_modifyitems(config, items):
    """Auto-skip tests marked with @pytest.mark.ollama when Ollama is down."""
    if _OLLAMA_UP:
        return
    skip = pytest.mark.skip(reason="Ollama not running")
    for item in items:
        if "ollama" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
async def components(tmp_path):
    """Create components with a temporary DB for isolated testing."""
    import json
    import os

    db_path = str(tmp_path / "test.db")
    mem_dir = str(tmp_path / "memories")
    (tmp_path / "memories").mkdir()

    os.environ["MEMTOMEM_STORAGE__SQLITE_PATH"] = db_path
    os.environ["MEMTOMEM_INDEXING__MEMORY_DIRS"] = json.dumps([mem_dir])
    os.environ["MEMTOMEM_EMBEDDING__MODEL"] = "bge-m3"
    os.environ["MEMTOMEM_EMBEDDING__DIMENSION"] = "1024"

    # Prevent ~/.memtomem/config.json from overriding test settings
    config = Mem2MemConfig()
    # Apply env vars directly (bypass load_config_overrides)
    config.storage.sqlite_path = Path(db_path)
    config.embedding.model = "bge-m3"
    config.embedding.dimension = 1024
    config.indexing.memory_dirs = [Path(mem_dir)]

    # Monkey-patch to skip config.json loading
    import memtomem.config as _cfg
    _orig = _cfg.load_config_overrides
    _cfg.load_config_overrides = lambda c: None
    comp = await create_components(config)
    _cfg.load_config_overrides = _orig
    yield comp
    await close_components(comp)

    for key in ("MEMTOMEM_STORAGE__SQLITE_PATH", "MEMTOMEM_INDEXING__MEMORY_DIRS",
                "MEMTOMEM_EMBEDDING__MODEL", "MEMTOMEM_EMBEDDING__DIMENSION"):
        os.environ.pop(key, None)


@pytest.fixture
def storage(components: Components):
    return components.storage


@pytest.fixture
def pipeline(components: Components):
    return components.search_pipeline


@pytest.fixture
def engine(components: Components):
    return components.index_engine


@pytest.fixture
def memory_dir(components: Components):
    return Path(components.config.indexing.memory_dirs[0]).expanduser().resolve()
