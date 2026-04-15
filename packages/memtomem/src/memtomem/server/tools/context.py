"""Tools: context_detect, context_generate, context_sync, context_diff."""

from __future__ import annotations

from pathlib import Path

from memtomem.server import mcp
from memtomem.server.context import CtxType
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

# Known --include values (mirrors cli.context_cmd._KNOWN_INCLUDES).
_KNOWN_INCLUDES: frozenset[str] = frozenset({"skills", "agents", "commands", "settings"})


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
    ctx: CtxType = None,
) -> str:
    """Detect agent configuration files in the current project.

    Scans for CLAUDE.md, .cursorrules, GEMINI.md, AGENTS.md,
    and .github/copilot-instructions.md. Pass
    ``include="skills,agents,commands"`` to also list runtime skill
    directories, sub-agent files, and slash-command files.
    """
    from memtomem.context.detector import (
        detect_agent_dirs,
        detect_agent_files,
        detect_command_dirs,
        detect_skill_dirs,
    )

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

    if "agents" in inc:
        agents = detect_agent_dirs(root)
        if lines:
            lines.append("")
        if agents:
            lines.append(f"{len(agents)} sub-agent file(s):")
            for a in agents:
                rel = a.path.relative_to(root) if a.path.is_relative_to(root) else a.path
                lines.append(f"  {a.agent}: {rel} ({a.size} bytes)")
        else:
            lines.append("No sub-agent files found.")

    if "commands" in inc:
        cmds = detect_command_dirs(root)
        if lines:
            lines.append("")
        if cmds:
            lines.append(f"{len(cmds)} slash-command file(s):")
            for c in cmds:
                rel = c.path.relative_to(root) if c.path.is_relative_to(root) else c.path
                lines.append(f"  {c.agent}: {rel} ({c.size} bytes)")
        else:
            lines.append("No slash-command files found.")

    if "settings" in inc:
        from memtomem.context.detector import detect_settings_files

        settings = detect_settings_files()
        if lines:
            lines.append("")
        if settings:
            lines.append(f"{len(settings)} settings file(s):")
            for s in settings:
                status = f"({s.size} bytes)" if s.size else "(not yet created)"
                lines.append(f"  {s.agent}: {s.path} {status}")
        else:
            lines.append("No settings files detected.")

    return "\n".join(lines) if lines else "Nothing detected."


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_generate(
    agent: str = "all",
    include: str = "",
    strict: bool = False,
    ctx: CtxType = None,
) -> str:
    """Generate agent configuration files from .memtomem/context.md.

    Args:
        agent: Agent name (claude, cursor, gemini, codex, copilot) or "all".
        include: Comma-separated extra artifact kinds
            (``skills``, ``agents``, ``commands``).
        strict: Promote dropped-field warnings to errors when converting
            sub-agents or slash commands.
    """
    from memtomem.context.agents import StrictDropError, generate_all_agents
    from memtomem.context.commands import (
        StrictDropError as CommandStrictDropError,
        generate_all_commands,
    )
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

    if "agents" in inc:
        try:
            agent_result = generate_all_agents(root, strict=strict)
        except StrictDropError as exc:
            return f"strict error: {exc}"
        if agent_result.generated:
            results.append("")
            results.append(f"Sub-agent fan-out: {len(agent_result.generated)}")
            for runtime, path in agent_result.generated:
                try:
                    rel = path.relative_to(root) if path.is_relative_to(root) else path
                except ValueError:
                    rel = path
                results.append(f"  {runtime}: {rel}")
        for runtime, reason in agent_result.skipped:
            results.append(f"  skipped {runtime}: {reason}")
        for runtime, agent_name, dropped in agent_result.dropped:
            results.append(f"  {runtime} dropped {dropped} from '{agent_name}'")

    if "commands" in inc:
        try:
            command_result = generate_all_commands(root, strict=strict)
        except CommandStrictDropError as exc:
            return f"strict error: {exc}"
        if command_result.generated:
            results.append("")
            results.append(f"Command fan-out: {len(command_result.generated)}")
            for runtime, path in command_result.generated:
                try:
                    rel = path.relative_to(root) if path.is_relative_to(root) else path
                except ValueError:
                    rel = path
                results.append(f"  {runtime}: {rel}")
        for runtime, reason in command_result.skipped:
            results.append(f"  skipped {runtime}: {reason}")
        for runtime, cmd_name, dropped in command_result.dropped:
            results.append(f"  {runtime} dropped {dropped} from '{cmd_name}'")

    if "settings" in inc:
        from memtomem.context.settings import generate_all_settings

        settings_results = generate_all_settings(root)
        for sname, sr in settings_results.items():
            if sr.status == "ok":
                results.append(f"\nSettings: {sname} → {sr.target}")
                for w in sr.warnings:
                    results.append(f"  warning: {w}")
            elif sr.status == "skipped":
                results.append(f"  skipped {sname}: {sr.reason}")
            elif sr.status in ("error", "aborted"):
                results.append(f"  {sr.status} {sname}: {sr.reason}")

    return "Generated:\n" + "\n".join(results)


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_diff(
    include: str = "",
    ctx: CtxType = None,
) -> str:
    """Show sync status between context.md and agent files.

    Pass ``include="skills,agents,commands"`` to also compare canonical
    skills, sub-agents, and slash commands against their runtime counterparts.
    """
    from memtomem.context.agents import diff_agents
    from memtomem.context.commands import diff_commands
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

    if "agents" in inc:
        rows = diff_agents(root)
        if rows:
            if lines:
                lines.append("")
            lines.append("Sub-agents:")
            for runtime, name, status in rows:
                lines.append(f"  {runtime}: {name} [{status}]")
        else:
            lines.append("No sub-agents to compare.")

    if "commands" in inc:
        rows = diff_commands(root)
        if rows:
            if lines:
                lines.append("")
            lines.append("Commands:")
            for runtime, name, status in rows:
                lines.append(f"  {runtime}: {name} [{status}]")
        else:
            lines.append("No commands to compare.")

    if "settings" in inc:
        from memtomem.context.settings import diff_settings as _diff_settings

        settings_results = _diff_settings(root)
        if settings_results:
            if lines:
                lines.append("")
            lines.append("Settings:")
            for sname, sr in settings_results.items():
                if sr.status in ("in sync", "out of sync", "missing target"):
                    lines.append(f"  {sname} [{sr.status}]")
                    for w in sr.warnings:
                        lines.append(f"    warning: {w}")
                elif sr.status == "skipped":
                    lines.append(f"  skipped {sname}: {sr.reason}")
                elif sr.status == "error":
                    lines.append(f"  error {sname}: {sr.reason}")

    return "\n".join(lines) if lines else "Nothing to compare."


