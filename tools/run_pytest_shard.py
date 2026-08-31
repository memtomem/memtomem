#!/usr/bin/env python3
"""Run a cost-balanced file-level shard of the memtomem pytest suite."""

from __future__ import annotations

import argparse
import json
import math
import subprocess
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

#: Recorded per-file cost, used to balance the shards. See ``load_durations``.
DURATIONS_FILE = "pytest_shard_durations.json"

#: Largest cost a single test file may claim, in seconds. No file legitimately
#: runs for a day; a larger value means a corrupted or hand-edited map. The cap
#: is what keeps the *sum* finite too — individually rejecting ``inf`` is not
#: enough, because a handful of merely-huge finite values (``1e308``) overflow
#: to ``inf`` once added together, which collapses the balance to one shard.
MAX_FILE_SECONDS = 86_400.0


def test_files(repo_root: Path) -> list[Path]:
    """Return the regular pytest files covered by the cross-platform suite.

    Matches both patterns pytest collects by default (``test_*.py`` and
    ``*_test.py``); globbing only the first would let a suffix-style test pass
    on Linux/macOS while being silently dropped from the Windows shards. The
    dedicated golden-path suite runs in its own job, so it is excluded here to
    mirror the ``--ignore`` the cross-platform ``test`` job applies.
    """
    tests_root = repo_root / "packages" / "memtomem" / "tests"
    matches = {*tests_root.rglob("test_*.py"), *tests_root.rglob("*_test.py")}
    return sorted(path for path in matches if path.name != "test_golden_path.py")


def load_durations(path: Path) -> dict[str, float]:
    """Read the recorded ``{repo-relative test file: seconds}`` map.

    Missing or unreadable is not an error: the sharder falls back to treating
    every file as average, which is the old file-count split. A CI job must not
    fail because a cost hint went stale — the hint only decides *where* a file
    runs, never *whether* it runs.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    durations = payload.get("durations") if isinstance(payload, dict) else None
    if not isinstance(durations, dict):
        return {}
    out: dict[str, float] = {}
    for name, seconds in durations.items():
        # Per-entry conversion inside the guard: a valid-JSON but absurd value
        # (``10**400`` raises OverflowError, ``1e999`` parses to inf) would
        # otherwise escape the fallback and fail the shard job, which is the
        # one thing a cost *hint* must never do.
        try:
            value = float(seconds)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and 0.0 <= value <= MAX_FILE_SECONDS:
            out[str(name)] = value
    return out


def shard_files(
    files: list[Path],
    *,
    index: int,
    count: int,
    durations: dict[str, float] | None = None,
    repo_root: Path | None = None,
) -> list[Path]:
    """Partition test files into *count* shards of roughly equal cost.

    Longest-processing-time-first bin packing: sort by recorded cost, hand each
    file to the lightest shard so far. The previous ``files[index::count]``
    stride split by file *count*, which measured 4m50s against 6m03s locally
    (#2060) — and, worse, reassigned 141 of 465 files whenever a single new
    test file shifted the alternation, so no shard's runtime was predictable
    from the last run's.

    A file with no recorded duration is charged the mean of the recorded ones,
    so a newly added test lands on the lightest shard rather than
    systematically on shard 0. Ties break on the path, so the partition is
    fully deterministic and identical on every runner.
    """
    if count < 1:
        raise ValueError("shard count must be positive")
    if index < 0 or index >= count:
        raise ValueError(f"shard index must be in [0, {count}), got {index}")

    durations = durations or {}
    known = [seconds for seconds in durations.values() if seconds > 0.0]
    default = sum(known) / len(known) if known else 1.0

    def cost(path: Path) -> float:
        key = path.relative_to(repo_root).as_posix() if repo_root else path.as_posix()
        return durations.get(key, default)

    # Heaviest first: LPT's guarantee comes from placing the big items while
    # there is still room to even them out.
    ordered = sorted(files, key=lambda path: (-cost(path), path.as_posix()))
    loads = [0.0] * count
    shards: list[list[Path]] = [[] for _ in range(count)]
    for path in ordered:
        target = min(range(count), key=lambda shard: (loads[shard], shard))
        shards[target].append(path)
        loads[target] += cost(path)
    return sorted(shards[index])


def durations_from_junit(xml_path: Path, repo_root: Path) -> dict[str, float]:
    """Sum a JUnit XML report's per-test times into per-file totals.

    pytest writes the module as a dotted ``classname`` (with the test class
    appended, when there is one) and no file attribute, so the path is
    recovered by walking the dotted name back. The walk only accepts a
    candidate that is one of the files the sharder actually distributes: a
    plain "first path that exists" walk charges
    ``tests.test_api.TestRoutes`` to ``tests/test_api/TestRoutes.py`` if such
    a file happens to exist, silently moving a module's cost onto a namesake.
    Restricting to the known set also drops ``test_golden_path.py``, whose
    cost belongs to its own job rather than to a shard.

    Unresolvable entries are skipped rather than guessed — a wrong key charges
    its cost to the wrong file, which is worse than charging it to none.
    """
    known = {path.relative_to(repo_root).as_posix() for path in test_files(repo_root)}
    totals: dict[str, float] = defaultdict(float)
    root = ET.parse(xml_path).getroot()  # noqa: S314 - our own CI artifact
    for case in root.iter("testcase"):
        classname = case.get("classname") or ""
        try:
            seconds = float(case.get("time") or 0.0)
        except ValueError:
            continue
        parts = classname.split(".")
        while parts:
            candidate = Path(*parts).with_suffix(".py").as_posix()
            if candidate in known:
                totals[candidate] += seconds
                break
            parts.pop()
    return dict(totals)


def write_durations(totals: dict[str, float], path: Path, *, platform: str) -> None:
    """Persist the per-file cost map, rounded and key-sorted for a clean diff."""
    payload = {
        "note": (
            "Per-file pytest cost in seconds, used by run_pytest_shard.py to balance "
            "the Windows shards. Regenerate with: "
            "uv run pytest -m 'not ollama and not llm' --junit-xml=junit.xml && "
            "uv run python tools/run_pytest_shard.py --record-durations junit.xml. "
            "Stale or missing entries only cost balance, never correctness."
        ),
        "platform": platform,
        "durations": {name: round(totals[name], 3) for name in sorted(totals)},
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-index", type=int)
    parser.add_argument("--shard-count", type=int)
    parser.add_argument(
        "--record-durations",
        type=Path,
        metavar="JUNIT_XML",
        help="Rewrite the cost map from a JUnit XML report and exit.",
    )
    args, pytest_args = parser.parse_known_args(argv)
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]

    repo_root = Path(__file__).resolve().parents[1]
    durations_path = Path(__file__).resolve().parent / DURATIONS_FILE

    if args.record_durations is not None:
        totals = durations_from_junit(args.record_durations, repo_root)
        if not totals:
            parser.error(f"no test files resolved from {args.record_durations}")
        write_durations(totals, durations_path, platform=sys.platform)
        print(f"recorded {len(totals)} file durations to {durations_path}")
        return 0

    if args.shard_index is None or args.shard_count is None:
        parser.error("--shard-index and --shard-count are required to run a shard")

    selected = shard_files(
        test_files(repo_root),
        index=args.shard_index,
        count=args.shard_count,
        durations=load_durations(durations_path),
        repo_root=repo_root,
    )
    if not selected:
        parser.error("selected shard contains no test files")
    command = [sys.executable, "-m", "pytest", *(str(path) for path in selected), *pytest_args]
    return subprocess.call(command, cwd=repo_root)  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
