"""Tools: context_detect, context_generate, context_sync, context_diff."""

from __future__ import annotations

from pathlib import Path

from memtomem.server import mcp
from memtomem.server.context import CtxType
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


def _find_project_root() -> Path:
    """Walk up from cwd to find project root."""
    p = Path.cwd()
    for _ in range(10):
        if (p / ".git").exists() or (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_detect(
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Detect agent configuration files in the current project.

    Scans for CLAUDE.md, .cursorrules, GEMINI.md, AGENTS.md,
    and .github/copilot-instructions.md.
    """
    from memtomem.context.detector import detect_agent_files

    root = _find_project_root()
    files = detect_agent_files(root)

    if not files:
        return "No agent configuration files found."

    lines = [f"Found {len(files)} agent file(s):\n"]
    for f in files:
        rel = f.path.relative_to(root) if f.path.is_relative_to(root) else f.path
        lines.append(f"  {f.agent}: {rel} ({f.size} bytes)")
    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_generate(
    agent: str = "all",
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Generate agent configuration files from .memtomem/context.md.

    Args:
        agent: Agent name (claude, cursor, gemini, codex, copilot) or "all"
    """
    from memtomem.context.generator import GENERATORS
    from memtomem.context.parser import CONTEXT_FILENAME, parse_context

    root = _find_project_root()
    ctx_path = root / CONTEXT_FILENAME

    if not ctx_path.exists():
        return f"{CONTEXT_FILENAME} not found. Create it with 'mm context init'."

    sections = parse_context(ctx_path)
    if not sections:
        return f"{CONTEXT_FILENAME} is empty."

    targets = list(GENERATORS.keys()) if agent == "all" else [agent]
    results = []

    for name in targets:
        if name not in GENERATORS:
            results.append(f"Unknown agent: {name}")
            continue

        gen = GENERATORS[name]
        content = gen.generate(sections)
        out_path = root / gen.output_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        results.append(f"{name}: {gen.output_path}")

    return "Generated:\n" + "\n".join(results)


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_diff(
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show sync status between context.md and agent files."""
    from memtomem.context.detector import detect_agent_files
    from memtomem.context.generator import GENERATORS
    from memtomem.context.parser import CONTEXT_FILENAME, parse_context

    root = _find_project_root()
    ctx_path = root / CONTEXT_FILENAME

    if not ctx_path.exists():
        return f"{CONTEXT_FILENAME} not found."

    sections = parse_context(ctx_path)
    files = detect_agent_files(root)

    if not files:
        return "No agent files to compare."

    lines = []
    for f in files:
        gen = GENERATORS.get(f.agent)
        if not gen:
            continue

        current = f.path.read_text(encoding="utf-8").strip()
        expected = gen.generate(sections).strip()
        status = "in sync" if current == expected else "out of sync"
        lines.append(f"{f.agent}: {f.path.name} [{status}]")

    return "\n".join(lines) if lines else "No comparable agent files."


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_sync(
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Sync .memtomem/context.md to all detected agent files."""
    from memtomem.context.detector import detect_agent_files
    from memtomem.context.generator import GENERATORS
    from memtomem.context.parser import CONTEXT_FILENAME, parse_context

    root = _find_project_root()
    ctx_path = root / CONTEXT_FILENAME

    if not ctx_path.exists():
        return f"{CONTEXT_FILENAME} not found. Create it with 'mm context init'."

    sections = parse_context(ctx_path)
    files = detect_agent_files(root)

    if not files:
        return "No agent files detected. Use mem_context_generate to create them."

    agents_synced = set()
    results = []

    for f in files:
        if f.agent in agents_synced:
            continue
        gen = GENERATORS.get(f.agent)
        if not gen:
            continue

        content = gen.generate(sections)
        out_path = root / gen.output_path
        out_path.write_text(content, encoding="utf-8")
        results.append(f"{f.agent}: {gen.output_path}")
        agents_synced.add(f.agent)

    return "Synced:\n" + "\n".join(results)
