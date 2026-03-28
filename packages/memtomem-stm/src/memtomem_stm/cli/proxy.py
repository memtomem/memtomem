"""CLI commands for managing the memtomem-stm proxy gateway."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import click

_DEFAULT_CONFIG = Path("~/.memtomem/stm_proxy.json")


def _load(config_path: Path) -> dict[str, Any]:
    resolved = config_path.expanduser().resolve()
    if not resolved.exists():
        return {"enabled": True, "upstream_servers": {}}
    try:
        return json.loads(resolved.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, ValueError) as exc:
        click.echo(f"Error: Failed to parse {resolved}: {exc}", err=True)
        raise SystemExit(1) from exc


def _save(config_path: Path, data: dict[str, Any]) -> None:
    resolved = config_path.expanduser().resolve()
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


@click.group()
def cli() -> None:
    """memtomem-stm proxy gateway management."""


@cli.command()
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
def status(config_path: str) -> None:
    """Show proxy gateway configuration and server list."""
    path = Path(config_path)
    resolved = path.expanduser().resolve()

    if not resolved.exists():
        click.echo(f"Config not found: {resolved}")
        click.echo("Run `memtomem-stm-proxy add` to create a configuration.")
        return

    data = _load(path)
    enabled = data.get("enabled", False)
    servers: dict[str, Any] = data.get("upstream_servers", {})

    click.echo(f"Config : {resolved}")
    click.echo(f"Enabled: {'yes' if enabled else 'no'}")
    click.echo(f"Servers: {len(servers)}")

    if servers:
        click.echo("")
        for name, cfg in servers.items():
            transport = cfg.get("transport", "stdio")
            prefix = cfg.get("prefix", "")
            if transport == "stdio":
                cmd = cfg.get("command", "")
                args_str = " ".join(cfg.get("args", []))
                detail = f"{cmd} {args_str}".strip()
            else:
                detail = cfg.get("url", "")
            click.echo(f"  {name:<20} prefix={prefix}  [{transport}] {detail}")


@cli.command(name="list")
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
def list_servers(config_path: str) -> None:
    """List configured upstream servers."""
    data = _load(Path(config_path))
    servers: dict[str, Any] = data.get("upstream_servers", {})

    if not servers:
        click.echo("No upstream servers configured.")
        return

    click.echo(f"{'NAME':<20} {'PREFIX':<10} {'TRANSPORT':<12} COMMAND / URL")
    click.echo("-" * 70)
    for name, cfg in servers.items():
        transport = cfg.get("transport", "stdio")
        prefix = cfg.get("prefix", "")
        if transport == "stdio":
            cmd = cfg.get("command", "")
            args_str = " ".join(cfg.get("args", []))
            detail = f"{cmd} {args_str}".strip()
        else:
            detail = cfg.get("url", "")
        click.echo(f"{name:<20} {prefix:<10} {transport:<12} {detail}")


@cli.command()
@click.argument("name")
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--command", "command", default="", help="Executable command (stdio).")
@click.option("--args", "args_str", default="", help="Space-separated arguments.")
@click.option("--prefix", required=True, help="Tool name prefix (e.g. 'fs').")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "sse", "streamable_http"]),
    default="stdio",
    show_default=True,
)
@click.option("--url", default="", help="Endpoint URL (SSE / HTTP).")
@click.option("--env", "env_pairs", multiple=True, metavar="KEY=VALUE")
@click.option(
    "--compression",
    type=click.Choice(["none", "truncate", "selective", "hybrid"]),
    default="hybrid",
    show_default=True,
)
@click.option("--max-chars", "max_result_chars", type=int, default=8000, show_default=True)
def add(
    name: str,
    config_path: str,
    command: str,
    args_str: str,
    prefix: str,
    transport: str,
    url: str,
    env_pairs: tuple[str, ...],
    compression: str,
    max_result_chars: int,
) -> None:
    """Add an upstream MCP server to the proxy configuration."""
    path = Path(config_path)
    data = _load(path)
    servers: dict[str, Any] = data.setdefault("upstream_servers", {})

    if name in servers:
        click.echo(f"Error: server '{name}' already exists. Use `remove` first.", err=True)
        sys.exit(1)

    entry: dict[str, Any] = {
        "prefix": prefix,
        "transport": transport,
        "compression": compression,
        "max_result_chars": max_result_chars,
    }
    if transport == "stdio":
        entry["command"] = command
        if args_str:
            entry["args"] = args_str.split()
    else:
        entry["url"] = url

    if env_pairs:
        env_dict: dict[str, str] = {}
        for pair in env_pairs:
            if "=" not in pair:
                click.echo(f"Error: --env must be KEY=VALUE, got: {pair}", err=True)
                sys.exit(1)
            k, v = pair.split("=", 1)
            env_dict[k] = v
        entry["env"] = env_dict

    servers[name] = entry
    _save(path, data)
    click.echo(f"Added server '{name}' (prefix={prefix})")


@cli.command()
@click.argument("name")
@click.option("--config", "config_path", default=str(_DEFAULT_CONFIG), show_default=True)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation.")
def remove(name: str, config_path: str, yes: bool) -> None:
    """Remove an upstream MCP server from the proxy configuration."""
    path = Path(config_path)
    data = _load(path)
    servers: dict[str, Any] = data.get("upstream_servers", {})

    if name not in servers:
        click.echo(f"Error: server '{name}' not found.", err=True)
        sys.exit(1)

    if not yes:
        click.confirm(f"Remove server '{name}'?", abort=True)

    del servers[name]
    _save(path, data)
    click.echo(f"Removed server '{name}'.")
