"""Fake Hermes modules for exercising packages/memtomem-hermes-memory without Hermes installed.

Shared by the unit tests (packages/memtomem/tests/test_hermes_memory_provider.py) and the
Python 3.11 smoke (tools/hermes_memory_py311_smoke.py), so both drive the provider through the
same surface. Standard library only, Python 3.11 compatible.

The ``agent.memory_provider`` part is a reduced copy of Hermes Agent v0.21.5 (tag v2026.9.24,
commit f97608f1) ``agent/memory_provider.py``, sha256
a4e44a293013fff831ac136ab77c556541cd18817f53bd662e97090cd80bd5a6, MIT License, Copyright (c)
Nous Research. Kept: ``MemoryProvider``'s public methods, ``RecallStatus``,
``TRIVIAL_PROMPT_RE`` / ``is_trivial_prompt`` and ``spawn_context_thread``. The opt-in
``hermes`` contract tests compare it with the real module.
"""

from __future__ import annotations

import contextvars
import re
import threading
import types
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

# --- agent.memory_provider (reduced copy, see the module docstring) ---------------------------


def _ctx_bound(fn: Callable[..., Any]) -> Callable[..., Any]:
    ctx = contextvars.copy_context()
    return lambda *args, **kwargs: ctx.run(fn, *args, **kwargs)


def spawn_context_thread(
    target: Callable[..., Any],
    *,
    name: str,
    daemon: bool = True,
    args: tuple = (),
    kwargs: Optional[Dict[str, Any]] = None,
) -> threading.Thread:
    return threading.Thread(
        target=_ctx_bound(target), args=args, kwargs=kwargs, name=name, daemon=daemon
    )


INDICATOR_GLYPH = "🧠"


@dataclass(frozen=True)
class RecallStatus:
    provider_label: str
    count: int
    glyph: str = INDICATOR_GLYPH


TRIVIAL_PROMPT_RE = re.compile(
    r"^(yes|no|ok|okay|sure|thanks|thank you|y|n|yep|nope|yeah|nah|"
    r"hi|hey|hello|yo|sup|"
    r"continue|go ahead|do it|proceed|got it|cool|nice|great|done|next|lgtm|k)"
    r'[\s!?.:;,"'
    + "'"
    + r"~\u2018\u2019\u201c\u201d\u2014\u2013\u2026()\[\]{}<>*&^%$#@!+=`\u00a0]*$",
    re.IGNORECASE,
)


def is_trivial_prompt(text: Optional[str]) -> bool:
    stripped = (text or "").strip()
    if not stripped or stripped.startswith("/"):
        return True
    return bool(TRIVIAL_PROMPT_RE.match(stripped))


class MemoryProvider(ABC):
    pre_compress_checkpoint_api_version = 1

    @property
    @abstractmethod
    def name(self) -> str: ...

    @abstractmethod
    def is_available(self) -> bool: ...

    @abstractmethod
    def initialize(self, session_id: str, **kwargs: Any) -> None: ...

    def unavailable_reason(self) -> str:
        return ""

    def system_prompt_block(self) -> str:
        return ""

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        return None

    def recall_status(self) -> Optional[RecallStatus]:
        return None

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
        turn_author: Optional[Dict[str, Any]] = None,
    ) -> None:
        return None

    @abstractmethod
    def get_tool_schemas(self) -> List[Dict[str, Any]]: ...

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        raise NotImplementedError(f"Provider {self.name} does not handle tool {tool_name}")

    def shutdown(self) -> None:
        return None

    def on_turn_start(self, turn_number: int, message: str, **kwargs: Any) -> None:
        return None

    def identity_signature(self) -> Dict[str, Any]:
        return {}

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        return None

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        return None

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        return ""

    def on_delegation(
        self, task: str, result: str, *, child_session_id: str = "", **kwargs: Any
    ) -> None:
        return None

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return []

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        return None

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        return None

    def backup_paths(self) -> List[str]:
        return []


# --- scripted hermes_cli.plugins / hermes_cli.config ------------------------------------------


def ok_envelope(
    results: Optional[List[Dict[str, Any]]] = None,
    *,
    recorded: Any = False,
    hints: Optional[List[str]] = None,
    as_text: bool = True,
) -> Dict[str, Any]:
    """What ``PluginContext.call_mcp`` returns for a structured ``mem_search``.

    ``recorded=None`` omits the key (a server older than the contract echo).
    """
    import json

    payload: Dict[str, Any] = {"results": list(results or [])}
    if hints:
        payload["hints"] = list(hints)
    if recorded is not None:
        payload["recorded"] = recorded
    return {"ok": True, "result": json.dumps(payload) if as_text else payload}


def chunk(n: int, *, content: str = "", source: str = "notes.md", hierarchy: str = "") -> dict:
    return {
        "chunk_id": f"c{n}",
        "source": source,
        "hierarchy": hierarchy,
        "content": content or f"fact number {n}",
    }


PROFILE: contextvars.ContextVar[str] = contextvars.ContextVar("fake_hermes_profile", default="")


@dataclass
class Call:
    plugin_id: str
    server: str
    tool: str
    arguments: Dict[str, Any]
    timeout: float
    thread: str


