"""memtomem context settings-doctor / settings-migrate / settings-copy commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, get_args

import click

from memtomem import privacy
from memtomem._runtime_paths import scrub_text
from memtomem.cli._context_facade import context_cmd as _ctx
from memtomem.cli.context_options import _SCOPE_OPTION
from memtomem.config import TargetScope
from memtomem.context.error_redact import redact_secret_value
from memtomem.context.privacy_scan import PrivacyScanError
from memtomem.context.projects import UnknownProjectSelectorError
from memtomem.context.settings_copy import HookCopyPlan, HookCopyResult
from memtomem.context.settings_doctor import (
    format_signature_label,
    redact_command_shape,
    redact_unportable_command_fields,
)


@click.command("settings-doctor")
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

    Exit codes: ``0`` clean (or advisory warnings only), ``1`` duplicates or
    malformed matchers found.
    """
    root = _ctx._find_project_root()
    scope = _ctx._resolve_cli_scope(scope_flag)
    duplicates = _ctx.detect_duplicate_tiers(root, active_scope=scope)
    malformed = _ctx.find_malformed_matchers(root)
    unportable = _ctx.find_unportable_hook_commands(root)
    unscanned = _ctx.find_unscanned_settings_files(root)

    if json_out:
        if duplicates:
            status = "duplicates"
        elif malformed:
            status = "malformed"
        elif unscanned:
            status = "incomplete"
        elif unportable:
            # Advisory like ``incomplete`` (exit 0), but a script reading
            # ``status`` must not see "clean" beside findings (#2482).
            status = "advisory"
        else:
            status = "clean"
        payload = {
            "status": status,
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
            "malformed_matchers": [
                {
                    "source": finding.source,
                    "tier": finding.tier,
                    "path": str(finding.path),
                    "event": finding.event,
                    "rule_index": finding.rule_index,
                    "matcher_type": finding.matcher_type,
                }
                for finding in malformed
            ],
            "unportable_commands": [
                {
                    "source": finding.source,
                    "tier": finding.tier,
                    "path": str(finding.path),
                    "event": finding.event,
                    "rule_index": finding.rule_index,
                    "hook_index": finding.hook_index,
                    "command": finding.command,
                    "unportable_literal": finding.unportable_literal,
                }
                for finding in unportable
            ],
            "unscanned_settings": [
                {
                    "source": item.source,
                    "tier": item.tier,
                    "path": str(item.path),
                    "reason": item.reason,
                }
                for item in unscanned
            ],
        }
        click.echo(json.dumps(payload, indent=2))
    else:
        if not duplicates and not malformed and not unportable and not unscanned:
            click.secho(
                f"✓ No memtomem-managed hooks duplicated outside the active scope ({scope}).",
                fg="green",
            )
        if duplicates:
            click.secho(
                f"✗ Found memtomem-managed hooks in {len(duplicates)} other "
                f"tier(s) (active scope: {scope}):",
                fg="yellow",
            )
            for dup in duplicates:
                click.secho(f"  • {dup.tier} ({scrub_text(str(dup.path))})", fg="yellow")
                for sig in dup.entries:
                    click.echo(
                        f"      [{format_signature_label(sig)}] "
                        f"{redact_command_shape(sig.command_shape)}"
                    )
            click.echo(
                "\nRun `mm context settings-migrate --from=<scope> "
                "--to=<scope>` to move these into the active scope."
            )
        if malformed:
            click.secho(
                f"✗ Found {len(malformed)} hook rule(s) with malformed matchers:",
                fg="yellow",
            )
            for finding in malformed:
                location = "canonical" if finding.source == "canonical" else f"{finding.tier} tier"
                click.secho(f"  • {location} ({scrub_text(str(finding.path))})", fg="yellow")
                click.echo(
                    f"      [{scrub_text(redact_secret_value(finding.event))} rule #{finding.rule_index}] "
                    f"non-string matcher ({scrub_text(finding.matcher_type)})"
                )
            click.echo("\n`matcher` must be a string. Omit it for match-all, or quote the value.")
        if unportable:
            num_commands = len({(f.path, f.event, f.rule_index, f.hook_index) for f in unportable})
            click.secho(
                f"✗ Found {num_commands} hook command(s) with unportable home paths:",
                fg="yellow",
            )
            for unportable_finding in unportable:
                location = (
                    "canonical"
                    if unportable_finding.source == "canonical"
                    else f"{unportable_finding.tier} tier"
                )
                click.secho(
                    f"  • {location} ({scrub_text(str(unportable_finding.path))})", fg="yellow"
                )
                safe_cmd, safe_lit = redact_unportable_command_fields(
                    unportable_finding.command, unportable_finding.unportable_literal
                )
                click.echo(
                    f"      [{scrub_text(redact_secret_value(unportable_finding.event))} "
                    f"rule #{unportable_finding.rule_index} "
                    f"hook #{unportable_finding.hook_index}] "
                    f"'{safe_lit}' in '{safe_cmd}'"
                )
            click.echo("\nRewrite hook commands with $HOME, ~, or repo-relative paths.")
        if unscanned:
            click.secho(
                f"! Could not check {len(unscanned)} settings file(s):",
                fg="yellow",
            )
            for item in unscanned:
                location = "canonical" if item.source == "canonical" else f"{item.tier} tier"
                click.secho(f"  • {location} ({scrub_text(str(item.path))})", fg="yellow")
                click.echo(f"      {scrub_text(item.reason)}")
            click.echo("\nFix or remove these files; nothing in them was verified.")

    if duplicates or malformed:
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


