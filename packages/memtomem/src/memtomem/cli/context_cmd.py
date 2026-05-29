"""memtomem context — unified agent context management."""

from __future__ import annotations

import json
from pathlib import Path

import click

from memtomem.context.agents import (
    ON_DROP_LEVELS,
    StrictDropError,
    canonical_agent_name,
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
    MigratePartialError,
    MigrateRow,
    MigrateScopeResult,
    SCOPE_MIGRATABLE_KINDS,
    classify_migrate,
    migrate_one,
    migrate_scope,
)
from memtomem.context.projects import KnownProjectsStore
from memtomem.context.lockfile import LockfileVersionError
from memtomem.context.status import classify_status, load_with_recovery
from memtomem.context.generator import (
    GENERATORS,
    extract_sections_from_agent_file,
)
from memtomem.context.parser import CONTEXT_FILENAME, parse_context, sections_to_markdown
from memtomem.context.privacy_scan import PrivacyScanError, scan_artifact_tree
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
from memtomem.context.scope_resolver import canonical_artifact_dir, find_project_root
from memtomem.context.skills import (
    diff_skills,
    extract_skills_to_canonical,
    generate_all_skills,
)
from memtomem.context import _skip_reasons as skip_codes
from memtomem.wiki.store import WikiNotFoundError, WikiStore
from typing import Any, cast, get_args

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
    """Walk up from cwd to find project root (has .git or pyproject.toml).

    Thin wrapper over the shared ``scope_resolver.find_project_root`` so the
    CLI, MCP tools, and web app share one definition of the project root.
    """
    return find_project_root()


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


def _print_skills_init(
    root: Path,
    overwrite: bool,
    *,
    scope: TargetScope = "project_shared",
    force_unsafe_import: bool = False,
) -> None:
    result = extract_skills_to_canonical(
        root,
        overwrite=overwrite,
        scope=scope,
        force_unsafe_import=force_unsafe_import,
    )
    dest_label = (
        canonical_artifact_dir("skills", scope, root)
        if scope != "project_local"
        else "(skipped — project_local has no runtime fan-out)"
    )
    if result.imported:
        click.secho(f"  Imported {len(result.imported)} skill(s) → {dest_label}", fg="green")
        for p in result.imported:
            click.echo(f"    {p.name}")
    else:
        click.echo(f"  (no runtime skills imported into {scope})")
    for name, reason, code in result.skipped:
        color = (
            "red"
            if code
            in (
                skip_codes.PRIVACY_BLOCKED,
                skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED,
            )
            else "yellow"
        )
        click.secho(f"    skipped {name}: {reason}", fg=color)


def _print_skills_generate(
    root: Path,
    *,
    scope: TargetScope = "project_shared",
) -> None:
    try:
        result = generate_all_skills(root, scope=scope)
    except PrivacyScanError as exc:
        raise click.ClickException(exc.message) from exc
    if result.generated:
        click.secho(f"  Skills fan-out: {len(result.generated)}", fg="green")
        for runtime, path in result.generated:
            rel = path.relative_to(root) if path.is_relative_to(root) else path
            click.echo(f"    {runtime:15s}  {rel}")
    for runtime, reason, code in result.skipped:
        color = (
            "red"
            if code
            in (
                skip_codes.PRIVACY_BLOCKED,
                skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED,
            )
            else "yellow"
        )
        click.secho(f"  skipped {runtime}: {reason}", fg=color)


def _print_skills_diff(root: Path, *, scope: TargetScope = "project_shared") -> None:
    rows = diff_skills(root, scope=scope)
    if not rows:
        click.echo(f"  (no skills to compare in {scope})")
        return
    for runtime, name, status in rows:
        color = "green" if status == "in sync" else "yellow"
        click.secho(f"  {runtime:15s}  {name}  [{status}]  scope={scope}", fg=color)


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


def _print_agents_init(
    root: Path,
    overwrite: bool,
    *,
    scope: TargetScope = "project_shared",
    force_unsafe_import: bool = False,
) -> None:
    result = extract_agents_to_canonical(
        root,
        overwrite=overwrite,
        scope=scope,
        force_unsafe_import=force_unsafe_import,
    )
    dest_label = (
        canonical_artifact_dir("agents", scope, root)
        if scope != "project_local"
        else "(skipped — project_local has no runtime fan-out)"
    )
    if result.imported:
        click.secho(f"  Imported {len(result.imported)} sub-agent(s) → {dest_label}", fg="green")
        for path, layout in result.imported:
            click.echo(f"    {canonical_agent_name(path, layout)}")
    else:
        click.echo(f"  (no runtime sub-agents imported into {scope})")
    for name, reason, code in result.skipped:
        color = (
            "red"
            if code
            in (
                skip_codes.PRIVACY_BLOCKED,
                skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED,
            )
            else "yellow"
        )
        click.secho(f"    skipped {name}: {reason}", fg=color)


def _print_agents_generate(
    root: Path,
    strict: bool,
    on_drop: str = "ignore",
    *,
    scope: TargetScope = "project_shared",
) -> None:
    try:
        result = generate_all_agents(root, strict=strict, on_drop=on_drop, scope=scope)
    except StrictDropError as exc:
        click.secho(f"  [strict] {exc}", fg="red")
        raise click.Abort()
    except PrivacyScanError as exc:
        raise click.ClickException(exc.message) from exc

    if result.generated:
        click.secho(f"  Sub-agent fan-out: {len(result.generated)}", fg="green")
        for runtime, path in result.generated:
            try:
                rel = path.relative_to(root) if path.is_relative_to(root) else path
            except ValueError:
                rel = path
            click.echo(f"    {runtime:15s}  {rel}")
    for runtime, reason, code in result.skipped:
        color = (
            "red"
            if code
            in (
                skip_codes.PRIVACY_BLOCKED,
                skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED,
            )
            else "yellow"
        )
        click.secho(f"  skipped {runtime}: {reason}", fg=color)
    for runtime, agent_name, dropped in result.dropped:
        click.secho(
            f"  {runtime} dropped {dropped} from '{agent_name}'",
            fg="yellow",
        )


def _print_agents_diff(root: Path, *, scope: TargetScope = "project_shared") -> None:
    rows = diff_agents(root, scope=scope)
    if not rows:
        click.echo(f"  (no sub-agents to compare in {scope})")
        return
    for runtime, name, status in rows:
        color = "green" if status == "in sync" else "yellow"
        click.secho(f"  {runtime:15s}  {name}  [{status}]  scope={scope}", fg=color)


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


def _print_commands_init(
    root: Path,
    overwrite: bool,
    *,
    scope: TargetScope = "project_shared",
    force_unsafe_import: bool = False,
) -> None:
    result = extract_commands_to_canonical(
        root,
        overwrite=overwrite,
        scope=scope,
        force_unsafe_import=force_unsafe_import,
    )
    dest_label = (
        canonical_artifact_dir("commands", scope, root)
        if scope != "project_local"
        else "(skipped — project_local has no runtime fan-out)"
    )
    if result.imported:
        click.secho(f"  Imported {len(result.imported)} command(s) → {dest_label}", fg="green")
        for path, layout in result.imported:
            display = path.parent.name if layout == "dir" else path.stem
            click.echo(f"    {display}")
    else:
        click.echo(f"  (no runtime commands imported into {scope})")
    for name, reason, code in result.skipped:
        color = (
            "red"
            if code
            in (
                skip_codes.PRIVACY_BLOCKED,
                skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED,
            )
            else "yellow"
        )
        click.secho(f"    skipped {name}: {reason}", fg=color)


def _print_commands_generate(
    root: Path,
    strict: bool,
    on_drop: str = "ignore",
    *,
    scope: TargetScope = "project_shared",
) -> None:
    try:
        result = generate_all_commands(root, strict=strict, on_drop=on_drop, scope=scope)
    except CommandStrictDropError as exc:
        click.secho(f"  [strict] {exc}", fg="red")
        raise click.Abort() from exc
    except PrivacyScanError as exc:
        raise click.ClickException(exc.message) from exc

    if result.generated:
        click.secho(f"  Command fan-out: {len(result.generated)}", fg="green")
        for runtime, path in result.generated:
            try:
                rel = path.relative_to(root) if path.is_relative_to(root) else path
            except ValueError:
                rel = path
            click.echo(f"    {runtime:17s}  {rel}")
    for runtime, reason, code in result.skipped:
        color = (
            "red"
            if code
            in (
                skip_codes.PRIVACY_BLOCKED,
                skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED,
            )
            else "yellow"
        )
        click.secho(f"  skipped {runtime}: {reason}", fg=color)
    for runtime, cmd_name, dropped in result.dropped:
        click.secho(
            f"  {runtime} dropped {dropped} from '{cmd_name}'",
            fg="yellow",
        )


