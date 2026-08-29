"""``mm doctor`` — health of the memtomem *runtime* on this host.

The doctor family is split by what a check must inspect, not by severity:

* ``mm doctor`` (here) — the host runtime: server processes and the runtime
  directory. Needs no configured store and answers even when none exists.
* ``mm memory doctor`` — a configured store: disk / index-file / DB drift.
* ``mm sync-doctor`` — the private multi-device sync repo working tree.
* ``mm context settings-doctor`` — client hook settings across tiers.

The motivating gap (#2226): a host can accumulate dozens of ``memtomem-server``
processes and no existing surface says so, because every one of them either
narrows to a single store (``mm status``'s concurrent-writer warning) or
collapses to a yes/no verdict (the uninstall probe). This command is the
unfiltered, all-store read.

Report-only by design. It never terminates a process and never garbage-collects
a sentinel, so it is safe to run repeatedly and its output is comparable across
runs. Acting on what it reports is a process-lifecycle decision deferred by
#2226.
"""

from __future__ import annotations

import json
import statistics
import time
from typing import Literal, NamedTuple

import click

from memtomem._instance_registry import RegistrySnapshot, snapshot_all_instances
from memtomem._process_probe import probe_pid
from memtomem._runtime_paths import runtime_dir, validate_runtime_dir

__all__ = ["CheckResult", "InstanceRow", "doctor"]

Status = Literal["pass", "fail", "warn", "info"]

_GLYPH = {"pass": "✓", "fail": "✗", "warn": "!", "info": "·"}
_COLOR = {"pass": "green", "fail": "red", "warn": "yellow", "info": None}

# At or above this many distinct server processes the report escalates from a
# neutral observation to a warning. Chosen from field data (#2226: 88 on one
# host, 29 on another) — one process per active client is normal, so dozens is
# worth a look. Deliberately not a hard failure, and deliberately says nothing
# about *why*: the count alone cannot tell an abandoned server from a busy
# machine.
_BUSY_PROCESS_COUNT = 10

_HOUR_S = 3600.0
_DAY_S = 86400.0

_EPILOG = """\
\b
Other doctors (this one covers the host runtime only):
  mm memory doctor           store contents: disk / index / DB drift
  mm sync-doctor             the private multi-device sync repo
  mm context settings-doctor client hook settings across tiers
"""


class CheckResult(NamedTuple):
    name: str  # stable machine key, e.g. "server-instances"
    status: Status
    message: str
    detail: str | None = None
    data: dict[str, object] | None = None  # JSON-only elaboration


class InstanceRow(NamedTuple):
    """One *registration*. A process may hold several (one per store)."""

    pid: int
    procid: str
    recorded_ppid: int
    store_digest: str
    age_seconds: float | None
    recorded_parent: Literal["alive", "missing", "unknown"]
    recorded_ppid_is_one: bool


def _emit(result: CheckResult) -> None:
    click.secho(f"{_GLYPH[result.status]} {result.message}", fg=_COLOR[result.status])
    if result.detail:
        for line in result.detail.splitlines():
            click.echo(f"  {line}")


def _overall_status(results: list[CheckResult]) -> Status:
    """Reduce check statuses to one. ``info`` is an observation, not a problem."""
    if any(r.status == "fail" for r in results):
        return "fail"
    if any(r.status == "warn" for r in results):
        return "warn"
    return "pass"


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "unknown"
    if seconds < _HOUR_S:
        return f"{seconds / 60:.0f}m"
    if seconds < _DAY_S:
        return f"{seconds / _HOUR_S:.1f}h"
    return f"{seconds / _DAY_S:.1f}d"


