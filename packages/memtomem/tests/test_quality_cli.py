"""``mm quality`` CLI tests (#1802, Quality Lab PR-4).

In-process ``CliRunner`` coverage: arg parsing, output shape (``--format`` /
``{"ok": ...}`` acks), and the compare exit-code gate. Storage-backed commands
use a mocked Components double (real storage integration lives in
``test_quality_replay.py`` and the subprocess e2e); ``compare`` is pure and runs
on real report files with no components.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

from click.testing import CliRunner

from memtomem.cli.quality_cmd import quality
from memtomem.quality import metrics
from memtomem.quality.fingerprints import case_set_fingerprint
from memtomem.quality.replay import (
    REPLAY_REPORT_KIND,
    REPLAY_REPORT_SCHEMA_VERSION,
    report_case_to_fingerprint_input,
)

_STAGE_OUTCOME_KEYS = (
    "bm25_error",
    "dense_error",
    "dense_suppressed_mismatch",
    "expansion_failed",
    "rerank_fallback",
    "rescue_failed",
)


def _patch_components(monkeypatch, comp) -> None:
    @asynccontextmanager
    async def fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)


def _one_case_report(case_id: str, retrieved: list[str], relevant: list[str], *, profile: str):
    rel = set(relevant)
    case = {
        "case_id": case_id,
        "name": None,
        "version": 1,
        "status": "active",
        "query_text": "q",
        "top_k": 5,
        "filters": {"namespace": None, "scope": None},
        "stale": {"profile": None, "corpus": None, "index": None},
        "flags": [],
        "labels": {"relevant": sorted(rel), "not_relevant": []},
        "retrieved": [
            {"content_hash": h, "score": 1.0 - i * 0.01, "rank": i, "source": "bm25"}
            for i, h in enumerate(retrieved, start=1)
        ],
        "metrics": {
            "hit_rate": metrics.hit_rate_at_k(retrieved, rel, 5),
            "reciprocal_rank": metrics.reciprocal_rank_at_k(retrieved, rel, 5),
            "recall_labeled": metrics.recall_labeled_at_k(retrieved, rel, 5),
            "ndcg": metrics.ndcg_at_k(retrieved, {h: 1.0 for h in rel}, 5),
            "precision": metrics.precision_at_k(retrieved, rel, set(), 5),
        },
        "stage_outcomes": {k: False for k in _STAGE_OUTCOME_KEYS},
        "included_in_aggregate": True,
    }
    case_set = case_set_fingerprint([report_case_to_fingerprint_input(case)])
    return {
        "schema_version": REPLAY_REPORT_SCHEMA_VERSION,
        "kind": REPLAY_REPORT_KIND,
        "as_of_unix": 1000,
        "deterministic": True,
        "nondeterministic_stages": [],
        "fingerprints": {
            "profile": profile,
            "corpus": "corp",
            "index": "idx",
            "case_set": case_set,
        },
        "profile_knobs": {"decay": {"enabled": False}},
        "counts": {},
        "aggregate": {},
        "cases": [case],
    }


class TestWriteAcks:
    def test_import_ack(self, monkeypatch, tmp_path):
        comp = SimpleNamespace(
            storage=SimpleNamespace(import_eval_cases=AsyncMock(return_value={"imported": 3}))
        )
        _patch_components(monkeypatch, comp)
        env = tmp_path / "cases.json"
        env.write_text(json.dumps({"schema_version": 1, "kind": "eval_case_set", "cases": []}))

        result = CliRunner().invoke(quality, ["import", str(env)])
        assert result.exit_code == 0
        assert json.loads(result.output) == {"ok": True, "imported": 3}

    def test_status_ack(self, monkeypatch):
        comp = SimpleNamespace(
            storage=SimpleNamespace(
                set_eval_case_status=AsyncMock(
                    return_value={"case_id": "abc", "status": "archived"}
                )
            )
        )
        _patch_components(monkeypatch, comp)
        result = CliRunner().invoke(quality, ["status", "abc", "archived"])
        assert result.exit_code == 0
        assert json.loads(result.output) == {"ok": True, "case_id": "abc", "status": "archived"}

    def test_promote_ack(self, monkeypatch):
        monkeypatch.setattr(
            "memtomem.quality.state.current_fingerprints",
            lambda storage, config: ({"profile": "p", "corpus": "c", "index": "i"}, {}),
        )
        comp = SimpleNamespace(
            config=SimpleNamespace(),
            storage=SimpleNamespace(
                promote_search_run=AsyncMock(
                    return_value={
                        "case_id": "case-1",
                        "name": "q1",
                        "labels": [{"content_hash": "h"}],
                    }
                )
            ),
        )
        _patch_components(monkeypatch, comp)
        result = CliRunner().invoke(quality, ["promote", "run-1", "--name", "q1"])
        assert result.exit_code == 0
        ack = json.loads(result.output)
        assert ack == {"ok": True, "case_id": "case-1", "name": "q1", "label_count": 1}


class TestCasesListing:
    def test_json_envelope_is_self_describing(self, monkeypatch):
        rows = [
            {"case_id": "a", "name": None, "query_text": "q", "status": "active", "label_count": 2}
        ]
        comp = SimpleNamespace(
            storage=SimpleNamespace(list_eval_cases=AsyncMock(return_value=rows))
        )
        _patch_components(monkeypatch, comp)
        result = CliRunner().invoke(quality, ["cases", "--format", "json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload == {"cases": rows, "count": 1}

    def test_export_name_resolution(self, monkeypatch):
        get = AsyncMock(return_value={"case_id": "resolved-id"})
        export = AsyncMock(
            return_value={
                "schema_version": 1,
                "kind": "eval_case_set",
                "cases": [{"case_id": "resolved-id"}],
            }
        )
        comp = SimpleNamespace(storage=SimpleNamespace(get_eval_case=get, export_eval_cases=export))
        _patch_components(monkeypatch, comp)
        result = CliRunner().invoke(quality, ["export", "--case", "some-name"])
        assert result.exit_code == 0
        get.assert_awaited_once_with("some-name")
        export.assert_awaited_once_with(case_ids=["resolved-id"])


class TestReplayCommand:
    def test_out_writes_canonical_bytes_and_warns_on_nondeterministic(self, monkeypatch, tmp_path):
        report = _one_case_report("c1", ["r"], ["r"], profile="p")
        report["deterministic"] = False
        report["nondeterministic_stages"] = ["query_expansion_llm"]

        async def fake_replay(storage, pipeline, config, *, case_ids=None, as_of_unix=None):
            return report

        monkeypatch.setattr("memtomem.quality.replay.replay_cases", fake_replay)
        comp = SimpleNamespace(storage=None, search_pipeline=None, config=None)
        _patch_components(monkeypatch, comp)

        out = tmp_path / "base.json"
        result = CliRunner().invoke(quality, ["replay", "--out", str(out), "--format", "json"])
        assert result.exit_code == 0
        # --out holds the canonical serialization.
        from memtomem.quality.replay import serialize_report

        assert out.read_text() == serialize_report(report)
        # Nondeterministic warning goes to stderr (mix_stderr default merges it).
        assert "nondeterministic" in result.output


class TestCompareGate:
    def _write(self, tmp_path, name, report):
        p = tmp_path / name
        p.write_text(json.dumps(report))
        return str(p)

    def test_advisory_default_exits_zero_even_on_regression(self, tmp_path):
        base = self._one(tmp_path, "base.json", ["r", "x"], profile="p1")  # RR 1.0
        cand = self._regressed(tmp_path, "cand.json", profile="p2")  # RR 0.5
        result = CliRunner().invoke(quality, ["compare", base, cand])
        assert result.exit_code == 0
        assert "regressed=1" in result.output

    def test_fail_on_regression_exits_one(self, tmp_path):
        base = self._one(tmp_path, "base.json", ["r", "x"], profile="p1")
        cand = self._regressed(tmp_path, "cand.json", profile="p2")
        result = CliRunner().invoke(quality, ["compare", base, cand, "--fail-on-regression"])
        assert result.exit_code == 1

    def test_fail_on_case_set_mismatch(self, tmp_path):
        base = self._one(tmp_path, "base.json", ["r"], profile="p1")
        # candidate has a different case → case_set differs, one-sided cases.
        cand_report = _one_case_report("c2", ["r"], ["r"], profile="p2")
        cand = self._write(tmp_path, "cand.json", cand_report)
        result = CliRunner().invoke(quality, ["compare", base, cand, "--fail-on-regression"])
        assert result.exit_code == 1

    def _one(self, tmp_path, name, retrieved, *, profile):
        return self._write(
            tmp_path, name, _one_case_report("c1", retrieved, ["r"], profile=profile)
        )

    def _regressed(self, tmp_path, name, *, profile):
        # r at rank 2 → RR 0.5, a regression from rank 1.
        return self._write(
            tmp_path, name, _one_case_report("c1", ["x", "r"], ["r"], profile=profile)
        )


class TestGateCommand:
    def _write(self, tmp_path, name, doc):
        p = tmp_path / name
        p.write_text(json.dumps(doc))
        return str(p)

    def _reports(self, tmp_path, *, regressed: bool):
        base = self._write(tmp_path, "base.json", _one_case_report("c1", ["r"], ["r"], profile="p"))
        cand_retrieved = ["x", "r"] if regressed else ["r"]
        cand = self._write(
            tmp_path, "cand.json", _one_case_report("c1", cand_retrieved, ["r"], profile="p")
        )
        return base, cand

    def _policy(self, tmp_path, **kw):
        return self._write(
            tmp_path, "policy.json", {"schema_version": 1, "kind": "replay_gate_policy", **kw}
        )

    def test_pass_exits_zero(self, tmp_path):
        base, cand = self._reports(tmp_path, regressed=False)
        policy = self._policy(tmp_path, max_verdict_counts={"regressed": 0})
        result = CliRunner().invoke(
            quality, ["gate", base, cand, "--policy", policy, "--format", "json"]
        )
        assert result.exit_code == 0
        verdict = json.loads(result.output)
        assert verdict["pass"] is True
        assert verdict["kind"] == "replay_gate_verdict"

    def test_violation_exits_one_with_verdict_emitted(self, tmp_path):
        base, cand = self._reports(tmp_path, regressed=True)
        policy = self._policy(tmp_path, max_verdict_counts={"regressed": 0})
        result = CliRunner().invoke(
            quality, ["gate", base, cand, "--policy", policy, "--format", "json"]
        )
        assert result.exit_code == 1
        # exit 1, but the verdict is still emitted before exiting.
        verdict = json.loads(result.output)
        assert verdict["pass"] is False
        assert verdict["violations"][0]["rule"] == "verdict_count"

    def test_malformed_policy_exits_two(self, tmp_path):
        base, cand = self._reports(tmp_path, regressed=False)
        policy = self._policy(tmp_path, max_verdict_counts={"bogus": 0})
        result = CliRunner().invoke(quality, ["gate", base, cand, "--policy", policy])
        assert result.exit_code == 2

    def test_malformed_report_exits_two(self, tmp_path):
        base, _ = self._reports(tmp_path, regressed=False)
        bad = tmp_path / "bad.json"
        bad.write_text("{ not json")
        policy = self._policy(tmp_path)
        result = CliRunner().invoke(quality, ["gate", base, str(bad), "--policy", policy])
        assert result.exit_code == 2
        # Error references the input by role, never by filesystem path.
        assert str(bad) not in result.output
        assert "candidate" in result.output

    def test_missing_input_exits_two_without_path(self, tmp_path):
        base, cand = self._reports(tmp_path, regressed=False)
        missing = str(tmp_path / "nope" / "ghost-policy.json")
        result = CliRunner().invoke(quality, ["gate", base, cand, "--policy", missing])
        assert result.exit_code == 2
        # Click's exists=True would have echoed the literal path — it must not.
        assert missing not in result.output
        assert "ghost-policy.json" not in result.output
        assert "policy" in result.output

    def test_directory_input_exits_two_without_path(self, tmp_path):
        base, cand = self._reports(tmp_path, regressed=False)
        result = CliRunner().invoke(quality, ["gate", str(tmp_path), cand, "--policy", base])
        assert result.exit_code == 2
        assert str(tmp_path) not in result.output
        assert "baseline" in result.output

    def test_invalid_utf8_exits_two(self, tmp_path):
        base, cand = self._reports(tmp_path, regressed=False)
        bad = tmp_path / "bad-utf8.json"
        bad.write_bytes(b"\xff\xfe not utf-8")
        result = CliRunner().invoke(quality, ["gate", base, cand, "--policy", str(bad)])
        assert result.exit_code == 2
        assert str(bad) not in result.output

    def test_out_and_comparison_out_write_canonical_bytes(self, tmp_path):
        base, cand = self._reports(tmp_path, regressed=False)
        policy = self._policy(tmp_path, min_compared_cases=1)
        out = tmp_path / "verdict.json"
        cmp_out = tmp_path / "comparison.json"
        result = CliRunner().invoke(
            quality,
            [
                "gate",
                base,
                cand,
                "--policy",
                policy,
                "--out",
                str(out),
                "--comparison-out",
                str(cmp_out),
            ],
        )
        assert result.exit_code == 0
        assert json.loads(out.read_text())["kind"] == "replay_gate_verdict"
        assert out.read_text().endswith("\n")
        assert json.loads(cmp_out.read_text())["kind"] == "replay_comparison"

    def test_table_format_renders_violations(self, tmp_path):
        base, cand = self._reports(tmp_path, regressed=True)
        policy = self._policy(tmp_path, max_verdict_counts={"regressed": 0})
        result = CliRunner().invoke(quality, ["gate", base, cand, "--policy", policy])
        assert result.exit_code == 1
        assert "gate: FAIL" in result.output
        assert "verdict_count" in result.output


class TestExperiment:
    """`mm quality experiment` — exit-code contract and fail-fast ordering.

    `run_experiment` is mocked (real storage orchestration lives in the
    subprocess e2e); these pin the CLI's input validation, exit codes, and that
    a rejected input never opens storage or writes `--out`.
    """

    def _profile(self, tmp_path, name, knobs=None):
        p = tmp_path / f"{name}.json"
        p.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "retrieval_profile",
                    "name": name,
                    "knobs": knobs or {"search": {"rrf_k": 40}},
                }
            )
        )
        return str(p)

    def _canned(
        self,
        *,
        policy_supplied=False,
        gate_pass=None,
        nondeterministic=False,
        baseline_warnings=(),
        candidate_warnings=(),
    ):
        cand = {
            "profile_name": "cand-a",
            "profile_fingerprint": "candidate-profile-fingerprint",
            "deterministic": not nondeterministic,
            "nondeterministic_stages": ["rerank_remote"] if nondeterministic else [],
            "gate": None if gate_pass is None else {"pass": gate_pass},
            "warnings": list(candidate_warnings),
            "comparison": {
                "aggregate_deltas": {
                    "hit_rate": {"delta": 0.0},
                    "reciprocal_rank": {"delta": 0.0},
                    "recall_labeled": {"delta": 0.0},
                    "ndcg": {"delta": 0.0},
                    "precision": {"delta": 0.0, "cohort_size": 1},
                    "cohort_size": 1,
                },
                "summary": {
                    "improved": 0,
                    "regressed": 0,
                    "mixed": 0,
                    "unchanged": 1,
                    "candidate_degraded": 0,
                    "excluded": 0,
                },
                "cases": [],
                "compatibility": {"notes": []},
            },
        }
        return {
            "schema_version": 1,
            "kind": "quality_experiment",
            "as_of_unix": 1000,
            "deterministic": not nondeterministic,
            "policy_supplied": policy_supplied,
            "case_count": 1,
            "fingerprints": {"corpus": "c", "index": "i", "case_set": "cs"},
            "baseline": {
                "profile_name": "ambient",
                "profile_fingerprint": "baseline-profile-fingerprint",
                "deterministic": True,
                "nondeterministic_stages": [],
                "warnings": list(baseline_warnings),
                "aggregate": {
                    "mean_hit_rate": 1.0,
                    "mrr": 1.0,
                    "mean_recall_labeled": 1.0,
                    "mean_ndcg": 1.0,
                    "evaluated_cases": 1,
                },
            },
            "candidates": [cand],
        }

    def _patch_run(self, monkeypatch, comp, result):
        _patch_components(monkeypatch, comp)
        run = AsyncMock(return_value=result)
        monkeypatch.setattr("memtomem.quality.experiment.run_experiment", run)
        return run

    def test_happy_path_json_out_matches_stdout(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        self._patch_run(monkeypatch, comp, self._canned())
        out = tmp_path / "exp.json"
        result = CliRunner().invoke(
            quality,
            [
                "experiment",
                "--profile",
                self._profile(tmp_path, "cand-a"),
                "--as-of",  # pin so no reproducibility warning pollutes stdout
                "1784500000",
                "--format",
                "json",
                "--out",
                str(out),
            ],
        )
        assert result.exit_code == 0
        assert json.loads(result.output)["kind"] == "quality_experiment"
        assert out.read_text() == result.output

    def test_invalid_profile_exits_2_before_storage(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        run = self._patch_run(monkeypatch, comp, self._canned())
        bad = tmp_path / "bad.json"
        bad.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "retrieval_profile",
                    "name": "x",
                    "knobs": {"search": {"rrf_k": -5}},
                }
            )
        )
        out = tmp_path / "exp.json"
        result = CliRunner().invoke(
            quality, ["experiment", "--profile", str(bad), "--out", str(out)]
        )
        assert result.exit_code == 2
        assert run.call_count == 0  # fail-fast: never reached the orchestrator
        assert not out.exists()  # nothing written on exit 2

    def test_unreadable_profile_exits_2_without_path(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        self._patch_run(monkeypatch, comp, self._canned())
        result = CliRunner().invoke(
            quality, ["experiment", "--profile", str(tmp_path / "missing.json")]
        )
        assert result.exit_code == 2
        assert "missing.json" not in result.output  # role, never path

    def test_run_error_maps_to_exit_2(self, monkeypatch, tmp_path):
        from memtomem.errors import EvalCaseError

        comp = SimpleNamespace()
        _patch_components(monkeypatch, comp)
        run = AsyncMock(side_effect=EvalCaseError("no evaluation cases selected"))
        monkeypatch.setattr("memtomem.quality.experiment.run_experiment", run)
        result = CliRunner().invoke(
            quality, ["experiment", "--profile", self._profile(tmp_path, "cand-a")]
        )
        assert result.exit_code == 2

    def test_policy_failure_exits_1_with_result_emitted(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        self._patch_run(monkeypatch, comp, self._canned(policy_supplied=True, gate_pass=False))
        policy = tmp_path / "policy.json"
        policy.write_text(json.dumps({"schema_version": 1, "kind": "replay_gate_policy"}))
        result = CliRunner().invoke(
            quality,
            [
                "experiment",
                "--profile",
                self._profile(tmp_path, "cand-a"),
                "--policy",
                str(policy),
                "--as-of",  # pin so no reproducibility warning pollutes stdout
                "1784500000",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 1
        assert json.loads(result.output)["kind"] == "quality_experiment"  # emitted first

    def test_policy_pass_exits_0(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        self._patch_run(monkeypatch, comp, self._canned(policy_supplied=True, gate_pass=True))
        policy = tmp_path / "policy.json"
        policy.write_text(json.dumps({"schema_version": 1, "kind": "replay_gate_policy"}))
        result = CliRunner().invoke(
            quality,
            [
                "experiment",
                "--profile",
                self._profile(tmp_path, "cand-a"),
                "--policy",
                str(policy),
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0

    def test_nondeterministic_profile_warns_on_stderr(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        self._patch_run(monkeypatch, comp, self._canned(nondeterministic=True))
        # Default CliRunner merges stderr into output (mix_stderr default).
        result = CliRunner().invoke(
            quality,
            ["experiment", "--profile", self._profile(tmp_path, "cand-a"), "--format", "json"],
        )
        assert result.exit_code == 0
        assert "nondeterministic" in result.output
        assert "rerank_remote" in result.output

    def test_table_surfaces_baseline_and_candidate_profile_warnings(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        self._patch_run(
            monkeypatch,
            comp,
            self._canned(
                baseline_warnings=("baseline_warning",),
                candidate_warnings=("rerank_provider_model_mismatch",),
            ),
        )
        result = CliRunner().invoke(
            quality, ["experiment", "--profile", self._profile(tmp_path, "cand-a")]
        )

        assert result.exit_code == 0
        baseline_warning = result.output.index("warning: baseline_warning")
        candidate_header = result.output.index("cand-a cand-a")
        candidate_warning = result.output.index("warning: rerank_provider_model_mismatch")
        assert baseline_warning < candidate_header < candidate_warning

    def test_missing_as_of_warns_not_reproducible(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        self._patch_run(monkeypatch, comp, self._canned())
        result = CliRunner().invoke(
            quality,
            ["experiment", "--profile", self._profile(tmp_path, "cand-a"), "--format", "json"],
        )
        assert result.exit_code == 0
        assert "not byte-reproducible" in result.output

    def test_explicit_as_of_does_not_warn(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        self._patch_run(monkeypatch, comp, self._canned())
        result = CliRunner().invoke(
            quality,
            [
                "experiment",
                "--profile",
                self._profile(tmp_path, "cand-a"),
                "--as-of",
                "1784500000",
                "--format",
                "json",
            ],
        )
        assert result.exit_code == 0
        assert "not byte-reproducible" not in result.output

    def test_unwritable_out_exits_2_without_path(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        self._patch_run(monkeypatch, comp, self._canned())
        # Parent directory does not exist → _write_file raises FileNotFoundError.
        bad_out = tmp_path / "nope" / "exp.json"
        result = CliRunner().invoke(
            quality,
            ["experiment", "--profile", self._profile(tmp_path, "cand-a"), "--out", str(bad_out)],
        )
        assert result.exit_code == 2
        assert str(bad_out) not in result.output  # path never echoed
        assert "not writable" in result.output

    def test_storage_error_maps_to_exit_2(self, monkeypatch, tmp_path):
        from memtomem.errors import StorageError

        comp = SimpleNamespace()
        _patch_components(monkeypatch, comp)
        run = AsyncMock(side_effect=StorageError("db is locked"))
        monkeypatch.setattr("memtomem.quality.experiment.run_experiment", run)
        result = CliRunner().invoke(
            quality, ["experiment", "--profile", self._profile(tmp_path, "cand-a")]
        )
        assert result.exit_code == 2
        assert "db is locked" not in result.output  # message is type-only

    def test_invalid_baseline_names_its_role(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        self._patch_run(monkeypatch, comp, self._canned())
        bad = tmp_path / "bad-baseline.json"
        bad.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "retrieval_profile",
                    "name": "b",
                    "knobs": {"search": {"rrf_k": -1}},
                }
            )
        )
        result = CliRunner().invoke(
            quality,
            ["experiment", "--baseline", str(bad), "--profile", self._profile(tmp_path, "cand-a")],
        )
        assert result.exit_code == 2
        assert "baseline profile rejected" in result.output

    def test_explicit_falsy_baseline_is_validated_not_ignored(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        run = self._patch_run(monkeypatch, comp, self._canned())
        empty = tmp_path / "empty.json"
        empty.write_text("{}")  # a supplied but empty doc must be rejected, not skipped
        result = CliRunner().invoke(
            quality,
            [
                "experiment",
                "--baseline",
                str(empty),
                "--profile",
                self._profile(tmp_path, "cand-a"),
            ],
        )
        assert result.exit_code == 2
        assert run.call_count == 0  # never ran against ambient by mistake
        assert "baseline profile rejected" in result.output

    def test_config_error_maps_to_exit_2_without_path(self, monkeypatch, tmp_path):
        from memtomem.errors import ConfigError

        comp = SimpleNamespace()
        _patch_components(monkeypatch, comp)
        secret_path = "/private/secret/config.json"
        run = AsyncMock(side_effect=ConfigError(f"bad config at {secret_path}"))
        monkeypatch.setattr("memtomem.quality.experiment.run_experiment", run)
        result = CliRunner().invoke(
            quality, ["experiment", "--profile", self._profile(tmp_path, "cand-a")]
        )
        assert result.exit_code == 2
        assert secret_path not in result.output  # path never echoed

    def test_sqlite_error_maps_to_exit_2(self, monkeypatch, tmp_path):
        import sqlite3

        comp = SimpleNamespace()
        _patch_components(monkeypatch, comp)
        run = AsyncMock(side_effect=sqlite3.OperationalError("no such table: chunks"))
        monkeypatch.setattr("memtomem.quality.experiment.run_experiment", run)
        result = CliRunner().invoke(
            quality, ["experiment", "--profile", self._profile(tmp_path, "cand-a")]
        )
        assert result.exit_code == 2

    def test_invalid_second_profile_names_its_index(self, monkeypatch, tmp_path):
        comp = SimpleNamespace()
        self._patch_run(monkeypatch, comp, self._canned())
        bad = tmp_path / "bad2.json"
        bad.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "kind": "retrieval_profile",
                    "name": "b2",
                    "knobs": {"mmr": {"lambda_param": 5.0}},
                }
            )
        )
        result = CliRunner().invoke(
            quality,
            [
                "experiment",
                "--profile",
                self._profile(tmp_path, "cand-a"),
                "--profile",
                str(bad),
            ],
        )
        assert result.exit_code == 2
        assert "profile #2 rejected" in result.output
