"""memtomem context — unified agent context management."""

from __future__ import annotations

import json
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
    CommitNotFoundError,
    NotInstalledError,
    ProjectClassification,
    ProjectInstallClassification,
    StaleInstallError,
    _apply_pinned_install,
    _apply_update,
    _classify_for_all_update,
    _classify_for_install_all,
    install_agent,
    install_command,
    install_skill,
    update_agent,
    update_command,
    update_skill,
)
from memtomem.context.migrate import (
    MigrateRow,
    classify_migrate,
    migrate_one,
)
from memtomem.context.projects import KnownProjectsStore
from memtomem.context.lockfile import LockfileVersionError
from memtomem.context.status import classify_status, load_with_recovery
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
from memtomem.context.settings_doctor import (
    detect_duplicate_tiers,
    format_warning,
)
from memtomem.context.settings_migrate import (
    apply_migration,
    format_plan_summary,
    plan_migration,
)
from memtomem.context.skills import (
    diff_skills,
    extract_skills_to_canonical,
    generate_all_skills,
)
from memtomem.wiki.store import WikiNotFoundError, WikiStore
from typing import get_args

from memtomem.config import (
    ContextGatewayConfig,
    Mem2MemConfig,
    TargetScope,
    load_config_d,
    load_config_overrides,
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


def _resolve_cli_scope(override: str | None) -> str:
    """Return the resolved ``hooks.target_scope`` for a CLI invocation.

    Per-invocation ``--scope`` flag wins; otherwise build a fresh
    ``Mem2MemConfig`` and apply the user-level config + env overrides.
    Always passes ``migrate=False`` because scope resolution is itself
    a read-only lookup — even from mutating commands (sync/generate)
    the migration belongs in the path that actually persists state, not
    in the scope read (see ``feedback_doctor_no_migration_loader``).
    """
    if override is not None:
        return override
    cfg = Mem2MemConfig()
    load_config_d(cfg, quiet=True)
    load_config_overrides(cfg, migrate=False)
    return cfg.hooks.target_scope


def _print_settings_detect(root: Path, scope: str) -> None:
    files = detect_settings_files(root, scope)
    if not files:
        click.echo("  (no settings files detected)")
        return
    click.secho(f"  {len(files)} settings file(s):", fg="cyan")
    for f in files:
        status = f"({f.size} bytes)" if f.size else "(not yet created)"
        click.echo(f"    {f.agent:17s}  {f.path}  {status}")


def _confirm_settings_host_writes(root: Path, *, scope: str, yes: bool) -> bool:
    """Prompt before mutating settings files outside the project root.

    Returns ``True`` when the caller may pass ``allow_host_writes=True``
    to :func:`generate_all_settings` (no host writes pending, ``--yes``
    supplied, or user confirmed at the prompt). Returns ``False`` when
    the user declines, in which case the caller should not invoke
    settings sync at all.

    The host-write check is computed against *scope* so
    ``--scope=project_local`` skips the prompt: project-tier writes stay
    inside *root* and never leave the project.
    """
    pending = host_write_targets(root, scope=scope)
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


def _print_duplicate_tier_warnings(root: Path, *, scope: str) -> None:
    """Emit non-blocking warnings for memtomem hooks duplicated across tiers.

    Per ADR-0010 §4 this is the primary detection surface: it fires in
    the user's actual sync workflow rather than behind a separate
    command. Output goes to stderr with yellow color and never blocks
    the sync — duplicates are informational.
    """
    duplicates = detect_duplicate_tiers(root, active_scope=scope)
    for dup in duplicates:
        click.secho(f"  warning: {format_warning(dup, active_scope=scope)}", err=True, fg="yellow")


def _print_settings_generate(root: Path, *, scope: str, allow_host_writes: bool) -> None:
    _print_duplicate_tier_warnings(root, scope=scope)
    results = generate_all_settings(root, scope=scope, allow_host_writes=allow_host_writes)
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


def _print_settings_diff(root: Path, *, scope: str) -> None:
    _print_duplicate_tier_warnings(root, scope=scope)
    results = diff_settings(root, scope=scope)
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


_SCOPE_OPTION = click.option(
    "--scope",
    "scope_flag",
    type=click.Choice(list(get_args(TargetScope))),
    default=None,
    help=(
        "Override hooks.target_scope for this invocation only "
        "(user / project_shared / project_local). "
        "Host-write confirmation is computed against the resolved scope, "
        "so --scope=project_local skips the prompt (writes stay in-project)."
    ),
)


_RULES_STYLE_MERGE_RUNTIMES: frozenset[str] = frozenset({"cursor", "codex", "copilot"})


def _warn_rules_style_merge(sections: dict[str, str], targets: list[str]) -> None:
    """Warn when generated targets fold Rules and Style into one block.

    Cursor / Codex / Copilot generators concatenate Rules + Style under a
    single heading via ``_compact_rules`` in :mod:`memtomem.context.generator`.
    Claude / Gemini emit them as separate ``##`` blocks. When both sections
    are present in ``context.md`` and at least one merging runtime is in the
    target list, surface a single stderr notice naming only the affected
    runtimes — file format itself is unchanged.
    """
    if not (sections.get("Rules", "").strip() and sections.get("Style", "").strip()):
        return
    affected = [t for t in targets if t in _RULES_STYLE_MERGE_RUNTIMES]
    if not affected:
        return
    click.secho(
        f"warning: {'/'.join(affected)} merge Rules and Style into a single block; "
        "context.md remains the source of truth — edit there, not in generated files.",
        err=True,
        fg="yellow",
    )


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
        _print_settings_detect(root, _resolve_cli_scope(None))


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
        click.secho("No agent files found. Creating empty context template.", fg="yellow")
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
@_SCOPE_OPTION
def generate_cmd(
    agent: str,
    include: tuple[str, ...],
    strict: bool,
    on_drop: str,
    yes: bool,
    scope_flag: str | None,
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

            _warn_rules_style_merge(sections, targets)

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
        scope = _resolve_cli_scope(scope_flag)
        if _confirm_settings_host_writes(root, scope=scope, yes=yes):
            _print_settings_generate(root, scope=scope, allow_host_writes=True)
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
        _print_settings_diff(root, scope=_resolve_cli_scope(None))


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
@_SCOPE_OPTION
def sync_cmd(
    include: tuple[str, ...],
    strict: bool,
    on_drop: str,
    yes: bool,
    scope_flag: str | None,
) -> None:
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
        scope = _resolve_cli_scope(scope_flag)
        if _confirm_settings_host_writes(root, scope=scope, yes=yes):
            _print_settings_generate(root, scope=scope, allow_host_writes=True)
        else:
            click.secho("  Skipped settings sync (declined).", fg="yellow")

    click.secho("Synced.", fg="green")


@context.command("install")
@click.argument("asset_type", type=click.Choice(["skill", "agent", "command"]), required=False)
@click.argument("name", required=False)
@click.option(
    "--all",
    "all_assets",
    is_flag=True,
    help="Re-install every entry from <project>/.memtomem/lock.json at its pinned commit.",
)
@click.option(
    "--yes",
    is_flag=True,
    help="Skip the confirmation prompt — intended for automation (cron / CI).",
)
@click.option(
    "--force",
    is_flag=True,
    help="Overwrite dirty dest; each dirty file is preserved as <file>.bak.",
)
def install_cmd(
    asset_type: str | None,
    name: str | None,
    all_assets: bool,
    yes: bool,
    force: bool,
) -> None:
    """Install a wiki asset into ``<project>/.memtomem/<type>/<name>/``.

    Without ``--all``: install a single ``<type> <name>`` from the wiki at
    HEAD. The wiki at ``~/.memtomem-wiki/`` must be initialized first
    (``mm wiki init``).

    With ``--all``: walk ``<project>/.memtomem/lock.json`` and re-install
    every entry **at the commit each entry pins** (NOT wiki HEAD). This
    is the "fresh-machine restore" verb: a teammate ``git clone``s the
    project, runs ``mm context install --all``, and reproduces the exact
    asset bytes the lockfile recorded. To advance to wiki HEAD instead,
    use ``mm context update --all``.
    """
    if all_assets:
        if asset_type or name:
            raise click.UsageError("`--all` takes no <type> <name> arguments")
        root = _find_project_root()
        _run_install_all(root, yes=yes, force=force)
        return

    if not asset_type or not name:
        raise click.UsageError("install requires <type> <name>, or pass --all")
    if yes or force:
        raise click.UsageError("--yes / --force only valid with --all")

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
    except InvalidNameError as exc:
        # Validation gate fires before any per-project loop runs; surface
        # the message verbatim so the user knows which name was rejected.
        raise click.ClickException(str(exc)) from exc
    except AssetNotFoundError as exc:
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


_STATUS_GLYPH: dict[str, tuple[str, str]] = {
    # state -> (glyph, click color)
    "ok": ("✓", "green"),
    "behind": ("↑", "cyan"),
    "dirty": ("✗", "yellow"),
    "missing": ("!", "red"),
    "stale-pin": ("⚠", "red"),
}


@context.command("status")
def status_cmd() -> None:
    """Show installed wiki assets and their drift state.

    Read-only. Walks ``<project>/.memtomem/lock.json`` and classifies
    each entry against the dest tree and the wiki. Always exits 0 for
    normal runs (cron-friendly chaining via ``mm context status && mm
    context update --all``); only a corrupt / version-mismatched
    lockfile produces a non-zero exit.
    """
    root = _find_project_root()

    # Diagnostic lockfile read — surfaces a version mismatch as an
    # error row at the top without crashing on the strict-mode path.
    _doc, lockfile_error = load_with_recovery(root)

    wiki = WikiStore.at_default()
    wiki_head, rows = classify_status(root, wiki=wiki)

    if lockfile_error is not None:
        click.secho(f"  ✗ lock.json: {lockfile_error}", fg="red", err=True)

    # Header — counts + wiki HEAD (or "wiki not present" annotation).
    if wiki_head is None:
        wiki_root = wiki.root
        click.echo(
            f".memtomem/ — {len(rows)} asset(s) installed — "
            f"wiki not present at {wiki_root}; pin reachability not checked"
        )
    else:
        click.echo(f".memtomem/ — {len(rows)} asset(s) installed — wiki HEAD {wiki_head[:12]}")

    if not rows and lockfile_error is None:
        click.echo("\nNo wiki assets installed in this project.")
        return

    # Sectioned by asset type, preserving iter_entries() order
    # (alphabetical: agents → commands → skills, names alpha within).
    last_type: str | None = None
    summary: dict[str, int] = {"ok": 0, "behind": 0, "dirty": 0, "missing": 0, "stale-pin": 0}
    for row in rows:
        if row.asset_type != last_type:
            click.secho(f"\n{row.asset_type}", fg="cyan")
            last_type = row.asset_type
        glyph, color = _STATUS_GLYPH[row.state]
        installed_date = row.installed_at[:10] if row.installed_at else "—"
        line = (
            f"  {glyph}  {row.name:24s}  {(row.pin_commit or '?')[:12]}  installed {installed_date}"
        )
        if row.reason:
            line += f"  ({row.reason})"
        click.secho(line, fg=color)
        summary[row.state] += 1

    if rows:
        parts = [
            f"{summary[k]} {k}"
            for k in ("ok", "behind", "dirty", "missing", "stale-pin")
            if summary[k] > 0
        ]
        if parts:
            click.echo("\nSummary: " + ", ".join(parts) + ".")

    if lockfile_error is not None:
        raise click.exceptions.Exit(1)


# ── install --all (PR-D C3) ─────────────────────────────────────────────


def _run_install_all(
    root: Path,
    *,
    yes: bool,
    force: bool,
) -> None:
    """Orchestrate ``mm context install --all`` (Option A: lockfile-pin restore).

    Flow:

    1. ``WikiStore.require_exists()`` — install --all needs the wiki repo
       to read pinned commits via ``git show``. Without it, abort cleanly.
    2. **No wiki dirty warning.** ``git show <pin>:<path>`` reads from
       committed objects, so a dirty working tree in the wiki is
       irrelevant — intentionally diverges from ``update --all`` which
       does warn (it cares about HEAD).
    3. Classify every entry in ``<project>/.memtomem/lock.json`` once
       via ``_classify_for_install_all`` (cached lock_entry +
       dirty_report passed through to execute).
    4. Empty lockfile → "No entries..." stderr, exit 0.
    5. Print 5-state preview table.
    6. If only skip/orphan/error rows (no install/refuse) → exit 0.
    7. ``refuse`` rows + no ``--force`` → red error + exit 1.
    8. ``--yes --force`` → destructive WARNING.
    9. Confirm prompt unless ``--yes``.
    10. Serial execute loop with row-level icons.
    11. Summary line.
    """
    try:
        wiki = WikiStore.at_default()
        wiki.require_exists()
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        classifications = _classify_for_install_all(root, wiki=wiki)
    except LockfileVersionError as exc:
        raise click.ClickException(str(exc)) from exc

    if not classifications:
        click.echo(
            "No entries in lock.json — run `mm context install <type> <name>` first.",
            err=True,
        )
        return

    _print_install_classification_table(classifications)

    needs_install = [c for c in classifications if c.state == "install"]
    needs_force = [c for c in classifications if c.state == "refuse"]
    has_skip_force = force and any(c.state == "skip" for c in classifications)
    has_errors = [c for c in classifications if c.state == "error"]

    # Refuse-blocks-batch: any dirty entry without --force aborts before any
    # writes (mirrors update --all). Errors classified upfront still get
    # surfaced via the table; we just refuse to continue.
    if needs_force and not force:
        click.secho(
            f"\n{len(needs_force)} entry(ies) have local edits; "
            f"pass --force to overwrite (each dirty file gets a .bak sibling). "
            f"Refusing to write any entry — re-run with --force or resolve manually.",
            fg="red",
            err=True,
        )
        raise click.exceptions.Exit(1)

    actionable = bool(needs_install) or bool(needs_force) or has_skip_force
    if not actionable:
        # Only skip / orphan / error rows; nothing to write.
        click.echo("\nNothing to install.")
        if has_errors:
            raise click.exceptions.Exit(1)
        return

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
    skipped = 0
    failures = 0
    orphans = 0
    for c in classifications:
        if c.state == "orphan":
            click.secho(f"  ⚠ {c.asset_type}/{c.name}: {c.reason}", fg="yellow")
            orphans += 1
            continue
        if c.state == "error":
            click.secho(f"  ✗ {c.asset_type}/{c.name}: {c.reason}", fg="red")
            failures += 1
            continue
        if c.state == "skip" and not force:
            click.secho(f"  - {c.asset_type}/{c.name}: already installed", fg="cyan")
            skipped += 1
            continue

        # state ∈ {"install", "skip" with --force, "refuse"} — execute
        try:
            _apply_pinned_install(root, c, wiki=wiki, force=force)
        except StaleInstallError as exc:
            # state=refuse without --force shouldn't reach here; defense in depth.
            click.secho(f"  ✗ {c.asset_type}/{c.name}: {exc}", fg="red")
            failures += 1
        except CommitNotFoundError as exc:
            # Race: commit was reachable at classify time, gone now.
            click.secho(f"  ⚠ {c.asset_type}/{c.name}: {exc}", fg="yellow")
            orphans += 1
        except AssetNotFoundError as exc:
            click.secho(f"  ✗ {c.asset_type}/{c.name}: {exc}", fg="red")
            failures += 1
        except OSError as exc:
            click.secho(f"  ✗ {c.asset_type}/{c.name}: {exc}", fg="red")
            failures += 1
        else:
            click.secho(
                f"  ✓ {c.asset_type}/{c.name}: installed at pin {c.pin_commit[:12]}", fg="green"
            )
            successes += 1

    parts: list[str] = []
    if successes:
        parts.append(f"{successes} installed")
    if skipped:
        parts.append(f"{skipped} skipped")
    if orphans:
        parts.append(f"{orphans} orphaned")
    if failures:
        parts.append(f"{failures} failed")
    click.echo("\nSummary: " + (", ".join(parts) if parts else "0 actions") + ".")

    if failures:
        raise click.exceptions.Exit(1)


def _print_install_classification_table(
    classifications: list[ProjectInstallClassification],
) -> None:
    """Render the 5-state preview table for ``install --all`` confirmation."""
    click.echo(f"\nWill install across {len(classifications)} entry(ies):")
    state_color = {
        "install": "green",
        "skip": "cyan",
        "refuse": "yellow",
        "orphan": "yellow",
        "error": "red",
    }
    for c in classifications:
        color = state_color[c.state]
        line = f"  {c.state:8s}  {c.asset_type}/{c.name:20s}  {c.pin_commit[:12] or '?':12s}"
        if c.reason:
            line += f"  ({c.reason})"
        click.secho(line, fg=color)


# ── migrate (PR-D C4) ───────────────────────────────────────────────────


_MIGRATE_GLYPH: dict[str, tuple[str, str]] = {
    # state -> (glyph, click color)
    "migrate": ("→", "green"),
    "noop": ("·", "white"),
    "cleanup_flat": ("±", "yellow"),
    "refuse_dirty": ("✗", "red"),
    "skip_manual": ("?", "yellow"),
    "skip_orphan": ("?", "yellow"),
}

# Glyphs are intentionally distinct from `_STATUS_GLYPH` for cleanup_flat
# (`±` vs status's `⚠`) so users running `mm context status` and `mm
# context migrate` back-to-back don't conflate "stale-pin (red)" with
# "flat+dir collision (yellow)". `✗` is shared but the colour differs
# (status: yellow=dirty in dest tree; migrate: red=blocked by --force).


_MIGRATE_ACTION_LABEL: dict[str, str] = {
    "migrate": "flat → dir",
    "noop": "no-op",
    "cleanup_flat": "flat+dir collision",
    "skip_manual": "skip (manual)",
    "skip_orphan": "skip (orphan)",
}


def _migrate_action_label(row: MigrateRow) -> str:
    if row.state == "refuse_dirty":
        return "flat+dir collision" if row.dir_exists else "flat → dir"
    return _MIGRATE_ACTION_LABEL.get(row.state, row.state)


def _print_migrate_preview(rows: list[MigrateRow], *, skills_section: bool) -> None:
    """Render the migrate dry-run / pre-apply preview.

    Sectioned by asset_type alphabetically (matches `_classify_for_install_all`
    convention). When ``skills_section`` is true, print an informational
    footer noting that skills are always directory layout.
    """
    click.echo("\nWill migrate (review; pass --apply to execute):")
    last_type: str | None = None
    for row in rows:
        if row.asset_type != last_type:
            click.secho(f"\n{row.asset_type}", fg="cyan")
            last_type = row.asset_type
        glyph, color = _MIGRATE_GLYPH[row.state]
        action = _migrate_action_label(row)
        line = f"  {glyph}  {row.name:24s}  {action:24s}  ({row.reason})"
        click.secho(line, fg=color)
    if skills_section:
        click.secho("\nskills", fg="cyan")
        click.secho(
            "  i  always directory layout — no migration needed",
            fg="cyan",
        )


def _summarize_migrate_rows(rows: list[MigrateRow]) -> str:
    """Build the post-preview summary line."""
    counts: dict[str, int] = {s: 0 for s in _MIGRATE_GLYPH}
    for row in rows:
        counts[row.state] += 1
    parts: list[str] = []
    if counts["migrate"]:
        parts.append(f"{counts['migrate']} ready")
    if counts["cleanup_flat"]:
        parts.append(f"{counts['cleanup_flat']} cleanup (flat+dir)")
    if counts["refuse_dirty"]:
        parts.append(f"{counts['refuse_dirty']} need --force")
    if counts["skip_manual"]:
        parts.append(f"{counts['skip_manual']} skip (manual)")
    if counts["skip_orphan"]:
        parts.append(f"{counts['skip_orphan']} skip (orphan)")
    if counts["noop"]:
        parts.append(f"{counts['noop']} no-op")
    return ", ".join(parts) if parts else "0 actions"


@context.command("migrate")
@click.argument(
    "asset_type",
    type=click.Choice(["agents", "commands", "skills"]),
    required=False,
)
@click.argument("name", required=False)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Execute the migration (default is a dry-run preview).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Migrate dirty flat files; each gets a .bak sibling. Requires --apply.",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt. Requires --apply.",
)
def migrate_cmd(
    asset_type: str | None,
    name: str | None,
    apply_: bool,
    force: bool,
    yes: bool,
) -> None:
    """Convert flat-layout context assets to canonical directory layout.

    PR-C made the directory layout canonical for agents and commands;
    pre-PR-C installs and reverse-imports leave behind flat files
    (``agents/<name>.md``). This command renames each such file to
    ``agents/<name>/agent.md`` (and the equivalent for commands) atomically
    via ``os.replace``.

    Skills are always directory layout (Agent Skills spec) and are not
    in scope. Invoking ``migrate skills`` exits 0 with an informational
    message rather than an error.

    Default mode is a dry-run preview; pass ``--apply`` to execute. Dirty
    flat files (mtime > installed_at) require ``--force`` and produce a
    ``.bak`` sibling before mutation, mirroring ``mm context update --force``.
    """
    if (force or yes) and not apply_:
        raise click.UsageError("--force / --yes are only valid with --apply")
    if name is not None and asset_type is None:
        raise click.UsageError("name argument requires asset_type")

    if asset_type == "skills":
        click.secho(
            "skills are always directory layout (Agent Skills spec) — no migration needed.",
            fg="cyan",
        )
        return

    root = _find_project_root()

    try:
        rows = classify_migrate(root, asset_type=asset_type, name=name)
    except (FileNotFoundError, ValueError, InvalidNameError) as exc:
        raise click.ClickException(str(exc)) from exc

    # When the user passes no asset_type, mention skills in the preview
    # footer so the absence is explicit rather than silent.
    skills_section = asset_type is None

    if not rows:
        if name is not None:
            click.echo(
                f"No matching asset to migrate (checked {asset_type}/{name}).",
                err=True,
            )
        else:
            click.echo("No flat-layout assets to migrate.")
        if skills_section:
            click.secho(
                "  (skills are always directory layout — no migration needed.)",
                fg="cyan",
            )
        return

    _print_migrate_preview(rows, skills_section=skills_section)
    summary = _summarize_migrate_rows(rows)
    click.echo(f"\nSummary: {summary}.")

    needs_force = [r for r in rows if r.state == "refuse_dirty"]
    actionable = [r for r in rows if r.state in {"migrate", "cleanup_flat"}]

    if not apply_:
        click.echo("\nRun with --apply to execute.")
        if needs_force:
            click.echo("Dirty/collision assets need --apply --force (creates .bak per dirty file).")
        return

    # --apply path
    if needs_force and not force:
        click.secho(
            f"\n{len(needs_force)} entry(ies) have local edits since install; "
            f"pass --force to migrate (each dirty flat file gets a .bak sibling). "
            f"Refusing to write any entry — re-run with --force or resolve manually.",
            fg="red",
            err=True,
        )
        raise click.exceptions.Exit(1)

    if not actionable and not (force and needs_force):
        click.echo("\nNothing to migrate.")
        return

    if yes and force:
        click.secho(
            "WARNING: --yes --force will migrate dirty flat files without prompting.",
            fg="red",
            err=True,
        )

    if not yes:
        plan_count = len(actionable) + (len(needs_force) if force else 0)
        click.confirm(f"\nMigrate {plan_count} asset(s)? Continue?", abort=True)

    successes = 0
    failures = 0
    skipped = 0
    for row in rows:
        if row.state in {"noop", "skip_manual", "skip_orphan"}:
            glyph, color = _MIGRATE_GLYPH[row.state]
            click.secho(
                f"  {glyph}  {row.asset_type}/{row.name}: {row.reason}",
                fg=color,
            )
            skipped += 1
            continue
        if row.state == "refuse_dirty" and not force:
            # Already gated above; defense in depth.
            click.secho(
                f"  ✗  {row.asset_type}/{row.name}: dirty without --force",
                fg="red",
            )
            failures += 1
            continue

        result = migrate_one(root, row, force=force)
        if result.ok:
            tag = "migrated"
            if row.state == "cleanup_flat" or (row.state == "refuse_dirty" and row.dir_exists):
                tag = "flat removed (dir wins)"
            bak_note = f" (.bak: {result.bak_path.name})" if result.bak_path is not None else ""
            click.secho(
                f"  ✓  {row.asset_type}/{row.name}: {tag}{bak_note}",
                fg="green",
            )
            successes += 1
        else:
            click.secho(
                f"  ✗  {row.asset_type}/{row.name}: {result.error}",
                fg="red",
            )
            failures += 1

    parts: list[str] = []
    if successes:
        parts.append(f"{successes} migrated")
    if skipped:
        parts.append(f"{skipped} skipped")
    if failures:
        parts.append(f"{failures} failed")
    click.echo("\nSummary: " + (", ".join(parts) if parts else "0 actions") + ".")

    if failures:
        raise click.exceptions.Exit(1)


@context.command("settings-doctor")
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    default=False,
    help="Emit a structured JSON result instead of human-readable output.",
)
@_SCOPE_OPTION
def settings_doctor_cmd(json_out: bool, scope_flag: str | None) -> None:
    """Detect memtomem-managed hooks duplicated across settings tiers.

    Implements ADR-0010 §4's scoped on-demand check. Same canonical-
    signature detection used by the sync-time warning, exposed as a
    standalone subcommand for CI / scripting use.

    Exit codes: ``0`` clean, ``1`` duplicates found.
    """
    root = _find_project_root()
    scope = _resolve_cli_scope(scope_flag)
    duplicates = detect_duplicate_tiers(root, active_scope=scope)

    if json_out:
        payload = {
            "status": "duplicates" if duplicates else "clean",
            "active_scope": scope,
            "duplicates": [
                {
                    "tier": dup.tier,
                    "path": str(dup.path),
                    "entries": [
                        {
                            "event": sig.event,
                            "matcher": sig.matcher,
                            "command_preview": sig.command_shape,
                        }
                        for sig in dup.entries
                    ],
                }
                for dup in duplicates
            ],
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        if not duplicates:
            click.secho(
                f"✓ No memtomem-managed hooks duplicated outside the active scope ({scope}).",
                fg="green",
            )
        else:
            click.secho(
                f"✗ Found memtomem-managed hooks in {len(duplicates)} other "
                f"tier(s) (active scope: {scope}):",
                fg="yellow",
            )
            for dup in duplicates:
                click.secho(f"  • {dup.tier} ({dup.path})", fg="yellow")
                for sig in dup.entries:
                    label = f"{sig.event}:{sig.matcher}" if sig.matcher else sig.event
                    click.echo(f"      [{label}] {sig.command_shape}")
            click.echo(
                "\nRun `mm context settings-migrate --from=<scope> "
                "--to=<scope>` to move these into the active scope."
            )

    if duplicates:
        raise click.exceptions.Exit(1)


# ── settings-migrate (ADR-0010 §4) ─────────────────────────────────


_MIGRATE_SCOPE_FROM = click.option(
    "--from",
    "from_scope",
    type=click.Choice(list(get_args(TargetScope))),
    required=True,
    help="Source tier holding the canonical-matched hook entries.",
)


_MIGRATE_SCOPE_TO = click.option(
    "--to",
    "to_scope",
    type=click.Choice(list(get_args(TargetScope))),
    required=True,
    help="Target tier the entries move into.",
)


def _print_migrate_plan_human(plan, *, scope: str) -> None:
    """Render the settings-migrate dry-run / pre-apply preview."""
    if not plan.moves:
        click.echo(
            f"  no memtomem-managed hook entries in {plan.source_scope} "
            f"({plan.source_path}) match the canonical source — nothing to migrate."
        )
        return

    click.echo(
        f"\nWill migrate hook entries from {plan.source_scope} ({plan.source_path}) "
        f"→ {plan.target_scope} ({plan.target_path}):"
    )
    for move in plan.moves:
        sig = move.signature
        label = f"{sig.event}:{sig.matcher}" if sig.matcher else sig.event
        if move.conflict_at_target:
            glyph, color = "✗", "red"
            note = f"skip (conflict: {move.conflict_reason})"
        elif move.already_at_target:
            glyph, color = "·", "cyan"
            note = "already at target — source clean-up only"
        else:
            glyph, color = "→", "green"
            note = "move"
        click.secho(f"  {glyph}  [{label}]  {sig.command_shape}  ({note})", fg=color)


@context.command("settings-migrate")
@_MIGRATE_SCOPE_FROM
@_MIGRATE_SCOPE_TO
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Execute the migration. Default is a dry-run preview.",
)
@click.option(
    "--yes",
    "-y",
    "yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt and the host-write prompt. Requires --apply.",
)
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    default=False,
    help="Emit a structured JSON result instead of human-readable output.",
)
def settings_migrate_cmd(
    from_scope: str,
    to_scope: str,
    apply_: bool,
    yes: bool,
    json_out: bool,
) -> None:
    """Move memtomem-managed hook entries between settings tiers.

    Implements ADR-0010 §4's third follow-up. Reads canonical-signature-
    matched entries from the source tier (``--from``) and lands them in
    the target tier (``--to``) using the canonical rule from
    ``.memtomem/settings.json`` so the target ends up byte-clean rather
    than carrying any source-side whitespace variant. Idempotent —
    re-running after a clean migration finds nothing to move.

    Default is a dry-run; pass ``--apply`` to mutate disk. The host-
    write prompt mirrors ``mm context sync --include=settings``: writes
    outside the project root require an interactive confirmation (or
    ``--yes``).

    Exit codes: ``0`` clean (or dry-run), ``1`` user declined the
    confirmation prompt or the plan reported conflicts requiring manual
    resolution.
    """
    root = _find_project_root()
    try:
        plan = plan_migration(root, source_scope=from_scope, target_scope=to_scope)
    except ValueError as exc:
        if json_out:
            click.echo(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        else:
            click.secho(f"error: {exc}", fg="red", err=True)
        raise click.exceptions.Exit(1)

    conflicts = [m for m in plan.moves if m.conflict_at_target]
    summary = format_plan_summary(plan)

    if json_out:
        payload = {
            "status": "noop" if plan.is_noop else ("conflicts" if conflicts else "ok"),
            "applied": False,
            "from": plan.source_scope,
            "to": plan.target_scope,
            "source_path": str(plan.source_path),
            "target_path": str(plan.target_path),
            "summary": summary,
            "moves": [
                {
                    "event": m.signature.event,
                    "matcher": m.signature.matcher,
                    "command_preview": m.signature.command_shape,
                    "already_at_target": m.already_at_target,
                    "conflict_at_target": m.conflict_at_target,
                    "conflict_reason": m.conflict_reason,
                }
                for m in plan.moves
            ],
        }
    else:
        _print_migrate_plan_human(plan, scope=from_scope)
        if plan.moves:
            click.echo(f"\nSummary: {summary}")

    if not apply_:
        if json_out:
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo("\nRun with --apply to execute.")
        return

    # --apply path
    if plan.is_noop:
        if json_out:
            payload["applied"] = True
            click.echo(json.dumps(payload, indent=2))
        return

    # Host-write confirmation: target outside the project root requires
    # the same gate as `mm context sync --include=settings` so a stray
    # `--apply` from a worktree can't silently rewrite ~/.claude/.
    target_outside = not _is_within(plan.target_path, root)
    source_outside = not _is_within(plan.source_path, root)
    if (target_outside or source_outside) and not yes:
        if not json_out:
            click.secho(
                "settings-migrate will modify the following files outside this project:",
                fg="yellow",
            )
            if target_outside:
                click.echo(f"  {plan.target_path}  (target)")
            if source_outside:
                click.echo(f"  {plan.source_path}  (source)")
            if not click.confirm("Continue?", default=False):
                click.echo("Aborted.")
                raise click.exceptions.Exit(1)
        else:
            # JSON callers must be explicit — refuse without --yes.
            click.echo(
                json.dumps(
                    {
                        "status": "needs_confirmation",
                        "applied": False,
                        "from": plan.source_scope,
                        "to": plan.target_scope,
                        "source_path": str(plan.source_path),
                        "target_path": str(plan.target_path),
                        "host_writes": [
                            str(p)
                            for p, outside in [
                                (plan.target_path, target_outside),
                                (plan.source_path, source_outside),
                            ]
                            if outside
                        ],
                        "hint": "Re-run with --yes after confirming.",
                    },
                    indent=2,
                )
            )
            raise click.exceptions.Exit(1)

    result = apply_migration(plan)

    if json_out:
        payload["applied"] = True
        payload["target_written"] = result.target_written
        payload["source_written"] = result.source_written
        click.echo(json.dumps(payload, indent=2))
    else:
        if result.target_written:
            click.secho(
                f"  ✓ wrote target {plan.target_path}",
                fg="green",
            )
        if result.source_written:
            click.secho(
                f"  ✓ cleaned source {plan.source_path}",
                fg="green",
            )
        if not result.target_written and not result.source_written:
            click.echo("  (no changes written — source was already clean)")

    if conflicts:
        # Conflicts left in source; user must resolve manually before
        # re-running migrate. Match `mm context migrate`'s exit-1 on
        # any partial failure.
        raise click.exceptions.Exit(1)


def _is_within(path: Path, project_root: Path) -> bool:
    """``True`` when *path* resolves under *project_root*. Symlink-safe."""
    try:
        return path.resolve(strict=False).is_relative_to(project_root.resolve(strict=False))
    except (OSError, RuntimeError):
        return False
