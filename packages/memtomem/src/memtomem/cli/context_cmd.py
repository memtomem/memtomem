"""memtomem context — unified agent context management."""

from __future__ import annotations

from pathlib import Path

import click

from memtomem.context.agents import (
    ON_DROP_LEVELS,
    StrictDropError,
    diff_agents,
    extract_agents_to_canonical,
    generate_all_agents,
)
from memtomem.context.commands import (
    StrictDropError as CommandStrictDropError,
    diff_commands,
    extract_commands_to_canonical,
    generate_all_commands,
)
from memtomem.context.detector import (
    detect_agent_dirs,
    detect_agent_files,
    detect_command_dirs,
    detect_settings_files,
    detect_skill_dirs,
)
from memtomem.context.generator import (
    GENERATORS,
    extract_sections_from_agent_file,
)
from memtomem.context.parser import CONTEXT_FILENAME, parse_context, sections_to_markdown
from memtomem.context.settings import (
    diff_settings,
    generate_all_settings,
)
from memtomem.context.skills import (
    diff_skills,
    extract_skills_to_canonical,
    generate_all_skills,
)

# Phase 1-3 supports skills/agents/commands; Phase D adds settings.
_KNOWN_INCLUDES: frozenset[str] = frozenset({"skills", "agents", "commands", "settings"})


def _find_project_root() -> Path:
    """Walk up from cwd to find project root (has .git or pyproject.toml)."""
    p = Path.cwd()
    for _ in range(10):
        if (p / ".git").exists() or (p / "pyproject.toml").exists():
            return p
        p = p.parent
    return Path.cwd()


def _context_path(root: Path) -> Path:
    return root / CONTEXT_FILENAME


def _parse_include(include_tuple: tuple[str, ...]) -> set[str]:
    """Normalize ``--include`` values (repeatable option + comma-split within each)."""
    values: set[str] = set()
    for raw in include_tuple:
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if token not in _KNOWN_INCLUDES:
                raise click.BadParameter(
                    f"Unknown --include value '{token}'. Supported: {sorted(_KNOWN_INCLUDES)}"
                )
            values.add(token)
    return values


_INCLUDE_OPTION = click.option(
    "--include",
    "include",
    multiple=True,
    metavar="KIND",
    help=(
        "Additional artifact kinds to process (repeatable or comma-separated). "
        "Phase 1 supports: skills."
    ),
)


# ── Skill sub-handlers (shared by the commands below) ───────────────


def _print_skills_detect(root: Path) -> None:
    skills = detect_skill_dirs(root)
    if not skills:
        click.echo("  (no skill directories)")
        return
    click.secho(f"  {len(skills)} skill(s):", fg="cyan")
    for s in skills:
        rel = s.path.relative_to(root) if s.path.is_relative_to(root) else s.path
        click.echo(f"    {s.agent:15s}  {rel}  ({s.size} bytes)")


def _print_skills_init(root: Path, overwrite: bool) -> None:
    result = extract_skills_to_canonical(root, overwrite=overwrite)
    if result.imported:
        click.secho(f"  Imported {len(result.imported)} skill(s) → .memtomem/skills/", fg="green")
        for p in result.imported:
            click.echo(f"    {p.name}")
    else:
        click.echo("  (no runtime skills to import)")
    for name, reason in result.skipped:
        click.secho(f"    skipped {name}: {reason}", fg="yellow")


def _print_skills_generate(root: Path) -> None:
    result = generate_all_skills(root)
    if result.generated:
        click.secho(f"  Skills fan-out: {len(result.generated)}", fg="green")
        for runtime, path in result.generated:
            rel = path.relative_to(root) if path.is_relative_to(root) else path
            click.echo(f"    {runtime:15s}  {rel}")
    for runtime, reason in result.skipped:
        click.secho(f"  skipped {runtime}: {reason}", fg="yellow")


def _print_skills_diff(root: Path) -> None:
    rows = diff_skills(root)
    if not rows:
        click.echo("  (no skills to compare)")
        return
    for runtime, name, status in rows:
        color = "green" if status == "in sync" else "yellow"
        click.secho(f"  {runtime:15s}  {name}  [{status}]", fg=color)


# ── Sub-agent sub-handlers (Phase 2) ─────────────────────────────────


def _print_agents_detect(root: Path) -> None:
    agents = detect_agent_dirs(root)
    if not agents:
        click.echo("  (no sub-agent files)")
        return
    click.secho(f"  {len(agents)} sub-agent file(s):", fg="cyan")
    for a in agents:
        rel = a.path.relative_to(root) if a.path.is_relative_to(root) else a.path
        click.echo(f"    {a.agent:15s}  {rel}  ({a.size} bytes)")


