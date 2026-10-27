"""memtomem context — unified agent context management."""

from __future__ import annotations

import difflib
import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import click

from memtomem import privacy
from memtomem.context import versioning
from memtomem.context._canonical_txn import versioning_op_locked
from memtomem.context._atomic import atomic_write_text
from memtomem.context.agents import (
    AGENT_DIR_FILENAME,
    ON_DROP_LEVELS,
    StrictDropError,
    canonical_agent_name,
    diff_agents,
    extract_agents_to_canonical,
    generate_all_agents,
)
from memtomem.context.commands import (
    COMMAND_DIR_FILENAME,
    StrictDropError as CommandStrictDropError,
    diff_commands,
    extract_commands_to_canonical,
    generate_all_commands,
)
from memtomem.context._names import InvalidNameError, validate_name
from memtomem.context.detector import (
    detect_agent_dirs,
    detect_agent_files,
    detect_command_dirs,
    detect_settings_files,
    detect_skill_dirs,
)
from memtomem.context.install import (  # noqa: F401
    AdoptMismatchError,
    AlreadyInstalledError,
    AssetNotFoundError,
    CommitNotFoundError,
    NotInstalledError,
    PinNotAncestorError,
    ProjectClassification,
    ProjectInstallClassification,
    StaleInstallError,
    UncommittedAssetError,
    _apply_pinned_install,
    _apply_update,
    _asset_dirty_rels,
    _classify_for_all_update,
    _classify_for_install_all,
    _commit_hint,
    _has_recorded_pin,
    _pin_backward_reason,
    adopt_agent,
    adopt_command,
    adopt_skill,
    install_agent,
    install_command,
    install_skill,
    update_agent,
    update_command,
    update_skill,
)
from memtomem.context.migrate import (  # noqa: F401
    MigratePartialError,
    MigrateRow,
    MigrateScopeResult,
    SCOPE_MIGRATABLE_KINDS,
    StagingIdentityLostError,
    TransferStagingBusyError,
    _detect_source_scope,
    adopt_flat_to_dir,
    classify_migrate,
    migrate_one,
    migrate_scope,
)
from memtomem.context._dir_swap import SwapRecoveryError, swap_failure_text
from memtomem.context.bundle import (  # noqa: F401
    BundleFormatError,
    BundlePrivacyError,
    BundleSourceError,
    export_artifact_bundle,
    receive_artifact_bundle,
)
from memtomem.context.transfer import TransferMode, TransferResult, transfer_artifact  # noqa: F401
from memtomem.context.mcp_servers import (
    PROJECT_MCP_CONFIG,
    McpServerParseError,
    McpServerPrivacyError,
    generate_all_mcp_servers,
)
from memtomem.context.mcp_servers_copy import McpServerCopyResult, copy_mcp_server  # noqa: F401
from memtomem.context.projects import (  # noqa: F401
    KnownProjectsCorruptError,
    KnownProjectsStore,
    ProjectScope,
    UnknownProjectSelectorError,
    annotate_project_health,
    compute_scope_id,
    discover_project_scopes,
    resolve_project_selector,
    sync_skip_reason,
)
from memtomem.context.lockfile import Lockfile, LockfileError  # noqa: F401
from memtomem.context.status import (  # noqa: F401
    DRIFT_STATES,
    ProjectStatus,
    classify_status,
    collect_project_status,
    iter_kind_drift_counts,
    load_with_recovery,
    scan_user_artifacts,
)
from memtomem.context.generator import (
    GENERATORS,
    extract_sections_from_agent_file,
    preamble_source,
)
from memtomem.context.parser import CONTEXT_FILENAME, parse_context, sections_to_markdown
from memtomem.context.privacy_scan import (
    PrivacyScanError,
    raise_or_collect,
    scan_artifact_tree,
    scan_text_content,
)
from memtomem.context.settings import (
    diff_settings,
    generate_all_settings,
    host_write_targets,
)
from memtomem._runtime_paths import _hint_quote, scrub_text
from memtomem.context.error_redact import redact_secret_value  # noqa: F401
from memtomem.context.settings_doctor import (  # noqa: F401
    detect_duplicate_tiers,
    find_malformed_matchers,
    find_unportable_hook_commands,
    find_unscanned_settings_files,
    format_malformed_warning,
    format_signature_label,
    format_unportable_command_warning,
    format_unscanned_settings_warning,
    format_warning,
    redact_command_shape,
    redact_unportable_command_fields,
)
from memtomem.context.settings_copy import (  # noqa: F401
    HookCopyPlan,
    HookCopyResult,
    apply_hook_copy,
    plan_hook_copy,
)
from memtomem.context.settings_migrate import (  # noqa: F401
    apply_migration,
    format_plan_summary,
    plan_migration,
)
from memtomem.context.scope_resolver import (
    ArtifactKind,
    canonical_artifact_dir,
    find_project_root,
)
from memtomem.context.skills import (
    SKILL_MANIFEST,
    diff_skills,
    extract_skills_to_canonical,
    generate_all_skills,
)
from memtomem.context.pull_apply import (
    PullApplyResult,
    PullPlan,
    commit_pull,
    prepare_pull,
)
from memtomem.context.pull_preview import PullPreview, preview_pull
from memtomem.context._runtime_targets import (  # noqa: F401
    KNOWN_RUNTIMES,
    resolve_import_runtimes,
)
from memtomem.context import _skip_reasons as skip_codes
from memtomem.context import remediation
from memtomem.wiki.store import WikiNotFoundError, WikiStore, WikiUnbornHeadError  # noqa: F401
from typing import Any, Literal, cast, get_args

from memtomem.cli.context_options import (  # noqa: F401
    _LABEL_OPTION,
    _SCOPE_OPTION,
    _SYNC_INCLUDE_OPTION,
)
from memtomem.config import (
    ContextGatewayConfig,
    Mem2MemConfig,
    TargetScope,
    load_config_d,
    load_config_overrides,
)

# Phase 1-3 supports skills/agents/commands; Phase D adds settings. This is
# the SHARED include contract for detect/init/generate/diff/sync (mirrored in
# server/tools/context.py); keep the two frozensets in lockstep.
_KNOWN_INCLUDES: frozenset[str] = frozenset({"skills", "agents", "commands", "settings"})

# ``mm context sync`` accepts one extra kind the other four commands do not:
# ``mcp-servers`` (#1311). It is opt-in (a bare ``mm context sync`` never
# touches ``.mcp.json``) and SYNC-ONLY — the engine has a canonical→``.mcp.json``
# fan-out (``generate_all_mcp_servers``) but no extract/init/diff, so adding it
# to the shared ``_KNOWN_INCLUDES`` would make detect/init/generate/diff accept
# a token they silently no-op. It is also CLI-only: MCP ``sync``-contract
# parity (and an ``all`` alias) is a deliberate scope-out (ADR-0021 §"Open
# questions" §5), so ``server/tools/context.py`` is intentionally NOT widened.
_SYNC_INCLUDES: frozenset[str] = _KNOWN_INCLUDES | {"mcp-servers"}


def _find_project_root() -> Path:
    """Walk up from cwd to find project root (has .git or pyproject.toml).

    Thin wrapper over the shared ``scope_resolver.find_project_root`` so the
    CLI, MCP tools, and web app share one definition of the project root.
    """
    return find_project_root()


def _context_path(root: Path) -> Path:
    return root / CONTEXT_FILENAME


