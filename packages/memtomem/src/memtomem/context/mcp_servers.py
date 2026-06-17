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
import logging
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from memtomem import privacy
from memtomem.context import _skip_reasons as skip_codes
from memtomem.context._atomic import atomic_write_text
from memtomem.context._names import InvalidNameError, validate_name
from memtomem.context._runtime_targets import DiffRow

logger = logging.getLogger(__name__)

CANONICAL_MCP_SERVER_ROOT = ".memtomem/mcp-servers"
PROJECT_MCP_CONFIG = ".mcp.json"
MCP_RUNTIME = "project_mcp"


class McpServerParseError(ValueError):
    """Raised when a canonical MCP server definition cannot be parsed.

    ``safe_message`` is a path-free rendering of the failure for the web
    trust boundary (#1412). The JSON-decode raise embeds the absolute,
    ``.resolve()``'d source ``Path`` in the default message — useful on the
    CLI / MCP surfaces, but a ``$HOME``/username disclosure over the loopback
    dashboard (the #1385 finding-1 class, on the parse branch). Web catch
    sites render ``exc.safe_message`` (basename + the JSON problem, no host
    path); CLI / MCP keep ``str(exc)`` with the full path. The validation-shape
    raises already name only the server (``'{name}'``), never a path, so
    ``safe_message`` defaults to the full message.
    """

    def __init__(self, message: str, *, safe_message: str | None = None) -> None:
        super().__init__(message)
        self.safe_message = message if safe_message is None else safe_message


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
        try:
            validate_name(path.stem, kind="MCP server")
        except InvalidNameError as exc:
            # One stray invalid-named file must not abort the whole panel /
            # diff / sync (#1247 id 40) — skip for sync like skills, while
            # diff_mcp_servers surfaces the dedicated "invalid name" row.
            logger.warning("skip canonical MCP server %r: invalid name (%s)", path.name, exc)
            continue
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
        raise McpServerParseError(
            f"invalid JSON in {source}: {exc.msg}",
            # Path-free twin for the loopback web boundary (#1412): the default
            # message embeds the resolved source path ($HOME/username leak over
            # the dashboard), so web catch sites render this basename form. The
            # JSON-decode ``exc.msg`` ("Expecting ...") never carries the path.
            safe_message=f"invalid JSON in {source.name}: {exc.msg}",
        ) from exc
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


