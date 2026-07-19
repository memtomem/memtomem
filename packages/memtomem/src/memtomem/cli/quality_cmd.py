"""``mm quality`` — evaluation-case + replay/compare CLI (#1802, Quality Lab PR-4).

Thin surface over the storage eval-case methods and the ``memtomem.quality``
replay/compare engine: promote a labeled run into a case, list/inspect/archive
cases, export/import case sets, replay cases into a deterministic report, and
compare two reports (advisory by default, opt-in blocking gate). All report/list
commands take ``--format table|json``; write commands emit ``{"ok": ...}`` acks.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import click


@click.group("quality")
def quality() -> None:
    """Evaluate retrieval quality: eval cases, replay, and profile comparison."""


# --------------------------------------------------------------------------- #
# eval-case management                                                        #
# --------------------------------------------------------------------------- #


@quality.command("promote")
@click.argument("run_id")
@click.option("--name", default=None, help="Optional stable name for the case.")
@click.option(
    "--allow-unreplayable-filters",
    is_flag=True,
    help="Promote even if the run carried filters replay can't reproduce.",
)
def promote(run_id: str, name: str | None, allow_unreplayable_filters: bool) -> None:
    """Promote a labeled search run into a durable evaluation case."""
    _run(_promote(run_id, name, allow_unreplayable_filters))


async def _promote(run_id: str, name: str | None, allow_unreplayable_filters: bool) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.quality.state import current_fingerprints

    async with cli_components() as comp:
        fingerprints, _ = current_fingerprints(comp.storage, comp.config)
        case = await comp.storage.promote_search_run(
            run_id,
            name=name,
            fingerprints=fingerprints,
            allow_unreplayable_filters=allow_unreplayable_filters,
        )
        click.echo(
            json.dumps(
                {
                    "ok": True,
                    "case_id": case["case_id"],
                    "name": case["name"],
                    "label_count": len(case["labels"]),
                }
            )
        )


@quality.command("cases")
@click.option("--status", type=click.Choice(["active", "archived"]), default=None)
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def cases(status: str | None, fmt: str) -> None:
    """List evaluation cases (newest first)."""
    _run(_cases(status, fmt))


async def _cases(status: str | None, fmt: str) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        rows = await comp.storage.list_eval_cases(status=status)
    if fmt == "json":
        click.echo(json.dumps({"cases": rows, "count": len(rows)}, ensure_ascii=False, indent=2))
        return
    if not rows:
        click.echo("No evaluation cases.")
        return
    for r in rows:
        name = r["name"] or "-"
        click.echo(
            f"{r['case_id'][:8]}  {r['status']:<8}  labels={r['label_count']:<3}  "
            f"{name}  {r['query_text']!r}"
        )


@quality.command("show")
@click.argument("case_id_or_name")
def show(case_id_or_name: str) -> None:
    """Show one evaluation case with its labels."""
    _run(_show(case_id_or_name))


async def _show(case_id_or_name: str) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        case = await comp.storage.get_eval_case(case_id_or_name)
    click.echo(json.dumps(case, ensure_ascii=False, indent=2))


@quality.command("status")
@click.argument("case_id_or_name")
@click.argument("status", type=click.Choice(["active", "archived"]))
def set_status(case_id_or_name: str, status: str) -> None:
    """Set a case's lifecycle status (active/archived)."""
    _run(_set_status(case_id_or_name, status))


async def _set_status(case_id_or_name: str, status: str) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        case = await comp.storage.set_eval_case_status(case_id_or_name, status)
    click.echo(json.dumps({"ok": True, "case_id": case["case_id"], "status": case["status"]}))


@quality.command("export")
@click.option("--case", "case_selectors", multiple=True, help="Case id or name (repeatable).")
@click.option("--out", type=click.Path(dir_okay=False), default=None, help="Write to a file.")
def export_cases(case_selectors: tuple[str, ...], out: str | None) -> None:
    """Export evaluation cases as a portable JSON envelope."""
    _run(_export(case_selectors, out))


