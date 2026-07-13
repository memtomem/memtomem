"""CLI review queue for candidate memories."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import click

from memtomem.formation import DEFAULT_STALE_CLAIM_MINUTES


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


@review.command("recover")
@click.option(
    "--stale-after-minutes",
    type=click.IntRange(min=1, max=1440),
    default=DEFAULT_STALE_CLAIM_MINUTES,
    show_default=True,
)
@click.option("--limit", type=click.IntRange(min=1, max=1000), default=100)
@click.option("--actor", default="cli-operator")
def recover(stale_after_minutes: int, limit: int, actor: str) -> None:
    """Return stale interrupted approval claims to the pending queue."""
    asyncio.run(_recover(stale_after_minutes, limit, actor))


async def _recover(stale_after_minutes: int, limit: int, actor: str) -> None:
    from memtomem.cli._bootstrap import cli_components

    if not actor.strip():
        raise click.ClickException("Recovery actor cannot be empty")
    stale_before = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
    async with cli_components() as comp:
        recovered = await comp.storage.recover_stale_memory_candidates(
            stale_before=stale_before.isoformat(timespec="seconds"),
            actor=actor,
            limit=limit,
        )
        click.echo(
            json.dumps(
                {
                    "ok": True,
                    "recovered": len(recovered),
                    "candidate_ids": recovered,
                    "stale_before": stale_before.isoformat(timespec="seconds"),
                }
            )
        )


async def _decide(candidate_id: str, decision: str, reviewer: str, reason: str) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.pinned import PinnedContextStore
    from memtomem.server.tools.search import _resolve_project_context_root
    from memtomem.tools.memory_writer import append_entry

    async with cli_components() as comp:
        candidate = await comp.storage.get_memory_candidate(candidate_id)
        if candidate is None:
            raise click.ClickException("Candidate not found")
        if decision == "rejected" and candidate["status"] == "write_uncertain":
            if not reviewer.strip():
                raise click.ClickException("Resolution reviewer cannot be empty")
            if not reason.strip():
                raise click.ClickException(
                    "Resolving write_uncertain requires --reason after inspecting "
                    "the durable destination"
                )
            changed = await comp.storage.resolve_uncertain_memory_candidate(
                candidate_id, reviewer=reviewer, reason=reason
            )
            if not changed:
                raise click.ClickException("Candidate state changed concurrently")
            click.echo(
                json.dumps(
                    {
                        "ok": True,
                        "status": "rejected",
                        "resolved_from": "write_uncertain",
                    }
                )
            )
            return
        if candidate["status"] != "pending":
            raise click.ClickException(f"Candidate is not pending (status={candidate['status']})")
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
            write_location = "the durable destination"
            try:
                if candidate["destination"] == "pinned":
                    block = PinnedContextStore(
                        comp.config, project_root=_resolve_project_context_root(comp)
                    ).set(
                        f"candidate-{candidate_id[:8]}",
                        candidate["content"],
                        description=f"Approved {candidate['kind']} candidate",
                    )
                    write_location = str(block.source_path)
                else:
                    from memtomem.context._atomic import (
                        _CRUD_SIDECAR_LOCK_BUDGET_S,
                        _lock_path_for,
                        async_file_lock,
                    )

                    base = Path(comp.config.indexing.memory_dirs[0]).expanduser().resolve()
                    target = base / f"{datetime.now(timezone.utc):%Y-%m-%d}.md"
                    write_location = str(target)
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
                warning = (
                    "Durable write completed, but the approval claim was recovered "
                    "concurrently. The content already persists at "
                    f"{write_location}; inspect it before taking further action and "
                    "do not re-approve this candidate."
                )
                quarantined = await comp.storage.mark_memory_candidate_write_uncertain(
                    candidate_id, actor="cli-finalizer", reason=warning
                )
                suffix = (
                    " Candidate moved to write_uncertain."
                    if quarantined
                    else " Candidate state changed again; inspect the review queue."
                )
                raise click.ClickException(warning + suffix)
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
    """Reject without writing; uncertain writes require --reason."""
    asyncio.run(_decide(candidate_id, "rejected", reviewer, reason))
