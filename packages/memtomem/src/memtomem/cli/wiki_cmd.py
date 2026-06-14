"""``mm wiki`` — manage the local wiki (``~/.memtomem-wiki/``).

See ADR-0008 for the wiki layer's role in the context-gateway pipeline.
"""

from __future__ import annotations

from typing import Literal

import click

from memtomem.context._names import InvalidNameError, override_vendors
from memtomem.wiki import (
    WIKI_ASSET_TYPES,
    WikiAlreadyExistsError,
    WikiNotFoundError,
    WikiStore,
)
from memtomem.wiki.inspect import (
    diff_override,
    lint_asset,
)
from memtomem.wiki.override import (
    OverrideExistsError,
    seed_override,
)

# ``--vendor`` Choices derive from OVERRIDE_FORMATS (the single source of
# truth) so they never drift from the matrix: kimi is valid for skills/agents
# but not commands. See ADR-0008 "Vendor format matrix". Computed once at
# import — the matrix is a module-level constant.
_SKILL_VENDORS = override_vendors("skills")
_AGENT_VENDORS = override_vendors("agents")
_COMMAND_VENDORS = override_vendors("commands")


@click.group("wiki")
def wiki() -> None:
    """Manage the local memtomem wiki (skills/agents/commands library)."""


def _run_seed_override(
    asset_type: Literal["skills", "agents", "commands"],
    name: str,
    vendor: str,
    *,
    force: bool,
    editor: bool,
) -> None:
    """Shared body for ``mm wiki {skill,agent,command} override``.

    Mirrors the seed → stdout summary → optional stderr warning →
    optional ``$EDITOR`` flow across all three asset types so the trust-UX
    is identical: classified ClickException for known errors, no Python
    traceback leaks, and any vendor-renderer drops surface as a yellow
    stderr line so the user knows what the runtime won't see in the
    override.
    """
    store = WikiStore.at_default()
    try:
        result = seed_override(store, asset_type, name, vendor, force=force)
    except (
        WikiNotFoundError,
        OverrideExistsError,
        FileNotFoundError,
        InvalidNameError,
        NotImplementedError,
    ) as exc:
        # 5 sibling classes (verified disjoint: WikiNotFoundError /
        # OverrideExistsError / NotImplementedError -> RuntimeError;
        # FileNotFoundError -> OSError; InvalidNameError -> ValueError —
        # no cross-inheritance, ordering irrelevant). NotImplementedError
        # carries the ("commands", "codex") placeholder message from
        # seed_override; surfacing it as ClickException prints a classified
        # error rather than a Python traceback.
        raise click.ClickException(str(exc)) from exc

    # ``seed_override`` invariant: target lives under ``store.root``.
    # No is_relative_to fallback — a violation is a real bug worth
    # surfacing as ValueError, not a silent path mismatch to mask.
    rel = result.path.relative_to(store.root)
    ext = result.path.suffix.lstrip(".")
    # ``as_posix()`` matches the hardcoded ``/`` in the ``git add`` hint below.
    click.secho(f"Seeded {rel.as_posix()}", fg="green")
    click.echo(str(result.path))
    click.echo(
        f"# next: cd {store.root} && "
        f"git add {asset_type}/{name}/overrides/{vendor}.{ext} && git commit"
    )

    if result.dropped:
        click.secho(
            f"warning: vendor {vendor!r} will not represent these fields: "
            f"{', '.join(result.dropped)}",
            fg="yellow",
            err=True,
        )

    if editor:
        click.edit(filename=str(result.path), require_save=False)


def _echo_diff_line(line: str) -> None:
    """Colorize one ``difflib.unified_diff`` line the way git does — added
    green, removed red, hunk header cyan, file headers / context plain."""
    text = line.rstrip("\n")
    if line.startswith("+") and not line.startswith("+++"):
        click.secho(text, fg="green")
    elif line.startswith("-") and not line.startswith("---"):
        click.secho(text, fg="red")
    elif line.startswith("@@"):
        click.secho(text, fg="cyan")
    else:
        click.echo(text)


