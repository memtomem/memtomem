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
from memtomem.context._names import InvalidNameError
from memtomem.context.detector import (
    detect_agent_dirs,
    detect_agent_files,
    detect_command_dirs,
    detect_settings_files,
    detect_skill_dirs,
)
from memtomem.context.install import (
    AlreadyInstalledError,
    AssetNotFoundError,
    NotInstalledError,
    ProjectClassification,
    StaleInstallError,
    _apply_update,
    _classify_for_all_update,
    install_agent,
    install_command,
    install_skill,
    update_agent,
    update_command,
    update_skill,
)
from memtomem.context.projects import KnownProjectsStore
from memtomem.context.lockfile import LockfileVersionError
from memtomem.context.generator import (
    GENERATORS,
    extract_sections_from_agent_file,
)
from memtomem.context.parser import CONTEXT_FILENAME, parse_context, sections_to_markdown
from memtomem.context.settings import (
    diff_settings,
    generate_all_settings,
    host_write_targets,
)
from memtomem.context.skills import (
    diff_skills,
    extract_skills_to_canonical,
    generate_all_skills,
)
from memtomem.wiki.store import WikiNotFoundError, WikiStore
from memtomem.config import ContextGatewayConfig

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
        "Supported: skills, agents, commands, settings."
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
    for name, reason, _code in result.skipped:
        click.secho(f"    skipped {name}: {reason}", fg="yellow")


def _print_skills_generate(root: Path) -> None:
    result = generate_all_skills(root)
    if result.generated:
        click.secho(f"  Skills fan-out: {len(result.generated)}", fg="green")
        for runtime, path in result.generated:
            rel = path.relative_to(root) if path.is_relative_to(root) else path
            click.echo(f"    {runtime:15s}  {rel}")
    for runtime, reason, _code in result.skipped:
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
    for name, reason, _code in result.skipped:
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
    for runtime, reason, _code in result.skipped:
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
    for name, reason, _code in result.skipped:
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
    for runtime, reason, _code in result.skipped:
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


def _confirm_settings_host_writes(root: Path, *, yes: bool) -> bool:
    """Prompt before mutating settings files outside the project root.

    Returns ``True`` when the caller may pass ``allow_host_writes=True``
    to :func:`generate_all_settings` (no host writes pending, ``--yes``
    supplied, or user confirmed at the prompt). Returns ``False`` when
    the user declines, in which case the caller should not invoke
    settings sync at all.
    """
    pending = host_write_targets(root)
    if not pending:
        return True
    if yes:
        return True
    click.secho(
        "Settings sync will modify the following files outside this project:",
        fg="yellow",
    )
    for p in pending:
        click.echo(f"  {p}")
    return click.confirm("Continue?", default=False)


def _print_settings_generate(root: Path, *, allow_host_writes: bool) -> None:
    results = generate_all_settings(root, allow_host_writes=allow_host_writes)
    for name, r in results.items():
        if r.status == "ok":
            click.secho(f"  Settings: {name} → {r.target}", fg="green")
            for w in r.warnings:
                click.secho(f"    warning: {w}", fg="yellow")
        elif r.status == "skipped":
            click.secho(f"  skipped {name}: {r.reason}", fg="yellow")
        elif r.status == "needs_confirmation":
            # Defense in depth: should not normally reach here because the CLI
            # caller already gated on ``_confirm_settings_host_writes``.
            click.secho(f"  needs confirmation {name}: {r.reason}", fg="yellow")
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
@click.option(
    "--yes",
    "-y",
    "yes",
    is_flag=True,
    help="Skip confirmation prompts before writing settings files outside this project.",
)
def generate_cmd(
    agent: str, include: tuple[str, ...], strict: bool, on_drop: str, yes: bool
) -> None:
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
        if _confirm_settings_host_writes(root, yes=yes):
            _print_settings_generate(root, allow_host_writes=True)
        else:
            click.secho("  Skipped settings sync (declined).", fg="yellow")

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
@click.option(
    "--yes",
    "-y",
    "yes",
    is_flag=True,
    help="Skip confirmation prompts before writing settings files outside this project.",
)
def sync_cmd(include: tuple[str, ...], strict: bool, on_drop: str, yes: bool) -> None:
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
        if _confirm_settings_host_writes(root, yes=yes):
            _print_settings_generate(root, allow_host_writes=True)
        else:
            click.secho("  Skipped settings sync (declined).", fg="yellow")

    click.secho("Synced.", fg="green")