def _print_agents_init(root: Path, overwrite: bool) -> None:
    result = extract_agents_to_canonical(root, overwrite=overwrite)
    if result.imported:
        click.secho(
            f"  Imported {len(result.imported)} sub-agent(s) → .memtomem/agents/", fg="green"
        )
        for p in result.imported:
            click.echo(f"    {p.stem}")
    else:
        click.echo("  (no runtime sub-agents to import)")
    for name, reason in result.skipped:
        click.secho(f"    skipped {name}: {reason}", fg="yellow")


def _print_agents_generate(root: Path, strict: bool, on_drop: str = "ignore") -> None:
    try:
        result = generate_all_agents(root, strict=strict, on_drop=on_drop)
    except StrictDropError as exc:
        click.secho(f"  [strict] {exc}", fg="red")
        raise click.Abort()

    if result.generated:
        click.secho(f"  Sub-agent fan-out: {len(result.generated)}", fg="green")
        for runtime, path in result.generated:
            try:
                rel = path.relative_to(root) if path.is_relative_to(root) else path
            except ValueError:
                rel = path
            click.echo(f"    {runtime:15s}  {rel}")
    for runtime, reason in result.skipped:
        click.secho(f"  skipped {runtime}: {reason}", fg="yellow")
    for runtime, agent_name, dropped in result.dropped:
        click.secho(
            f"  {runtime} dropped {dropped} from '{agent_name}'",
            fg="yellow",
        )


def _print_agents_diff(root: Path) -> None:
    rows = diff_agents(root)
    if not rows:
        click.echo("  (no sub-agents to compare)")
        return
    for runtime, name, status in rows:
        color = "green" if status == "in sync" else "yellow"
        click.secho(f"  {runtime:15s}  {name}  [{status}]", fg=color)


# ── Slash-command sub-handlers (Phase 3) ─────────────────────────────


def _print_commands_detect(root: Path) -> None:
    cmds = detect_command_dirs(root)
    if not cmds:
        click.echo("  (no slash command files)")
        return
    click.secho(f"  {len(cmds)} command file(s):", fg="cyan")
    for c in cmds:
        rel = c.path.relative_to(root) if c.path.is_relative_to(root) else c.path
        click.echo(f"    {c.agent:17s}  {rel}  ({c.size} bytes)")


def _print_commands_init(root: Path, overwrite: bool) -> None:
    result = extract_commands_to_canonical(root, overwrite=overwrite)
    if result.imported:
        click.secho(
            f"  Imported {len(result.imported)} command(s) → .memtomem/commands/", fg="green"
        )
        for p in result.imported:
            click.echo(f"    {p.stem}")
    else:
        click.echo("  (no runtime commands to import)")
    for name, reason in result.skipped:
        click.secho(f"    skipped {name}: {reason}", fg="yellow")


def _print_commands_generate(root: Path, strict: bool, on_drop: str = "ignore") -> None:
    try:
        result = generate_all_commands(root, strict=strict, on_drop=on_drop)
    except CommandStrictDropError as exc:
        click.secho(f"  [strict] {exc}", fg="red")
        raise click.Abort() from exc

    if result.generated:
        click.secho(f"  Command fan-out: {len(result.generated)}", fg="green")
        for runtime, path in result.generated:
            try:
                rel = path.relative_to(root) if path.is_relative_to(root) else path
            except ValueError:
                rel = path
            click.echo(f"    {runtime:17s}  {rel}")
    for runtime, reason in result.skipped:
        click.secho(f"  skipped {runtime}: {reason}", fg="yellow")
    for runtime, cmd_name, dropped in result.dropped:
        click.secho(
            f"  {runtime} dropped {dropped} from '{cmd_name}'",
            fg="yellow",
        )


def _print_commands_diff(root: Path) -> None:
    rows = diff_commands(root)
    if not rows:
        click.echo("  (no commands to compare)")
        return
    for runtime, name, status in rows:
        color = "green" if status == "in sync" else "yellow"
        click.secho(f"  {runtime:17s}  {name}  [{status}]", fg=color)


# ── Settings sub-handlers (Phase D) ─────────────────────────────────


def _print_settings_detect() -> None:
    files = detect_settings_files()
    if not files:
        click.echo("  (no settings files detected)")
        return
    click.secho(f"  {len(files)} settings file(s):", fg="cyan")
    for f in files:
        status = f"({f.size} bytes)" if f.size else "(not yet created)"
        click.echo(f"    {f.agent:17s}  {f.path}  {status}")