def _note_dropped(dropped: list[str], vendor: str) -> None:
    """Stderr note listing canonical fields the vendor format cannot carry.

    ``diff`` surfaces these so a side-by-side reader is not surprised that an
    override never contains them — the override could not represent them even
    if the user wanted. Stderr keeps stdout a clean diff for capture.
    """
    if dropped:
        click.secho(
            f"note: vendor {vendor!r} does not represent: {', '.join(dropped)}",
            fg="yellow",
            err=True,
        )


def _run_diff(
    asset_type: Literal["skills", "agents", "commands"],
    name: str,
    vendor: str,
) -> None:
    """Shared body for ``mm wiki {skill,agent,command} diff``.

    Prints the unified diff between the canonical render and the committed
    override (``mm context diff``-style), classifies wiki / canonical errors
    as a :class:`click.ClickException` so no traceback leaks, and always exits
    0 — ``diff`` is informational, not a gate.
    """
    store = WikiStore.at_default()
    try:
        result = diff_override(store, asset_type, name, vendor)
    except (
        WikiNotFoundError,
        FileNotFoundError,
        InvalidNameError,
        NotImplementedError,
        ValueError,
    ) as exc:
        # Same disjoint sibling set as ``_run_seed_override`` plus ValueError
        # for an unregistered (asset_type, vendor); ordering irrelevant.
        raise click.ClickException(str(exc)) from exc

    # ``override_path`` is built under ``store.root`` by construction — a
    # violation is a real bug worth surfacing, so no is_relative_to fallback.
    rel = result.override_path.relative_to(store.root).as_posix()
    if not result.exists:
        click.secho(f"No override at {rel}", fg="yellow")
        click.echo(
            f"# seed one: mm wiki {asset_type.removesuffix('s')} override {name} --vendor {vendor}"
        )
    elif result.in_sync:
        click.secho(f"{rel} is in sync with the canonical render.", fg="green")
    else:
        for line in result.diff_lines:
            _echo_diff_line(line)

    _note_dropped(result.dropped, vendor)


def _run_lint(
    asset_type: Literal["skills", "agents", "commands"],
    name: str,
    vendor: str | None,
) -> None:
    """Shared body for ``mm wiki {skill,agent,command} lint``.

    Prints one line per finding to stdout and exits non-zero when the report
    carries any error, so the verb is usable as a CI gate. The whole report
    is the output; the exit code is the machine signal. Only the absent-wiki
    case is a :class:`click.ClickException` (it is not asset-specific).
    """
    store = WikiStore.at_default()
    try:
        report = lint_asset(store, asset_type, name, vendor)
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    for finding in report.findings:
        if finding.level == "error":
            click.secho(f"  error: {finding.message}", fg="red")
        else:
            click.secho(f"  warning: {finding.message}", fg="yellow")

    target = f"{asset_type}/{name}"
    if report.ok:
        n_warn = sum(1 for f in report.findings if f.level == "warning")
        suffix = f" ({n_warn} warning{'s' if n_warn != 1 else ''})" if n_warn else ""
        click.secho(f"{target}: OK{suffix}", fg="green")
        return
    n_err = sum(1 for f in report.findings if f.level == "error")
    click.secho(f"{target}: lint failed ({n_err} error{'s' if n_err != 1 else ''})", fg="red")
    click.get_current_context().exit(1)


@wiki.command("init")
@click.option(
    "--from",
    "from_url",
    metavar="GIT_URL",
    default=None,
    help="Clone the wiki from a git URL instead of initializing from scratch.",
)
def init_cmd(from_url: str | None) -> None:
    """Create or clone the wiki at ``~/.memtomem-wiki/``."""
    store = WikiStore.at_default()
    try:
        if from_url:
            store.init_from_url(from_url)
            click.secho(f"Cloned wiki from {from_url} → {store.root}", fg="green")
        else:
            store.init()
            click.secho(f"Initialized wiki at {store.root}", fg="green")
            click.echo("  Layout: skills/, agents/, commands/")
            click.echo("  Run `mm wiki list` or `mm wiki --help` to see what is available.")
    except WikiAlreadyExistsError as exc:
        raise click.ClickException(str(exc)) from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@wiki.command("list")
