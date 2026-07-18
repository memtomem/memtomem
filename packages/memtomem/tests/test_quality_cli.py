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