def _print_migrate_plan_human(plan: Any) -> None:
    """Render the settings-migrate dry-run / pre-apply preview.

    Event, matcher, command and tier paths all come out of the settings files
    being migrated, so each is redacted and escaped the way the doctor renders
    the same fields. The ``--json`` branch keeps the raw values (#2477).
    """
    if not plan.moves:
        click.echo(
            f"  no memtomem-managed hook entries in {plan.source_scope} "
            f"({scrub_text(str(plan.source_path))}) match the canonical source "
            f"— nothing to migrate."
        )
        return

    click.echo(
        f"\nWill migrate hook entries from {plan.source_scope} "
        f"({scrub_text(str(plan.source_path))}) "
        f"→ {plan.target_scope} ({scrub_text(str(plan.target_path))}):"
    )
    for move in plan.moves:
        sig = move.signature
        label = format_signature_label(sig)
        if move.conflict_at_target:
            glyph, color = "✗", "red"
            note = f"skip (conflict: {scrub_text(str(move.conflict_reason))})"
        elif move.already_at_target:
            glyph, color = "·", "cyan"
            note = "already at target — source clean-up only"
        else:
            glyph, color = "→", "green"
            note = "move"
        click.secho(
            f"  {glyph}  [{label}]  {redact_command_shape(sig.command_shape)}  ({note})",
            fg=color,
        )


