"""CLI review queue for candidate memories."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

import click


@click.group("review")
def review() -> None:
    """Generate and adjudicate review-first memory candidates."""


@review.command("scan")
@click.argument("session_id")
def scan(session_id: str) -> None:
    """Generate candidates from one exact session."""
    asyncio.run(_scan(session_id))


async def _scan(session_id: str) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.formation import scan_session_candidates

    async with cli_components() as comp:
        candidates = await scan_session_candidates(comp.storage, session_id)
        click.echo(
            json.dumps({"created": len(candidates), "candidates": candidates}, ensure_ascii=False)
        )


@review.command("list")
@click.option("--status", default="pending")
@click.option("--limit", type=click.IntRange(min=1, max=1000), default=100)
def list_candidates(status: str, limit: int) -> None:
    """List candidates by state."""
    asyncio.run(_list_candidates(status, limit))


async def _list_candidates(status: str, limit: int) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        click.echo(
            json.dumps(
                await comp.storage.list_memory_candidates(status=status, limit=limit),
                ensure_ascii=False,
            )
        )


@review.command("show")
@click.argument("candidate_id")
def show(candidate_id: str) -> None:
    """Show one candidate and its evidence."""
    asyncio.run(_show(candidate_id))


async def _show(candidate_id: str) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        candidate = await comp.storage.get_memory_candidate(candidate_id)
        if candidate is None:
            raise click.ClickException("Candidate not found")
        click.echo(json.dumps(candidate, ensure_ascii=False, indent=2))


async def _decide(candidate_id: str, decision: str, reviewer: str, reason: str) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.pinned import PinnedContextStore
    from memtomem.server.tools.search import _resolve_project_context_root
    from memtomem.tools.memory_writer import append_entry

    async with cli_components() as comp:
        candidate = await comp.storage.get_memory_candidate(candidate_id)
        if candidate is None or candidate["status"] != "pending":
            raise click.ClickException("Candidate is not pending")
        if decision == "approved":
            from memtomem import privacy

            guard = privacy.enforce_write_guard(
                candidate["content"], surface="cli_candidate_approve"
            )
            if guard.decision != "pass":
                raise click.ClickException("Candidate now fails the privacy gate")
            claimed = await comp.storage.claim_memory_candidate(candidate_id, reviewer, reason)
            if claimed is None:
                raise click.ClickException("Candidate state changed concurrently")
            try:
                if candidate["destination"] == "pinned":
                    PinnedContextStore(
                        comp.config, project_root=_resolve_project_context_root(comp)
                    ).set(
                        f"candidate-{candidate_id[:8]}",
                        candidate["content"],
                        description=f"Approved {candidate['kind']} candidate",
                    )
                else:
                    from memtomem.context._atomic import (
                        _CRUD_SIDECAR_LOCK_BUDGET_S,
                        _lock_path_for,
                        async_file_lock,
                    )

                    base = Path(comp.config.indexing.memory_dirs[0]).expanduser().resolve()
                    target = base / f"{datetime.now(timezone.utc):%Y-%m-%d}.md"
                    async with async_file_lock(
                        _lock_path_for(target), timeout=_CRUD_SIDECAR_LOCK_BUDGET_S
                    ):
                        await asyncio.to_thread(
                            append_entry,
                            target,
                            candidate["content"],
                            title=f"Approved {candidate['kind']}",
                            tags=["formation-approved", candidate["kind"]],
                        )
                        await comp.index_engine.index_file(
                            target, already_scanned=True, lock_held=True
                        )
            except asyncio.CancelledError:
                await comp.storage.release_memory_candidate(candidate_id)
                raise
            except Exception:
                await comp.storage.release_memory_candidate(candidate_id)
                raise
            if not await comp.storage.finalize_memory_candidate(candidate_id):
                raise click.ClickException("Candidate claim was lost before finalization")
        else:
            changed = await comp.storage.decide_memory_candidate(
                candidate_id, decision, reviewer, reason
            )
            if not changed:
                raise click.ClickException("Candidate state changed concurrently")
        click.echo(json.dumps({"ok": True, "status": decision}))


@review.command("approve")
@click.argument("candidate_id")
@click.option("--reviewer", default="user")
@click.option("--reason", default="")
def approve(candidate_id: str, reviewer: str, reason: str) -> None:
    """Approve and persist a candidate."""
    asyncio.run(_decide(candidate_id, "approved", reviewer, reason))


@review.command("reject")
@click.argument("candidate_id")
@click.option("--reviewer", default="user")
@click.option("--reason", default="")
def reject(candidate_id: str, reviewer: str, reason: str) -> None:
    """Reject a candidate without writing durable memory."""
    asyncio.run(_decide(candidate_id, "rejected", reviewer, reason))