@click.option(
    "--type",
    "asset_type",
    type=click.Choice(WIKI_ASSET_TYPES),
    default=None,
    help="Restrict output to one asset kind.",
)
def list_cmd(asset_type: str | None) -> None:
    """List skills, agents, and commands in the wiki."""
    store = WikiStore.at_default()
    try:
        assets = store.list_assets(asset_type)
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    if not assets:
        scope = asset_type or "any kind"
        click.echo(f"  (no assets of {scope})")
        return

    click.secho(f"Wiki: {store.root}", fg="cyan")
    click.echo(f"  HEAD: {store.current_commit()[:12]}")
    click.echo("")
    last_type: str | None = None
    for asset in assets:
        if asset.type != last_type:
            click.secho(f"  {asset.type}/", fg="cyan")
            last_type = asset.type
        click.echo(f"    {asset.name}")


# ── Skill subgroup ──────────────────────────────────────────────────────


@wiki.group("skill")
def skill_group() -> None:
    """Per-skill operations on the wiki (override seeding, ...)."""


@skill_group.command("override")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    type=click.Choice(_SKILL_VENDORS),
    required=True,
    help="Which runtime this override targets.",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite existing override file in the wiki (creates .bak).",
)
@click.option(
    "--editor",
    "-e",
    is_flag=True,
    help="Open $EDITOR on the seeded file after writing.",
)
def skill_override_cmd(name: str, vendor: str, force: bool, editor: bool) -> None:
    """Seed a wiki override file from the canonical skill content.

    ``mm wiki skill override <name> --vendor <claude|gemini|codex|kimi>`` writes
    ``<wiki>/skills/<name>/overrides/<vendor>.md`` using the canonical
    ``SKILL.md`` as the working baseline. Edit the file (``--editor`` opens
    ``$EDITOR``) and commit it inside the wiki repo so that future
    ``mm context install`` snapshots pick it up.
    """
    _run_seed_override("skills", name, vendor, force=force, editor=editor)


@skill_group.command("diff")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    type=click.Choice(_SKILL_VENDORS),
    required=True,
    help="Which runtime override to diff against the canonical render.",
)
def skill_diff_cmd(name: str, vendor: str) -> None:
    """Show how a skill override diverges from the canonical render.

    ``mm wiki skill diff <name> --vendor <vendor>`` re-renders the canonical
    ``SKILL.md`` the way ``override`` would seed it and prints a unified diff
    against the committed ``overrides/<vendor>.md`` — surfacing both your
    hand-edits and any canonical drift since the override was seeded. Exits 0.
    """
    _run_diff("skills", name, vendor)


@skill_group.command("lint")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    type=click.Choice(_SKILL_VENDORS),
    default=None,
    help="Restrict representability checks to one runtime (default: every override on disk).",
)
def skill_lint_cmd(name: str, vendor: str | None) -> None:
    """Validate a wiki skill is well-formed and installable.

    Checks the name, the canonical ``SKILL.md`` presence, and (per vendor)
    representability + override UTF-8 validity. Exits non-zero on any error
    so it is usable as a CI gate; dropped-field warnings leave the exit 0.
    """
    _run_lint("skills", name, vendor)


# ── Agent subgroup ──────────────────────────────────────────────────────


@wiki.group("agent")
def agent_group() -> None:
    """Per-agent operations on the wiki (override seeding, ...)."""


@agent_group.command("override")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    type=click.Choice(_AGENT_VENDORS),
    required=True,
    help="Which runtime this override targets.",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite existing override file in the wiki (creates .bak).",
)
@click.option(
    "--editor",
    "-e",
    is_flag=True,
    help="Open $EDITOR on the seeded file after writing.",
)
def agent_override_cmd(name: str, vendor: str, force: bool, editor: bool) -> None:
    """Seed a wiki override file from the canonical agent content.

    ``mm wiki agent override <name> --vendor <claude|gemini|codex|kimi>`` writes
    ``<wiki>/agents/<name>/overrides/<vendor>.<ext>``. Bytes come from the
    vendor renderer applied to the canonical ``agent.md`` so the seed
    matches what the runtime would produce. Fields the vendor format
    cannot represent (e.g. gemini agents drop ``skills`` / ``isolation``)
    are surfaced via a stderr warning so the editor knows what the
    runtime won't see.
    """
    _run_seed_override("agents", name, vendor, force=force, editor=editor)


