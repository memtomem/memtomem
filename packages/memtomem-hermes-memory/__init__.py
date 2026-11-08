"""memtomem-memory: a Hermes memory provider backed by memtomem.

On every non-trivial user turn it makes one ``mem_search`` call on the ``memtomem`` MCP entry
through Hermes's own MCP client (``PluginContext.call_mcp``) and injects the shaped results. It
owns no server, connection or child process: Hermes does. Recall writes nothing: the call passes
``record=False``, and the provider refuses a server that does not echo ``"recorded": false``.

With ``memory.memtomem-memory.mirror_writes: true`` it also mirrors Hermes's built-in memory
writes (``MEMORY.md`` / ``USER.md``) into memtomem: each committed add, replace or remove becomes
one ``mem_do(action="hermes_mirror_apply")`` call, sent in order by a per-instance worker. The
server keeps the entries (last writer wins per entry) and renders them to a Markdown file; the
provider keeps no copy. Pending operations live in memory only, so a crash loses them.

Each instance asks the server what it supports (``mem_do(action="version")``) before its first
call and every 10 minutes, and sends ``namespace="!system"``, ``deadline_ms`` and mirror calls
only to a server that advertises them; an older server gets exactly the recall call it always got.

That connection, its call queue and its circuit breaker are shared with the model's own memtomem
tools, so recall is rationed: at most one outstanding recall call per MCP entry name in the whole
process (every profile, every module copy), at most 32 across all names, and a cooldown of
15 s, 60 s, then 300 s after consecutive failures. See README.md for what that means in use.

Standard library only, and Python 3.11 compatible: Hermes supports 3.11 while the memtomem
monorepo targets 3.12, so tests parse this file with ``feature_version=(3, 11)`` and CI imports
and exercises it under 3.11.
"""

from __future__ import annotations

import collections
import contextvars
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import types
import uuid
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

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
_DISPATCH_TOOL = "mem_do"
_MIRROR_ACTION = "hermes_mirror_apply"
_VERSION_ACTION = "version"
# The server's "every non-system namespace" read selector (memtomem 0.7.1+). Sent only when the
# server lists it: an older server reads it as a namespace name and returns nothing.
_NON_SYSTEM = "!system"
_CAPS_TTL_S = 600.0
# Below this a processing budget leaves the server no room to answer; the argument is omitted.
_MIN_DEADLINE_MS = 500
# ``PluginContext.call_mcp`` clamps its timeout to [1, 600] s (Hermes 0.21.5 and 0.23.0).
_HERMES_MIN_TIMEOUT_S = 1.0
_MIRROR_QUEUE_MAX = 256
_MIRROR_ATTEMPTS = 3
_RETRY_BACKOFF_S = (1.0, 2.0, 4.0)
_MIRROR_ACTIONS = frozenset({"add", "replace", "remove"})
_TARGET_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")

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


class _Caps:
    """One provider instance's view of what its server supports.

    Filled by the instance's first RPC (a ``mem_do`` version probe), dropped after any failure
    that starts a cooldown, and stale after ``_CAPS_TTL_S``, so a server replaced under the same
    entry is asked again. An empty mapping means an older server: nothing new is sent to it.
    """

    __slots__ = ("_lock", "_value", "_at")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: Optional[Dict[str, Any]] = None
        self._at = 0.0

    def fresh(self, now: float) -> Optional[Dict[str, Any]]:
        with self._lock:
            if self._value is not None and now - self._at < _CAPS_TTL_S:
                return self._value
            return None

    def store(self, value: Dict[str, Any], now: float) -> None:
        with self._lock:
            self._value, self._at = value, now

    def drop(self) -> None:
        with self._lock:
            self._value = None


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
    "mirror_writes": False,
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
    mirror = _strict_bool(raw.get("mirror_writes", False))
    if mirror is None:
        mirror = False
        if warn:
            logger.warning(
                "memtomem-memory: memory.%s.mirror_writes = %r is not true or false; mirroring "
                "stays off",
                _NAME,
                raw.get("mirror_writes"),
            )
    settings["mirror_writes"] = mirror
    unknown = sorted(set(raw) - set(_DEFAULTS))
    if unknown and warn:
        logger.warning("memtomem-memory: ignoring unknown keys under memory.%s: %s", _NAME, unknown)
    return settings


