import time
import uuid
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Generator, Dict, Optional

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


def format_payload(payload: Any, mode: str, max_chars: int) -> Any:
    if mode == "metadata":
        return None
    try:
        serialized = json.dumps(payload, default=str)
    except Exception:
        serialized = str(payload)

    if mode == "redacted":
        if isinstance(payload, dict):
            redacted_payload = {}
            for k, v in payload.items():
                if any(
                    sec in k.lower() for sec in ["api_key", "secret", "password", "token", "key"]
                ):
                    redacted_payload[k] = "***"
                else:
                    redacted_payload[k] = v
            try:
                serialized = json.dumps(redacted_payload, default=str)
            except Exception:
                serialized = str(redacted_payload)
        else:
            serialized = re.sub(
                r'(?i)(api_key|secret|password|token|key)["\s:]+[\w\-]+', r'\1: "***"', serialized
            )

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

    import random

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
        try:
            from langfuse import propagate_attributes

            with obs_context as span:
                clean_prop_metadata = format_propagated_metadata(ctx["metadata"])

                with propagate_attributes(
                    session_id=ctx.get("session_id") or f"no-session-{agent_id}",
                    user_id=ctx.get("agent_id") or agent_id,
                    metadata=clean_prop_metadata,
                ):
                    yield ctx

                # If session_id/metadata updated during yield, propagate again to update the span
                final_session_id = ctx.get("session_id") or f"no-session-{agent_id}"
                final_agent_id = ctx.get("agent_id") or agent_id
                final_clean_metadata = format_propagated_metadata(ctx["metadata"])

                with propagate_attributes(
                    session_id=final_session_id,
                    user_id=final_agent_id,
                    metadata=final_clean_metadata,
                ):
                    pass

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
        except Exception as exc:
            ctx["status"] = "error"
            if not ctx.get("exit_code"):
                ctx["exit_code"] = 1
            if "error" not in ctx["metadata"]:
                ctx["metadata"]["error"] = str(exc)
            raise
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
