"""``mm wiki`` — manage the local wiki (``~/.memtomem-wiki/``).

See ADR-0008 for the wiki layer's role in the context-gateway pipeline.
"""

from __future__ import annotations

from typing import Literal

import click

from memtomem import privacy
from memtomem.context._names import (
    OVERRIDE_FORMATS,
    InvalidNameError,
    override_vendors,
    validate_name,
)
from memtomem.wiki import (
    WIKI_ASSET_TYPES,
    WikiAlreadyExistsError,
    WikiNotFoundError,
    WikiStore,
)
from memtomem.wiki.commit import (
    ResolvedTarget,
    WikiTargetChangedError,
    commit_targets,
)
from memtomem.wiki.inspect import (
    diff_override,
    lint_asset,
)
from memtomem.wiki.override import (
    OverrideExistsError,
    canonical_asset_file,
    seed_override,
)
from memtomem.wiki.store import WikiHeadMovedError, _redact_url_userinfo

# ``--vendor`` Choices derive from OVERRIDE_FORMATS (the single source of
# truth) so they never drift from the matrix: kimi is valid for skills/agents
# but not commands. See ADR-0008 "Vendor format matrix". Computed once at
# import — the matrix is a module-level constant.
_SKILL_VENDORS = override_vendors("skills")
_AGENT_VENDORS = override_vendors("agents")
_COMMAND_VENDORS = override_vendors("commands")