def _print_commands_diff(root: Path, *, scope: TargetScope = "project_shared") -> None:
    rows = diff_commands(root, scope=scope)
    if not rows:
        click.echo(f"  (no commands to compare in {scope})")
        return
    for runtime, name, status in rows:
        color = "green" if status == "in sync" else "yellow"
        click.secho(f"  {runtime:17s}  {name}  [{status}]  scope={scope}", fg=color)


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


def _resolve_artifact_cli_scope(scope_flag: str | None) -> TargetScope:
    """Resolve ``--scope`` for non-memory artifact CLI commands (ADR-0011 PR-E2).

    Unlike :func:`_resolve_cli_scope` this does NOT consult
    ``cfg.hooks.target_scope``: artifact storage location is a different
    feature axis from settings host placement. Leaning on the hooks
    default would silently flip the artifact target whenever a user
    pinned ``hooks.target_scope = "user"`` for unrelated reasons (e.g.
    Cursor-style settings layout), which Codex flagged as a
    ``_resolve_cli_scope`` default-leak risk in PR-E1 review. The
    artifact-side default is fixed at ``"project_shared"`` to match the
    pre-PR-E2 import behavior — implicit invocation stays back-compat.
    """
    if scope_flag is not None:
        return scope_flag  # type: ignore[return-value]
    return "project_shared"


# ── ADR-0011 PR-E2 — `.gitignore` auto-append for project_local tier ──

_GITIGNORE_MARKER = "# memtomem local artifacts (ADR-0011 project_local tier)"
_GITIGNORE_PATTERNS: tuple[str, ...] = (".memtomem/*.local/", ".memtomem/.staging/")


def _append_gitignore_marker(project_root: Path) -> tuple[bool, str]:
    """Idempotent grep-then-append of the project_local block on ``.gitignore``.

    The grep is on the comment marker line, NOT the pattern lines — users
    may legitimately have ``.memtomem/.staging/`` listed elsewhere for
    unrelated reasons, and we do not want to consider those "already
    present".

    Returns ``(wrote, msg)``:
      - ``(False, "no_git_repo_pyproject_only")`` — ``.git`` absent but
        ``pyproject.toml`` is present. ``.gitignore`` is meaningful only
        in a git working tree, so we skip the write and surface a
        specific warning so the user knows to ``git init`` first.
      - ``(False, "no_project_signal")`` — neither ``.git`` nor
        ``pyproject.toml`` present.
      - ``(False, "already_present")`` — marker comment already on disk.
      - ``(True, "appended")`` — newly written.
    """
    has_git = (project_root / ".git").exists()
    has_pyproject = (project_root / "pyproject.toml").exists()
    if not has_git:
        return (
            False,
            "no_git_repo_pyproject_only" if has_pyproject else "no_project_signal",
        )
    gi = project_root / ".gitignore"
    existing = gi.read_text(encoding="utf-8") if gi.exists() else ""
    if _GITIGNORE_MARKER in existing:
        return False, "already_present"
    block = "\n" if existing and not existing.endswith("\n") else ""
    block += f"\n{_GITIGNORE_MARKER}\n"
    block += "\n".join(_GITIGNORE_PATTERNS) + "\n"
    with gi.open("a", encoding="utf-8") as fh:
        fh.write(block)
    return True, "appended"


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
    help="Overwrite existing canonical entries when importing from runtimes.",
)
@_SCOPE_OPTION
@click.option(
    "--confirm-project-shared",
    is_flag=True,
    default=False,
    help=(
        "Confirm seeding the git-tracked project_shared canonical tier. "
        "Required when --scope is explicitly set to project_shared."
    ),
)
@click.option(
    "--force-unsafe-import",
    is_flag=True,
    default=False,
    help=(
        "Bypass Gate A on existing runtime files being imported. "
        "user / project_local destinations only — project_shared "
        "hard-refuses (ADR-0011 §5)."
    ),
)
def init_cmd(
    include: tuple[str, ...],
    overwrite: bool,
    scope_flag: str | None,
    confirm_project_shared: bool,
    force_unsafe_import: bool,
) -> None:
    """Seed canonical artifact dirs and (optionally) import existing runtime files.

    Without ``--scope`` the command preserves pre-PR-E2 behavior: writes
    ``<proj>/.memtomem/context.md`` and (with ``--include``) imports under
    ``<proj>/.memtomem/{agents,skills,commands}/`` (the implicit
    ``project_shared`` tier). Pass ``--scope=user`` /
    ``--scope=project_local`` to seed at the user or local-draft tier
    instead. ADR-0011 §5 Gate A scans every imported file's bytes for
    secrets; project_shared hard-refuses on any hit.
    """
    inc = _parse_include(include)
    root = _find_project_root()
    scope_explicit = scope_flag is not None
    scope = _resolve_artifact_cli_scope(scope_flag)
    has_project_signal = (root / ".git").exists() or (root / "pyproject.toml").exists()

    # EXPLICIT --scope project_* requires a real project context. Implicit
    # default (no --scope) preserves pre-PR-E2 behaviour where the command
    # could run from any cwd (``_find_project_root`` falls back to cwd) —
    # raising would be a back-compat regression for users who run
    # ``mm context init`` from a fresh directory.
    if scope_explicit and scope != "user" and not has_project_signal:
        raise click.ClickException(
            f"--scope={scope} requires a project root (with .git or pyproject.toml). "
            "Use --scope=user from outside a project, or run from inside one."
        )

    # Round-3 review D-new-1: when implicit ``mm context init`` runs in
    # a directory with no ``.git``/``pyproject.toml`` we still proceed
    # (back-compat with pre-PR-E2), but the user is silently dropping
    # ``.memtomem/`` into a non-project location which is rarely what
    # they want. Surface a yellow hint pointing to ``--scope=user`` for
    # the cross-project case. Block is intentionally NOT raised — the
    # back-compat path stays open.
    if not scope_explicit and not has_project_signal:
        click.secho(
            f"  warning: no .git or pyproject.toml in {root} — "
            "creating .memtomem/ here. Use --scope=user for cross-project "
            "artifacts (writes to ~/.memtomem/ instead).",
            fg="yellow",
        )

    # Gate B — only on EXPLICIT --scope project_shared. Implicit default
    # (no --scope) keeps pre-PR-E2 behavior so non-interactive CI invocations
    # of `mm context init` don't suddenly start prompting.
    if scope_explicit and scope == "project_shared" and not confirm_project_shared:
        prompt = f"\n--scope=project_shared writes to git-tracked {root}/.memtomem/. Continue?"
        if not click.confirm(prompt, default=False):
            raise click.Abort()

    # context.md is a **project_shared** artifact in nature: it lives at
    # ``<proj>/.memtomem/context.md``, gets git-tracked when the project
    # has a ``.git`` (just like the project_shared canonical tree), and
    # is the source of truth for ``mm context generate``'s fan-out.
    # Artifact-only scopes (``--scope user``, ``--scope project_local``)
    # MUST NOT mutate or even prompt on it — that would (a) violate the
    # local/user scope contract, (b) bypass Gate B for an effective
    # project_shared write, and (c) re-introduce the "existing context.md
    # blocks scoped seeding" surprise the C1 review fix ostensibly closed
    # (PR #889 review P2 round 2). The write fires only when:
    #   - implicit invocation (no --scope) AND a project context exists
    #     (pre-PR-E2 back-compat — ``mm context init`` writes context.md), OR
    #   - explicit ``--scope project_shared`` AND a project context
    #     exists (Gate B already opted in upstream of this branch).
    artifact_only_scope = scope_explicit and scope in ("user", "project_local")
    write_context_md = has_project_signal and not artifact_only_scope
    if write_context_md:
        ctx_path = _context_path(root)
        if ctx_path.exists() and not click.confirm(
            f"{CONTEXT_FILENAME} already exists. Overwrite?", default=False
        ):
            click.secho(
                f"  (skipped {CONTEXT_FILENAME} rewrite — continuing with scoped artifact seeding)",
                fg="yellow",
            )
            write_context_md = False

    if write_context_md:
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
            best = max(files, key=lambda f: f.size)
            click.echo(f"Extracting from {best.agent}: {best.path.name} ({best.size} bytes)")
            content = best.path.read_text(encoding="utf-8")
            sections = extract_sections_from_agent_file(content)
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

    # Seed canonical sub-artifact dirs at the resolved scope (idempotent).
    for kind in ("agents", "skills", "commands"):
        d = canonical_artifact_dir(kind, scope, root)
        d.mkdir(parents=True, exist_ok=True)
        click.secho(f"  Created {d}", fg="green")

    # project_local — auto-append the .gitignore block so the local-draft
    # tier and staging dir never end up tracked by accident.
    if scope == "project_local":
        wrote, msg = _append_gitignore_marker(root)
        if wrote:
            click.secho(
                "  Appended .gitignore marker (.memtomem/*.local/, .memtomem/.staging/)",
                fg="green",
            )
        elif msg == "already_present":
            click.echo("  (.gitignore marker already present — idempotent)")
        elif msg == "no_git_repo_pyproject_only":
            click.secho(
                "  warning: project root resolved via pyproject.toml but `.git` "
                "missing — .gitignore not appended. Run `git init` first to "
                "git-protect the local tier.",
                fg="yellow",
            )
        elif msg == "no_project_signal":
            click.secho(
                "  warning: no .git and no pyproject.toml in project root — "
                ".gitignore append skipped.",
                fg="yellow",
            )

    # --include runtime-import path. Gate A is applied inside each
    # extract_*_to_canonical helper (per-file for agents/commands;
    # tree walk for skills).
    if "skills" in inc:
        click.echo("")
        _print_skills_init(
            root,
            overwrite=overwrite,
            scope=scope,
            force_unsafe_import=force_unsafe_import,
        )

    if "agents" in inc:
        click.echo("")
        _print_agents_init(
            root,
            overwrite=overwrite,
            scope=scope,
            force_unsafe_import=force_unsafe_import,
        )

    if "commands" in inc:
        click.echo("")
        _print_commands_init(
            root,
            overwrite=overwrite,
            scope=scope,
            force_unsafe_import=force_unsafe_import,
        )


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

    # ADR-0011 PR-E3: resolve the canonical-artifact scope once and thread
    # it through the three include helpers. Defaults to ``project_shared``
    # via ``_resolve_artifact_cli_scope`` (NOT ``_resolve_cli_scope``,
    # which leaks ``cfg.hooks.target_scope`` into the artifact axis —
    # ADR-0011 PR-E1 Codex review trip-wire).
    artifact_scope = _resolve_artifact_cli_scope(scope_flag)

    if "skills" in inc:
        click.echo("")
        _print_skills_generate(root, scope=artifact_scope)

    if "agents" in inc:
        click.echo("")
        _print_agents_generate(root, strict=strict, on_drop=on_drop, scope=artifact_scope)

    if "commands" in inc:
        click.echo("")
        _print_commands_generate(root, strict=strict, on_drop=on_drop, scope=artifact_scope)

    if "settings" in inc:
        click.echo("")
        # Settings has its own (separate from artifact) scope axis — see
        # ADR-0010. ``_resolve_cli_scope`` consults ``cfg.hooks.target_scope``;
        # this is intentional for settings and intentionally NOT used for
        # artifacts above.
        scope = _resolve_cli_scope(scope_flag)
        if _confirm_settings_host_writes(root, scope=scope, yes=yes):
            _print_settings_generate(root, scope=scope, allow_host_writes=True)
        else:
            click.secho("  Skipped settings sync (declined).", fg="yellow")

    click.secho("Done.", fg="green")


