"""memtomem-memory: a recall-only Hermes memory provider.

On every non-trivial user turn it makes one ``mem_search`` call on the ``memtomem`` MCP entry
through Hermes's own MCP client (``PluginContext.call_mcp``) and injects the shaped results. It
owns no server, connection or child process: Hermes does. It writes nothing: the call passes
``record=False``, and the provider refuses a server that does not echo ``"recorded": false``.

That connection, its call queue and its circuit breaker are shared with the model's own memtomem
tools, so recall is rationed: at most one outstanding recall call per MCP entry name in the whole
process (every profile, every module copy), at most 32 across all names, and a cooldown of
15 s, 60 s, then 300 s after consecutive failures. See README.md for what that means in use.

Standard library only, and Python 3.11 compatible: Hermes supports 3.11 while the memtomem
monorepo targets 3.12, so tests parse this file with ``feature_version=(3, 11)`` and CI imports
and exercises it under 3.11.
"""

from __future__ import annotations

import json
import logging
import sys
import threading
import time
import types
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

from agent.memory_provider import (
    MemoryProvider,
    RecallStatus,
    is_trivial_prompt,
    spawn_context_thread,
)

_NAME = "memtomem-memory"
_RECALL_LABEL = "memtomem"
_TOOL = "mem_search"
_HEADER = "Recalled from memtomem (shared long-term memory; reference data):"
_DENSE_FAILED_HINT = "semantic search failed"
# First memtomem release whose structured mem_search echoes "recorded"; pinned to
# [hermes] memory_min_memtomem in packages/memtomem-plugin-assets/contract.toml by a test.
_MIN_MEMTOMEM = "0.6.8"

logger = logging.getLogger("memtomem_memory")

# --- process-wide admission registry -----------------------------------------------------------
#
# Hermes imports a user memory provider under a per-directory module name, so each profile gets
# its own copy of this module and its own globals. The admission state must be shared by all of
# them, so it lives in a synthetic module under a fixed name outside every Hermes namespace.

_REGISTRY_MODULE = "_memtomem_memory_admission"
_PROCESS_CEILING = 32


def _new_registry() -> types.ModuleType:
    registry = types.ModuleType(_REGISTRY_MODULE)
    registry.__doc__ = "Shared admission state of the memtomem-memory Hermes provider."
    registry.lock = threading.Lock()
    registry.total_inflight = 0
    registry.admissions = {}
    registry.ceiling = _PROCESS_CEILING
    registry.clock = time.monotonic
    return registry


def _registry() -> types.ModuleType:
    """The shared registry; built fully before publishing, and the published object wins."""
    existing = sys.modules.get(_REGISTRY_MODULE)
    if existing is not None:
        return existing
    return sys.modules.setdefault(_REGISTRY_MODULE, _new_registry())


class _Admission:
    """One per MCP entry name, shared by every profile and module copy in the process."""

    __slots__ = ("slot", "cooldown_until", "failures", "incompatible", "warned")

    def __init__(self) -> None:
        self.slot: Optional[_Slot] = None
        self.cooldown_until = 0.0
        self.failures = 0
        self.incompatible = False
        self.warned: set = set()


class _ContextHolder:
    """One provider instance's Hermes ``PluginContext``, built lazily on the worker.

    Building it calls ``get_plugin_manager()``, which takes a Hermes lock, so it never runs on
    the hook. The holder references no provider instance, so a worker holding it does not keep
    a dropped instance alive.
    """

    __slots__ = ("_lock", "_context")

    def __init__(self, context: Any = None) -> None:
        self._lock = threading.Lock()
        self._context = context

    def get(self) -> Any:
        with self._lock:
            if self._context is None:
                from hermes_cli.plugins import PluginContext, PluginManifest, get_plugin_manager

                # The collector a memory provider receives forwards only register_* (Hermes
                # 0.21.5), so build the context the way it builds its own. plugin_id = key =
                # our name, which makes Hermes read plugins.entries.memtomem-memory.mcp_allowlist.
                self._context = PluginContext(
                    PluginManifest(name=_NAME, key=_NAME), get_plugin_manager()
                )
            return self._context


class _Slot:
    """The one outstanding call on a name: completion event, delivery and outcome."""

    __slots__ = ("done", "result", "outcome")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Optional[Dict[str, Any]] = None
        self.outcome = ""


