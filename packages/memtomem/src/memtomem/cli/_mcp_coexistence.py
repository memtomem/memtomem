"""Host-specific registration checks and explicit, single-file reconciliation.

Inventory commands are fixed and read-only. Never execute MCP launch commands.
Preview is the default; apply makes a private backup before an atomic replacement.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import stat
import subprocess  # nosec B404
import tempfile
import tomllib
from pathlib import Path

from memtomem.cli._claude_mcp import Report, _candidate, _launch, _mapping, _object, _plugin_servers


def _inventory(client: str, report: Report) -> list[dict]:
    try:
        executable = shutil.which(client)
        if executable is None:
            raise ValueError
        result = subprocess.run(  # nosec B603
            [str(Path(executable).absolute()), "plugin", "list", "--json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            raise ValueError
        if getattr(result, "stderr", "").strip():
            report.unknown(f"{client} plugin inventory reported warnings; review its CLI output.")
        data = json.loads(result.stdout)
        if client == "codex":
            data = data["installed"]
        if not isinstance(data, list) or any(not isinstance(row, dict) for row in data):
            raise ValueError
        return data
    except (OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired):
        report.unknown(
            f"{client} plugin inventory unavailable; check `{client} plugin list --json`."
        )
        return []


def _toml(path: Path, report: Report) -> dict:
    try:
        raw = path.read_bytes()
        report.snapshots[path] = raw
        return tomllib.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        report.snapshots[path] = None
        if path.is_symlink():
            report.unknown("Codex configuration is a broken symlink.")
    except (OSError, ValueError, UnicodeError):
        report.unknown("Codex configuration could not be read as TOML.")
    return {}


def _plugin_path(client: str, row: dict, root: Path, report: Report) -> Path | None:
    if client == "agy":
        value = row.get("path")
        if isinstance(value, str) and Path(value).is_absolute():
            return Path(value)
    else:
        # plugin list exposes source paths, not installed cache paths. Do not
        # edit/read a marketplace checkout as if it were the installed bundle.
        parts = [row.get("marketplaceName"), row.get("name"), row.get("version")]
        if all(
            isinstance(p, str) and re.fullmatch(r"[\w.+-]+", p) and p not in {".", ".."}
            for p in parts
        ):
            return root.joinpath("plugins/cache", *(str(p) for p in parts))
    report.unknown("memtomem plugin installation path cannot be resolved.")
    return None


def inspect_host(client: str) -> Report:
    report = Report()
    try:
        _inspect_host(client, report)
    except (OSError, ValueError, RuntimeError):
        report.unknown("Client configuration paths could not be inspected safely.")
    report.add("session_boundary", "Configuration check only; restart the client and verify /mcp.")
    return report


def _inspect_host(client: str, report: Report) -> None:
    codex = client == "codex"
    root = (
        Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
        if codex
        else Path.home() / ".gemini/config"
    )
    path = root / ("config.toml" if codex else "mcp_config.json")
    data = _toml(path, report) if codex else _object(path, report, "Antigravity MCP")
    key = "mcp_servers" if codex else "mcpServers"
    if codex:
        # A plain config.toml inspection cannot reconstruct session overrides,
        # managed requirements or trusted project/profile layers.
        for parent in [Path.cwd(), *Path.cwd().parents]:
            extra = parent / ".codex/config.toml"
            if extra != path and extra.exists():
                report.unknown("Codex project configuration exists; review effective layers first.")
                break
        if data.get("profiles") or data.get("profile") or (root / "requirements.toml").exists():
            report.unknown("Codex profile or requirements need effective-configuration review.")
        if Path("/etc/codex/requirements.toml").exists() or Path("/etc/codex/config.toml").exists():
            report.unknown("Managed Codex configuration needs review.")
    manual = []
    for name, entry in _mapping(data, key, report, client).items():
        if not isinstance(entry, dict):
            report.unknown("Manual MCP entry must be an object.")
            continue
        if not _candidate(name, entry):
            continue
        report.manual_sources.append((path, (key, name)))
        flag = entry.get("enabled", True) if codex else entry.get("disabled", False)
        enabled = flag if codex else not flag
        if not isinstance(flag, bool):
            report.unknown("Manual MCP enablement must be boolean.")
            state = "unclassified"
        elif not enabled:
            state = "disabled"
        elif _launch(entry) is None:
            report.unknown("Manual memtomem launch cannot be classified without executing it.")
            state = "unclassified"
        else:
            state = "configured"
        report.add(
            "manual_registration",
            "Manual memtomem registration found.",
            state=state,
        )
        if state == "configured":
            manual.append(name)
    plugins = []
    for row in _inventory(client, report):
        if not isinstance(row.get("name"), str):
            report.unknown("Plugin inventory has an unsupported identity format.")
            continue
        if row.get("name") != "memtomem":
            continue
        if not isinstance(row.get("enabled"), bool):
            report.unknown("Plugin enablement could not be resolved.")
            continue
        if row["enabled"] is False:
            report.add("plugin_disabled", "memtomem plugin is disabled.")
            continue
        installed = _plugin_path(client, row, root, report)
        if installed is None:
            continue
        manifest_path = installed / (".codex-plugin/plugin.json" if codex else "plugin.json")
        manifest = _object(manifest_path, report, "Plugin manifest", required=True)
        if manifest.get("name") != "memtomem":
            report.unknown("Plugin manifest identity is unsupported.")
            continue
        filename = ".mcp.json" if codex else "mcp_config.json"
        if codex and manifest.get("mcpServers", "./.mcp.json") != "./.mcp.json":
            report.unknown("Custom plugin MCP manifest needs manual review.")
            continue
        plugin_path = installed / filename
        servers = _plugin_servers(plugin_path, report)
        if servers is None:
            continue
        if not servers:
            if len(manual) == 1:
                report.add(
                    "skills_only", "Plugin MCP omitted; the manual registration supplies tools."
                )
            else:
                report.unknown("Plugin MCP is omitted without one usable manual registration.")
        elif (
            len(servers) != 1
            or not isinstance(servers.get("memtomem"), dict)
            or _launch(servers["memtomem"]) is None
        ):
            report.unknown("Plugin MCP launch configuration is unsupported.")
            continue
        else:
            entry = servers["memtomem"]
            if entry.get("enabled", True) is not True or entry.get("disabled", False) is not False:
                report.unknown(
                    "Plugin MCP has an enablement override; review it before reconciliation."
                )
                continue
            plugins.append(plugin_path)
            report.add("plugin_enabled", "memtomem plugin MCP is configured.")
        report.plugin_paths.append(plugin_path)
    if len(manual) > 1 or len(report.plugin_paths) > 1:
        report.current_risk = True
        report.add("multiple_registrations", "Multiple memtomem registrations need manual review.")
    if manual and plugins:
        if codex and manual == ["memtomem"]:
            report.add(
                "native_dedup", "Codex gives the manual memtomem name precedence over the plugin."
            )
        else:
            report.current_risk = True
            report.add(
                "duplicate_risk", "Manual and plugin MCP registrations can both start servers."
            )
    if (
        codex
        and not manual
        and plugins
        and any(
            f["code"] == "manual_registration" and f.get("state") == "disabled"
            for f in report.findings
        )
    ):
        report.unknown("A disabled manual name may suppress the plugin; review /mcp.")
    report.reusable = report.complete and not report.current_risk and bool(manual or plugins)


def _remove_toml(raw: bytes, keys: tuple[str, ...]) -> bytes:
    """Remove ordinary table sections only, then prove semantic preservation.

    Inline/dotted-key layouts that cannot be safely preserved are refused.
    Parsing both sides also guards against headers inside multiline strings.
    """
    original = tomllib.loads(raw.decode("utf-8"))
    expected = copy.deepcopy(original)
    del expected[keys[0]][keys[1]]
    output = []
    dropping = False
    for line in raw.decode("utf-8").splitlines(keepends=True):
        if line.lstrip().startswith("["):
            try:
                header = tomllib.loads(line)
                section = header.get(keys[0], {})
                dropping = isinstance(section, dict) and keys[1] in section
            except ValueError:
                dropping = False
        if not dropping:
            output.append(line)
    candidate = "".join(output).encode("utf-8")
    # tomllib omits an implicit empty parent table when its last child is removed.
    parsed = tomllib.loads(candidate.decode("utf-8"))
    if not expected[keys[0]] and keys[0] not in parsed:
        del expected[keys[0]]
    if parsed != expected:
        raise ValueError(
            "Unsupported TOML layout; remove the manual entry with `codex mcp remove memtomem`."
        )
    return candidate


def reconcile(report: Report, client: str, choice: str, *, apply: bool) -> None:
    """Add a redacted preview or apply exactly one unambiguous config edit."""
    try:
        _reconcile(report, client, choice, apply=apply)
    except (OSError, ValueError, UnicodeError, KeyError, TypeError):
        report.unknown(
            "Reconciliation refused: configuration is ambiguous, changed, or not safely writable. No further edits made."
        )


def _reconcile(report: Report, client: str, choice: str, *, apply: bool) -> None:
    if not report.complete:
        return
    active = [
        f
        for f in report.findings
        if f["code"] == "manual_registration" and f.get("state") == "configured"
    ]
    if report.shadowed_manual or len(report.plugin_paths) != 1 or len(report.manual_sources) > 1:
        report.unknown(
            "Reconciliation needs one enabled plugin and at most one unshadowed manual registration."
        )
        return
    keys: tuple[str, ...]
    if choice == "keep-manual":
        if len(active) != 1:
            report.unknown("keep-manual requires one usable manual registration.")
            return
        if client == "codex" and report.manual_sources[0][1][-1] == "memtomem":
            report.add(
                "reconcile_noop",
                "keep-manual: Codex already gives the manual name precedence; no file changes.",
            )
            return
        if any(f["code"] in {"skills_only", "plugin_server_disabled"} for f in report.findings):
            report.add(
                "reconcile_noop",
                "keep-manual: plugin already supplies skills only; no file changes.",
            )
            return
        path = report.plugin_paths[0]
        keys = ("mcpServers", "memtomem")
        summary = "Remove only the bundled MCP entry; keep manual MCP and all plugin skills. Plugin updates may restore the entry."
    else:
        if not any(f["code"] == "plugin_enabled" for f in report.findings):
            report.unknown(
                "keep-plugin requires a usable bundled MCP server; restore/re-enable it first."
            )
            return
        if not report.manual_sources:
            report.add("reconcile_noop", "keep-plugin: no manual registration to remove.")
            return
        path, keys = report.manual_sources[0]
        summary = "Remove only the manual MCP entry; keep the plugin's bundled server and skills."
    raw = report.snapshots[path]
    if raw is None:
        raise ValueError
    if path.suffix == ".toml":
        replacement = _remove_toml(raw, keys)
    else:
        data = json.loads(raw)
        parent = data
        for key in keys[:-1]:
            parent = parent[key]
        del parent[keys[-1]]
        replacement = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    report.add("reconcile_plan", summary, path=str(path), choice=choice)
    if not apply:
        report.add(
            "preview",
            "Preview only. Repeat with --apply to write a private backup and apply this edit.",
        )
        return
    backup = _replace(report, path, raw, replacement)
    report.add(
        "reconcile_applied",
        "Configuration updated. Restart the client and verify /mcp.",
        backup=str(backup),
    )


def _replace(report: Report, path: Path, raw: bytes, replacement: bytes) -> Path:
    # Refuse symlinks in every component; do not rewrite a linked source bundle.
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise ValueError
    mode = path.stat().st_mode
    if not stat.S_ISREG(mode) or path.stat().st_nlink != 1:
        raise ValueError
    for source, expected in report.snapshots.items():
        try:
            actual = source.read_bytes()
        except FileNotFoundError:
            actual = None
        if actual != expected:
            raise ValueError
    backup_fd, backup_name = tempfile.mkstemp(prefix=path.name + ".mm-backup-", dir=path.parent)
    with os.fdopen(backup_fd, "wb") as stream:
        stream.write(raw)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".mm-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(replacement)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, stat.S_IMODE(mode))
        if path.is_symlink() or path.read_bytes() != raw:
            raise ValueError
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return Path(backup_name)