def _parse_include(
    include_tuple: tuple[str, ...], *, allowed: frozenset[str] = _KNOWN_INCLUDES
) -> set[str]:
    """Normalize ``--include`` values (repeatable option + comma-split within each).

    ``allowed`` is the per-command accepted set: detect/init/generate/diff use
    the default ``_KNOWN_INCLUDES``; ``sync`` passes ``_SYNC_INCLUDES`` to also
    accept ``mcp-servers`` (#1311).
    """
    values: set[str] = set()
    for raw in include_tuple:
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if token not in allowed:
                raise click.BadParameter(
                    f"Unknown --include value '{token}'. Supported: {sorted(allowed)}"
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


# ── Sub-artifact print handlers (skills / agents / commands) ────────
#
# The three include-kinds share verb-identical CLI rendering (detect /
# init / generate / diff); the per-kind differences live in
# ``_ArtifactPrintSpec`` so each printer exists once (#1516). Stdout is
# pinned by the CLI tests — keep it byte-identical when editing here.


def _skip_color(code: skip_codes.SkipCode) -> str:
    """Privacy-refusal skips (Gate A hard refusals) render red; every
    other skip reason is informational yellow."""
    if code in (skip_codes.PRIVACY_BLOCKED, skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED):
        return "red"
    return "yellow"


@dataclass(frozen=True)
class _ArtifactPrintSpec:
    """Per-kind wiring for the shared print handlers below (#1516)."""

    kind: ArtifactKind  # --include key AND canonical dir name
    width: int  # runtime column width in detect/generate/diff rows
    detect_fn: Callable[[Path], Any]
    detect_empty: str  # "(no <detect_empty>)" when nothing is detected
    detect_noun: str  # "<N> <detect_noun>:" header over detected rows
    extract_fn: Callable[..., Any]
    import_noun: str  # "Imported <N> <import_noun> → <dest>"
    import_plural: str  # "(no runtime <import_plural> imported into <scope>)"
    render_imported: Callable[[Any], str]  # one ``result.imported`` element → display name
    generate_fn: Callable[..., Any]
    generate_label: str  # "<generate_label> fan-out: <N>"
    generate_takes_strict: bool  # generate_fn accepts strict/on_drop/label kwargs
    diff_fn: Callable[..., Any]
    diff_plural: str  # "(no <diff_plural> to compare in <scope>)"


_ARTIFACT_PRINT_SPECS: tuple[_ArtifactPrintSpec, ...] = (
    _ArtifactPrintSpec(
        kind="skills",
        width=15,
        detect_fn=detect_skill_dirs,
        detect_empty="no skill directories",
        detect_noun="skill(s)",
        extract_fn=extract_skills_to_canonical,
        import_noun="skill(s)",
        import_plural="skills",
        render_imported=lambda item: item.name,
        generate_fn=generate_all_skills,
        generate_label="Skills",
        generate_takes_strict=False,
        diff_fn=diff_skills,
        diff_plural="skills",
    ),
    _ArtifactPrintSpec(
        kind="agents",
        width=15,
        detect_fn=detect_agent_dirs,
        detect_empty="no sub-agent files",
        detect_noun="sub-agent file(s)",
        extract_fn=extract_agents_to_canonical,
        import_noun="sub-agent(s)",
        import_plural="sub-agents",
        render_imported=lambda item: canonical_agent_name(item[0], item[1]),
        generate_fn=generate_all_agents,
        generate_label="Sub-agent",
        generate_takes_strict=True,
        diff_fn=diff_agents,
        diff_plural="sub-agents",
    ),
    _ArtifactPrintSpec(
        kind="commands",
        width=17,
        detect_fn=detect_command_dirs,
        detect_empty="no slash command files",
        detect_noun="command file(s)",
        extract_fn=extract_commands_to_canonical,
        import_noun="command(s)",
        import_plural="commands",
        render_imported=lambda item: item[0].parent.name if item[1] == "dir" else item[0].stem,
        generate_fn=generate_all_commands,
        generate_label="Command",
        generate_takes_strict=True,
        diff_fn=diff_commands,
        diff_plural="commands",
    ),
)


def _print_artifact_detect(spec: _ArtifactPrintSpec, root: Path) -> None:
    items = spec.detect_fn(root)
    if not items:
        click.echo(f"  ({spec.detect_empty})")
        return
    click.secho(f"  {len(items)} {spec.detect_noun}:", fg="cyan")
    for item in items:
        rel = item.path.relative_to(root) if item.path.is_relative_to(root) else item.path
        click.echo(f"    {item.agent:{spec.width}s}  {rel}  ({item.size} bytes)")


def _print_artifact_init(
    spec: _ArtifactPrintSpec,
    root: Path,
    overwrite: bool,
    *,
    scope: TargetScope = "project_shared",
    force_unsafe_import: bool = False,
    only_name: str | None = None,
) -> Any:
    """Run one kind's runtime import and print the outcome; returns the
    engine result so ``init --only`` can detect the not-found case
    (empty ``imported`` + ``skipped`` — the engine silently skips
    non-matching stems)."""
    result = spec.extract_fn(
        root,
        overwrite=overwrite,
        only_name=only_name,
        scope=scope,
        force_unsafe_import=force_unsafe_import,
    )
    dest_label = (
        canonical_artifact_dir(spec.kind, scope, root)
        if scope != "project_local"
        else "(skipped — project_local has no runtime fan-out)"
    )
    if result.imported:
        click.secho(
            f"  Imported {len(result.imported)} {spec.import_noun} → {dest_label}", fg="green"
        )
        for item in result.imported:
            click.echo(f"    {spec.render_imported(item)}")
    else:
        click.echo(f"  (no runtime {spec.import_plural} imported into {scope})")
    for name, reason, code in result.skipped:
        # The engine states the condition; the CLI adds the CLI's own spelling
        # of the remediation (#1869). Codes with no CLI action render unchanged.
        shown = remediation.append_hint(reason, code, "cli")
        click.secho(f"    skipped {name}: {shown}", fg=_skip_color(code))
    return result


def _print_artifact_generate(
    spec: _ArtifactPrintSpec,
    root: Path,
    *,
    scope: TargetScope = "project_shared",
    strict: bool = False,
    on_drop: str = "ignore",
    label: str | None = None,
    surface: str = "cli_context_sync",
    force_unsafe: bool = False,
    runtimes_filter: tuple[str, ...] | None = None,
) -> None:
    kwargs: dict[str, Any] = {"scope": scope, "surface": surface, "force_unsafe": force_unsafe}
    if spec.generate_takes_strict:
        kwargs.update(strict=strict, on_drop=on_drop, label=label)
    if runtimes_filter is not None:
        # Bare runtime names → the engine's generator-name targets
        # (``f"{runtime}_{kind}"`` — the registry convention). An unregistered
        # combo (e.g. codex commands) surfaces as a typed UNKNOWN_RUNTIME skip
        # row from the engine, not a silent no-op (ADR-0030 §5, PR-C).
        kwargs["runtimes"] = [f"{r}_{spec.kind}" for r in runtimes_filter]
    try:
        result = spec.generate_fn(root, **kwargs)
    except (StrictDropError, CommandStrictDropError) as exc:
        click.secho(f"  [strict] {exc}", fg="red")
        raise click.Abort() from exc
    except PrivacyScanError as exc:
        raise click.ClickException(exc.message) from exc

    if result.generated:
        click.secho(f"  {spec.generate_label} fan-out: {len(result.generated)}", fg="green")
        for runtime, path in result.generated:
            try:
                rel = path.relative_to(root) if path.is_relative_to(root) else path
            except ValueError:
                rel = path
            click.echo(f"    {runtime:{spec.width}s}  {rel}")
    for runtime, reason, code in result.skipped:
        click.secho(f"  skipped {runtime}: {reason}", fg=_skip_color(code))
    # Skills results carry no ``dropped`` (field-drop is an atomic-sync
    # concept — agents/commands only).
    for runtime, item_name, dropped in getattr(result, "dropped", ()):
        click.secho(
            f"  {runtime} dropped {dropped} from '{item_name}'",
            fg="yellow",
        )


def _print_artifact_diff(
    spec: _ArtifactPrintSpec, root: Path, *, scope: TargetScope = "project_shared"
) -> None:
    rows = spec.diff_fn(root, scope=scope)
    if not rows:
        click.echo(f"  (no {spec.diff_plural} to compare in {scope})")
        return
    for row in rows:
        runtime, name, status = row
        color = "green" if status == "in sync" else "yellow"
        # Diagnostic reason (parse error / invalid name rows, #1229 U7) —
        # raw engine text is fine for the local operator.
        reason = getattr(row, "reason", None)
        suffix = f"  — {reason}" if reason else ""
        click.secho(
            f"  {runtime:{spec.width}s}  {name}  [{status}]  scope={scope}{suffix}", fg=color
        )


# ── Pull (ADR-0030 PR-C) rendering ─────────────────────────────────────────


def _print_pull_preview(
    preview: PullPreview, *, overwrite: bool, only_runtime: str | None = None
) -> None:
    """Human table + summary for ``mm context pull`` (dry-run default).

    ``only_runtime`` (from ``--from``) narrows the table + summary to the named
    source, so ``pull … --from gemini`` previews exactly what a ``--apply``
    would pull rather than every candidate.
    """
    rows = [c for c in preview.candidates if only_runtime is None or c.runtime == only_runtime]
    if not rows:
        who = f"runtime '{only_runtime}'" if only_runtime else "any runtime"
        click.echo(f"  ({who} has no {preview.kind}/{preview.name} to pull)")
    else:
        click.secho(f"  {'runtime':10s}  {'content':14s}  {'gate':28s}  notes", fg="cyan")
        for c in rows:
            notes: list[str] = []
            if c.override_warning:
                notes.append("matches vendor override — pull would bake it into base")
            if c.reason:
                notes.append(c.reason)
            if c.landing_group is not None and preview.distinct_landing_count > 1:
                notes.append(f"content #{c.landing_group + 1}")
            gate = c.gate_status or "-"
            click.echo(f"  {c.runtime:10s}  {c.content_status:14s}  {gate:28s}  {'; '.join(notes)}")
    store = "present" if preview.store_present else "absent"
    if only_runtime is not None:
        summary = f"store: {store} · source: {only_runtime}"
    elif preview.ambiguous:
        summary = (
            f"store: {store} · distinct contents: {preview.distinct_landing_count} · "
            "ambiguous — pass --from <runtime> to --apply"
        )
    elif preview.auto_source:
        summary = f"store: {store} · auto-source: {preview.auto_source}"
    else:
        summary = f"store: {store} · nothing pullable"
    click.echo(f"  {summary}")
    if preview.store_present and not overwrite:
        click.secho("  note: --apply needs --overwrite (the Store already has it).", fg="yellow")


def _pull_preview_json(preview: PullPreview) -> dict[str, Any]:
    """The wire-parallel preview shape (no raw bytes — CLI JSON is local)."""
    return {
        "kind": preview.kind,
        "name": preview.name,
        "target_scope": preview.scope,
        "store_present": preview.store_present,
        "distinct_landing_count": preview.distinct_landing_count,
        "ambiguous": preview.ambiguous,
        "auto_source": preview.auto_source,
        "candidates": [
            {
                "runtime": c.runtime,
                "content_status": c.content_status,
                "gate_status": c.gate_status,
                "importable": c.importable,
                "landing_group": c.landing_group,
                "override_warning": c.override_warning,
                "reason": c.reason,
            }
            for c in preview.candidates
        ],
    }


def _print_pull_diff(preview: PullPreview, *, only_runtime: str | None = None) -> None:
    """Unified diff of each candidate's Pull PAYLOAD vs the Store payload.

    Both sides are the payload surface — the bytes a Pull actually lands after
    the §10 strip — so the diff shows exactly what would change, never a
    store-owned ``overrides/`` / ``versions/`` that is preserved rather than
    landed (ADR-0030 §5/§10). ``only_runtime`` (from ``--from``) restricts the
    diff to the named source.
    """
    store = dict(preview.store_content or ())
    for cand in preview.candidates:
        if cand.content is None or (only_runtime is not None and cand.runtime != only_runtime):
            continue
        click.secho(f"  diff  store → {cand.runtime}", fg="cyan")
        cand_files = dict(cand.content)
        for rel in sorted(set(store) | set(cand_files)):
            old = store.get(rel)
            new = cand_files.get(rel)
            if old == new:
                continue
            label = rel or preview.name
            if new is None:
                click.echo(f"    - in store only: {label}")
                continue
            try:
                old_lines = (old or b"").decode("utf-8").splitlines(keepends=True)
                new_lines = new.decode("utf-8").splitlines(keepends=True)
            except UnicodeDecodeError:
                click.echo(
                    f"    ~ binary content differs: {label} ({len(old or b'')} → {len(new)} bytes)"
                )
                continue
            for line in difflib.unified_diff(
                old_lines, new_lines, fromfile=f"store/{label}", tofile=f"{cand.runtime}/{label}"
            ):
                click.echo("    " + line.rstrip("\n"))


def _render_pull_plan(plan: PullPlan) -> None:
    action = "overwrite" if plan.store_present else "create"
    click.echo(
        f"  Pull {plan.kind}/{plan.name} from {plan.selected_runtime} → {plan.scope} ({action})"
    )
    if plan.duplicate_runtimes:
        click.echo(f"    (identical Pull payload also in: {', '.join(plan.duplicate_runtimes)})")


def _pull_refusal(result: PullApplyResult) -> click.ClickException:
    """The CLI's rendering of a Pull refusal — reason + the CLI's remediation.

    One helper for BOTH refusal sites (prepare and commit): the same
    ``canonical_exists`` can arrive from either, and a preview that names
    ``--overwrite`` while the commit stays silent is exactly the drift #1869
    set out to remove.

    A ``gate_blocked`` refusal carries ``privacy_blocked`` on BOTH tiers — the
    force valve only exists on the non-git-tracked ones
    (``pull_apply._evaluate_gate``). Keying the hint on the code alone would
    advertise ``--force-unsafe-import`` for a ``project_shared`` block that
    hard-refuses it (ADR-0011 §5), so the valve flag gates the lookup.
    """
    code = result.reason_code
    if result.status == "gate_blocked" and not result.force_bypassable:
        code = None
    return click.ClickException(remediation.append_hint(result.reason, code, "cli"))


def _print_pull_applied(result: PullApplyResult) -> None:
    if result.write_outcome == "identical":
        click.secho(f"  {result.reason}", fg="green")
        return
    click.secho(
        f"  Pulled {result.kind}/{result.name} from {result.selected_runtime} "
        f"into {result.scope} ({result.write_outcome}).",
        fg="green",
    )
    if result.duplicate_runtimes:
        click.echo(f"    (identical Pull payload also in: {', '.join(result.duplicate_runtimes)})")


@contextmanager
def _translate_to_click(*error_types: type[Exception]) -> Iterator[None]:
    """Collapse an engine-error ``except`` ladder into one arm (#1516).

    Every listed error type exits 1 as a one-line ``ClickException``
    instead of a raw traceback. ``PrivacyScanError`` /
    ``MigratePartialError`` surface their pre-formatted ``exc.message``
    (the secret-free remediation text — ADR-0011 §5 for Gate A
    refusals); ``TransferStagingBusyError`` is constructed with a single
    argument precisely so its ``str(exc)`` is already that sentence rather
    than the errno form below; ``SwapRecoveryError`` surfaces
    :func:`~memtomem.context._dir_swap.swap_failure_text`, because its
    three-argument ``OSError`` form renders ``str(exc)`` as
    ``[Errno 16] <sentence>: '<path>'`` — listing the type here without this
    branch would leave ``mm context install skill`` / ``update skill``, the two
    primary single-asset commands, printing the errno form the rest of this
    change removed (PR review); everything else surfaces ``str(exc)``.
    """
    try:
        yield
    except error_types as exc:
        if isinstance(exc, (PrivacyScanError, MigratePartialError)):
            message = exc.message
        elif isinstance(exc, SwapRecoveryError):
            message = swap_failure_text(exc)
        else:
            message = str(exc)
        raise click.ClickException(message) from exc


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
        # Validated upstream by the click.Choice on --scope; cast for mypy.
        return cast(TargetScope, scope_flag)
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


def _ensure_local_tier_gitignored(project_root: Path, *, required: bool = False) -> None:
    """Append the project_local gitignore marker at *project_root* and report.

    Shared by every verb that can land a canonical on the project_local
    tier without ``mm context init`` having run there (``migrate --to``,
    ``move`` / ``copy`` — for cross-project transfers pass the
    DESTINATION root). ``already_present`` is deliberately silent: the
    marker is already there, the user does not need a redundant green
    tick on every transfer.

    *required* makes an unprotectable tier a refusal instead of a warning.
    ADR-0037 §6 says receipt establishes the marker "failing closed if the
    tier cannot be protected", and a bundle is the one input that arrives from
    another machine: warning and landing it anyway puts foreign bytes in a
    directory the next ``git add -A`` would stage. The in-machine verbs keep
    the warning, where the content was already on this disk and already the
    user's.
    """
    wrote, msg = _append_gitignore_marker(project_root)
    if wrote:
        click.secho(
            "  Appended .gitignore marker (.memtomem/*.local/, .memtomem/.staging/)",
            fg="green",
        )
        return
    if msg == "no_git_repo_pyproject_only":
        detail = (
            "project root resolved via pyproject.toml but `.git` missing. "
            "Run `git init` first to git-protect the local tier."
        )
    elif msg == "no_project_signal":
        detail = "no .git and no pyproject.toml in project root."
    else:
        return
    if required:
        raise click.ClickException(
            f"cannot protect the project_local tier at {project_root}: {detail}\n"
            f"  The .gitignore marker is what keeps a received artifact out of "
            f"`git add -A`, so import refuses rather than land unprotected bytes."
        )
    click.secho(f"  warning: {detail} .gitignore not appended.", fg="yellow")


def _print_settings_detect(root: Path, scope: str) -> None:
    files = detect_settings_files(root, scope)
    if not files:
        click.echo("  (no settings files detected)")
        return
    click.secho(f"  {len(files)} settings file(s):", fg="cyan")
    for f in files:
        status = f"({f.size} bytes)" if f.size else "(not yet created)"
        click.echo(f"    {f.agent:17s}  {scrub_text(str(f.path))}  {status}")


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
        "Settings push will modify the following files outside this project:",
        fg="yellow",
    )
    for p in pending:
        click.echo(f"  {scrub_text(str(p))}")
    return click.confirm("Continue?", default=False)