@context.command("install")
@click.argument("asset_type", type=click.Choice(["skill", "agent", "command"]))
@click.argument("name")
def install_cmd(asset_type: str, name: str) -> None:
    """Install a wiki asset into ``<project>/.memtomem/<type>/<name>/``.

    The wiki at ``~/.memtomem-wiki/`` must be initialized first
    (``mm wiki init``). Skills, agents, and commands all flow through
    fan-out after install.
    """
    root = _find_project_root()
    try:
        if asset_type == "skill":
            result = install_skill(root, name)
        elif asset_type == "agent":
            result = install_agent(root, name)
        elif asset_type == "command":
            result = install_command(root, name)
        else:  # pragma: no cover — guarded by click.Choice
            raise click.ClickException(f"unknown asset type: {asset_type}")
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except AssetNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except AlreadyInstalledError as exc:
        raise click.ClickException(str(exc)) from exc
    except InvalidNameError as exc:
        raise click.ClickException(str(exc)) from exc
    except LockfileVersionError as exc:
        raise click.ClickException(str(exc)) from exc

    click.secho(
        f"Installed {result.asset_type}/{result.name} (wiki {result.wiki_commit[:12]})",
        fg="green",
    )
    rel_dest = result.dest.relative_to(root) if result.dest.is_relative_to(root) else result.dest
    click.echo(f"  → {rel_dest}/")
    click.echo(f"  {result.files_written} file(s) copied")


_WIKI_DIRTY_WARN = "warning: wiki has uncommitted changes; using HEAD which doesn't include them"


@context.command("update")
@click.argument("asset_type", type=click.Choice(["skill", "agent", "command"]))
@click.argument("name")
@click.option(
    "--all",
    "all_projects",
    is_flag=True,
    help="Apply across every known project that has this asset installed.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Skip the confirmation prompt — intended for automation (cron / CI).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite local edits; each dirty file is preserved as <file>.bak.",
)
def update_cmd(
    asset_type: str,
    name: str,
    all_projects: bool,
    yes: bool,
    force: bool,
) -> None:
    """Refresh an installed wiki asset from the wiki HEAD.

    Without ``--all``: refresh in the current project only. No-op when
    the wiki commit pinned in ``.memtomem/lock.json`` already matches
    the wiki HEAD — the lockfile is not touched and ``unchanged`` is
    reported. Refuses to write when local edits are detected, unless
    ``--force`` is passed (each dirty file is preserved as ``<file>.bak``
    before overwriting with wiki bytes).

    With ``--all``: classify the asset across every known project, print
    a 4-state preview (update / unchanged / refuse / error), prompt for
    confirmation, then refresh each project that needs it. ``--yes``
    skips the prompt for automation. ``--yes --force`` is the
    automation invariant — destructive batch with WARNING on stderr,
    no prompt.
    """
    root = _find_project_root()

    # Pin wiki + dirty warn — applies to both single-asset and --all paths.
    # Warn timing: at update entry, BEFORE classification, BEFORE prompt.
    try:
        wiki = WikiStore.at_default()
        wiki.require_exists()
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    if wiki.is_dirty():
        click.secho(_WIKI_DIRTY_WARN, fg="yellow", err=True)

    if all_projects:
        _run_update_all(asset_type, name, root, wiki=wiki, yes=yes, force=force)
        return

    try:
        if asset_type == "skill":
            result = update_skill(root, name, wiki=wiki, force=force)
        elif asset_type == "agent":
            result = update_agent(root, name, wiki=wiki, force=force)
        elif asset_type == "command":
            result = update_command(root, name, wiki=wiki, force=force)
        else:  # pragma: no cover — guarded by click.Choice
            raise click.ClickException(f"unknown asset type: {asset_type}")
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except AssetNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except NotInstalledError as exc:
        raise click.ClickException(str(exc)) from exc
    except StaleInstallError as exc:
        raise click.ClickException(str(exc)) from exc
    except InvalidNameError as exc:
        raise click.ClickException(str(exc)) from exc
    except LockfileVersionError as exc:
        raise click.ClickException(str(exc)) from exc

    if result.was_no_op:
        click.secho(
            f"{result.asset_type}/{result.name} unchanged (wiki {result.new_wiki_commit[:12]})",
            fg="cyan",
        )
        return

    click.secho(
        f"Updated {result.asset_type}/{result.name} "
        f"({result.old_wiki_commit[:12]} → {result.new_wiki_commit[:12]})",
        fg="green",
    )
    rel_dest = result.dest.relative_to(root) if result.dest.is_relative_to(root) else result.dest
    click.echo(f"  → {rel_dest}/")
    click.echo(f"  {result.files_written} file(s) updated")
    if result.bak_files_written:
        click.secho(
            f"  {len(result.bak_files_written)} dirty file(s) preserved as .bak",
            fg="yellow",
        )


