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