async def _export(case_selectors: tuple[str, ...], out: str | None) -> None:
    from memtomem.cli._bootstrap import cli_components

    async with cli_components() as comp:
        case_ids: list[str] | None = None
        if case_selectors:
            # Storage exports by case_id only; resolve any names first.
            case_ids = [
                (await comp.storage.get_eval_case(sel))["case_id"] for sel in case_selectors
            ]
        envelope = await comp.storage.export_eval_cases(case_ids=case_ids)
    payload = json.dumps(envelope, ensure_ascii=False, indent=2)
    if out:
        _write_file(out, payload + "\n")
        click.echo(json.dumps({"ok": True, "exported": len(envelope["cases"]), "out": out}))
    else:
        click.echo(payload)


@quality.command("import")
@click.argument("file", type=click.Path(exists=True, dir_okay=False))
@click.option("--replace", is_flag=True, help="Overwrite cases with matching case_id.")
def import_cases(file: str, replace: bool) -> None:
    """Import an evaluation-case envelope from a JSON file."""
    _run(_import(file, replace))


async def _import(file: str, replace: bool) -> None:
    from memtomem.cli._bootstrap import cli_components

    payload = _read_json(file)
    async with cli_components() as comp:
        result = await comp.storage.import_eval_cases(payload, replace=replace)
    click.echo(json.dumps({"ok": True, "imported": result["imported"]}))


# --------------------------------------------------------------------------- #
# replay + compare                                                            #
# --------------------------------------------------------------------------- #


@quality.command("replay")
@click.option("--case", "case_selectors", multiple=True, help="Case id or name (repeatable).")
@click.option("--as-of", type=int, default=None, help="Pin temporal validity + decay (unix).")
@click.option("--out", type=click.Path(dir_okay=False), default=None, help="Write report to file.")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def replay(case_selectors: tuple[str, ...], as_of: int | None, out: str | None, fmt: str) -> None:
    """Replay evaluation cases into a deterministic report."""
    _run(_replay(case_selectors, as_of, out, fmt))


async def _replay(
    case_selectors: tuple[str, ...], as_of: int | None, out: str | None, fmt: str
) -> None:
    from memtomem.cli._bootstrap import cli_components
    from memtomem.quality.replay import replay_cases, serialize_report

    async with cli_components() as comp:
        report = await replay_cases(
            comp.storage,
            comp.search_pipeline,
            comp.config,
            case_ids=list(case_selectors) or None,
            as_of_unix=as_of,
        )
    canonical = serialize_report(report)
    if not report["deterministic"]:
        click.echo(
            "warning: profile is nondeterministic "
            f"({', '.join(report['nondeterministic_stages'])}); "
            "replays may not be byte-reproducible",
            err=True,
        )
    if out:
        _write_file(out, canonical)
    if fmt == "json":
        click.echo(canonical, nl=False)
    else:
        _render_replay_table(report)


def _render_replay_table(report: dict) -> None:
    agg = report["aggregate"]
    counts = report["counts"]
    click.echo(
        f"replayed {counts['replayed']} case(s) "
        f"(archived_skipped={counts['archived_skipped']}, degraded={counts['degraded']}, "
        f"excluded={counts['excluded_from_aggregate']})"
    )
    for c in report["cases"]:
        flags = f" [{', '.join(c['flags'])}]" if c["flags"] else ""
        m = c["metrics"]
        precision = "n/a" if m["precision"] is None else f"{m['precision']:.3f}"
        click.echo(
            f"  {c['case_id'][:8]}  hit={m['hit_rate']:.0f}  rr={m['reciprocal_rank']:.3f}  "
            f"recall={m['recall_labeled']:.3f}  ndcg={m['ndcg']:.3f}  p={precision}{flags}"
        )
    click.echo(
        f"aggregate: hit_rate={agg['mean_hit_rate']:.3f}  mrr={agg['mrr']:.3f}  "
        f"recall={agg['mean_recall_labeled']:.3f}  ndcg={agg['mean_ndcg']:.3f}  "
        f"(over {agg['evaluated_cases']} case(s))"
    )


