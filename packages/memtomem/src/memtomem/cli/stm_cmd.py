"""mm stm init — interactive STM proxy setup wizard."""

from __future__ import annotations

import importlib.util
import json
import platform
from pathlib import Path

import click

from memtomem.cli.wizard import nav_confirm, nav_prompt, run_steps, step_header


# ── MCP client config detection ──────────────────────────────────────


def _mcp_config_sources() -> list[tuple[str, Path, str]]:
    """Return (label, path, format) for known MCP client config files.

    format: "standard" = {mcpServers: {...}}, "claude_json" = ~/.claude.json with nested structure
    """
    home = Path.home()
    sources: list[tuple[str, Path, str]] = [
        ("Claude Code (user)", home / ".claude.json", "claude_json"),
        ("Cursor", home / ".cursor" / "mcp.json", "standard"),
        ("Gemini CLI", home / ".gemini" / "settings.json", "standard"),
        ("Project (.mcp.json)", Path.cwd() / ".mcp.json", "standard"),
    ]
    if platform.system() == "Darwin":
        sources.append(
            (
                "Claude Desktop",
                home / "Library/Application Support/Claude/claude_desktop_config.json",
                "standard",
            )
        )
    elif platform.system() == "Windows":
        appdata = Path.home() / "AppData" / "Roaming"
        sources.append(
            ("Claude Desktop", appdata / "Claude" / "claude_desktop_config.json", "standard")
        )
    sources.append(("Windsurf", home / ".codeium" / "windsurf" / "mcp_config.json", "standard"))
    return sources


