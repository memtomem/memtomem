"""CLI: mm schedule — direct-cron registration for scheduled jobs (P2 Phase A).

Mirrors the shape of ``cli/watchdog_cmd.py``. Direct-cron only —
natural-language ``--spec`` lands in Phase B.
"""

from __future__ import annotations

import asyncio
import json

import click

from croniter import croniter

from memtomem.scheduler.jobs import JOB_KINDS


@click.group()
def schedule() -> None:
    """Scheduled jobs — register, list, run, and delete cron schedules (UTC)."""


@schedule.command("add")
@click.option("--cron", "cron_expr", required=True, help="5-field cron expression (UTC)")
@click.option(
    "--job",
    "job_kind",
    required=True,
    type=click.Choice(sorted(JOB_KINDS)),
    help="Job kind to run",
)
@click.option("--params", "params_json", default=None, help="JSON-encoded params dict")
def schedule_add(cron_expr: str, job_kind: str, params_json: str | None) -> None:
    """Register a new schedule."""
    if not croniter.is_valid(cron_expr):
        raise click.ClickException(f"invalid cron expression: {cron_expr!r}")
    try:
        params = json.loads(params_json) if params_json else {}
    except json.JSONDecodeError as exc:
        raise click.ClickException(f"--params must be valid JSON: {exc}") from exc
    if not isinstance(params, dict):
        raise click.ClickException("--params must decode to a JSON object")

    spec = JOB_KINDS[job_kind]
    try:
        spec.params_model.model_validate(params)
    except Exception as exc:
        raise click.ClickException(f"invalid params: {exc}") from exc

    sched_id = asyncio.run(_insert(cron_expr, job_kind, params))
    click.echo(f"Registered {sched_id}  {cron_expr}  {job_kind}")


@schedule.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def schedule_list(as_json: bool) -> None:
    """List all registered schedules."""
    rows = asyncio.run(_list_all())

    if as_json:
        click.echo(json.dumps(rows, indent=2, default=str))
        return

    if not rows:
        click.echo("No schedules registered.  Use 'mm schedule add' to create one.")
        return

    for r in rows:
        status = r.get("last_run_status") or "—"
        last = r.get("last_run_at") or "never"
        enabled = "on" if r["enabled"] else "off"
        click.echo(
            f"  {r['id']}  [{enabled}]  {r['cron_expr']:<15}  "
            f"{r['job_kind']:<24}  last={last} ({status})"
        )


@schedule.command("run-now")
@click.argument("sched_id")
def schedule_run_now(sched_id: str) -> None:
    """Run a scheduled job synchronously (out-of-band)."""
    result = asyncio.run(_run_now(sched_id))
    if not result["ok"]:
        raise click.ClickException(result["reason"])
    click.echo(f"OK  {sched_id}  {json.dumps(result.get('result', {}))}")


@schedule.command("delete")
@click.argument("sched_id")
def schedule_delete(sched_id: str) -> None:
    """Delete a schedule by id."""
    deleted = asyncio.run(_delete(sched_id))
    if not deleted:
        raise click.ClickException(f"schedule {sched_id!r} not found")
    click.echo(f"Deleted {sched_id}")


# ── Async helpers ────────────────────────────────────────────────────


async def _insert(cron_expr: str, job_kind: str, params: dict) -> str:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        return await comp.storage.schedule_insert(cron_expr, job_kind, params)


async def _list_all() -> list[dict]:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        return await comp.storage.schedule_list_all()


async def _delete(sched_id: str) -> bool:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        return await comp.storage.schedule_delete(sched_id)


async def _run_now(sched_id: str) -> dict:
    """Run a schedule synchronously through the same path the dispatcher uses.

    Mirrors ``mem_schedule_run_now`` (validation, ``schedule_mark_run``,
    timeout) but bypasses the MCP context plumbing — CLI commands run
    against a bootstrapped ``Components`` directly.
    """
    from memtomem.cli._bootstrap import cli_components
    from memtomem.server.context import AppContext

    async with cli_components() as comp:
        app = AppContext.from_components(comp)
        await app.ensure_initialized()

        sched = await app.storage.schedule_get(sched_id)
        if sched is None:
            return {"ok": False, "reason": f"schedule {sched_id!r} not found"}

        spec = JOB_KINDS.get(sched["job_kind"])
        if spec is None:
            reason = f"unknown job_kind: {sched['job_kind']}"
            await app.storage.schedule_mark_run(sched_id, "error", error=reason)
            return {"ok": False, "reason": reason}

        try:
            validated = spec.params_model.model_validate(sched.get("params") or {})
        except Exception as exc:
            reason = f"invalid params: {exc}"
            await app.storage.schedule_mark_run(sched_id, "error", error=reason)
            return {"ok": False, "reason": reason}

        from memtomem.server.tools.schedule import _run_now_timeout

        timeout = _run_now_timeout(app)

        try:
            result = await asyncio.wait_for(
                spec.runner(app, **validated.model_dump()),
                timeout=timeout,
            )
            await app.storage.schedule_mark_run(sched_id, "ok")
            return {"ok": True, "reason": "ran", "result": result}
        except asyncio.TimeoutError:
            reason = f"exceeded {timeout}s"
            await app.storage.schedule_mark_run(sched_id, "timeout", error=reason)
            return {"ok": False, "reason": f"timeout: {reason}"}
        except Exception as exc:
            await app.storage.schedule_mark_run(sched_id, "error", error=str(exc))
            return {"ok": False, "reason": f"runner failed: {exc}"}
