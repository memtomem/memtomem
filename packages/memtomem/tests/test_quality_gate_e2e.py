"""End-to-end machinery tests for the quality-gate CI driver (#1833 PR-B).

These drive ``tools/quality-gate/run_gate.py`` as a subprocess. The
refresh/check tests use the real ``mm`` binary and the committed corpus (so they
need it installed); the emit-boundary tests substitute a fake ``mm`` via
``--mm-bin`` to prove the driver never forwards raw child output.

None of these assert a committed-baseline-vs-fresh-candidate comparison — they
refresh into a throwaway asset copy first, so a cross-OS ranking difference in
the committed baseline never turns this blocking job red.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_DRIVER = _REPO_ROOT / "tools" / "quality-gate" / "run_gate.py"
_ASSETS_SRC = _REPO_ROOT / "tools" / "quality-gate"


def _load_driver_module():
    spec = importlib.util.spec_from_file_location("qg_run_gate", _DRIVER)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# A distinctive, non-secret, non-path-shaped marker: if it ever appears in the
# driver's own stdout/stderr, the emit boundary leaked child output.
_SENTINEL = "QG_CHILD_LEAK_MARKER_9137"

_FAKE_MM = """#!/usr/bin/env python3
import os
import sys

SENTINEL = "QG_CHILD_LEAK_MARKER_9137"
args = sys.argv[1:]
scenario = os.environ.get("FAKE_MM_SCENARIO", "")


def _out_path():
    for i, a in enumerate(args):
        if a == "--out" and i + 1 < len(args):
            return args[i + 1]
    return None


# index is the driver's first stage; fail it while leaking a path + marker.
if args[:1] == ["index"]:
    if scenario == "fail-index":
        sys.stdout.write("indexing /private/var/leak/abs/path " + SENTINEL + "\\n")
        sys.stderr.write("index error at /private/var/secret " + SENTINEL + "\\n")
        sys.exit(1)
    sys.stdout.write("indexed ok\\n")
    sys.exit(0)

if args[:2] == ["quality", "import"]:
    sys.stdout.write('{"ok": true, "imported": 1}\\n')
    sys.exit(0)

if args[:2] == ["quality", "replay"]:
    out = _out_path()
    if out:
        with open(out, "w") as fh:
            fh.write("{}\\n")
    sys.exit(0)

if args[:2] == ["quality", "gate"]:
    import json as _json

    # A consistent violation verdict: pass False with a non-empty violation list,
    # exiting 1. Written to --out (what the driver trusts); a marker is ALSO
    # sprayed into stdout AND stderr, neither of which the driver may forward.
    verdict = {
        "schema_version": 1,
        "kind": "replay_gate_verdict",
        "pass": False,
        "violations": [{"rule": "verdict_count", "key": "regressed", "observed": 1}],
        "allowlisted": [],
        "warnings": [],
        "summary_effective": {},
    }
    if scenario == "gate-inconsistent":
        verdict["violations"] = []  # pass False but no violations → contradictory
    elif scenario == "gate-extra-key":
        verdict["surprise"] = "/private/var/leak " + SENTINEL  # extra top-level payload
    elif scenario == "gate-nested-leak":
        verdict["warnings"] = ["/private/var/leak " + SENTINEL]  # emit-risk nested string
    out = _out_path()
    if scenario != "gate-no-verdict" and out:
        with open(out, "w") as fh:
            fh.write(_json.dumps(verdict) + "\\n")
    sys.stdout.write("gate stdout /private/var/leak " + SENTINEL + "\\n")
    sys.stderr.write("gate stderr /private/var/leak " + SENTINEL + "\\n")
    sys.exit(1)