class FakeHermes:
    """The state the fake modules read. Tests mutate it between turns."""

    def __init__(self) -> None:
        self.config: Dict[str, Any] = default_config()
        # Hermes scopes config to the calling thread's profile through contextvars; a key here
        # overrides ``config`` while ``PROFILE`` holds that name (workers inherit it).
        self.profiles: Dict[str, Dict[str, Any]] = {}
        self.config_gate: Optional[threading.Event] = None
        self.config_error: Optional[BaseException] = None
        self.config_reads = 0
        self.portable: Any = {}
        self.calls: List[Call] = []
        self.calls_lock = threading.Lock()
        # Each call pops the next responder; when empty, ``default`` answers.
        self.responders: List[Callable[[Call], Dict[str, Any]]] = []
        self.default: Callable[[Call], Dict[str, Any]] = lambda call: ok_envelope([chunk(1)])
        self.call_gate: Optional[threading.Event] = None
        self.call_started = threading.Event()
        self.contexts_built = 0
        # When set, get_plugin_manager() waits on it (Hermes takes a lock there).
        self.manager_gate: Optional[threading.Event] = None

    # helpers for tests -------------------------------------------------------------------------

    def respond(self, *responders: Any) -> None:
        """Queue answers: an envelope dict, an exception instance, or a callable(Call)."""
        for item in responders:
            if callable(item) and not isinstance(item, BaseException):
                self.responders.append(item)
            else:
                self.responders.append(lambda call, item=item: _raise_or_return(item))

    def block_calls(self) -> threading.Event:
        """Every call waits on the returned event before answering."""
        self.call_gate = threading.Event()
        return self.call_gate

    # what the fake modules call -----------------------------------------------------------------

    def _load_config_readonly(self) -> Dict[str, Any]:
        self.config_reads += 1
        if self.config_gate is not None:
            self.config_gate.wait(30)
        if self.config_error is not None:
            raise self.config_error
        return self.profiles.get(PROFILE.get(), self.config)

    def _call_mcp(
        self,
        plugin_id: str,
        server: str,
        tool: str,
        arguments: Optional[Dict[str, Any]],
        timeout: float,
    ) -> Dict[str, Any]:
        call = Call(
            plugin_id, server, tool, dict(arguments or {}), timeout, threading.current_thread().name
        )
        with self.calls_lock:
            self.calls.append(call)
            responder = self.responders.pop(0) if self.responders else self.default
        self.call_started.set()
        if self.call_gate is not None:
            self.call_gate.wait(30)
        return responder(call)


def _raise_or_return(item: Any) -> Dict[str, Any]:
    if isinstance(item, BaseException):
        raise item
    return item


def default_config(server: str = "memtomem") -> Dict[str, Any]:
    """A profile where recall is allowed: a native entry and the allowlist grant."""
    return {
        "mcp_servers": {server: {"command": "memtomem-server"}},
        "plugins": {"entries": {"memtomem-memory": {"mcp_allowlist": [server]}}},
    }


def build_modules(state: FakeHermes) -> Dict[str, types.ModuleType]:
    """Fresh fake ``agent``, ``agent.memory_provider``, ``hermes_cli``, ``hermes_cli.plugins``
    and ``hermes_cli.config`` modules bound to *state*."""
    agent = types.ModuleType("agent")
    agent.__path__ = []
    memory_provider = types.ModuleType("agent.memory_provider")
    for name in (
        "MemoryProvider",
        "RecallStatus",
        "INDICATOR_GLYPH",
        "TRIVIAL_PROMPT_RE",
        "is_trivial_prompt",
        "spawn_context_thread",
    ):
        setattr(memory_provider, name, globals()[name])
    agent.memory_provider = memory_provider

    hermes_cli = types.ModuleType("hermes_cli")
    hermes_cli.__path__ = []
    plugins = types.ModuleType("hermes_cli.plugins")
    config = types.ModuleType("hermes_cli.config")

    @dataclass
    class PluginManifest:
        name: str
        version: str = ""
        key: str = ""

    class PluginManager:
        def get_portable_mcp_servers(self) -> Dict[str, Dict[str, Any]]:
            if isinstance(state.portable, BaseException):
                raise state.portable
            return {name: dict(cfg) for name, cfg in state.portable.items()}

    manager = PluginManager()

    class PluginContext:
        def __init__(self, manifest: PluginManifest, mgr: PluginManager) -> None:
            state.contexts_built += 1
            self.manifest = manifest
            self._manager = mgr

        @property
        def plugin_id(self) -> str:
            return self.manifest.key or self.manifest.name

        def call_mcp(
            self,
            server: str,
            tool: str,
            arguments: Optional[Dict[str, Any]] = None,
            timeout: float = 30,
        ) -> Dict[str, Any]:
            return state._call_mcp(self.plugin_id, server, tool, arguments, timeout)

    plugins.PluginManifest = PluginManifest
    plugins.PluginContext = PluginContext

    def get_plugin_manager() -> PluginManager:
        if state.manager_gate is not None:
            state.manager_gate.wait(30)
        return manager

    plugins.get_plugin_manager = get_plugin_manager
    config.load_config_readonly = state._load_config_readonly
    hermes_cli.plugins = plugins
    hermes_cli.config = config
    return {
        "agent": agent,
        "agent.memory_provider": memory_provider,
        "hermes_cli": hermes_cli,
        "hermes_cli.plugins": plugins,
        "hermes_cli.config": config,
    }


def install(setitem: Callable[[Any, str, Any], None], state: FakeHermes) -> None:
    """Install the fake modules into ``sys.modules`` through *setitem*
    (``monkeypatch.setitem`` in tests, so they are removed afterwards)."""
    import sys

    for name, module in build_modules(state).items():
        setitem(sys.modules, name, module)


class Collector:
    """Stands in for Hermes's ``_ProviderCollector``: forwards only ``register_*``."""

    def __init__(self) -> None:
        self.provider: Any = None

    def register_memory_provider(self, provider: Any) -> None:
        self.provider = provider

    def __getattr__(self, name: str) -> Any:
        raise AttributeError(name)
