"""Tools: context_detect, context_generate, context_sync, context_diff."""

from __future__ import annotations

from pathlib import Path

from memtomem.server import mcp
from memtomem.server.context import CtxType
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

# Known --include values (mirrors cli.context_cmd._KNOWN_INCLUDES).
_KNOWN_INCLUDES: frozenset[str] = frozenset({"skills"})


def _find_project_root() -> Path:
    """Walk up from cwd to find project root."""
    p = Path.cwd()
    for _ in range(10):
        if (p / ".git").exists() or (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


def _parse_include(include: str) -> set[str]:
    """Parse a comma-separated ``include`` argument coming from an MCP caller."""
    values: set[str] = set()
    for token in include.split(","):
        token = token.strip()
        if not token:
            continue
        if token not in _KNOWN_INCLUDES:
            raise ValueError(
                f"Unknown include value '{token}'. Supported: {sorted(_KNOWN_INCLUDES)}"
            )
        values.add(token)
    return values


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_detect(
    include: str = "",
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Detect agent configuration files in the current project.

    Scans for CLAUDE.md, .cursorrules, GEMINI.md, AGENTS.md,
    and .github/copilot-instructions.md. Pass ``include="skills"`` to also
    list runtime skill directories (.claude/skills/, .gemini/skills/,
    .agents/skills/).
    """
    from memtomem.context.detector import detect_agent_files, detect_skill_dirs

    inc = _parse_include(include)
    root = _find_project_root()
    files = detect_agent_files(root)

    lines: list[str] = []
    if files:
        lines.append(f"Found {len(files)} agent file(s):\n")
        for f in files:
            rel = f.path.relative_to(root) if f.path.is_relative_to(root) else f.path
            lines.append(f"  {f.agent}: {rel} ({f.size} bytes)")
    elif not inc:
        return "No agent configuration files found."

    if "skills" in inc:
        skills = detect_skill_dirs(root)
        if lines:
            lines.append("")
        if skills:
            lines.append(f"{len(skills)} skill(s):")
            for s in skills:
                rel = s.path.relative_to(root) if s.path.is_relative_to(root) else s.path
                lines.append(f"  {s.agent}: {rel} ({s.size} bytes)")
        else:
            lines.append("No skill directories found.")

    return "\n".join(lines) if lines else "Nothing detected."


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_generate(
    agent: str = "all",
    include: str = "",
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Generate agent configuration files from .memtomem/context.md.

    Args:
        agent: Agent name (claude, cursor, gemini, codex, copilot) or "all".
        include: Comma-separated extra artifact kinds. Phase 1: ``skills``.
    """
    from memtomem.context.generator import GENERATORS
    from memtomem.context.parser import CONTEXT_FILENAME, parse_context
    from memtomem.context.skills import generate_all_skills

    inc = _parse_include(include)
    root = _find_project_root()
    ctx_path = root / CONTEXT_FILENAME

    results: list[str] = []

    if ctx_path.exists():
        sections = parse_context(ctx_path)
        if sections:
            targets = list(GENERATORS.keys()) if agent == "all" else [agent]
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
        else:
            results.append(f"{CONTEXT_FILENAME} is empty.")
    elif not inc:
        return f"{CONTEXT_FILENAME} not found. Create it with 'mm context init'."
    else:
        results.append(f"({CONTEXT_FILENAME} missing — skipping project memory)")

    if "skills" in inc:
        skill_result = generate_all_skills(root)
        if skill_result.generated:
            results.append("")
            results.append(f"Skills fan-out: {len(skill_result.generated)}")
            for runtime, path in skill_result.generated:
                rel = path.relative_to(root) if path.is_relative_to(root) else path
                results.append(f"  {runtime}: {rel}")
        for runtime, reason in skill_result.skipped:
            results.append(f"  skipped {runtime}: {reason}")

    return "Generated:\n" + "\n".join(results)


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_diff(
    include: str = "",
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Show sync status between context.md and agent files.

    Pass ``include="skills"`` to also compare canonical and runtime skill dirs.
    """
    from memtomem.context.detector import detect_agent_files
    from memtomem.context.generator import GENERATORS
    from memtomem.context.parser import CONTEXT_FILENAME, parse_context
    from memtomem.context.skills import diff_skills

    inc = _parse_include(include)
    root = _find_project_root()
    ctx_path = root / CONTEXT_FILENAME

    lines: list[str] = []

    if ctx_path.exists():
        sections = parse_context(ctx_path)
        files = detect_agent_files(root)

        if files:
            for f in files:
                gen = GENERATORS.get(f.agent)
                if not gen:
                    continue
                current = f.path.read_text(encoding="utf-8").strip()
                expected = gen.generate(sections).strip()
                status = "in sync" if current == expected else "out of sync"
                lines.append(f"{f.agent}: {f.path.name} [{status}]")
        elif not inc:
            return "No agent files to compare."
    elif not inc:
        return f"{CONTEXT_FILENAME} not found."
    else:
        lines.append(f"({CONTEXT_FILENAME} missing — skipping project memory)")

    if "skills" in inc:
        rows = diff_skills(root)
        if rows:
            if lines:
                lines.append("")
            lines.append("Skills:")
            for runtime, name, status in rows:
                lines.append(f"  {runtime}: {name} [{status}]")
        else:
            lines.append("No skills to compare.")

    return "\n".join(lines) if lines else "Nothing to compare."


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_sync(
    include: str = "",
    ctx: CtxType = None,  # type: ignore[assignment]
) -> str:
    """Sync .memtomem/context.md to all detected agent files.

    Pass ``include="skills"`` to also fan out .memtomem/skills/ to
    .claude/skills/, .gemini/skills/, and .agents/skills/ (Codex CLI).
    """
    from memtomem.context.detector import detect_agent_files
    from memtomem.context.generator import GENERATORS
    from memtomem.context.parser import CONTEXT_FILENAME, parse_context
    from memtomem.context.skills import generate_all_skills

    inc = _parse_include(include)
    root = _find_project_root()
    ctx_path = root / CONTEXT_FILENAME

    results: list[str] = []

    if ctx_path.exists():
        sections = parse_context(ctx_path)
        files = detect_agent_files(root)

        if files:
            agents_synced: set[str] = set()
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
        elif not inc:
            return "No agent files detected. Use mem_context_generate to create them."
    elif not inc:
        return f"{CONTEXT_FILENAME} not found. Create it with 'mm context init'."
    else:
        results.append(f"({CONTEXT_FILENAME} missing — skipping project memory)")

    if "skills" in inc:
        skill_result = generate_all_skills(root)
        if skill_result.generated:
            if results:
                results.append("")
            results.append(f"Skills fan-out: {len(skill_result.generated)}")
            for runtime, path in skill_result.generated:
                rel = path.relative_to(root) if path.is_relative_to(root) else path
                results.append(f"  {runtime}: {rel}")
        for runtime, reason in skill_result.skipped:
            results.append(f"  skipped {runtime}: {reason}")

    return "Synced:\n" + "\n".join(results) if results else "Nothing to sync."