sys.exit(3)
"""


def _mm_available() -> bool:
    return shutil.which("mm", path=os.path.dirname(sys.executable)) is not None


def _run_driver(args: list[str], extra_env: dict[str, str] | None = None):
    env = os.environ.copy()
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(_DRIVER), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=300,
        env=env,
    )


def _copy_hand_assets(dst: Path) -> Path:
    """Copy only the hand-written assets (fixture + policy) into ``dst``."""
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("fixture.json", "policy.json"):
        shutil.copy(_ASSETS_SRC / name, dst / name)
    return dst


def _write_fake_mm(tmp_path: Path) -> Path:
    fake = tmp_path / "fake_mm.py"
    fake.write_text(_FAKE_MM, encoding="utf-8")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


# --------------------------------------------------------------------------- #
# Real-mm machinery                                                           #
# --------------------------------------------------------------------------- #

_needs_mm = pytest.mark.skipif(not _mm_available(), reason="mm binary not installed")


@_needs_mm
def test_refresh_then_check_passes(tmp_path: Path) -> None:
    assets = _copy_hand_assets(tmp_path / "assets")
    refresh = _run_driver(["--refresh-baseline", "--assets-dir", str(assets)])
    assert refresh.returncode == 0, refresh.stderr
    assert (assets / "cases.json").is_file()
    assert (assets / "baseline_replay.json").is_file()

    check = _run_driver(["--assets-dir", str(assets)])
    assert check.returncode == 0, check.stderr
    verdict = json.loads(check.stdout)
    assert verdict["pass"] is True
    assert verdict["kind"] == "replay_gate_verdict"


@_needs_mm
def test_refresh_is_reproducible(tmp_path: Path) -> None:
    from memtomem.quality.compare import compare_reports
    from memtomem.quality.gate import evaluate_gate, load_policy

    a1 = _copy_hand_assets(tmp_path / "a1")
    a2 = _copy_hand_assets(tmp_path / "a2")
    assert _run_driver(["--refresh-baseline", "--assets-dir", str(a1)]).returncode == 0
    assert _run_driver(["--refresh-baseline", "--assets-dir", str(a2)]).returncode == 0

    # content_hash labels are byte-reproducible (they carry no scores).
    assert (a1 / "cases.json").read_bytes() == (a2 / "cases.json").read_bytes()

    b1 = json.loads((a1 / "baseline_replay.json").read_text(encoding="utf-8"))
    b2 = json.loads((a2 / "baseline_replay.json").read_text(encoding="utf-8"))
    # The ranking-relevant fingerprints match; only the timestamp-folding
    # corpus/index fingerprints churn across re-indexes (documented).
    assert b1["fingerprints"]["profile"] == b2["fingerprints"]["profile"]
    assert b1["fingerprints"]["case_set"] == b2["fingerprints"]["case_set"]
    # Two independent same-machine refreshes must gate as fully unchanged. Raw
    # BM25 score floats can wobble below epsilon across separate indexes, so the
    # guarantee is asserted at the gate's semantic level, not byte-exact dicts.
    policy = load_policy(json.loads((a1 / "policy.json").read_text(encoding="utf-8")))
    verdict = evaluate_gate(compare_reports(b1, b2), policy)
    assert verdict["pass"] is True, verdict["violations"]
    assert verdict["summary_effective"]["unchanged"] == len(b1["cases"])


@_needs_mm
def test_mutated_case_fails_gate(tmp_path: Path) -> None:
    assets = _copy_hand_assets(tmp_path / "assets")
    assert _run_driver(["--refresh-baseline", "--assets-dir", str(assets)]).returncode == 0

    # Mutate one committed case's top_k: the candidate's case set no longer
    # matches the baseline, so the required case_set_match flag fails.
    cases = json.loads((assets / "cases.json").read_text(encoding="utf-8"))
    cases["cases"][0]["top_k"] = 1
    (assets / "cases.json").write_text(json.dumps(cases), encoding="utf-8")

    result = _run_driver(["--assets-dir", str(assets)])
    assert result.returncode == 1, (
        f"expected policy violation, got {result.returncode}: {result.stderr}"
    )
    verdict = json.loads(result.stdout)
    assert verdict["pass"] is False


# --------------------------------------------------------------------------- #
# Emit boundary (fake mm — no real binary needed)                            #
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(os.name == "nt", reason="fake mm uses a POSIX shebang")
def test_index_failure_hides_child_output(tmp_path: Path) -> None:
    assets = _copy_hand_assets(tmp_path / "assets")
    fake = _write_fake_mm(tmp_path)
    result = _run_driver(
        ["--assets-dir", str(assets), "--mm-bin", str(fake)],
        extra_env={"FAKE_MM_SCENARIO": "fail-index"},
    )
    assert result.returncode == 2
    combined = result.stdout + result.stderr
    assert _SENTINEL not in combined, "child marker leaked past the emit boundary"
    assert "/private/var" not in combined, "child absolute path leaked"
    assert "stage 'index' failed" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="fake mm uses a POSIX shebang")
def test_gate_violation_forwards_only_verdict(tmp_path: Path) -> None:
    assets = _copy_hand_assets(tmp_path / "assets")
    fake = _write_fake_mm(tmp_path)
    result = _run_driver(
        ["--assets-dir", str(assets), "--mm-bin", str(fake)],
        extra_env={"FAKE_MM_SCENARIO": "gate-violation"},
    )
    # exit 1 is preserved, and the validated verdict (read from the gate's --out
    # file, not its streams) is on stdout.
    assert result.returncode == 1, result.stderr
    verdict = json.loads(result.stdout)
    assert verdict["kind"] == "replay_gate_verdict"
    assert verdict["pass"] is False
    # The gate sprays the marker into BOTH stdout and stderr; neither is
    # forwarded — only the validated --out verdict reaches the driver's output.
    assert _SENTINEL not in result.stdout
    assert _SENTINEL not in result.stderr
    assert "/private/var" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="fake mm uses a POSIX shebang")
def test_gate_without_verdict_file_is_infra(tmp_path: Path) -> None:
    # Gate exits 0/1 but never wrote the --out verdict → the driver cannot trust
    # its streams, so it must discard everything and exit 2.
    assets = _copy_hand_assets(tmp_path / "assets")
    fake = _write_fake_mm(tmp_path)
    result = _run_driver(
        ["--assets-dir", str(assets), "--mm-bin", str(fake)],
        extra_env={"FAKE_MM_SCENARIO": "gate-no-verdict"},
    )
    assert result.returncode == 2
    assert _SENTINEL not in (result.stdout + result.stderr)
    assert "stage 'gate' failed" in result.stderr


@pytest.mark.skipif(os.name == "nt", reason="fake mm uses a POSIX shebang")
def test_gate_contradictory_verdict_is_infra(tmp_path: Path) -> None:
    # Exit 1 but the verdict says pass with no violations → the gate is
    # misbehaving; the driver must not return a bogus code.
    assets = _copy_hand_assets(tmp_path / "assets")
    fake = _write_fake_mm(tmp_path)
    result = _run_driver(
        ["--assets-dir", str(assets), "--mm-bin", str(fake)],
        extra_env={"FAKE_MM_SCENARIO": "gate-inconsistent"},
    )
    assert result.returncode == 2
    assert _SENTINEL not in (result.stdout + result.stderr)


@pytest.mark.skipif(os.name == "nt", reason="fake mm uses a POSIX shebang")
def test_gate_extra_payload_is_rejected(tmp_path: Path) -> None:
    # A verdict carrying an unexpected top-level key (with a marker inside) must
    # be rejected, never reserialized onto the driver's stdout.
    assets = _copy_hand_assets(tmp_path / "assets")
    fake = _write_fake_mm(tmp_path)
    result = _run_driver(
        ["--assets-dir", str(assets), "--mm-bin", str(fake)],
        extra_env={"FAKE_MM_SCENARIO": "gate-extra-key"},
    )
    assert result.returncode == 2
    assert _SENTINEL not in (result.stdout + result.stderr)
    assert "surprise" not in result.stdout


@pytest.mark.skipif(os.name == "nt", reason="fake mm uses a POSIX shebang")
def test_gate_nested_emit_risk_is_rejected(tmp_path: Path) -> None:
    # A structurally valid, consistent verdict whose nested `warnings` string
    # hides an absolute path must be rejected — exact top-level keys are not
    # enough; every nested string is scanned before the verdict is forwarded.
    assets = _copy_hand_assets(tmp_path / "assets")
    fake = _write_fake_mm(tmp_path)
    result = _run_driver(
        ["--assets-dir", str(assets), "--mm-bin", str(fake)],
        extra_env={"FAKE_MM_SCENARIO": "gate-nested-leak"},
    )
    assert result.returncode == 2
    assert _SENTINEL not in (result.stdout + result.stderr)
    assert "/private/var" not in result.stdout


def test_publish_all_rolls_back_on_partial_failure(tmp_path: Path) -> None:
    # First asset swaps successfully, the second fails (its dest is a directory,
    # so os.replace raises) → the first must roll back to its old content and the
    # whole publish must raise, never leaving cases new while baseline is old.
    drv = _load_driver_module()
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "cases.json").write_text("OLD_CASES", encoding="utf-8")
    (assets / "baseline_replay.json").mkdir()  # replacing a file onto a dir fails

    new_cases = tmp_path / "new_cases.json"
    new_cases.write_text("NEW_CASES", encoding="utf-8")
    new_baseline = tmp_path / "new_baseline.json"
    new_baseline.write_text("NEW_BASELINE", encoding="utf-8")

    with pytest.raises(Exception):
        drv._publish_all(
            [
                (new_cases, assets / "cases.json"),
                (new_baseline, assets / "baseline_replay.json"),
            ]
        )
    # cases.json rolled back; no leftover .new / .bak sidecars.
    assert (assets / "cases.json").read_text(encoding="utf-8") == "OLD_CASES"
    assert not list(assets.glob("*.new"))
    assert not list(assets.glob("*.bak"))


def test_publish_all_rolls_back_into_fresh_dir(tmp_path: Path) -> None:
    # Fresh assets dir: both destinations are absent. The first swap creates
    # cases.json, the second fails → the newly created first asset must be
    # removed, not left behind.
    drv = _load_driver_module()
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "baseline_replay.json").mkdir()  # a dir → the second swap fails

    new_cases = tmp_path / "new_cases.json"
    new_cases.write_text("NEW_CASES", encoding="utf-8")
    new_baseline = tmp_path / "new_baseline.json"
    new_baseline.write_text("NEW_BASELINE", encoding="utf-8")

    with pytest.raises(Exception):
        drv._publish_all(
            [
                (new_cases, assets / "cases.json"),
                (new_baseline, assets / "baseline_replay.json"),
            ]
        )
    # The created-then-rolled-back asset must be gone, no sidecars left.
    assert not (assets / "cases.json").exists()
    assert not list(assets.glob("*.new"))
    assert not list(assets.glob("*.bak"))


def test_missing_mm_binary_is_infra(tmp_path: Path) -> None:
    # A non-launchable mm (OSError at spawn) must map to exit 2, not a traceback.
    assets = _copy_hand_assets(tmp_path / "assets")
    missing = tmp_path / "does-not-exist-mm"
    result = _run_driver(["--assets-dir", str(assets), "--mm-bin", str(missing)])
    assert result.returncode == 2
    assert "stage 'index' failed" in result.stderr
    # The non-existent path must not be echoed into the diagnostic.
    assert str(missing) not in result.stderr