def _strict_bool(value: Any) -> Optional[bool]:
    """A real boolean or one of Hermes's boolish words; anything else is ``None`` (not a yes).

    Stricter than ``_parse_boolish``: a number or a stray string never switches writes on.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_WORDS:
            return True
        if lowered in _FALSE_WORDS:
            return False
    return None


def _profile_key(hermes_home: Any) -> str:
    """The profile this instance serves, as Hermes keys it; ``""`` when Hermes gave no home."""
    if not hermes_home:
        return ""
    try:
        from hermes_constants import hermes_home_key

        return str(hermes_home_key(hermes_home))
    except Exception:
        return str(hermes_home)


def _owner_id(profile_key: str) -> str:
    """Opaque mirror owner: no path of the profile reaches the store."""
    return hashlib.sha256(profile_key.encode("utf-8")).hexdigest()[:32]


def _deadline_ms(settings: Dict[str, Any]) -> Optional[int]:
    """Server processing budget: the RPC timeout less one second, if that leaves room."""
    budget = int(float(settings["rpc_timeout_seconds"]) * 1000) - 1000
    return budget if budget >= _MIN_DEADLINE_MS else None


# --- the worker ---------------------------------------------------------------------------------


def _event(event: str, **fields: Any) -> None:
    """One DEBUG JSON line. Never carries query text."""
    if logger.isEnabledFor(logging.DEBUG):
        record = {"event": event, **fields}
        logger.debug("%s", json.dumps(record, sort_keys=True, default=str, ensure_ascii=False))


def _warn_once(registry: types.ModuleType, admission: _Admission, key: str, message: str) -> None:
    """One WARNING per admission (entry name) per key, for the life of the process.

    Callers put the profile key in ``key``, so each profile sees its own configuration warning.
    """
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


def _probe(context: Any, server: str, timeout: float) -> Tuple[str, Dict[str, Any]]:
    """Ask the server what it supports. ``("ok", caps)``, ``("legacy", {})`` for an answer
    without capabilities, ``("failure", {})`` for a transport failure. ``PermissionError``
    propagates (the allowlist refused the call)."""
    try:
        envelope = context.call_mcp(
            server, _DISPATCH_TOOL, {"action": _VERSION_ACTION}, timeout=timeout
        )
    except PermissionError:
        raise
    except Exception:
        return "failure", {}
    if not isinstance(envelope, dict) or not envelope.get("ok"):
        return "failure", {}
    raw = envelope.get("result")
    try:
        payload = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return "legacy", {}
    caps = payload.get("capabilities") if isinstance(payload, dict) else None
    return ("ok", dict(caps)) if isinstance(caps, dict) else ("legacy", {})


def _recall_arguments(
    arguments: Dict[str, Any], caps: Dict[str, Any], deadline_ms: Optional[int]
) -> Dict[str, Any]:
    """The recall call's arguments for this server: new ones only where it advertises them."""
    out = dict(arguments)
    selectors = caps.get("namespace_selectors")
    if isinstance(selectors, list) and _NON_SYSTEM in selectors:
        out["namespace"] = _NON_SYSTEM
    if caps.get("search_deadline_ms") is True and deadline_ms is not None:
        out["deadline_ms"] = deadline_ms
    return out


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
    caps_holder: Optional[_Caps] = None,
    profile_key: str = "",
    deadline_ms: Optional[int] = None,
) -> None:
    """Worker body. Module-level on purpose: it holds the slot and the admission, never the
    provider instance, so a dropped instance is not kept alive by its last call."""
    started = time.perf_counter()
    verdict = "failure"  # failure | success | incompatible | neutral
    caps_holder = caps_holder if caps_holder is not None else _Caps()
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
            _warn_once(registry, admission, f"{profile_key}:{outcome}", warning)
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
                f"{profile_key}:context",
                f"cannot build a Hermes plugin context ({type(exc).__name__}); is this Hermes "
                "Agent 0.21.5 or later?",
            )
            _event("preflight", outcome=slot.outcome, server=server, **base)
            return
        try:
            caps = caps_holder.fresh(registry.clock())
            if caps is None:
                kind, caps = _probe(context, server, timeout)
                _event("probe", kind=kind, server=server, **base)
                if kind == "failure":
                    # One failure, and the cycle ends here: no recall call behind it.
                    slot.outcome = "rpc_error"
                    return
                caps_holder.store(caps, registry.clock())
            arguments = _recall_arguments(arguments, caps, deadline_ms)
            envelope = context.call_mcp(server, _TOOL, arguments, timeout=timeout)
        except PermissionError:
            slot.outcome = "rpc_error"
            _warn_once(registry, admission, f"{profile_key}:permission", _allowlist_text(server))
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
                f"{profile_key}:incompatible",
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
        if verdict in ("failure", "incompatible"):
            caps_holder.drop()
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