def _instance_rows(snapshot: RegistrySnapshot) -> list[InstanceRow]:
    rows: list[InstanceRow] = []
    # One probe per process, not per registration. A process's registrations all
    # record the same parent, so probing each separately buys nothing and can
    # *disagree* — a parent exiting mid-report would leave one process holding
    # rows that say both "alive" and "missing", and any summary over them would
    # be picking one arbitrarily.
    probed_by_procid: dict[str, Literal["alive", "missing", "unknown"]] = {}
    for info in snapshot.instances:
        try:
            age: float | None = max(time.time() - info.path.stat().st_mtime, 0.0)
        except OSError:
            # The sentinel vanished between enumeration and stat — the process
            # exited mid-report. Unknown age, not zero.
            age = None
        parent = probed_by_procid.get(info.procid)
        if parent is None:
            # "dead" is the probe's vocabulary about a pid; this column reports
            # a *recorded* parent, so it says "missing" — we observed the pid is
            # not running, not that the client necessarily died (pids reused).
            probed = probe_pid(info.ppid)
            parent = "alive" if probed == "alive" else "missing" if probed == "dead" else "unknown"
            probed_by_procid[info.procid] = parent
        rows.append(
            InstanceRow(
                pid=info.pid,
                procid=info.procid,
                recorded_ppid=info.ppid,
                store_digest=info.digest,
                age_seconds=age,
                recorded_parent=parent,
                # POSIX-only signal: Windows pids are multiples of four, so 1
                # cannot occur there, and Windows never reparents anyway.
                recorded_ppid_is_one=info.ppid == 1,
            )
        )
    return rows


def _process_ages(rows: list[InstanceRow]) -> tuple[list[float], int]:
    """Collapse registrations to one age per process.

    A process registered against several stores must count once, or a
    multi-store server skews the median and the buckets. Its age is the oldest
    of its registrations whose mtime we could read; a process with no readable
    mtime contributes to the unknown tally instead of a fabricated value.
    """
    oldest: dict[str, float] = {}
    procids: set[str] = set()
    for row in rows:
        procids.add(row.procid)
        if row.age_seconds is None:
            continue
        current = oldest.get(row.procid)
        if current is None or row.age_seconds > current:
            oldest[row.procid] = row.age_seconds
    return sorted(oldest.values(), reverse=True), len(procids - set(oldest))


def _check_runtime_dir(snapshot: RegistrySnapshot) -> CheckResult:
    """The host's coordination directory: does it exist and is it ours alone?"""
    if snapshot.canonical_error is not None:
        return CheckResult(
            name="runtime-dir",
            status="fail",
            message="runtime directory is unusable",
            detail=str(snapshot.canonical_error),
        )
    try:
        target = runtime_dir()
    except OSError as exc:
        return CheckResult(
            name="runtime-dir",
            status="fail",
            message="runtime directory could not be resolved",
            detail=str(exc),
        )
    try:
        present = validate_runtime_dir(target)
    except OSError as exc:
        return CheckResult(
            name="runtime-dir",
            status="fail",
            message=f"runtime directory is unusable: {target}",
            detail=str(exc),
        )
    data: dict[str, object] = {"path": str(target), "present": present}
    if snapshot.refusal is not None:
        refused, refusal_exc = snapshot.refusal
        return CheckResult(
            name="runtime-dir",
            status="warn",
            message=f"runtime directory {target}",
            detail=f"a historical runtime directory was refused: {refused} ({refusal_exc})",
            data={**data, "refused": str(refused)},
        )
    if not present:
        # Absent is normal, but it does not mean nothing is running: a server
        # started before the runtime dir moved registers under a historical
        # root, which the snapshot still reads. Only say "nothing here" when
        # the scan actually found nothing.
        found = len(snapshot.instances)
        return CheckResult(
            name="runtime-dir",
            status="pass",
            message=(
                f"canonical runtime directory absent: {target}"
                + ("" if found else " (no server has registered here)")
            ),
            detail=(
                f"{found} registration(s) found under a historical runtime directory"
                if found
                else None
            ),
            data=data,
        )
    return CheckResult(
        name="runtime-dir", status="pass", message=f"runtime directory {target}", data=data
    )


