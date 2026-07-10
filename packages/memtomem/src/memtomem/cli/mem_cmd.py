"""CLI: ``mm mem`` subcommand group.

ADR-0011 follow-up (issue #885). ``mm mem rescan`` re-runs the LTM trust-
boundary content scan over already-stored chunks so a deployment can audit
``project_shared`` content against the current ``DEFAULT_PATTERNS`` without
re-embedding or recreating chunks. The rescan is **privacy-only** by design:
chunk identity, content, validity windows, and access stats are not
touched. Quarantine / soft-delete is a v2 concern (issue #885 follow-up).

Issue #934 (ADR-0011 / ADR-0016 follow-up) wires the three-tier model
into the rescan: ``--scope=project_shared`` / ``--scope=project_local``
require a project context (``.git`` or ``pyproject.toml`` marker) so the
audit cannot accidentally walk chunks owned by an unrelated project in
the same SQLite database. ``--scope=user`` stays global by design.

``mm add`` / ``mm recall`` intentionally remain top-level CLI commands —
folding them into ``mm mem`` is a separate UX migration tracked outside
this issue.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast, get_args

import click

from memtomem.cli._errors import raise_cli_error

from memtomem import privacy
from memtomem.cli.context_cmd import _append_gitignore_marker, _find_project_root
from memtomem.config import TargetScope, register_project_memory_dir
from memtomem.memory_scope import resolve_memory_scope_dir


_MEMORY_SCOPE_CHOICES = list(get_args(TargetScope))


@click.group("mem")
def mem() -> None:
    """Audit, inspect, and set up stored memories."""


def _resolve_source_filter(source: str) -> tuple[Path | None, Path | None]:
    """Resolve a user-supplied ``--source`` to (exact, prefix) filters.

    Component-aware, normalized. Per the plan: cwd-relative resolve →
    file becomes ``source_exact``, directory becomes ``source_prefix``,
    nonexistent path is a hard fail. No fuzzy / substring matching.
    """
    resolved = (Path.cwd() / source).resolve(strict=False)
    if not resolved.exists():
        raise click.BadParameter(
            f"--source path does not exist: {resolved}",
            param_hint="--source",
        )
    if resolved.is_file():
        return resolved, None
    if resolved.is_dir():
        return None, resolved
    raise click.BadParameter(
        f"--source is not a file or directory: {resolved}",
        param_hint="--source",
    )


@mem.command("init")
@click.option(
    "--scope",
    type=click.Choice(["project_local", "project_shared"]),
    default="project_local",
    show_default=True,
    help=(
        "Memory tier to initialize. project_local writes to the gitignored "
        "<project>/.memtomem/memories.local/; project_shared writes to "
        "<project>/.memtomem/memories/ (files written there will be "
        "git-tracked) and requires --confirm-project-shared."
    ),
)
@click.option(
    "--confirm-project-shared",
    is_flag=True,
    default=False,
    help=(
        "Confirm initializing the project_shared tier: memories written "
        "there land in git-tracked files. Required (or answered "
        "interactively) when --scope=project_shared."
    ),
)
def init_cmd(scope: str, confirm_project_shared: bool) -> None:
    """Create and register the project memory tier for the current project.

    ADR-0011 project-tier writes (``mm add`` / MCP ``mem_add`` with
    ``scope=project_local|project_shared``) require the tier directory to
    be registered in ``IndexingConfig.project_memory_dirs`` — a deliberate
    trust gate so unregistered project trees are never silently picked up
    by the indexer. This command is the explicit opt-in: it creates
    ``<project>/.memtomem/memories[.local]`` and appends it to
    ``indexing.project_memory_dirs`` in ``~/.memtomem/config.json``
    (locked, atomic, idempotent).

    Registration is an explicit trust operation, so a real project root
    (``.git`` or ``pyproject.toml`` marker) is required — run ``git init``
    first in a scratch directory. For ``project_local`` the ``.gitignore``
    guard block is established *before* registration; a failed
    ``.gitignore`` write aborts so the local tier is never registered
    unprotected. Deliberately CLI-only: exposing registration over MCP
    would let the same principal the gate blocks self-authorize (#1700).
    """
    root = _find_project_root()
    has_signal = (root / ".git").exists() or (root / "pyproject.toml").exists()
    if not has_signal:
        raise click.ClickException(
            f"mm mem init requires a project root (with .git or pyproject.toml); "
            f"none found at or above {root}. Run `git init` first."
        )

    # Gate B — same disclosure as ``mm context init --scope=project_shared``
    # (ADR-0011 §5). Registration only creates an empty directory, but it
    # authorizes future git-tracked writes, so confirm up front.
    if scope == "project_shared" and not confirm_project_shared:
        prompt = (
            f"\n--scope=project_shared: memories written under "
            f"{root}/.memtomem/memories/ will be git-tracked. Continue?"
        )
        if not click.confirm(prompt, default=False):
            raise click.Abort()

    tier_dir = resolve_memory_scope_dir(cast(TargetScope, scope), root)

    # project_local: git protection FIRST, registration last. A
    # ``.gitignore`` is only meaningful inside a git working tree, so
    # requiring ``.git`` here is a hard gate — registering the draft tier in
    # a pyproject-only project would leave it exposed the moment the user
    # runs ``git init`` (the guard block was skipped, so the local memories
    # would be tracked). Refuse instead of warning-and-registering.
    if scope == "project_local":
        if not (root / ".git").exists():
            raise click.ClickException(
                f"--scope=project_local needs a git repository at {root} so the "
                "draft tier stays gitignored; without one a later `git init` "
                "would start tracking it. Run `git init` first, then re-run "
                "(or use --scope=project_shared for a git-tracked tier)."
            )
        try:
            wrote, _msg = _append_gitignore_marker(root)
        except OSError as exc:
            raise click.ClickException(
                f"could not append the .gitignore guard block at {root}/.gitignore: "
                f"{exc}. Aborting before registration so the project_local tier "
                "is never registered without git protection."
            ) from exc
        if wrote:
            click.secho(
                "  Appended .gitignore marker (.memtomem/*.local/, .memtomem/.staging/)",
                fg="green",
            )

    created = not tier_dir.exists()
    tier_dir.mkdir(parents=True, exist_ok=True)
    if created:
        click.secho(f"  Created {tier_dir}", fg="green")

    try:
        newly_registered = register_project_memory_dir(tier_dir)
    except TimeoutError as exc:
        raise click.ClickException(
            "another process holds the config lock (~/.memtomem/config.json); retry in a moment."
        ) from exc
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc

    if newly_registered:
        click.secho(
            "  Registered in indexing.project_memory_dirs (~/.memtomem/config.json)",
            fg="green",
        )
        click.echo(
            f'  You can now run `mm add "..." --scope {scope}` from inside '
            f"{root}.\n"
            "  note: a running MCP server / mm web picks up the new tier "
            "after restart."
        )
    elif not created:
        click.echo(f"Already initialized: {tier_dir} exists and is registered. Nothing to do.")
    else:
        click.echo(f"  {tier_dir} was already registered; directory recreated.")


@mem.command("rescan")
@click.option(
    "--scope",
    type=click.Choice(_MEMORY_SCOPE_CHOICES),
    required=True,
    help=(
        "Memory scope tier to audit. Required — there is no implicit default "
        "so audits are explicit and CI-readable. The value is forwarded to "
        "enforce_write_guard's scope= argument."
    ),
)
@click.option(
    "--source",
    type=str,
    default=None,
    help=(
        "Optional path filter. File: exact match. Directory: descendant "
        "prefix. Resolved against the current working directory. No "
        "fuzzy / substring matching."
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
    help="Suppress per-chunk lines in human output; only print the summary.",
)
def rescan_cmd(
    scope: TargetScope,
    source: str | None,
    as_json: bool,
    quiet: bool,
) -> None:
    """Re-run the privacy guard over stored chunks for ``--scope``.

    Read-only: no chunk is created, deleted, or re-embedded. The guard is
    invoked with ``record_outcome=False`` so the rescan does not double-
    count outcomes or emit bypass audit lines. Decision values reported
    in v1 are ``"pass"`` or ``"blocked"``; ``"bypassed"`` /
    ``"blocked_project_shared"`` cannot fire because v1 always passes
    ``force_unsafe=False``.

    Exit codes: 0 if no violations, 1 if any violation found.
    """
    source_exact: Path | None = None
    source_prefix: Path | None = None
    if source is not None:
        source_exact, source_prefix = _resolve_source_filter(source)

    # ADR-0011 / ADR-0016 / issue #934 cross-project isolation gate.
    # Project tiers must run from inside a real project so the audit
    # cannot accidentally walk chunks owned by a sibling project that
    # shares the same SQLite DB. Mirror ``mm context init --scope``'s
    # marker check at ``cli/context_cmd.py:737-748``.
    project_root: Path | None = None
    if scope != "user":
        root = _find_project_root()
        has_signal = (root / ".git").exists() or (root / "pyproject.toml").exists()
        if not has_signal:
            raise click.ClickException(
                f"--scope={scope} requires a project root (with .git or "
                "pyproject.toml). Use --scope=user from outside a project, "
                "or run from inside one."
            )
        project_root = root

    try:
        result = asyncio.run(
            _rescan(
                scope=scope,
                source_exact=source_exact,
                source_prefix=source_prefix,
                project_root=project_root,
            )
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise_cli_error(exc)

    scanned, violations = result
    if as_json:
        payload = {
            "scope": scope,
            "scanned": scanned,
            "violations": violations,
        }
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if not quiet:
            click.echo(f"scanning {scanned} chunks in scope={scope}...")
            for v in violations:
                click.echo(
                    f"✗ {v['source']} chunk_id={v['chunk_id']} "
                    f"scope={v['scope']} (decision={v['decision']}, "
                    f"{len(v['hits'])} hit"
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
            f"{scanned} chunk{'s' if scanned != 1 else ''} scanned. "
            f"Exit {1 if violations else 0}."
        )

    if violations:
        # Exit code 1 communicated to CI / pre-commit.
        raise SystemExit(1)


async def _rescan(
    *,
    scope: TargetScope,
    source_exact: Path | None,
    source_prefix: Path | None,
    project_root: Path | None,
) -> tuple[int, list[dict]]:
    from memtomem.cli._bootstrap import cli_components

    scanned = 0
    violations: list[dict] = []

    async with cli_components() as comp:
        async for row in comp.storage.iter_chunks_for_audit(
            scope=scope,
            source_exact=source_exact,
            source_prefix=source_prefix,
            project_root=project_root,
        ):
            scanned += 1
            guard = privacy.enforce_write_guard(
                row.content,
                surface="cli_mm_rescan",
                force_unsafe=False,
                scope=row.scope,
                audit_context={
                    "kind": "rescan",
                    "scope": row.scope,
                    "chunk_id": row.chunk_id,
                    "source": str(row.source),
                },
                record_outcome=False,
            )
            if guard.decision == "pass":
                continue
            violations.append(
                {
                    "chunk_id": row.chunk_id,
                    "source": str(row.source),
                    "scope": row.scope,
                    "decision": guard.decision,
                    "hits": [
                        {
                            "pattern_index": h.pattern_index,
                            "span_start": h.span[0],
                            "span_end": h.span[1],
                        }
                        for h in guard.hits
                    ],
                }
            )

    return scanned, violations
