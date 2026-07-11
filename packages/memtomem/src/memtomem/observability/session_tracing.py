import json
import logging
import os
import random
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, Optional, TypedDict

logger = logging.getLogger(__name__)

_trace_config: Optional[Any] = None
_langfuse_client: Optional[Any] = None

# Config-load failure disables tracing invisibly; log it once so a
# broken config doesn't silently drop every trace (loud once, then
# DEBUG per feedback_silent_except_log_level — reference pattern:
# storage/mixins/schedules.py).
_config_load_warned = False


class TraceContext(TypedDict):
    """Mutable per-command trace state yielded from :func:`trace_session`.

    Typed so ``ctx["exit_code"]``/``ctx["status"]`` etc. keep their
    concrete types instead of collapsing to an ``int | dict | str | None``
    union the moment the literal dict is inferred (#1612). Callers mutate
    ``status`` / ``exit_code`` / ``metadata`` in place.
    """

    session_id: Optional[str]
    agent_id: str
    metadata: Dict[str, Any]
    payload: Dict[str, Any]
    exit_code: int
    status: str


class TraceRow(TypedDict):
    """One JSONL trace record. Pins the heterogeneous value types so the
    ``row`` dict in :func:`_write_local_jsonl` stops inferring
    ``dict[str, object]`` and threading an ``int | dict | str | None``
    union into ``_write_local_jsonl``'s typed parameters (#1612)."""

    trace_id: str
    session_id: Optional[str]
    agent_id: str
    command: str
    event_type: str
    started_at: str
    ended_at: str
    duration_ms: float
    status: str
    exit_code: int
    metadata: Dict[str, Any]
    payload: Any


def get_trace_config(*, force_reload: bool = False) -> Any:
    global _trace_config, _config_load_warned
    if force_reload:
        _trace_config = None
    if _trace_config is None:
        try:
            from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

            cfg = Mem2MemConfig()
            load_config_d(cfg, quiet=True)
            load_config_overrides(cfg)
            _trace_config = getattr(cfg, "session_trace", None)
        except Exception:
            # Fall through to DummyConfig (tracing disabled). Loud once so
            # an operator whose config broke sees why traces stopped.
            level = logging.DEBUG if _config_load_warned else logging.WARNING
            _config_load_warned = True
            logger.log(
                level,
                "session-trace config load failed; tracing disabled",
                exc_info=True,
            )
        if _trace_config is None:

            class DummyConfig:
                enabled = False
                jsonl_enabled = False
                langfuse_enabled = False
                jsonl_path = Path("~/.memtomem/traces/session-traces.jsonl")
                sampling_rate = 1.0
                payload_mode = "metadata"
                max_payload_chars = 10000

            _trace_config = DummyConfig()
    return _trace_config


def get_langfuse_client(config: Any, *, force_reload: bool = False) -> Any:
    global _langfuse_client
    if force_reload:
        _langfuse_client = None
    if (
        _langfuse_client is None
        and getattr(config, "enabled", False)
        and getattr(config, "langfuse_enabled", False)
    ):
        try:
            from langfuse import Langfuse

            kwargs: Dict[str, Any] = {}
            if public_key := getattr(config, "langfuse_public_key", ""):
                kwargs["public_key"] = public_key
            if secret_key := getattr(config, "langfuse_secret_key", ""):
                kwargs["secret_key"] = secret_key
            if host := getattr(config, "langfuse_host", ""):
                kwargs["host"] = host
            _langfuse_client = Langfuse(**kwargs)
        except Exception as e:
            logger.warning("Failed to initialize Langfuse client: %s", e)
    return _langfuse_client


def sanitize_metadata_key(key: str) -> str:
    sanitized = re.sub(r"[^a-zA-Z0-9]", "", key)
    return sanitized if sanitized else "key"


def sanitize_metadata_value(val: Any) -> str:
    s = str(val)
    if len(s) > 200:
        s = s[:197] + "..."
    return s


def format_propagated_metadata(metadata: Dict[str, Any]) -> Dict[str, str]:
    if not metadata:
        return {}
    res = {}
    for k, v in metadata.items():
        clean_key = sanitize_metadata_key(k)
        res[clean_key] = sanitize_metadata_value(v)
    return res


