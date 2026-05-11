"""CLI: ``mm mem`` subcommand group.

ADR-0011 follow-up (issue #885). ``mm mem rescan`` re-runs the LTM trust-
boundary content scan over already-stored chunks so a deployment can audit
``project_shared`` content against the current ``DEFAULT_PATTERNS`` without
re-embedding or recreating chunks. The rescan is **privacy-only** by design:
chunk identity, content, validity windows, and access stats are not
touched. Quarantine / soft-delete is a v2 concern (issue #885 follow-up).

``mm add`` / ``mm recall`` intentionally remain top-level CLI commands —
folding them into ``mm mem`` is a separate UX migration tracked outside
this issue.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import get_args

import click

from memtomem import privacy
from memtomem.config import TargetScope


_MEMORY_SCOPE_CHOICES = list(get_args(TargetScope))


@click.group("mem")
def mem() -> None:
    """Audit and inspect stored memories."""


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

    try:
        result = asyncio.run(
            _rescan(
                scope=scope,
                source_exact=source_exact,
                source_prefix=source_prefix,
            )
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

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
) -> tuple[int, list[dict]]:
    from memtomem.cli._bootstrap import cli_components

    scanned = 0
    violations: list[dict] = []

    async with cli_components() as comp:
        async for row in comp.storage.iter_chunks_for_audit(
            scope=scope,
            source_exact=source_exact,
            source_prefix=source_prefix,
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