# --- mirroring built-in memory writes ----------------------------------------------------------


class _MirrorOp:
    """One operation for ``hermes_mirror_apply``, with the context it was captured under."""

    __slots__ = ("params", "context", "attempts", "not_before")

    def __init__(self, params: Dict[str, Any], context: contextvars.Context) -> None:
        self.params = params
        self.context = context
        self.attempts = 0
        self.not_before = 0.0


class _Mirror:
    """One provider instance's mirror queue and worker state. Holds no provider instance.

    Operations run in order, one at a time, through the entry name's recall slot. The queue is
    bounded; what does not fit, what the server refuses and what still fails after
    ``_MIRROR_ATTEMPTS`` tries is dropped and counted, never retried forever.
    """

    def __init__(
        self,
        *,
        server: str,
        owner: str,
        profile_key: str,
        settings: Dict[str, Any],
        holder: "_ContextHolder",
        caps: _Caps,
        base: Dict[str, Any],
    ) -> None:
        self.server = server
        self.owner = owner
        self.profile_key = profile_key
        self.timeout = float(settings["rpc_timeout_seconds"])
        self.deadline_ms = _deadline_ms(settings)
        self.schedule = (
            float(settings["cooldown_seconds"]),
            float(settings["long_cooldown_seconds"]),
            float(settings["max_cooldown_seconds"]),
        )
        self.holder = holder
        self.caps = caps
        self.base = dict(base)
        self.cond = threading.Condition()
        self.queue: Deque[_MirrorOp] = collections.deque()
        # Re-renders asked for after a ``rendered: false`` reply, by target. Kept out of the
        # queue: a reconcile waits out a cooldown, and must not hold back the writes behind it.
        self.reconcile_due: Dict[str, Tuple[float, contextvars.Context]] = {}
        self.counts: Dict[str, int] = collections.Counter()
        self.thread: Optional[threading.Thread] = None
        self.closing = False
        self.disabled = False
        self.flush_until = 0.0

    def count(self, registry: types.ModuleType, reason: str, message: str, n: int = 1) -> None:
        """Warn once per profile and reason, then count (a reader of ``counts`` sees the log)."""
        admission = _admission_for(registry, self.server)
        _warn_once(registry, admission, f"{self.profile_key}:mirror:{reason}", message)
        total = self.counts[reason] + n
        _event("mirror_" + reason, n=n, total=total, server=self.server, **self.base)
        self.counts[reason] = total


def _admission_for(registry: types.ModuleType, server: str) -> _Admission:
    with registry.lock:
        admission = registry.admissions.get(server)
        if admission is None:
            admission = registry.admissions[server] = _Admission()
        return admission


def _take_slot(registry: types.ModuleType, server: str) -> Tuple[Optional[_Slot], float]:
    """The name's slot for one write, or ``(None, seconds to wait)``: writes wait, never skip."""
    with registry.lock:
        admission = registry.admissions.get(server)
        if admission is None:
            admission = registry.admissions[server] = _Admission()
        now = registry.clock()
        if now < admission.cooldown_until:
            return None, min(1.0, admission.cooldown_until - now)
        if admission.slot is not None or registry.total_inflight >= registry.ceiling:
            return None, 0.05
        slot = _Slot()
        admission.slot = slot
        registry.total_inflight += 1
        return slot, 0.0


def _release_slot(registry: types.ModuleType, mirror: _Mirror, slot: _Slot, verdict: str) -> None:
    admission = _admission_for(registry, mirror.server)
    if verdict in ("failure", "incompatible"):
        mirror.caps.drop()
    with registry.lock:
        _apply_verdict(registry, admission, verdict, mirror.schedule)
        if admission.slot is slot:
            admission.slot = None
        registry.total_inflight -= 1
    slot.done.set()