@click.group("wiki")
def wiki() -> None:
    """Manage the host-global wiki (~/.memtomem-wiki) of canonical skills, agents, and commands.

    These commands edit, check, and commit the host-global wiki itself. Use
    mm context install / mm context update to copy committed wiki artifacts into
    a project's .memtomem/ directory.
    """


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
    # ``as_posix()`` keeps the displayed relative path forward-slashed on Windows.
    click.secho(f"Seeded {rel.as_posix()}", fg="green")
    click.echo(str(result.path))
    # Steer to the target-isolated commit, not a raw ``git add``/``git commit``
    # (which would sweep unrelated staged changes). ``override`` only seeds the
    # vendor file, so the hint points at ``--vendor`` — never ``--canonical``.
    singular = asset_type[:-1]  # "skills" -> "skill": the per-type CLI verb is singular
    click.echo(f"# next: mm wiki {singular} commit {name} --vendor {vendor}")
    click.echo(
        f"# then: cd <project> && mm context install {singular} {name}"
        "   # or update an installed copy"
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


def _run_commit(
    asset_type: Literal["skills", "agents", "commands"],
    name: str,
    vendors: tuple[str, ...],
    *,
    canonical: bool,
    message: str | None,
) -> None:
    """Shared body for ``mm wiki {skill,agent,command} commit``.

    Parity with the web Commit affordance (ADR-0027 §3): commits ONLY the
    selected canonical / override paths layered onto HEAD via the shared
    :func:`memtomem.wiki.commit.commit_targets` engine — never a bare
    ``git add . && git commit`` that would sweep unrelated staged changes. The
    paths are server-resolved from the typed ``--canonical`` / ``--vendor`` flags
    (a raw path is never accepted), and every engine error maps to a classified
    :class:`click.ClickException` so no traceback — and no absolute wiki path —
    leaks. Unlike the web route there is no client Save token, so the commit
    takes the bytes currently on disk and lands on the freshest HEAD (the
    cross-process lock + ref CAS still guard a concurrent ``mm web`` / second
    ``mm wiki`` commit).
    """
    store = WikiStore.at_default()
    try:
        store.require_exists()
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc

    try:
        validate_name(name, kind=f"{asset_type.removesuffix('s')} name")
    except InvalidNameError as exc:
        raise click.ClickException(str(exc)) from exc

    if not canonical and not vendors:
        raise click.ClickException(
            "nothing to commit: pass --canonical and/or --vendor <vendor> to select targets"
        )

    targets: list[ResolvedTarget] = []
    if canonical:
        path = canonical_asset_file(store, asset_type, name)
        targets.append(ResolvedTarget(rel=path.relative_to(store.root).as_posix(), path=path))
    for vendor in vendors:
        fmt = OVERRIDE_FORMATS.get((asset_type, vendor))
        if fmt is None:
            # Unreachable via the Click Choices (derived from OVERRIDE_FORMATS),
            # but stay classified rather than KeyError if called directly.
            raise click.ClickException(f"no override format registered for vendor {vendor!r}")
        _, ext = fmt
        path = store.root / asset_type / name / "overrides" / f"{vendor}.{ext}"
        targets.append(ResolvedTarget(rel=path.relative_to(store.root).as_posix(), path=path))

    # Friendly pre-check: a selected file that never existed on disk gets a clear
    # "create it first" message instead of the engine's generic
    # WikiTargetChangedError(rel, 0). Re-checked authoritatively under the lock.
    missing = [t.rel for t in targets if not t.path.is_file()]
    if missing:
        raise click.ClickException(
            "no such file in the wiki: "
            + ", ".join(missing)
            + " — create the canonical or seed an override before committing"
        )

    msg = (message or "").strip() or f"wiki: update {asset_type}/{name}"
    privacy_warning = len(privacy.scan(msg))

    try:
        outcome = commit_targets(store, targets, message=msg, expected_head=None)
    except WikiTargetChangedError as exc:
        raise click.ClickException(
            f"{exc.rel} changed on disk during the commit; re-run to pick up the new bytes"
        ) from exc
    except WikiHeadMovedError as exc:
        raise click.ClickException(
            f"the wiki HEAD moved during the commit ({exc}); re-run to commit onto the new HEAD"
        ) from exc
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except TimeoutError as exc:
        # The shared cross-process wiki lock is held past ``_COMMIT_LOCK_TIMEOUT``
        # by a concurrent committer (another ``mm wiki`` / an ``mm web`` commit).
        # ``TimeoutError`` is an ``OSError``, not a ``RuntimeError``, so it needs
        # its own clause; the web route maps the same path to a 503.
        raise click.ClickException(
            "wiki commit timed out — another wiki operation may be in progress; retry shortly"
        ) from exc
    except RuntimeError as exc:
        # Covers WikiDetachedHeadError (its str() IS the friendly, actionable
        # "check out a branch" message — the push/pull precedent) and any git
        # failure (e.g. missing git identity) — surface the error the way the
        # sibling ``mm wiki init`` does. The embedded wiki path is the user's
        # own local path, not a secret as it would be in the web route's HTTP
        # response (which uses a fixed, path-free message instead).
        raise click.ClickException(str(exc)) from exc

    rels = ", ".join(t.rel for t in targets)
    if outcome.committed:
        click.secho(f"Committed {outcome.wiki_head[:12]} ({rels})", fg="green")
    else:
        click.secho(f"Nothing to commit — {rels} already match HEAD.", fg="yellow")

    if privacy_warning:
        click.secho(
            f"warning: commit message has {privacy_warning} possible "
            f"secret/PII match{'es' if privacy_warning != 1 else ''} (committed anyway)",
            fg="yellow",
            err=True,
        )


@wiki.command("init")
@click.option(
    "--from",
    "from_url",
    metavar="GIT_URL",
    default=None,
    help="Clone the wiki from a git URL instead of initializing from scratch.",
)
def init_cmd(from_url: str | None) -> None:
    """Create or clone the wiki at ~/.memtomem-wiki/."""
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


# ── Remote / backup ─────────────────────────────────────────────────────
#
# Thin wrappers over git (ADR-0008: "git remotes — no new sync protocol"): they
# surface git's own errors and own no merge/conflict resolution. The wiki is a
# normal git repo, so anything these don't cover is plain `git -C <wiki> ...`.


@wiki.command("remote")
@click.argument("url", required=False, default=None)
def remote_cmd(url: str | None) -> None:
    """Show or set the wiki's backup remote ('origin').

    With no argument, prints the configured origin URL (credentials redacted).
    With a git URL, configures origin so `mm wiki push` / `mm wiki pull` can back
    up and restore the wiki across machines.

    WARNING: a URL with embedded credentials (https://user:token@host/...) is
    stored as plaintext in the wiki's .git/config — prefer SSH keys or a git
    credential helper.
    """
    store = WikiStore.at_default()
    try:
        if url is None:
            current = store.remote_url()
            if current is None:
                click.secho("No wiki remote configured.", fg="yellow")
                click.echo("# set one: mm wiki remote <git-url>")
                return
            click.echo(f"origin\t{_redact_url_userinfo(current)}")
            return
        action = store.set_remote(url)
        click.secho(
            f"Set wiki remote 'origin' → {_redact_url_userinfo(url)} ({action})",
            fg="green",
        )
        click.echo("# back up: mm wiki push   # restore elsewhere: mm wiki pull")
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc


@wiki.command("push")
def push_cmd() -> None:
    """Push the wiki to its backup remote ('origin').

    Thin pass-through to `git push origin <branch>`. memtomem owns no conflict
    resolution: if git rejects the push (e.g. the remote moved), git's own
    message — which already tells you to `mm wiki pull` / `git pull` first —
    is surfaced verbatim. Configure the remote once with `mm wiki remote <url>`.
    """
    store = WikiStore.at_default()
    try:
        output = store.push()
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except RuntimeError as exc:
        # Covers WikiDetachedHeadError, the no-remote precondition, and any git
        # failure (non-fast-forward, auth, unborn branch). str(exc) is already
        # credential-redacted at the _git boundary; the local wiki path it may
        # carry is the user's own (the init/commit CLI convention).
        raise click.ClickException(str(exc)) from exc
    if output:
        click.echo(output)
    click.secho("Pushed.", fg="green")


@wiki.command("pull")
def pull_cmd() -> None:
    """Pull the wiki from its backup remote ('origin').

    Thin pass-through to `git pull origin <branch>` (a normal merge). On a merge
    conflict or dirty working tree git stops and leaves the wiki for you to
    resolve with ordinary git — memtomem owns no conflict resolution; git's own
    message is surfaced verbatim. To restore onto a fresh machine instead, clone
    with `mm wiki init --from <url>`.
    """
    store = WikiStore.at_default()
    try:
        output = store.pull()
    except WikiNotFoundError as exc:
        raise click.ClickException(str(exc)) from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    if output:
        click.echo(output)
    click.secho("Pulled.", fg="green")


# ── Skill subgroup ──────────────────────────────────────────────────────


@wiki.group("skill")
def skill_group() -> None:
    """Manage wiki skills.

    Seed vendor overrides, then diff, lint, and commit selected paths.
    """


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
    ``$EDITOR``), then record it with
    ``mm wiki skill commit <name> --vendor <vendor>`` (or the in-browser Commit
    button) so a later ``mm context install`` can snapshot it — no raw ``git``
    needed.
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