def _read_mcp_servers(path: Path, fmt: str = "standard") -> dict:
    """Read mcpServers from an MCP config file.

    For claude_json format, reads top-level mcpServers (user scope)
    and current project's mcpServers, merged together.
    Filters out disabled servers.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}

    if fmt == "claude_json":
        servers = dict(data.get("mcpServers", {}))
        cwd = str(Path.cwd())
        for proj_path, proj_data in data.get("projects", {}).items():
            if cwd.startswith(proj_path) or proj_path == cwd:
                proj_servers = proj_data.get("mcpServers", {})
                if proj_servers:
                    servers.update(proj_servers)
        # Filter out disabled servers
        servers = {
            k: v for k, v in servers.items() if not (isinstance(v, dict) and v.get("disabled"))
        }
        return servers

    return data.get("mcpServers", {})


def _generate_prefix(name: str, existing: set[str]) -> str:
    """Generate a short prefix from a server name."""
    parts = name.replace("_", "-").split("-")
    if len(parts) >= 2:
        prefix = "".join(p[0] for p in parts if p)
    else:
        prefix = name[:2]
    prefix = prefix.lower()

    candidate = prefix
    counter = 2
    while candidate in existing:
        candidate = f"{prefix}{counter}"
        counter += 1
    return candidate


def _detect_transport(server_config: dict) -> tuple[str, str | None]:
    """Detect transport type and URL from server config.

    Claude Code uses: {"type": "http", "command": "https://..."}
    Standard MCP uses: {"command": "npx", "args": [...]} or {"url": "https://..."}

    Returns (transport, url_or_none).
    Transport values: "stdio", "sse", "streamable_http"
    """
    # Claude Code format: type field explicitly set
    cfg_type = server_config.get("type", "")
    if cfg_type in ("http", "sse", "streamable_http"):
        url = server_config.get("url") or server_config.get("command", "")
        if url.startswith(("http://", "https://")):
            # Claude Code "http" = Streamable HTTP, "sse" = SSE
            transport = "sse" if cfg_type == "sse" else "streamable_http"
            return transport, url
        return "streamable_http", None

    # Standard MCP format: url field
    if "url" in server_config:
        return "streamable_http", server_config["url"]

    # Check if args contain a URL
    for arg in server_config.get("args", []):
        if isinstance(arg, str) and arg.startswith(("http://", "https://")):
            return "streamable_http", arg

    return "stdio", None


# ── Step functions ────────────────────────────────────────────────────


def _step_detect_clients(state: dict) -> None:
    step_header(1, "Detect MCP Clients")

    found: list[tuple[str, Path, str, dict]] = []
    for label, path, fmt in _mcp_config_sources():
        if path.exists():
            servers = _read_mcp_servers(path, fmt)
            if servers:
                found.append((label, path, fmt, servers))

    if not found:
        click.secho("  No MCP configurations found.", fg="red")
        click.echo("  Configure your AI editor first, then re-run 'mm stm init'.")
        raise SystemExit(1)

    for i, (label, path, fmt, servers) in enumerate(found, 1):
        click.echo(f"    [{i}] {label} ({path}) — {len(servers)} servers")

    default_idx = 1
    if len(found) > 1:
        default_idx = max(range(len(found)), key=lambda i: len(found[i][3])) + 1
    source_idx = nav_prompt(
        "  Select source", type=click.IntRange(1, len(found)), default=default_idx
    )
    label, path, fmt, servers = found[source_idx - 1]
    state["selected_label"] = label
    state["selected_path"] = path
    state["selected_fmt"] = fmt
    state["all_servers"] = servers
    click.echo()


def _step_select_servers(state: dict) -> None:
    step_header(2, "Select Servers to Proxy")
    click.echo(f"  Servers in {state['selected_label']}:")

    candidates: list[tuple[str, dict]] = []
    for name, config in state["all_servers"].items():
        if "memtomem" in name.lower():
            click.echo(f"    [ ] {name} — skipped (memtomem itself)")
        else:
            idx = len(candidates) + 1
            transport, url = _detect_transport(config)
            if transport == "sse" and url:
                desc = url
            else:
                cmd = config.get("command", "")
                # Skip URL-like commands (Claude Code HTTP format)
                if cmd.startswith(("http://", "https://")):
                    desc = cmd
                else:
                    args = " ".join(config.get("args", []))
                    desc = f"{cmd} {args}".strip()
            click.echo(f"    [{idx}] {name} ({desc})")
            candidates.append((name, config))

    if not candidates:
        click.secho("  No servers to proxy (only memtomem found).", fg="yellow")
        raise SystemExit(0)

    click.echo()
    selection = nav_prompt(
        "  Proxy which servers? (comma-separated numbers, or 'all')",
        default="all",
    )

    if selection.strip().lower() == "all":
        selected = candidates
    else:
        indices = []
        for s in selection.split(","):
            s = s.strip()
            if s.isdigit():
                idx = int(s)
                if 1 <= idx <= len(candidates):
                    indices.append(idx - 1)
        selected = [candidates[i] for i in indices]

    if not selected:
        click.secho("  No servers selected.", fg="red")
        raise SystemExit(1)

    state["selected_servers"] = selected
    click.echo()


def _step_prefixes(state: dict) -> None:
    step_header(3, "Assign Prefixes")
    click.echo("  Tools will be renamed: {prefix}__{original_tool}")
    click.echo()

    used_prefixes: set[str] = set()
    server_configs: dict[str, dict] = {}

    for name, config in state["selected_servers"]:
        auto_prefix = _generate_prefix(name, used_prefixes)
        prefix = nav_prompt(f"  {name} → prefix", default=auto_prefix)
        prefix = prefix.lower().strip()
        used_prefixes.add(prefix)

        transport, url = _detect_transport(config)
        entry: dict = {
            "prefix": prefix,
            "transport": transport,
            "compression": "selective",
            "max_result_chars": 16000,
            "_original": config,  # backup for mm stm reset
        }
        if transport == "stdio":
            entry["command"] = config.get("command", "")
            entry["args"] = config.get("args", [])
        else:
            entry["url"] = url or ""
        if config.get("env"):
            entry["env"] = config["env"]

        server_configs[name] = entry

    state["server_configs"] = server_configs
    click.echo()


def _step_compression(state: dict) -> None:
    step_header(4, "Compression")
    click.echo("  How to compress large tool responses:")
    click.echo("    [1] hybrid (default) — preserve first 5K chars + TOC for the rest")
    click.echo("    [2] selective — 2-phase: TOC only, then pick sections on demand")
    click.echo("    [3] truncate — simple character limit")
    click.echo("    [4] none — pass-through")
    comp_choice = nav_prompt("  Select", type=click.IntRange(1, 4), default=1)
    compression = {1: "hybrid", 2: "selective", 3: "truncate", 4: "none"}[comp_choice]

    for cfg in state["server_configs"].values():
        cfg["compression"] = compression
    state["compression"] = compression
    click.echo()


def _step_cache(state: dict) -> None:
    step_header(5, "Response Cache")
    click.echo("  Cache identical tool calls to avoid redundant upstream requests.")
    click.echo("  Cached responses expire after the TTL (default: 1 hour).")
    click.echo()

    enable_cache = nav_confirm("  Enable response cache?", default=True)
    state["cache_enabled"] = enable_cache

    if enable_cache:
        ttl = nav_prompt("  Cache TTL in seconds (3600 = 1 hour)", type=int, default=3600)
        state["cache_ttl"] = ttl
    click.echo()


def _step_langfuse(state: dict) -> None:
    step_header(6, "Langfuse Tracing (optional)")
    click.echo("  Langfuse records proxy calls for observability and debugging.")
    click.echo("  Requires a running Langfuse instance (self-hosted or cloud).")
    click.echo()

    enable_langfuse = nav_confirm("  Enable Langfuse tracing?", default=False)
    state["langfuse_enabled"] = enable_langfuse

    if enable_langfuse:
        host = nav_prompt("  Langfuse host", default="http://localhost:3000")
        public_key = nav_prompt("  Public key (pk-lf-...)", default="")
        secret_key = nav_prompt("  Secret key (sk-lf-...)", default="")
        state["langfuse_host"] = host
        state["langfuse_public_key"] = public_key
        state["langfuse_secret_key"] = secret_key
    click.echo()


def _step_write_config(state: dict) -> None:
    step_header(7, "Writing Configuration")

    config_dir = Path("~/.memtomem").expanduser()
    config_dir.mkdir(parents=True, exist_ok=True)
    proxy_path = config_dir / "stm_proxy.json"

    proxy_data: dict = {
        "enabled": True,
        "upstream_servers": state["server_configs"],
        "metrics": {"enabled": True},
        "_source": {
            "label": state["selected_label"],
            "path": str(state["selected_path"]),
            "format": state["selected_fmt"],
        },
    }

    # Cache config
    if state.get("cache_enabled"):
        proxy_data["cache"] = {
            "enabled": True,
            "default_ttl_seconds": state.get("cache_ttl", 3600),
        }

    # Langfuse config (stored in env-compatible format)
    if state.get("langfuse_enabled"):
        state["_langfuse_env"] = {
            "MEMTOMEM_STM_LANGFUSE__ENABLED": "true",
            "MEMTOMEM_STM_LANGFUSE__HOST": state.get("langfuse_host", ""),
            "MEMTOMEM_STM_LANGFUSE__PUBLIC_KEY": state.get("langfuse_public_key", ""),
            "MEMTOMEM_STM_LANGFUSE__SECRET_KEY": state.get("langfuse_secret_key", ""),
        }

    if proxy_path.exists():
        try:
            existing = json.loads(proxy_path.read_text(encoding="utf-8"))
            existing.setdefault("upstream_servers", {}).update(state["server_configs"])
            existing["enabled"] = True
            proxy_data = existing
        except (json.JSONDecodeError, OSError):
            pass

    proxy_path.write_text(json.dumps(proxy_data, indent=2), encoding="utf-8")
    try:
        proxy_path.chmod(0o600)
    except OSError:
        pass
    state["proxy_path"] = proxy_path
    click.echo(f"  Config: {proxy_path}")
    click.echo(f"  Servers: {len(state['server_configs'])} upstream servers configured")
    if state.get("cache_enabled"):
        click.echo(f"  Cache:   enabled (TTL {state.get('cache_ttl', 3600)}s)")

    # Write Langfuse env hints
    if state.get("langfuse_enabled"):
        langfuse_env = state.get("_langfuse_env", {})
        env_path = config_dir / "stm_langfuse.env"
        lines = [f"{k}={v}" for k, v in langfuse_env.items()]
        env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        try:
            env_path.chmod(0o600)
        except OSError:
            pass
        click.echo(f"  Langfuse: {env_path}")
        click.echo("  Load with: export $(cat ~/.memtomem/stm_langfuse.env | xargs)")

    click.echo()


def _step_enable_stm(state: dict) -> None:
    step_header(8, "Enable STM")

    enable = nav_confirm("  Enable STM proxy in memtomem config?", default=True)
    state["stm_enabled"] = enable

    if enable:
        config_dir = Path("~/.memtomem").expanduser()
        mm_config_path = config_dir / "config.json"
        mm_config: dict = {}
        if mm_config_path.exists():
            try:
                mm_config = json.loads(mm_config_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        mm_config.setdefault("stm_proxy", {})["enabled"] = True
        mm_config_path.write_text(json.dumps(mm_config, indent=2), encoding="utf-8")
        click.echo(f"  Updated: {mm_config_path}")

    click.echo()

    remove = nav_confirm(
        f"  Remove proxied servers from {state['selected_label']} config?\n"
        f"  (memtomem will proxy them instead)",
        default=True,
    )
    if remove:
        _remove_proxied_servers(state)
    click.echo()


def _remove_proxied_servers(state: dict) -> None:
    """Remove proxied servers from the source MCP config."""
    selected_path = state["selected_path"]
    selected_fmt = state["selected_fmt"]
    try:
        full_config = json.loads(selected_path.read_text(encoding="utf-8"))
        removed = []
        if selected_fmt == "claude_json":
            top_servers = full_config.get("mcpServers", {})
            for name, _ in state["selected_servers"]:
                if name in top_servers:
                    del top_servers[name]
                    removed.append(name)
            cwd = str(Path.cwd())
            for proj_path, proj_data in full_config.get("projects", {}).items():
                if cwd.startswith(proj_path) or proj_path == cwd:
                    proj_servers = proj_data.get("mcpServers", {})
                    for name, _ in state["selected_servers"]:
                        if name in proj_servers:
                            del proj_servers[name]
                            if name not in removed:
                                removed.append(name)
        else:
            mcp_servers = full_config.get("mcpServers", {})
            for name, _ in state["selected_servers"]:
                if name in mcp_servers:
                    del mcp_servers[name]
                    removed.append(name)
        selected_path.write_text(json.dumps(full_config, indent=2), encoding="utf-8")
        if removed:
            click.echo(f"  Removed: {', '.join(removed)} from {selected_path}")
    except (json.JSONDecodeError, OSError) as e:
        click.secho(f"  Warning: Could not update {selected_path}: {e}", fg="yellow")


# ── CLI ───────────────────────────────────────────────────────────────


@click.group("stm")
def stm() -> None:
    """STM proxy management — proactive memory surfacing."""


@stm.command("init")
def stm_init() -> None:
    """Set up STM proxy with an interactive wizard."""
    click.echo()
    click.secho("  memtomem STM setup", fg="cyan", bold=True)
    click.secho("  ───────────────────", fg="cyan")
    click.echo()

    # Pre-check: memtomem-stm package
    if importlib.util.find_spec("memtomem_stm") is None:
        click.secho("  memtomem-stm is not installed.", fg="red")
        click.echo('  Install it first: pip install "memtomem-stm[ltm]"')
        click.echo('  Source install:   uv pip install -e "packages/memtomem-stm[ltm]"')
        raise SystemExit(1)
    click.secho("  ✓ memtomem-stm is installed.", fg="green")
    click.echo()

    state: dict = {}
    steps = [
        _step_detect_clients,
        _step_select_servers,
        _step_prefixes,
        _step_compression,
        _step_cache,
        _step_langfuse,
        _step_write_config,
        _step_enable_stm,
    ]
    run_steps(steps, state)

    # Summary
    click.secho("  STM Setup Complete!", fg="green", bold=True)
    click.echo()
    click.echo(f"  Proxy config:  {state.get('proxy_path', '~/.memtomem/stm_proxy.json')}")
    prefix_map = ", ".join(
        f"{n} → {c['prefix']}" for n, c in state.get("server_configs", {}).items()
    )
    click.echo(f"  Servers:       {len(state.get('server_configs', {}))} ({prefix_map})")
    click.echo(f"  Compression:   {state.get('compression', 'hybrid')}")
    click.echo(f"  Cache:         {'yes' if state.get('cache_enabled') else 'no'}")
    click.echo(f"  Langfuse:      {'yes' if state.get('langfuse_enabled') else 'no'}")
    click.echo(f"  STM enabled:   {'yes' if state.get('stm_enabled') else 'no'}")
    click.echo()
    click.secho("  Next steps:", fg="cyan")
    click.echo("    1. Restart your AI editor to apply changes")
    if state.get("langfuse_enabled"):
        click.echo("    2. Load Langfuse env: export $(cat ~/.memtomem/stm_langfuse.env | xargs)")
        click.echo("    3. Ask your agent to call stm_proxy_stats to verify")
    else:
        click.echo("    2. Ask your agent to call stm_proxy_stats to verify")
    click.echo("    To undo: mm stm reset")
    click.echo()


@stm.command("reset")
def stm_reset() -> None:
    """Disable STM proxy and restore original MCP server configs."""
    click.echo()
    click.secho("  memtomem STM reset", fg="cyan", bold=True)
    click.secho("  ───────────────────", fg="cyan")
    click.echo()

    config_dir = Path("~/.memtomem").expanduser()
    proxy_path = config_dir / "stm_proxy.json"

    if not proxy_path.exists():
        click.secho("  No STM proxy config found. Nothing to reset.", fg="yellow")
        raise SystemExit(0)

    try:
        proxy_data = json.loads(proxy_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        click.secho("  Could not read stm_proxy.json.", fg="red")
        raise SystemExit(1)

    upstream = proxy_data.get("upstream_servers", {})
    source_info = proxy_data.get("_source", {})

    if not upstream:
        click.secho("  No upstream servers found in config.", fg="yellow")
        raise SystemExit(0)

    # Show what will be restored
    click.echo("  Upstream servers to restore:")
    for name, cfg in upstream.items():
        prefix = cfg.get("prefix", "?")
        click.echo(f"    {name} (prefix: {prefix})")
    click.echo()

    if not click.confirm("  Disable STM and restore original MCP configs?", default=True):
        click.echo("  Cancelled.")
        raise SystemExit(0)

    # 1. Restore servers to MCP client config
    restored_count = 0
    source_path_str = source_info.get("path", "")
    source_fmt = source_info.get("format", "standard")

    if source_path_str:
        source_path = Path(source_path_str)
        if source_path.exists():
            try:
                full_config = json.loads(source_path.read_text(encoding="utf-8"))

                for name, cfg in upstream.items():
                    original = cfg.get("_original")
                    if not original:
                        # Rebuild from proxy config
                        original = {}
                        if cfg.get("command"):
                            original["command"] = cfg["command"]
                        if cfg.get("args"):
                            original["args"] = cfg["args"]
                        if cfg.get("url"):
                            original["url"] = cfg["url"]
                        if cfg.get("env"):
                            original["env"] = cfg["env"]

                    if not original:
                        continue

                    if source_fmt == "claude_json":
                        full_config.setdefault("mcpServers", {})[name] = original
                    else:
                        full_config.setdefault("mcpServers", {})[name] = original
                    restored_count += 1

                source_path.write_text(json.dumps(full_config, indent=2), encoding="utf-8")
                click.echo(f"  Restored {restored_count} servers to {source_path}")
            except (json.JSONDecodeError, OSError) as e:
                click.secho(f"  Warning: Could not update {source_path}: {e}", fg="yellow")
        else:
            click.secho(f"  Warning: Source config not found: {source_path}", fg="yellow")
    else:
        click.secho("  Warning: No source config path saved. Servers not restored.", fg="yellow")
        click.echo("  You may need to re-add MCP servers manually.")

    # 2. Disable STM in config.json
    mm_config_path = config_dir / "config.json"
    if mm_config_path.exists():
        try:
            mm_config = json.loads(mm_config_path.read_text(encoding="utf-8"))
            if "stm_proxy" in mm_config:
                mm_config["stm_proxy"]["enabled"] = False
                mm_config_path.write_text(json.dumps(mm_config, indent=2), encoding="utf-8")
                click.echo(f"  STM disabled in {mm_config_path}")
        except (json.JSONDecodeError, OSError):
            pass

    # 3. Remove stm_proxy.json
    proxy_path.unlink()
    click.echo(f"  Removed {proxy_path}")

    # Summary
    click.echo()
    click.secho("  STM Reset Complete!", fg="green", bold=True)
    click.echo()
    if restored_count > 0:
        click.echo(f"  Restored:  {restored_count} servers to original MCP config")
    click.echo("  STM proxy: disabled")
    click.echo()
    click.secho("  Next: Restart your AI editor to apply changes.", fg="cyan")
    click.echo()