def _redact_value(val: Any) -> Any:
    if isinstance(val, dict):
        new_dict = {}
        for k, v in val.items():
            if isinstance(k, str) and any(
                sec in k.lower() for sec in ["api_key", "secret", "password", "token", "key"]
            ):
                new_dict[k] = "***"
            else:
                new_dict[k] = _redact_value(v)
        return new_dict
    elif isinstance(val, list):
        return [_redact_value(item) for item in val]
    elif isinstance(val, tuple):
        return tuple(_redact_value(item) for item in val)
    elif isinstance(val, str):
        # Try parsing as JSON first
        stripped = val.strip()
        if (stripped.startswith("{") and stripped.endswith("}")) or (
            stripped.startswith("[") and stripped.endswith("]")
        ):
            try:
                parsed = json.loads(val)
                redacted_parsed = _redact_value(parsed)
                return json.dumps(redacted_parsed, default=str)
            except Exception:
                # Not valid JSON after all — fall through to scan the raw
                # string. No log: this is an expected, benign miss (any
                # ``{``/``[``-bracketed non-JSON text), and the string
                # still gets secret-scanned below, so nothing leaks.
                pass

        # Scan for secrets using memtomem.privacy.scan
        try:
            from memtomem.privacy import scan

            hits = scan(val)
        except Exception:
            hits = []

        if hits:
            # Sort hits by span start descending to replace from right to left
            # so that replacement doesn't shift indexes of remaining spans.
            sorted_hits = sorted(hits, key=lambda h: h.span[0], reverse=True)
            for hit in sorted_hits:
                start, end = hit.span
                # Replace the matched span with "***"
                val = val[:start] + "***" + val[end:]

        # Also apply the pattern-matching for common key=value or key: value
        p1 = r'(?i)\b(api[_-]?key|secret[_-]?key|password|token|key)\b(\s*[:=]\s*)(["\']?)([\w\-\.\@\+\=\\\/]{3,})(["\']?)'
        val = re.sub(p1, r"\1\2\3***\5", val)

        # Pattern 2: CLI-style arguments like --api-key sk-... or -token xxxx
        p2 = r"(?i)\b(api[_-]?key|secret[_-]?key|password|token)\b(\s+)([\w\-\.\@\+\=\\\/]{3,})"
        val = re.sub(p2, r"\1\2***", val)

        return val
    elif val is None or isinstance(val, (bool, int, float)):
        # JSON scalars cannot carry a secret string — leave them untouched so
        # we don't stringify legitimate numbers/bools.
        return val
    else:
        # Any other object (Exception, bytes, custom type) is stringified by
        # the exporters (json.dumps(default=str) for the JSONL row,
        # sanitize_metadata_value's str() for Langfuse), so a value whose
        # __str__ contains "--api-key sk-…" would otherwise leak verbatim.
        # Redact its string form here too.
        return _redact_value(str(val))


def _redact_metadata(metadata: Dict[str, Any], mode: str) -> Dict[str, Any]:
    """Redact metadata values unless ``full`` payload mode is selected.

    Metadata (e.g. the wrapped command line, or an error string echoing it) can
    carry secrets just like the payload — ``mm session wrap -- tool --api-key
    sk-...`` lands the raw command in ``metadata["command"]``. The payload is
    redacted via :func:`format_payload`, but metadata is exported through a
    separate path (Langfuse propagated attributes + span output + the JSONL
    row), so it must be redacted here too or ``redacted`` / ``metadata`` mode
    leaks the secret. ``full`` mode opts into verbatim content for both.

    Delegates to :func:`_redact_value` on the whole dict so metadata is scrubbed
    exactly like the payload — including the dict-key policy: an ``api_key`` /
    ``password`` / ``token`` key redacts its value even when that value carries
    no inline marker (iterating values alone would drop the top-level key check
    and leak it).
    """
    if mode == "full" or not metadata:
        return metadata
    return _redact_value(metadata)


def format_payload(payload: Any, mode: str, max_chars: int) -> Any:
    if mode == "metadata":
        return None

    if mode == "redacted":
        payload = _redact_value(payload)

    try:
        serialized = json.dumps(payload, default=str)
    except Exception:
        serialized = str(payload)

    if len(serialized) > max_chars:
        serialized = (serialized[: max(0, max_chars - 14)] + "...[TRUNCATED]")[:max_chars]

    try:
        return json.loads(serialized)
    except Exception:
        return serialized