def _print_duplicate_tier_warnings(root: Path, *, scope: str) -> None:
    """Emit non-blocking warnings for cross-tier duplicates + malformed matchers.

    Per ADR-0010 §4 this is the primary detection surface: it fires in
    the user's actual sync workflow rather than behind a separate
    command. Output goes to stderr with yellow color and never blocks
    the sync — both axes are informational.

    The malformed axis rides here rather than staying doctor-only because
    ``_iter_signatures`` skips non-string matchers (#1987): a corrupted
    managed tier rule no longer surfaces as a (spurious) duplicate, so
    without this leg the sync workflow would report nothing at all about a
    rule Claude Code silently ignores.
    """
    duplicates = detect_duplicate_tiers(root, active_scope=scope)
    for dup in duplicates:
        click.secho(f"  warning: {format_warning(dup, active_scope=scope)}", err=True, fg="yellow")
    for finding in find_malformed_matchers(root):
        click.secho(f"  warning: {format_malformed_warning(finding)}", err=True, fg="yellow")
    for unportable_finding in find_unportable_hook_commands(root):
        click.secho(
            f"  warning: {format_unportable_command_warning(unportable_finding)}",
            err=True,
            fg="yellow",
        )
    for unscanned in find_unscanned_settings_files(root):
        click.secho(
            f"  warning: {format_unscanned_settings_warning(unscanned)}", err=True, fg="yellow"
        )


def _print_settings_generate(root: Path, *, scope: str, allow_host_writes: bool) -> None:
    _print_duplicate_tier_warnings(root, scope=scope)
    results = generate_all_settings(root, scope=scope, allow_host_writes=allow_host_writes)
    for name, r in results.items():
        if r.status == "ok":
            click.secho(f"  Settings: {name} → {scrub_text(str(r.target))}", fg="green")
            for w in r.warnings:
                click.secho(f"    warning: {scrub_text(w)}", fg="yellow")
        elif r.status == "skipped":
            click.secho(f"  skipped {name}: {scrub_text(r.reason)}", fg="yellow")
        elif r.status == "needs_confirmation":
            # Defense in depth: should not normally reach here because the CLI
            # caller already gated on ``_confirm_settings_host_writes``.
            click.secho(f"  needs confirmation {name}: {scrub_text(r.reason)}", fg="yellow")
        elif r.status in ("error", "aborted"):
            click.secho(f"  {r.status} {name}: {scrub_text(r.reason)}", fg="red")


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
                click.secho(f"    warning: {scrub_text(w)}", fg="yellow")
        elif r.status == "skipped":
            click.secho(f"  skipped {name}: {scrub_text(r.reason)}", fg="yellow")
        elif r.status == "error":
            click.secho(f"  error {name}: {scrub_text(r.reason)}", fg="red")


def _print_mcp_servers_generate(root: Path, *, surface: str = "cli_context_sync") -> None:
    """Fan canonical MCP server definitions into the project ``.mcp.json`` (#1311).

    The CLI leg of ``mm context sync --include=mcp-servers`` reuses the SAME
    pure ``generate_all_mcp_servers`` core the web ``/context/mcp-servers/sync``
    route calls, so the two surfaces stay byte-parallel: canonical-wins-per-name
    merge, ``IN_SYNC`` no-op (no mtime churn when already in sync), and the
    foreign-entry-preserving mode policy. A Gate A privacy hit or an unparseable
    canonical aborts the whole leg (the engine refuses to write a partial
    ``.mcp.json``), surfaced as a red ``ClickException`` like the other legs.
    """
    try:
        result = generate_all_mcp_servers(root, surface=surface)
    except McpServerPrivacyError as exc:
        raise click.ClickException(str(exc)) from exc
    except McpServerParseError as exc:
        raise click.ClickException(str(exc)) from exc
    if result.generated:
        click.secho(f"  MCP servers fan-out: {len(result.generated)}", fg="green")
        for runtime, name, path in result.generated:
            rel = path.relative_to(root) if path.is_relative_to(root) else path
            click.echo(f"    {runtime:15s}  {name}  {rel}")
    for runtime, reason, _code in result.skipped:
        # NO_CANONICAL_ROOT / IN_SYNC are informational (nothing to fan, or
        # already in sync) — yellow, never an error.
        click.secho(f"  skipped {runtime}: {reason}", fg="yellow")


def _confirm_mcp_servers_fanout(root: Path, *, yes: bool) -> bool:
    """Confirm a ``.mcp.json`` rewrite before an ``--all-projects`` mcp-servers leg.

    A single-project ``mm context sync --include=mcp-servers`` treats the
    explicit ``--include`` as consent and never prompts. ``--all-projects`` is
    different: its only confirmation is the count-only "Sync N project(s)?"
    gate, which does NOT disclose that EXECUTABLE MCP-server config
    (command/args/env, foreign ``mcpServers`` entries carried verbatim) in each
    project's ``.mcp.json`` is about to be rewritten. So in a batch we disclose
    the per-target file and confirm before writing (#1311). ``--yes`` skips it.
    """
    if yes:
        return True
    target = root / PROJECT_MCP_CONFIG
    verb = "rewrite" if target.exists() else "create"
    click.secho(
        f"  mcp-servers push will {verb} executable MCP server config at {target}",
        fg="yellow",
    )
    return click.confirm("  Continue?", default=False)


# Kinds whose fan-out honors --label (ADR-0022 v1 scope: agents + commands).
_LABEL_ELIGIBLE_KINDS: frozenset[str] = frozenset({"agents", "commands"})


def _warn_label_ineligible_kinds(label: str | None, inc: set[str]) -> None:
    """Warn when --label is given but cannot apply to (some of) ``--include``.

    ADR-0022 invariant 10: ``--label`` governs only agents/commands. Ineligible
    included kinds (skills/settings/project-memory) run label-less; a label with
    no eligible kind in ``--include`` is a warned no-op, never an error.
    """
    if label is None or label == "latest":
        return
    ineligible = sorted(inc - _LABEL_ELIGIBLE_KINDS)
    if ineligible:
        click.secho(
            f"  note: --label does not apply to {', '.join(ineligible)} "
            "(only agents/commands are versioned); they push from the working file.",
            fg="yellow",
        )
    if not (inc & _LABEL_ELIGIBLE_KINDS):
        click.secho(
            f"  note: --label={label} had no effect — no versioned kind "
            "(agents/commands) in --include.",
            fg="yellow",
        )