def _check_server_instances(snapshot: RegistrySnapshot, rows: list[InstanceRow]) -> CheckResult:
    """How many servers are live on this host, and how long have they been up?

    Reported unconditionally rather than only when something looks wrong. The
    field data is the reason: on one host all 29 servers had live parents, so
    every orphan heuristic called the machine clean — gating the report on
    "looks abandoned" would have hidden the common case entirely.
    """
    ages, age_unknown = _process_ages(rows)
    processes = len({r.procid for r in rows})
    stores = len({r.store_digest for r in rows})
    # Parent state is a property of a *process*, not of each registration it
    # holds: a server registered against three stores has one parent, and
    # counting it three times would inflate every tally against the process
    # counts printed beside them.
    by_process: dict[str, InstanceRow] = {}
    for row in rows:
        by_process.setdefault(row.procid, row)
    alive_parents = sum(1 for r in by_process.values() if r.recorded_parent == "alive")
    missing_parents = sum(1 for r in by_process.values() if r.recorded_parent == "missing")
    unknown_parents = sum(1 for r in by_process.values() if r.recorded_parent == "unknown")
    ppid_one = sum(1 for r in by_process.values() if r.recorded_ppid_is_one)

    data: dict[str, object] = {
        "processes": processes,
        "registrations": len(rows),
        "stores": stores,
        "age_unknown": age_unknown,
        "alive_recorded_parents": alive_parents,
        "missing_recorded_parents": missing_parents,
        "unknown_recorded_parents": unknown_parents,
        "recorded_ppid_is_one": ppid_one,
        "buckets": _age_buckets(ages),
    }
    if ages:
        data["age_seconds"] = {
            "min": min(ages),
            "median": statistics.median(ages),
            "max": max(ages),
        }

    if processes == 0:
        if snapshot.complete:
            return CheckResult(
                name="server-instances",
                status="pass",
                message="no live server processes",
                data=data,
            )
        # Zero from a scan that could not finish is a lower bound like any other
        # count, and saying "no live server processes" would state the one thing
        # the scan failed to establish.
        return CheckResult(
            name="server-instances",
            status="warn",
            message="no live server processes found",
            detail="enumeration was incomplete — zero here is a lower bound, not an absence",
            data=data,
        )

    summary = f"{processes} live server process{'es' if processes != 1 else ''}"
    summary += f" across {stores} store{'s' if stores != 1 else ''}"
    if ages:
        summary += f" (median age {_format_age(statistics.median(ages))}"
        summary += f", max {_format_age(max(ages))})"
    detail_lines: list[str] = []
    if age_unknown:
        detail_lines.append(f"age distribution over {len(ages)} of {processes} processes")
    # Report the three probe outcomes separately. Folding "unknown" into the
    # alive tally would let a report that learned nothing about any parent still
    # print "all recorded parents alive", which is the exact overclaim the
    # tri-state probe exists to prevent.
    if missing_parents or unknown_parents:
        parent_parts = [f"recorded parents: {alive_parents} alive"]
        if missing_parents:
            parent_parts.append(f"{missing_parents} gone")
        if unknown_parents:
            parent_parts.append(f"{unknown_parents} could not be probed")
        detail_lines.append(", ".join(parent_parts))
    else:
        detail_lines.append("all recorded parents alive")
    if ppid_one:
        detail_lines.append(f"{ppid_one} recorded a parent pid of 1 (daemonized or reparented)")

    status: Status = "info"
    if not snapshot.complete:
        status = "warn"
        detail_lines.append("enumeration was incomplete — this count is a lower bound")
    elif processes >= _BUSY_PROCESS_COUNT:
        status = "warn"
        # Neutral on purpose: a live parent is not proof a client is still
        # using the server, a missing one is not proof it was abandoned, and on
        # Windows the recorded parent may be an unrelated reused pid. The count
        # is the signal; the cause is for the reader to establish.
        detail_lines.append(
            f"{processes} is unusually many — check which clients and services still hold one"
        )

    return CheckResult(
        name="server-instances",
        status=status,
        message=summary,
        detail="\n".join(detail_lines),
        data=data,
    )


def _age_buckets(ages: list[float]) -> dict[str, int]:
    buckets = {"lt_1h": 0, "1h_1d": 0, "1d_7d": 0, "gt_7d": 0}
    for age in ages:
        if age < _HOUR_S:
            buckets["lt_1h"] += 1
        elif age < _DAY_S:
            buckets["1h_1d"] += 1
        elif age < 7 * _DAY_S:
            buckets["1d_7d"] += 1
        else:
            buckets["gt_7d"] += 1
    return buckets


