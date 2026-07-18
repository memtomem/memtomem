#!/usr/bin/env python3
"""CI driver for the policy-driven replay quality gate (#1833, Quality Lab Q4).

Two modes, both driving the installed ``mm`` binary in an isolated environment
(a throwaway ``$HOME`` + temp sqlite DB, all ``MEMTOMEM_*`` env scrubbed) so a
run never touches a developer's real ``~/.memtomem/``:

* **check mode** (default) — index the committed fixture corpus, import the
  committed ``cases.json``, replay a fresh *candidate* report, and gate it
  against the committed ``baseline_replay.json`` + ``policy.json``. Exit
  ``0`` pass / ``1`` policy violation / ``2`` infrastructure error.
* **``--refresh-baseline``** — regenerate ``cases.json`` (resolving each case's
  ``relevant_globs`` to chunk ``content_hash`` labels) and ``baseline_replay.json``
  from ``fixture.json``, then self-check that the fresh baseline passes its own
  gate. Ubuntu is the canonical producer (see README).

Emit boundary (Codex design-gate rounds 4-6): a child ``mm`` process can print
resolved/blocked absolute paths and raw per-file errors, so its stdout/stderr is
always *captured* and never forwarded raw. The one exception is the gate's own
``--format json`` verdict on exit 0/1 — that is built by the emit-safe
:func:`serialize_gate_verdict`, so it is forwarded (and only it) so a real
policy violation still surfaces its verdict. A child failure at any other stage
is collapsed to a fixed ``stage``/``role`` diagnostic and exit ``2``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

EXIT_PASS = 0
EXIT_VIOLATION = 1
EXIT_INFRA = 2

#: The exact top-level shape of a ``replay_gate_verdict`` (PR-A,
#: ``quality/gate.py``). The driver forwards a gate verdict only after checking
#: the parsed ``--out`` file has EXACTLY these keys with the right types — no
#: extra top-level payload crosses the emit boundary — and that ``pass`` is
#: internally and exit-code consistent.
_VERDICT_LIST_KEYS = ("violations", "allowlisted", "warnings")
_VERDICT_KEYS = frozenset(
    {"schema_version", "kind", "pass", "summary_effective", *_VERDICT_LIST_KEYS}
)

_CHILD_TIMEOUT = 300  # seconds — generous ceiling for a 48-file BM25 index + replay

_HERE = Path(__file__).resolve().parent
# tools/quality-gate/run_gate.py -> repo root is two parents up.
_REPO_ROOT = _HERE.parents[1]


class _InfraError(Exception):
    """A pre-gate stage failed; the driver must exit 2 with a fixed diagnostic.

    Carries only a stage label and an optional role — never child output, a
    filesystem path, or a raw child error — so the emit boundary holds on
    failure.
    """

    def __init__(self, stage: str, detail: str = "") -> None:
        self.stage = stage
        self.detail = detail
        super().__init__(f"{stage}: {detail}" if detail else stage)


def _load_json(path: Path, stage: str) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        # Reference the asset by stage/role, never by path — the path could be
        # an absolute developer path that would leak into CI logs.
        raise _InfraError(stage, "unreadable or invalid JSON") from None


def _resolve_mm_bin(override: str | None) -> str:
    """Locate the ``mm`` CLI: an explicit override, else next to this Python."""
    if override:
        return override
    mm = shutil.which("mm", path=os.path.dirname(sys.executable)) or shutil.which("mm")
    if not mm:
        raise _InfraError("setup", "mm binary not found")
    return mm


def _isolated_env(home: Path) -> dict[str, str]:
    """A scrubbed environment pinned to a throwaway HOME and deterministic seeds."""
    env = os.environ.copy()
    for key in list(env):
        if key.startswith("MEMTOMEM_"):
            del env[key]
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    env["XDG_CONFIG_HOME"] = str(home / ".config")
    # Deterministic hashing / single-threaded math so BM25 ordering is stable
    # regardless of how the driver itself was launched.
    env["PYTHONHASHSEED"] = "0"
    env["OMP_NUM_THREADS"] = "1"
    return env


def _write_config(home: Path, sqlite_path: Path, corpus_abs: Path, overrides: dict) -> None:
    config: dict[str, Any] = {
        "storage": {"sqlite_path": str(sqlite_path)},
        "indexing": {"memory_dirs": [str(corpus_abs)]},
    }
    # Fixture overrides (embedding.provider=none, search.enable_dense=false) sit
    # at the top level alongside storage/indexing.
    config.update(overrides)
    cfg_dir = home / ".memtomem"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "config.json").write_text(json.dumps(config), encoding="utf-8")


def _run_child(
    mm: str, args: list[str], env: dict[str, str], cwd: Path, stage: str
) -> subprocess.CompletedProcess[str]:
    """Run a child ``mm`` invocation, mapping launch/timeout failures to exit 2.

    A ``TimeoutExpired`` or an ``OSError`` (e.g. the binary is missing or not
    executable) carries the command line — including absolute paths — in its
    message, so it is collapsed to a fixed stage diagnostic instead of being
    allowed to surface as a traceback.
    """
    try:
        return subprocess.run(
            [mm, *args],
            env=env,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_CHILD_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise _InfraError(stage, "child process timed out") from None
    except OSError:
        raise _InfraError(stage, "child process could not be launched") from None


def _corpus_abs(fixture: dict) -> Path:
    corpus_dir = fixture.get("corpus_dir")
    if not isinstance(corpus_dir, str) or not corpus_dir:
        raise _InfraError("fixture", "missing corpus_dir")
    return (_REPO_ROOT / corpus_dir).resolve()


def _stage_corpus(fixture: dict, tmp: Path) -> Path:
    """Copy the committed corpus into the temp dir before indexing.

    Indexing writes ``.<name>.lock`` sidecars next to each source file; pointing
    it at the tracked corpus would pollute the working tree. A copy keeps every
    side effect inside the auto-removed temp dir, and ``content_hash`` is byte
    identity so the copy resolves to the same labels as the original.
    """
    src = _corpus_abs(fixture)
    if not src.is_dir():
        raise _InfraError("fixture", "corpus_dir does not exist")
    staged = tmp / "corpus"
    try:
        shutil.copytree(src, staged)
    except OSError:
        raise _InfraError("refresh", "could not stage corpus") from None
    return staged


def _read_chunk_rows(sqlite_path: Path) -> list[tuple[str, str]]:
    try:
        conn = sqlite3.connect(str(sqlite_path))
        try:
            return conn.execute("SELECT content_hash, source_file FROM chunks").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        raise _InfraError("refresh", "index db read failed") from None


def _resolve_labels(
    corpus_abs: Path, chunk_rows: list[tuple[str, str]], globs: list[str]
) -> list[dict[str, str]]:
    """Resolve ``relevant_globs`` to a *complete* judgment set for one case.

    The globs' files are the relevant chunks; every OTHER chunk in the corpus is
    labelled ``not_relevant``. Complete judgments over the whole corpus keep
    precision comparable and stable for every case — technical terms
    (``Redis``, ``pg_partman`` …) let a query retrieve cross-language chunks, so
    a language-restricted pool would leave some retrieved items unlabelled and
    precision incomparable. Completeness is what lets the policy carry a
    precision floor that actually fires: an appended off-target result is a
    labelled ``not_relevant`` hit, so it lowers precision instead of silently
    dropping the case out of the comparable cohort.

    Files are joined to chunks by *relative-path suffix* because
    ``chunks.source_file`` is an absolute, machine-resolved path that differs
    across checkouts and OSes.
    """
    rel_targets: set[str] = set()
    for pattern in globs:
        for match in sorted(corpus_abs.glob(pattern)):
            if match.is_file():
                rel_targets.add(match.relative_to(corpus_abs).as_posix())
    if not rel_targets:
        raise _InfraError("refresh", "a case resolved to no corpus files")

    relevant: set[str] = set()
    all_hashes: set[str] = set()
    for content_hash, source_file in chunk_rows:
        all_hashes.add(content_hash)
        sf_posix = Path(source_file).as_posix()
        if any(sf_posix.endswith("/" + rel) for rel in rel_targets):
            relevant.add(content_hash)
    if not relevant:
        raise _InfraError("refresh", "a case resolved to no indexed chunks")
    not_relevant = all_hashes - relevant
    return [{"content_hash": h, "judgment": "relevant"} for h in sorted(relevant)] + [
        {"content_hash": h, "judgment": "not_relevant"} for h in sorted(not_relevant)
    ]


def _build_cases(fixture: dict, corpus_abs: Path, sqlite_path: Path) -> dict[str, Any]:
    case_defs = fixture.get("case_defs")
    if not isinstance(case_defs, list) or not case_defs:
        raise _InfraError("fixture", "missing case_defs")
    chunk_rows = _read_chunk_rows(sqlite_path)
    cases = []
    for cd in case_defs:
        if not isinstance(cd, dict):
            raise _InfraError("fixture", "a case_def is not an object")
        globs = cd.get("relevant_globs")
        if not isinstance(globs, list) or not globs:
            raise _InfraError("fixture", "a case_def is missing relevant_globs")
        try:
            cases.append(
                {
                    "case_id": cd["case_id"],
                    "name": cd["name"],
                    "query_text": cd["query_text"],
                    "top_k": cd["top_k"],
                    "version": 1,
                    "status": "active",
                    "filters": {"namespace": None, "scope": None},
                    "labels": _resolve_labels(corpus_abs, chunk_rows, globs),
                }
            )
        except KeyError:
            raise _InfraError("fixture", "a case_def is missing a required field") from None
    return {"schema_version": 1, "kind": "eval_case_set", "cases": cases}


def _serialize_asset(payload: dict) -> str:
    """Canonical, diff-stable JSON for a committed asset (trailing newline)."""
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


# --------------------------------------------------------------------------- #
# Pipeline stages                                                             #
# --------------------------------------------------------------------------- #


def _index(mm: str, corpus_abs: Path, env: dict[str, str], cwd: Path) -> None:
    proc = _run_child(mm, ["index", str(corpus_abs)], env, cwd, "index")
    if proc.returncode != 0:
        raise _InfraError("index")


def _import(mm: str, cases_path: Path, env: dict[str, str], cwd: Path) -> None:
    proc = _run_child(mm, ["quality", "import", str(cases_path), "--replace"], env, cwd, "import")
    if proc.returncode != 0:
        raise _InfraError("import")


def _replay(mm: str, as_of: int, out: Path, env: dict[str, str], cwd: Path) -> None:
    proc = _run_child(
        mm,
        ["quality", "replay", "--as-of", str(as_of), "--out", str(out), "--format", "json"],
        env,
        cwd,
        "replay",
    )
    if proc.returncode != 0:
        raise _InfraError("replay")


def _run_gate_verdict(
    mm: str,
    baseline: Path,
    candidate: Path,
    policy: Path,
    tmp: Path,
    env: dict[str, str],
    cwd: Path,
) -> tuple[int, str]:
    """Run the gate and return ``(exit_code, canonical_verdict_json)``.

    The verdict is read from a temp ``--out`` file the gate writes with its own
    emit-safe serializer — never from the child's stdout/stderr, which are
    discarded so no diagnostic can cross the emit boundary. The file is then
    structurally validated and canonically reserialized. Exit 0/1 with a valid
    verdict returns that code; any other outcome (unexpected exit, or a
    missing / malformed / wrong-kind verdict) raises ``_InfraError`` → exit 2.
    """
    verdict_path = tmp / "verdict.json"
    proc = _run_child(
        mm,
        [
            "quality",
            "gate",
            str(baseline),
            str(candidate),
            "--policy",
            str(policy),
            "--out",
            str(verdict_path),
            "--format",
            "json",
        ],
        env,
        cwd,
        "gate",
    )
    if proc.returncode not in (EXIT_PASS, EXIT_VIOLATION):
        raise _InfraError("gate")
    try:
        raw = json.loads(verdict_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise _InfraError("gate", "verdict was not written") from None
    verdict = _validate_verdict(raw, proc.returncode)
    # Reserialize the RECONSTRUCTED verdict (only the known keys, in canonical
    # order) — no extra top-level payload from the child's file crosses the
    # boundary. allow_nan=False rejects NaN/inf.
    canonical = json.dumps(verdict, sort_keys=True, ensure_ascii=False, indent=2, allow_nan=False)
    return proc.returncode, canonical + "\n"


def _validate_verdict(raw: Any, code: int) -> dict[str, Any]:
    """Strictly validate a gate ``--out`` verdict and return a clean dict.

    Rejects (→ exit 2) anything that is not exactly a ``replay_gate_verdict``
    with the expected key set and types, whose ``pass`` disagrees with its
    violation list, or whose ``pass`` disagrees with the child exit code (exit 0
    ⇔ pass). This catches a truncated / spoofed / contradictory verdict — e.g.
    exit 0 with ``"pass": false`` — instead of returning a bogus success. Only
    the reconstructed known keys are re-emitted, so no extra top-level payload
    survives.
    """
    if not isinstance(raw, dict) or set(raw) != _VERDICT_KEYS:
        raise _InfraError("gate", "verdict has an unexpected shape")
    if raw["schema_version"] != 1 or raw["kind"] != "replay_gate_verdict":
        raise _InfraError("gate", "verdict schema mismatch")
    passed = raw["pass"]
    if not isinstance(passed, bool) or not isinstance(raw["summary_effective"], dict):
        raise _InfraError("gate", "verdict field types are wrong")
    if any(not isinstance(raw[key], list) for key in _VERDICT_LIST_KEYS):
        raise _InfraError("gate", "verdict field types are wrong")
    if passed != (len(raw["violations"]) == 0):
        raise _InfraError("gate", "verdict pass flag disagrees with its violations")
    if (code == EXIT_PASS) != passed:
        raise _InfraError("gate", "verdict pass flag disagrees with the exit code")
    clean = {key: raw[key] for key in _VERDICT_KEYS}
    _assert_emit_safe(clean)
    return clean


def _assert_emit_safe(obj: Any) -> None:
    """Reject a verdict that carries any emit-risk string, at any nesting depth.

    Exact top-level keys don't stop a path or secret hiding inside a nested
    ``warnings``/``violations[*]``/``allowlisted[*]`` string, so every string in
    the reconstructed verdict is scanned with the shared secret/path detector
    (:func:`memtomem.privacy.has_emit_risk`) before it is reserialized. A hit
    means the gate produced (or something spoofed) output the driver must not
    forward → exit 2. JSON scalars only; anything else is already excluded by the
    structural checks above.
    """
    from memtomem.privacy import has_emit_risk

    def walk(value: Any) -> None:
        if isinstance(value, str):
            if has_emit_risk(value):
                raise _InfraError("gate", "verdict carries an emit-risk string")
        elif isinstance(value, dict):
            for key, sub in value.items():
                walk(key)
                walk(sub)
        elif isinstance(value, list):
            for sub in value:
                walk(sub)

    walk(obj)


# --------------------------------------------------------------------------- #
# Modes                                                                       #
# --------------------------------------------------------------------------- #


def _fixture_as_of(fixture: dict) -> int:
    value = fixture.get("as_of_unix")
    if not isinstance(value, int) or isinstance(value, bool):
        raise _InfraError("fixture", "missing or non-integer as_of_unix")
    return value


def _check(mm: str, assets: Path, fixture: dict, tmp: Path) -> int:
    """Index → import committed cases → replay candidate → gate vs baseline."""
    home = tmp / "home"
    sqlite_path = tmp / "gate.db"
    corpus_abs = _stage_corpus(fixture, tmp)
    as_of = _fixture_as_of(fixture)
    overrides = fixture.get("config_overrides", {})

    _write_config(home, sqlite_path, corpus_abs, overrides)
    env = _isolated_env(home)

    _index(mm, corpus_abs, env, tmp)
    _import(mm, assets / "cases.json", env, tmp)
    candidate = tmp / "candidate.json"
    _replay(mm, as_of, candidate, env, tmp)

    code, verdict = _run_gate_verdict(
        mm, assets / "baseline_replay.json", candidate, assets / "policy.json", tmp, env, tmp
    )
    # `verdict` is the validated, reserialized JSON — the only thing forwarded.
    sys.stdout.write(verdict)
    return code


def _publish_all(pairs: list[tuple[Path, Path]]) -> None:
    """Publish several freshly built assets together, all-or-nothing.

    Each asset is first copied to a sibling ``.new`` file (same directory → same
    filesystem), then the ``os.replace`` swaps run back-to-back with a backup of
    every prior destination kept. If any swap fails, the already-swapped
    destinations are rolled back from their backups, so a partial publish never
    leaves ``cases.json`` new while ``baseline_replay.json`` stays old. (A hard
    kill between two back-to-back ``os.replace`` calls is not userspace
    recoverable; re-running ``--refresh-baseline`` reconverges.)
    """
    staged: list[tuple[Path, Path]] = []
    backups: list[tuple[Path, Path]] = []
    created: list[Path] = []
    try:
        for tmp_file, dest in pairs:
            new = dest.with_name(dest.name + ".new")
            shutil.copyfile(tmp_file, new)
            staged.append((new, dest))
        for new, dest in staged:
            if dest.exists():
                bak = dest.with_name(dest.name + ".bak")
                shutil.copyfile(dest, bak)
                backups.append((bak, dest))
            else:
                # No prior version: on rollback this destination must be removed,
                # not restored, so a fresh-dir partial publish leaves nothing.
                created.append(dest)
            os.replace(new, dest)
    except OSError:
        for bak, dest in backups:
            try:
                os.replace(bak, dest)
            except OSError:
                pass
        for dest in created:
            dest.unlink(missing_ok=True)
        for new, _ in staged:
            new.unlink(missing_ok=True)
        raise _InfraError("refresh", "could not publish regenerated assets") from None
    for bak, _ in backups:
        bak.unlink(missing_ok=True)


def _refresh(mm: str, assets: Path, fixture: dict, tmp: Path) -> int:
    """Regenerate cases.json + baseline_replay.json, self-check, then publish.

    Both assets are built and self-checked entirely inside the temp dir; only
    after the fresh baseline passes its own policy are they published together
    (all-or-nothing, with rollback) so a failed publish never strands a partial
    refresh.
    """
    home = tmp / "home"
    sqlite_path = tmp / "gate.db"
    corpus_abs = _stage_corpus(fixture, tmp)
    as_of = _fixture_as_of(fixture)
    overrides = fixture.get("config_overrides", {})

    _write_config(home, sqlite_path, corpus_abs, overrides)
    env = _isolated_env(home)

    _index(mm, corpus_abs, env, tmp)
    cases = _build_cases(fixture, corpus_abs, sqlite_path)
    tmp_cases = tmp / "cases.json"
    try:
        tmp_cases.write_text(_serialize_asset(cases), encoding="utf-8")
    except OSError:
        raise _InfraError("refresh", "could not write cases") from None

    _import(mm, tmp_cases, env, tmp)
    tmp_baseline = tmp / "baseline_replay.json"
    _replay(mm, as_of, tmp_baseline, env, tmp)

    # Self-check the fresh baseline against its own policy BEFORE publishing.
    code, _ = _run_gate_verdict(
        mm, tmp_baseline, tmp_baseline, assets / "policy.json", tmp, env, tmp
    )
    if code != EXIT_PASS:
        raise _InfraError("refresh-self-check")

    _publish_all(
        [
            (tmp_cases, assets / "cases.json"),
            (tmp_baseline, assets / "baseline_replay.json"),
        ]
    )
    sys.stderr.write(
        f"refreshed {len(cases['cases'])} case(s): "
        "cases.json + baseline_replay.json (self-check passed)\n"
    )
    return EXIT_PASS


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Replay quality-gate CI driver (#1833).")
    parser.add_argument(
        "--refresh-baseline",
        action="store_true",
        help="Regenerate cases.json + baseline_replay.json from fixture.json.",
    )
    parser.add_argument(
        "--assets-dir",
        default=str(_HERE),
        help="Directory holding fixture.json / cases.json / baseline_replay.json / policy.json.",
    )
    parser.add_argument(
        "--mm-bin",
        default=None,
        help="Path to the mm binary (default: discovered next to this Python).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        # Resolve inside the guard so a bad --assets-dir maps to exit 2, not a
        # traceback echoing the supplied path.
        try:
            assets = Path(args.assets_dir).resolve()
        except OSError:
            raise _InfraError("setup", "invalid assets dir") from None
        mm = _resolve_mm_bin(args.mm_bin)
        fixture = _load_json(assets / "fixture.json", "fixture")
        with tempfile.TemporaryDirectory(prefix="mm-quality-gate-") as td:
            tmp = Path(td)
            if args.refresh_baseline:
                return _refresh(mm, assets, fixture, tmp)
            return _check(mm, assets, fixture, tmp)
    except _InfraError as e:
        role = f" ({e.detail})" if e.detail else ""
        sys.stderr.write(f"quality-gate driver: stage '{e.stage}' failed{role}; exiting 2\n")
        return EXIT_INFRA
    except Exception:
        # Backstop: any unforeseen error still exits 2 with a path-free message,
        # never a traceback that could echo an absolute path into CI logs.
        sys.stderr.write("quality-gate driver: unexpected error; exiting 2\n")
        return EXIT_INFRA


if __name__ == "__main__":
    raise SystemExit(main())