@context.command("diff")
@_INCLUDE_OPTION
@click.option(
    "--scope",
    "scope_flag",
    type=click.Choice(get_args(TargetScope)),
    default="project_shared",
    show_default=True,
    help=(
        "Canonical artifact tier for skills/agents/commands. "
        "Use project_local to inspect local drafts with no runtime fan-out."
    ),
)
def diff_cmd(include: tuple[str, ...], scope_flag: TargetScope) -> None:
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
        _print_skills_diff(root, scope=scope_flag)

    if "agents" in inc:
        click.echo("")
        _print_agents_diff(root, scope=scope_flag)

    if "commands" in inc:
        click.echo("")
        _print_commands_diff(root, scope=scope_flag)

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
        if not sections:
            # Mirror generate_cmd's empty-guard (line ~932). parse_context
            # returns {} for any context.md with content but no ``## Heading``
            # delimiters (a stub, all-prose, or a mid-edit file). Without this
            # guard, gen.generate({}) yields header-only/empty output that would
            # overwrite the user's existing CLAUDE.md/GEMINI.md/.cursorrules —
            # silent data loss with no backup. Refuse instead.
            click.secho(
                f"{CONTEXT_FILENAME} is empty — refusing to overwrite agent files.",
                fg="yellow",
            )
        else:
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

    # ADR-0011 PR-E3: resolve the canonical-artifact scope once and thread
    # it through the three include helpers. Defaults to ``project_shared``
    # via ``_resolve_artifact_cli_scope`` (NOT ``_resolve_cli_scope``,
    # which leaks ``cfg.hooks.target_scope`` into the artifact axis —
    # ADR-0011 PR-E1 Codex review trip-wire).
    artifact_scope = _resolve_artifact_cli_scope(scope_flag)

    if "skills" in inc:
        click.echo("")
        _print_skills_generate(root, scope=artifact_scope)

    if "agents" in inc:
        click.echo("")
        _print_agents_generate(root, strict=strict, on_drop=on_drop, scope=artifact_scope)

    if "commands" in inc:
        click.echo("")
        _print_commands_generate(root, strict=strict, on_drop=on_drop, scope=artifact_scope)

    if "settings" in inc:
        click.echo("")
        # Settings has its own (separate from artifact) scope axis — see
        # ADR-0010. ``_resolve_cli_scope`` consults ``cfg.hooks.target_scope``;
        # this is intentional for settings and intentionally NOT used for
        # artifacts above.
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
    "local-draft": ("·", "magenta"),
}

# ADR-0011 §3 / ADR-0016 §7: project_local agents / commands / skills never
# reach a runtime fan-out path. The annotation flags the row inline so a
# reader of ``mm context status`` understands the entry is a draft tier with
# no Claude Code (or other agent) discovery path.
_PROJECT_LOCAL_ANNOTATION = "(draft, no fan-out)"