@quality.command("compare")
@click.argument("baseline", type=click.Path(exists=True, dir_okay=False))
@click.argument("candidate", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", type=click.Path(dir_okay=False), default=None, help="Write result to file.")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.option(
    "--fail-on-regression",
    is_flag=True,
    help="Exit non-zero on regressions, degraded candidates, or case-set drift.",
)
def compare(
    baseline: str, candidate: str, out: str | None, fmt: str, fail_on_regression: bool
) -> None:
    """Compare two replay reports (baseline vs candidate).

    Advisory by default (always exits 0). ``--fail-on-regression`` opts into a
    blocking gate. Runs on an unconfigured machine — no storage needed.
    """
    from memtomem.cli._errors import raise_cli_error

    try:
        _compare(baseline, candidate, out, fmt, fail_on_regression)
    except click.ClickException:
        raise
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001 — normalized to a CLI error
        raise_cli_error(e)


def _compare(
    baseline: str, candidate: str, out: str | None, fmt: str, fail_on_regression: bool
) -> None:
    from memtomem.quality.compare import compare_reports, serialize_comparison

    result = compare_reports(_read_json(baseline), _read_json(candidate))
    canonical = serialize_comparison(result)
    if out:
        _write_file(out, canonical)
    if fmt == "json":
        click.echo(canonical, nl=False)
    else:
        _render_compare_table(result)

    if fail_on_regression:
        s = result["summary"]
        gate_hits = (
            s["regressed"]
            + s["mixed"]
            + s["candidate_degraded"]
            + s["both_degraded"]
            + s["version_mismatch"]
            + s["definition_mismatch"]
            + s["baseline_only"]
            + s["candidate_only"]
        )
        if gate_hits or not result["compatibility"]["case_set_match"]:
            raise SystemExit(1)


def _render_compare_table(result: dict) -> None:
    s = result["summary"]
    click.echo(
        f"improved={s['improved']}  regressed={s['regressed']}  mixed={s['mixed']}  "
        f"unchanged={s['unchanged']}  candidate_degraded={s['candidate_degraded']}  "
        f"excluded={s['excluded']}"
    )
    for note in result["compatibility"]["notes"]:
        click.echo(f"  note: {note}")


# --------------------------------------------------------------------------- #
# policy gate                                                                 #
# --------------------------------------------------------------------------- #


class GateInputError(click.ClickException):
    """Bad gate input (unreadable/malformed report or policy).

    Exit code 2 distinguishes "the gate could not run" from a genuine policy
    violation (exit 1) and from success (exit 0), so CI can tell an
    infrastructure failure apart from a real quality regression. Messages
    reference inputs by role ("baseline"/"candidate"/"policy"), never by
    filesystem path, to keep the emit boundary clean on failure.
    """

    exit_code = 2


@quality.command("gate")
@click.argument("baseline", type=click.Path())
@click.argument("candidate", type=click.Path())
@click.option(
    "--policy",
    "policy_path",
    required=True,
    type=click.Path(),
    help="Committed gate-policy JSON file.",
)
@click.option("--out", type=click.Path(dir_okay=False), default=None, help="Write verdict to file.")
@click.option(
    "--comparison-out",
    type=click.Path(dir_okay=False),
    default=None,
    help="Also write the intermediate comparison to file.",
)
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def gate(
    baseline: str,
    candidate: str,
    policy_path: str,
    out: str | None,
    comparison_out: str | None,
    fmt: str,
) -> None:
    """Gate two replay reports against a policy (baseline vs candidate).

    Computes the comparison internally, then evaluates it against the policy
    file. Exit 0 = pass, 1 = policy violation (verdict still emitted first),
    2 = invalid input. Runs on an unconfigured machine — no storage needed.
    """
    _gate(baseline, candidate, policy_path, out, comparison_out, fmt)