@agent_group.command("diff")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    type=click.Choice(_AGENT_VENDORS),
    required=True,
    help="Which runtime override to diff against the canonical render.",
)
def agent_diff_cmd(name: str, vendor: str) -> None:
    """Show how an agent override diverges from the canonical render.

    ``mm wiki agent diff <name> --vendor <vendor>`` feeds the canonical
    ``agent.md`` through the vendor renderer (the same path ``override``
    uses) and prints a unified diff against ``overrides/<vendor>.<ext>``.
    Exits 0; canonical fields the vendor cannot represent are noted on stderr.
    """
    _run_diff("agents", name, vendor)


@agent_group.command("lint")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    type=click.Choice(_AGENT_VENDORS),
    default=None,
    help="Restrict representability checks to one runtime (default: every override on disk).",
)
def agent_lint_cmd(name: str, vendor: str | None) -> None:
    """Validate a wiki agent is well-formed and installable.

    Checks the name, that the canonical ``agent.md`` is present and parses,
    and (per vendor) representability + override UTF-8 validity. Exits
    non-zero on any error; dropped-field warnings leave the exit 0.
    """
    _run_lint("agents", name, vendor)


# ── Command subgroup ────────────────────────────────────────────────────


@wiki.group("command")
def command_group() -> None:
    """Per-command operations on the wiki (override seeding, ...)."""


@command_group.command("override")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    type=click.Choice(_COMMAND_VENDORS),
    required=True,
    help="Which runtime this override targets.",
)
@click.option(
    "--force",
    "-f",
    is_flag=True,
    help="Overwrite existing override file in the wiki (creates .bak).",
)
@click.option(
    "--editor",
    "-e",
    is_flag=True,
    help="Open $EDITOR on the seeded file after writing.",
)
def command_override_cmd(name: str, vendor: str, force: bool, editor: bool) -> None:
    """Seed a wiki override file from the canonical command content.

    ``mm wiki command override <name> --vendor <claude|gemini|codex>`` writes
    ``<wiki>/commands/<name>/overrides/<vendor>.<ext>``. ``--vendor codex``
    is a permanent placeholder (no ``codex_commands`` generator); the
    command surfaces a classified error rather than silently failing.
    Fields the vendor format cannot represent (e.g. gemini commands drop
    ``argument-hint`` / ``allowed-tools`` / ``model``) are surfaced via a
    stderr warning so the editor knows what the runtime won't see.
    """
    _run_seed_override("commands", name, vendor, force=force, editor=editor)


@command_group.command("diff")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    type=click.Choice(_COMMAND_VENDORS),
    required=True,
    help="Which runtime override to diff against the canonical render.",
)
def command_diff_cmd(name: str, vendor: str) -> None:
    """Show how a command override diverges from the canonical render.

    ``mm wiki command diff <name> --vendor <vendor>`` feeds the canonical
    ``command.md`` through the vendor renderer and prints a unified diff
    against ``overrides/<vendor>.<ext>``. ``--vendor codex`` is a permanent
    placeholder (no ``codex_commands`` generator) and surfaces a classified
    error rather than a traceback. Exits 0 on a real diff.
    """
    _run_diff("commands", name, vendor)


@command_group.command("lint")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    type=click.Choice(_COMMAND_VENDORS),
    default=None,
    help="Restrict representability checks to one runtime (default: every override on disk).",
)
def command_lint_cmd(name: str, vendor: str | None) -> None:
    """Validate a wiki command is well-formed and installable.

    Checks the name, that the canonical ``command.md`` is present and parses,
    and (per vendor) representability + override UTF-8 validity. A committed
    ``codex`` command override is an error (no generator can render it).
    Exits non-zero on any error; dropped-field warnings leave the exit 0.
    """
    _run_lint("commands", name, vendor)