@context.command("status")
@click.option(
    "--scope",
    "scope_flag",
    type=click.Choice(get_args(TargetScope)),
    default="project_shared",
    show_default=True,
    help=(
        "Canonical artifact tier to show. project_local rows are drafts with no runtime fan-out."
    ),
)
def status_cmd(scope_flag: TargetScope) -> None:
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
    rows = [row for row in rows if row.tier == scope_flag]

    if lockfile_error is not None:
        click.secho(f"  ✗ lock.json: {lockfile_error}", fg="red", err=True)

    # Header — counts + wiki HEAD (or "wiki not present" annotation).
    # Lockfile-tracked installs (project_shared) drive the "installed"
    # count; project_local rows are surfaced separately so the header
    # word ("installed") stays accurate for both groups.
    installed_count = sum(1 for r in rows if r.tier != "project_local")
    draft_count = sum(1 for r in rows if r.tier == "project_local")
    draft_suffix = (
        f" (+ {draft_count} local draft{'s' if draft_count != 1 else ''})" if draft_count else ""
    )
    scope_suffix = f" — scope {scope_flag}"
    if wiki_head is None:
        wiki_root = wiki.root
        click.echo(
            f".memtomem/ — {installed_count} asset(s) installed{draft_suffix} — "
            f"wiki not present at {wiki_root}; pin reachability not checked{scope_suffix}"
        )
    else:
        click.echo(
            f".memtomem/ — {installed_count} asset(s) installed{draft_suffix} — "
            f"wiki HEAD {wiki_head[:12]}{scope_suffix}"
        )

    if not rows and lockfile_error is None:
        if scope_flag == "project_local":
            click.echo("\nNo project_local draft assets in this project.")
        else:
            click.echo(f"\nNo wiki assets installed in this project for scope {scope_flag}.")
        return

    # Sectioned by asset type, preserving classify_status() order
    # (alphabetical: agents → commands → skills, names alpha within,
    # project_shared rows before project_local rows on a name collision).
    last_type: str | None = None
    summary: dict[str, int] = {
        "ok": 0,
        "behind": 0,
        "dirty": 0,
        "missing": 0,
        "stale-pin": 0,
        "local-draft": 0,
    }
    for row in rows:
        if row.asset_type != last_type:
            click.secho(f"\n{row.asset_type}", fg="cyan")
            last_type = row.asset_type
        glyph, color = _STATUS_GLYPH[row.state]
        # Local-draft rows have no lockfile metadata — render the
        # pin and installed-at columns as dashes rather than the
        # "?"/"—" placeholders the lockfile-tracked branches use, so
        # the visual distinction is obvious to a reader scanning the
        # column.
        if row.tier == "project_local":
            line = f"  {glyph}  {row.name:24s}  {'—':<12}  {'(no install record)':<23}"
        else:
            installed_date = row.installed_at[:10] if row.installed_at else "—"
            line = (
                f"  {glyph}  {row.name:24s}  "
                f"{(row.pin_commit or '?')[:12]}  installed {installed_date}"
            )
        if row.tier == "project_local":
            line += f"  {_PROJECT_LOCAL_ANNOTATION}"
        elif row.reason:
            line += f"  ({row.reason})"
        click.secho(line, fg=color)
        summary[row.state] += 1

    if rows:
        parts = [
            f"{summary[k]} {k}"
            for k in ("ok", "behind", "dirty", "missing", "stale-pin", "local-draft")
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


def _migrate_scope_dispatch(
    asset_type: str,
    name: str,
    from_scope: str | None,
    to_scope: str,
    apply_: bool,
    yes: bool,
    confirm_project_shared: bool,
) -> None:
    """ADR-0011 PR-E4 scope-tier move for agents / commands / skills.

    Calls :func:`memtomem.context.migrate.migrate_scope` after running the
    project_shared confirmation gate. Click prompts and stdout writes
    live here; the pure module stays prompt-free.
    """
    project_root = _find_project_root()

    # Pre-flight Gate B (project_shared opt-in, mirroring memory-migrate).
    # Apply only — dry-run reads the same plan without touching disk and
    # is the recommended way to inspect the move before opting in.
    if to_scope == "project_shared" and apply_ and not confirm_project_shared:
        if yes:
            raise click.ClickException(
                "--to project_shared requires --confirm-project-shared. "
                "--yes alone is not sufficient: project_shared writes go "
                "to the git-tracked tier and require explicit opt-in."
            )
        if not click.confirm(
            "\nThis will move the canonical into the git-tracked project_shared tier. Continue?",
            default=False,
        ):
            raise click.Abort()

    try:
        result = migrate_scope(
            asset_type,  # type: ignore[arg-type]
            name,
            from_scope=from_scope,  # type: ignore[arg-type]
            to_scope=to_scope,  # type: ignore[arg-type]
            project_root=project_root,
            apply_=apply_,
        )
    except (FileNotFoundError, ValueError, InvalidNameError) as exc:
        raise click.ClickException(str(exc)) from exc
    except PrivacyScanError as exc:
        raise click.ClickException(exc.message) from exc
    except MigratePartialError as exc:
        # Fan-out cleanup never ran (raise short-circuited), and the
        # "Next: run sync ..." hint at the bottom of
        # _print_migrate_scope_result is skipped because we exit before
        # the result rendering — the user sees only the recovery hint
        # embedded in exc.message.
        raise click.ClickException(exc.message) from exc

    _print_migrate_scope_result(result, apply_=apply_)

    # ADR-0011 project_local contract: ``.memtomem/*.local/`` and the
    # staging dir must be gitignored. ``mm context init --scope
    # project_local`` already appends the marker, but a user can land
    # on project_local for the first time via ``mm context migrate
    # <kind> <name> --to project_local --apply`` without ever running
    # init — without this call the new local-draft tier shows up in
    # ``git status`` and risks being committed by accident
    # (#895 P2 review #3 fold).
    if apply_ and to_scope == "project_local":
        wrote, msg = _append_gitignore_marker(project_root)
        if wrote:
            click.secho(
                "  Appended .gitignore marker (.memtomem/*.local/, .memtomem/.staging/)",
                fg="green",
            )
        elif msg == "no_git_repo_pyproject_only":
            click.secho(
                "  warning: project root resolved via pyproject.toml but `.git` "
                "missing — .gitignore not appended. Run `git init` first to "
                "git-protect the local tier.",
                fg="yellow",
            )
        elif msg == "no_project_signal":
            click.secho(
                "  warning: no .git and no pyproject.toml in project root — "
                ".gitignore append skipped.",
                fg="yellow",
            )
        # ``already_present`` is silent — the marker is already there,
        # the user does not need a redundant green tick on every migrate.


def _print_migrate_scope_result(result: MigrateScopeResult, *, apply_: bool) -> None:
    """User-facing summary for one scope-tier migration.

    Dry-run prints the plan + a "run with --apply" hint. Apply prints a
    green-tick line plus any cleaned runtime fan-out paths so the user
    sees both halves of the move (canonical + stale fan-out).
    """
    layout_note = " (flat layout)" if result.layout == "flat" else ""
    click.echo(f"Plan: migrate {result.kind}/{result.name}{layout_note}")
    click.echo(f"  from {result.from_scope}: {result.src_path}")
    click.echo(f"  to   {result.to_scope}: {result.dst_path}")

    if not apply_:
        click.echo("\nRun with --apply to execute.")
        click.echo(
            f"After apply, run `mm context sync --scope {result.to_scope}` "
            f"to refresh runtime fan-out."
        )
        return

    click.secho(
        f"  ✓ moved {result.kind}/{result.name}: {result.from_scope} → {result.to_scope}",
        fg="green",
    )
    if result.fanout_cleaned:
        click.echo(
            f"  cleaned {len(result.fanout_cleaned)} stale runtime fan-out "
            f"target(s) at scope='{result.from_scope}':"
        )
        for path in result.fanout_cleaned:
            click.echo(f"    - {path}")
    click.echo(
        f"\nNext: run `mm context sync --scope {result.to_scope}` to "
        f"regenerate runtime fan-out at the new tier."
    )


def _migrate_memory_dispatch(
    operand: str | None,
    from_scope: TargetScope | None,
    to_scope: TargetScope | None,
    apply_: bool,
    force: bool,
    yes: bool,
    confirm_project_shared: bool,
) -> None:
    """ADR-0011 PR-E4 cross-link to ``mm context memory-migrate``.

    The operand is a SOURCE PATH (not a name) since memory canonical
    files are addressed by absolute path, not by an artifact-style name.
    Both ``--from`` and ``--to`` are required (memory has no
    auto-detection — paths can be ambiguous between user / project_shared
    / project_local in nested layouts). Behaviour is byte-identical to
    ``mm context memory-migrate`` since both call the same impl.
    """
    if operand is None:
        raise click.UsageError(
            "source path argument is required for kind=memory (operand is a path, not a name)"
        )
    if from_scope is None or to_scope is None:
        raise click.UsageError("--from and --to are both required for kind=memory")
    if force:
        raise click.UsageError(
            "--force does not apply to memory migration (no flat/dir layouts in this kind)"
        )
    if from_scope == to_scope:
        raise click.ClickException("--from and --to must differ.")

    # Narrow to literal scope types after validating required values.
    from_scope_t = cast(TargetScope, from_scope)
    to_scope_t = cast(TargetScope, to_scope)

    source = Path(operand).expanduser()
    if not source.exists():
        raise click.ClickException(f"source path does not exist: {source}")
    if not source.is_file():
        raise click.ClickException(f"source path must be a file, not a directory: {source}")
    source_resolved = source.resolve()

    import asyncio

    asyncio.run(
        _memory_migrate_run(
            [source_resolved],
            from_scope_t,
            to_scope_t,
            apply_,
            yes,
            confirm_project_shared,
        )
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
    type=click.Choice(["agents", "commands", "skills", "memory"]),
    required=False,
)
@click.argument("name", required=False)
@click.option(
    "--from",
    "from_scope",
    type=click.Choice(list(get_args(TargetScope))),
    default=None,
    help=(
        "Explicit source scope. Required for kind=memory; auto-detected "
        "for agents/commands/skills (pass to disambiguate when the same "
        "name lives in multiple scopes)."
    ),
)
@click.option(
    "--to",
    "to_scope",
    type=click.Choice(list(get_args(TargetScope))),
    default=None,
    help=(
        "Target scope tier. When set, migrate moves the artifact's "
        "canonical between ADR-0011 tiers (PR-E4). When omitted, falls "
        "back to the PR-D flat→dir layout migration."
    ),
)
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
    help=(
        "Flat→dir mode: migrate dirty flat files; each gets a .bak sibling. "
        "Has no effect in scope-tier mode (--to) — destinations always "
        "refuse on conflict in PR-E4. Requires --apply."
    ),
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt. Requires --apply.",
)
@click.option(
    "--confirm-project-shared",
    "confirm_project_shared",
    is_flag=True,
    default=False,
    help=(
        "Required (in addition to --apply) when --to=project_shared "
        "or kind=memory --to=project_shared, mirroring "
        "``mm context memory-migrate``. --yes alone does not satisfy "
        "the project_shared opt-in."
    ),
)
def migrate_cmd(
    asset_type: str | None,
    name: str | None,
    from_scope: str | None,
    to_scope: str | None,
    apply_: bool,
    force: bool,
    yes: bool,
    confirm_project_shared: bool,
) -> None:
    """Migrate canonical context artifacts.

    Two modes share this verb:

    * **Flat→dir layout** (PR-D, default when ``--to`` is omitted) —
      converts pre-PR-C ``agents/<name>.md`` to ``agents/<name>/agent.md``.
      Skills are always directory layout (Agent Skills spec) and exit 0
      with an informational message in this mode.
    * **Scope-tier move** (PR-E4, when ``--to`` is set) — moves the
      canonical between ADR-0011 tiers (``user`` /
      ``project_shared`` / ``project_local``). Supports agents,
      commands, skills, and memory (memory delegates to
      ``mm context memory-migrate``'s impl with parity behaviour).

    Default mode is a dry-run preview; pass ``--apply`` to execute.
    """
    if (force or yes) and not apply_:
        raise click.UsageError("--force / --yes are only valid with --apply")
    if name is not None and asset_type is None:
        raise click.UsageError("name argument requires asset_type")

    # ── PR-E4 scope-mode dispatch ────────────────────────────────────
    if asset_type == "memory":
        if from_scope is None or to_scope is None:
            raise click.UsageError("--from and --to are both required for kind=memory")
        from_scope_t = cast(TargetScope, from_scope)
        to_scope_t = cast(TargetScope, to_scope)
        _migrate_memory_dispatch(
            name,
            from_scope_t,
            to_scope_t,
            apply_,
            force,
            yes,
            confirm_project_shared,
        )
        return
    if to_scope is not None:
        if asset_type is None:
            raise click.UsageError("--to requires an asset_type")
        if asset_type not in SCOPE_MIGRATABLE_KINDS:
            raise click.UsageError(
                f"--to is not supported for asset_type={asset_type!r} "
                f"(use one of {SCOPE_MIGRATABLE_KINDS} or 'memory')"
            )
        if name is None:
            raise click.UsageError("name argument is required with --to")
        if force:
            raise click.UsageError(
                "--force does not apply to --to scope-tier moves "
                "(destinations always refuse on conflict in PR-E4)"
            )
        _migrate_scope_dispatch(
            asset_type,
            name,
            from_scope,
            to_scope,
            apply_,
            yes,
            confirm_project_shared,
        )
        return

    # ── Flat→dir mode validation gates ───────────────────────────────
    if from_scope is not None:
        raise click.UsageError("--from requires --to (scope-tier mode)")
    if confirm_project_shared:
        raise click.UsageError("--confirm-project-shared requires --to")

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