def _gate(
    baseline: str,
    candidate: str,
    policy_path: str,
    out: str | None,
    comparison_out: str | None,
    fmt: str,
) -> None:
    from memtomem.errors import EvalCaseError
    from memtomem.quality.compare import compare_reports, serialize_comparison
    from memtomem.quality.gate import evaluate_gate, load_policy, serialize_gate_verdict

    baseline_doc = _read_json_role(baseline, "baseline")
    candidate_doc = _read_json_role(candidate, "candidate")
    policy_doc = _read_json_role(policy_path, "policy")

    try:
        comparison = compare_reports(baseline_doc, candidate_doc)
        policy = load_policy(policy_doc)
    except EvalCaseError as e:
        # Both report validation (compare) and policy validation raise the
        # EvalCaseError family; map every such failure to exit 2 without
        # echoing the raw message verbatim (it may interpolate a case_id).
        raise GateInputError(f"gate input rejected: {type(e).__name__}") from e

    verdict = evaluate_gate(comparison, policy)
    canonical = serialize_gate_verdict(verdict)
    if comparison_out:
        _write_file(comparison_out, serialize_comparison(comparison))
    if out:
        _write_file(out, canonical)

    if fmt == "json":
        click.echo(canonical, nl=False)
    else:
        _render_gate_table(verdict)

    if not verdict["pass"]:
        raise SystemExit(1)


def _render_gate_table(verdict: dict) -> None:
    click.echo(f"gate: {'PASS' if verdict['pass'] else 'FAIL'}")
    for v in verdict["violations"]:
        detail = ", ".join(f"{k}={v[k]}" for k in v if k != "rule")
        click.echo(f"  violation: {v['rule']} ({detail})")
    for a in verdict["allowlisted"]:
        click.echo(f"  allowlisted: {a['case_id']} [{a['status']}] — {a['reason']}")
    for w in verdict["warnings"]:
        click.echo(f"  warning: {w}")


# --------------------------------------------------------------------------- #
# multi-candidate experiment                                                  #
# --------------------------------------------------------------------------- #


@quality.command("experiment")
@click.option(
    "--baseline",
    "baseline_path",
    type=click.Path(),
    default=None,
    help="Baseline profile document (default: the ambient effective config).",
)
@click.option(
    "--profile",
    "profile_paths",
    multiple=True,
    required=True,
    help="Candidate profile document (repeatable).",
)
@click.option("--case", "case_selectors", multiple=True, help="Case id or name (repeatable).")
@click.option(
    "--as-of",
    type=int,
    default=None,
    help="Pin temporal validity + decay (unix). Required for byte-identical reruns.",
)
@click.option(
    "--policy",
    "policy_path",
    type=click.Path(),
    default=None,
    help="Gate-policy JSON, evaluated independently per candidate.",
)
@click.option("--out", type=click.Path(dir_okay=False), default=None, help="Write result to file.")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def experiment(
    baseline_path: str | None,
    profile_paths: tuple[str, ...],
    case_selectors: tuple[str, ...],
    as_of: int | None,
    policy_path: str | None,
    out: str | None,
    fmt: str,
) -> None:
    """Replay a baseline + candidate profiles against one case set.

    Replays the baseline (a ``--profile`` document, or the ambient config by
    default) and every candidate against one pinned case set, then compares each
    candidate with the baseline. Emits a deterministic, PR-attachable result; it
    never selects a winner or changes defaults. Exit 0 = ran (no policy, or all
    candidates pass the policy), 1 = a candidate failed the supplied ``--policy``
    (result still emitted first), 2 = invalid input.
    """
    _run(_experiment(baseline_path, profile_paths, case_selectors, as_of, policy_path, out, fmt))


