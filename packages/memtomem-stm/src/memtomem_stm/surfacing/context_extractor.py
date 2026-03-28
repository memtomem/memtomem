"""Extract search queries from MCP tool call arguments."""

from __future__ import annotations

import re
from typing import Any

from memtomem_stm.surfacing.config import SurfacingConfig


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HEX_RE = re.compile(r"^[0-9a-f]{24,}$", re.I)
_SEMANTIC_KEYS = {"query", "search", "path", "file", "url", "topic", "name", "title", "description"}
_PATH_KEYS = {"path", "file", "filepath", "file_path", "filename"}


class ContextExtractor:
    """Extract a search query from tool call context."""

    def extract_query(
        self,
        server: str,
        tool: str,
        arguments: dict[str, Any],
        config: SurfacingConfig,
    ) -> str | None:
        # 1. Per-tool template
        tool_cfg = config.context_tools.get(tool)
        if tool_cfg and tool_cfg.query_template:
            return self._apply_template(tool_cfg.query_template, server, tool, arguments)

        # 2. Agent-provided context
        if "_context_query" in arguments:
            cq = arguments["_context_query"]
            if isinstance(cq, str) and cq.strip():
                return cq.strip()

        # 3. Heuristic extraction — prioritize argument values over tool name
        parts: list[str] = []

        for key, value in arguments.items():
            if key.startswith("_"):
                continue
            if isinstance(value, str) and len(value) > 2 and not self._is_identifier(value):
                # Tokenize pure file paths into meaningful words
                # Only tokenize if value looks like a path (no spaces)
                if key in _PATH_KEYS and ("/" in value or "\\" in value) and " " not in value:
                    parts.append(self._tokenize_path(value))
                else:
                    parts.append(self._first_sentence(value, max_chars=200))
            elif key in _SEMANTIC_KEYS:
                parts.append(str(value))

        # Fall back to tool name only if no semantic args found
        if not parts:
            parts.append(tool.replace("_", " "))

        query = " ".join(parts).strip()
        if len(query.split()) < config.min_query_tokens:
            return None
        return query

    def _apply_template(
        self,
        template: str,
        server: str,
        tool: str,
        arguments: dict[str, Any],
    ) -> str:
        result = template.replace("{tool_name}", tool).replace("{server}", server)
        for key, value in arguments.items():
            result = result.replace(f"{{arg.{key}}}", str(value))
        return result.strip()

    @staticmethod
    def _is_identifier(value: str) -> bool:
        if _UUID_RE.match(value):
            return True
        if _HEX_RE.match(value):
            return True
        if value.lower() in ("true", "false", "null", "none"):
            return True
        return False

    @staticmethod
    def _tokenize_path(path: str) -> str:
        """Convert a file path into space-separated meaningful tokens.

        /src/auth/jwt_handler.py → "src auth jwt handler py"
        """
        # Strip leading slashes and split by / . _ -
        parts = re.split(r"[/._\-]+", path.strip("/"))
        # Filter out empty, very short, or purely numeric parts
        tokens = [p for p in parts if len(p) > 1 and not p.isdigit()]
        return " ".join(tokens)

    @staticmethod
    def _first_sentence(text: str, max_chars: int = 200) -> str:
        text = text[: max_chars * 2]
        text = text.replace("\n", " ").strip()
        for delim in (". ", "! ", "? ", "\n"):
            idx = text.find(delim)
            if 0 < idx < max_chars:
                return text[: idx + 1]
        return text[:max_chars]