def _print_migrate_plan_human(plan) -> None:
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
        _print_migrate_plan_human(plan)
        if plan.moves:
            click.echo(f"\nSummary: {summary}")

    if not apply_:
        if json_out:
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo("\nRun with --apply to execute.")
        return

    # --apply path. ``is_noop`` is True both when there is nothing to
    # migrate at all AND when every move is a conflict (applicable_moves
    # is empty in both cases). Differentiate so all-conflict still exits
    # 1 — the user has unresolved drift to fix.
    applicable = plan.applicable_moves
    if plan.is_noop and not conflicts:
        if json_out:
            payload["applied"] = True
            click.echo(json.dumps(payload, indent=2))
        return

    # Host-write confirmation: target outside the project root requires
    # the same gate as `mm context sync --include=settings` so a stray
    # `--apply` from a worktree can't silently rewrite ~/.claude/. Only
    # prompt when we'd actually write — an all-conflict plan touches
    # nothing on disk.
    target_outside = not _is_within(plan.target_path, root)
    source_outside = not _is_within(plan.source_path, root)
    if applicable and (target_outside or source_outside) and not yes:
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
            if conflicts:
                click.secho(
                    "  (no changes written — conflicts must be resolved first)",
                    fg="yellow",
                )
            else:
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


@context.command("memory-migrate")
@click.argument("source", type=str)
@click.option(
    "--from",
    "from_scope",
    type=click.Choice(list(get_args(TargetScope))),
    required=True,
    help="Source memory tier the file currently lives in.",
)
@click.option(
    "--to",
    "to_scope",
    type=click.Choice(list(get_args(TargetScope))),
    required=True,
    help="Target memory tier the file moves into.",
)
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
    help="Skip the project_shared confirmation prompt. Requires --apply.",
)
@click.option(
    "--confirm-project-shared",
    "confirm_project_shared",
    is_flag=True,
    default=False,
    help="Confirm writing to the git-tracked project_shared memory tier.",
)
def memory_migrate_cmd(
    source: str,
    from_scope: TargetScope,
    to_scope: TargetScope,
    apply_: bool,
    yes: bool,
    confirm_project_shared: bool,
) -> None:
    """Move markdown memory file(s) between ADR-0011 scope tiers.

    Chunk-id-stable, single-DB rename. The source file moves on disk
    to the target tier's canonical directory; the chunks table is
    UPDATED in-place via ``update_chunks_scope_for_source`` so chunk
    UUIDs and the ``chunk_links`` lineage are preserved. No re-index
    is triggered.

    ``SOURCE`` is either a single existing file path (back-compat with
    v1) or a glob pattern (quote it to prevent shell expansion). For
    a glob, pre-flight (privacy scan + per-file lockfile probe) runs
    over every match before any FS move; on per-file DB failure
    mid-batch, the failing file is reverted, the remaining files are
    left untouched, and the command exits — ADR-0011 §5 compensation
    applies per file so the user has a deterministic resumption point.

    Default is dry-run; pass ``--apply`` to execute. When the target
    is ``project_shared`` (git-tracked), file content is re-scanned by
    ``enforce_write_guard``; secret hits reject the migration with no
    force bypass — git history would carry them forever (ADR-0011 §5).

    Cross-DB migration is deferred; see ADR-0012 / #911.
    """
    if from_scope == to_scope:
        raise click.ClickException("--from and --to must differ.")

    sources = _resolve_memory_migrate_sources(source)

    import asyncio

    asyncio.run(
        _memory_migrate_run(
            sources,
            from_scope,
            to_scope,
            apply_,
            yes,
            confirm_project_shared,
        )
    )