async def _experiment(
    baseline_path: str | None,
    profile_paths: tuple[str, ...],
    case_selectors: tuple[str, ...],
    as_of: int | None,
    policy_path: str | None,
    out: str | None,
    fmt: str,
) -> None:
    import sqlite3

    from memtomem.cli._bootstrap import cli_components
    from memtomem.errors import EvalCaseError, Mem2MemError
    from memtomem.quality.experiment import run_experiment, serialize_experiment
    from memtomem.quality.gate import load_policy

    # Read + validate every input first, by role (never by path), so a bad file
    # fails at exit 2 before any storage is opened and nothing is emitted. Each
    # document is validated under its own role so the message names which input
    # was rejected (still type-only — never the offending value). The presence of
    # the --baseline FLAG (not the file's content) decides whether a baseline doc
    # is required, so an explicit but falsy document ({}, null) is validated and
    # rejected rather than silently treated as "use the ambient config".
    baseline_json = _read_json_role(baseline_path, "baseline profile") if baseline_path else None
    profile_jsons = [
        _read_json_role(path, f"profile #{i + 1}") for i, path in enumerate(profile_paths)
    ]
    policy_json = _read_json_role(policy_path, "policy") if policy_path else None

    baseline_doc = _load_doc_role(baseline_json, "baseline profile") if baseline_path else None
    candidate_docs = [_load_doc_role(j, f"profile #{i + 1}") for i, j in enumerate(profile_jsons)]
    policy = None
    if policy_json is not None:
        try:
            policy = load_policy(policy_json)
        except EvalCaseError as e:
            raise GateInputError(f"policy rejected: {type(e).__name__}") from e

    # Exit-code contract: 1 is reserved for an emitted policy failure (the
    # SystemExit at the very end). EVERY expected operational "could not run"
    # failure — unconfigured install, config/storage/embedding/LLM error, a raw
    # SQLite read error from the fingerprint readers, a filesystem error, or an
    # invalid experiment shape — maps to a path-free exit 2 so CI cannot confuse
    # it with a regression and no raw message (which may carry a path) is emitted.
    try:
        async with cli_components() as comp:
            result = await run_experiment(
                comp,
                baseline_doc=baseline_doc,
                candidate_docs=candidate_docs,
                case_selectors=list(case_selectors) or None,
                as_of_unix=as_of,
                policy=policy,
            )
    except GateInputError:
        raise
    except click.ClickException as e:
        # e.g. cli_components' "not configured" — operational, not a regression.
        raise GateInputError("experiment could not run: not configured") from e
    except (Mem2MemError, sqlite3.Error, OSError) as e:
        raise GateInputError(f"experiment could not run: {type(e).__name__}") from e

    canonical = serialize_experiment(result)
    for entry in [result["baseline"], *result["candidates"]]:
        if not entry["deterministic"]:
            click.echo(
                f"warning: profile {entry['profile_name']!r} is nondeterministic "
                f"({', '.join(entry['nondeterministic_stages'])}); "
                "not valid as deterministic gate evidence",
                err=True,
            )
    if out:
        try:
            _write_file(out, canonical)
        except OSError as e:
            # A path-free error at the same exit-2 boundary as bad input — never
            # echo the output path, and never let an unwritable --out read as a
            # policy failure (exit 1).
            raise GateInputError(f"experiment output is not writable: {type(e).__name__}") from e
    if fmt == "json":
        click.echo(canonical, nl=False)
    else:
        _render_experiment_table(result)

    if result["policy_supplied"] and any(
        c["gate"] is not None and not c["gate"]["pass"] for c in result["candidates"]
    ):
        raise SystemExit(1)


def _load_doc_role(data: Any, role: str):
    """Validate one profile document under a role-aware, value-free boundary."""
    from memtomem.errors import EvalCaseError
    from memtomem.quality.profiles import load_profile_document

    try:
        return load_profile_document(data)
    except EvalCaseError as e:
        raise GateInputError(f"{role} rejected: {type(e).__name__}") from e


