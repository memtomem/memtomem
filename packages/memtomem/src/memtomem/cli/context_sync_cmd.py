"""memtomem context sync — push context.md + included artifacts to runtimes."""

from __future__ import annotations

from pathlib import Path

import click

from memtomem.cli._context_facade import context_cmd as _ctx
from memtomem.cli.context_options import _LABEL_OPTION, _SCOPE_OPTION, _SYNC_INCLUDE_OPTION
from memtomem.context._runtime_targets import KNOWN_RUNTIMES
from memtomem.context.agents import ON_DROP_LEVELS
from memtomem.context.parser import CONTEXT_FILENAME
from memtomem.context.projects import ProjectScope


@click.command("sync")
@_SYNC_INCLUDE_OPTION
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
@click.option(
    "--all-projects",
    "all_projects",
    is_flag=True,
    help=(
        "Push every eligible discovered project (enrolled + on disk), not just "
        "the current one. project_shared tier only; ineligible projects are "
        "reported and skipped. One project's failure does not abort the batch."
    ),
)
@_SCOPE_OPTION
@_LABEL_OPTION
@click.option(
    "--force-unsafe",
    "force_unsafe",
    is_flag=True,
    default=False,
    help=(
        "Bypass Gate A on canonical files being fanned out — for a reviewed "
        "false positive (e.g. an 'api_key: str' type annotation). user / "
        "project_local destinations only; project_shared hard-refuses "
        "regardless (ADR-0011 §5). Mirrors 'mm context init "
        "--force-unsafe-import'."
    ),
)
@click.option(
    "--runtime",
    "runtimes",
    multiple=True,
    type=click.Choice(list(KNOWN_RUNTIMES)),
    help=(
        "Restrict artifact fan-out to these runtimes (repeatable; default: all "
        "detected). Applies to the skills/agents/commands legs only — "
        "settings and mcp-servers have no per-runtime targeting. Additive: "
        "omitting it keeps the default (all runtimes)."
    ),
)
def sync_cmd(
    include: tuple[str, ...],
    strict: bool,
    on_drop: str,
    yes: bool,
    all_projects: bool,
    scope_flag: str | None,
    label: str | None,
    force_unsafe: bool,
    runtimes: tuple[str, ...],
) -> None:
    """Push context.md + included artifacts to detected agent files.

    Regenerates the detected project-memory files (CLAUDE.md / GEMINI.md /
    .cursorrules / …) from ``.memtomem/context.md``. For every kind passed via
    ``--include`` it also fans out the canonical artifacts: skills, agents,
    commands, settings, and (opt-in) mcp-servers. ``--all-projects`` pushes
    every enrolled/discovered project (project_shared tier only); ``--scope`` /
    ``--label`` pick the artifact tier; ``--force-unsafe`` bypasses Gate A on a
    reviewed false positive (user / project_local destinations only);
    ``--runtime`` restricts the artifact fan-out to specific runtimes.
    """
    # sync accepts mcp-servers on top of the shared kinds (#1311); the other
    # context commands keep the default _KNOWN_INCLUDES set.
    inc = _ctx._parse_include(include, allowed=_ctx._SYNC_INCLUDES)

    # --runtime filters the artifact fan-out (skills/agents/commands) only.
    runtime_filter = tuple(dict.fromkeys(runtimes)) or None
    if runtime_filter is not None:
        non_artifact = inc & {"settings", "mcp-servers"}
        if non_artifact:
            raise click.UsageError(
                "--runtime filters artifact fan-out (skills/agents/commands); "
                f"{', '.join(sorted(non_artifact))} has no per-runtime targeting — "
                "push those separately without --runtime."
            )
        if not inc & {"skills", "agents", "commands"}:
            raise click.UsageError(
                "--runtime needs an artifact kind in --include "
                "(skills, agents, or commands) to filter."
            )

    if all_projects:
        # ADR-0025: the batch is project_shared-only — `user` would fan the
        # SAME host files out once per project and `project_local` has no
        # runtime fan-out (ADR-0011 §3), mirroring the web sync-all gates.
        if scope_flag not in (None, "project_shared"):
            raise click.UsageError(
                "--all-projects syncs the project_shared tier only; drop "
                "--scope or pass --scope project_shared (push other tiers "
                "per project, without --all-projects)."
            )
        # The batch is project_shared, where Gate A hard-refuses a forced
        # fan-out regardless of the flag (ADR-0011 §5) — reject the combo
        # rather than silently ignore it (sibling of the --scope guard above).
        if force_unsafe:
            raise click.UsageError(
                "--force-unsafe does not apply to --all-projects (project_shared "
                "tier only — Gate A hard-refuses a forced fan-out there). "
                "Force-push a user / project_local tier per project, without "
                "--all-projects."
            )
        _ctx._warn_label_ineligible_kinds(label, inc)
        _ctx._run_sync_all_projects(
            inc=inc,
            strict=strict,
            on_drop=on_drop,
            yes=yes,
            label=label,
            runtime_filter=runtime_filter,
        )
        return

    root = _ctx._find_project_root()
    if not _ctx._run_sync_legs(
        root,
        inc=inc,
        strict=strict,
        on_drop=on_drop,
        yes=yes,
        scope_flag=scope_flag,
        label=label,
        force_unsafe=force_unsafe,
        runtime_filter=runtime_filter,
    ):
        # _run_sync_legs returns False only on the single-project
        # missing-context.md refusal (red note already printed, no legs ran).
        # Exit non-zero so a script / CI wrapping `mm context sync` can detect
        # it — this used to just skip `Synced.` and still exit 0. Mirrors
        # generate_cmd; batch mode never reaches the False leg.
        raise SystemExit(1)
    click.secho("Pushed.", fg="green")