def _rpc_timeout(mirror: _Mirror) -> Optional[float]:
    """The timeout for the next RPC: the RPC timeout, or what is left of the shutdown flush.
    ``None`` when the flush has run out (the operation is dropped, not started)."""
    if not mirror.closing:
        return mirror.timeout
    left = mirror.flush_until - time.monotonic()
    # Hermes raises any call_mcp timeout below 1 s to 1 s, so a shorter remainder cannot
    # be honoured: such a call is not started.
    return min(mirror.timeout, left) if left >= _HERMES_MIN_TIMEOUT_S else None


_REFUSAL_KINDS = (
    ("Error: content matches", "privacy_refused"),
    ("Error: purged", "purged"),
    ("Error: read_only_target", "destination_refused"),
    ("Error: source_excluded", "destination_refused"),
    ("Error: mirror_target_link", "destination_refused"),
    ("Error: invalid parameter", "invalid_params"),
)


def _refusal_kind(text: str) -> str:
    """A fixed code for a refusal: server text can quote paths or entry text, so it is
    never logged."""
    for prefix, kind in _REFUSAL_KINDS:
        if text.startswith(prefix):
            return kind
    return "refused"


def _error_type(text: Any) -> str:
    """The leading exception name of a ``render_error`` (``OSError: …`` → ``OSError``)."""
    head = str(text or "").split(":", 1)[0].strip()
    return head if head.isidentifier() else "other"


def _send_mirror_op(registry: types.ModuleType, mirror: _Mirror, op: _MirrorOp) -> str:
    """Send ``op`` once under the name's slot. ``done`` | ``drop`` | ``retry`` | ``disable``."""
    if _rpc_timeout(mirror) is None:
        return "flush_over"
    slot, wait = _take_slot(registry, mirror.server)
    if slot is None:
        op.not_before = time.monotonic() + wait
        return "wait"
    verdict = "neutral"
    try:
        try:
            outcome, warning = _preflight(mirror.server)
        except Exception as exc:
            outcome, warning = "skipped_config_error", f"cannot read Hermes config ({exc!r})"
        if outcome:
            mirror.count(registry, "dropped_" + outcome, warning)
            return "drop"
        try:
            context = mirror.holder.get()
        except Exception as exc:
            mirror.count(
                registry, "dropped_no_context", f"cannot build a Hermes plugin context ({exc!r})"
            )
            return "drop"
        caps = mirror.caps.fresh(registry.clock())
        if caps is None:
            timeout = _rpc_timeout(mirror)
            if timeout is None:
                return "flush_over"
            try:
                kind, caps = _probe(context, mirror.server, timeout)
            except PermissionError:
                mirror.count(registry, "dropped_permission", _allowlist_text(mirror.server))
                return "drop"
            _event("probe", kind=kind, server=mirror.server, **mirror.base)
            if kind == "failure":
                verdict = "failure"
                return "retry"
            mirror.caps.store(caps, registry.clock())
        if not isinstance(caps.get("hermes_mirror"), dict):
            return "disable"
        params = dict(op.params)
        if mirror.deadline_ms is not None and params.get("op") != "reconcile":
            params["deadline_ms"] = mirror.deadline_ms
        timeout = _rpc_timeout(mirror)
        if timeout is None:
            return "flush_over"
        try:
            envelope = context.call_mcp(
                mirror.server,
                _DISPATCH_TOOL,
                {"action": _MIRROR_ACTION, "params": params},
                timeout=timeout,
            )
        except PermissionError:
            mirror.count(registry, "dropped_permission", _allowlist_text(mirror.server))
            return "drop"
        except Exception as exc:
            verdict = "failure"
            _event(
                "mirror_rpc_error", error=type(exc).__name__, server=mirror.server, **mirror.base
            )
            return "retry"
        if not isinstance(envelope, dict) or not envelope.get("ok"):
            verdict = "failure"
            _event("mirror_rpc_error", error="not_ok", server=mirror.server, **mirror.base)
            return "retry"
        raw = envelope.get("result")
        text = raw if isinstance(raw, str) else json.dumps(raw)
        if text.startswith("Error (retryable)"):
            return "backoff"
        if text.startswith("Error"):
            # Validation, a privacy refusal, a refused destination, a purge fence: final.
            kind = _refusal_kind(text)
            mirror.count(
                registry,
                "dropped_" + kind,
                f"memtomem refused a mirror write ({kind}); later writes still go",
            )
            return "drop"
        try:
            reply = json.loads(text)
        except ValueError:
            reply = None
        if not isinstance(reply, dict) or reply.get("schema_version") != 1:
            mirror.count(registry, "dropped_unparsed", "memtomem sent an unreadable mirror reply")
            return "drop"
        verdict = "success"
        _event(
            "mirror_applied",
            op=params.get("op"),
            changed=reply.get("changed"),
            rendered=reply.get("rendered"),
            server=mirror.server,
            **mirror.base,
        )
        if reply.get("previous") == "unmatched":
            mirror.count(
                registry,
                "unmatched",
                "a replaced or removed Hermes memory entry was not found in the mirror (its text "
                "differed from when it was mirrored, e.g. masked differently); the old copy stays "
                "in memtomem. To clear it, purge the target and let Hermes write again",
            )
        if reply.get("content_refused"):
            mirror.count(
                registry,
                "content_refused",
                "a replacement entry matched a memtomem privacy pattern: the old entry was "
                "removed from the mirror and the new one was not stored",
            )
        if reply.get("rendered") is False:
            _event(
                "mirror_not_rendered",
                error=_error_type(reply.get("render_error")),
                error_chars=len(str(reply.get("render_error") or "")),
                server=mirror.server,
                **mirror.base,
            )
            target = str(params.get("target"))
            with mirror.cond:
                if target not in mirror.reconcile_due:
                    due = time.monotonic() + mirror.schedule[0]
                    mirror.reconcile_due[target] = (due, op.context)
                    mirror.cond.notify_all()
        return "done"
    finally:
        _release_slot(registry, mirror, slot, verdict)