@click.group("context")
def context() -> None:
    """Manage the canonical artifact Store and push it to your AI tools.

    The Store (``.memtomem/`` / ``~/.memtomem/``) holds your master copies
    of skills, agents, commands, MCP servers and settings. ``sync`` fans
    them out to detected runtimes (Claude Code, Codex, Kimi, Antigravity,
    ...); ``init`` pulls existing runtime copies into the Store;
    ``install`` / ``update`` / ``version`` manage wiki assets. Also
    includes the unified ``context.md`` generators (CLAUDE.md, GEMINI.md,
    etc.).
    """


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

    for spec in _ARTIFACT_PRINT_SPECS:
        if spec.kind in inc:
            click.echo("")
            _print_artifact_detect(spec, root)

    if "settings" in inc:
        click.echo("")
        _print_settings_detect(root, _resolve_cli_scope(None))


@context.command("init")
@_INCLUDE_OPTION
@click.option(
    "--overwrite",
    is_flag=True,
    help="Overwrite existing canonical entries when pulling from runtimes.",
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
        "Bypass Gate A on existing runtime files being pulled. "
        "user / project_local destinations only — project_shared "
        "hard-refuses (ADR-0011 §5)."
    ),
)
@click.option(
    "--only",
    "only_name",
    metavar="NAME",
    default=None,
    help=(
        "Pull only the runtime artifact with this exact name (pull-only "
        "mode: skips context.md and directory seeding). Requires exactly one "
        "--include kind of skills, agents, or commands."
    ),
)
def init_cmd(
    include: tuple[str, ...],
    overwrite: bool,
    scope_flag: str | None,
    confirm_project_shared: bool,
    force_unsafe_import: bool,
    only_name: str | None,
) -> None:
    """Seed canonical artifact dirs and (optionally) pull existing runtime files.

    Without ``--scope`` the command preserves pre-PR-E2 behavior: writes
    ``<proj>/.memtomem/context.md`` and (with ``--include``) pulls under
    ``<proj>/.memtomem/{agents,skills,commands}/`` (the implicit
    ``project_shared`` tier). Pass ``--scope=user`` /
    ``--scope=project_local`` to seed at the user or local-draft tier
    instead. ADR-0011 §5 Gate A scans every pulled file's bytes for
    secrets; project_shared hard-refuses on any hit.

    ``--only NAME`` (#1520 item 4) narrows the runtime pull to one named
    artifact — the CLI twin of the web's single-name pull route. It is
    pull-only: context.md, directory seeding, and the .gitignore append
    are skipped; the scope gates above still apply because they guard the
    pull write itself. ``--scope=project_local`` is rejected — the draft
    tier has no runtime fan-out to pull from, so the command could only
    ever no-op. Exits 1 when no runtime artifact matches (the engine
    contract reports not-found as empty ``imported`` + ``skipped``).
    """
    inc = _parse_include(include)
    if only_name is not None:
        # Fail fast, before any prompt or filesystem write. ``settings`` is
        # parse-accepted for init but has no import branch at all, and the
        # engine ``only_name=`` narrowing exists only for the three artifact
        # kinds — so exactly one of skills/agents/commands is required.
        if len(inc) != 1 or "settings" in inc:
            raise click.UsageError(
                "--only requires exactly one --include kind (skills, agents, or commands)."
            )
        try:
            validate_name(only_name)
        except InvalidNameError as exc:
            raise click.ClickException(str(exc)) from exc
        # Codex review: project_local has no runtime fan-out to import FROM
        # (ADR-0011 §3 — the engines short-circuit to a skip), and --only
        # disables every seeding side effect, so the command would exit 0
        # having done literally nothing. Reject up front instead.
        if scope_flag == "project_local":
            raise click.UsageError(
                "--only cannot pull into --scope=project_local: the draft "
                "tier has no runtime fan-out to pull from (ADR-0011 §3)."
            )
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

    # ADR-0011 §5 Gate B consent (#2306). After the block, not inside it —
    # the block runs only when the flag is absent. ``scope_explicit`` is
    # kept: an implicit project_shared default asks for no consent.
    if scope_explicit and scope == "project_shared":
        privacy.emit_project_shared_confirmation(
            surface="cli_context_init",
            mechanism="flag" if confirm_project_shared else "prompt",
            action="init",
        )

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
    write_context_md = has_project_signal and not artifact_only_scope and only_name is None
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
            content = _read_agent_file(best.path)
            sections = extract_sections_from_agent_file(
                content, source=preamble_source(best.agent, best.path)
            )
            for f in files:
                if f.path == best.path:
                    continue
                other_content = _read_agent_file(f.path)
                other_sections = extract_sections_from_agent_file(
                    other_content, source=preamble_source(f.agent, f.path)
                )
                for key, val in other_sections.items():
                    if key not in sections and val.strip():
                        sections[key] = val

        ctx_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write (#1247 id 19): a crash mid-write must not truncate the
        # canonical context.md — unlike the runtime fan-out targets it is NOT
        # regenerable. 0o644: project file meant to be read (and committed)
        # by other tools, not a 0o600 state file.
        atomic_write_text(ctx_path, sections_to_markdown(sections), 0o644)
        click.secho(f"Created {CONTEXT_FILENAME}", fg="green")
        click.echo(f"  Sections: {', '.join(sections.keys())}")
        click.echo("  Edit this file, then run 'mm context generate' to push.")

    # Seed canonical sub-artifact dirs at the resolved scope (idempotent).
    # Skipped in --only import-only mode: the extract engines mkdir their
    # own destination parents, and a single-name import should not seed
    # sibling kinds' directories.
    if only_name is None:
        for kind in ("agents", "skills", "commands"):
            d = canonical_artifact_dir(kind, scope, root)
            d.mkdir(parents=True, exist_ok=True)
            click.secho(f"  Created {d}", fg="green")

    # project_local — auto-append the .gitignore block so the local-draft
    # tier and staging dir never end up tracked by accident.
    if only_name is None and scope == "project_local":
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
    for spec in _ARTIFACT_PRINT_SPECS:
        if spec.kind in inc:
            click.echo("")
            result = _print_artifact_init(
                spec,
                root,
                overwrite=overwrite,
                scope=scope,
                force_unsafe_import=force_unsafe_import,
                only_name=only_name,
            )
            # Engine contract: non-matching stems are silently skipped, so
            # "no such runtime artifact" surfaces as empty imported+skipped
            # (mirrors the web single-name import's 404).
            if only_name and not result.imported and not result.skipped:
                raise click.ClickException(
                    f"No runtime {spec.kind} entry named {only_name!r} to pull at scope='{scope}'"
                )


def _read_agent_file(path: Path) -> str:
    """Read a detected agent file, turning IO/decode errors into a clean CLI
    error instead of a raw traceback (#1123 B2-3).

    Agent files are auto-detected, so one may be binary, unreadable, or have
    vanished between detection and read; surface that as a ``ClickException``.
    """
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise click.ClickException(f"Could not read {path}: {exc}") from exc


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
@_LABEL_OPTION
def generate_cmd(
    agent: str,
    include: tuple[str, ...],
    strict: bool,
    on_drop: str,
    yes: bool,
    scope_flag: str | None,
    label: str | None,
) -> None:
    """Generate agent files from .memtomem/context.md."""
    # Validate --agent up front so a typo fails loudly (exit 1) instead of
    # printing a red line and still exiting 0 (#1123 B2-1). "all" expands to
    # every registered generator, so it never needs validating.
    if agent != "all" and agent not in GENERATORS:
        raise click.ClickException(f"Unknown agent: {agent}. Available: {', '.join(GENERATORS)}")
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
                gen = GENERATORS[name]
                content = gen.generate(sections)
                out_path = root / gen.output_path
                out_path.parent.mkdir(parents=True, exist_ok=True)
                # Atomic write (#1247 id 19): crash mid-write must not
                # truncate the user's CLAUDE.md / GEMINI.md / etc.
                atomic_write_text(out_path, content, 0o644)
                click.echo(f"  {name:10s}  {gen.output_path}")
    elif not inc:
        click.secho(f"{CONTEXT_FILENAME} not found. Run 'mm context init' first.", fg="red")
        # Exit non-zero so a script / CI wrapping `mm context generate` can
        # detect the refusal — this used to `return` and still exit 0. Raise
        # SystemExit (not ClickException) to keep the red guidance text exactly
        # as printed, matching the `seed-validation` scan leg's convention.
        raise SystemExit(1)
    else:
        click.secho(f"  ({CONTEXT_FILENAME} missing — skipping project memory)", fg="yellow")

    # ADR-0011 PR-E3: resolve the canonical-artifact scope once and thread
    # it through the three include helpers. Defaults to ``project_shared``
    # via ``_resolve_artifact_cli_scope`` (NOT ``_resolve_cli_scope``,
    # which leaks ``cfg.hooks.target_scope`` into the artifact axis —
    # see ADR-0011 PR-E1).
    artifact_scope = _resolve_artifact_cli_scope(scope_flag)
    _warn_label_ineligible_kinds(label, inc)

    for spec in _ARTIFACT_PRINT_SPECS:
        if spec.kind in inc:
            click.echo("")
            _print_artifact_generate(
                spec, root, strict=strict, on_drop=on_drop, scope=artifact_scope, label=label
            )

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
            click.secho("  Skipped settings push (declined).", fg="yellow")

    click.secho("Done.", fg="green")


@context.command("diff")
@_INCLUDE_OPTION
@click.option(
    "--scope",
    "scope_flag",
    type=click.Choice(get_args(TargetScope)),
    default=None,
    help=(
        "Canonical artifact tier for skills/agents/commands "
        "(default: project_shared). Use project_local to inspect local drafts "
        "with no runtime fan-out. When omitted, the settings axis follows the "
        "configured hooks.target_scope, matching generate/sync."
    ),
)
def diff_cmd(include: tuple[str, ...], scope_flag: str | None) -> None:
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

                current = _read_agent_file(f.path).strip()
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

    # Dual-axis scope resolution, mirroring sync_cmd/generate_cmd. Resolving
    # both axes off the same raw scope_flag (default None) keeps `diff`
    # consistent with the commands whose drift it reports: artifacts default to
    # project_shared via _resolve_artifact_cli_scope (ADR-0011 PR-E3), while the
    # settings axis follows cfg.hooks.target_scope via _resolve_cli_scope when
    # --scope is omitted (ADR-0010). The earlier "project_shared" option default
    # made scope_flag never-None, so _resolve_cli_scope could not fall back to
    # the configured target — diff compared a different settings tier than
    # generate/sync wrote (#1123 B2-2).
    artifact_scope = _resolve_artifact_cli_scope(scope_flag)

    for spec in _ARTIFACT_PRINT_SPECS:
        if spec.kind in inc:
            click.echo("")
            _print_artifact_diff(spec, root, scope=artifact_scope)

    if "settings" in inc:
        click.echo("")
        _print_settings_diff(root, scope=_resolve_cli_scope(scope_flag))


@context.command("pull")
@click.argument("kind", type=click.Choice(["skills", "agents", "commands"]))
@click.argument("name")
@click.option(
    "--from",
    "from_runtime",
    metavar="RUNTIME",
    default=None,
    help=(
        "Pull from this runtime only (per-kind eligibility: skills = "
        "claude/gemini/codex/kimi; agents/commands = claude/gemini). Required "
        "when candidates diverge."
    ),
)
@click.option(
    "--scope",
    "scope_flag",
    type=click.Choice(list(get_args(TargetScope))),
    default=None,
    help=(
        "Destination canonical tier. The preview may infer project_shared; "
        "--apply into project_shared requires this flag explicitly (ADR-0030 "
        "§11). project_local is rejected (no runtime fan-out to pull from)."
    ),
)
@click.option(
    "--overwrite",
    is_flag=True,
    help=(
        "Allow replacing an existing Store entry; the current canonical is "
        "snapshotted first (agents/commands into versions/vN.md, skills into a "
        "versions/vN/ tree, preserving Store-owned overrides/ and versions/)."
    ),
)
@click.option(
    "--diff",
    "show_diff",
    is_flag=True,
    help="Preview only: show unified diffs of the would-land content vs the Store.",
)
@click.option(
    "--apply",
    "apply_",
    is_flag=True,
    help="Execute the pull (default is a dry-run preview).",
)
@click.option(
    "--yes",
    "-y",
    "yes",
    is_flag=True,
    help=(
        "Skip the confirmation prompt. Requires --apply. Not consent for a "
        "project_shared landing: pass --confirm-project-shared for that (#2318)."
    ),
)
@click.option(
    "--confirm-project-shared",
    "confirm_project_shared",
    is_flag=True,
    help=(
        "Consent for a project_shared landing, as on every other mm write "
        "surface; --yes alone is not sufficient."
    ),
)
@click.option(
    "--force-unsafe-import",
    "force_unsafe_import",
    is_flag=True,
    help=(
        "Bypass Gate A for a reviewed false positive. user-tier destinations "
        "only — project_shared hard-refuses regardless (ADR-0011 §5), and "
        "project_local is not a Pull destination. Mirrors 'mm context init "
        "--force-unsafe-import'."
    ),
)
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    help="Preview only: emit the structured preview as JSON.",
)
def pull_cmd(
    kind: str,
    name: str,
    from_runtime: str | None,
    scope_flag: str | None,
    overwrite: bool,
    show_diff: bool,
    apply_: bool,
    yes: bool,
    confirm_project_shared: bool,
    force_unsafe_import: bool,
    json_out: bool,
) -> None:
    """Pull one runtime's copy of an artifact into the canonical Store.

    Dry-run preview by default (source-selectable, privacy-gated); --apply
    executes. When runtime copies diverge, --apply refuses until you name a
    source with --from (ADR-0030 §5).
    """
    artifact_kind = cast(ArtifactKind, kind)
    # Flag-combination guards (exit 2), mirroring migrate's UsageError style.
    if yes and not apply_:
        raise click.UsageError("--yes only applies with --apply.")
    if show_diff and apply_:
        raise click.UsageError("--diff is a preview-only flag; drop --apply.")
    if json_out and apply_:
        raise click.UsageError("--json is a preview-only flag; drop --apply.")
    if show_diff and json_out:
        raise click.UsageError("--diff and --json cannot be combined.")

    try:
        validate_name(name, kind=f"{kind[:-1]} name")
    except InvalidNameError as exc:
        raise click.ClickException(str(exc)) from exc

    scope_explicit = scope_flag is not None
    scope = _resolve_artifact_cli_scope(scope_flag)
    if scope == "project_local":
        raise click.ClickException(
            "project_local has no runtime fan-out to pull from (ADR-0011 §3); "
            "pull into user or project_shared instead."
        )
    root = _find_project_root()
    has_project_signal = (root / ".git").exists() or (root / "pyproject.toml").exists()
    if scope_explicit and scope != "user" and not has_project_signal:
        raise click.ClickException(
            f"--scope={scope} requires a project root (with .git or pyproject.toml). "
            "Use --scope=user from outside a project, or run from inside one."
        )
    # Validate --from eligibility early (reuse the engine's export-only /
    # unknown-runtime wording verbatim), for both preview and apply.
    if from_runtime is not None:
        with _translate_to_click(ValueError):
            resolve_import_runtimes(artifact_kind, from_runtime)

    if not apply_:
        preview = preview_pull(
            artifact_kind, name, scope=scope, project_root=root, include_content=show_diff
        )
        if json_out:
            click.echo(json.dumps(_pull_preview_json(preview), indent=2))
            return
        _print_pull_preview(preview, overwrite=overwrite, only_runtime=from_runtime)
        if show_diff:
            _print_pull_diff(preview, only_runtime=from_runtime)
        click.echo("\nRun with --apply to execute.")
        return

    # ── apply ──
    if scope == "project_shared" and not scope_explicit:
        raise click.ClickException(
            "--apply into the git-tracked project_shared tier requires an explicit "
            "--scope project_shared (ADR-0030 §11)."
        )
    gate_b_applies = scope == "project_shared"
    # ADR-0011 §5: --yes is not Gate B consent (#2318). Refused pre-flight like
    # every sibling's, before ``prepare_pull``: that function returns its own
    # refusals (divergent source, canonical exists, Gate A) and the
    # byte-identical no-op as values, so a later check would answer a --yes run
    # with whichever of those came first instead of the refusal every other
    # surface gives. Gates here name ``confirm_project_shared`` literally:
    # ``test_project_shared_confirmation_audit_guard.py`` finds Gate B sites by
    # that identifier.
    if gate_b_applies and yes and not confirm_project_shared:
        raise click.ClickException(
            "--scope project_shared requires --confirm-project-shared. "
            "--yes alone is not sufficient: project_shared writes go "
            "to the git-tracked tier and require explicit opt-in."
        )
    outcome = prepare_pull(
        artifact_kind,
        name,
        scope=scope,
        project_root=root,
        source_runtime=from_runtime,
        overwrite=overwrite,
        force_unsafe_import=force_unsafe_import,
    )
    if isinstance(outcome, PullApplyResult):
        # A refusal, or the byte-identical no-op (status == "applied").
        if outcome.status == "applied":
            _print_pull_applied(outcome)
            return
        raise _pull_refusal(outcome)

    plan = outcome
    _render_pull_plan(plan)
    # ADR-0011 §5 Gate B, spelled the way every other CLI surface spells it. A
    # bare --yes never reaches here on project_shared (refused above); on the
    # other tiers it keeps its ordinary "skip the prompt" meaning, which was
    # never Gate B.
    #
    # ``consent_mechanism`` is the audit line's only input: the flag path
    # records ``flag``, so only the prompt arm has to say otherwise.
    consent_mechanism = "flag"
    if gate_b_applies and not confirm_project_shared:
        if not click.confirm(
            f"\n--scope=project_shared writes to git-tracked {root}/.memtomem/. "
            f"Pull {kind}/{name} from {plan.selected_runtime}. Continue?",
            default=False,
        ):
            raise click.Abort()
        consent_mechanism = "prompt"
    elif not gate_b_applies and not yes:
        click.confirm(
            f"\nPull {kind}/{name} from {plan.selected_runtime} into {scope}?",
            abort=True,
        )
    # ADR-0011 §5 Gate B consent (#2306). A *later sibling* of the gate and
    # never inside its body: that body runs only when the consent flag is
    # absent, so an emit there would miss every --confirm-project-shared run.
    if gate_b_applies:
        privacy.emit_project_shared_confirmation(
            surface="cli_context_pull",
            mechanism=consent_mechanism,
            action="pull",
            audit_context={
                "kind": kind,
                "name": name,
                "runtime": plan.selected_runtime,
            },
        )
    result = commit_pull(plan)
    if result.status != "applied":
        raise _pull_refusal(result)
    _print_pull_applied(result)