def _render_experiment_table(result: dict) -> None:
    fp = result["fingerprints"]
    click.echo(
        f"experiment: {result['case_count']} case(s)  as_of={result['as_of_unix']}  "
        f"corpus={fp['corpus'][:8]}  index={fp['index'][:8]}  case_set={fp['case_set'][:8]}"
    )
    base = result["baseline"]
    _render_profile_header("baseline", base, gate=None)
    agg = base["aggregate"]
    click.echo(
        f"  hit_rate={agg['mean_hit_rate']:.3f}  mrr={agg['mrr']:.3f}  "
        f"recall={agg['mean_recall_labeled']:.3f}  ndcg={agg['mean_ndcg']:.3f}  "
        f"(over {agg['evaluated_cases']} case(s))"
    )
    _render_profile_warnings(base)
    for cand in result["candidates"]:
        click.echo("")
        _render_profile_header(cand["profile_name"], cand, gate=cand["gate"])
        comparison = cand["comparison"]
        _render_candidate_deltas(comparison)
        for case in comparison["cases"]:
            label = case.get("classification") or case.get("status") or "?"
            deltas = case.get("metric_deltas") or {}
            ndcg = deltas.get("ndcg")
            ndcg_str = f"  Δndcg={ndcg:+.3f}" if isinstance(ndcg, (int, float)) else ""
            click.echo(f"    {case['case_id'][:8]}  {label}{ndcg_str}")
        if cand["gate"] is not None:
            for v in cand["gate"]["violations"]:
                detail = ", ".join(f"{k}={v[k]}" for k in v if k != "rule")
                click.echo(f"  violation: {v['rule']} ({detail})")
        for note in comparison["compatibility"]["notes"]:
            click.echo(f"  note: {note}")
        _render_profile_warnings(cand)


def _render_profile_warnings(entry: dict) -> None:
    for warning in entry["warnings"]:
        click.echo(f"  warning: {warning}")


def _render_profile_header(label: str, entry: dict, *, gate: dict | None) -> None:
    if entry["deterministic"]:
        det = "yes"
    else:
        det = f"NO ({', '.join(entry['nondeterministic_stages'])})"
    gate_str = ""
    if gate is not None:
        gate_str = f"  gate={'PASS' if gate['pass'] else 'FAIL'}"
    click.echo(
        f"{label} {entry['profile_name']}  profile={entry['profile_fingerprint'][:8]}  "
        f"deterministic={det}{gate_str}"
    )


def _render_candidate_deltas(comparison: dict) -> None:
    d = comparison["aggregate_deltas"]
    precision = d["precision"]
    p_str = (
        f"Δprecision={precision['delta']:+.3f} (cohort {precision['cohort_size']})"
        if precision["cohort_size"]
        else "Δprecision=n/a (cohort 0)"
    )
    click.echo(
        f"  Δhit_rate={d['hit_rate']['delta']:+.3f}  "
        f"Δmrr={d['reciprocal_rank']['delta']:+.3f}  "
        f"Δrecall={d['recall_labeled']['delta']:+.3f}  "
        f"Δndcg={d['ndcg']['delta']:+.3f}  {p_str} (cohort {d['cohort_size']})"
    )
    s = comparison["summary"]
    click.echo(
        f"  improved={s['improved']}  regressed={s['regressed']}  mixed={s['mixed']}  "
        f"unchanged={s['unchanged']}  candidate_degraded={s['candidate_degraded']}  "
        f"excluded={s['excluded']}"
    )


def _read_json_role(path: str, role: str) -> Any:
    """Read a JSON input, mapping any failure to a path-free exit-2 error.

    Handles missing/unreadable files and directories (``OSError``), invalid
    UTF-8 (``UnicodeError``), and malformed JSON (``JSONDecodeError``) — all as
    a role-only message so the filesystem path never reaches the output.
    """
    import pathlib

    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as e:
        raise GateInputError(f"{role} is not a readable valid JSON file: {type(e).__name__}") from e


# --------------------------------------------------------------------------- #
# helpers                                                                      #
# --------------------------------------------------------------------------- #


def _run(coro) -> None:
    """Run an async command body with the house error-normalization tail."""
    from memtomem.cli._errors import raise_cli_error

    try:
        asyncio.run(coro)
    except click.ClickException:
        raise
    except Exception as e:  # noqa: BLE001 — normalized to a CLI error
        raise_cli_error(e)


def _read_json(path: str) -> dict:
    import pathlib

    try:
        return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise click.ClickException(f"{path} is not valid JSON: {e}") from e


def _write_file(path: str, content: str) -> None:
    import pathlib

    pathlib.Path(path).write_text(content, encoding="utf-8")