def _run_sync_legs(
    root: Path,
    *,
    inc: set[str],
    strict: bool,
    on_drop: str,
    yes: bool,
    scope_flag: str | None,
    label: str | None,
    batch: bool = False,
    surface: str = "cli_context_sync",
    force_unsafe: bool = False,
    runtime_filter: tuple[str, ...] | None = None,
) -> bool:
    """Run one project's sync legs (project memory + included artifact kinds).

    Returns False only on the single-project missing-``context.md`` early
    refusal (no legs ran — the caller must not print ``Synced.``, the
    historical behavior Codex impl review caught a regression on, and now
    exits non-zero so a script/CI can detect the refusal); True on every
    path that ran the legs.

    The extracted body of ``mm context sync`` — the single-project path
    calls it once with ``batch=False`` and keeps its historical behavior
    byte-identical. ``--all-projects`` (ADR-0025) calls it per project
    with ``batch=True``, which changes exactly two things:

    - a missing ``context.md`` is a yellow note (a registered project
      without project memory is not a batch failure; the single-project
      red "run init first" guidance stays single-only), and
    - ``_warn_label_ineligible_kinds`` is suppressed — the batch
      orchestrator prints it once, not once per project.

    The batch caller passes ``scope_flag="project_shared"`` explicitly:
    the settings leg resolves its scope via ``_resolve_cli_scope``, which
    otherwise consults ``cfg.hooks.target_scope`` — a config-pinned
    ``user`` value would fan the same host files out once per project
    (the ADR-0025 tier pin; covered by the batch settings-scope test).
    """
    ctx_path = _ctx._context_path(root)

    if ctx_path.exists():
        sections = _ctx.parse_context(ctx_path)
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
            files = _ctx.detect_agent_files(root)

            if files:
                agents_to_sync = {f.agent for f in files}

                for agent_name in sorted(agents_to_sync):
                    gen = _ctx.GENERATORS.get(agent_name)
                    if not gen:
                        continue

                    content = gen.generate(sections)
                    out_path = root / gen.output_path
                    # Atomic write (#1247 id 19): crash mid-write must not
                    # truncate the user's CLAUDE.md / GEMINI.md / etc.
                    _ctx.atomic_write_text(out_path, content, 0o644)
                    click.echo(f"  {agent_name:10s}  {gen.output_path}")
            else:
                click.echo(
                    "No agent files detected. Use 'mm context generate --agent all' to create them."
                )
    elif not inc and not batch:
        click.secho(f"{CONTEXT_FILENAME} not found. Run 'mm context init' first.", fg="red")
        return False
    else:
        click.secho(f"  ({CONTEXT_FILENAME} missing — skipping project memory)", fg="yellow")

    # ADR-0011 PR-E3: resolve the canonical-artifact scope once and thread
    # it through the three include helpers. Defaults to ``project_shared``
    # via ``_resolve_artifact_cli_scope`` (NOT ``_resolve_cli_scope``,
    # which leaks ``cfg.hooks.target_scope`` into the artifact axis —
    # see ADR-0011 PR-E1).
    artifact_scope = _ctx._resolve_artifact_cli_scope(scope_flag)
    if not batch:
        _ctx._warn_label_ineligible_kinds(label, inc)

    for spec in _ctx._ARTIFACT_PRINT_SPECS:
        if spec.kind in inc:
            click.echo("")
            _ctx._print_artifact_generate(
                spec,
                root,
                strict=strict,
                on_drop=on_drop,
                scope=artifact_scope,
                label=label,
                surface=surface,
                force_unsafe=force_unsafe,
                runtimes_filter=runtime_filter,
            )

    if "settings" in inc:
        click.echo("")
        # Settings has its own (separate from artifact) scope axis — see
        # ADR-0010. ``_resolve_cli_scope`` consults ``cfg.hooks.target_scope``;
        # this is intentional for settings and intentionally NOT used for
        # artifacts above.
        scope = _ctx._resolve_cli_scope(scope_flag)
        if _ctx._confirm_settings_host_writes(root, scope=scope, yes=yes):
            _ctx._print_settings_generate(root, scope=scope, allow_host_writes=True)
        else:
            click.secho("  Skipped settings push (declined).", fg="yellow")

    if "mcp-servers" in inc:
        click.echo("")
        if artifact_scope != "project_shared":
            # mcp-servers canonicals are single-tier project_shared (ADR-0016
            # §3): there is no other tier to honor, so --scope is ignored — not
            # an error, because a mixed --include can still carry a real scope
            # for the other kinds (mirrors _warn_label_ineligible_kinds).
            click.secho(
                f"  note: --scope={artifact_scope} does not apply to mcp-servers "
                "(single-tier project_shared); fanning out project_shared.",
                fg="yellow",
            )
        # Single-project runs treat the explicit --include as consent; a batch
        # (--all-projects) confirms per target first (see the helper).
        if not batch or _ctx._confirm_mcp_servers_fanout(root, yes=yes):
            _ctx._print_mcp_servers_generate(root, surface=surface)
        else:
            click.secho("  Skipped mcp-servers push (declined).", fg="yellow")

    return True


