"""Read-only Claude registration inventory; never connect to an MCP server.

Configuration predicts duplicate registrations, not live process counts or
store identity. Session-only flags and managed policies are not reconstructed.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil

# Fixed read-only CLI inventory only, never configured MCP commands (B404).
import subprocess  # nosec B404
import sys
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

import click

from memtomem._claude_plugin_contract import CORE_VERSION, TOOL_MODE


@dataclass
class Report:
    findings: list[dict[str, Any]] = field(default_factory=list)
    complete: bool = True
    current_risk: bool = False
    install_risk: bool = False
    reusable: bool = False

    @property
    def exit_code(self) -> int:
        return 2 if not self.complete else int(self.current_risk or self.install_risk)

    def payload(self) -> dict[str, Any]:
        return {
            "status": ("incomplete" if not self.complete else "risk" if self.exit_code else "pass"),
            "complete": self.complete,
            "current_risk": self.current_risk,
            "plugin_install_risk": self.install_risk,
            "reusable_registration": self.reusable,
            "findings": self.findings,
        }

    def add(self, code: str, message: str, **detail: Any) -> None:
        self.findings.append({"code": code, "message": message, **detail})

    def unknown(self, message: str) -> None:
        self.complete = False
        self.add("incomplete", message)


def _object(path: Path, report: Report, label: str, *, required: bool = False) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        return value
    except FileNotFoundError:
        if required or path.is_symlink():
            report.unknown(f"{label}: configuration is missing.")
    except (OSError, ValueError, UnicodeError):
        # Exceptions and raw config text may contain credentials.
        report.unknown(f"{label}: configuration could not be read as a JSON object.")
    return {}


def _mapping(data: dict, key: str, report: Report, label: str) -> dict:
    value = data.get(key, {})
    if not isinstance(value, dict):
        report.unknown(f"{label}: {key} must be an object.")
        return {}
    return value


def _names(data: dict, key: str, report: Report) -> set[str]:
    value = data.get(key, [])
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        report.unknown(f"Claude configuration: {key} must be a list of names.")
        return set()
    return set(value)


def _basename(value: str) -> str:
    return PureWindowsPath(value).name.casefold().removesuffix(".exe")


def _candidate(name: str, entry: dict) -> bool:
    if name == "memtomem":
        return True
    tokens = (
        [entry.get("command", ""), *entry.get("args", [])]
        if isinstance(entry.get("args", []), list)
        else [entry.get("command", "")]
    )
    return any(
        isinstance(token, str)
        and (
            _basename(token) == "memtomem-server"
            or token in {"memtomem.server", "memtomem.server.__main__"}
            or re.search(r"(?<![\w-])memtomem-server(?![\w-])", token) is not None
        )
        for token in tokens
    )


def _launch(entry: dict) -> tuple[str, tuple[str, ...]] | None:
    command, args = entry.get("command"), entry.get("args", [])
    env = entry.get("env", {})
    if (
        not isinstance(command, str)
        or not command
        or not isinstance(args, list)
        or any(not isinstance(arg, str) for arg in args)
        or not isinstance(env, dict)
        or any(not isinstance(v, str) for v in env.values())
        or entry.get("type", "stdio") != "stdio"
    ):
        return None
    if any("${" in token for token in [command, *args]):
        return None
    base = _basename(command)
    # Do not execute or guess at shell wrappers and arbitrary launch scripts.
    recognized = (
        base == "memtomem-server"
        or (base in {"uv", "uvx"} and "memtomem-server" in args)
        or (
            re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", base) is not None
            and any(args[i : i + 2] == ["-m", "memtomem.server"] for i in range(len(args)))
        )
    )
    return (command, tuple(args)) if recognized else None


def _plugin_rows(cwd: Path, report: Report) -> list[dict]:
    try:
        executable = shutil.which("claude")
        if executable is None:
            raise FileNotFoundError
        # Resolve the user's CLI on PATH once; no shell and no config-derived
        # command/arguments. The project cwd selects the inventory's scope.
        result = subprocess.run(  # nosec B603
            [str(Path(executable).absolute()), "plugin", "list", "--json"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode:
            raise ValueError
        rows = json.loads(result.stdout)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise ValueError
        return rows
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
        report.unknown("Claude plugin inventory unavailable; check `claude plugin list --json`.")
        return []


def _managed_paths() -> list[Path]:
    if sys.platform == "darwin":
        root = Path("/Library/Application Support/ClaudeCode")
    elif sys.platform == "win32":
        root = Path(os.environ.get("ProgramFiles", "C:/Program Files")) / "ClaudeCode"
    else:
        root = Path("/etc/claude-code")
    return [root / "managed-mcp.json", root / "managed-settings.json"]


def _effective_plugins(cwd: Path, report: Report) -> list[dict]:
    """Resolve installations of the same plugin ID by applicable scope."""
    selected: dict[str, dict] = {}
    priorities = {"user": 0, "project": 1, "local": 2}
    for row in _plugin_rows(cwd, report):
        identity = row.get("id")
        if not isinstance(identity, str):
            report.unknown("Claude plugin inventory has an unsupported identity format.")
            continue
        if identity.split("@", 1)[0] != "memtomem":
            continue
        if re.fullmatch(r"memtomem@[A-Za-z0-9._-]+", identity) is None:
            report.unknown(
                "memtomem plugin identity cannot be safely used in remediation commands."
            )
            continue
        scope = row.get("scope")
        if (
            not isinstance(row.get("enabled"), bool)
            or not isinstance(scope, str)
            or scope not in priorities
        ):
            report.unknown("memtomem plugin enablement or scope could not be resolved.")
            continue
        if scope != "user":
            path = row.get("projectPath")
            if not isinstance(path, str) or not path or not Path(path).is_absolute():
                report.unknown("memtomem plugin project scope has no absolute project path.")
                continue
            project = Path(path).resolve()
            if project != cwd:
                if project in cwd.parents:
                    report.unknown(
                        "Parent-scoped memtomem plugin exists; run the check at its project root."
                    )
                continue
        previous = selected.get(identity)
        if previous is None or priorities[scope] > priorities[previous["scope"]]:
            selected[identity] = row
        elif priorities[scope] == priorities[previous["scope"]] and row != previous:
            report.unknown("Conflicting install records exist for the same memtomem plugin scope.")
    return list(selected.values())


def _inspect_claude_mcp() -> Report:
    """Inspect the current project and account, including prospective install risk."""
    report = Report()
    cwd = Path.cwd().resolve()
    custom = os.environ.get("CLAUDE_CONFIG_DIR")
    config_dir = Path(custom).expanduser() if custom else Path.home() / ".claude"
    global_path = config_dir / ".claude.json" if custom else Path.home() / ".claude.json"
    account = _object(global_path, report, "Claude account")
    projects = _mapping(account, "projects", report, "Claude account")
    local = projects.get(str(cwd), {})
    if not isinstance(local, dict):
        report.unknown("Claude local project configuration must be an object.")
        local = {}
    # A parent-defined project can change which local key and .mcp.json apply.
    # Require running at that root rather than silently assuming a clean scan.
    for parent in () if (cwd / ".git").exists() else cwd.parents:
        parent_data = projects.get(str(parent), {})
        parent_settings = (parent / ".claude").resolve() != config_dir.resolve() and (
            (parent / ".claude/settings.json").exists()
            or (parent / ".claude/settings.local.json").exists()
        )
        if (
            (parent / ".mcp.json").exists()
            or parent_settings
            or (
                isinstance(parent_data, dict)
                and (parent_data.get("mcpServers") or parent_data.get("disabledMcpServers"))
            )
        ):
            report.unknown(
                "Parent project configuration exists; run the check at its project root."
            )
            break
        if (parent / ".git").exists():
            break
    if any(path.exists() for path in _managed_paths()):
        report.unknown("Managed Claude policy is present; effective MCP registration needs review.")

    settings = [
        _object(config_dir / "settings.json", report, "Claude user settings"),
        _object(cwd / ".claude/settings.json", report, "Claude project settings"),
        _object(cwd / ".claude/settings.local.json", report, "Claude local settings"),
    ]
    for item in settings:
        if "enableAllProjectMcpServers" in item and not isinstance(
            item["enableAllProjectMcpServers"], bool
        ):
            report.unknown("Claude project approval setting must be a boolean.")
        if any(
            key in item
            for key in ("allowedMcpServers", "deniedMcpServers", "allowManagedMcpServersOnly")
        ):
            report.unknown("Claude MCP access policy needs manual review.")
    disabled = _names(local, "disabledMcpServers", report)
    rejected = set().union(
        *(_names(s, "disabledMcpjsonServers", report) for s in [local, *settings])
    )
    # Repo-controlled approvals do not establish workspace trust.
    approvals = _names(local, "enabledMcpjsonServers", report) | _names(
        settings[0], "enabledMcpjsonServers", report
    )
    approve_all = settings[0].get("enableAllProjectMcpServers") is True
    if local.get("hasTrustDialogAccepted") is True:
        approvals |= set().union(
            *(_names(s, "enabledMcpjsonServers", report) for s in settings[1:])
        )
        approve_all |= any(s.get("enableAllProjectMcpServers") is True for s in settings[1:])
    project = _object(cwd / ".mcp.json", report, "Claude project MCP")
    effective: dict[str, tuple[str, dict]] = {}
    for scope, data in [("user", account), ("project", project), ("local", local)]:
        for name, entry in _mapping(data, "mcpServers", report, scope).items():
            if not isinstance(entry, dict):
                report.unknown(f"{scope} MCP entry must be an object.")
                continue
            effective[name] = (scope, entry)

    manual: list[tuple[str, dict]] = []
    for name, (scope, entry) in effective.items():
        if not _candidate(name, entry):
            continue
        safe_name = (
            name if re.fullmatch(r"[\w-]{1,128}", name, flags=re.ASCII) else "<invalid-name>"
        )
        if safe_name == "<invalid-name>" or safe_name.startswith("-"):
            report.unknown("Manual memtomem registration has an unsupported server name.")
            safe_name = "<invalid-name>"
        state = "configured"
        if name in disabled or (scope == "project" and name in rejected):
            state = "disabled"
        elif scope == "project" and not (approve_all or name in approvals):
            state = "pending_approval"
        report.add(
            "manual_registration",
            "Manual memtomem registration found.",
            name=safe_name,
            scope=scope,
            state=state,
            remediation=(
                f"claude mcp remove {shlex.quote(safe_name)} -s {scope}"
                if safe_name != "<invalid-name>"
                else "Review the invalid server name in Claude settings."
            ),
        )
        if state == "disabled":
            continue
        if state == "pending_approval":
            report.unknown(
                "Project memtomem registration is awaiting approval; review `/mcp` first."
            )
        if _launch(entry) is None:
            report.unknown("Manual memtomem launch cannot be classified without executing it.")
        manual.append((name, entry))

    plugins: list[dict] = []
    for row in _effective_plugins(cwd, report):
        if row.get("enabled") is False:
            report.add("plugin_disabled", "memtomem plugin is disabled.")
            continue
        install_path = row.get("installPath")
        if (
            not isinstance(install_path, str)
            or not install_path
            or not Path(install_path).is_absolute()
        ):
            report.unknown("memtomem plugin installation path unavailable.")
            continue
        manifest = _object(
            Path(install_path) / ".claude-plugin/plugin.json",
            report,
            "memtomem plugin manifest",
            required=True,
        )
        if manifest.get("mcpServers", "./.mcp.json") != "./.mcp.json":
            report.unknown("Custom memtomem plugin MCP manifest needs manual review.")
            continue
        config = _object(
            Path(install_path) / ".mcp.json", report, "memtomem plugin MCP", required=True
        )
        servers = _mapping(config, "mcpServers", report, "memtomem plugin")
        entry = servers.get("memtomem")
        if len(servers) != 1 or not isinstance(entry, dict) or _launch(entry) is None:
            report.unknown("memtomem plugin launch configuration is unsupported.")
            continue
        plugins.append(entry)
        report.add(
            "plugin_enabled",
            "memtomem plugin is enabled for this project.",
            plugin_id=row["id"],
            scope=row["scope"],
            uninstall_command=f"claude plugin uninstall {row['id']} -s {row['scope']}",
        )

    if len(plugins) > 1 or len(manual) > 1:
        report.current_risk = True
        report.add(
            "multiple_registrations",
            "Multiple memtomem registrations need review; process count is not known.",
        )
    target = (
        plugins[0]
        if plugins
        else {
            "command": "uvx",
            "args": ["--from", f"memtomem=={CORE_VERSION}", "memtomem-server"],
            "env": {"MEMTOMEM_TOOL_MODE": TOOL_MODE},
        }
    )
    for _, entry in manual:
        if _launch(entry) is None:
            continue
        if _launch(entry) == _launch(target):
            report.add(
                "native_dedup",
                "Matching command and arguments: Claude is expected to suppress the plugin copy.",
            )
            if entry.get("env", {}) != target.get("env", {}):
                report.add(
                    "environment_difference",
                    "Environment settings differ; the manual entry wins, including its environment.",
                )
        else:
            report.current_risk |= bool(plugins)
            report.install_risk = True
            report.add(
                "duplicate_risk" if plugins else "plugin_install_risk",
                "Different launch commands can expose duplicate tools"
                + ("." if plugins else " after installing the plugin."),
            )
    report.reusable = report.complete and not report.current_risk and bool(manual or plugins)
    report.add(
        "session_boundary",
        "Configuration check only; session flags and live connections require `/mcp` verification.",
    )
    return report


def inspect_claude_mcp() -> Report:
    try:
        return _inspect_claude_mcp()
    except (OSError, ValueError, RuntimeError):
        report = Report()
        report.unknown("Claude configuration paths could not be inspected safely.")
        return report


def emit_report(report: Report, *, as_json: bool = False) -> None:
    if as_json:
        click.echo(json.dumps(report.payload(), indent=2))
        return
    click.echo(f"Claude MCP: {report.payload()['status']}")
    for finding in report.findings:
        label = (
            f" [{finding['scope']}: {finding['name']}; {finding['state']}]"
            if "name" in finding
            else ""
        )
        click.echo(f"  {finding['message']}{label}")
        if "remediation" in finding:
            click.echo(
                f"    To keep the plugin, remove only this manual registration: {finding['remediation']}"
            )
    if report.current_risk:
        for finding in report.findings:
            if "uninstall_command" in finding:
                click.echo(f"  To keep the manual setup instead: {finding['uninstall_command']}")


def preflight() -> bool:
    """Return whether to register; preserve existing setups and refuse unknowns."""
    report = inspect_claude_mcp()
    if not report.complete or report.current_risk:
        emit_report(report)
        click.echo(
            "MCP registration deferred. Resolve the findings and retry, or initialize with --mcp skip."
        )
        raise click.exceptions.Exit(2 if not report.complete else 1)
    if report.reusable:
        click.echo(
            "  Claude Code: memtomem is already configured — additional MCP registration skipped. Verify the connection in /mcp."
        )
        return False
    if any(row["code"] == "manual_registration" for row in report.findings):
        click.echo(
            "Existing memtomem registration is disabled; review `/mcp` or use --mcp skip. No registration was added."
        )
        raise click.exceptions.Exit(1)
    return True