def _parse_project_mcp_text(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpServerParseError(f"invalid JSON in {PROJECT_MCP_CONFIG}: {exc.msg}") from exc
    if not isinstance(data, dict):
        raise McpServerParseError(f"{PROJECT_MCP_CONFIG} must contain a JSON object")
    mcp_servers = data.get("mcpServers")
    if mcp_servers is not None and not isinstance(mcp_servers, dict):
        raise McpServerParseError(f"{PROJECT_MCP_CONFIG} field 'mcpServers' must be an object")
    return data


def _read_project_mcp_config(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return _parse_project_mcp_text(path.read_text(encoding="utf-8"))


def diff_mcp_servers(project_root: Path) -> list[tuple[str, str, str]]:
    target = _project_mcp_path(project_root)
    target_parse_reason: str | None = None
    try:
        target_config = _read_project_mcp_config(target)
        target_servers = target_config.get("mcpServers") or {}
    except McpServerParseError as exc:
        target_servers = None
        # A broken .mcp.json marks EVERY canonical row "parse error" — the
        # reason must say so, or the user chases N healthy canonical files
        # (#1229 U7).
        target_parse_reason = str(exc)

    # Canonical-side invalid names: list_canonical_mcp_servers filters them
    # out for SYNC (fan-out must never propagate an invalid name), which would
    # make them fully invisible — enumerate them here for the dedicated
    # "invalid name" row, mirroring skills/commands/agents (#1243 / #1247
    # id 40). Runtime-side invalid keys join the same dict below; on a
    # name collision the runtime-side reason wins, matching diff_skills.
    invalid_by_name: dict[str, str] = {}
    root = canonical_mcp_server_root(project_root)
    if root.is_dir():
        for path in sorted(root.glob("*.json")):
            if not path.is_file():
                continue
            try:
                validate_name(path.stem, kind="MCP server")
            except InvalidNameError as exc:
                invalid_by_name[path.stem] = str(exc)

    rows: list[tuple[str, str, str]] = []
    canonical_names: set[str] = set()
    for path in list_canonical_mcp_servers(project_root):
        name = path.stem
        canonical_names.add(name)
        try:
            canonical = parse_canonical_mcp_server(path).definition
        except McpServerParseError as exc:
            rows.append(DiffRow(MCP_RUNTIME, name, "parse error", str(exc)))
            continue
        if target_servers is None:
            rows.append(DiffRow(MCP_RUNTIME, name, "parse error", target_parse_reason))
            continue
        if name not in target_servers:
            rows.append((MCP_RUNTIME, name, "missing target"))
            continue
        status = "in sync" if target_servers.get(name) == canonical else "out of sync"
        rows.append((MCP_RUNTIME, name, status))

    # Runtime side (#1247 id 31): .mcp.json entries with no canonical were
    # invisible end-to-end — the panel implied no servers beyond canonicals.
    # Valid runtime-only keys get the family-standard "missing canonical"
    # row; invalid keys ride the "invalid name" dict. Skipped entirely when
    # .mcp.json failed to parse (the per-canonical parse-error rows above
    # already name the broken file).
    if target_servers is not None:
        for raw_name in target_servers:
            if raw_name in canonical_names:
                continue
            try:
                validate_name(raw_name, kind="MCP server")
            except InvalidNameError as exc:
                invalid_by_name[raw_name] = str(exc)
                continue
            rows.append((MCP_RUNTIME, raw_name, "missing canonical"))

    for raw_name in sorted(invalid_by_name):
        rows.append(DiffRow(MCP_RUNTIME, raw_name, "invalid name", invalid_by_name[raw_name]))
    return rows


@dataclass(frozen=True)
class McpServerSyncResult:
    """``generated`` rows are ``(runtime, server_name, path)`` — the name is
    load-bearing because every server fans into the SAME ``.mcp.json`` file;
    nameless ``(runtime, path)`` rows rendered as N identical duplicates
    (#1247 id 42)."""

    generated: list[tuple[str, str, Path]]
    skipped: list[tuple[str, str, str]]


def generate_all_mcp_servers(
    project_root: Path, *, surface: str = "context_mcp_servers_sync"
) -> McpServerSyncResult:
    """Fan canonical MCP server definitions out into the project ``.mcp.json``.

    ``surface`` names the calling fan-out surface for Gate A audit
    attribution only (web ``/context/mcp-servers/sync``, the CLI
    ``mm context sync --include=mcp-servers`` leg, ...); it does not
    change the merge or write behavior. Callers pass their own surface so
    the privacy-scan audit trail names the real entry point.
    """
    paths = list_canonical_mcp_servers(project_root)
    if not paths:
        return McpServerSyncResult(
            generated=[],
            skipped=[
                (
                    MCP_RUNTIME,
                    "No canonical MCP server definitions found",
                    skip_codes.NO_CANONICAL_ROOT,
                )
            ],
        )

    definitions: dict[str, dict[str, Any]] = {}
    for path in paths:
        text = path.read_text(encoding="utf-8")
        scan_mcp_server_text(
            text,
            source_path=path,
            project_root=project_root,
            surface=surface,
        )
        parsed = parse_mcp_server_text(text, name=path.stem, source=path)
        definitions[parsed.name] = parsed.definition

    target = _project_mcp_path(project_root)
    current_text = target.read_text(encoding="utf-8") if target.exists() else None
    config = _parse_project_mcp_text(current_text) if current_text is not None else {}
    mcp_servers = dict(config.get("mcpServers") or {})
    for name, definition in definitions.items():
        mcp_servers[name] = definition
    config["mcpServers"] = mcp_servers
    merged_text = json.dumps(config, indent=2, sort_keys=False) + "\n"

    if merged_text == current_text:
        # Nothing to change — rewriting anyway churned mtime, reflowed user
        # formatting, and chmodded the file every run (#1247 id 43). Typed
        # skip, not silent: an MCP-only project that is fully in sync must
        # not read as "nothing to sync" in the Sync All no-op detection.
        return McpServerSyncResult(
            generated=[],
            skipped=[
                (
                    MCP_RUNTIME,
                    f"all {len(definitions)} canonical server(s) already in sync "
                    f"with {PROJECT_MCP_CONFIG}",
                    skip_codes.IN_SYNC,
                )
            ],
        )

    # Mode policy (Codex design gate): a NEW file holds only Gate-A-scanned
    # canonical content → 0o644 like every other fan-out target. A REWRITE
    # preserves the existing mode in both directions — the merge carries
    # foreign ``mcpServers`` entries verbatim without scanning them, so
    # widening a user's 0600 file could expose unscanned secret env values,
    # and forcing 0600 onto a 0644 file was the original id 43 complaint.
    mode = 0o644 if current_text is None else stat.S_IMODE(target.stat().st_mode)
    atomic_write_text(target, merged_text, mode=mode)

    return McpServerSyncResult(
        generated=[(MCP_RUNTIME, name, target) for name in definitions],
        skipped=[],
    )
