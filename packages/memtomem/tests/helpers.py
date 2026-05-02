"""Shared test helpers for memtomem tests."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.models import Chunk, ChunkMetadata
from memtomem.server.context import AppContext

# Developer ``MEMTOMEM_*`` env vars that would override an in-test config
# and break hermeticity. Add new top-level config sections here when they
# grow an env-var binding.
_MEMTOMEM_ENV_VARS = (
    "MEMTOMEM_EMBEDDING__PROVIDER",
    "MEMTOMEM_EMBEDDING__MODEL",
    "MEMTOMEM_EMBEDDING__DIMENSION",
    "MEMTOMEM_STORAGE__SQLITE_PATH",
    "MEMTOMEM_INDEXING__MEMORY_DIRS",
    "MEMTOMEM_SCHEDULER__ENABLED",
)


def isolate_memtomem_env(monkeypatch) -> None:
    """Strip ``MEMTOMEM_*`` env vars and stub out ``load_config_overrides``
    so a freshly constructed ``Mem2MemConfig`` is not mutated by the
    developer's ``~/.memtomem/config.json`` or shell environment.

    Used directly by tests that construct their own components (e.g. the
    LangGraph adapter cases). The ``bm25_only_components`` fixture in
    ``conftest.py`` calls this internally for fixture-based callers.
    """
    for var in _MEMTOMEM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)

    import memtomem.config as _cfg

    monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)


def set_home(monkeypatch: pytest.MonkeyPatch, path: Path | str) -> None:
    """Override the home directory for tests that exercise ``Path.home()``
    or ``Path("~/...").expanduser()``.

    On POSIX, ``Path.home()`` reads ``HOME``. On Windows it reads
    ``USERPROFILE`` first (then ``HOMEDRIVE``+``HOMEPATH``), so a bare
    ``monkeypatch.setenv("HOME", ...)`` is silently ignored on Windows
    runners and tests end up reading/writing the real user home. Setting
    both env vars is harmless on POSIX (stdlib ignores ``USERPROFILE``)
    and correct on Windows.

    A handful of pre-existing call sites still set ``HOME`` and
    ``USERPROFILE`` by hand (``test_context_settings.py``,
    ``test_web_routes.py``, ``test_web_routes_extended.py``,
    ``test_context_agents.py``, ``test_server_tools_context_settings_gate.py``);
    they should migrate to ``set_home`` in the follow-up that sweeps the
    remaining ~130 ``monkeypatch.setenv("HOME", ...)`` sites.
    """
    monkeypatch.setenv("HOME", str(path))
    monkeypatch.setenv("USERPROFILE", str(path))


class StubCtx:
    """Minimal stand-in for MCP ``Context`` so MCP tools can be invoked
    directly from tests without a live FastMCP session.
    """

    def __init__(self, app: AppContext) -> None:
        class _RC:
            pass

        self.request_context = _RC()
        self.request_context.lifespan_context = app


def make_chunk(
    content: str = "test content",
    tags: tuple[str, ...] = (),
    namespace: str = "default",
    source: str = "test.md",
    heading: tuple[str, ...] = (),
    embedding: list[float] | None = None,
) -> Chunk:
    """Create a test Chunk with sensible defaults."""
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(f"/tmp/{source}"),
            tags=tuple(tags),
            namespace=namespace,
            heading_hierarchy=tuple(heading),
        ),
        content_hash=f"hash-{uuid4().hex[:8]}",
        embedding=embedding if embedding is not None else [0.1] * 1024,
    )
