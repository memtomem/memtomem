"""Surfacing configuration models."""

from __future__ import annotations

from pydantic import BaseModel


class ToolSurfacingConfig(BaseModel):
    """Per-tool override for surfacing behavior."""

    enabled: bool = True
    query_template: str = ""
    namespace: str | None = None
    min_score: float | None = None
    max_results: int | None = None


class SurfacingConfig(BaseModel):
    """Proactive memory surfacing configuration."""

    enabled: bool = True
    ltm_mode: str = "in_process"  # "in_process" | "mcp_client"
    ltm_mcp_command: str = "memtomem-server"
    ltm_mcp_args: list[str] = []
    min_score: float = 0.02
    max_results: int = 3
    min_query_tokens: int = 3
    cooldown_seconds: float = 5.0
    timeout_seconds: float = 3.0
    injection_mode: str = "prepend"  # prepend | append | section
    section_header: str = "## Relevant Memories"
    default_namespace: str | None = None
    exclude_tools: list[str] = []
    write_tool_patterns: list[str] = [
        "*write*",
        "*create*",
        "*delete*",
        "*push*",
        "*send*",
        "*remove*",
    ]
    context_tools: dict[str, ToolSurfacingConfig] = {}
    feedback_enabled: bool = True
    max_surfacings_per_minute: int = 15
    cache_ttl_seconds: float = 60.0
    circuit_max_failures: int = 3
    circuit_reset_seconds: float = 60.0
    auto_tune_enabled: bool = True
    auto_tune_min_samples: int = 20
    auto_tune_score_increment: float = 0.002
    min_response_chars: int = 5000
    include_session_context: bool = True
    fire_webhook: bool = True
    max_injection_chars: int = 2000