def _print_settings_generate(root: Path) -> None:
    results = generate_all_settings(root)
    for name, r in results.items():
        if r.status == "ok":
            click.secho(f"  Settings: {name} → {r.target}", fg="green")
            for w in r.warnings:
                click.secho(f"    warning: {w}", fg="yellow")
        elif r.status == "skipped":
            click.secho(f"  skipped {name}: {r.reason}", fg="yellow")
        elif r.status in ("error", "aborted"):
            click.secho(f"  {r.status} {name}: {r.reason}", fg="red")


def _print_settings_diff(root: Path) -> None:
    results = diff_settings(root)
    if not results:
        click.echo("  (no settings to compare)")
        return
    for name, r in results.items():
        if r.status in ("in sync", "out of sync", "missing target"):
            color = "green" if r.status == "in sync" else "yellow"
            click.secho(f"  {name:17s}  [{r.status}]", fg=color)
            for w in r.warnings:
                click.secho(f"    warning: {w}", fg="yellow")
        elif r.status == "skipped":
            click.secho(f"  skipped {name}: {r.reason}", fg="yellow")
        elif r.status == "error":
            click.secho(f"  error {name}: {r.reason}", fg="red")


@click.group("context")
def context() -> None:
    """Manage unified agent context (CLAUDE.md, .cursorrules, GEMINI.md, etc.)."""


@context.command("detect")
@_INCLUDE_OPTION
def detect_cmd(include: tuple[str, ...]) -> None:
    """Detect agent configuration files in the current project."""
    inc = _parse_include(include)
    root = _find_project_root()
    files = detect_agent_files(root)

    if not files and not inc:
        click.echo("No agent configuration files found.")
        return

    if files:
        click.secho(f"Found {len(files)} agent file(s):\n", fg="cyan")
        for f in files:
            rel = f.path.relative_to(root) if f.path.is_relative_to(root) else f.path
            click.echo(f"  {f.agent:10s}  {rel}  ({f.size} bytes)")

    if "skills" in inc:
        click.echo("")
        _print_skills_detect(root)

    if "agents" in inc:
        click.echo("")
        _print_agents_detect(root)

    if "commands" in inc:
        click.echo("")
        _print_commands_detect(root)

    if "settings" in inc:
        click.echo("")
        _print_settings_detect()


@context.command("init")
@_INCLUDE_OPTION
@click.option(
    "--overwrite",
    is_flag=True,
    help="Overwrite existing entries in .memtomem/skills/ when importing from runtimes.",
)
def init_cmd(include: tuple[str, ...], overwrite: bool) -> None:
    """Create .memtomem/context.md from existing agent files."""
    inc = _parse_include(include)
    root = _find_project_root()
    ctx_path = _context_path(root)

    if ctx_path.exists():
        if not click.confirm(f"{CONTEXT_FILENAME} already exists. Overwrite?", default=False):
            return

    # Detect existing files
    files = detect_agent_files(root)
    if not files:
        click.echo("No agent files found. Creating empty context template.")
        sections: dict[str, str] = {
            "Project": "- Name: \n- Language: \n- Package manager: ",
            "Commands": "- Build: \n- Test: \n- Lint: ",
            "Architecture": "",
            "Rules": "",
            "Style": "",
        }
    else:
        # Pick the richest file to extract from
        best = max(files, key=lambda f: f.size)
        click.echo(f"Extracting from {best.agent}: {best.path.name} ({best.size} bytes)")
        content = best.path.read_text(encoding="utf-8")
        sections = extract_sections_from_agent_file(content)

        # Merge other files for missing sections
        for f in files:
            if f.path == best.path:
                continue
            other_content = f.path.read_text(encoding="utf-8")
            other_sections = extract_sections_from_agent_file(other_content)
            for key, val in other_sections.items():
                if key not in sections and val.strip():
                    sections[key] = val

    ctx_path.parent.mkdir(parents=True, exist_ok=True)
    ctx_path.write_text(sections_to_markdown(sections), encoding="utf-8")
    click.secho(f"Created {CONTEXT_FILENAME}", fg="green")
    click.echo(f"  Sections: {', '.join(sections.keys())}")
    click.echo("  Edit this file, then run 'mm context generate' to sync.")

    if "skills" in inc:
        click.echo("")
        _print_skills_init(root, overwrite=overwrite)

    if "agents" in inc:
        click.echo("")
        _print_agents_init(root, overwrite=overwrite)

    if "commands" in inc:
        click.echo("")
        _print_commands_init(root, overwrite=overwrite)