#: CLI remediation per ``sync_skip_reason`` code (the code derivation is
#: shared with the web batch route — ``context.projects.sync_skip_reason``).
_CLI_SYNC_SKIP_REASONS: dict[str, str] = {
    "missing_root": "missing — root no longer exists; re-register or `mm context projects remove`",
    "sync_paused": "paused — `mm context projects resume <scope_id>` to include it",
    "sync_not_enrolled": "not enrolled — `mm context projects add <path>` to include it",
    "stale_project": "stale — no .memtomem/ store; run `mm context init` there first",
}


def _run_sync_all_projects(
    *,
    inc: set[str],
    strict: bool,
    on_drop: str,
    yes: bool,
    label: str | None,
    runtime_filter: tuple[str, ...] | None = None,
) -> None:
    """Orchestrate ``mm context sync --all-projects`` (ADR-0025, #1279).

    Cloned from the ``_run_update_all`` flow: discover → classify
    (eligibility/health — NOT a dirty-diff preview) → preview table →
    confirm → serial execute with per-project failure isolation →
    summary, exit 1 if any project failed. Zero eligible projects exits
    0 informationally (cron safety, the update-all precedent).

    Discovery anchors at ``_find_project_root()`` — not raw ``Path.cwd()``
    like ``mm context projects list`` — so running from a project
    SUBDIRECTORY treats that project as the cwd scope, matching both
    single-sync semantics (which walks up) and the web lifespan anchor.
    """
    cfg = _ctx._projects_gateway_cfg()
    scopes = _ctx._projects_discover(cfg, cwd=_ctx._find_project_root())
    rows: list[tuple[ProjectScope, str | None]] = [(s, _ctx.sync_skip_reason(s)) for s in scopes]
    runnable = [s for s, code in rows if code is None]

    click.echo(f"{len(rows)} project scope(s) discovered:")
    for s, code in rows:
        if code is None:
            click.secho(f"  push  {s.scope_id}  {s.label}  ({s.root})", fg="green")
        else:
            click.secho(
                f"  skip  {s.scope_id}  {s.label}  ({s.root})  [{_CLI_SYNC_SKIP_REASONS[code]}]",
                fg="yellow",
            )

    if not runnable:
        click.echo("No projects are eligible for push; nothing to do.")
        return

    if not yes:
        click.confirm(f"\nPush {len(runnable)} project(s)?", abort=True)

    successes = 0
    failures = 0
    for s in runnable:
        assert s.root is not None  # sync_skip_reason() filtered missing roots
        click.secho(f"\n→ {s.label} ({s.root})", fg="cyan")
        try:
            _ctx._run_sync_legs(
                s.root,
                inc=inc,
                strict=strict,
                on_drop=on_drop,
                yes=yes,
                # Explicit tier pin — see _run_sync_legs docstring (ADR-0025).
                scope_flag="project_shared",
                label=label,
                batch=True,
                surface="cli_context_sync_all_projects",
                runtime_filter=runtime_filter,
            )
        except click.exceptions.Abort:
            # Strict-drop legs raise Abort after printing their [strict]
            # diagnostic; in a batch that fails the project, not the run.
            click.secho(f"  ✗ {s.root}: aborted (strict-drop)", fg="red")
            failures += 1
        except click.ClickException as exc:
            # Privacy Gate A and engine refusals — this project's row fails,
            # the batch continues (a secret in one project must not block
            # clean siblings; the _run_update_all precedent).
            click.secho(f"  ✗ {s.root}: {exc.message}", fg="red")
            failures += 1
        except OSError as exc:
            click.secho(f"  ✗ {s.root}: {exc}", fg="red")
            failures += 1
        else:
            successes += 1

    click.echo(
        f"\nSummary: {successes} pushed, {failures} failed, {len(rows) - len(runnable)} skipped."
    )
    if failures:
        raise click.exceptions.Exit(1)
