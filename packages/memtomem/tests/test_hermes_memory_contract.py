"""The memtomem-memory provider against a real Hermes Agent checkout (opt-in).

Run with ``HERMES_SRC=<checkout of Hermes Agent v2026.9.24> uv run pytest -m hermes``; without
it these tests are skipped (PyPI's ``hermes-agent`` stops at 0.19.0, so CI cannot install the
version the provider targets). Hermes's own loader imports the package directory, its
``MemoryManager`` drives the provider, and only ``PluginContext.call_mcp`` is scripted.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.hermes

_ROOT = Path(__file__).resolve().parents[3]
_PACKAGE = _ROOT / "packages/memtomem-hermes-memory"
_FAKES = _ROOT / "tools/hermes_memory_fakes.py"
_REGISTRY = "_memtomem_memory_admission"


@pytest.fixture(autouse=True)
def hermes_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Every test imports from the checkout, whichever test runs first or alone."""
    monkeypatch.syspath_prepend(os.environ["HERMES_SRC"])
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))


@pytest.fixture
def real_hermes(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Any:
    home = tmp_path / "hermes-home"
    home.mkdir()
    (home / "config.yaml").write_text(
        json.dumps(  # JSON is YAML
            {
                "mcp_servers": {"memtomem": {"command": "memtomem-server"}},
                "plugins": {"entries": {"memtomem-memory": {"mcp_allowlist": ["memtomem"]}}},
            }
        ),
        encoding="utf-8",
    )
    before = set(sys.modules)
    monkeypatch.delitem(sys.modules, _REGISTRY, raising=False)
    yield home
    for name in set(sys.modules) - before:
        if name.startswith("_hermes_user_memory") or name == _REGISTRY:
            sys.modules.pop(name, None)


@pytest.fixture
def scripted_call_mcp(monkeypatch: pytest.MonkeyPatch, real_hermes: Path) -> Any:
    from hermes_cli import plugins

    class Script:
        gate: threading.Event | None = None
        calls: list = []

        @staticmethod
        def answer(server: str, tool: str, arguments: dict) -> dict:
            payload = {
                "results": [
                    {
                        "chunk_id": "c1",
                        "source": "decisions.md",
                        "hierarchy": "Storage",
                        "content": "We keep SQLite for the index.",
                    }
                ],
                "recorded": False,
            }
            return {"ok": True, "result": json.dumps(payload)}

    def call_mcp(self: Any, server: str, tool: str, arguments: Any = None, timeout: float = 30):
        Script.calls.append((self.plugin_id, server, tool, dict(arguments or {}), timeout))
        if Script.gate is not None:
            Script.gate.wait(30)
        return Script.answer(server, tool, dict(arguments or {}))

    Script.calls = []
    monkeypatch.setattr(plugins.PluginContext, "call_mcp", call_mcp)
    yield Script
    if Script.gate is not None:
        Script.gate.set()


def _load_through_hermes() -> Any:
    from plugins.memory import _load_provider_from_dir

    provider = _load_provider_from_dir(_PACKAGE, register_skills=False)
    assert provider is not None, "Hermes's loader found no provider in the package"
    return provider


def _fakes() -> Any:
    spec = importlib.util.spec_from_file_location("_hermes_memory_fakes_contract", _FAKES)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return module


def test_hermes_loads_the_package_and_the_manager_drives_it(scripted_call_mcp: Any) -> None:
    from agent.memory_manager import MemoryManager, build_memory_context_block

    provider = _load_through_hermes()
    assert provider.name == "memtomem-memory"
    assert provider.is_available(), provider.unavailable_reason()
    manager = MemoryManager()
    manager.add_provider(provider)
    manager.initialize_all("session-1", hermes_home=os.environ["HERMES_HOME"], platform="cli")

    text = manager.prefetch_all("which database do we keep for the index?")
    assert text.startswith("Recalled from memtomem (shared long-term memory; reference data):")
    assert "- [decisions.md › Storage] We keep SQLite for the index." in text
    block = build_memory_context_block(text)
    assert block.startswith("<memory-context>") and "We keep SQLite" in block
    assert manager.describe_recall().endswith("memtomem — recalled 1 memory")

    [(plugin_id, server, tool, arguments, timeout)] = scripted_call_mcp.calls
    assert (plugin_id, server, tool, timeout) == ("memtomem-memory", "memtomem", "mem_search", 10.0)
    assert arguments["record"] is False and arguments["rerank"] is False

    assert manager.prefetch_all("ok") == ""
    assert manager.describe_recall() == ""
    manager.on_session_end([])
    manager.shutdown_all()
    assert manager.prefetch_all("which database do we keep for the index?") == ""
    assert len(scripted_call_mcp.calls) == 1


def test_a_blocked_call_never_reaches_the_core_join(scripted_call_mcp: Any) -> None:
    from agent.memory_manager import MemoryManager

    scripted_call_mcp.gate = threading.Event()
    provider = _load_through_hermes()
    manager = MemoryManager()
    manager.add_provider(provider)
    manager.initialize_all("session-1", hermes_home=os.environ["HERMES_HOME"], platform="cli")
    for _ in range(3):
        started = time.perf_counter()
        assert manager.prefetch_all("which database do we keep for the index?") == ""
        assert time.perf_counter() - started < 1.0
        # The core's prefetch thread for the provider ended: Hermes does not skip it next turn.
        thread = manager._external_prefetch_threads.get("memtomem-memory")
        assert thread is None or not thread.is_alive()
    assert len(scripted_call_mcp.calls) == 1
    scripted_call_mcp.gate.set()
    manager.shutdown_all()


def _shape_of(fn: Any) -> list[tuple[str, Any, Any]]:
    """Parameter names, kinds and defaults; annotations differ in spelling only."""
    return [(p.name, p.kind, p.default) for p in inspect.signature(fn).parameters.values()]


def test_the_fake_abc_matches_hermes() -> None:
    from agent import memory_provider as real

    fake = _fakes()
    real_names = {n for n in dir(real.MemoryProvider) if not n.startswith("_")}
    assert {n for n in dir(fake.MemoryProvider) if not n.startswith("_")} == real_names
    for name in sorted(real_names):
        real_attr = inspect.getattr_static(real.MemoryProvider, name)
        fake_attr = inspect.getattr_static(fake.MemoryProvider, name)
        if callable(real_attr):
            assert _shape_of(fake_attr) == _shape_of(real_attr), name
    assert fake.MemoryProvider.__abstractmethods__ == real.MemoryProvider.__abstractmethods__
    assert fake.TRIVIAL_PROMPT_RE.pattern == real.TRIVIAL_PROMPT_RE.pattern
    assert fake.TRIVIAL_PROMPT_RE.flags == real.TRIVIAL_PROMPT_RE.flags
    assert fake.INDICATOR_GLYPH == real.INDICATOR_GLYPH
    assert list(fake.RecallStatus.__dataclass_fields__) == list(
        real.RecallStatus.__dataclass_fields__
    )
    assert _shape_of(fake.spawn_context_thread) == _shape_of(real.spawn_context_thread)


@pytest.mark.parametrize(
    "prompt",
    ["", "   ", "ok", "OK!", "thanks :)", "/new", "go ahead", "k8s", "yolo", "note", "hi there"],
)
def test_the_fake_trivial_prompt_check_agrees(prompt: str) -> None:
    from agent import memory_provider as real

    assert _fakes().is_trivial_prompt(prompt) is real.is_trivial_prompt(prompt)


def test_hermes_semantics_the_provider_replicates(real_hermes: Path) -> None:
    from tools.mcp_tool_common import _parse_boolish
    from tools.mcp_tool_registration import _normalize_server_trust

    provider = _load_through_hermes()
    module = sys.modules[type(provider).__module__]
    for value in (None, True, False, 0, 1, 0.0, 2.5, "on", "OFF", " yes ", "0", "maybe", [], {}):
        assert module._parse_boolish(value) is _parse_boolish(value), value
    for value in (None, "full", " FULL ", "untrusted", " UNTRUSTED ", "typo", 1, False):
        assert module._normalize_server_trust(value) == _normalize_server_trust(value), value
