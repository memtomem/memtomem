"""Tool: mem_watchdog — health watchdog status, history, and manual trigger."""

from __future__ import annotations

import json

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("maintenance")
async def mem_watchdog(
    command: str = "status",
    check: str | None = None,
    hours: float = 24.0,
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Health watchdog: view status, trends, or force a check run.

    Args:
        command: "status" (default), "history", "run"
        check: Filter by check name (for history command)
        hours: Hours of history to return (default 24)
    """
    app = _get_app(ctx)
    watchdog = app.health_watchdog
    if watchdog is None:
        return "Health watchdog is not enabled. Set MEMTOMEM_HEALTH_WATCHDOG__ENABLED=true"

    if command == "status":
        data = watchdog.get_status()
        return _format_status(data)

    elif command == "history":
        if not check:
            return "Please specify a check name. Use command='status' to see available checks."
        trends = watchdog.get_trends(check, hours)
        if not trends:
            return f"No history found for '{check}' in the last {hours}h."
        lines = [f"History for '{check}' (last {hours}h): {len(trends)} snapshots\n"]
        for t in trends[-20:]:  # last 20 entries
            lines.append(f"  [{t['status']}] {_fmt_time(t['at'])} — {json.dumps(t['value'])}")
        return "\n".join(lines)

    elif command == "run":
        results = await watchdog.run_now()
        lines = ["Health check results:\n"]
        for name, info in sorted(results.items()):
            marker = "✓" if info["status"] == "ok" else "⚠" if info["status"] == "warning" else "✗"
            lines.append(f"  {marker} {name}: {info['status']} — {json.dumps(info['value'])}")
        return "\n".join(lines)

    return f"Unknown command '{command}'. Use: status, history, run"


def _format_status(data: dict) -> str:
    if not data.get("enabled"):
        return "Health watchdog is not enabled."
    running = "running" if data.get("running") else "stopped"
    lines = [f"Health watchdog: {running}\n"]
    checks = data.get("checks", {})
    if not checks:
        lines.append("  No checks recorded yet.")
    else:
        for name, info in sorted(checks.items()):
            marker = "✓" if info["status"] == "ok" else "⚠" if info["status"] == "warning" else "✗"
            lines.append(
                f"  {marker} {name} [{info['tier']}]: {info['status']} — {json.dumps(info['value'])}"
            )
    return "\n".join(lines)


def _fmt_time(ts: float) -> str:
    import datetime

    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M:%S")