from memtomem.cli.context_sync_cmd import (  # noqa: E402, F401
    _CLI_SYNC_SKIP_REASONS,
    _run_sync_all_projects,
    _run_sync_legs,
    sync_cmd,
)

context.add_command(sync_cmd)


# ── Version snapshots + label pointers (ADR-0022) ────────────────────

_VERSION_ARTIFACT_TYPES = ("agents", "commands", "skills")

#: Types whose version store this CLI may MUTATE. Skills are READ-ONLY
#: (ADR-0030 §10): a skill version is a ``versions/vN/`` tree snapshot created
#: only by an overwrite pull, and labeled skill fan-out is deferred.
_VERSION_WRITABLE_TYPES = frozenset({"agents", "commands"})

_VERSION_WORKING_FILENAME = {
    "agents": AGENT_DIR_FILENAME,
    "commands": COMMAND_DIR_FILENAME,
    # A skill has no single working canonical — its content is the whole
    # payload tree. ``SKILL.md`` stands in as the EXISTENCE PROBE only; it is
    # consumed by ``.is_file()`` checks behind the write gate, never snapshotted.
    "skills": SKILL_MANIFEST,
}


def _resolve_version_target(
    artifact_type: str, name: str, scope_flag: str | None
) -> tuple[Path, Path, TargetScope, Path]:
    """Resolve ``(artifact_dir, working_file, scope, project_root)``.

    Validates ``artifact_type`` and ``name``, resolves the scoped canonical
    root, and returns the per-artifact directory, its working canonical file
    (``agent.md`` / ``command.md``, or ``SKILL.md`` as an existence probe — see
    :data:`_VERSION_WORKING_FILENAME`), the resolved scope, and the project
    root. Raises ``click.ClickException`` on a bad type/name. The directory may
    not exist yet (flat-layout or absent) — the versioning layer raises a clean
    error in that case.
    """
    if artifact_type not in _VERSION_ARTIFACT_TYPES:
        raise click.ClickException(
            f"Unknown artifact type: {artifact_type}. "
            f"Expected one of: {', '.join(_VERSION_ARTIFACT_TYPES)}."
        )
    try:
        validate_name(name)
    except InvalidNameError as exc:
        raise click.ClickException(str(exc)) from exc
    root = _find_project_root()
    scope = _resolve_artifact_cli_scope(scope_flag)
    # ``artifact_type`` is narrowed by the guard above; cast to the
    # ``canonical_artifact_dir`` literal so mypy keeps the kind typed.
    kind = cast("Literal['agents', 'commands', 'skills']", artifact_type)
    artifact_dir = canonical_artifact_dir(kind, scope, root) / name
    return artifact_dir, artifact_dir / _VERSION_WORKING_FILENAME[artifact_type], scope, root


