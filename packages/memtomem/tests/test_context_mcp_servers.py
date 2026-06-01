from __future__ import annotations

import json
from pathlib import Path

import pytest

from memtomem.context.mcp_servers import (
    McpServerParseError,
    diff_mcp_servers,
    generate_all_mcp_servers,
    parse_canonical_mcp_server,
)


def _canonical(root: Path, name: str, definition: dict) -> Path:
    path = root / ".memtomem" / "mcp-servers" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(definition, indent=2) + "\n", encoding="utf-8")
    return path


def test_parse_requires_command_string(tmp_path: Path) -> None:
    path = _canonical(tmp_path, "demo", {"args": []})
    with pytest.raises(McpServerParseError, match="command"):
        parse_canonical_mcp_server(path)


def test_rejects_network_transport_definition(tmp_path: Path) -> None:
    """v1 accepts only stdio servers. A network (type/url SSE/HTTP) definition
    is rejected, and the message names the stdio limitation so it does not read
    as a generic schema bug."""
    path = _canonical(tmp_path, "remote", {"type": "http", "url": "https://example.com/mcp"})
    with pytest.raises(McpServerParseError, match="stdio"):
        parse_canonical_mcp_server(path)


def test_sync_merges_project_mcp_json_without_clobbering_other_entries(tmp_path: Path) -> None:
    _canonical(
        tmp_path,
        "demo",
        {"command": "uvx", "args": ["--from", "demo", "demo-server"]},
    )
    mcp_json = tmp_path / ".mcp.json"
    mcp_json.write_text(
        json.dumps(
            {
                "comment": "keep me",
                "mcpServers": {
                    "existing": {"command": "node", "args": ["server.js"]},
                    "demo": {"command": "old"},
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    result = generate_all_mcp_servers(tmp_path)

    assert result.skipped == []
    assert result.generated == [("project_mcp", mcp_json.resolve())]
    written = json.loads(mcp_json.read_text(encoding="utf-8"))
    assert written["comment"] == "keep me"
    assert written["mcpServers"]["existing"] == {"command": "node", "args": ["server.js"]}
    assert written["mcpServers"]["demo"] == {
        "command": "uvx",
        "args": ["--from", "demo", "demo-server"],
    }


def test_diff_reports_missing_and_in_sync(tmp_path: Path) -> None:
    definition = {"command": "uvx", "args": ["demo"]}
    _canonical(tmp_path, "demo", definition)

    assert diff_mcp_servers(tmp_path) == [("project_mcp", "demo", "missing target")]

    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"demo": definition}}, indent=2) + "\n",
        encoding="utf-8",
    )
    assert diff_mcp_servers(tmp_path) == [("project_mcp", "demo", "in sync")]


def test_no_canonical_root_returns_empty_skip(tmp_path: Path) -> None:
    result = generate_all_mcp_servers(tmp_path)
    assert result.generated == []
    assert result.skipped == [
        ("project_mcp", "No canonical MCP server definitions found", "no_canonical_root")
    ]
