"""The tools await ``webhook_manager.fire`` — they do not detach it (#2185).

Each call site used to wrap the already-async ``fire`` in an outer
``asyncio.create_task`` and attach a done-callback, which is not a strong
reference. Worse, the lifespan closes the webhook manager before storage, so an
outer task still queued at shutdown could call ``fire`` after ``close()`` had
cleared ``_pending_tasks`` — spawning a send nothing tracks and an
``AsyncClient`` nobody closes.

``fire`` only builds the request and hands it to a task the manager itself
tracks, so awaiting it costs no network wait. That is what these pin, and they
pin it two ways: ``mem_ask`` behaviourally, with no yield for a detached task to
sneak through, and every call site structurally, because a behavioural harness
for ``mem_add``'s write path would cost more than the one line it protects.
"""

from __future__ import annotations

import ast
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import memtomem
from memtomem.models import Chunk, ChunkMetadata, SearchResult
from memtomem.search.pipeline import RetrievalStats

_TOOLS = Path(memtomem.__file__).parent / "server" / "tools"

#: The modules that fire webhooks, as paths relative to ``server/tools/``.
#: Named rather than globbed so a new firing module has to be added here
#: deliberately — and ``test_every_firing_module_is_listed`` fails if one
#: appears that is not. Relative paths rather than basenames: the directory is
#: flat today, but a basename registry would collapse two same-named modules in
#: different subpackages into one entry and stop being an enumeration.
_FIRING_MODULES = ("ask.py", "memory_crud.py", "search.py")


def _result(content: str = "a memory") -> SearchResult:
    from uuid import uuid4

    chunk = Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=Path("note.md")),
        id=uuid4(),
        embedding=[],
    )
    return SearchResult(chunk=chunk, score=0.9, rank=1, source="fused")


@pytest.mark.asyncio
async def test_mem_ask_awaits_the_webhook_before_returning(monkeypatch):
    """No ``sleep(0)`` here on purpose — a detached task would not have run."""
    from memtomem.server.tools import ask as ask_mod

    app = MagicMock()
    app.current_namespace = None
    app.webhook_manager.fire = AsyncMock(return_value=None)
    app.search_pipeline.search = AsyncMock(
        return_value=([_result(), _result("another")], RetrievalStats(final_total=2))
    )

    monkeypatch.setattr(ask_mod, "_get_app_initialized", AsyncMock(return_value=app))
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root", lambda _app: None
    )

    await ask_mod.mem_ask("what did we decide?", ctx=None)

    app.webhook_manager.fire.assert_awaited_once_with(
        "ask", {"question": "what did we decide?", "context_chunks": 2}
    )


@pytest.mark.asyncio
async def test_mem_ask_without_a_webhook_manager_is_not_an_error(monkeypatch):
    from memtomem.server.tools import ask as ask_mod

    app = MagicMock()
    app.current_namespace = None
    app.webhook_manager = None
    app.search_pipeline.search = AsyncMock(
        return_value=([_result()], RetrievalStats(final_total=1))
    )
    monkeypatch.setattr(ask_mod, "_get_app_initialized", AsyncMock(return_value=app))
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root", lambda _app: None
    )

    out = await ask_mod.mem_ask("still fine?", ctx=None)
    assert "still fine?" in out


def _fire_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "fire"
        and (chain := node.func.value) is not None
        and isinstance(chain, ast.Attribute)
        and chain.attr == "webhook_manager"
    ]


def _awaited_calls(tree: ast.Module) -> set[int]:
    return {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call)
    }


@pytest.mark.parametrize("module", _FIRING_MODULES)
def test_every_fire_call_site_is_directly_awaited(module):
    """``create_task(fire(...))`` reintroduces both the leak and the race."""
    tree = ast.parse((_TOOLS / module).read_text(encoding="utf-8"))
    calls = _fire_calls(tree)
    assert calls, f"{module} no longer fires a webhook — update _FIRING_MODULES"

    awaited = _awaited_calls(tree)
    detached = [call.lineno for call in calls if id(call) not in awaited]
    assert not detached, f"{module}: fire() not awaited at line(s) {detached}"


def test_every_firing_module_is_listed():
    """A new firing module must join the list above, not slip past it."""
    firing = {
        path.relative_to(_TOOLS).as_posix()
        for path in sorted(_TOOLS.rglob("*.py"))
        if _fire_calls(ast.parse(path.read_text(encoding="utf-8")))
    }
    assert firing == set(_FIRING_MODULES), firing