def _require_version_writes(artifact_type: str) -> None:
    """Refuse a mutating ``mm context version`` verb on a read-only type.

    Deliberately a DIFFERENT message from the unknown-type refusal even though
    both exit 1: "dogs is not a thing" and "skills is read-only" call for
    different next actions, and a script parsing stderr must be able to tell
    them apart. That is also why an UNKNOWN type falls through untouched —
    callers run this before ``_resolve_version_target``, so claiming a
    nonexistent type is merely "read-only" would swallow the real error.
    """
    if artifact_type in _VERSION_WRITABLE_TYPES or artifact_type not in _VERSION_ARTIFACT_TYPES:
        return
    raise click.ClickException(
        f"`mm context version` is read-only for {artifact_type}: a {artifact_type} version is "
        f"written only by the Store itself, and that path is not exposed yet. "
        f"`mm context version list {artifact_type} <name>` works."
    )


@context.group("version")
def version_group() -> None:
    """Manage version snapshots + label pointers (ADR-0022).

    A version is an immutable snapshot of an artifact's working canonical;
    a label (e.g. 'production') is a movable pointer over versions. Use
    `mm context sync --label <name>` to fan out a labeled version.

    Writable for agents and commands. For skills this group is READ-ONLY
    (`list` only, ADR-0030 §10): a skill version is a directory-tree snapshot
    created only by an overwrite pull.
    """


@version_group.command("create")
@click.argument("artifact_type")
@click.argument("name")
@click.option("--note", default="", help="Optional annotation stored with the snapshot.")
@_SCOPE_OPTION
def version_create_cmd(artifact_type: str, name: str, note: str, scope_flag: str | None) -> None:
    """Snapshot the current working canonical as a new immutable version.

    Example: `mm context version create agents my-agent --note "stable"`
    """
    _require_version_writes(artifact_type)
    artifact_dir, working_file, scope, root = _resolve_version_target(
        artifact_type, name, scope_flag
    )
    if not working_file.is_file():
        # `mm context migrate` only consumes lockfile-tracked flats (a
        # hand-authored or UI-created flat classifies skip_manual), so it was
        # a dead end for exactly the artifacts most likely to hit this branch.
        # Lead with `version enable` — the layout-only adopt that works for
        # ANY flat canonical — and keep migrate for wiki-installed assets,
        # where it also consolidates the lockfile manifest.
        raise click.ClickException(
            f"No working canonical at {working_file}. The artifact must exist in "
            f"directory layout — run `mm context version enable {artifact_type} {name}` "
            f"to adopt a flat-layout file (wiki-installed assets: "
            f"`mm context migrate {artifact_type} {name}`)."
        )

    def _create_locked(lt: float | None) -> versioning.VersionRecord:
        # ADR-0030 §6: read + Gate-A scan + snapshot all run INSIDE the canonical
        # lock (C0), so a concurrent Pull/CRUD edit landing between the read and
        # the version write cannot slip unscanned / stale bytes into the snapshot.
        # Gate A on the snapshot bytes (ADR-0011 trust boundary): a version write
        # into a git-tracked project_shared tree must hard-refuse a privacy hit
        # before versions/vN.md lands; user / project_local are permissive
        # (raise_or_collect only raises for project_shared).
        try:
            snapshot_bytes = working_file.read_bytes()
        except OSError as exc:
            raise click.ClickException(f"cannot read {working_file}: {exc}") from exc
        file_scan = scan_text_content(
            snapshot_bytes.decode("utf-8", errors="replace"),
            source_path=working_file,
            surface="cli_context_version_create",
            scope=scope,
            project_root=root,
        )
        if file_scan.decision in ("blocked", "blocked_project_shared"):
            try:
                raise_or_collect(
                    file_scan, scope=scope, kind=artifact_type[:-1], artifact_name=name
                )
            except PrivacyScanError as exc:
                raise click.ClickException(exc.message) from exc
        return versioning.create_version(
            artifact_dir, working_file, note=note, source_bytes=snapshot_bytes, lock_timeout=lt
        )

    try:
        # Canonical name lock → versions.json (blocking, the CLI's historical
        # Ctrl-C-able wait).
        record = versioning_op_locked(artifact_dir, timeout=None, op=_create_locked)
    except versioning.VersionError as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"Created {artifact_type}/{name} version {record.tag}", fg="green")


@version_group.command("promote")
@click.argument("artifact_type")
@click.argument("name")
@click.option(
    "--to", "label", required=True, help="Label to point at the version (e.g. production)."
)
@click.option("--version", "version", required=True, help="Version tag to promote (e.g. v2).")
@_SCOPE_OPTION
def version_promote_cmd(
    artifact_type: str, name: str, label: str, version: str, scope_flag: str | None
) -> None:
    """Move a label pointer to a specific version (rollout == rollback).

    Example: `mm context version promote agents my-agent --to production --version v2`
    """
    _require_version_writes(artifact_type)
    artifact_dir, _, _, _ = _resolve_version_target(artifact_type, name, scope_flag)
    try:
        # ADR-0030 §6 (Codex B4): canonical name lock first, then versions.json.
        versioning_op_locked(
            artifact_dir,
            timeout=None,
            op=lambda lt: versioning.promote_label(artifact_dir, label, version, lock_timeout=lt),
        )
    except versioning.VersionError as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"Promoted {artifact_type}/{name}: {label} → {version}", fg="green")


@version_group.command("list")
@click.argument("artifact_type")
@click.argument("name")
@_SCOPE_OPTION
def version_list_cmd(artifact_type: str, name: str, scope_flag: str | None) -> None:
    """List versions and label pointers for an artifact."""
    artifact_dir, _, _, _ = _resolve_version_target(artifact_type, name, scope_flag)
    try:
        manifest = versioning.load_manifest(artifact_dir)
    except versioning.VersionError as exc:
        raise click.ClickException(str(exc)) from exc
    if not manifest.versions:
        click.echo(f"  (no versions for {artifact_type}/{name})")
        return
    # Reverse label map: tag → [labels] so each version line shows its pointers.
    labels_by_tag: dict[str, list[str]] = {}
    for label, tag in manifest.labels.items():
        labels_by_tag.setdefault(tag, []).append(label)
    click.secho(f"{artifact_type}/{name} versions:", fg="cyan")
    for tag in sorted(manifest.versions, key=lambda t: int(t[1:])):
        rec = manifest.versions[tag]
        pointers = labels_by_tag.get(tag, [])
        suffix = f"  [{', '.join(sorted(pointers))}]" if pointers else ""
        note = f"  — {rec.note}" if rec.note else ""
        # A tree snapshot (versions/vN/) reads differently from a file one —
        # mark it so `vN` is not mistaken for a single .md copy (ADR-0030 §10).
        shape = "  (tree)" if rec.layout == "tree" else ""
        click.echo(f"  {tag:6s} {rec.created_at}{shape}{suffix}{note}")


@version_group.command("delete-label")
@click.argument("artifact_type")
@click.argument("name")
@click.argument("label")
@_SCOPE_OPTION
def version_delete_label_cmd(
    artifact_type: str, name: str, label: str, scope_flag: str | None
) -> None:
    """Drop a label pointer from an artifact's manifest.

    CLI parity for the web ``DELETE /context/{type}/{name}/labels/{label}``
    route and ``mem_context_label(delete=True)`` — until now the pointer
    could be created and moved from the CLI (`version promote`) but only
    dropped from the web UI or MCP. Deleting an absent label is a no-op
    (still exits 0, mirroring the web 200); the reserved `latest` is refused.

    Example: `mm context version delete-label agents my-agent production`
    """
    _require_version_writes(artifact_type)
    artifact_dir, working_file, _, _ = _resolve_version_target(artifact_type, name, scope_flag)
    if not working_file.is_file():
        # Mirror the web route's dir-layout gate: a flat artifact has no
        # version store, so "deleted" would be a misleading no-op and an
        # absent artifact deserves a loud miss, not silence.
        raise click.ClickException(
            f"No working canonical at {working_file}. A flat-layout artifact has no "
            f"version store — run `mm context version enable {artifact_type} {name}` first."
        )
    try:
        # ADR-0030 §6 (Codex B4): canonical name lock first, then versions.json;
        # the manifest read rides the same lock span.
        def _delete(lt: float | None) -> versioning.VersionsManifest:
            versioning.delete_label(artifact_dir, label, lock_timeout=lt)
            return versioning.load_manifest(artifact_dir)

        manifest = versioning_op_locked(artifact_dir, timeout=None, op=_delete)
    except versioning.VersionError as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"Dropped label {label!r} from {artifact_type}/{name}", fg="green")
    if manifest.labels:
        remaining = ", ".join(f"{lbl} → {tag}" for lbl, tag in sorted(manifest.labels.items()))
        click.echo(f"  remaining labels: {remaining}")
    else:
        click.echo("  no labels remain")