def _next_op(mirror: _Mirror) -> Tuple[Optional[_MirrorOp], float]:
    """The operation to send now, or ``(None, seconds to wait)``. Caller holds ``mirror.cond``.

    Queued writes go first and in order; a due reconcile goes when the head write is not ready.
    """
    now = time.monotonic()
    wait = 1.0
    if mirror.queue:
        head = mirror.queue[0]
        if head.not_before <= now:
            return head, 0.0
        wait = head.not_before - now
    for target, (due, context) in list(mirror.reconcile_due.items()):
        if due <= now:
            del mirror.reconcile_due[target]
            return _MirrorOp(
                {"owner": mirror.owner, "target": target, "op": "reconcile"}, context
            ), 0.0
        wait = min(wait, due - now)
    return None, min(wait, 1.0)


def _retire(mirror: _Mirror) -> None:
    """Stop accepting writes; the worker flushes for at most one RPC timeout, then drops."""
    with mirror.cond:
        if not mirror.closing:
            mirror.closing = True
            mirror.flush_until = time.monotonic() + mirror.timeout
        mirror.cond.notify_all()


def _mirror_worker(registry: types.ModuleType, mirror: _Mirror) -> None:
    """Send queued operations in order until the instance shuts down and the flush ends."""
    while True:
        with mirror.cond:
            while not mirror.queue and not mirror.reconcile_due and not mirror.closing:
                mirror.cond.wait(1.0)
            if not mirror.queue and (mirror.closing or not mirror.reconcile_due):
                return
            if mirror.closing and time.monotonic() >= mirror.flush_until:
                left = len(mirror.queue)
                mirror.queue.clear()
                mirror.reconcile_due.clear()
                break
            op, delay = _next_op(mirror)
            if op is None:
                mirror.cond.wait(delay)
                continue
        try:
            result = op.context.run(_send_mirror_op, registry, mirror, op)
        except Exception as exc:  # never let the worker die
            _event("mirror_worker_error", error=type(exc).__name__, **mirror.base)
            result = "drop"
        if result == "flush_over":
            with mirror.cond:
                left = len(mirror.queue)
                mirror.queue.clear()
                mirror.reconcile_due.clear()
            break
        if op.params.get("op") == "reconcile":
            if result in ("wait", "retry", "backoff"):
                with mirror.cond:
                    target = str(op.params["target"])
                    if target not in mirror.reconcile_due:
                        retry_at = max(op.not_before, time.monotonic() + mirror.schedule[0])
                        mirror.reconcile_due[target] = (retry_at, op.context)
            continue
        with mirror.cond:
            if result == "wait":
                continue
            if result in ("retry", "backoff"):
                op.attempts += 1
                if op.attempts < _MIRROR_ATTEMPTS:
                    if result == "backoff":
                        op.not_before = time.monotonic() + _RETRY_BACKOFF_S[op.attempts - 1]
                    continue
                result = "drop_retries"
            if mirror.queue and mirror.queue[0] is op:
                mirror.queue.popleft()
            if result == "disable":
                mirror.disabled = True
                left = len(mirror.queue) + 1
                mirror.queue.clear()
        if result == "drop_retries":
            mirror.count(
                registry,
                "dropped_retries",
                f"a mirror write failed {_MIRROR_ATTEMPTS} times and was dropped",
            )
        elif result == "disable":
            mirror.count(
                registry,
                "disabled_old_server",
                f"the {mirror.server!r} MCP entry runs a memtomem without hermes_mirror_apply; "
                "mirroring is off for this session (recall continues)",
                n=left,
            )
            return
    if left:
        mirror.count(
            registry,
            "dropped_shutdown",
            "memtomem-memory shut down with mirror writes still queued; they were dropped",
            n=left,
        )