@context.command("generate")
@click.option("--agent", "-a", default="all", help="Agent name or 'all'")
@_INCLUDE_OPTION
@click.option(
    "--strict",
    is_flag=True,
    help="Legacy alias for --on-drop=error.",
)
@click.option(
    "--on-drop",
    "on_drop",
    type=click.Choice(ON_DROP_LEVELS),
    default="ignore",
    help="Severity when fields are dropped: ignore (default), warn, or error.",
)
def generate_cmd(agent: str, include: tuple[str, ...], strict: bool, on_drop: str) -> None:
    """Generate agent files from .memtomem/context.md."""
    inc = _parse_include(include)
    root = _find_project_root()
    ctx_path = _context_path(root)

    # Project memory (CLAUDE.md / GEMINI.md / ...) branch
    if ctx_path.exists():
        sections = parse_context(ctx_path)
        if not sections:
            click.secho(f"{CONTEXT_FILENAME} is empty.", fg="yellow")
        else:
            targets = list(GENERATORS.keys()) if agent == "all" else [agent]

            for name in targets:
                if name not in GENERATORS:
                    click.secho(
                        f"Unknown agent: {name}. Available: {', '.join(GENERATORS)}", fg="red"
                    )
                    continue

                gen = GENERATORS[name]
                content = gen.generate(sections)
                out_path = root / gen.output_path
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(content, encoding="utf-8")
                click.echo(f"  {name:10s}  {gen.output_path}")
    elif not inc:
        click.secho(f"{CONTEXT_FILENAME} not found. Run 'mm context init' first.", fg="red")
        return
    else:
        click.secho(f"  ({CONTEXT_FILENAME} missing — skipping project memory)", fg="yellow")

    if "skills" in inc:
        click.echo("")
        _print_skills_generate(root)

    if "agents" in inc:
        click.echo("")
        _print_agents_generate(root, strict=strict, on_drop=on_drop)

    if "commands" in inc:
        click.echo("")
        _print_commands_generate(root, strict=strict, on_drop=on_drop)

    if "settings" in inc:
        click.echo("")
        _print_settings_generate(root)

    click.secho("Done.", fg="green")


@context.command("diff")
@_INCLUDE_OPTION
def diff_cmd(include: tuple[str, ...]) -> None:
    """Show differences between context.md and agent files."""
    inc = _parse_include(include)
    root = _find_project_root()
    ctx_path = _context_path(root)

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

                if current == expected:
                    click.secho(f"  {f.agent:10s}  {f.path.name}  [in sync]", fg="green")
                else:
                    click.secho(f"  {f.agent:10s}  {f.path.name}  [out of sync]", fg="yellow")
        else:
            click.echo("No agent files to compare.")
    elif not inc:
        click.secho(f"{CONTEXT_FILENAME} not found.", fg="red")
        return
    else:
        click.secho(f"  ({CONTEXT_FILENAME} missing — skipping project memory)", fg="yellow")

    if "skills" in inc:
        click.echo("")
        _print_skills_diff(root)

    if "agents" in inc:
        click.echo("")
        _print_agents_diff(root)

    if "commands" in inc:
        click.echo("")
        _print_commands_diff(root)

    if "settings" in inc:
        click.echo("")
        _print_settings_diff(root)


@context.command("sync")
@_INCLUDE_OPTION
@click.option(
    "--strict",
    is_flag=True,
    help="Legacy alias for --on-drop=error.",
)
@click.option(
    "--on-drop",
    "on_drop",
    type=click.Choice(ON_DROP_LEVELS),
    default="ignore",
    help="Severity when fields are dropped: ignore (default), warn, or error.",
)
def sync_cmd(include: tuple[str, ...], strict: bool, on_drop: str) -> None:
    """Sync context.md to all detected agent files."""
    inc = _parse_include(include)
    root = _find_project_root()
    ctx_path = _context_path(root)

    if ctx_path.exists():
        sections = parse_context(ctx_path)
        files = detect_agent_files(root)

        if files:
            agents_to_sync = {f.agent for f in files}

            for agent_name in sorted(agents_to_sync):
                gen = GENERATORS.get(agent_name)
                if not gen:
                    continue

                content = gen.generate(sections)
                out_path = root / gen.output_path
                out_path.write_text(content, encoding="utf-8")
                click.echo(f"  {agent_name:10s}  {gen.output_path}")
        else:
            click.echo(
                "No agent files detected. Use 'mm context generate --agent all' to create them."
            )
    elif not inc:
        click.secho(f"{CONTEXT_FILENAME} not found. Run 'mm context init' first.", fg="red")
        return
    else:
        click.secho(f"  ({CONTEXT_FILENAME} missing — skipping project memory)", fg="yellow")

    if "skills" in inc:
        click.echo("")
        _print_skills_generate(root)

    if "agents" in inc:
        click.echo("")
        _print_agents_generate(root, strict=strict, on_drop=on_drop)

    if "commands" in inc:
        click.echo("")
        _print_commands_generate(root, strict=strict, on_drop=on_drop)

    if "settings" in inc:
        click.echo("")
        _print_settings_generate(root)

    click.secho("Synced.", fg="green")