@click.command("settings-migrate")
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
    help=(
        "Skip the host-write prompt (a source or target outside the project "
        "root). Requires --apply. Does NOT satisfy --confirm-project-shared."
    ),
)
@click.option(
    "--confirm-project-shared",
    "confirm_project_shared",
    is_flag=True,
    default=False,
    help=(
        "Required (in addition to --apply) when either leg is "
        "project_shared: the target tier file is tracked by this "
        "repository, and so is the source the migration strips. --yes "
        "alone does not satisfy this opt-in (mirrors "
        "`mm context settings-copy`)."
    ),
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
    confirm_project_shared: bool,
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

    Two independent gates, because they answer different questions. The
    host-write prompt asks about leaving the project; ADR-0011 §5 Gate B
    asks about touching the tier this repository tracks, and it fires
    whenever **either** leg is ``project_shared`` — the target because
    entries land in the tracked file, the source because the migration
    strips them out of it. ``--yes`` answers only the first.

    Exit codes: ``0`` clean (or dry-run), ``1`` user declined either
    confirmation or the plan reported conflicts requiring manual
    resolution.
    """
    root = _ctx._find_project_root()
    try:
        plan = _ctx.plan_migration(root, source_scope=from_scope, target_scope=to_scope)
    except ValueError as exc:
        if json_out:
            click.echo(json.dumps({"status": "error", "error": str(exc)}, indent=2))
        else:
            click.secho(f"error: {scrub_text(str(exc))}", fg="red", err=True)
        raise click.exceptions.Exit(1)

    conflicts = [m for m in plan.moves if m.conflict_at_target]
    summary = _ctx.format_plan_summary(plan)

    # ADR-0011 §5 Gate B predicate. Exactly one leg can be project_shared —
    # plan_migration rejects a same-scope and same-path request — and either
    # one is a pending change to a file this repository tracks: the target
    # receives entries, the source has them stripped out. ``applicable_moves``
    # (not ``moves``) keeps the #1263 contract that a no-op or all-conflict
    # plan, which writes nothing, never prompts. A move the planner marked
    # ``already_at_target`` stays in scope: apply re-classifies against the
    # live tier and restores an entry that drifted away in between.
    applicable = plan.applicable_moves
    shared_leg = (
        "target"
        if plan.target_scope == "project_shared"
        else ("source" if plan.source_scope == "project_shared" else None)
    )
    git_tracked_write = bool(applicable) and shared_leg is not None

    if json_out:
        payload: dict[str, Any] = {
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
        _ctx._print_migrate_plan_human(plan)
        if plan.moves:
            click.echo(f"\nSummary: {summary}")

    if not apply_:
        if json_out:
            click.echo(json.dumps(payload, indent=2))
        else:
            confirm_note = " --confirm-project-shared" if git_tracked_write else ""
            click.echo(f"\nRun with --apply{confirm_note} to execute.")
        return

    # --apply path. ``is_noop`` is True both when there is nothing to
    # migrate at all AND when every move is a conflict (applicable_moves
    # is empty in both cases). Differentiate so all-conflict still exits
    # 1 — the user has unresolved drift to fix.
    if plan.is_noop and not conflicts:
        if json_out:
            payload["applied"] = True
            click.echo(json.dumps(payload, indent=2))
        return

    # ADR-0011 §5 Gate B. Before the host-write prompt, because the two
    # gates are not interchangeable: --yes answers the host prompt, and
    # nothing but --confirm-project-shared answers this one.
    if git_tracked_write and not confirm_project_shared:
        if shared_leg == "target":
            refusal = (
                "--to project_shared requires --confirm-project-shared. "
                "--yes alone is not sufficient: project_shared writes go to "
                "the tier this repository tracks and require explicit opt-in."
            )
        else:
            refusal = (
                "--from project_shared requires --confirm-project-shared. "
                "--yes alone is not sufficient: the migration strips the "
                "entries out of the tier this repository tracks, and "
                "removing bytes the project committed requires the same "
                "explicit opt-in as adding them."
            )
        # ``--json`` is answered before ``--yes`` so a machine caller always
        # gets a machine answer. The plan-time ValueError branch above
        # already hands this command's JSON callers a structured error; a
        # bare stderr line here would be the single refusal they could not
        # parse. ``--yes`` is not silently ignored — it comes back as
        # ``error``, so a script can tell "you passed the wrong flag" from
        # "you passed no flag".
        if json_out:
            refusal_payload: dict[str, Any] = {
                "status": "needs_confirmation",
                "applied": False,
                "from": plan.source_scope,
                "to": plan.target_scope,
                "source_path": str(plan.source_path),
                "target_path": str(plan.target_path),
                "project_shared_leg": shared_leg,
                "project_shared_path": str(
                    plan.target_path if shared_leg == "target" else plan.source_path
                ),
                "hint": "Re-run with --confirm-project-shared after confirming.",
            }
            if yes:
                refusal_payload["error"] = refusal
            click.echo(json.dumps(refusal_payload, indent=2))
            raise click.exceptions.Exit(1)
        if yes:
            raise click.ClickException(refusal)
        count = len(applicable)
        entries = "entry" if count == 1 else "entries"
        if shared_leg == "target":
            question = (
                f"\nThis will write {count} hook {entries} into the "
                f"project_shared tier ({scrub_text(str(plan.target_path))}), which this "
                f"repository tracks. Continue?"
            )
        else:
            question = (
                f"\nThis will remove {count} hook {entries} from the "
                f"project_shared tier ({scrub_text(str(plan.source_path))}), which this "
                f"repository tracks. Continue?"
            )
        if not click.confirm(question, default=False):
            raise click.Abort()

    # ADR-0011 §5 Gate B consent (#2306). After the block, not inside it —
    # the block runs only when the flag is absent. What is recorded is the
    # consent given, not the write landing: the host-write prompt below and
    # Gate A inside the engine can still refuse afterwards, and the record
    # stays. ``from_scope``/``to_scope`` name the shared leg between them,
    # so no separate action verb is invented for a removal.
    if git_tracked_write:
        privacy.emit_project_shared_confirmation(
            surface="cli_context_settings_migrate",
            mechanism="flag" if confirm_project_shared else "prompt",
            action="move",
            audit_context={
                "from_scope": plan.source_scope,
                "to_scope": plan.target_scope,
                "shared_leg": shared_leg,
                "moves": len(applicable),
            },
        )

    # Host-write confirmation: target outside the project root requires
    # the same gate as `mm context sync --include=settings` so a stray
    # `--apply` from a worktree can't silently rewrite ~/.claude/. Only
    # prompt when we'd actually write — an all-conflict plan touches
    # nothing on disk.
    target_outside = not _ctx._is_within(plan.target_path, root)
    source_outside = not _ctx._is_within(plan.source_path, root)
    if applicable and (target_outside or source_outside) and not yes:
        if not json_out:
            click.secho(
                "settings-migrate will modify the following files outside this project:",
                fg="yellow",
            )
            if target_outside:
                click.echo(f"  {scrub_text(str(plan.target_path))}  (target)")
            if source_outside:
                click.echo(f"  {scrub_text(str(plan.source_path))}  (source)")
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

    try:
        result = _ctx.apply_migration(plan, surface="cli_context_settings_migrate")
    except PrivacyScanError as exc:
        # Gate A refused inside the engine. Answer a JSON caller in JSON, as
        # the plan-time failure above and the Gate B refusal already do —
        # this is the last refusal this command can make, and leaving it as
        # the one unparseable one is what the Gate B fix set out to avoid.
        # ``status`` reuses the plan-time vocabulary rather than inventing a
        # value a consumer does not branch on; ``refusal`` is the machine
        # discriminator, since both carry a human message under ``error``.
        # Any consent recorded above stands: it reports the consent given,
        # not the write landing.
        if json_out:
            click.echo(
                json.dumps(
                    {
                        "status": "error",
                        "refusal": "gate_a",
                        "applied": False,
                        "from": plan.source_scope,
                        "to": plan.target_scope,
                        "source_path": str(plan.source_path),
                        "target_path": str(plan.target_path),
                        "error": exc.message,
                    },
                    indent=2,
                )
            )
            raise click.exceptions.Exit(1) from exc
        # The Gate A message names the canonical and target tier paths.
        raise click.ClickException(scrub_text(exc.message)) from exc

    # Drift the planner could not see — the target changed between plan and
    # apply — surfaces as apply-time warnings (#1123 B4-3). Treat it like a
    # plan-time conflict for reporting and the exit code.
    apply_drift = bool(result.warnings)

    if json_out:
        payload["applied"] = True
        payload["target_written"] = result.target_written
        payload["source_written"] = result.source_written
        if result.warnings:
            payload["warnings"] = result.warnings
            payload["status"] = "conflicts"
        click.echo(json.dumps(payload, indent=2))
    else:
        if result.target_written:
            click.secho(
                f"  ✓ wrote target {scrub_text(str(plan.target_path))}",
                fg="green",
            )
        if result.source_written:
            click.secho(
                f"  ✓ cleaned source {scrub_text(str(plan.source_path))}",
                fg="green",
            )
        # Apply-time drift warnings quote the target rule the planner could
        # not see, so they carry settings text like every other warning here.
        for warning in result.warnings:
            click.secho(f"  ⚠ {scrub_text(warning)}", fg="yellow", err=True)
        if not result.target_written and not result.source_written and not result.warnings:
            if conflicts:
                click.secho(
                    "  (no changes written — conflicts must be resolved first)",
                    fg="yellow",
                )
            else:
                click.echo("  (no changes written — source was already clean)")

    if conflicts or apply_drift:
        # Conflicts left in source — flagged at plan time, or drift
        # discovered at apply time; the user must resolve manually before
        # re-running migrate. Match `mm context migrate`'s exit-1 on any
        # partial failure.
        raise click.exceptions.Exit(1)


# ── settings-copy (#1281, campaign #1270 A-11) ──────────────────────


def _leg_glyph_note(state: str, reason: str, *, already_note: str, add_note: str) -> tuple:
    """(glyph, color, note) for one settings-copy plan leg."""
    if state == "conflict":
        return ("✗", "red", f"skip (conflict: {reason})")
    if state == "exact":
        return ("·", "cyan", already_note)
    return ("→", "green", add_note)


def _print_hook_copy_plan(plan: HookCopyPlan) -> None:
    """Render the settings-copy dry-run / pre-apply preview.

    The command comes out of the source canonical file, so it gets the full
    :func:`redact_command_shape` treatment. ``label`` is escaped but NOT
    secret-redacted: it is the ``--event`` / ``--matcher`` selector the caller
    just typed, and a redacted echo of their own argument would tell them
    nothing about which hook was copied (#2477).
    """
    click.echo(
        f"Plan: copy hook [{scrub_text(plan.label)}]  "
        f"{redact_command_shape(plan.signature.command_shape)}"
    )
    click.echo(f"  from {scrub_text(str(plan.src_canonical_path))}")
    click.echo(f"  to   {scrub_text(str(plan.dst_project_root))} ({plan.dst_scope} tier)")
    for leg, path, state, reason, add_note in (
        (
            "canonical",
            plan.dst_canonical_path,
            plan.canonical_state,
            plan.canonical_reason,
            "add entry (durable definition)",
        ),
        (
            "tier     ",
            plan.dst_target_path,
            plan.target_state,
            plan.target_reason,
            "add stamped rule (live immediately)",
        ),
    ):
        glyph, color, note = _leg_glyph_note(
            state, reason, already_note="already present", add_note=add_note
        )
        click.secho(f"  {glyph}  {leg} {scrub_text(str(path))}  ({scrub_text(note)})", fg=color)


def _hook_copy_payload(plan: HookCopyPlan, status: str) -> dict[str, Any]:
    """Base ``--json`` payload for one settings-copy plan."""
    return {
        "status": status,
        "applied": False,
        "event": plan.signature.event,
        "matcher": plan.signature.matcher,
        "command_preview": plan.signature.command_shape,
        "src_project": str(plan.src_project_root),
        "dst_project": str(plan.dst_project_root),
        "dst_scope": plan.dst_scope,
        "src_canonical": str(plan.src_canonical_path),
        "dst_canonical": str(plan.dst_canonical_path),
        "dst_target": str(plan.dst_target_path),
        "canonical": {"state": plan.canonical_state, "reason": plan.canonical_reason},
        "target": {"state": plan.target_state, "reason": plan.target_reason},
    }


def _print_hook_copy_result(result: HookCopyResult) -> None:
    """Human-readable apply outcome for one settings-copy.

    ``sync_command`` is deliberately left as written: it is a copy-pasteable
    command, and escaping it would produce something that no longer runs.
    Display-safe rendering of a *command* needs shell-correct quoting rather
    than :func:`scrub_text` — see ``_runtime_paths._hint_quote`` — which is a
    separate change from this one.
    """
    plan = result.plan
    if result.canonical_written:
        click.secho(
            f"  ✓ wrote canonical entry to {scrub_text(str(plan.dst_canonical_path))}",
            fg="green",
        )
    elif result.canonical_already:
        click.echo(f"  · canonical already carries [{scrub_text(plan.label)}] — no change")
    if result.target_written:
        click.secho(
            f"  ✓ wrote stamped rule to {scrub_text(str(plan.dst_target_path))}", fg="green"
        )
    elif result.target_already:
        click.echo(
            f"  · {plan.dst_scope} tier already carries [{scrub_text(plan.label)}] — no change"
        )
    for warning in result.warnings:
        click.secho(f"  ⚠ {scrub_text(warning)}", fg="yellow", err=True)
    if result.canonical_written or result.target_written:
        click.echo(
            f"\nNext: run `{result.sync_command}` to fan the entry out to the "
            f"destination's other runtimes (Codex/Gemini/Kimi)."
        )


@click.command("settings-copy")
@click.option("--event", required=True, help="Hook event name (e.g. PostToolUse).")
@click.option(
    "--matcher",
    default="",
    help="Hook matcher under the event; omit for matcher-less events (e.g. SessionStart).",
)
@click.option(
    "--hook-command",
    "hook_command",
    default=None,
    metavar="SUBSTRING",
    help=(
        "Disambiguator when several entries share (event, matcher): "
        "substring matched against the normalized command."
    ),
)
@click.option(
    "--to-project",
    "to_project",
    required=True,
    metavar="SCOPE_ID|PATH",
    help=(
        "Destination project — a p-<sha12> scope_id from `mm context projects "
        "list`, or a filesystem path (typing a path is consent to target an "
        "unregistered project)."
    ),
)
@click.option(
    "--to",
    "to_scope",
    type=click.Choice(list(get_args(TargetScope))),
    default=None,
    help=(
        "Destination Claude settings tier for the immediate fan-out. "
        "Omitted: the resolved hooks.target_scope (the tier push writes)."
    ),
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    default=False,
    help="Execute the copy (default is a dry-run preview).",
)
@click.option(
    "--yes",
    "-y",
    is_flag=True,
    default=False,
    help="Skip the confirmation and host-write prompts. Requires --apply.",
)
@click.option(
    "--confirm-project-shared",
    "confirm_project_shared",
    is_flag=True,
    default=False,
    help=(
        "Required (in addition to --apply) when the copy writes the "
        "destination's git-tracked files — the canonical "
        ".memtomem/settings.json always is one. --yes alone does not "
        "satisfy this opt-in (mirrors `mm context copy`)."
    ),
)
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    default=False,
    help="Emit a structured JSON result instead of human-readable output.",
)
def settings_copy_cmd(
    event: str,
    matcher: str,
    hook_command: str | None,
    to_project: str,
    to_scope: str | None,
    apply_: bool,
    yes: bool,
    confirm_project_shared: bool,
    json_out: bool,
) -> None:
    """Copy one canonical-matched hook entry into another project.

    Cross-project sibling of ``settings-migrate`` (which moves entries
    between tiers of ONE project). The copy is durable by construction:
    it writes the destination's canonical ``.memtomem/settings.json``
    (so the destination's own syncs maintain the rule) AND the
    destination-tier Claude settings file (ADR-0019-stamped, live
    immediately). Other runtimes (Codex/Gemini/Kimi) pick the entry up
    on the destination's next ``mm context sync --include=settings`` —
    the exact command is printed.

    Copy-only in v1; the source project is never modified. Identity is
    the canonical hook signature; ``already at target`` re-runs are
    no-ops and a conflicting same-matcher rule at the destination is
    skipped with a report naming the colliding entry (never duplicated).
    A privacy scan (Gate A) always runs — the destination canonical is
    git-tracked for every tier; there is no force valve.

    Exit codes: ``0`` clean (or dry-run), ``1`` declined confirmation,
    conflicts, or apply-time drift.
    """
    if yes and not apply_:
        raise click.UsageError("--yes is only valid with --apply")

    src_root = _ctx._find_project_root()

    cfg = _ctx._projects_gateway_cfg()
    try:
        dst_root, dst_scope_rec = _ctx.resolve_project_selector(
            to_project, _ctx._projects_discover(cfg)
        )
    except UnknownProjectSelectorError as exc:
        raise click.ClickException(str(exc)) from exc
    if dst_scope_rec is not None and not dst_scope_rec.enabled:
        # Same refusal as `mm context copy` — a paused destination would
        # hold a canonical entry that never fans out there.
        raise _ctx._paused_destination_error(dst_scope_rec.scope_id, dst_root, "copied hook")
    # Unconditional (unlike artifact transfer's user-tier exemption): the
    # canonical leg lands in the destination PROJECT for every tier.
    if not (dst_root / ".memtomem").is_dir():
        raise _ctx._missing_store_error(dst_root)

    dst_scope = to_scope if to_scope is not None else _ctx._resolve_cli_scope(None)

    try:
        plan = _ctx.plan_hook_copy(
            src_root,
            event=event,
            matcher=matcher,
            hook_command=hook_command,
            dst_project_root=dst_root,
            dst_scope=dst_scope,
        )
    except ValueError as exc:
        # HookNotFoundError / AmbiguousHookSelectorError / same-project /
        # unknown tier — all ValueError subclasses with CLI-ready messages.
        # The not-found and ambiguous messages quote canonical hook labels and
        # the canonical path, i.e. settings text, so escape before printing
        # (#2477).
        raise click.ClickException(scrub_text(str(exc))) from exc

    # Pending writes drive the gates (the #1263 contract: no-op requests
    # never prompt). The plan properties encode the cross-leg rule (a
    # canonical conflict blocks both legs) so the web route's envelopes
    # and these prompts cannot drift on what counts as pending.
    git_tracked_write = plan.pending_canonical_write or (
        plan.pending_target_write and dst_scope == "project_shared"
    )
    host_write = plan.pending_target_write and dst_scope == "user"

    if json_out:
        payload = _hook_copy_payload(
            plan,
            "noop" if plan.is_noop else ("conflicts" if plan.has_conflict else "ok"),
        )
    else:
        _ctx._print_hook_copy_plan(plan)

    if not apply_:
        if json_out:
            click.echo(json.dumps(payload, indent=2))
        else:
            confirm_note = " --confirm-project-shared" if git_tracked_write else ""
            click.echo(f"\nRun with --apply{confirm_note} to execute.")
        return

    if plan.is_noop:
        # Nothing to write — never prompt for a no-op (#1263 contract).
        if json_out:
            payload["applied"] = True
            click.echo(json.dumps(payload, indent=2))
        else:
            click.echo("  (nothing to do — the entry is already at the destination)")
        return

    # Gate B — pending write into the destination's git-tracked files
    # (the canonical is one for EVERY tier; the project_shared tier file
    # adds a second). Wording parity with `mm context copy`.
    if git_tracked_write and not confirm_project_shared:
        if yes:
            raise click.ClickException(
                "copying into the destination's git-tracked files (canonical "
                ".memtomem/settings.json) requires --confirm-project-shared. "
                "--yes alone is not sufficient: project-shared writes require "
                "explicit opt-in."
            )
        if json_out:
            payload["status"] = "needs_confirmation"
            payload["hint"] = "Re-run with --confirm-project-shared after confirming."
            click.echo(json.dumps(payload, indent=2))
            raise click.exceptions.Exit(1)
        if not click.confirm(
            f"\nThis will copy the hook into {scrub_text(str(plan.dst_project_root))}'s "
            f"git-tracked canonical settings (.memtomem/settings.json)"
            + (" and its project_shared tier file" if dst_scope == "project_shared" else "")
            + ". Continue?",
            default=False,
        ):
            raise click.Abort()

    # ADR-0011 §5 Gate B consent (#2306). Mirrors the gate's predicate
    # exactly — ``git_tracked_write`` fires for a user/project_local
    # destination too, because the canonical settings file is git-tracked
    # in every tier. After the block, not inside it; dry runs and no-ops
    # returned above, and the host-write prompt below can still abort.
    if git_tracked_write:
        privacy.emit_project_shared_confirmation(
            surface="cli_context_settings_copy",
            mechanism="flag" if confirm_project_shared else "prompt",
            action="copy",
            audit_context={"event": event, "dst_scope": dst_scope},
        )

    # Host-write prompt — the user tier file lives outside any project
    # root (mirrors settings-migrate; satisfied by --yes).
    if host_write and not yes:
        if json_out:
            payload["status"] = "needs_confirmation"
            payload["host_writes"] = [str(plan.dst_target_path)]
            payload["hint"] = "Re-run with --yes after confirming."
            click.echo(json.dumps(payload, indent=2))
            raise click.exceptions.Exit(1)
        click.secho(
            "settings-copy will modify the following file outside the destination project:",
            fg="yellow",
        )
        click.echo(f"  {scrub_text(str(plan.dst_target_path))}  (user tier)")
        if not click.confirm("Continue?", default=False):
            click.echo("Aborted.")
            raise click.exceptions.Exit(1)

    try:
        result = _ctx.apply_hook_copy(plan, surface="cli_context_settings_copy")
    except PrivacyScanError as exc:
        # The Gate A message names the source and destination canonical paths.
        raise click.ClickException(scrub_text(exc.message)) from exc

    if json_out:
        payload["applied"] = True
        payload["canonical"]["written"] = result.canonical_written
        payload["canonical"]["already"] = result.canonical_already
        payload["target"]["written"] = result.target_written
        payload["target"]["already"] = result.target_already
        payload["needs_sync"] = result.needs_sync
        payload["sync_command"] = result.sync_command
        if result.warnings:
            payload["warnings"] = result.warnings
            payload["status"] = "conflicts"
        click.echo(json.dumps(payload, indent=2))
    else:
        _ctx._print_hook_copy_result(result)

    if result.warnings:
        # Conflicts or apply-time drift left unresolved — match the
        # settings-migrate exit-1 contract.
        raise click.exceptions.Exit(1)


def _is_within(path: Path, project_root: Path) -> bool:
    """``True`` when *path* resolves under *project_root*. Symlink-safe."""
    try:
        return path.resolve(strict=False).is_relative_to(project_root.resolve(strict=False))
    except (OSError, RuntimeError):
        return False
