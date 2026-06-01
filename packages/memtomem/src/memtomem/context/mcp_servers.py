"""Canonical MCP server definitions for the Context Gateway.

v1 is intentionally narrow on two axes:

- **Residency.** Canonical definitions live under
  ``.memtomem/mcp-servers/<name>.json`` and fan out only to the project
  ``.mcp.json`` file's ``mcpServers`` object. User-home client configs and
  reverse import are left to a follow-up because they need stronger
  host-write and secret-handling policy.
- **Transport.** Only stdio servers (a non-empty ``command`` field) are
  accepted. Network transports — the ``type``/``url`` SSE/HTTP shape added
  in v0.2.2 — are rejected by :func:`validate_mcp_server_definition` and are
  a deliberate follow-up, not an oversight.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memtomem import privacy
from memtomem.context._atomic import atomic_write_text
from memtomem.context._names import validate_name

CANONICAL_MCP_SERVER_ROOT = ".memtomem/mcp-servers"
PROJECT_MCP_CONFIG = ".mcp.json"
MCP_RUNTIME = "project_mcp"


class McpServerParseError(ValueError):
    """Raised when a canonical MCP server definition cannot be parsed."""


class McpServerPrivacyError(ValueError):
    """Raised when a write/sync would propagate secret-shaped content."""


@dataclass(frozen=True)
class McpServerDefinition:
    name: str
    definition: dict[str, Any]
    path: Path


def canonical_mcp_server_root(project_root: Path) -> Path:
    return (project_root / CANONICAL_MCP_SERVER_ROOT).resolve()


def canonical_mcp_server_path(project_root: Path, raw_name: str) -> Path:
    name = validate_name(raw_name, kind="MCP server")
    return canonical_mcp_server_root(project_root) / f"{name}.json"


def list_canonical_mcp_servers(project_root: Path) -> list[Path]:
    root = canonical_mcp_server_root(project_root)
    if not root.is_dir():
        return []
    out: list[Path] = []
    for path in sorted(root.glob("*.json")):
        if not path.is_file():
            continue
        validate_name(path.stem, kind="MCP server")
        out.append(path)
    return out


def validate_mcp_server_definition(data: object, *, name: str) -> dict[str, Any]:
    """Validate a stdio MCP server definition.

    v1 accepts only stdio servers (a non-empty ``command``, optional ``args`` /
    ``env``). Network ``type``/``url`` SSE/HTTP definitions are rejected here by
    design — see the module docstring; the error names the limitation so the
    user does not read it as a generic schema bug.
    """
    if not isinstance(data, dict):
        raise McpServerParseError(f"MCP server '{name}' must be a JSON object")
    command = data.get("command")
    if not isinstance(command, str) or not command.strip():
        raise McpServerParseError(
            f"MCP server '{name}' requires a non-empty string field 'command'. "
            "Only stdio servers are supported in this release; network "
            "(SSE/HTTP) transports with 'type'/'url' are not yet accepted."
        )
    args = data.get("args")
    if args is not None and (
        not isinstance(args, list) or any(not isinstance(item, str) for item in args)
    ):
        raise McpServerParseError(f"MCP server '{name}' field 'args' must be an array of strings")
    env = data.get("env")
    if env is not None and (
        not isinstance(env, dict)
        or any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items())
    ):
        raise McpServerParseError(
            f"MCP server '{name}' field 'env' must be an object of string values"
        )
    return data


def parse_mcp_server_text(text: str, *, name: str, source: Path) -> McpServerDefinition:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpServerParseError(f"invalid JSON in {source}: {exc.msg}") from exc
    return McpServerDefinition(
        name=validate_name(name, kind="MCP server"),
        definition=validate_mcp_server_definition(data, name=name),
        path=source,
    )


def parse_canonical_mcp_server(path: Path) -> McpServerDefinition:
    return parse_mcp_server_text(path.read_text(encoding="utf-8"), name=path.stem, source=path)


def format_mcp_server_definition(definition: dict[str, Any]) -> str:
    return json.dumps(definition, indent=2, sort_keys=False) + "\n"


def scan_mcp_server_text(
    text: str,
    *,
    source_path: Path,
    project_root: Path,
    surface: str,
) -> None:
    guard = privacy.enforce_write_guard(
        text,
        surface=surface,
        force_unsafe=False,
        scope="project_shared",
        audit_context={
            "kind": "mcp_server",
            "scope": "project_shared",
            "path": str(source_path),
            "project_root": str(project_root),
        },
    )
    if guard.decision in ("blocked", "blocked_project_shared"):
        raise McpServerPrivacyError(
            f"Gate A: {source_path.name} contains {len(guard.hits)} privacy pattern hit(s); "
            "MCP server fan-out to project .mcp.json rejected."
        )


def _project_mcp_path(project_root: Path) -> Path:
    return (project_root / PROJECT_MCP_CONFIG).resolve()


def _read_project_mcp_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise McpServerParseError(f"invalid JSON in {PROJECT_MCP_CONFIG}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise McpServerParseError(f"{PROJECT_MCP_CONFIG} must contain a JSON object")
    mcp_servers = data.get("mcpServers")
    if mcp_servers is not None and not isinstance(mcp_servers, dict):
        raise McpServerParseError(f"{PROJECT_MCP_CONFIG} field 'mcpServers' must be an object")
    return data


def diff_mcp_servers(project_root: Path) -> list[tuple[str, str, str]]:
    target = _project_mcp_path(project_root)
    try:
        target_config = _read_project_mcp_config(target)
        target_servers = target_config.get("mcpServers") or {}
    except McpServerParseError:
        target_servers = None

    rows: list[tuple[str, str, str]] = []
    for path in list_canonical_mcp_servers(project_root):
        name = path.stem
        try:
            canonical = parse_canonical_mcp_server(path).definition
        except McpServerParseError:
            rows.append((MCP_RUNTIME, name, "parse error"))
            continue
        if target_servers is None:
            rows.append((MCP_RUNTIME, name, "parse error"))
            continue
        if name not in target_servers:
            rows.append((MCP_RUNTIME, name, "missing target"))
            continue
        status = "in sync" if target_servers.get(name) == canonical else "out of sync"
        rows.append((MCP_RUNTIME, name, status))
    return rows


@dataclass(frozen=True)
class McpServerSyncResult:
    generated: list[tuple[str, Path]]
    skipped: list[tuple[str, str, str]]


def generate_all_mcp_servers(project_root: Path) -> McpServerSyncResult:
    paths = list_canonical_mcp_servers(project_root)
    if not paths:
        return McpServerSyncResult(
            generated=[],
            skipped=[
                (MCP_RUNTIME, "No canonical MCP server definitions found", "no_canonical_root")
            ],
        )

    definitions: dict[str, dict[str, Any]] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        scan_mcp_server_text(
            text,
            source_path=path,
            project_root=project_root,
            surface="web_context_mcp_servers_sync",
        )
        parsed = parse_mcp_server_text(text, name=path.stem, source=path)
        definitions[parsed.name] = parsed.definition

    target = _project_mcp_path(project_root)
    config = _read_project_mcp_config(target)
    mcp_servers = dict(config.get("mcpServers") or {})
    for name, definition in definitions.items():
        mcp_servers[name] = definition
    config["mcpServers"] = mcp_servers
    atomic_write_text(target, json.dumps(config, indent=2, sort_keys=False) + "\n")

    return McpServerSyncResult(
        generated=[(MCP_RUNTIME, target) for _name in definitions],
        skipped=[],
    )