@contextmanager
def trace_session(
    command: str,
    event_type: str,
    agent_id: str = "default",
    session_id: Optional[str] = None,
    initial_metadata: Optional[Dict[str, Any]] = None,
    initial_payload: Optional[Dict[str, Any]] = None,
) -> Generator[TraceContext, None, None]:
    config = get_trace_config()
    if not getattr(config, "enabled", False):
        disabled_ctx: TraceContext = {
            "session_id": session_id,
            "agent_id": agent_id,
            "metadata": initial_metadata.copy() if initial_metadata else {},
            "payload": initial_payload.copy() if initial_payload else {},
            "exit_code": 0,
            "status": "success",
        }
        yield disabled_ctx
        return

    start_time = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    trace_id = str(uuid.uuid4())

    ctx: TraceContext = {
        "session_id": session_id,
        "agent_id": agent_id,
        "metadata": initial_metadata.copy() if initial_metadata else {},
        "payload": initial_payload.copy() if initial_payload else {},
        "exit_code": 0,
        "status": "success",
    }

    sampling_rate = getattr(config, "sampling_rate", 1.0)
    sampled_in = not (sampling_rate < 1.0 and random.random() >= sampling_rate)

    langfuse_client = get_langfuse_client(config) if sampled_in else None
    obs_context = None

    if langfuse_client is not None:
        try:
            payload_mode = getattr(config, "payload_mode", "metadata")
            max_payload_chars = getattr(config, "max_payload_chars", 10000)
            formatted_payload = format_payload(ctx["payload"], payload_mode, max_payload_chars)

            obs_context = langfuse_client.start_as_current_observation(
                name="memtomem_session_command",
                as_type="span",
                input=formatted_payload,
            )
        except Exception as e:
            logger.warning("Failed to start Langfuse observation: %s", e)
            obs_context = None

    if obs_context is not None:
        command_exception = None
        span = None
        try:
            propagate_attributes: Any = None
            try:
                from langfuse import propagate_attributes
            except Exception as exc:
                logger.warning("Failed to import Langfuse propagate_attributes: %s", exc)
                propagate_attributes = None

            try:
                span = obs_context.__enter__()
            except Exception as exc:
                logger.warning("Failed to enter Langfuse observation context: %s", exc)
                span = None

            if span is not None:
                clean_prop_metadata = {}
                try:
                    _mode = getattr(config, "payload_mode", "metadata")
                    clean_prop_metadata = format_propagated_metadata(
                        _redact_metadata(ctx["metadata"], _mode)
                    )
                except Exception as exc:
                    logger.warning("Failed to format propagated metadata: %s", exc)

                prop_ctx = None
                if propagate_attributes is not None:
                    try:
                        prop_ctx = propagate_attributes(
                            session_id=ctx.get("session_id") or f"no-session-{agent_id}",
                            user_id=ctx.get("agent_id") or agent_id,
                            metadata=clean_prop_metadata,
                        )
                        prop_ctx.__enter__()
                    except Exception as exc:
                        logger.warning("Failed to enter propagate_attributes context: %s", exc)
                        prop_ctx = None

                try:
                    yield ctx
                except Exception as exc:
                    command_exception = exc
                    ctx["status"] = "error"
                    if not ctx.get("exit_code"):
                        ctx["exit_code"] = 1
                    if "error" not in ctx["metadata"]:
                        ctx["metadata"]["error"] = str(exc)
                finally:
                    if prop_ctx is not None:
                        try:
                            prop_ctx.__exit__(None, None, None)
                        except Exception as exc:
                            logger.warning("Failed to exit propagate_attributes context: %s", exc)

                # Finalize telemetry
                try:
                    final_session_id = ctx.get("session_id") or f"no-session-{agent_id}"
                    final_agent_id = ctx.get("agent_id") or agent_id
                    _final_mode = getattr(config, "payload_mode", "metadata")
                    final_clean_metadata = format_propagated_metadata(
                        _redact_metadata(ctx["metadata"], _final_mode)
                    )

                    final_prop_ctx = None
                    if propagate_attributes is not None:
                        try:
                            final_prop_ctx = propagate_attributes(
                                session_id=final_session_id,
                                user_id=final_agent_id,
                                metadata=final_clean_metadata,
                            )
                            final_prop_ctx.__enter__()
                        except Exception as exc:
                            logger.warning(
                                "Failed to enter final propagate_attributes context: %s", exc
                            )

                    try:
                        pass
                    finally:
                        if final_prop_ctx is not None:
                            try:
                                final_prop_ctx.__exit__(None, None, None)
                            except Exception as exc:
                                logger.warning(
                                    "Failed to exit final propagate_attributes context: %s", exc
                                )

                    payload_mode = getattr(config, "payload_mode", "metadata")
                    max_payload_chars = getattr(config, "max_payload_chars", 10000)
                    final_payload = format_payload(ctx["payload"], payload_mode, max_payload_chars)
                    final_output = {
                        "exit_code": ctx.get("exit_code", 0),
                        "status": ctx.get("status", "success"),
                        "metadata": _redact_metadata(ctx.get("metadata", {}), payload_mode),
                        "payload": final_payload,
                    }
                    span.update(output=final_output)
                except Exception as telemetry_exc:
                    logger.warning(
                        "Langfuse telemetry finalization error (suppressed): %s", telemetry_exc
                    )

                try:
                    obs_context.__exit__(None, None, None)
                except Exception as exc:
                    logger.warning("Failed to exit Langfuse observation context: %s", exc)

            else:
                # Fallback run of command if we failed to obtain span
                try:
                    yield ctx
                except Exception as exc:
                    command_exception = exc
                    ctx["status"] = "error"
                    if not ctx.get("exit_code"):
                        ctx["exit_code"] = 1
                    if "error" not in ctx["metadata"]:
                        ctx["metadata"]["error"] = str(exc)

            if command_exception is not None:
                raise command_exception

        except Exception as exc:
            if exc is command_exception:
                raise
            logger.warning("Langfuse telemetry error (suppressed): %s", exc)
            if command_exception is not None:
                raise command_exception
        finally:
            try:
                if langfuse_client is not None:
                    langfuse_client.flush()
            except Exception as e:
                logger.warning("Failed to flush Langfuse client: %s", e)

            end_time = time.perf_counter()
            ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            duration_ms = (end_time - start_time) * 1000.0

            _write_local_jsonl(
                config=config,
                trace_id=trace_id,
                session_id=ctx.get("session_id"),
                agent_id=ctx.get("agent_id") or agent_id,
                command=command,
                event_type=event_type,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                status=ctx["status"],
                exit_code=ctx.get("exit_code", 0),
                metadata=ctx["metadata"],
                payload=ctx["payload"],
            )
    else:
        try:
            yield ctx
        except Exception as exc:
            ctx["status"] = "error"
            if not ctx.get("exit_code"):
                ctx["exit_code"] = 1
            if "error" not in ctx["metadata"]:
                ctx["metadata"]["error"] = str(exc)
            raise
        finally:
            end_time = time.perf_counter()
            ended_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            duration_ms = (end_time - start_time) * 1000.0

            _write_local_jsonl(
                config=config,
                trace_id=trace_id,
                session_id=ctx.get("session_id"),
                agent_id=ctx.get("agent_id") or agent_id,
                command=command,
                event_type=event_type,
                started_at=started_at,
                ended_at=ended_at,
                duration_ms=duration_ms,
                status=ctx["status"],
                exit_code=ctx.get("exit_code", 0),
                metadata=ctx["metadata"],
                payload=ctx["payload"],
            )


