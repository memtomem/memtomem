#!/usr/bin/env python3
"""Record where the committed retrieval baselines came from.

These two files carry *quality floors*, and a floor that moved down is a claim
that the drop was acceptable. Reviewing that claim later needs more than the
numbers: which commit produced them, on which runner, with which interpreter
and dependency versions, and whether the files in the tree are still the ones
that run produced. ``baseline_v2.json`` records only the memtomem version and
``baseline_v0.3.8.json`` only its platform block, and neither binds the pair
together or to a workflow run (#2224 review).

Written by the ``refresh_retrieval_baselines`` workflow_dispatch path and
committed alongside the baselines it describes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
#: The files this manifest vouches for, and the tools that produce them.
_BASELINES = {
    "baseline_v0.3.8.json": (
        "uv run python tools/retrieval-eval/calibrate_portfolio.py --runs 10 "
        "--factor 0.85 --output tools/retrieval-eval/baseline_v0.3.8.json"
    ),
    "baseline_v2.json": (
        "uv run python tools/retrieval-eval/tune_rrf_v2.py "
        "--output tools/retrieval-eval/baseline_v2.json"
    ),
}
#: Set for both commands by the workflow. Recorded because they are part of the
#: measurement, not of the invocation's spelling.
_MEASUREMENT_ENV = ("PYTHONHASHSEED", "OMP_NUM_THREADS")
#: Pinned in the manifest because a floor is only comparable against the stack
#: that measured it. Not a lockfile — the lockfile is `uv.lock`; this records
#: what was actually importable at measurement time.
_TRACKED_DISTRIBUTIONS = ("fastembed", "onnxruntime", "numpy", "tokenizers", "kiwipiepy")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _distribution_versions() -> dict[str, str | None]:
    from importlib.metadata import PackageNotFoundError, version

    out: dict[str, str | None] = {}
    for name in _TRACKED_DISTRIBUTIONS:
        try:
            out[name] = version(name)
        except PackageNotFoundError:
            # Recorded as absent rather than omitted: "kiwipiepy was not
            # installed" is itself a fact about the measurement.
            out[name] = None
    return out


def _commit() -> str:
    # The workflow passes the SHA it checked out; the git call is the local
    # fallback so a maintainer reproducing by hand still gets a real answer.
    env = os.environ.get("MEMTOMEM_PROVENANCE_COMMIT")
    if env:
        return env
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=_HERE,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def build() -> dict[str, object]:
    return {
        "schema_version": 1,
        "kind": "retrieval_baseline_provenance",
        "description": (
            "Where the committed retrieval-eval baselines were produced. "
            "Regenerate with the CI workflow_dispatch input "
            "refresh_retrieval_baselines; see tools/retrieval-eval/README.md."
        ),
        "source_commit": _commit(),
        "workflow_run_url": os.environ.get("MEMTOMEM_PROVENANCE_RUN_URL"),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": sys.version.split()[0],
            # FTS5 computes the BM25 half of every one of these numbers, so the
            # SQLite build is as much a part of the measurement as the models.
            "sqlite": sqlite3.sqlite_version,
        },
        # ``null`` here means the variable was genuinely unset when the
        # baselines were produced, which makes the run non-reproducible in the
        # way these two settings exist to prevent. Recorded rather than
        # defaulted so that shows up in review instead of reading as fine.
        "measurement_env": {name: os.environ.get(name) for name in _MEASUREMENT_ENV},
        "distributions": _distribution_versions(),
        "baselines": {
            name: {
                "command": command,
                "sha256": _sha256(_HERE / name),
            }
            for name, command in _BASELINES.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the recorded hashes still describe the committed baselines",
    )
    args = parser.parse_args()

    if args.check:
        recorded = json.loads(args.output.read_text(encoding="utf-8"))
        # Coverage before hashes: iterating whatever the manifest happens to
        # list is fail-open — an empty ``baselines`` object, or one with a file
        # quietly dropped, verifies clean while checking nothing. The manifest
        # has to describe exactly the canonical set.
        covered = set(recorded.get("baselines", {}))
        expected = set(_BASELINES)
        if covered != expected:
            missing = ", ".join(sorted(expected - covered)) or "none"
            unexpected = ", ".join(sorted(covered - expected)) or "none"
            print(
                f"provenance does not cover the canonical baselines "
                f"(missing: {missing}; unexpected: {unexpected})",
                file=sys.stderr,
            )
            return 1
        unset = sorted(
            name for name, value in recorded.get("measurement_env", {}).items() if value is None
        )
        if unset:
            print(
                "provenance records an unpinned measurement environment "
                f"({', '.join(unset)} unset) — regenerate on a run that sets them",
                file=sys.stderr,
            )
            return 1
        drift = [
            name
            for name, entry in recorded["baselines"].items()
            if entry["sha256"] != _sha256(_HERE / name)
        ]
        if drift:
            print(
                "provenance does not describe the committed baselines: " + ", ".join(sorted(drift)),
                file=sys.stderr,
            )
            return 1
        print(f"provenance matches {len(recorded['baselines'])} baseline(s)")
        return 0

    args.output.write_text(
        json.dumps(build(), indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