def _check_registry_hygiene(snapshot: RegistrySnapshot) -> CheckResult:
    """Leftovers in the sentinel directory, as an observation."""
    data: dict[str, object] = {
        "stale": snapshot.stale_seen,
        "unlocked_fresh": snapshot.unlocked_fresh_seen,
        "unparseable": snapshot.unparseable_seen,
        "roots_consulted": snapshot.roots_consulted,
    }
    if snapshot.unparseable_seen:
        return CheckResult(
            name="registry-hygiene",
            status="warn",
            message=f"{snapshot.unparseable_seen} held sentinel(s) could not be attributed",
            detail="the live-server count above is a lower bound",
            data=data,
        )
    if snapshot.stale_seen or snapshot.unlocked_fresh_seen:
        parts = []
        if snapshot.stale_seen:
            parts.append(f"{snapshot.stale_seen} stale sentinel(s) awaiting collection")
        if snapshot.unlocked_fresh_seen:
            parts.append(f"{snapshot.unlocked_fresh_seen} registration(s) still starting up")
        return CheckResult(
            name="registry-hygiene",
            status="info",
            message="; ".join(parts),
            data=data,
        )
    if not snapshot.complete:
        # Zero counters on a scan that could not finish is absence of evidence,
        # not a clean bill of health — the entries it failed to read are exactly
        # the ones that would have been counted here.
        return CheckResult(
            name="registry-hygiene",
            status="warn",
            message="instance registry could not be fully assessed",
            detail="the scan did not complete; counts above are lower bounds",
            data=data,
        )
    return CheckResult(
        name="registry-hygiene",
        status="pass",
        message=f"instance registry clean ({snapshot.roots_consulted} root(s) consulted)",
        data=data,
    )


def _row_json(row: InstanceRow) -> dict[str, object]:
    return {
        "pid": row.pid,
        "procid": row.procid,
        "recorded_ppid": row.recorded_ppid,
        "store_digest": row.store_digest,
        "age_seconds": row.age_seconds,
        "recorded_parent": row.recorded_parent,
        "recorded_ppid_is_one": row.recorded_ppid_is_one,
    }


@click.command("doctor", epilog=_EPILOG)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit a structured JSON result instead of human-readable output.",
)
def doctor(as_json: bool) -> None:
    """Check the memtomem runtime on this machine (read-only).

    Reports the server processes running on this host — across *every* store,
    which is what makes accumulation visible — and the health of the runtime
    directory they coordinate through. Never terminates anything.

    Exits non-zero only when the runtime directory itself is unusable.
    Accumulated servers and an incomplete scan are warnings, not failures:
    a count alone cannot distinguish an abandoned server from a busy machine.
    Scripts should read ``--json`` rather than the exit code.

    Caveats worth knowing when reading the output: ``recorded_parent`` is a
    probe of the parent pid captured at registration, so on Windows (where pids
    are reused and never reparented) an "alive" parent may be an unrelated
    process; and a live parent does not mean a server is in *use* — an idle
    editor session left open for days holds one just as firmly.
    """
    snapshot = snapshot_all_instances()
    rows = _instance_rows(snapshot)
    results = [
        _check_runtime_dir(snapshot),
        _check_server_instances(snapshot, rows),
        _check_registry_hygiene(snapshot),
    ]
    status = _overall_status(results)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "status": status,
                    "checks": [
                        {
                            "name": r.name,
                            "status": r.status,
                            "message": r.message,
                            "detail": r.detail,
                            "data": r.data,
                        }
                        for r in results
                    ],
                    "instances": [_row_json(r) for r in rows],
                    "complete": snapshot.complete,
                },
                indent=2,
            )
        )
    else:
        for result in results:
            _emit(result)
        if rows:
            click.echo()
            _emit_table(rows)

    if status == "fail":
        raise SystemExit(1)


def _emit_table(rows: list[InstanceRow]) -> None:
    click.secho(f"{'PID':>8}  {'PARENT':>8}  {'STORE':<10}  {'AGE':>7}  PARENT STATE", bold=True)
    for row in sorted(rows, key=lambda r: (-(r.age_seconds or 0.0), r.pid)):
        note = str(row.recorded_parent)
        if row.recorded_ppid_is_one:
            note += " (ppid 1)"
        click.echo(
            f"{row.pid:>8}  {row.recorded_ppid:>8}  {row.store_digest[:10]:<10}  "
            f"{_format_age(row.age_seconds):>7}  {note}"
        )