def _run_update_all(
    asset_type: str,
    name: str,
    root: Path,
    *,
    wiki: WikiStore,
    yes: bool,
    force: bool,
) -> None:
    """Orchestrate ``mm context update <type> <name> --all``.

    Flow (matches plan locked decisions):
    1. Load known projects (``KnownProjectsStore.load()`` — gap E
       correction; the design's earlier ``list_entries()`` reference does
       not exist in the actual API).
    2. Classify across all roots in one pass — ``current_commit`` and
       ``is_asset_dirty`` each run at most once per project, all cached
       on :class:`ProjectClassification` for the execute phase.
    3. Print a 4-state preview table.
    4. Empty store / "no projects have asset" exit 0 informationally —
       cron/CI safety so a first-run before any registration succeeds.
    5. ``refuse`` without ``--force`` aborts the entire batch (no partial
       writes).
    6. ``--yes --force`` prints WARNING + skips prompt + executes.
    7. Serial execute consumes cached ``dirty_report`` / ``lock_entry``;
       no second walk per project.
    """
    cfg = ContextGatewayConfig()
    store = KnownProjectsStore(Path(cfg.known_projects_path).expanduser())
    project_entries = store.load()

    if not project_entries:
        click.echo(
            "No known projects registered. Add via the `mm web` UI "
            "or directly in known_projects.json.",
            err=True,
        )
        return

    project_roots = [e.root for e in project_entries if e.root.is_dir()]
    if not project_roots:
        click.echo(
            "No registered projects exist on disk; nothing to update.",
            err=True,
        )
        return

    asset_type_plural = f"{asset_type}s"

    try:
        new_commit, classifications = _classify_for_all_update(
            asset_type_plural,
            name,
            wiki=wiki,
            projects=project_roots,
        )
    except AssetNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except InvalidNameError as exc:
        raise click.ClickException(str(exc)) from exc

    if not classifications:
        click.echo(
            f"No projects have {asset_type_plural}/{name} installed.",
            err=True,
        )
        return

    _print_classification_table(classifications, asset_type_plural, name, new_commit)

    needs_update = [c for c in classifications if c.state == "update"]
    needs_force = [c for c in classifications if c.state == "refuse"]
    has_errors = [c for c in classifications if c.state == "error"]

    if not needs_update and not needs_force and not has_errors:
        click.echo("\nAll projects are up to date.")
        return

    if needs_force and not force:
        click.secho(
            f"\n{len(needs_force)} project(s) have local edits; "
            f"pass --force to overwrite (each dirty file gets a .bak sibling). "
            f"Refusing to write any project — re-run with --force or resolve manually.",
            fg="red",
            err=True,
        )
        raise click.exceptions.Exit(1)

    if yes and force:
        click.secho(
            "WARNING: --yes --force is destructive — all dirty files will be "
            "overwritten across the batch.",
            fg="red",
            err=True,
        )

    if not yes:
        click.confirm("\nContinue?", abort=True)

    successes = 0
    failures = 0
    for c in classifications:
        if c.state == "unchanged":
            click.secho(f"  - {c.project_root}: unchanged", fg="cyan")
            continue
        if c.state == "error":
            click.secho(f"  ✗ {c.project_root}: {c.reason}", fg="red")
            failures += 1
            continue

        # state in {"update", "refuse"} — execute via _apply_update with
        # cached dirty_report + lock_entry (no re-walk).
        assert c.lock_entry is not None
        assert c.dirty_report is not None
        src = wiki.root / asset_type_plural / name
        dest = c.project_root / ".memtomem" / asset_type_plural / name
        try:
            _apply_update(
                c.project_root,
                asset_type_plural,
                name,
                src=src,
                dest=dest,
                wiki_commit=new_commit,
                lock_entry=c.lock_entry,
                dirty_report=c.dirty_report,
                force=force,
            )
        except StaleInstallError as exc:
            click.secho(f"  ✗ {c.project_root}: {exc}", fg="red")
            failures += 1
        except OSError as exc:
            click.secho(f"  ✗ {c.project_root}: {exc}", fg="red")
            failures += 1
        else:
            click.secho(f"  ✓ {c.project_root}: updated", fg="green")
            successes += 1

    click.echo(
        f"\nSummary: {successes} updated, {failures} failed, "
        f"{len(classifications) - successes - failures} unchanged."
    )


def _print_classification_table(
    classifications: list[ProjectClassification],
    asset_type_plural: str,
    name: str,
    new_commit: str,
) -> None:
    """Render the 4-state preview table for ``--all`` confirmation.

    Columns: state · project root (relative when possible) · reason
    (only shown for ``refuse`` and ``error`` rows where it carries info).
    """
    click.echo(
        f"\n{asset_type_plural}/{name} — wiki HEAD {new_commit[:12]} — "
        f"{len(classifications)} project(s):"
    )
    state_color = {
        "update": "green",
        "unchanged": "cyan",
        "refuse": "yellow",
        "error": "red",
    }
    for c in classifications:
        color = state_color[c.state]
        line = f"  {c.state:10s}  {c.project_root}"
        if c.reason:
            line += f"  ({c.reason})"
        click.secho(line, fg=color)