def _write_local_jsonl(
    config: Any,
    trace_id: str,
    session_id: Optional[str],
    agent_id: str,
    command: str,
    event_type: str,
    started_at: str,
    ended_at: str,
    duration_ms: float,
    status: str,
    exit_code: int,
    metadata: Dict[str, Any],
    payload: Dict[str, Any],
) -> None:
    if not getattr(config, "jsonl_enabled", True):
        return

    payload_mode = getattr(config, "payload_mode", "metadata")
    max_payload_chars = getattr(config, "max_payload_chars", 10000)
    jsonl_path_raw = getattr(config, "jsonl_path", "~/.memtomem/traces/session-traces.jsonl")
    try:
        # Redaction + serialization run inside the guard: a pathological value
        # (a failing __str__, a self-referential structure) must not let a
        # telemetry failure escape here and override the wrapped command result.
        final_payload = format_payload(payload, payload_mode, max_payload_chars)
        # ``command`` and other metadata can carry secrets — scrub before write.
        safe_metadata = _redact_metadata(metadata, payload_mode)

        jsonl_path = Path(jsonl_path_raw).expanduser().resolve()
        jsonl_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(jsonl_path.parent, 0o700)

        row: TraceRow = {
            "trace_id": trace_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "command": command,
            "event_type": event_type,
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": round(duration_ms, 2),
            "status": status,
            "exit_code": exit_code,
            "metadata": safe_metadata,
            "payload": final_payload,
        }
        flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(jsonl_path, flags, 0o600)
        try:
            # ``os.open(..., 0o600)`` already sets the mode; the ``fchmod`` is
            # belt-and-suspenders against a permissive umask. Windows Python
            # < 3.13 lacks ``os.fchmod`` (POSIX-only), so guard it the same way
            # ``provenance.py`` and ``context/_atomic.py`` do — otherwise the
            # trace write raises ``AttributeError`` and every trace silently
            # fails on Windows. NTFS ignores POSIX modes beyond the read-only
            # bit anyway, so skipping ``fchmod`` there loses nothing.
            if hasattr(os, "fchmod"):
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as f:
                fd = -1
                f.write(json.dumps(row, default=str) + "\n")
        finally:
            if fd >= 0:
                os.close(fd)
    except Exception as e:
        logger.warning("Failed to write trace to JSONL path %s: %s", jsonl_path_raw, e)