@version_group.command("enable")
@click.argument("artifact_type")
@click.argument("name")
@_SCOPE_OPTION
def version_enable_cmd(artifact_type: str, name: str, scope_flag: str | None) -> None:
    """Adopt a flat-layout artifact into directory layout so it can be versioned.

    The version store is dir-layout-only (ADR-0022 invariant 3), and
    `mm context migrate` deliberately skips flats with no lockfile entry
    (``skip_manual``) — so a hand-authored or web-created flat canonical was
    permanently locked out of CLI versioning. This is the explicit adopt
    action (CLI parity for the web ``POST …/versions/enable`` route): one
    byte-identical rename of ``<type>/<name>.md`` → ``<type>/<name>/<manifest>``,
    after which `version create` works normally. Idempotent on an
    already-dir artifact; refuses a flat+dir collision and an orphaned
    version store rather than silently mishandling them.

    Skills are always directory layout (ADR-0030 §10), so this is a reported
    no-op for them rather than a refusal — the end state it asks for already
    holds — even though every other `version` verb is read-only for skills.

    Example: `mm context version enable agents my-agent`
    """
    artifact_dir, working_file, _, _ = _resolve_version_target(artifact_type, name, scope_flag)
    canonical_root = artifact_dir.parent
    flat_path = canonical_root / f"{name}.md"

    if artifact_type not in _VERSION_WRITABLE_TYPES:
        # No flat form exists for this type, so the flat/dir collision and
        # orphan-store checks below are meaningless. Mirror the web route's
        # idempotent ``migrated: false``.
        if not working_file.is_file():
            raise click.ClickException(f"No canonical artifact at {artifact_dir}.")
        click.secho(
            f"{artifact_type}/{name} always uses directory layout; nothing to do.",
            fg="cyan",
        )
        return

    if working_file.is_file():
        if flat_path.exists():
            # flat+dir collision — refuse rather than report a misleading
            # idempotent success while a stray flat lingers (web-route mirror).
            raise click.ClickException(
                f"{name!r} has both flat ({flat_path}) and directory layouts; "
                f"remove the redundant flat file to resolve the collision."
            )
        click.secho(
            f"{artifact_type}/{name} already uses directory layout; nothing to do.",
            fg="cyan",
        )
        return

    if not flat_path.is_file():
        raise click.ClickException(
            f"No canonical artifact to adopt: neither {working_file} nor {flat_path} exists."
        )

    # Orphaned version store — adopting into it would silently attach a prior
    # same-named artifact's version history to this file (web-route mirror).
    if (
        versioning.versions_json_path(artifact_dir).exists()
        or versioning.versions_dir(artifact_dir).is_dir()
    ):
        raise click.ClickException(
            f"{name!r} cannot be adopted: an orphaned version store already exists "
            f"at {artifact_dir}. Resolve it manually first."
        )

    # Byte-identical same-scope rename — no privacy re-scan needed here; a
    # labeled sync still re-scans the frozen versions/vN.md at deploy time
    # (ADR-0022 Gate A).
    try:
        new_working = adopt_flat_to_dir(artifact_type, flat_path, artifact_dir)
    except (FileNotFoundError, FileExistsError, OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.secho(f"Adopted {artifact_type}/{name} into directory layout", fg="green")
    click.echo(f"  → {new_working}")
    click.echo(f"  next: mm context version create {artifact_type} {name}")


from memtomem.cli.context_install_cmd import (  # noqa: E402, F401
    _PROJECT_LOCAL_ANNOTATION,
    _STATUS_GLYPH,
    _dirty_asset_refusal_text,
    _maybe_hint_dirty_noop,
    _print_classification_table,
    _print_install_classification_table,
    _render_project_status,
    _run_install_all,
    _run_status_all_projects,
    _run_update_all,
    _run_update_dry_run_single,
    adopt_cmd,
    install_cmd,
    status_cmd,
    update_cmd,
)

for _extracted_install_cmd in (install_cmd, adopt_cmd, update_cmd, status_cmd):
    context.add_command(_extracted_install_cmd)


def _paused_destination_error(scope_id: str, dst_root: Path, landing: str) -> click.ClickException:
    """Refusal for a destination whose sync enrollment is paused.

    One wording for every cross-project writer (transfer, import, settings
    copy); ``landing`` names what would have been written. The path is escaped
    because a directory name can carry terminal control characters — the three
    copies this replaced had drifted, and only one of them escaped it (#2477).
    """
    return click.ClickException(
        f"destination project {scope_id} ({scrub_text(str(dst_root))}) is "
        f"paused — sync enrollment is disabled, so the {landing} would not fan "
        f"out there. Run `mm context projects resume {scope_id}` first, or pick "
        f"another destination."
    )


def _missing_store_error(dst_root: Path) -> click.ClickException:
    """Refusal for a destination directory that was never ``mm context init``-ed.

    The prose escapes the path; the ``cd`` hint quotes it with
    :func:`~memtomem._runtime_paths._hint_quote`, so it both displays safely and
    still names the same directory when pasted. The copies this replaced left
    the hint unquoted entirely, which also broke on a path with a space.
    """
    return click.ClickException(
        f"destination project has no .memtomem/ store: {scrub_text(str(dst_root))}\n"
        f"  Initialize it first: cd {_hint_quote(dst_root)} && mm context init"
    )


from memtomem.cli.context_bundle_cmd import (  # noqa: E402, F401
    _MIGRATE_ACTION_LABEL,
    _MIGRATE_GLYPH,
    _maybe_hint_adopt_untracked,
    _migrate_action_label,
    _migrate_memory_dispatch,
    _migrate_scope_dispatch,
    _print_migrate_preview,
    _print_migrate_scope_result,
    _print_transfer_result,
    _summarize_migrate_rows,
    _transfer_dispatch,
    _transfer_options,
    copy_cmd,
    export_cmd,
    import_cmd,
    migrate_cmd,
    move_cmd,
)
from memtomem.cli.context_settings_cmd import (  # noqa: E402, F401
    _MIGRATE_SCOPE_FROM,
    _MIGRATE_SCOPE_TO,
    _hook_copy_payload,
    _is_within,
    _leg_glyph_note,
    _print_hook_copy_plan,
    _print_hook_copy_result,
    _print_migrate_plan_human,
    settings_copy_cmd,
    settings_doctor_cmd,
    settings_migrate_cmd,
)

for _extracted_bundle_or_settings_cmd in (
    migrate_cmd,
    move_cmd,
    copy_cmd,
    export_cmd,
    import_cmd,
    settings_doctor_cmd,
    settings_migrate_cmd,
    settings_copy_cmd,
):
    context.add_command(_extracted_bundle_or_settings_cmd)


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
        # Even an explicit single-file source must be .md, matching the glob
        # branch's filter and the documented `.md files` contract (#1123 B2-5).
        if expanded.suffix != ".md":
            raise click.ClickException(f"Memory source must be a .md file: {source_arg}")
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
    surface: str = "cli_context_memory_migrate",
    consent_mechanism: str = "flag",
    stdout_buf: list[str] | None = None,
    stderr_buf: list[str] | None = None,
) -> None:
    """Apply or dry-run a memory-migrate plan over the resolved sources.

    ``surface`` / ``consent_mechanism``: this helper carries Gate B for
    both the CLI verb and the MCP tool that delegates to it, so the
    ADR-0011 §5 consent line must name the caller rather than the impl.
    The MCP wrapper passes its own surface and ``param``; an accepted
    interactive prompt overrides the mechanism to ``prompt``.

    ``stdout_buf`` / ``stderr_buf``: when provided, every plan/summary/
    error message that the CLI would emit via ``click.echo`` /
    ``click.secho`` is appended to the corresponding list INSTEAD of
    being written to the global ``sys.stdout`` / ``sys.stderr``. Used by
    the MCP wrapper (``mem_context_memory_migrate``) to capture per-call
    output without ``contextlib.redirect_stdout`` — which would leak
    output from other concurrent MCP tool calls into the migrate
    response while this coroutine is suspended on I/O. When both
    buffers are ``None`` (CLI default) the helper behaves exactly as
    before.
    """
    import shutil
    import time
    from contextlib import AsyncExitStack

    from memtomem import privacy
    from memtomem.cli._bootstrap import cli_components
    from memtomem.context._atomic import (
        _MIGRATE_SIDECAR_LOCK_BUDGET_S,
        memory_lock_path,
        async_file_lock,
    )
    from memtomem.errors import ConfigError
    from memtomem.memory_scope import (
        MemoryScopeError,
        is_project_tier_registered,
        project_tier_registration_error,
        require_user_base,
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
        # A migration touching the user tier refuses on an empty
        # ``memory_dirs`` instead of falling back to the historical
        # literal: ``--apply`` moves files, and the "index nothing" state
        # must not move them into or out of a directory the active config
        # disabled (#1768). Project-to-project migrations resolve
        # independently of the user tier.
        try:
            if "user" in (from_scope, to_scope):
                user_base = require_user_base(
                    comp.config.indexing.memory_dirs,
                    comp.config.indexing.project_memory_dirs,
                )
                from_dir = resolve_memory_scope_dir(from_scope, project_root, user_base=user_base)
                to_dir = resolve_memory_scope_dir(to_scope, project_root, user_base=user_base)
            else:
                from_dir = resolve_memory_scope_dir(from_scope, project_root)
                to_dir = resolve_memory_scope_dir(to_scope, project_root)
        except (MemoryScopeError, ConfigError) as exc:
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
            # A glob like ``**/*.md`` can match two sources in
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

        # ADR-0011 PR-D: project_shared targets need explicit consent —
        # ``--confirm-project-shared`` or the interactive prompt below.
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

        # ADR-0011 §5 Gate B: record the consent that just cleared the gate
        # (#2306). One line per batch, mirroring the one prompt above —
        # every file in the plan lands in the same tier. ``to_dir`` is an
        # absolute path and stays out of the line; the count is the part
        # that says how much was opted into git.
        if to_scope == "project_shared":
            privacy.emit_project_shared_confirmation(
                surface=surface,
                mechanism=consent_mechanism if confirm_project_shared else "prompt",
                action="move",
                audit_context={"files": len(plan), "from_scope": from_scope},
            )

        to_dir.mkdir(parents=True, exist_ok=True)
        # ADR-0011 PR-D: hold an exclusive sidecar
        # lock on BOTH source and target paths spanning the FS move and
        # the DB UPDATE. A concurrent ``mm web`` watcher fires
        # ``index_file(target)`` on the move event; without the lock the
        # watcher can race in between ``shutil.move`` and
        # ``update_chunks_scope_for_source`` and INSERT a fresh chunk
        # row at ``target`` (new UUID) before our UPDATE flips the
        # original chunk's source_file. End state: two sets of chunks
        # at the destination, defeating the chunk-id-stability guarantee
        # the migrate command promises. For batch mode we acquire every
        # lock up front via ``AsyncExitStack`` so a watcher cannot race any
        # of the per-file pairs at any point mid-batch; reverse-order
        # release on context exit. Locks live on the file's parent so
        # they survive the rename (``feedback_sidecar_lockfile_for_
        # replaced_files.md``).
        completed: list[tuple[Path, Path, int]] = []
        # Acquire locks in a globally
        # stable order (sorted by string path) rather than plan order.
        # ``async_file_lock`` uses a bounded ``portalocker.LOCK_EX`` poll, so
        # two concurrent batch migrations that share files in opposite orders
        # cannot deadlock indefinitely; the sort keeps the acquisition order
        # stable so they can't livelock either. ``context/migrate.py`` sorts
        # lock paths for the same reason. The unique set is built first so
        # plan-order duplicates (same source dir matched twice) don't try to
        # re-acquire the same lock.
        #
        # #1587: acquire the sidecars via the ASYNC bounded primitive, not the
        # synchronous blocking ``_file_lock``. This function is awaited by the
        # MCP ``mem_context_memory_migrate`` tool, so a blocking ``LOCK_EX`` on
        # the event loop would freeze the whole server if a watcher held one of
        # these sidecars while suspended on an embed await (the sidecar is now
        # held across CRUD spans too). ONE shared deadline bounds the WHOLE
        # batch — per-lock timeouts would sum to N×budget (#1145) — so each lock
        # gets only the remaining budget.
        all_lock_paths: set[Path] = set()
        for entry in plan:
            all_lock_paths.add(memory_lock_path(entry["source"]))
            all_lock_paths.add(memory_lock_path(entry["target"]))
        deadline = time.monotonic() + _MIGRATE_SIDECAR_LOCK_BUDGET_S
        async with AsyncExitStack() as stack:
            for lp in sorted(all_lock_paths, key=str):
                remaining = deadline - time.monotonic()
                try:
                    if remaining <= 0:
                        raise TimeoutError
                    await stack.enter_async_context(async_file_lock(lp, timeout=remaining))
                except TimeoutError:
                    raise click.ClickException(
                        f"could not acquire memory-migrate locks within "
                        f"{_MIGRATE_SIDECAR_LOCK_BUDGET_S:g}s — a CRUD writer or another "
                        "migrate is holding a memory file; retry."
                    ) from None

            for i, entry in enumerate(plan, 1):
                src, tgt = entry["source"], entry["target"]

                # Apply-time privacy audit record (Gate A re-scan).
                # Two reasons to re-scan here, not just to log an audit
                # hit:
                # 1. ``record_outcome=True`` only fires for writes that
                #    actually proceed (the pre-flight pass used
                #    ``record_outcome=False`` to avoid recording allowed
                #    writes for files a later batch failure would skip).
                # 2. The file can
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
                        # The double-
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


# ── mm context projects ──────────────────────────────────────────────────
#
# CLI face of the multi-project registry (#1272, mechanism-track campaign
# #1270). Web has had discovery + enrollment since ADR-0021; these commands
# expose the same KnownProjectsStore / discover_project_scopes surface so
# cross-project flags (--to-project, --all-projects) have a CLI-native way
# to enumerate scope_ids and manage enrollment.


def _projects_gateway_cfg() -> ContextGatewayConfig:
    """One construction point so every subcommand reads the same env/config."""
    return ContextGatewayConfig()


def _projects_store(cfg: ContextGatewayConfig) -> KnownProjectsStore:
    return KnownProjectsStore(Path(cfg.known_projects_path).expanduser())


def _projects_discover(cfg: ContextGatewayConfig, cwd: Path | None = None) -> list[ProjectScope]:
    """Discover with the exact flags the web GET /api/context/projects uses.

    ``cwd`` overrides the server-cwd anchor: the ``--all-projects`` batch
    passes ``_find_project_root()`` so a run from a project subdirectory
    anchors at the project root (the web lifespan does the same); the
    ``mm context projects`` subcommands keep the raw-cwd default.
    """
    return discover_project_scopes(
        Path.cwd() if cwd is None else cwd,
        Path(cfg.known_projects_path).expanduser(),
        experimental_claude_projects_scan=cfg.experimental_claude_projects_scan,
        auto_display_configured_projects=cfg.auto_display_configured_projects,
    )


def _projects_resolve_or_die(selector: str, scopes: list[ProjectScope]) -> tuple[Path, str]:
    """Resolve a ``scope_id | path`` selector → ``(root, scope_id)`` or exit."""
    try:
        root, scope = resolve_project_selector(selector, scopes)
    except UnknownProjectSelectorError as exc:
        raise click.ClickException(str(exc)) from exc
    return root, scope.scope_id if scope is not None else compute_scope_id(root)


@context.group("projects")
def projects_group() -> None:
    """Inspect and manage the multi-project registry (known_projects.json).

    Subcommands accept a project as either its ``p-<sha12>`` scope_id (from
    ``list``) or a filesystem path. ``pause`` / ``resume`` flip the per-project
    sync-enrollment flag that ``--all`` batch commands and the web Sync gate
    honor.
    """


@projects_group.command("list")
@click.option(
    "--json",
    "json_out",
    is_flag=True,
    help="Emit the discovery payload as JSON (same fields as GET /api/context/projects).",
)
def projects_list_cmd(json_out: bool) -> None:
    """List every discovered project scope with health and enrollment."""
    cfg = _projects_gateway_cfg()
    scopes = _projects_discover(cfg)
    if json_out:
        rows = []
        for s in scopes:
            health = annotate_project_health(s)
            rows.append(
                {
                    "scope_id": s.scope_id,
                    "label": s.label,
                    "root": str(s.root) if s.root is not None else None,
                    "tier": s.tier,
                    "sources": list(s.sources),
                    "missing": health.missing,
                    "stale": health.stale,
                    "experimental": s.experimental,
                    "enabled": s.enabled,
                    "sync_eligible": s.sync_eligible,
                }
            )
        click.echo(json.dumps({"scopes": rows}, indent=2))
        return

    click.echo(f"{len(scopes)} project scope(s):")
    for s in scopes:
        health = annotate_project_health(s)
        # Pad BEFORE styling — ANSI escapes are invisible but count toward
        # f-string width, so styling first skews the columns.
        if health.missing:
            health_txt = click.style(f"{'missing':<8}", fg="red")
        elif health.stale:
            health_txt = click.style(f"{'stale':<8}", fg="yellow")
        else:
            health_txt = click.style(f"{'ok':<8}", fg="green")
        if s.enabled:
            enrollment = f"{'enabled':<8}"
        else:
            enrollment = click.style(f"{'paused':<8}", fg="yellow")
        eligible = "sync" if s.sync_eligible else "-"
        sources = ",".join(s.sources)
        click.echo(
            f"  {s.scope_id}  {health_txt}  {enrollment}  {eligible:<4}  "
            f"{s.label}  ({s.root})  [{sources}]"
        )


@projects_group.command("add")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@click.option("--label", default=None, help="Display label for the project.")
def projects_add_cmd(path: Path, label: str | None) -> None:
    """Register PATH in known_projects.json (idempotent)."""
    cfg = _projects_gateway_cfg()
    store = _projects_store(cfg)
    try:
        # ``add_with_status`` decides created-vs-existing INSIDE the store's write
        # lock, replacing a racy load-then-add check across two separate lock
        # windows (the web route shares this same atomic path — #1292).
        entry, created = store.add_with_status(path, label=label)
    except KnownProjectsCorruptError as exc:
        raise click.ClickException(str(exc)) from exc
    scope_id = compute_scope_id(entry.root)
    if created:
        click.secho(f"Registered: {scope_id} ({entry.root})", fg="green")
    else:
        click.echo(f"Already registered: {scope_id} ({entry.root})")


@projects_group.command("remove")
@click.argument("selector")
def projects_remove_cmd(selector: str) -> None:
    """Unregister a project (scope_id or path). Does not touch the project's files."""
    cfg = _projects_gateway_cfg()
    scopes = _projects_discover(cfg)
    _root, scope_id = _projects_resolve_or_die(selector, scopes)
    try:
        removed = _projects_store(cfg).remove_by_scope_id(scope_id)
    except KnownProjectsCorruptError as exc:
        raise click.ClickException(str(exc)) from exc
    if not removed:
        raise click.ClickException(
            f"{selector!r} resolved to {scope_id} but it has no known_projects entry "
            f"(server-cwd and scan-discovered scopes are not registered; nothing to remove)"
        )
    click.secho(f"Removed: {scope_id}", fg="green")


def _projects_set_enabled(selector: str, *, enabled: bool, verb: str) -> None:
    cfg = _projects_gateway_cfg()
    scopes = _projects_discover(cfg)
    _root, scope_id = _projects_resolve_or_die(selector, scopes)
    try:
        entry = _projects_store(cfg).set_enabled_by_scope_id(scope_id, enabled)
    except KnownProjectsCorruptError as exc:
        raise click.ClickException(str(exc)) from exc
    if entry is None:
        raise click.ClickException(
            f"{selector!r} resolved to {scope_id} but it has no known_projects entry — "
            f"only registered projects can be {verb}d; run `mm context projects add <path>` first"
        )
    click.secho(f"{verb.capitalize()}d: {scope_id} ({entry.root})", fg="green")


@projects_group.command("pause")
@click.argument("selector")
def projects_pause_cmd(selector: str) -> None:
    """Pause sync enrollment for a registered project (skipped by --all batches)."""
    _projects_set_enabled(selector, enabled=False, verb="pause")


@projects_group.command("resume")
@click.argument("selector")
def projects_resume_cmd(selector: str) -> None:
    """Resume sync enrollment for a paused project."""
    _projects_set_enabled(selector, enabled=True, verb="resume")


# ── seed-validation (hidden QA helper) ──────────────────────────────────────


@context.command("seed-validation", hidden=True)
@click.argument(
    "directory",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Seed even if DIRECTORY is non-empty (a real project is otherwise never overwritten).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Print the seed manifest as JSON (for scripting the facilitator setup).",
)
def seed_validation_cmd(directory: Path, force: bool, as_json: bool) -> None:
    """Seed the ADR-0026 §Validation first-run demo project into DIRECTORY.

    Hidden QA helper. Writes the six Context Gateway first-run affordances
    (out-of-sync / not-imported / empty / mcp-orphan / parse-error / in-sync)
    into a fresh project so a moderator — or an async user-test participant who
    only ``pip install``-ed memtomem — can reproduce the exact state ADR-0026
    §Validation measures. The recipe lives in the seeder docstring
    (``memtomem.context._validation_seed``); ``test_ctx_validation_harness``
    pins it against the real diff engine.

    Refuses a non-empty target directory unless ``--force`` so it can never
    silently overwrite a real project — the seeder writes ``.memtomem/`` *and*
    runtime trees (``.claude/`` …), ``.mcp.json``, and ``pyproject.toml``, any
    of which a real repo may already have, and the Gateway shows the server-cwd
    project, so launching ``mm web`` from a real repo would otherwise mix the
    seed into live data.
    """
    from memtomem.context._validation_seed import seed_adr0026_validation_states

    if directory.exists() and any(directory.iterdir()) and not force:
        raise click.ClickException(
            f"{directory} is not empty — refusing to seed so a real project is never "
            "overwritten. Pass a fresh/empty directory, or --force to seed in place."
        )

    try:
        manifest = seed_adr0026_validation_states(directory)
    except SwapRecoveryError as exc:
        # ADR-0030 §10 at the CLI boundary, not inside the seeder: a library
        # caller (the harness test) should still see the typed error. A dev tool
        # stops loudly — seeding into a Store with an interrupted swap would
        # produce a demo project whose state nobody can trust.
        raise click.ClickException(
            f"cannot seed: an interrupted directory swap is pending in the target "
            f"Store and must be resolved first — {swap_failure_text(exc)}"
        ) from exc

    if as_json:
        click.echo(json.dumps(manifest, indent=2))
        return

    root = manifest["project_root"]
    click.secho(f"Seeded ADR-0026 §Validation first-run state at {root}", fg="green")
    click.echo("\nSix Context Gateway affordances seeded:")
    for state in manifest["states"].values():
        name = state.get("name", "—")
        status = state.get("diff_status") or "empty"
        click.echo(f"  {state['tile']:12s} {name:16s} {status}")
    click.echo(
        "\nNext — launch the Gateway against this project:\n"
        f"  cd {root}\n"
        "  mm web\n"
        "  # open the Gateway tab — the Simple view is the default (ADR-0026 D-F).\n"
        "  # the Project path shown must match the directory above (the Gateway\n"
        "  # follows the server's working dir)."
    )