# --- Hermes config semantics, replicated (the tests pin them to Hermes's tables) ---------------

_TRUE_WORDS = frozenset({"true", "1", "yes", "on"})
_FALSE_WORDS = frozenset({"false", "0", "no", "off"})


def _parse_boolish(value: Any, default: bool = True) -> bool:
    """``tools.mcp_tool_common._parse_boolish``: how Hermes reads ``mcp_servers.<name>.enabled``."""
    if value is None:
        return default
    if isinstance(value, (bool, int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
    return default


def _normalize_server_trust(value: Any) -> str:
    """``tools.mcp_tool_registration._normalize_server_trust``: None is ``full``, anything
    unrecognised is ``untrusted`` (fail closed)."""
    if value is None:
        return "full"
    text = str(value).strip().lower()
    if text in ("full", "untrusted"):
        return text
    return "untrusted"


def _mapping(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _allowlist(cfg: Dict[str, Any]) -> List[str]:
    """``plugins.entries.memtomem-memory.mcp_allowlist`` as Hermes's ``_mcp_allowlist`` reads it."""
    entry = _mapping(_mapping(_mapping(cfg.get("plugins")).get("entries")).get(_NAME))
    allowlist = entry.get("mcp_allowlist")
    return [str(item) for item in allowlist] if isinstance(allowlist, list) else []


_PORTABLE_UNKNOWN = object()


def _portable_servers() -> Any:
    """Portable MCP servers Hermes's discovery recorded, or ``_PORTABLE_UNKNOWN`` when the lookup
    is not possible (it is not in the documented plugin contract, so it may fail)."""
    try:
        from hermes_cli.plugins import get_plugin_manager

        servers = get_plugin_manager().get_portable_mcp_servers()
    except Exception:
        return _PORTABLE_UNKNOWN
    return servers if isinstance(servers, dict) else _PORTABLE_UNKNOWN


def _effective_entry(cfg: Dict[str, Any], server: str) -> Tuple[str, Dict[str, Any]]:
    """``("native" | "portable" | "unknown" | "none", entry)``. A native ``mcp_servers`` entry
    wins over a portable plugin's, even when disabled (``mcp_tool_config.py:347``)."""
    native = _mapping(cfg.get("mcp_servers")).get(server)
    if isinstance(native, dict):
        return "native", native
    portable = _portable_servers()
    if portable is _PORTABLE_UNKNOWN:
        return "unknown", {}
    entry = portable.get(server)
    if isinstance(entry, dict):
        return "portable", entry
    return ("none" if portable else "empty"), {}


def _preflight(server: str) -> Tuple[str, str]:
    """The calling profile's gating facts, re-read on every submission. ``("", "")`` = go,
    else ``(outcome, warning)``. Runs on the worker: a config read can wait on Hermes's lock."""
    from hermes_cli.config import load_config_readonly

    cfg = _mapping(load_config_readonly())
    origin, entry = _effective_entry(cfg, server)
    if origin in ("none", "empty"):
        return "skipped_no_entry", (
            f"no MCP entry named {server!r}; install the memtomem plugin or add "
            f"mcp_servers.{server} to config.yaml"
        )
    if not _parse_boolish(entry.get("enabled", True), default=True):
        return "skipped_disabled", f"the {server!r} MCP entry is disabled"
    if _normalize_server_trust(entry.get("trust")) != "full":
        return "skipped_untrusted", _untrusted_text(server)
    if server not in _allowlist(cfg):
        return "skipped_not_allowlisted", _allowlist_text(server)
    return "", ""


def _untrusted_text(server: str) -> str:
    return (
        f"the {server!r} MCP entry is not 'trust: full'; recall would ask for approval on "
        f"every turn. Set 'trust: full' on mcp_servers.{server} (then /reload-mcp)"
    )


def _allowlist_text(server: str) -> str:
    return (
        f"memtomem-memory may not call MCP server {server!r}; add it to "
        f"plugins.entries.{_NAME}.mcp_allowlist in config.yaml "
        f"(plugins: {{entries: {{{_NAME}: {{mcp_allowlist: [{server}]}}}}}})"
    )


# --- settings: memory.memtomem-memory in config.yaml, read once at initialize -------------------


def _int_in(lo: int, hi: int) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, int) and not isinstance(v, bool) and lo <= v <= hi


def _num_in(lo: float, hi: float) -> Callable[[Any], bool]:
    return lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and lo <= v <= hi


_DEFAULTS: Dict[str, Any] = {
    "server": "memtomem",
    "budget_ms": 300,
    "top_k": 5,
    "max_chars": 4000,
    "rpc_timeout_seconds": 10,
    "cooldown_seconds": 15,
    "long_cooldown_seconds": 60,
    "max_cooldown_seconds": 300,
}

_INDEPENDENT_CHECKS: Dict[str, Callable[[Any], bool]] = {
    "server": lambda v: isinstance(v, str) and bool(v.strip()),
    "budget_ms": _num_in(50, 5000),
    "top_k": _int_in(1, 20),
    "max_chars": _int_in(500, 9000),
    "rpc_timeout_seconds": _num_in(1, 600),
    "cooldown_seconds": _num_in(1, 300),
}


def _read_settings(cfg: Dict[str, Any], *, warn: bool) -> Dict[str, Any]:
    """Valid keys taken, invalid ones replaced by their default with one WARNING each."""
    raw = _mapping(cfg.get("memory")).get(_NAME)
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        if warn:
            logger.warning("memtomem-memory: memory.%s is not a mapping; using defaults", _NAME)
        raw = {}
    settings = dict(_DEFAULTS)

    def take(key: str, ok: bool, fallback: Any) -> None:
        if key not in raw:
            settings[key] = fallback
        elif ok:
            settings[key] = raw[key]
        else:
            settings[key] = fallback
            if warn:
                logger.warning(
                    "memtomem-memory: memory.%s.%s = %r is out of range; using %r",
                    _NAME,
                    key,
                    raw[key],
                    fallback,
                )

    for key, check in _INDEPENDENT_CHECKS.items():
        take(key, key in raw and check(raw[key]), _DEFAULTS[key])
    settings["server"] = settings["server"].strip()
    # The schedule must not shrink: a default below the key before it is raised to it.
    first = settings["cooldown_seconds"]
    long_fallback = max(_DEFAULTS["long_cooldown_seconds"], first)
    long_value = raw.get("long_cooldown_seconds")
    take(
        "long_cooldown_seconds",
        _num_in(first, 600)(long_value),
        long_fallback,
    )
    second = settings["long_cooldown_seconds"]
    max_value = raw.get("max_cooldown_seconds")
    take(
        "max_cooldown_seconds",
        _num_in(second, 600)(max_value),
        max(_DEFAULTS["max_cooldown_seconds"], second),
    )
    unknown = sorted(set(raw) - set(_DEFAULTS))
    if unknown and warn:
        logger.warning("memtomem-memory: ignoring unknown keys under memory.%s: %s", _NAME, unknown)
    return settings


# --- the worker ---------------------------------------------------------------------------------


def _event(event: str, **fields: Any) -> None:
    """One DEBUG JSON line. Never carries query text."""
    if logger.isEnabledFor(logging.DEBUG):
        record = {"event": event, **fields}
        logger.debug("%s", json.dumps(record, sort_keys=True, default=str, ensure_ascii=False))


def _warn_once(registry: types.ModuleType, admission: _Admission, key: str, message: str) -> None:
    """One WARNING per admission (entry name) per reason, for the life of the process."""
    with registry.lock:
        if key in admission.warned:
            return
        admission.warned.add(key)
    logger.warning("memtomem-memory: %s", message)


def _parse_payload(envelope: Dict[str, Any]) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Classify an ``ok`` envelope: ``unparsed`` (no recall, not a failure), ``incompatible``
    (the server lacks the contract echo) or ``ok`` with the payload."""
    if envelope.get("truncated"):
        return "unparsed", None
    raw = envelope.get("result")
    if isinstance(raw, str):
        try:
            payload = json.loads(raw)
        except ValueError:
            return "unparsed", None
    else:
        payload = raw
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return "unparsed", None
    # The provider always passes record=False; a server that does not echo it back predates the
    # contract (or ignored the argument) and is refused.
    if payload.get("recorded") is not False:
        return "incompatible", None
    return "ok", payload


def _run_call(
    registry: types.ModuleType,
    admission: _Admission,
    slot: _Slot,
    holder: _ContextHolder,
    server: str,
    arguments: Dict[str, Any],
    timeout: float,
    schedule: Tuple[float, float, float],
    base: Dict[str, Any],
) -> None:
    """Worker body. Module-level on purpose: it holds the slot and the admission, never the
    provider instance, so a dropped instance is not kept alive by its last call."""
    started = time.perf_counter()
    verdict = "failure"  # failure | success | incompatible | neutral
    try:
        try:
            outcome, warning = _preflight(server)
        except Exception as exc:  # a config read failing is not the server's fault
            outcome, warning = (
                "skipped_config_error",
                f"cannot read Hermes config ({type(exc).__name__})",
            )
        if outcome:
            verdict = "neutral"
            slot.outcome = outcome
            _warn_once(registry, admission, outcome, warning)
            _event("preflight", outcome=outcome, server=server, **base)
            return
        try:
            context = holder.get()
        except Exception as exc:
            verdict = "neutral"
            slot.outcome = "skipped_no_context"
            _warn_once(
                registry,
                admission,
                "context",
                f"cannot build a Hermes plugin context ({type(exc).__name__}); is this Hermes "
                "Agent 0.21.5 or later?",
            )
            _event("preflight", outcome=slot.outcome, server=server, **base)
            return
        try:
            envelope = context.call_mcp(server, _TOOL, arguments, timeout=timeout)
        except PermissionError:
            slot.outcome = "rpc_error"
            _warn_once(registry, admission, "permission", _allowlist_text(server))
            _event("rpc_error", error="PermissionError", server=server, **base)
            return
        rpc_ms = round((time.perf_counter() - started) * 1000, 1)
        if not isinstance(envelope, dict) or not envelope.get("ok"):
            # Hermes's error text can quote the call's arguments, the query among them, so only
            # its kind and size are logged.
            error = envelope.get("error") if isinstance(envelope, dict) else envelope
            slot.outcome = "rpc_error"
            _event(
                "rpc_error",
                error=type(error).__name__,
                error_chars=len(str(error)),
                rpc_ms=rpc_ms,
                server=server,
                **base,
            )
            return
        kind, payload = _parse_payload(envelope)
        if kind == "unparsed":
            verdict = "neutral"
            slot.outcome = "rpc_unparsed"
            _event(
                "rpc_unparsed",
                truncated=bool(envelope.get("truncated")),
                rpc_ms=rpc_ms,
                server=server,
                **base,
            )
            return
        if kind == "incompatible":
            verdict = "incompatible"
            slot.outcome = "incompatible"
            _warn_once(
                registry,
                admission,
                "incompatible",
                f"the {server!r} MCP entry answers without the background-search contract "
                f"(memtomem older than {_MIN_MEMTOMEM}); recall is off until the entry is "
                "pinned to a newer release",
            )
            _event("rpc_incompatible", rpc_ms=rpc_ms, server=server, **base)
            return
        assert payload is not None
        results = [r for r in payload["results"] if isinstance(r, dict)]
        hints = [str(h) for h in payload.get("hints") or [] if isinstance(h, str)]
        verdict = "success"
        slot.result = {"results": results}
        slot.outcome = "delivered"
        _event(
            "rpc_done",
            rpc_ms=rpc_ms,
            n=len(results),
            chunk_ids=[r.get("chunk_id") for r in results],
            sources=[r.get("source") for r in results],
            dense_failed=any(_DENSE_FAILED_HINT in h for h in hints),
            server=server,
            **base,
        )
    except Exception as exc:  # logged, never raised into Hermes
        slot.outcome = "rpc_error"
        _event("rpc_error", error=type(exc).__name__, server=server, **base)
    finally:
        with registry.lock:
            _apply_verdict(registry, admission, verdict, schedule)
            if admission.slot is slot:
                admission.slot = None
            registry.total_inflight -= 1
        slot.done.set()


def _apply_verdict(
    registry: types.ModuleType,
    admission: _Admission,
    verdict: str,
    schedule: Tuple[float, float, float],
) -> None:
    """Caller holds ``registry.lock``."""
    if verdict == "success":
        admission.failures = 0
        admission.cooldown_until = 0.0
        admission.incompatible = False
        return
    if verdict == "neutral":
        return
    admission.incompatible = verdict == "incompatible"
    admission.failures += 1
    step = schedule[min(admission.failures, len(schedule)) - 1]
    admission.cooldown_until = registry.clock() + step


# --- the provider -------------------------------------------------------------------------------


class MemtomemMemoryProvider(MemoryProvider):
    """Recall-only: ``prefetch`` is the whole job; every other hook is a no-op or bookkeeping."""

    def __init__(self, context: Any = None) -> None:  # no side effects
        self._iid = uuid.uuid4().hex[:8]
        self._lock = threading.Lock()
        self._gen = 0
        self._turn = 0
        self._closed = False
        self._status: Optional[RecallStatus] = None
        self._settings: Dict[str, Any] = dict(_DEFAULTS)
        self._holder = _ContextHolder(context)

    @property
    def name(self) -> str:
        return _NAME

    # -- availability (config and imports only: no network, no spawn, no plugin discovery) ------

    def is_available(self) -> bool:
        return not self.unavailable_reason()

    def unavailable_reason(self) -> str:
        try:
            from hermes_cli.plugins import (  # noqa: F401  (availability probe only)
                PluginContext,
                PluginManifest,
                get_plugin_manager,
            )
            from hermes_cli.config import load_config_readonly
        except Exception as exc:
            return (
                f"this Hermes has no plugin MCP access (needs Hermes Agent 0.21.5 or later): {exc}"
            )
        try:
            cfg = _mapping(load_config_readonly())
        except Exception as exc:
            return f"cannot read Hermes config: {exc}"
        server = _read_settings(cfg, warn=False)["server"]
        if server not in _allowlist(cfg):
            return _allowlist_text(server)
        native = _mapping(cfg.get("mcp_servers")).get(server)
        if isinstance(native, dict):
            if not _parse_boolish(native.get("enabled", True), default=True):
                return f"the {server!r} MCP entry is disabled"
            if _normalize_server_trust(native.get("trust")) != "full":
                return _untrusted_text(server)
            return ""
        portable = _portable_servers()
        # Unknown or empty: Hermes fills the portable registry during its own discovery, which
        # this probe must not trigger. Treat as available; the per-turn check decides.
        if portable is _PORTABLE_UNKNOWN or not portable:
            return ""
        if server not in portable:
            return (
                f"no MCP entry named {server!r}; install the memtomem plugin or add "
                f"mcp_servers.{server} to config.yaml"
            )
        return ""

    # -- lifecycle --------------------------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        origin = "unknown"
        try:
            from hermes_cli.config import load_config_readonly

            cfg = _mapping(load_config_readonly())
            settings = _read_settings(cfg, warn=True)
            origin = _effective_entry(cfg, settings["server"])[0]
        except Exception as exc:
            logger.warning(
                "memtomem-memory: cannot read config.yaml (%s); using defaults", type(exc).__name__
            )
            settings = dict(_DEFAULTS)
        with self._lock:
            self._settings = settings
            self._gen += 1
            self._closed = False
        _event(
            "initialize",
            entry=origin,
            platform=kwargs.get("platform"),
            agent_context=kwargs.get("agent_context", "primary"),
            settings=settings,
            **self._base(),
        )

    def shutdown(self) -> None:
        # Detach only: the connection is Hermes's, and the worker drains into its own slot.
        with self._lock:
            self._closed = True
            self._gen += 1
            self._status = None
        _event("shutdown", **self._base())

    def on_session_switch(
        self,
        new_session_id: str,
        *,
        parent_session_id: str = "",
        reset: bool = False,
        rewound: bool = False,
        **kwargs: Any,
    ) -> None:
        with self._lock:
            if reset:
                self._gen += 1
                self._status = None
        _event("on_session_switch", reset=reset, rewound=rewound, **self._base())

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        return None

    # -- recall -------------------------------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        started = time.perf_counter()
        with self._lock:
            self._turn += 1
            token = (self._gen, self._turn)
            self._status = None
            settings = self._settings

        def finish(outcome: str, text: str = "", ids: Optional[List[str]] = None) -> str:
            _event(
                "prefetch",
                outcome=outcome,
                hook_ms=round((time.perf_counter() - started) * 1000, 2),
                chars=len(text),
                chunk_ids=ids or [],
                token=list(token),
                **self._base(),
            )
            return text

        if is_trivial_prompt(query):
            return finish("trivial")
        slot, outcome = self._submit(query, settings)
        if slot is None:
            return finish(outcome)
        budget = settings["budget_ms"] / 1000.0
        if not slot.done.wait(timeout=max(0.0, budget - (time.perf_counter() - started))):
            return finish("hook_timeout")  # the call keeps running and keeps the name's slot
        if slot.result is None:
            return finish(slot.outcome or "no_result")
        text, ids = _shape(slot.result["results"], settings["max_chars"])
        with self._lock:  # validate the token and publish the status in one step
            if (self._gen, self._turn) != token or self._closed:
                return finish("stale")
            if text:
                self._status = RecallStatus(_RECALL_LABEL, len(ids))
        return finish("hit" if text else "no_results", text, ids)

    def _submit(self, query: str, settings: Dict[str, Any]) -> Tuple[Optional[_Slot], str]:
        with self._lock:
            if self._closed:
                return None, "skipped_closed"
        server = settings["server"]
        registry = _registry()
        with registry.lock:
            admission = registry.admissions.get(server)
            if admission is None:
                admission = registry.admissions[server] = _Admission()
            if admission.slot is not None:
                return None, "skipped_busy"
            if registry.clock() < admission.cooldown_until:
                return None, "skipped_cooldown"
            if registry.total_inflight >= registry.ceiling:
                return None, "skipped_cap"
            slot = _Slot()
            admission.slot = slot
            registry.total_inflight += 1
        arguments = {
            "query": query,
            "top_k": settings["top_k"],
            "record": False,
            "rerank": False,
            "output_format": "structured",
        }
        schedule = (
            float(settings["cooldown_seconds"]),
            float(settings["long_cooldown_seconds"]),
            float(settings["max_cooldown_seconds"]),
        )
        try:
            spawn_context_thread(
                _run_call,
                name="memtomem-memory-call",
                args=(
                    registry,
                    admission,
                    slot,
                    self._holder,
                    server,
                    arguments,
                    float(settings["rpc_timeout_seconds"]),
                    schedule,
                    self._base(),
                ),
            ).start()
        except Exception as exc:
            with registry.lock:
                if admission.slot is slot:
                    admission.slot = None
                registry.total_inflight -= 1
            slot.done.set()
            logger.warning(
                "memtomem-memory: cannot start the recall worker (%s)", type(exc).__name__
            )
            return None, "spawn_failed"
        return slot, "submitted"

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        return None  # policy: the current turn's query, not the previous one's

    def recall_status(self) -> Optional[RecallStatus]:
        with self._lock:
            return self._status

    # -- everything a recall-only provider does not do --------------------------------------------

    def system_prompt_block(self) -> str:
        return ""

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return []

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return []

    def backup_paths(self) -> List[str]:
        return []

    def _base(self) -> Dict[str, Any]:
        return {"iid": self._iid, "gen": self._gen, "turn": self._turn}


def _shape(results: List[Dict[str, Any]], char_cap: int) -> Tuple[str, List[str]]:
    """Labelled bullets under a character cap; returns (text, chunk ids injected).

    Byte-for-byte the shaping the Phase 1 gate measured (identical deliveries on all 40 turns).
    """
    lines: List[str] = []
    ids: List[str] = []
    used = 0
    for r in results:
        where = r.get("source") or ""
        if r.get("hierarchy"):
            where += " › " + r["hierarchy"]
        body = " ".join(str(r.get("content") or "").split())
        entry = f"- [{where}] {body}"
        room = char_cap - used
        if room < 200 and ids:
            break
        if len(entry) > room:
            entry = entry[: max(0, room - 1)] + "…"
        lines.append(entry)
        ids.append(str(r.get("chunk_id")))
        used += len(entry) + 1
    if not lines:
        return "", []
    return _HEADER + "\n" + "\n".join(lines), ids


def register(ctx: Any) -> None:
    # A future Hermes may forward call_mcp on the collector; use it when it does.
    context = ctx if callable(getattr(ctx, "call_mcp", None)) else None
    ctx.register_memory_provider(MemtomemMemoryProvider(context))
