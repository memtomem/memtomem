"""End-to-end ``mm quality`` round-trip across the process boundary (#1802).

The in-process CLI tests (``test_quality_cli.py``) bind module-level constants
like ``_bootstrap._CONFIG_PATH`` at import time, so they can mask HOME/config
resolution regressions. This drives the installed ``mm`` binary against a fake
HOME with a real (BM25-only, no embedder download) config: index → import →
replay → profile-knob change → replay → compare, plus an export/import round
trip. Mirrors the inline+subprocess split of ``test_context_cli_subprocess_e2e``.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys

import pytest

from .helpers import _MEMTOMEM_ENV_VARS


def _mm_bin() -> str:
    bin_dir = os.path.dirname(sys.executable)
    mm_bin = shutil.which("mm", path=bin_dir)
    if mm_bin is None:
        pytest.fail(
            f"mm binary not found in {bin_dir}. "
            "Run `uv pip install -e packages/memtomem[all]` before testing."
        )
    return mm_bin


def test_quality_e2e_via_mm_binary(tmp_path):
    mm_bin = _mm_bin()
    home = tmp_path / "home"
    (home / ".memtomem").mkdir(parents=True)
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    db_path = tmp_path / "e2e.db"

    (mem_dir / "note.md").write_text(
        "# Alpha\n\nAlpha beta gamma retrieval quality lab content.\n",
        encoding="utf-8",
    )

    # BM25-only config (dimension>0 satisfies chunks_vec; no embedder pulled).
    config = {
        "storage": {"sqlite_path": str(db_path)},
        "indexing": {"memory_dirs": [str(mem_dir)]},
        "embedding": {"provider": "none", "dimension": 1024},
        "search": {"enable_dense": False},
    }
    (home / ".memtomem" / "config.json").write_text(json.dumps(config), encoding="utf-8")

    env = os.environ.copy()
    for var in _MEMTOMEM_ENV_VARS:
        env.pop(var, None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")

    def _run(*args: str, extra_env: dict | None = None) -> subprocess.CompletedProcess:
        run_env = dict(env)
        if extra_env:
            run_env.update(extra_env)
        return subprocess.run(
            [mm_bin, *args],
            env=run_env,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    r = _run("index", str(mem_dir))
    assert r.returncode == 0, f"index failed:\nstdout={r.stdout}\nstderr={r.stderr}"

    # Read a real content_hash straight from the DB to build a labeled case.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute("SELECT content_hash FROM chunks LIMIT 1").fetchone()
    finally:
        conn.close()
    assert row, "indexing produced no chunks"
    content_hash = row[0]

    envelope = {
        "schema_version": 1,
        "kind": "eval_case_set",
        "cases": [
            {
                "case_id": "e2e-case-1",
                "name": "alpha-q",
                "query_text": "alpha",
                "top_k": 5,
                "version": 1,
                "status": "active",
                "filters": {"namespace": None, "scope": None},
                "labels": [{"content_hash": content_hash, "judgment": "relevant"}],
            }
        ],
    }
    env_file = tmp_path / "cases.json"
    env_file.write_text(json.dumps(envelope), encoding="utf-8")

    r = _run("quality", "import", str(env_file))
    assert r.returncode == 0, f"import failed:\nstdout={r.stdout}\nstderr={r.stderr}"
    assert json.loads(r.stdout)["imported"] == 1

    base = tmp_path / "base.json"
    r = _run("quality", "replay", "--as-of", "1784500000", "--out", str(base))
    assert r.returncode == 0, f"replay failed:\nstdout={r.stdout}\nstderr={r.stderr}"
    assert base.is_file()

    # Change a ranking-affecting knob via env → same corpus/index, new profile.
    cand = tmp_path / "cand.json"
    r = _run(
        "quality",
        "replay",
        "--as-of",
        "1784500000",
        "--out",
        str(cand),
        extra_env={"MEMTOMEM_SEARCH__RRF_K": "97"},
    )
    assert r.returncode == 0, f"candidate replay failed:\nstdout={r.stdout}\nstderr={r.stderr}"

    base_report = json.loads(base.read_text())
    cand_report = json.loads(cand.read_text())
    assert base_report["fingerprints"]["profile"] != cand_report["fingerprints"]["profile"]
    assert base_report["fingerprints"]["case_set"] == cand_report["fingerprints"]["case_set"]

    r = _run("quality", "compare", str(base), str(cand), "--format", "json")
    assert r.returncode == 0, f"compare failed:\nstdout={r.stdout}\nstderr={r.stderr}"
    comparison = json.loads(r.stdout)
    assert comparison["compatibility"]["profile_match"] is False
    assert comparison["compatibility"]["case_set_match"] is True
    assert len(comparison["cases"]) == 1

    # export → import --replace round trip.
    export_file = tmp_path / "exported.json"
    r = _run("quality", "export", "--out", str(export_file))
    assert r.returncode == 0, f"export failed:\nstdout={r.stdout}\nstderr={r.stderr}"
    r = _run("quality", "import", str(export_file), "--replace")
    assert r.returncode == 0, f"re-import failed:\nstdout={r.stdout}\nstderr={r.stderr}"
    assert json.loads(r.stdout)["imported"] == 1


def test_quality_experiment_e2e_via_mm_binary(tmp_path):
    """Baseline + 2 candidates, BM25-only (no embedder download): the #1844
    acceptance run — deterministic byte-identical output, name ordering,
    shared-snapshot compatibility, a per-candidate gate, and no search-history
    mutation (record=False)."""
    mm_bin = _mm_bin()
    home = tmp_path / "home"
    (home / ".memtomem").mkdir(parents=True)
    mem_dir = tmp_path / "mem"
    mem_dir.mkdir()
    db_path = tmp_path / "e2e.db"

    (mem_dir / "note.md").write_text(
        "# Alpha\n\nAlpha beta gamma retrieval quality lab content.\n",
        encoding="utf-8",
    )
    config = {
        "storage": {"sqlite_path": str(db_path)},
        "indexing": {"memory_dirs": [str(mem_dir)]},
        "embedding": {"provider": "none", "dimension": 1024},
        "search": {"enable_dense": False},
    }
    (home / ".memtomem" / "config.json").write_text(json.dumps(config), encoding="utf-8")

    env = os.environ.copy()
    for var in _MEMTOMEM_ENV_VARS:
        env.pop(var, None)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")

    def _run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [mm_bin, *args],
            env=env,
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    assert _run("index", str(mem_dir)).returncode == 0

    conn = sqlite3.connect(str(db_path))
    try:
        content_hash = conn.execute("SELECT content_hash FROM chunks LIMIT 1").fetchone()[0]
    finally:
        conn.close()

    envelope = {
        "schema_version": 1,
        "kind": "eval_case_set",
        "cases": [
            {
                "case_id": "e2e-case-1",
                "name": "alpha-q",
                "query_text": "alpha",
                "top_k": 5,
                "version": 1,
                "status": "active",
                "filters": {"namespace": None, "scope": None},
                "labels": [{"content_hash": content_hash, "judgment": "relevant"}],
            }
        ],
    }
    (tmp_path / "cases.json").write_text(json.dumps(envelope), encoding="utf-8")
    assert _run("quality", "import", str(tmp_path / "cases.json")).returncode == 0

    # BM25-only profiles: enable_dense is an eligible knob, so it MUST be set
    # explicitly — an omitted knob resolves to the package default (dense on),
    # which would need an embedder. Candidates differ only by rrf_k.
    def _profile(name: str, rrf_k: int, extra_knobs: dict | None = None) -> str:
        knobs = {"search": {"enable_dense": False, "rrf_k": rrf_k}}
        if extra_knobs:
            knobs.update(extra_knobs)
        path = tmp_path / f"{name}.json"
        path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "retrieval_profile",
                    "name": name,
                    "knobs": knobs,
                }
            ),
            encoding="utf-8",
        )
        return str(path)

    baseline = _profile("baseline", 60)
    cand_a = _profile("candidate-a", 40)
    cand_b = _profile("candidate-b", 97)
    # candidate-c enables access boost — its per-report index fingerprint folds
    # in access counts, so it differs from the baseline's. This must NOT be
    # treated as corpus/index drift (the profile-independent snapshot is what
    # guards drift); it is the regression check for the profile-dependent-index
    # false rejection.
    cand_c = _profile("candidate-c", 60, {"access": {"enabled": True, "max_boost": 1.5}})

    def _experiment(out_name: str, *extra: str) -> subprocess.CompletedProcess:
        return _run(
            "quality",
            "experiment",
            "--baseline",
            baseline,
            "--profile",
            cand_b,  # deliberately out of name order to prove sorting
            "--profile",
            cand_a,
            "--profile",
            cand_c,
            "--as-of",
            "1784500000",
            "--format",
            "json",
            "--out",
            str(tmp_path / out_name),
            *extra,
        )

    def _query_history_rows() -> int:
        conn = sqlite3.connect(str(db_path))
        try:
            return conn.execute("SELECT COUNT(*) FROM query_history").fetchone()[0]
        finally:
            conn.close()

    before = _query_history_rows()

    r1 = _experiment("exp1.json")
    assert r1.returncode == 0, f"experiment failed:\nstdout={r1.stdout}\nstderr={r1.stderr}"
    r2 = _experiment("exp2.json")
    assert r2.returncode == 0

    exp1 = (tmp_path / "exp1.json").read_bytes()
    exp2 = (tmp_path / "exp2.json").read_bytes()
    assert exp1 == exp2, "same deterministic inputs must produce byte-identical output"

    result = json.loads(exp1)
    assert result["kind"] == "quality_experiment"
    assert [c["profile_name"] for c in result["candidates"]] == [
        "candidate-a",
        "candidate-b",
        "candidate-c",
    ]
    assert result["baseline"]["source"] == "document"
    by_name = {c["profile_name"]: c for c in result["candidates"]}
    for cand in result["candidates"]:
        compat = cand["comparison"]["compatibility"]
        assert compat["corpus_match"] is True
        assert compat["case_set_match"] is True
        assert compat["profile_match"] is False
        assert cand["gate"] is None
    # candidate-c enables access boost → a legitimately different per-report
    # index fingerprint, which must NOT abort the experiment (blocker fix).
    assert by_name["candidate-c"]["comparison"]["compatibility"]["index_match"] is False
    assert by_name["candidate-a"]["comparison"]["compatibility"]["index_match"] is True

    # record=False: replaying every profile mutated no search history.
    assert _query_history_rows() == before

    # A supplied policy is evaluated per candidate; min_compared_cases far above
    # the single fixture case fails every candidate deterministically → exit 1,
    # all verdicts present, result still emitted.
    policy = tmp_path / "policy.json"
    policy.write_text(
        json.dumps({"schema_version": 1, "kind": "replay_gate_policy", "min_compared_cases": 99}),
        encoding="utf-8",
    )
    rp = _experiment("exp3.json", "--policy", str(policy))
    assert rp.returncode == 1
    gated = json.loads((tmp_path / "exp3.json").read_text())
    assert gated["policy_supplied"] is True
    assert all(c["gate"]["pass"] is False for c in gated["candidates"])
