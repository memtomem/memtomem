"""memtomem context — unified agent context management."""

from __future__ import annotations

from pathlib import Path

import click

from memtomem.context.detector import detect_agent_files
from memtomem.context.generator import (
    GENERATORS,
    extract_sections_from_agent_file,
)
from memtomem.context.parser import CONTEXT_FILENAME, parse_context, sections_to_markdown


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


@click.group("context")
def context() -> None:
    """Manage unified agent context (CLAUDE.md, .cursorrules, GEMINI.md, etc.)."""


@context.command("detect")
def detect_cmd() -> None:
    """Detect agent configuration files in the current project."""
    root = _find_project_root()
    files = detect_agent_files(root)

    if not files:
        click.echo("No agent configuration files found.")
        return

    click.secho(f"Found {len(files)} agent file(s):\n", fg="cyan")
    for f in files:
        rel = f.path.relative_to(root) if f.path.is_relative_to(root) else f.path
        click.echo(f"  {f.agent:10s}  {rel}  ({f.size} bytes)")


@context.command("init")
def init_cmd() -> None:
    """Create .memtomem/context.md from existing agent files."""
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


@context.command("generate")
@click.option("--agent", "-a", default="all", help="Agent name or 'all'")
def generate_cmd(agent: str) -> None:
    """Generate agent files from .memtomem/context.md."""
    root = _find_project_root()
    ctx_path = _context_path(root)

    if not ctx_path.exists():
        click.secho(f"{CONTEXT_FILENAME} not found. Run 'mm context init' first.", fg="red")
        return

    sections = parse_context(ctx_path)
    if not sections:
        click.secho(f"{CONTEXT_FILENAME} is empty.", fg="yellow")
        return

    targets = list(GENERATORS.keys()) if agent == "all" else [agent]

    for name in targets:
        if name not in GENERATORS:
            click.secho(f"Unknown agent: {name}. Available: {', '.join(GENERATORS)}", fg="red")
            continue

        gen = GENERATORS[name]
        content = gen.generate(sections)
        out_path = root / gen.output_path
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(content, encoding="utf-8")
        click.echo(f"  {name:10s}  {gen.output_path}")

    click.secho("Done.", fg="green")


@context.command("diff")
def diff_cmd() -> None:
    """Show differences between context.md and agent files."""
    root = _find_project_root()
    ctx_path = _context_path(root)

    if not ctx_path.exists():
        click.secho(f"{CONTEXT_FILENAME} not found.", fg="red")
        return

    sections = parse_context(ctx_path)
    files = detect_agent_files(root)

    if not files:
        click.echo("No agent files to compare.")
        return

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


@context.command("sync")
def sync_cmd() -> None:
    """Sync context.md to all detected agent files."""
    root = _find_project_root()
    ctx_path = _context_path(root)

    if not ctx_path.exists():
        click.secho(f"{CONTEXT_FILENAME} not found. Run 'mm context init' first.", fg="red")
        return

    sections = parse_context(ctx_path)
    files = detect_agent_files(root)

    if not files:
        click.echo("No agent files detected. Use 'mm context generate --agent all' to create them.")
        return

    agents_to_sync = {f.agent for f in files}

    for agent_name in sorted(agents_to_sync):
        gen = GENERATORS.get(agent_name)
        if not gen:
            continue

        content = gen.generate(sections)
        out_path = root / gen.output_path
        out_path.write_text(content, encoding="utf-8")
        click.echo(f"  {agent_name:10s}  {gen.output_path}")

    click.secho("Synced.", fg="green")