def _resolve_memory_migrate_sources(source_arg: str) -> list[Path]:
    """Resolve the ``SOURCE`` positional to a sorted list of .md files.

    Single existing file path → ``[that_path]`` (back-compat with v1).
    Otherwise → ``glob.glob(source, recursive=True)`` filtered to .md
    regular files. Empty match → ``ClickException`` so the caller
    isn't silently no-op'd by a typo in the pattern.
    """
    import glob as _glob

    expanded = Path(source_arg).expanduser()
    if expanded.is_file():
        return [expanded.resolve()]

    matches = _glob.glob(str(expanded), recursive=True)
    sources: list[Path] = sorted(
        {Path(m).resolve() for m in matches if Path(m).is_file() and m.endswith(".md")}
    )
    if not sources:
        raise click.ClickException(f"No .md files matched: {source_arg}")
    return sources


async def _memory_migrate_run(
    sources: list[Path],
    from_scope: TargetScope,
    to_scope: TargetScope,
    apply_: bool,
    yes: bool,
    confirm_project_shared: bool,
    *,
    stdout_buf: list[str] | None = None,
    stderr_buf: list[str] | None = None,
) -> None:
    """Apply or dry-run a memory-migrate plan over the resolved sources.

    ``stdout_buf`` / ``stderr_buf``: when provided, every plan/summary/
    error message that the CLI would emit via ``click.echo`` /
    ``click.secho`` is appended to the corresponding list INSTEAD of
    being written to the global ``sys.stdout`` / ``sys.stderr``. Used by
    the MCP wrapper (``mem_context_migrate``) to capture per-call
    output without ``contextlib.redirect_stdout`` — which would leak
    output from other concurrent MCP tool calls into the migrate
    response while this coroutine is suspended on I/O. When both
    buffers are ``None`` (CLI default) the helper behaves exactly as
    before.
    """
    import shutil
    from contextlib import ExitStack

    from memtomem import privacy
    from memtomem.cli._bootstrap import cli_components
    from memtomem.context._atomic import _file_lock, _lock_path_for
    from memtomem.memory_scope import (
        MemoryScopeError,
        is_project_tier_registered,
        project_tier_registration_error,
        resolve_memory_scope_dir,
    )

    # Routed emit: when capture buffers are provided (MCP path) the
    # message is appended to the buffer ONLY; nothing touches
    # ``sys.stdout`` / ``sys.stderr``. Default (CLI path) calls
    # ``click.secho`` so the terminal output and color codes are
    # preserved. Avoids the process-global ``redirect_stdout``
    # cross-request leakage flagged by Codex on PR #926.
    def _emit_out(msg: str, fg: str | None = None) -> None:
        if stdout_buf is not None:
            stdout_buf.append(msg)
        else:
            click.secho(msg, fg=fg)

    def _emit_err(msg: str, fg: str | None = None) -> None:
        if stderr_buf is not None:
            stderr_buf.append(msg)
        else:
            click.secho(msg, fg=fg, err=True)

    async with cli_components() as comp:
        # Project-tier resolution. For ``from`` project tiers we walk
        # up the *first* source path looking for the ``.memtomem``
        # ancestor; the project root is its parent. Glob inputs are
        # rooted in a single tier (we re-check per file below) so the
        # anchor is sufficient. Walking the ancestry chain handles
        # arbitrary subdirectory depth and any ``memories`` /
        # ``memories.local`` sibling under ``.memtomem/`` (memtomem
        # indexes recursively under those roots, so subdirectories
        # are first-class).
        anchor = sources[0]
        project_root: Path | None = None
        if from_scope != "user":
            for ancestor in anchor.parents:
                if ancestor.name == ".memtomem":
                    project_root = ancestor.parent
                    break
        if project_root is None and to_scope != "user":
            cwd_root = _find_project_root()
            if (cwd_root / ".git").exists() or (cwd_root / "pyproject.toml").exists():
                project_root = cwd_root
            else:
                raise click.ClickException(
                    f"Cannot determine project_root for scope='{to_scope}'. "
                    "Run from a project directory (with .git or pyproject.toml), "
                    "or migrate from a project tier so the project root is "
                    "inferred from the source path."
                )

        # Use the configured user-tier directory rather than the
        # ``~/.memtomem/memories`` default — keeps CLI behaviour consistent
        # with the active config (and lets tests isolate via ``memory_dirs``).
        mdirs = comp.config.indexing.memory_dirs
        user_base = Path(mdirs[0]) if mdirs else Path("~/.memtomem/memories")
        try:
            from_dir = resolve_memory_scope_dir(from_scope, project_root, user_base=user_base)
            to_dir = resolve_memory_scope_dir(to_scope, project_root, user_base=user_base)
        except MemoryScopeError as exc:
            raise click.ClickException(str(exc)) from exc

        # ADR-0011: the migrated row's scope/project_root only become
        # visible to search/recall and the indexing watcher when the
        # target tier directory is registered in
        # ``IndexingConfig.project_memory_dirs``. Without this guard,
        # ``mm context memory-migrate --to project_shared`` against a
        # project that has not yet been registered would produce a row
        # whose scope says "project_shared" but which the read surface
        # treats as out-of-scope and the watcher does not reindex —
        # silent data loss from the user's perspective.
        if to_scope != "user" and not is_project_tier_registered(
            to_dir, comp.config.indexing.project_memory_dirs
        ):
            raise click.ClickException(project_tier_registration_error(to_dir, to_scope))

        # Pre-flight pass: build the plan over every source. We check
        # under-from-dir + target-nonexistent + target-uniqueness + Gate
        # A privacy here so that nothing on disk moves until the whole
        # batch is known to be migratable. ``record_outcome=False`` on
        # the pre-flight scan; the apply pass re-scans with
        # ``record_outcome=True`` so the privacy audit log only records
        # writes that actually proceed — important for batches where a
        # late-discovered fail would otherwise leave allowed-write
        # records for files we never touched.
        plan: list[dict[str, Any]] = []
        seen_targets: set[str] = set()
        for src in sources:
            try:
                src.relative_to(from_dir)
            except ValueError:
                raise click.ClickException(
                    f"Source {src} is not under --from={from_scope} directory {from_dir}."
                )
            tgt = (to_dir / src.name).resolve()
            if tgt.exists():
                raise click.ClickException(
                    f"Target already exists: {tgt}. Move or rename it first."
                )
            # Codex review round 1, Blocker 1 / round 2 follow-up:
            # a glob like ``**/*.md`` can match two sources in
            # different subdirectories with the same basename (e.g.
            # ``a/rule.md`` and ``b/rule.md``). Both flatten to
            # ``to_dir/rule.md``; the on-disk ``tgt.exists()`` check
            # passes for both because neither destination exists yet.
            # The dedup key uses ``str(...).casefold()`` so the guard
            # also catches case-only collisions (``a/Rule.md`` +
            # ``b/rule.md``) on case-insensitive filesystems — macOS
            # APFS and Windows NTFS treat those as the same directory
            # entry, so a per-Path key would let the second file's
            # ``shutil.move`` silently overwrite the first migrated
            # one. On case-sensitive filesystems this is conservative
            # (refuses a legitimate ``Rule.md`` + ``rule.md`` pairing)
            # but two basenames differing only by case in a batch
            # migrate are nearly always unintentional, and refusing
            # is the safer failure mode for a destructive operation.
            # Escape hatch is the same as the cross-subdir case:
            # narrow the glob or rename one source.
            target_key = str(tgt).casefold()
            if target_key in seen_targets:
                raise click.ClickException(
                    f"Duplicate target after rename: {tgt}. Glob matched two "
                    f"sources whose basenames collide at the destination "
                    f"({src.name}, case-insensitive); flat rename would "
                    "silently overwrite on case-insensitive filesystems. "
                    "Narrow the glob or rename one source before migrating."
                )
            seen_targets.add(target_key)
            affected = await comp.storage.count_chunks_by_source(src)
            lineage = await comp.storage.count_chunk_links_for_source(src)

            if to_scope == "project_shared":
                content = src.read_text(encoding="utf-8")
                guard = privacy.enforce_write_guard(
                    content,
                    surface="memory_migrate",
                    force_unsafe=False,
                    scope=to_scope,
                    audit_context={"source": str(src), "target": str(tgt)},
                    record_outcome=False,
                )
                if guard.decision in ("blocked", "blocked_project_shared"):
                    _emit_err(
                        f"  ✗ Gate A: {src.name} matches {len(guard.hits)} privacy "
                        f"pattern(s); migration to scope='{to_scope}' rejected. "
                        "git history is forever — no force bypass available.",
                        fg="red",
                    )
                    raise click.exceptions.Exit(1)

            plan.append({"source": src, "target": tgt, "affected": affected, "lineage": lineage})

        is_batch = len(plan) > 1
        for entry in plan:
            _emit_out(f"Plan: migrate {entry['source'].name}")
            _emit_out(f"  from {from_scope}: {entry['source']}")
            _emit_out(f"  to   {to_scope}: {entry['target']}")
            _emit_out(f"  chunks affected: {entry['affected']}")
            # ``N preserved, 0 dropped`` for single-DB chunk-id-stable
            # rename: chunks.id never changes so the entire chunk_links
            # neighborhood survives untouched. Cross-DB migration (#911,
            # deferred per ADR-0012) is where ``dropped`` could become non-zero.
            _emit_out(f"  chunk_links lineage: {entry['lineage']} preserved, 0 dropped")

        if is_batch:
            total_chunks = sum(int(e["affected"]) for e in plan)
            total_lineage = sum(int(e["lineage"]) for e in plan)
            _emit_out(
                f"\nTotal: {len(plan)} files, {total_chunks} chunks affected, "
                f"{total_lineage} chunk_links preserved"
            )

        if not apply_:
            _emit_out("\nRun with --apply to execute.")
            return

        # ADR-0011 PR-D review round 10 (M2): require an explicit
        # ``--confirm-project-shared`` for project_shared targets.
        # ``--yes`` is a generic "skip prompts" flag users alias for
        # unrelated reasons; accepting it as Gate B satisfaction would
        # let ``mm context memory-migrate --to project_shared --yes``
        # silently rewrite git-tracked memory without an explicit
        # project-shared opt-in. One prompt covers the whole batch
        # since every file lands in the same tier.
        if to_scope == "project_shared" and not confirm_project_shared:
            if yes:
                raise click.ClickException(
                    "--to project_shared requires --confirm-project-shared. "
                    "--yes alone is not sufficient: project_shared writes go to "
                    "the git-tracked memory tier and require explicit opt-in."
                )
            file_word = "files" if is_batch else "file"
            count_str = f"{len(plan)} {file_word}" if is_batch else "this file"
            if not click.confirm(
                f"\nThis will write {count_str} to the git-tracked tier {to_dir}. Continue?",
                default=False,
            ):
                raise click.Abort()

        to_dir.mkdir(parents=True, exist_ok=True)
        # ADR-0011 PR-D review round 10 (B2): hold an exclusive sidecar
        # lock on BOTH source and target paths spanning the FS move and
        # the DB UPDATE. A concurrent ``mm web`` watcher fires
        # ``index_file(target)`` on the move event; without the lock the
        # watcher can race in between ``shutil.move`` and
        # ``update_chunks_scope_for_source`` and INSERT a fresh chunk
        # row at ``target`` (new UUID) before our UPDATE flips the
        # original chunk's source_file. End state: two sets of chunks
        # at the destination, defeating the chunk-id-stability guarantee
        # the migrate command promises. For batch mode we acquire every
        # lock up front via ``ExitStack`` so a watcher cannot race any
        # of the per-file pairs at any point mid-batch; reverse-order
        # release on context exit. Locks live on the file's parent so
        # they survive the rename (``feedback_sidecar_lockfile_for_
        # replaced_files.md``).
        completed: list[tuple[Path, Path, int]] = []
        # Codex review round 1, Major 1: acquire locks in a globally
        # stable order (sorted by string path) rather than plan order.
        # ``_file_lock`` uses blocking ``portalocker.LOCK_EX`` with no
        # timeout (see ``context/_atomic.py``), so two concurrent batch
        # migrations that share files in opposite orders would deadlock
        # indefinitely. ``context/migrate.py`` sorts lock paths for the
        # same reason. The unique set is built first so plan-order
        # duplicates (same source dir matched twice) don't try to
        # re-acquire the same lock on this thread.
        all_lock_paths: set[Path] = set()
        for entry in plan:
            all_lock_paths.add(_lock_path_for(entry["source"]))
            all_lock_paths.add(_lock_path_for(entry["target"]))
        with ExitStack() as stack:
            for lp in sorted(all_lock_paths, key=str):
                stack.enter_context(_file_lock(lp))

            for i, entry in enumerate(plan, 1):
                src, tgt = entry["source"], entry["target"]

                # Apply-time privacy audit record (Gate A re-scan).
                # Two reasons to re-scan here, not just to log an audit
                # hit:
                # 1. ``record_outcome=True`` only fires for writes that
                #    actually proceed (the pre-flight pass used
                #    ``record_outcome=False`` to avoid recording allowed
                #    writes for files a later batch failure would skip).
                # 2. Codex review round 1, Blocker 2: the file can
                #    change between pre-flight and apply, especially
                #    during the ``--confirm-project-shared`` prompt
                #    pause. If a secret was added in that window the
                #    pre-flight result is stale — we must honour the
                #    apply-time decision and abort before ``shutil.move``
                #    lands the content in the git-tracked tier.
                if to_scope == "project_shared":
                    content = src.read_text(encoding="utf-8")
                    guard = privacy.enforce_write_guard(
                        content,
                        surface="memory_migrate",
                        force_unsafe=False,
                        scope=to_scope,
                        audit_context={"source": str(src), "target": str(tgt)},
                        record_outcome=True,
                    )
                    if guard.decision in ("blocked", "blocked_project_shared"):
                        n_done = len(completed)
                        n_total = len(plan)
                        _emit_err(
                            f"  ✗ Gate A: {src.name} matches {len(guard.hits)} "
                            f"privacy pattern(s) at apply time (content changed "
                            "since pre-flight); migration to "
                            f"scope='{to_scope}' rejected. git history is "
                            "forever — no force bypass available.",
                            fg="red",
                        )
                        if is_batch:
                            _emit_err(
                                f"  {n_done} of {n_total} migrated before this "
                                f"file; remaining {n_total - n_done - 1} "
                                "file(s) untouched.",
                                fg="red",
                            )
                        raise click.exceptions.Exit(1)

                shutil.move(str(src), str(tgt))
                try:
                    updated = await comp.storage.update_chunks_scope_for_source(
                        src,
                        tgt,
                        to_scope,
                        project_root if to_scope != "user" else None,
                    )
                except Exception as exc:
                    # Compensation: SQLite TX cannot roll back the FS
                    # move on its own. Revert the move (best-effort)
                    # so the source path remains canonical and the next
                    # attempt sees the pre-migration state.
                    #
                    # ADR-0011 §5: per-file revert, leave already-
                    # completed files migrated, abort remaining. Gives
                    # the user a deterministic resumption point.
                    n_done = len(completed)
                    n_total = len(plan)
                    try:
                        shutil.move(str(tgt), str(src))
                    except Exception as revert_exc:
                        # Codex review round 1, Major 2: the double-
                        # failure branch is the highest-risk failure
                        # mode and was previously silent about the
                        # batch state. Emit the same K-of-N context as
                        # the happy-revert branch so the user knows
                        # how many earlier files are already migrated
                        # before they go inspect the divergent
                        # source/target pair.
                        _emit_err(
                            f"  ✗ DB update failed AND filesystem rollback "
                            f"failed for {src.name}; source/target may diverge. "
                            "Inspect both paths.",
                            fg="red",
                        )
                        if is_batch:
                            _emit_err(
                                f"  Batch state: {n_done} of {n_total} migrated "
                                f"before file {i}; remaining "
                                f"{n_total - n_done - 1} file(s) untouched.",
                                fg="red",
                            )
                        raise click.ClickException(
                            f"DB update failed AND filesystem rollback failed: "
                            f"db_error={exc!r}; rollback_error={revert_exc!r}"
                        ) from exc
                    if is_batch:
                        _emit_err(
                            f"  ✗ DB update failed on file {i} of {n_total} "
                            f"({src.name}); reverted that file. {n_done} of "
                            f"{n_total} migrated; remaining "
                            f"{n_total - n_done - 1} file(s) untouched.",
                            fg="red",
                        )
                    raise click.ClickException(
                        f"DB update failed; filesystem move reverted: {exc}"
                    ) from exc
                completed.append((src, tgt, int(updated)))

        comp.search_pipeline.invalidate_cache()
        if is_batch:
            total_rows = sum(c[2] for c in completed)
            _emit_out(
                f"  ✓ moved {len(completed)} files → {to_scope} tier; "
                f"{total_rows} chunk row(s) updated.",
                fg="green",
            )
        else:
            src, _tgt, updated = completed[0]
            _emit_out(
                f"  ✓ moved {src.name} → {to_scope} tier; {updated} chunk row(s) updated.",
                fg="green",
            )


