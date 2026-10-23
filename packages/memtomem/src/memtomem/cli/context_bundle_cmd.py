"""memtomem context migrate / move / copy / export / import commands."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import Any, cast, get_args

import click

from memtomem import privacy
from memtomem.cli import context_cmd as _ctx
from memtomem.config import TargetScope
from memtomem.context._names import InvalidNameError
from memtomem.context.bundle import (
    BundleFormatError,
    BundlePrivacyError,
    BundleSourceError,
)
from memtomem.context.mcp_servers_copy import McpServerCopyResult
from memtomem.context.migrate import (
    SCOPE_MIGRATABLE_KINDS,
    MigratePartialError,
    MigrateRow,
    MigrateScopeResult,
    StagingIdentityLostError,
    TransferStagingBusyError,
)
from memtomem.context.privacy_scan import PrivacyScanError
from memtomem.context.projects import UnknownProjectSelectorError
from memtomem.context.scope_resolver import ArtifactKind
from memtomem.context.transfer import TransferMode, TransferResult


_MIGRATE_GLYPH: dict[str, tuple[str, str]] = {
    # state -> (glyph, click color)
    "migrate": ("→", "green"),
    "noop": ("·", "white"),
    "cleanup_flat": ("±", "yellow"),
    "refuse_dirty": ("✗", "red"),
    "skip_manual": ("?", "yellow"),
    "skip_orphan": ("?", "yellow"),
}

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
    """Render the migrate dry-run / pre-apply preview."""
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
    """ADR-0011 PR-E4 scope-tier move for agents / commands / skills."""
    project_root = _ctx._find_project_root()

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

    if to_scope == "project_shared" and apply_:
        privacy.emit_project_shared_confirmation(
            surface="cli_context_migrate",
            mechanism="flag" if confirm_project_shared else "prompt",
            action="move",
            audit_context={"kind": asset_type, "name": name, "from_scope": from_scope},
        )

    with _ctx._translate_to_click(
        FileNotFoundError,
        ValueError,
        InvalidNameError,
        PrivacyScanError,
        MigratePartialError,
        TransferStagingBusyError,
        StagingIdentityLostError,
    ):
        result = _ctx.migrate_scope(
            asset_type,  # type: ignore[arg-type]
            name,
            from_scope=from_scope,  # type: ignore[arg-type]
            to_scope=to_scope,  # type: ignore[arg-type]
            project_root=project_root,
            apply_=apply_,
        )

    _ctx._print_migrate_scope_result(result, apply_=apply_)

    if apply_ and to_scope == "project_local":
        _ctx._ensure_local_tier_gitignored(project_root)


def _print_migrate_scope_result(result: MigrateScopeResult, *, apply_: bool) -> None:
    """User-facing summary for one scope-tier migration."""
    layout_note = " (flat layout)" if result.layout == "flat" else ""
    click.echo(f"Plan: migrate {result.kind}/{result.name}{layout_note}")
    click.echo(f"  from {result.from_scope}: {result.src_path}")
    click.echo(f"  to   {result.to_scope}: {result.dst_path}")

    if not apply_:
        if result.fanout_planned:
            click.echo(
                f"  will remove {len(result.fanout_planned)} stale runtime "
                f"fan-out target(s) at scope='{result.from_scope}' (content "
                f"that diverges from the canonical render is snapshotted to "
                f"a .bak first):"
            )
            for path in result.fanout_planned:
                click.echo(f"    - {path}")
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
    if result.fanout_backed_up:
        click.secho(
            f"  {len(result.fanout_backed_up)} target(s) diverged from the "
            f"canonical render — snapshotted before removal (review and "
            f"delete manually):",
            fg="yellow",
        )
        for path in result.fanout_backed_up:
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
    """ADR-0011 PR-E4 cross-link to ``mm context memory-migrate``."""
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
        _ctx._memory_migrate_run(
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


@click.command("migrate")
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
        "Requires --apply. Not accepted with --to: scope-tier moves always "
        "refuse on conflict (PR-E4), so passing --force there is an error."
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
    """Migrate canonical context artifacts."""
    if (force or yes) and not apply_:
        raise click.UsageError("--force / --yes are only valid with --apply")
    if name is not None and asset_type is None:
        raise click.UsageError("name argument requires asset_type")

    if asset_type == "memory":
        if from_scope is None or to_scope is None:
            raise click.UsageError("--from and --to are both required for kind=memory")
        from_scope_t = cast(TargetScope, from_scope)
        to_scope_t = cast(TargetScope, to_scope)
        _ctx._migrate_memory_dispatch(
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
        _ctx._migrate_scope_dispatch(
            asset_type,
            name,
            from_scope,
            to_scope,
            apply_,
            yes,
            confirm_project_shared,
        )
        return

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

    root = _ctx._find_project_root()

    try:
        rows = _ctx.classify_migrate(root, asset_type=asset_type, name=name)
    except (FileNotFoundError, ValueError, InvalidNameError) as exc:
        raise click.ClickException(str(exc)) from exc

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

    _ctx._print_migrate_preview(rows, skills_section=skills_section)
    summary = _ctx._summarize_migrate_rows(rows)
    click.echo(f"\nSummary: {summary}.")

    needs_force = [r for r in rows if r.state == "refuse_dirty"]
    actionable = [r for r in rows if r.state in {"migrate", "cleanup_flat"}]

    if not apply_:
        click.echo("\nRun with --apply to execute.")
        if needs_force:
            click.echo("Dirty/collision assets need --apply --force (creates .bak per dirty file).")
        return

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
            click.secho(
                f"  ✗  {row.asset_type}/{row.name}: dirty without --force",
                fg="red",
            )
            failures += 1
            continue

        result = _ctx.migrate_one(root, row, force=force)
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


def _transfer_options(fn: Any) -> Any:
    """Shared option surface for ``mm context copy`` / ``mm context move``."""
    decorators = [
        click.argument("asset_type", type=click.Choice([*SCOPE_MIGRATABLE_KINDS, "mcp-servers"])),
        click.argument("name"),
        click.option(
            "--from",
            "from_scope",
            type=click.Choice(list(get_args(TargetScope))),
            default=None,
            help=(
                "Explicit source tier (auto-detected; pass to disambiguate "
                "when the same name lives in multiple tiers)."
            ),
        ),
        click.option(
            "--to",
            "to_scope",
            type=click.Choice(list(get_args(TargetScope))),
            default=None,
            help=(
                "Destination tier. Omitted: keeps the source tier (the "
                "common cross-project case). At least one of --to / "
                "--to-project is required."
            ),
        ),
        click.option(
            "--to-project",
            "to_project",
            default=None,
            metavar="SCOPE_ID|PATH",
            help=(
                "Destination project — a p-<sha12> scope_id from `mm context "
                "projects list`, or a filesystem path (typing a path is "
                "consent to target an unregistered project). Omitted: the "
                "current project."
            ),
        ),
        click.option(
            "--apply",
            "apply_",
            is_flag=True,
            default=False,
            help="Execute the transfer (default is a dry-run preview).",
        ),
        click.option(
            "--yes",
            "-y",
            is_flag=True,
            default=False,
            help="Skip the confirmation prompt. Requires --apply.",
        ),
        click.option(
            "--confirm-project-shared",
            "confirm_project_shared",
            is_flag=True,
            default=False,
            help=(
                "Required (in addition to --apply) when the destination tier "
                "is project_shared, mirroring ``mm context migrate``. --yes "
                "alone does not satisfy the project_shared opt-in."
            ),
        ),
    ]
    for deco in reversed(decorators):
        fn = deco(fn)
    return fn


def _transfer_dispatch(
    mode: TransferMode,
    asset_type: str,
    name: str,
    *,
    from_scope: str | None,
    to_scope: str | None,
    to_project: str | None,
    new_name: str | None,
    apply_: bool,
    yes: bool,
    confirm_project_shared: bool,
) -> None:
    """Shared driver for ``mm context copy`` / ``mm context move`` (#1274)."""
    if yes and not apply_:
        raise click.UsageError("--yes is only valid with --apply")

    is_mcp = asset_type == "mcp-servers"
    if is_mcp:
        if mode != "copy":
            raise click.ClickException(
                "mcp-servers support copy only: the canonical is single-tier "
                "(project_shared), and a cross-project move would orphan the "
                "source project's .mcp.json fan-out. Copy it, then delete the "
                "source definition from its web panel if you meant move."
            )
        if new_name is not None:
            raise click.ClickException(
                "--as is not supported for mcp-servers; the copy keeps the "
                "server name (#1282 scope)."
            )
        for flag, value in (("--from", from_scope), ("--to", to_scope)):
            if value is not None and value != "project_shared":
                raise click.ClickException(
                    f"{flag} {value} is not valid for mcp-servers: the "
                    f"canonical is single-tier (project_shared) by design."
                )
        if to_project is None:
            raise click.UsageError(
                "mcp-servers copy is cross-project only: pass --to-project "
                "<scope_id|path> (within one project the canonical already "
                "exists; there is no second tier to copy to)."
            )
        to_scope = "project_shared"

    if to_scope is None and to_project is None:
        raise click.UsageError(
            f"nothing to do: pass --to <tier> and/or --to-project "
            f"<scope_id|path> (a same-tier, same-project {mode} is a no-op)"
        )
    if to_scope == "user" and to_project is not None:
        raise click.UsageError(
            "--to-project cannot be combined with --to user: the user tier "
            "is global (~/.memtomem), not per-project."
        )
    try:
        name_kind = "MCP server" if is_mcp else f"{asset_type[:-1]} name"
        _ctx.validate_name(name, kind=name_kind)
        if new_name is not None:
            _ctx.validate_name(new_name, kind=name_kind)
    except InvalidNameError as exc:
        raise click.ClickException(str(exc)) from exc

    src_root = _ctx._find_project_root()

    if to_project is not None:
        cfg = _ctx._projects_gateway_cfg()
        try:
            dst_root, dst_scope_rec = _ctx.resolve_project_selector(
                to_project, _ctx._projects_discover(cfg)
            )
        except UnknownProjectSelectorError as exc:
            raise click.ClickException(str(exc)) from exc
        if dst_scope_rec is not None and not dst_scope_rec.enabled:
            raise _ctx._paused_destination_error(
                dst_scope_rec.scope_id, dst_root, "transferred artifact"
            )
    else:
        dst_root = src_root

    if to_scope is None:
        to_scope, _src_path, _layout = _ctx._detect_source_scope(
            cast(ArtifactKind, asset_type),
            name,
            src_root,
            cast("TargetScope | None", from_scope),
            marker_counts_as_presence=True,
        )
        if to_scope == "user" and to_project is not None:
            raise click.UsageError(
                f"{asset_type}/{name} lives at the user tier, which is global "
                f"— pass --to project_shared or --to project_local to choose "
                f"the tier it should land in inside the destination project."
            )
    to_scope_t = cast(TargetScope, to_scope)

    if to_project is not None and to_scope_t != "user" and not (dst_root / ".memtomem").is_dir():
        raise _ctx._missing_store_error(dst_root)

    if to_scope_t == "project_shared" and apply_ and not confirm_project_shared:
        if yes:
            raise click.ClickException(
                "--to project_shared requires --confirm-project-shared. "
                "--yes alone is not sufficient: project_shared writes go "
                "to the git-tracked tier and require explicit opt-in."
            )
        if not click.confirm(
            f"\nThis will {mode} the canonical into the git-tracked project_shared tier. Continue?",
            default=False,
        ):
            raise click.Abort()

    if to_scope_t == "project_shared" and apply_:
        privacy.emit_project_shared_confirmation(
            surface=f"cli_context_{mode}",
            mechanism="flag" if confirm_project_shared else "prompt",
            action=mode,
            audit_context={"kind": asset_type, "name": name, "from_scope": from_scope},
        )

    result: TransferResult | McpServerCopyResult
    with _ctx._translate_to_click(
        FileNotFoundError,
        ValueError,
        InvalidNameError,
        PrivacyScanError,
        MigratePartialError,
        TransferStagingBusyError,
        StagingIdentityLostError,
    ):
        if is_mcp:
            result = _ctx.copy_mcp_server(
                name,
                src_project_root=src_root,
                dst_project_root=dst_root,
                apply_=apply_,
                surface=f"cli_context_{mode}",
            )
        else:
            result = _ctx.transfer_artifact(
                cast(ArtifactKind, asset_type),
                name,
                src_project_root=src_root,
                from_scope=cast("TargetScope | None", from_scope),
                dst_project_root=None if to_scope_t == "user" else dst_root,
                to_scope=to_scope_t,
                mode=mode,
                apply_=apply_,
                surface=f"cli_context_{mode}",
                new_name=new_name,
            )

    _ctx._print_transfer_result(result, apply_=apply_)

    if apply_ and to_scope_t == "project_local":
        _ctx._ensure_local_tier_gitignored(dst_root)


def _maybe_hint_adopt_untracked(result: TransferResult | McpServerCopyResult) -> None:
    """One-line adopt follow-up under a transfer's untracked-landing warning (#1684)."""
    if result.kind == "mcp-servers":
        return
    singular = result.kind.removesuffix("s")
    click.echo(
        f"  if the destination bytes match its wiki HEAD, "
        f"`mm context adopt {singular} {result.dst_name}` there re-establishes "
        f"the pin without reinstalling"
    )


def _print_transfer_result(result: TransferResult | McpServerCopyResult, *, apply_: bool) -> None:
    """User-facing summary for one copy/move transfer (#1274)."""
    layout_note = (
        " (flat layout)" if result.layout == "flat" and result.kind != "mcp-servers" else ""
    )
    rename_note = f" as {result.dst_name}" if result.dst_name != result.name else ""
    click.echo(f"Plan: {result.mode} {result.kind}/{result.name}{rename_note}{layout_note}")
    click.echo(f"  from {result.from_scope}: {result.src_path}")
    click.echo(f"  to   {result.to_scope}: {result.dst_path}")

    if not apply_:
        if result.fanout_planned:
            click.echo(
                f"  will remove {len(result.fanout_planned)} stale runtime "
                f"fan-out target(s) at scope='{result.from_scope}' (content "
                f"that diverges from the canonical render is snapshotted to "
                f"a .bak first):"
            )
            for path in result.fanout_planned:
                click.echo(f"    - {path}")
        if result.provenance == "carried":
            click.echo(
                "  will carry the wiki install provenance (lock.json entry) to the destination"
            )
        elif result.provenance == "not_carried":
            click.secho(
                f"  install provenance will not be carried — the artifact "
                f"lands untracked: {result.provenance_reason}",
                fg="yellow",
            )
            _ctx._maybe_hint_adopt_untracked(result)
        for note in result.notes:
            click.secho(f"  note: {note}", fg="yellow")
        confirm_note = " --confirm-project-shared" if result.to_scope == "project_shared" else ""
        click.echo(f"\nRun with --apply{confirm_note} to execute.")
        if result.needs_sync and result.sync_command:
            click.echo(f"After apply, run `{result.sync_command}` to refresh runtime fan-out.")
        elif result.needs_sync and result.sync_hint:
            click.echo(f"After apply, {result.sync_hint}")
        return

    verb = "moved" if result.mode == "move" else "copied"
    click.secho(
        f"  ✓ {verb} {result.kind}/{result.name}: "
        f"{result.from_scope} → {result.to_scope}{rename_note}",
        fg="green",
    )
    if result.fanout_cleaned:
        click.echo(
            f"  cleaned {len(result.fanout_cleaned)} stale runtime fan-out "
            f"target(s) at scope='{result.from_scope}':"
        )
        for path in result.fanout_cleaned:
            click.echo(f"    - {path}")
    if result.fanout_backed_up:
        click.secho(
            f"  {len(result.fanout_backed_up)} target(s) diverged from the "
            f"canonical render — snapshotted before removal (review and "
            f"delete manually):",
            fg="yellow",
        )
        for path in result.fanout_backed_up:
            click.echo(f"    - {path}")
    if result.provenance == "carried":
        click.echo("  carried the wiki install provenance (lock.json entry) to the destination")
    elif result.provenance == "not_carried":
        click.secho(
            f"  install provenance not carried — the artifact lands "
            f"untracked at the destination: {result.provenance_reason}",
            fg="yellow",
        )
        _ctx._maybe_hint_adopt_untracked(result)
    for note in result.notes:
        click.secho(f"  note: {note}", fg="yellow")
    if result.needs_sync and result.sync_command:
        click.echo(
            f"\nNext: run `{result.sync_command}` to generate runtime fan-out at the destination."
        )
    elif result.needs_sync and result.sync_hint:
        click.echo(f"\nNext: {result.sync_hint}")


@click.command("move")
@_transfer_options
def move_cmd(
    asset_type: str,
    name: str,
    from_scope: str | None,
    to_scope: str | None,
    to_project: str | None,
    apply_: bool,
    yes: bool,
    confirm_project_shared: bool,
) -> None:
    """Move one canonical artifact to another tier and/or project."""
    _ctx._transfer_dispatch(
        "move",
        asset_type,
        name,
        from_scope=from_scope,
        to_scope=to_scope,
        to_project=to_project,
        new_name=None,
        apply_=apply_,
        yes=yes,
        confirm_project_shared=confirm_project_shared,
    )


@click.command("copy")
@_transfer_options
@click.option(
    "--as",
    "new_name",
    default=None,
    metavar="NEW_NAME",
    help=(
        "Name for the copy at the destination (copy only). The staged "
        "manifest's frontmatter name: is rewritten to match; overrides/ "
        "and frozen versions/ snapshots travel verbatim."
    ),
)
def copy_cmd(
    asset_type: str,
    name: str,
    from_scope: str | None,
    to_scope: str | None,
    to_project: str | None,
    apply_: bool,
    yes: bool,
    confirm_project_shared: bool,
    new_name: str | None,
) -> None:
    """Copy one canonical artifact to another tier and/or project."""
    _ctx._transfer_dispatch(
        "copy",
        asset_type,
        name,
        from_scope=from_scope,
        to_scope=to_scope,
        to_project=to_project,
        new_name=new_name,
        apply_=apply_,
        yes=yes,
        confirm_project_shared=confirm_project_shared,
    )


@click.command("export")
@click.argument("asset_type", type=click.Choice(list(SCOPE_MIGRATABLE_KINDS)))
@click.argument("name")
@click.option(
    "--from",
    "from_scope",
    type=click.Choice(["user", "project_shared", "project_local"]),
    default=None,
    help="Source tier. Auto-detected when omitted (refuses if the name resolves in more than one).",
)
@click.option(
    "--out",
    "out_path",
    required=True,
    type=click.Path(dir_okay=False, path_type=Path),
    metavar="FILE",
    help="Where to write the bundle. Never overwritten; pick another path to re-export.",
)
@click.option(
    "--no-versions",
    is_flag=True,
    help=(
        "Drop versions/ and versions.json together. They are removed before the "
        "walk, so a secret or an unhealthy manifest in frozen history cannot "
        "block the export."
    ),
)
def export_cmd(
    asset_type: str, name: str, from_scope: str | None, out_path: Path, no_versions: bool
) -> None:
    """Pack one canonical artifact into a portable bundle file (ADR-0037)."""
    with _ctx._translate_to_click(
        FileNotFoundError,
        ValueError,
        InvalidNameError,
        PrivacyScanError,
        BundleFormatError,
        BundleSourceError,
        OSError,
    ):
        result = _ctx.export_artifact_bundle(
            cast(ArtifactKind, asset_type),
            name,
            src_project_root=_ctx._find_project_root(),
            from_scope=cast("TargetScope | None", from_scope),
            out_path=out_path,
            include_versions=not no_versions,
        )
    click.echo(
        f"exported {result.kind}/{result.name} from {result.from_scope} "
        f"({result.file_count} file{'s' if result.file_count != 1 else ''}"
        f"{', with version history' if result.versions_included else ', no version history'})"
    )
    click.echo(f"  -> {result.out_path}")
    for note in result.notes:
        click.echo(f"  note: {note}")
    if result.redaction_exempted:
        click.secho(
            f"  warning: redaction: documents-patterns waived credential-shaped matches in "
            f"{', '.join(result.redaction_exempted)}",
            fg="yellow",
        )
        click.secho(
            "           confirm none of them is a real value before sending this file.",
            fg="yellow",
        )
    if result.source_wiki_commit:
        click.echo(f"  source is wiki-installed at {result.source_wiki_commit[:12]}")
    click.echo(
        f"  land it with: mm context import {shlex.quote(str(result.out_path))} --to <tier> --apply"
    )


@click.command("import")
@click.argument("bundle", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--to",
    "to_scope",
    required=True,
    type=click.Choice(["user", "project_shared", "project_local"]),
    help="Landing tier. Required: a foreign file must not choose where it lands.",
)
@click.option(
    "--to-project",
    default=None,
    metavar="SCOPE_ID|PATH",
    help="Land in another registered project (or a typed path, which is consent).",
)
@click.option(
    "--as",
    "new_name",
    default=None,
    metavar="NEW_NAME",
    help="Land under a different name. The manifest's name: is rewritten to match.",
)
@click.option("--apply", "apply_", is_flag=True, help="Perform the write (default is a preview).")
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation prompt.")
@click.option(
    "--confirm-project-shared",
    is_flag=True,
    help="Required for a project_shared landing; --yes alone is not sufficient.",
)
@click.option(
    "--force-unsafe-import",
    "force_unsafe",
    is_flag=True,
    help=(
        "Accept a secret-shaped value into user / project_local after review. "
        "project_shared refuses regardless — git history is forever (ADR-0011 §5)."
    ),
)
def import_cmd(
    bundle: Path,
    to_scope: str,
    to_project: str | None,
    new_name: str | None,
    apply_: bool,
    yes: bool,
    confirm_project_shared: bool,
    force_unsafe: bool,
) -> None:
    """Land a context artifact bundle in a canonical store (ADR-0037)."""
    to_scope_t = cast(TargetScope, to_scope)
    if to_scope_t == "user" and to_project is not None:
        raise click.UsageError("--to user is the global tier; --to-project does not apply to it.")
    if force_unsafe and to_scope_t == "project_shared":
        raise click.UsageError(
            "--force-unsafe-import cannot be combined with --to project_shared. "
            "That tier is git-tracked and refuses a secret regardless of the flag "
            "(ADR-0011 §5). Import to project_local, or ask the sender to re-export."
        )

    src_root = _ctx._find_project_root()
    if to_project is not None:
        cfg = _ctx._projects_gateway_cfg()
        try:
            dst_root, dst_scope_rec = _ctx.resolve_project_selector(
                to_project, _ctx._projects_discover(cfg)
            )
        except UnknownProjectSelectorError as exc:
            raise click.ClickException(str(exc)) from exc
        if dst_scope_rec is not None and not dst_scope_rec.enabled:
            raise _ctx._paused_destination_error(
                dst_scope_rec.scope_id, dst_root, "imported artifact"
            )
        if to_scope_t != "user" and not (dst_root / ".memtomem").is_dir():
            raise _ctx._missing_store_error(dst_root)
    else:
        dst_root = src_root

    if to_scope_t == "project_shared" and apply_ and not confirm_project_shared:
        if yes:
            raise click.ClickException(
                "--to project_shared requires --confirm-project-shared. "
                "--yes alone is not sufficient: project_shared writes go "
                "to the git-tracked tier and require explicit opt-in."
            )
        if not click.confirm(
            "\nThis will land a bundle in the git-tracked project_shared tier. Continue?",
            default=False,
        ):
            raise click.Abort()

    if to_scope_t == "project_shared" and apply_:
        privacy.emit_project_shared_confirmation(
            surface="cli_context_import",
            mechanism="flag" if confirm_project_shared else "prompt",
            action="import",
        )

    def _protect_local_tier() -> None:
        if to_scope_t == "project_local":
            _ctx._ensure_local_tier_gitignored(dst_root, required=True)

    try:
        with _ctx._translate_to_click(
            FileNotFoundError,
            ValueError,
            InvalidNameError,
            PrivacyScanError,
            BundleFormatError,
            BundleSourceError,
            OSError,
        ):
            result = _ctx.receive_artifact_bundle(
                bundle,
                dst_project_root=dst_root,
                to_scope=to_scope_t,
                apply_=apply_,
                new_name=new_name,
                force_unsafe=force_unsafe,
                pre_materialize=_protect_local_tier,
            )
    except BundlePrivacyError as exc:
        raise click.ClickException(_ctx.remediation.append_hint(exc.message, exc.code, "cli")) from exc

    verb = "imported" if result.received else "would import"
    click.echo(
        f"{verb} {result.kind}/{result.dst_name} to {result.to_scope} "
        f"({result.file_count} file{'s' if result.file_count != 1 else ''})"
    )
    click.echo(f"  -> {result.dst_path}")
    if result.dst_name != result.name:
        click.echo(f"  renamed from {result.name}")
    if not result.versions_included:
        click.echo("  no version history in this bundle")
    if result.redaction_exempted:
        click.secho(
            f"  warning: the sender declared redaction: documents-patterns, waiving "
            f"credential-shaped matches in {', '.join(result.redaction_exempted)}",
            fg="yellow",
        )
    for note in result.notes:
        click.echo(f"  note: {note}")
    if not result.received:
        click.echo("  preview only — re-run with --apply to write")
        return
    click.echo("  landed untracked: mm context update does not manage it")
    if result.adopt_hint:
        click.echo(f"  adopt it against your own wiki: {result.adopt_hint}")
    if result.sync_command:
        click.echo(f"  fan it out with: {result.sync_command}")