@mcp.tool()
@tool_handler
@register("context")
async def mem_context_sync(
    include: str = "",
    strict: bool = False,
    ctx: CtxType = None,
) -> str:
    """Sync .memtomem/context.md to all detected agent files.

    Pass ``include="skills,agents,commands"`` to also fan out
    ``.memtomem/skills/``, ``.memtomem/agents/``, and ``.memtomem/commands/``
    to their runtime targets (Claude Code, Gemini CLI, Codex CLI).
    ``strict=True`` turns dropped sub-agent / command fields into errors.
    """
    from memtomem.context.agents import StrictDropError, generate_all_agents
    from memtomem.context.commands import (
        StrictDropError as CommandStrictDropError,
        generate_all_commands,
    )
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

    if "agents" in inc:
        try:
            agent_result = generate_all_agents(root, strict=strict)
        except StrictDropError as exc:
            return f"strict error: {exc}"
        if agent_result.generated:
            if results:
                results.append("")
            results.append(f"Sub-agent fan-out: {len(agent_result.generated)}")
            for runtime, path in agent_result.generated:
                try:
                    rel = path.relative_to(root) if path.is_relative_to(root) else path
                except ValueError:
                    rel = path
                results.append(f"  {runtime}: {rel}")
        for runtime, reason in agent_result.skipped:
            results.append(f"  skipped {runtime}: {reason}")
        for runtime, agent_name, dropped in agent_result.dropped:
            results.append(f"  {runtime} dropped {dropped} from '{agent_name}'")

    if "commands" in inc:
        try:
            command_result = generate_all_commands(root, strict=strict)
        except CommandStrictDropError as exc:
            return f"strict error: {exc}"
        if command_result.generated:
            if results:
                results.append("")
            results.append(f"Command fan-out: {len(command_result.generated)}")
            for runtime, path in command_result.generated:
                try:
                    rel = path.relative_to(root) if path.is_relative_to(root) else path
                except ValueError:
                    rel = path
                results.append(f"  {runtime}: {rel}")
        for runtime, reason in command_result.skipped:
            results.append(f"  skipped {runtime}: {reason}")
        for runtime, cmd_name, dropped in command_result.dropped:
            results.append(f"  {runtime} dropped {dropped} from '{cmd_name}'")

    if "settings" in inc:
        from memtomem.context.settings import generate_all_settings

        settings_results = generate_all_settings(root)
        for sname, sr in settings_results.items():
            if sr.status == "ok":
                if results:
                    results.append("")
                results.append(f"Settings: {sname} → {sr.target}")
                for w in sr.warnings:
                    results.append(f"  warning: {w}")
            elif sr.status == "skipped":
                results.append(f"  skipped {sname}: {sr.reason}")
            elif sr.status in ("error", "aborted"):
                results.append(f"  {sr.status} {sname}: {sr.reason}")

    return "Synced:\n" + "\n".join(results) if results else "Nothing to sync."