# ---------------------------------------------------------------------------
# rescan — privacy-only audit, scope-aware across the three-tier model
# ---------------------------------------------------------------------------
#
# ADR-0011 / ADR-0016 follow-up (issue #934). The target set is now
# scope-dependent so a privacy audit can be run per tier without
# leaking into unrelated worktrees that happen to share state:
#
# - ``--scope=user`` walks ``~/.memtomem/{agents,skills,commands}/``
#   (canonical user tier). No project-root scanner files — those are
#   project-rooted by definition.
# - ``--scope=project_shared`` walks ``<root>/.memtomem/{agents,skills,
#   commands}/`` PLUS the project-root scanner files returned by
#   ``detect_agent_files(root)`` (CLAUDE.md, .cursorrules, GEMINI.md,
#   AGENTS.md, .github/copilot-instructions.md). Those scanner files
#   ARE the runtime fan-out of project_shared into the project root —
#   dropping them would silently regress v1 coverage from issue #885.
# - ``--scope=project_local`` walks ``<root>/.memtomem/{agents,skills,
#   commands}.local/`` only. No scanner files: per ADR-0011 §3 /
#   ADR-0016 §7 ``project_local`` is gitignored draft tier with NO
#   runtime fan-out — surfacing scanner-file violations under a
#   ``project_local`` audit would mislead the operator about the
#   tier's actual reach.
#
# Project tiers require a project context (``.git`` or
# ``pyproject.toml``); the marker check mirrors ``mm context init
# --scope`` at lines 737-748. Symlink-escape defence: descendants
# whose ``resolve()`` lands outside ``root.resolve()`` are dropped
# from the project-tier scan so a stray symlink into a sibling
# project's ``.memtomem/`` cannot pull foreign files into the
# decisions list.

