"""memtomem context install / adopt / update / status commands."""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import click

from memtomem.cli import context_cmd as _ctx
from memtomem.config import TargetScope
from memtomem.context._dir_swap import SwapRecoveryError, swap_failure_text
from memtomem.context._names import InvalidNameError
from memtomem.context.install import (
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
)
from memtomem.context.lockfile import LockfileError
from memtomem.context.privacy_scan import PrivacyScanError
from memtomem.context.status import DRIFT_STATES, ProjectStatus
from memtomem.wiki.store import WikiNotFoundError, WikiStore, WikiUnbornHeadError


@click.command("install")
@click.argument("asset_type", type=click.Choice(["skill", "agent", "command"]), required=False)
@click.argument("name", required=False)
@click.option(
    "--all",
    "all_assets",
    is_flag=True,
    help=(
        "Re-install every ASSET in THIS project's .memtomem/lock.json at its "
        "pinned commit (fresh-machine restore). Note: `update <type> <name> "
        "--all` means a different axis — that ONE asset across every known "
        "PROJECT."
    ),
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
    HEAD — commit-true (#1643): bytes are extracted from git objects at
    HEAD, so an asset whose wiki working tree differs from HEAD (or was
    never committed) refuses with a ``mm wiki <type> commit`` hint instead
    of recording a pin that can't reproduce the installed bytes. The wiki
    at ``~/.memtomem-wiki/`` must be initialized first (``mm wiki init``).

    With ``--all``: walk ``<project>/.memtomem/lock.json`` and re-install
    every entry **at the commit each entry pins** (NOT wiki HEAD). This
    is the "fresh-machine restore" verb: a teammate ``git clone``s the
    project, runs ``mm context install --all``, and reproduces the exact
    asset bytes the lockfile recorded. To advance to wiki HEAD instead,
    run ``mm context update <type> <name>`` per asset (there is
    deliberately no bulk per-project update verb).
    """
    if all_assets:
        if asset_type or name:
            raise click.UsageError("`--all` takes no <type> <name> arguments")
        root = _ctx._find_project_root()
        _ctx._run_install_all(root, yes=yes, force=force)
        return

    if not asset_type or not name:
        raise click.UsageError("install requires <type> <name>, or pass --all")
    if yes or force:
        raise click.UsageError("--yes / --force only valid with --all")

    root = _ctx._find_project_root()
    # PrivacyScanError here is a Gate A refusal (ADR-0011 §5, #1247).
    with _ctx._translate_to_click(
        WikiNotFoundError,
        WikiUnbornHeadError,
        AssetNotFoundError,
        AlreadyInstalledError,
        UncommittedAssetError,
        InvalidNameError,
        LockfileError,
        PrivacyScanError,
        SwapRecoveryError,
    ):
        if asset_type == "skill":
            result = _ctx.install_skill(root, name)
        elif asset_type == "agent":
            result = _ctx.install_agent(root, name)
        elif asset_type == "command":
            result = _ctx.install_command(root, name)
        else:  # pragma: no cover — guarded by click.Choice
            raise click.ClickException(f"unknown asset type: {asset_type}")

    click.secho(
        f"Installed {result.asset_type}/{result.name} (wiki {result.wiki_commit[:12]})",
        fg="green",
    )
    rel_dest = result.dest.relative_to(root) if result.dest.is_relative_to(root) else result.dest
    click.echo(f"  → {rel_dest}/")
    click.echo(f"  {result.files_written} file(s) copied")


@click.command("adopt")
@click.argument("asset_type", type=click.Choice(["skill", "agent", "command"]))
@click.argument("name")
def adopt_cmd(asset_type: str, name: str) -> None:
    """Lockfile-track an existing ``.memtomem/<type>/<name>/`` that matches wiki HEAD."""
    root = _ctx._find_project_root()
    with _ctx._translate_to_click(
        WikiNotFoundError,
        WikiUnbornHeadError,
        AssetNotFoundError,
        AlreadyInstalledError,
        NotInstalledError,
        UncommittedAssetError,
        InvalidNameError,
        LockfileError,
        PrivacyScanError,
        AdoptMismatchError,
    ):
        if asset_type == "skill":
            result = _ctx.adopt_skill(root, name)
        elif asset_type == "agent":
            result = _ctx.adopt_agent(root, name)
        elif asset_type == "command":
            result = _ctx.adopt_command(root, name)
        else:  # pragma: no cover — guarded by click.Choice
            raise click.ClickException(f"unknown asset type: {asset_type}")

    click.secho(
        f"Adopted {result.asset_type}/{result.name} (wiki {result.wiki_commit[:12]})",
        fg="green",
    )
    rel_dest = result.dest.relative_to(root) if result.dest.is_relative_to(root) else result.dest
    click.echo(f"  → {rel_dest}/")
    click.echo(f"  {result.files_verified} file(s) verified byte-identical — dest untouched")


def _maybe_hint_dirty_noop(wiki: WikiStore, asset_type_plural: str, name: str) -> None:
    """Asset-scoped stderr note on a no-op update whose wiki asset is dirty (#1652)."""
    if _ctx._asset_dirty_rels(wiki, asset_type_plural, name):
        singular = asset_type_plural.removesuffix("s")
        click.secho(
            f"note: {asset_type_plural}/{name} has uncommitted wiki edits; commit "
            f"them (`mm wiki {singular} commit {name} --canonical`) and re-run "
            f"update to make them installable",
            fg="yellow",
            err=True,
        )


def _dirty_asset_refusal_text(
    wiki: WikiStore, asset_type_plural: str, name: str, dirty: list[str]
) -> str:
    """Refusal copy for the wiki-side dirty gate on the batch/dry-run paths (#1652)."""
    shown = ", ".join(dirty[:5])
    more = "" if len(dirty) <= 5 else f", +{len(dirty) - 5} more"
    return (
        f"{asset_type_plural}/{name}: wiki working tree differs from HEAD for "
        f"this asset ({len(dirty)} file(s): {shown}{more}); update is "
        f"commit-true and records HEAD's bytes only (--force only overrides "
        f"project-side edits) — {_ctx._commit_hint(wiki, asset_type_plural, name)}"
    )


@click.command("update")
@click.argument("asset_type", type=click.Choice(["skill", "agent", "command"]))
@click.argument("name")
@click.option(
    "--all",
    "all_projects",
    is_flag=True,
    help=(
        "Apply this ONE asset across every known PROJECT that has it installed. "
        "Note: `install --all` means a different axis — every asset in a SINGLE "
        "project, restored at its pin."
    ),
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
@click.option(
    "--force-head",
    "force_head",
    is_flag=True,
    help=(
        "Follow a deliberate wiki rollback: record the current wiki HEAD even "
        "when it does NOT descend from the recorded pin (moves the pin "
        "BACKWARD). Orthogonal to --force, which only overrides project-side "
        "edits — a backward move onto locally edited files needs both."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Classify and print the preview only — nothing is written and no "
        "prompt is shown. Exits 0 whenever the preview was computable, "
        "including project-side rows the real run would refuse; wiki-side "
        "uncommitted-asset refusals exit non-zero like the real run."
    ),
)
def update_cmd(
    asset_type: str,
    name: str,
    all_projects: bool,
    yes: bool,
    force: bool,
    force_head: bool,
    dry_run: bool,
) -> None:
    """Refresh an installed wiki asset from the wiki HEAD."""
    root = _ctx._find_project_root()

    try:
        wiki = _ctx.WikiStore.at_default()
        wiki.require_exists()
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    if all_projects:
        _ctx._run_update_all(
            asset_type,
            name,
            root,
            wiki=wiki,
            yes=yes,
            force=force,
            force_head=force_head,
            dry_run=dry_run,
        )
        return

    if dry_run:
        _ctx._run_update_dry_run_single(asset_type, name, root, wiki=wiki, force_head=force_head)
        return

    pinned_commit: str | None = None
    if force_head:
        try:
            pinned_commit = wiki.current_commit()
            entry = _ctx.Lockfile.at(root).read_entry(f"{asset_type}s", name) or {}
            pin = entry.get("wiki_commit")
            if _ctx._has_recorded_pin(pin) and _ctx._pin_backward_reason(wiki, pin, pinned_commit):
                click.secho(
                    f"WARNING: --force-head will move the {asset_type}s/{name} pin "
                    f"BACKWARD ({str(pin)[:12]} → {pinned_commit[:12]}) to follow the "
                    f"wiki rollback.",
                    fg="red",
                    err=True,
                )
        except (WikiNotFoundError, WikiUnbornHeadError, LockfileError, OSError):
            pinned_commit = None

    with _ctx._translate_to_click(
        WikiNotFoundError,
        WikiUnbornHeadError,
        AssetNotFoundError,
        NotInstalledError,
        StaleInstallError,
        UncommittedAssetError,
        PinNotAncestorError,
        InvalidNameError,
        LockfileError,
        PrivacyScanError,
        SwapRecoveryError,
    ):
        if asset_type == "skill":
            result = _ctx.update_skill(
                root,
                name,
                wiki=wiki,
                force=force,
                force_head=force_head,
                pinned_commit=pinned_commit,
            )
        elif asset_type == "agent":
            result = _ctx.update_agent(
                root,
                name,
                wiki=wiki,
                force=force,
                force_head=force_head,
                pinned_commit=pinned_commit,
            )
        elif asset_type == "command":
            result = _ctx.update_command(
                root,
                name,
                wiki=wiki,
                force=force,
                force_head=force_head,
                pinned_commit=pinned_commit,
            )
        else:  # pragma: no cover — guarded by click.Choice
            raise click.ClickException(f"unknown asset type: {asset_type}")

    if result.was_no_op:
        click.secho(
            f"{result.asset_type}/{result.name} unchanged (wiki {result.new_wiki_commit[:12]})",
            fg="cyan",
        )
        _ctx._maybe_hint_dirty_noop(wiki, result.asset_type, result.name)
        return

    backward_note = " (pin moved BACKWARD)" if result.pin_moved_backward else ""
    click.secho(
        f"Updated {result.asset_type}/{result.name} "
        f"({result.old_wiki_commit[:12]} → {result.new_wiki_commit[:12]}){backward_note}",
        fg="green",
    )
    rel_dest = result.dest.relative_to(root) if result.dest.is_relative_to(root) else result.dest
    click.echo(f"  → {rel_dest}/")
    click.echo(f"  {result.files_written} file(s) updated")
    if result.files_removed:
        click.secho(
            f"  {len(result.files_removed)} file(s) removed (deleted upstream)",
            fg="yellow",
        )
    if result.bak_files_written:
        click.secho(
            f"  {len(result.bak_files_written)} dirty file(s) preserved as .bak",
            fg="yellow",
        )


def _run_update_dry_run_single(
    asset_type: str,
    name: str,
    root: Path,
    *,
    wiki: WikiStore,
    force_head: bool = False,
) -> None:
    """Read-only preview for the single-project ``update --dry-run`` path."""
    asset_type_plural = f"{asset_type}s"
    with _ctx._translate_to_click(
        InvalidNameError, AssetNotFoundError, UncommittedAssetError, WikiUnbornHeadError
    ):
        new_commit, classifications = _ctx._classify_for_all_update(
            asset_type_plural,
            name,
            wiki=wiki,
            projects=[root],
            force_head=force_head,
        )

    if not classifications:
        raise click.ClickException(
            f"{asset_type_plural}/{name}: no lockfile entry; "
            f"run `mm context install {asset_type} {name}` first"
        )

    _ctx._print_classification_table(classifications, asset_type_plural, name, new_commit)

    row = classifications[0]

    if row.state == "stale-pin":
        raise click.ClickException(f"{asset_type_plural}/{name}: {row.reason}")

    if row.state != "unchanged" and (dirty := _ctx._asset_dirty_rels(wiki, asset_type_plural, name)):
        raise click.ClickException(
            f"the real run would refuse — "
            f"{_ctx._dirty_asset_refusal_text(wiki, asset_type_plural, name, dirty)}"
        )

    if row.state == "update":
        old = (row.lock_entry or {}).get("wiki_commit", "")
        old_abbr = old[:12] if isinstance(old, str) and old else "(unpinned)"
        backward_note = " — moving the pin BACKWARD (--force-head)" if row.pin_backward else ""
        click.echo(
            f"\nDry run — nothing written; would update "
            f"{old_abbr} → {new_commit[:12]}{backward_note}."
        )
    elif row.state == "unchanged":
        click.echo("\nDry run — nothing written; already up to date.")
        _ctx._maybe_hint_dirty_noop(wiki, asset_type_plural, name)
    elif row.state == "refuse":
        click.echo(
            "\nDry run — nothing written; the real run would refuse "
            "(see reason above; --force overwrites with .bak siblings)."
        )
    else:  # "error"
        click.echo("\nDry run — nothing written; lockfile error (see reason above).")


def _run_update_all(
    asset_type: str,
    name: str,
    root: Path,
    *,
    wiki: WikiStore,
    yes: bool,
    force: bool,
    force_head: bool = False,
    dry_run: bool = False,
) -> None:
    """Orchestrate ``mm context update <type> <name> --all``."""
    cfg = _ctx.ContextGatewayConfig()
    store = _ctx.KnownProjectsStore(Path(cfg.known_projects_path).expanduser())
    project_entries = store.load()

    if not project_entries:
        click.echo(
            "No known projects registered. Add via the `mm web` UI "
            "or directly in known_projects.json.",
            err=True,
        )
        return

    project_roots = [e.root for e in project_entries if e.root.is_dir() and e.enabled]
    if not project_roots:
        click.echo(
            "No registered projects exist on disk; nothing to update.",
            err=True,
        )
        return

    asset_type_plural = f"{asset_type}s"

    with _ctx._translate_to_click(
        InvalidNameError, AssetNotFoundError, UncommittedAssetError, WikiUnbornHeadError
    ):
        new_commit, classifications = _ctx._classify_for_all_update(
            asset_type_plural,
            name,
            wiki=wiki,
            projects=project_roots,
            force_head=force_head,
        )

    if not classifications:
        click.echo(
            f"No projects have {asset_type_plural}/{name} installed.",
            err=True,
        )
        return

    _ctx._print_classification_table(classifications, asset_type_plural, name, new_commit)

    needs_update = [c for c in classifications if c.state == "update"]
    needs_force = [c for c in classifications if c.state == "refuse"]
    stale_pins = [c for c in classifications if c.state == "stale-pin"]
    has_errors = [c for c in classifications if c.state == "error"]
    backward_rows = [c for c in classifications if c.pin_backward]

    if not needs_update and not needs_force and not stale_pins and not has_errors:
        click.echo("\nAll projects are up to date.")
        _ctx._maybe_hint_dirty_noop(wiki, asset_type_plural, name)
        return

    if (needs_update or needs_force) and (
        dirty := _ctx._asset_dirty_rels(wiki, asset_type_plural, name)
    ):
        refusal = _ctx._dirty_asset_refusal_text(wiki, asset_type_plural, name, dirty)
        if dry_run:
            click.echo(f"\nDry run — nothing written; the real run would refuse — {refusal}.")
        else:
            click.secho(
                f"\nRefusing to write any project — {refusal}.",
                fg="red",
                err=True,
            )
        raise click.exceptions.Exit(1)

    if dry_run:
        parts = [f"{len(needs_update)} would update"]
        if backward_rows:
            parts.append(f"{len(backward_rows)} would move the pin BACKWARD (--force-head)")
        if needs_force:
            parts.append(f"{len(needs_force)} would refuse without --force")
        if stale_pins:
            parts.append(f"{len(stale_pins)} stale pin (wiki diverged, not force-able)")
        if has_errors:
            parts.append(f"{len(has_errors)} in error")
        click.echo(f"\nDry run — nothing written; {', '.join(parts)}.")
        return

    if needs_force and not force:
        click.secho(
            f"\n{len(needs_force)} project(s) have local edits; "
            f"pass --force to proceed (modified files in the dest tree get a .bak "
            f"sibling; flat-layout files are kept on disk but stop being served). "
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

    if backward_rows:
        click.secho(
            f"WARNING: --force-head will move {len(backward_rows)} project pin(s) "
            f"BACKWARD to follow the wiki rollback.",
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
        if c.state == "stale-pin":
            click.secho(f"  ⚠ {c.project_root}: {c.reason}", fg="red")
            failures += 1
            continue

        assert c.lock_entry is not None
        dest = c.project_root / ".memtomem" / asset_type_plural / name
        try:
            upd = _ctx._apply_update(
                c.project_root,
                asset_type_plural,
                name,
                wiki=wiki,
                dest=dest,
                wiki_commit=new_commit,
                lock_entry=c.lock_entry,
                force=force,
                pin_moved_backward=c.pin_backward,
                surface="cli_context_update_all",
            )
        except StaleInstallError as exc:
            click.secho(f"  ✗ {c.project_root}: {exc}", fg="red")
            failures += 1
        except CommitNotFoundError as exc:
            click.secho(f"  ✗ {c.project_root}: {exc}", fg="red")
            failures += 1
        except PrivacyScanError as exc:
            click.secho(f"  ✗ {c.project_root}: {exc.message}", fg="red")
            failures += 1
        except SwapRecoveryError as exc:
            click.secho(
                f"  ✗ {c.project_root}: interrupted directory swap in the Store — "
                f"{swap_failure_text(exc)}",
                fg="red",
            )
            failures += 1
        except OSError as exc:
            click.secho(f"  ✗ {c.project_root}: {exc}", fg="red")
            failures += 1
        else:
            removed_note = (
                f" ({len(upd.files_removed)} file(s) removed)" if upd.files_removed else ""
            )
            backward_note = " (pin moved BACKWARD)" if upd.pin_moved_backward else ""
            click.secho(f"  ✓ {c.project_root}: updated{removed_note}{backward_note}", fg="green")
            successes += 1

    click.echo(
        f"\nSummary: {successes} updated, {failures} failed, "
        f"{len(classifications) - successes - failures} unchanged."
    )

    if failures:
        raise click.exceptions.Exit(1)


def _print_classification_table(
    classifications: list[ProjectClassification],
    asset_type_plural: str,
    name: str,
    new_commit: str,
) -> None:
    """Render the 4-state preview table for ``--all`` confirmation."""
    click.echo(
        f"\n{asset_type_plural}/{name} — wiki HEAD {new_commit[:12]} — "
        f"{len(classifications)} project(s):"
    )
    state_color = {
        "update": "green",
        "unchanged": "cyan",
        "refuse": "yellow",
        "stale-pin": "red",
        "error": "red",
    }
    for c in classifications:
        color = "red" if c.pin_backward else state_color[c.state]
        line = f"  {c.state:10s}  {c.project_root}"
        if c.reason:
            line += f"  ({c.reason})"
        if c.pin_backward:
            line += "  [moves pin BACKWARD]"
        click.secho(line, fg=color)


_STATUS_GLYPH: dict[str, tuple[str, str]] = {
    # state -> (glyph, click color)
    "ok": ("✓", "green"),
    "behind": ("↑", "cyan"),
    "dirty": ("✗", "yellow"),
    "missing": ("!", "red"),
    "stale-pin": ("⚠", "red"),
    "local-draft": ("·", "magenta"),
    "untracked": ("○", "yellow"),
}

_PROJECT_LOCAL_ANNOTATION = "(draft, no fan-out)"


@click.command("status")
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
@click.option(
    "--all-projects",
    "all_projects",
    is_flag=True,
    help=(
        "Aggregate drift across every eligible discovered project (enrolled "
        "+ on disk), not just the current one. Read-only; project_shared "
        "tier only; ineligible projects are reported and skipped."
    ),
)
def status_cmd(scope_flag: TargetScope, all_projects: bool) -> None:
    """Show installed wiki assets and their drift state."""
    if all_projects:
        if scope_flag != "project_shared":
            raise click.UsageError(
                "--all-projects reports the project_shared tier only; drop "
                "--scope or pass --scope project_shared (inspect other "
                "tiers per project, without --all-projects)."
            )
        _ctx._run_status_all_projects()
        return

    root = _ctx._find_project_root()

    _doc, lockfile_error = _ctx.load_with_recovery(root)

    wiki = _ctx.WikiStore.at_default()
    wiki_head, rows = _ctx.classify_status(root, wiki=wiki)
    if scope_flag == "user":
        rows = sorted(_ctx.scan_user_artifacts(), key=lambda r: (r.asset_type, r.name))
    else:
        rows = [row for row in rows if row.tier == scope_flag]

    if lockfile_error is not None:
        click.secho(f"  ✗ lock.json: {lockfile_error}", fg="red", err=True)

    installed_count = sum(1 for r in rows if r.state not in ("local-draft", "untracked"))
    draft_count = sum(1 for r in rows if r.state == "local-draft")
    untracked_count = sum(1 for r in rows if r.state == "untracked")
    extra_parts: list[str] = []
    if draft_count:
        extra_parts.append(f"{draft_count} local draft{'s' if draft_count != 1 else ''}")
    if untracked_count:
        extra_parts.append(f"{untracked_count} untracked")
    extra_suffix = f" (+ {', '.join(extra_parts)})" if extra_parts else ""
    scope_suffix = f" — tier {scope_flag}"
    if wiki_head is None:
        wiki_root = wiki.root
        wiki_note = (
            f"wiki at {wiki_root} is present but unusable"
            if wiki.exists()
            else f"wiki not present at {wiki_root}"
        )
        click.echo(
            f".memtomem/ — {installed_count} asset(s) installed{extra_suffix} — "
            f"{wiki_note}; pin reachability not checked{scope_suffix}"
        )
    else:
        click.echo(
            f".memtomem/ — {installed_count} asset(s) installed{extra_suffix} — "
            f"wiki HEAD {wiki_head[:12]}{scope_suffix}"
        )

    if not rows and lockfile_error is None:
        if scope_flag == "project_local":
            click.echo("\nNo project_local draft assets in this project.")
            if (root / ".git").exists() or (root / "pyproject.toml").exists():
                click.echo("Run 'mm context init --scope=project_local' to seed drafts.")
            else:
                click.echo(
                    "Run 'mm context init --scope=project_local' from inside a "
                    "project (with .git or pyproject.toml) to seed drafts."
                )
        elif scope_flag == "user":
            click.echo("\nNo user-tier assets found under ~/.memtomem/.")
            click.echo(
                "Run 'mm context init --include=agents,commands,skills --scope=user' "
                "to pull from runtimes."
            )
        else:
            click.echo(
                f"\nNo wiki assets installed and no untracked canonicals "
                f"in this project for tier {scope_flag}."
            )
            click.echo(
                "Run 'mm context detect' to see pullable runtime files, "
                "'mm context init' to seed canonicals, or "
                "'mm context install <type> <name>' to install from the wiki."
            )
        return

    last_type: str | None = None
    summary: dict[str, int] = {
        "ok": 0,
        "behind": 0,
        "dirty": 0,
        "missing": 0,
        "stale-pin": 0,
        "untracked": 0,
        "local-draft": 0,
    }
    for row in rows:
        if row.asset_type != last_type:
            click.secho(f"\n{row.asset_type}", fg="cyan")
            last_type = row.asset_type
        glyph, color = _STATUS_GLYPH[row.state]
        if row.state in ("local-draft", "untracked"):
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
            for k in ("ok", "behind", "dirty", "missing", "stale-pin", "untracked", "local-draft")
            if summary[k] > 0
        ]
        if parts:
            click.echo("\nSummary: " + ", ".join(parts) + ".")

    if lockfile_error is not None:
        raise click.exceptions.Exit(1)


def _run_status_all_projects() -> None:
    """Orchestrate ``mm context status --all-projects`` (#1280)."""
    cfg = _ctx._projects_gateway_cfg()
    scopes = _ctx._projects_discover(cfg, cwd=_ctx._find_project_root())
    wiki = _ctx.WikiStore.at_default()

    click.echo(f"{len(scopes)} project scope(s) discovered:")
    drifted = clean = errors = skipped = 0
    for s in scopes:
        code = _ctx.sync_skip_reason(s)
        if code is not None:
            skipped += 1
            click.secho(
                f"\nskip  {s.scope_id}  {s.label}  ({s.root})  [{_ctx._CLI_SYNC_SKIP_REASONS[code]}]",
                fg="yellow",
            )
            continue
        assert s.root is not None
        click.secho(f"\n→ {s.label}  ({s.scope_id}, {s.root})", fg="cyan")
        try:
            status = _ctx.collect_project_status(s.root, wiki=wiki, target_scope="project_shared")
        except Exception as exc:
            click.secho(f"  ✗ status collection failed: {exc}", fg="red")
            errors += 1
            continue
        if _ctx._render_project_status(status):
            errors += 1
        elif status.drift:
            drifted += 1
        else:
            clean += 1

    click.echo(
        f"\nSummary: {drifted} with drift, {clean} clean, {errors} error(s), {skipped} skipped."
    )
    if errors:
        raise click.exceptions.Exit(1)


def _render_project_status(status: ProjectStatus) -> bool:
    """Render one project's aggregate block; True when it counts as an error."""
    if status.lockfile_error is not None:
        click.secho(f"  ✗ lock.json: {status.lockfile_error}", fg="red")

    installed = sum(1 for r in status.rows if r.state not in ("local-draft", "untracked"))
    untracked = status.state_counts["untracked"]
    extra = f" (+ {untracked} untracked)" if untracked else ""
    state_parts = [
        f"{status.state_counts[k]} {k}"
        for k in ("ok", "behind", "dirty", "missing", "stale-pin")
        if status.state_counts[k]
    ]
    states = ": " + ", ".join(state_parts) if state_parts else ""
    wiki_note = (
        f"wiki HEAD {status.wiki_head[:12]}"
        if status.wiki_head is not None
        else "wiki unavailable; pin reachability not checked"
    )
    click.echo(f"  {installed} asset(s) installed{extra}{states} — {wiki_note}")

    for row in status.rows:
        if row.state not in DRIFT_STATES:
            continue
        glyph, color = _STATUS_GLYPH[row.state]
        line = f"  {glyph}  {row.asset_type}/{row.name}"
        if row.reason:
            line += f"  ({row.reason})"
        click.secho(line, fg=color)

    runtime_bits = [
        f"{kind.replace('_', '-')} {count} {key.replace('_', ' ')}"
        for kind, key, count in _ctx.iter_kind_drift_counts(status.diff_counts)
    ]
    if runtime_bits:
        click.secho("  runtime drift: " + "; ".join(runtime_bits), fg="yellow")
    elif not status.diff_errors:
        click.echo("  runtime: in sync")
    for kind, exc in status.diff_errors.items():
        click.secho(f"  ✗ {kind.replace('_', '-')} diff failed: {exc}", fg="red")
    return status.lockfile_error is not None or bool(status.diff_errors)


def _run_install_all(
    root: Path,
    *,
    yes: bool,
    force: bool,
) -> None:
    """Orchestrate ``mm context install --all`` (Option A: lockfile-pin restore)."""
    try:
        wiki = _ctx.WikiStore.at_default()
        wiki.require_exists()
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        classifications = _ctx._classify_for_install_all(root, wiki=wiki)
    except LockfileError as exc:
        raise click.ClickException(str(exc)) from exc

    if not classifications:
        click.echo(
            "No entries in lock.json — run `mm context install <type> <name>` first.",
            err=True,
        )
        return

    _ctx._print_install_classification_table(classifications)

    needs_install = [c for c in classifications if c.state == "install"]
    needs_force = [c for c in classifications if c.state == "refuse"]
    has_skip_force = force and any(c.state == "skip" for c in classifications)
    has_errors = [c for c in classifications if c.state == "error"]

    if needs_force and not force:
        click.secho(
            f"\n{len(needs_force)} entry(ies) have local edits; "
            f"pass --force to proceed (modified files in the dest tree get a .bak "
            f"sibling; flat-layout files are kept on disk but stop being served). "
            f"Refusing to write any entry — re-run with --force or resolve manually.",
            fg="red",
            err=True,
        )
        raise click.exceptions.Exit(1)

    actionable = bool(needs_install) or bool(needs_force) or has_skip_force
    if not actionable:
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

        try:
            pinned = _ctx._apply_pinned_install(root, c, wiki=wiki, force=force)
        except StaleInstallError as exc:
            click.secho(f"  ✗ {c.asset_type}/{c.name}: {exc}", fg="red")
            failures += 1
        except PrivacyScanError as exc:
            click.secho(f"  ✗ {c.asset_type}/{c.name}: {exc.message}", fg="red")
            failures += 1
        except CommitNotFoundError as exc:
            click.secho(f"  ⚠ {c.asset_type}/{c.name}: {exc}", fg="yellow")
            orphans += 1
        except AssetNotFoundError as exc:
            click.secho(f"  ✗ {c.asset_type}/{c.name}: {exc}", fg="red")
            failures += 1
        except SwapRecoveryError as exc:
            click.secho(
                f"  ✗ {c.asset_type}/{c.name}: interrupted directory swap in the Store — "
                f"{swap_failure_text(exc)}",
                fg="red",
            )
            failures += 1
        except OSError as exc:
            click.secho(f"  ✗ {c.asset_type}/{c.name}: {exc}", fg="red")
            failures += 1
        else:
            if pinned.was_no_op:
                click.secho(
                    f"  - {c.asset_type}/{c.name}: already installed (clean, untouched)",
                    fg="cyan",
                )
                skipped += 1
            else:
                removed_note = (
                    f" ({len(pinned.files_removed)} leftover file(s) removed)"
                    if pinned.files_removed
                    else ""
                )
                click.secho(
                    f"  ✓ {c.asset_type}/{c.name}: installed at pin "
                    f"{c.pin_commit[:12]}{removed_note}",
                    fg="green",
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