# --- the provider -------------------------------------------------------------------------------


class MemtomemMemoryProvider(MemoryProvider):
    """``prefetch`` recalls; ``on_memory_write`` mirrors built-in writes when opted in."""

    def __init__(self, context: Any = None) -> None:  # no side effects
        self._iid = uuid.uuid4().hex[:8]
        self._lock = threading.Lock()
        self._gen = 0
        self._turn = 0
        self._closed = False
        self._status: Optional[RecallStatus] = None
        self._settings: Dict[str, Any] = dict(_DEFAULTS)
        self._holder = _ContextHolder(context)
        self._caps = _Caps()
        self._profile_key = ""
        self._mirror: Optional[_Mirror] = None
        self._last_ts = 0

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
        profile_key = _profile_key(kwargs.get("hermes_home"))
        with self._lock:
            self._settings = settings
            self._gen += 1
            self._closed = False
            self._profile_key = profile_key
            # A fresh capability view: an old worker still holding the previous one cannot
            # fill this one in for a server that may have changed.
            self._caps = _Caps()
            retired = self._mirror
            self._mirror = None
            if settings["mirror_writes"]:
                self._mirror = _Mirror(
                    server=settings["server"],
                    owner=_owner_id(profile_key),
                    profile_key=profile_key,
                    settings=settings,
                    holder=self._holder,
                    caps=self._caps,
                    base={"iid": self._iid},
                )
        if retired is not None:
            _retire(retired)
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
        # Queued mirror writes get one flush of at most the RPC timeout; the rest are dropped.
        with self._lock:
            self._closed = True
            self._gen += 1
            self._status = None
            mirror = self._mirror
        if mirror is not None:
            _retire(mirror)
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
                    self._caps,
                    self._profile_key,
                    _deadline_ms(settings),
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

    # -- mirroring built-in memory writes ---------------------------------------------------------

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Queue one committed built-in write; returns at once (Hermes calls this inline).

        A replace or remove is mirrored only with ``metadata["previous_content"]`` (the entry as
        the native store held it): identity is never inferred from ``old_text``. Mirrored in
        every ``agent_context``: the operation reports what Hermes's own store committed, not a
        write of this provider's.
        """
        with self._lock:
            mirror = self._mirror
            closed = self._closed
            if mirror is None or closed or mirror.disabled or mirror.closing:
                return
            ts = max(time.time_ns(), self._last_ts + 1)
            self._last_ts = ts
        registry = _registry()
        if action not in _MIRROR_ACTIONS or not isinstance(target, str):
            return
        if not _TARGET_RE.fullmatch(target):
            mirror.count(registry, "skipped_target", f"cannot mirror memory target {target!r}")
            return
        params: Dict[str, Any] = {"owner": mirror.owner, "target": target, "op": action, "ts": ts}
        if action in ("add", "replace"):
            if not isinstance(content, str) or not content.strip():
                return
            params["content"] = content
        if action in ("replace", "remove"):
            previous = (metadata or {}).get("previous_content")
            if not isinstance(previous, str) or not previous.strip():
                mirror.count(
                    registry,
                    "skipped_no_previous",
                    "this Hermes did not say which entry a replace or remove changed "
                    "(no previous_content); that write was not mirrored",
                )
                return
            params["previous_content"] = previous
        op = _MirrorOp(params, contextvars.copy_context())
        with mirror.cond:
            if len(mirror.queue) >= _MIRROR_QUEUE_MAX:
                full = True
            else:
                full = False
                mirror.queue.append(op)
                mirror.cond.notify_all()
            start = not full and mirror.thread is None
            if start:
                mirror.thread = spawn_context_thread(
                    _mirror_worker, name="memtomem-memory-mirror", args=(registry, mirror)
                )
        if full:
            mirror.count(
                registry,
                "dropped_overflow",
                f"more than {_MIRROR_QUEUE_MAX} mirror writes were waiting; the newest was dropped",
            )
            return
        if start:
            try:
                mirror.thread.start()  # type: ignore[union-attr]
            except Exception as exc:
                logger.warning(
                    "memtomem-memory: cannot start the mirror worker (%s)", type(exc).__name__
                )
                with mirror.cond:
                    mirror.thread = None

    # -- setup ------------------------------------------------------------------------------------

    def post_setup(self, hermes_home: str, config: Dict[str, Any]) -> None:
        """``hermes memory setup memtomem-memory``: activate, and ask about mirroring.

        Writes ``memory.provider`` and ``memory.memtomem-memory.mirror_writes`` only after an
        answer at a terminal; without one (cron, a gateway, piped input) it writes nothing and
        prints the commands. It never grants itself MCP access: it prints the allowlist command.
        """
        memory = config.get("memory") if isinstance(config.get("memory"), dict) else {}
        section = memory.get(_NAME) if isinstance(memory.get(_NAME), dict) else {}
        server = _read_settings(config, warn=False)["server"]
        granted = _allowlist(config)
        merged = granted if server in granted else granted + [server]
        allow_cmd = (
            f"hermes config set plugins.entries.{_NAME}.mcp_allowlist '[{', '.join(merged)}]'"
        )
        stdin = sys.stdin
        # ``hermes_cli.setup.is_noninteractive`` (not public): under it, and on EOF, Hermes's
        # prompt returns its default without asking, which must not pass for an answer.
        noninteractive = os.environ.get("HERMES_NONINTERACTIVE", "").strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
        if noninteractive or stdin is None or not stdin.isatty():
            print(f"\n  {_NAME}: not a terminal, so nothing was written. To set it up, run:")
            print(f"    hermes config set memory.provider {_NAME}")
            print(f"    hermes config set memory.{_NAME}.mirror_writes false   # or true")
            print(f"    {allow_cmd}\n")
            return
        stored = _strict_bool(section.get("mirror_writes"))
        try:
            from hermes_cli.config import save_config
            from hermes_cli.setup import prompt_yes_no

            mirror = bool(
                prompt_yes_no(
                    "Mirror Hermes's built-in memory (MEMORY.md / USER.md) into memtomem? "
                    "Each saved, replaced or removed entry is sent to the memtomem server",
                    default=bool(stored),
                )
            )
        except (EOFError, KeyboardInterrupt):
            # Hermes's prompt exits on Ctrl+C and returns the default on EOF; kept for a
            # prompt that raises instead.
            print(f"\n  {_NAME}: setup cancelled; nothing was written.\n")
            return
        new_memory = dict(memory)
        new_section = dict(section)
        new_section["mirror_writes"] = mirror
        new_memory["provider"] = _NAME
        new_memory[_NAME] = new_section
        config["memory"] = new_memory
        save_config(config, merge_existing=True)
        print(f"\n  Memory provider: {_NAME} (mirror_writes: {str(mirror).lower()})")
        if server in granted:
            print(f"  It may call the {server!r} MCP entry already.\n")
        else:
            print(
                f"  It cannot call memtomem until you grant it the {server!r} MCP entry "
                "(the grant lets it send recall queries and, if enabled, mirror writes):"
            )
            print(f"    {allow_cmd}\n")

    # -- everything else this provider does not do ------------------------------------------------

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