@skill_group.command("commit")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    "vendors",
    type=click.Choice(_SKILL_VENDORS),
    multiple=True,
    help="Commit this vendor's override file (repeatable).",
)
@click.option(
    "--canonical",
    "-c",
    is_flag=True,
    help="Also commit the canonical SKILL.md.",
)
@click.option(
    "--message",
    "-m",
    default=None,
    help="Commit message (default: 'wiki: update skills/<name>').",
)
def skill_commit_cmd(
    name: str, vendors: tuple[str, ...], canonical: bool, message: str | None
) -> None:
    """Commit a skill's canonical and/or override files as one isolated wiki commit.

    Parity with the web Commit affordance (ADR-0027 §3): commits ONLY the
    selected paths layered onto HEAD — never a bare ``git add . && git commit``
    that would sweep unrelated staged changes. Edit the files first (e.g.
    ``mm wiki skill override <name> --vendor <v> --editor``), then select targets
    with ``--canonical`` and/or one or more ``--vendor`` flags.
    """
    _run_commit("skills", name, vendors, canonical=canonical, message=message)


# ── Agent subgroup ──────────────────────────────────────────────────────


@wiki.group("agent")
def agent_group() -> None:
    """Manage wiki agents.

    Seed vendor overrides, then diff, lint, and commit selected paths.
    """


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
    runtime won't see. Record the seeded override with
    ``mm wiki agent commit <name> --vendor <vendor>`` so a later
    ``mm context install`` can snapshot it.
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


@agent_group.command("commit")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    "vendors",
    type=click.Choice(_AGENT_VENDORS),
    multiple=True,
    help="Commit this vendor's override file (repeatable).",
)
@click.option(
    "--canonical",
    "-c",
    is_flag=True,
    help="Also commit the canonical agent.md.",
)
@click.option(
    "--message",
    "-m",
    default=None,
    help="Commit message (default: 'wiki: update agents/<name>').",
)
def agent_commit_cmd(
    name: str, vendors: tuple[str, ...], canonical: bool, message: str | None
) -> None:
    """Commit an agent's canonical and/or override files as one isolated wiki commit.

    Parity with the web Commit affordance (ADR-0027 §3): commits ONLY the
    selected paths layered onto HEAD — never a bare ``git add . && git commit``
    that would sweep unrelated staged changes. Edit the files first (e.g.
    ``mm wiki agent override <name> --vendor <v> --editor``), then select targets
    with ``--canonical`` and/or one or more ``--vendor`` flags.
    """
    _run_commit("agents", name, vendors, canonical=canonical, message=message)


# ── Command subgroup ────────────────────────────────────────────────────


@wiki.group("command")
def command_group() -> None:
    """Manage wiki commands.

    Seed vendor overrides, then diff, lint, and commit selected paths.
    """


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
    stderr warning so the editor knows what the runtime won't see. Record the
    seeded override with ``mm wiki command commit <name> --vendor <vendor>`` so
    a later ``mm context install`` can snapshot it.
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


@command_group.command("commit")
@click.argument("name")
@click.option(
    "--vendor",
    "-v",
    "vendors",
    type=click.Choice(_COMMAND_VENDORS),
    multiple=True,
    help="Commit this vendor's override file (repeatable).",
)
@click.option(
    "--canonical",
    "-c",
    is_flag=True,
    help="Also commit the canonical command.md.",
)
@click.option(
    "--message",
    "-m",
    default=None,
    help="Commit message (default: 'wiki: update commands/<name>').",
)
def command_commit_cmd(
    name: str, vendors: tuple[str, ...], canonical: bool, message: str | None
) -> None:
    """Commit a command's canonical and/or override files as one isolated wiki commit.

    Parity with the web Commit affordance (ADR-0027 §3): commits ONLY the
    selected paths layered onto HEAD — never a bare ``git add . && git commit``
    that would sweep unrelated staged changes. Edit the files first (e.g.
    ``mm wiki command override <name> --vendor <v> --editor``), then select
    targets with ``--canonical`` and/or one or more ``--vendor`` flags.
    """
    _run_commit("commands", name, vendors, canonical=canonical, message=message)