_RESCAN_SCOPE_CHOICES = list(get_args(TargetScope))
_RESCAN_ARTIFACT_KINDS: tuple[str, ...] = ("agents", "skills", "commands")


def _rescan_targets(scope: TargetScope, project_root: Path | None) -> list[Path]:
    """Resolve the per-scope file/directory set the rescan walks.

    See the module-level comment block above the command for the full
    per-tier specification. Non-existent paths are silently dropped —
    rescan is an audit, not an inventory check, so a missing
    ``agents.local/`` does not constitute a failure.
    """
    targets: list[Path] = []
    if scope == "user":
        for kind in _RESCAN_ARTIFACT_KINDS:
            targets.append(canonical_artifact_dir(kind, "user", None))  # type: ignore[arg-type]
    elif scope == "project_shared":
        assert project_root is not None  # gated by caller
        for kind in _RESCAN_ARTIFACT_KINDS:
            targets.append(
                canonical_artifact_dir(kind, "project_shared", project_root)  # type: ignore[arg-type]
            )
        targets.extend(entry.path for entry in detect_agent_files(project_root))
    elif scope == "project_local":
        assert project_root is not None  # gated by caller
        for kind in _RESCAN_ARTIFACT_KINDS:
            targets.append(
                canonical_artifact_dir(kind, "project_local", project_root)  # type: ignore[arg-type]
            )
    return [t for t in targets if t.exists()]


@context.command("rescan")
@click.option(
    "--scope",
    type=click.Choice(_RESCAN_SCOPE_CHOICES),
    required=True,
    help=(
        "Tier to audit. Selects BOTH the canonical artifact directories "
        "walked AND the scope= value forwarded to enforce_write_guard. "
        "Required — there is no implicit default so audits are explicit "
        "and CI-readable. v1 always calls with force_unsafe=False."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a machine-readable JSON report instead of human output.",
)
@click.option(
    "--quiet",
    "-q",
    is_flag=True,
    default=False,
    help="Suppress per-file lines in human output; only print the summary.",
)
def rescan_cmd(
    scope: TargetScope,
    as_json: bool,
    quiet: bool,
) -> None:
    """Re-run the privacy guard over scope-tiered context artifacts.

    Read-only: no file is modified. The guard is invoked with
    ``record_outcome=False`` so the rescan does not double-count outcomes
    or emit bypass audit lines. ``on_blocked="skip_warn"`` collects every
    violation in the scanned set (not just the first).

    Exit codes: 0 if no violations, 1 if any violation found.
    """
    # ADR-0011 / issue #934: project tiers require a real project marker
    # so the audit can't accidentally walk a sibling worktree's tree
    # via ``_find_project_root``'s cwd fallback.
    project_root: Path | None
    if scope == "user":
        project_root = None
    else:
        candidate = _find_project_root()
        has_signal = (candidate / ".git").exists() or (candidate / "pyproject.toml").exists()
        if not has_signal:
            raise click.ClickException(
                f"--scope={scope} requires a project root (with .git or "
                "pyproject.toml). Use --scope=user from outside a project, "
                "or run from inside one."
            )
        project_root = candidate

    targets = _rescan_targets(scope, project_root)

    scanned = 0
    violations: list[dict] = []
    # Resolve once so the symlink-escape check is a cheap path-prefix
    # equality rather than a per-file resolve()-then-resolve() pair.
    resolved_root = project_root.resolve() if project_root is not None else None

    for target in targets:
        result = scan_artifact_tree(
            target,
            surface="cli_context_rescan",
            scope=scope,
            project_root=project_root,
            on_blocked="skip_warn",
            record_outcome=False,
        )
        for fs in result.decisions:
            # Symlink-escape defence (project tiers only). ``rglob`` in
            # ``scan_artifact_tree`` follows symlinks; if a descendant of
            # ``<root>/.memtomem/...`` resolves outside ``root`` it is
            # almost certainly a misconfigured / hostile link — drop it
            # from the audit rather than pretend the foreign file
            # belongs to this project. User tier is exempt because the
            # user canonical dir lives outside any project root by
            # design.
            if resolved_root is not None:
                try:
                    resolved_fs = fs.path.resolve()
                except OSError:
                    # Defensive: unresolvable symlink — fail closed by
                    # skipping rather than including. The fail-closed
                    # contract on read errors lives in scan_artifact_tree
                    # itself (PrivacyScanReadError); ``resolve()`` here
                    # is purely for the project-anchor check.
                    continue
                if not resolved_fs.is_relative_to(resolved_root):
                    continue
            scanned += 1
            if fs.decision == "pass":
                continue
            display_path = (
                fs.path.relative_to(project_root)
                if project_root is not None and fs.path.is_relative_to(project_root)
                else fs.path
            )
            violations.append(
                {
                    "path": str(display_path),
                    "scope": scope,
                    "decision": fs.decision,
                    "hits": [
                        {
                            "pattern_index": h.pattern_index,
                            "span_start": h.span[0],
                            "span_end": h.span[1],
                        }
                        for h in fs.hits
                    ],
                }
            )

    if as_json:
        payload = {
            "scope": scope,
            "scanned": scanned,
            "violations": violations,
        }
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if not quiet:
            click.echo(f"scanning {scanned} context file(s) in scope={scope}...")
            for v in violations:
                click.echo(
                    f"✗ {v['path']} scope={v['scope']} "
                    f"(decision={v['decision']}, {len(v['hits'])} hit"
                    f"{'s' if len(v['hits']) != 1 else ''})"
                )
                for h in v["hits"]:
                    click.echo(
                        f"    - pattern_index={h['pattern_index']} "
                        f"span=[{h['span_start']},{h['span_end']}]"
                    )
        click.echo(
            f"Summary: {len(violations)} violation"
            f"{'s' if len(violations) != 1 else ''} / "
            f"{scanned} file{'s' if scanned != 1 else ''} scanned. "
            f"Exit {1 if violations else 0}."
        )

    if violations:
        raise SystemExit(1)
