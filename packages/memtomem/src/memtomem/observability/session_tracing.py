import json
import logging
import random
import re
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Generator, Optional

logger = logging.getLogger(__name__)

_trace_config: Optional[Any] = None
_langfuse_client: Optional[Any] = None


def get_trace_config(*, force_reload: bool = False) -> Any:
    global _trace_config
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
            pass
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
    else:
        return val


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
        serialized = serialized[: max_chars - 14] + "...[TRUNCATED]"

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
) -> Generator[Dict[str, Any], None, None]:
    config = get_trace_config()
    if not getattr(config, "enabled", False):
        ctx = {
            "session_id": session_id,
            "agent_id": agent_id,
            "metadata": initial_metadata.copy() if initial_metadata else {},
            "payload": initial_payload.copy() if initial_payload else {},
            "exit_code": 0,
            "status": "success",
        }
        yield ctx
        return

    start_time = time.perf_counter()
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    trace_id = str(uuid.uuid4())

    ctx = {
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
                    clean_prop_metadata = format_propagated_metadata(ctx["metadata"])
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
                    final_clean_metadata = format_propagated_metadata(ctx["metadata"])

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
                        "metadata": ctx.get("metadata", {}),
                        "payload": final_payload,
                    }
                    span.update(output=final_output)
                except Exception as telemetry_exc:
                    logger.warning(
                        "Langfuse telemetry finalization error (suppressed): %s", telemetry_exc
                    )

                try:
                    span.__exit__(None, None, None)
                except Exception as exc:
                    logger.warning("Failed to exit Langfuse span context: %s", exc)

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
    final_payload = format_payload(payload, payload_mode, max_payload_chars)

    jsonl_path_raw = getattr(config, "jsonl_path", "~/.memtomem/traces/session-traces.jsonl")
    try:
        jsonl_path = Path(jsonl_path_raw).expanduser().resolve()
        jsonl_path.parent.mkdir(parents=True, exist_ok=True)

        row = {
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
            "metadata": metadata,
            "payload": final_payload,
        }
        with open(jsonl_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(row, default=str) + "\n")
    except Exception as e:
        logger.warning("Failed to write trace to JSONL path %s: %s", jsonl_path_raw, e)
