"""Tests for ``mm mem rescan`` — privacy-only audit over stored chunks.

Plan: see ``~/.claude/plans/issue-885-harmonic-sutherland.md``. v1 calls
``enforce_write_guard`` with ``force_unsafe=False`` and
``record_outcome=False`` so the only reachable decisions are ``"pass"``
and ``"blocked"``; the test suite pins that contract and the
chunk-identity / counter-drift invariants.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import AsyncIterator

import pytest
from click.testing import CliRunner

from memtomem import privacy
from memtomem.cli import cli
from memtomem.config import Mem2MemConfig
from memtomem.storage.base import ChunkAuditRow


# An AWS access-key fixture from the privacy ``DEFAULT_PATTERNS`` set
# (``\b(?:AKIA|ASIA)[0-9A-Z]{16}\b``). Matches public-doc test pattern.
_SECRET_CONTENT = "test: aws_access_key_id=AKIAIOSFODNN7EXAMPLE"
_SAFE_CONTENT = "plain prose with no secrets"


def _row(
    chunk_id: str,
    source: str,
    content: str,
    scope: str = "project_shared",
    project_root: str | None = None,
) -> ChunkAuditRow:
    return ChunkAuditRow(
        chunk_id=chunk_id,
        source=Path(source),
        content=content,
        scope=scope,
        project_root=Path(project_root) if project_root else None,
    )


def _mock_storage(rows_by_scope: dict[str, list[ChunkAuditRow]]) -> SimpleNamespace:
    """Build a minimal Components-shaped mock whose storage exposes the
    audit enumerator.

    ``iter_chunks_for_audit`` is an async generator filtered by scope and
    optional source. The CLI never touches any other storage method on the
    rescan path, so the rest of the surface area is left undefined.
    """

    async def iter_chunks_for_audit(
        *,
        scope: str,
        source_exact: Path | None = None,
        source_prefix: Path | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[ChunkAuditRow]:
        # Replicates the SQLite backend's exact / prefix split so the CLI's
        # source-filter contract is exercised end-to-end.
        for row in rows_by_scope.get(scope, []):
            if source_exact is not None and row.source != source_exact:
                continue
            if source_prefix is not None:
                try:
                    row.source.relative_to(source_prefix)
                except ValueError:
                    continue
            yield row

    storage = SimpleNamespace(iter_chunks_for_audit=iter_chunks_for_audit)
    return SimpleNamespace(config=Mem2MemConfig(), storage=storage)


def _patched_cli_components(comp: SimpleNamespace):
    @asynccontextmanager
    async def fake():
        yield comp

    return fake


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _reset_privacy_counters() -> None:
    """Each test starts with a clean privacy outcome snapshot.

    Pins tests 9 (counter drift) against cross-test contamination.
    """
    privacy.reset_for_tests()


class TestRescanRegistration:
    """``mm mem rescan`` is registered and discoverable."""

    def test_mem_group_in_top_level_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "mem " in result.output or "mem\n" in result.output

    def test_rescan_help_describes_command(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["mem", "rescan", "--help"])
        assert result.exit_code == 0
        assert "privacy guard" in result.output
        assert "record_outcome=False" in result.output

    def test_scope_is_required(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["mem", "rescan"])
        assert result.exit_code != 0
        assert "--scope" in result.output


class TestRescanBehaviour:
    """Privacy-only audit semantics."""

    def test_clean_chunks_exit_zero(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = {
            "project_shared": [
                _row("c1", "/proj/docs/a.md", _SAFE_CONTENT),
                _row("c2", "/proj/docs/b.md", _SAFE_CONTENT),
            ]
        }
        comp = _mock_storage(rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["mem", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 0, result.output
        assert "0 violations" in result.output
        assert "2 chunks scanned" in result.output

    def test_secret_chunk_exits_one(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = {
            "project_shared": [
                _row("c1", "/proj/docs/secret.md", _SECRET_CONTENT),
            ]
        }
        comp = _mock_storage(rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["mem", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 1
        assert "decision=blocked" in result.output
        assert "scope=project_shared" in result.output
        assert "chunk_id=c1" in result.output
        assert "pattern_index=" in result.output
        assert "span=[" in result.output

    def test_secret_chunk_clean_inverse_passes(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Pin-and-invert: same fixture shape without the secret exits 0."""
        rows = {
            "project_shared": [
                _row("c1", "/proj/docs/safe.md", _SAFE_CONTENT),
            ]
        }
        comp = _mock_storage(rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["mem", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 0, result.output

    def test_json_output_schema(self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch) -> None:
        rows = {
            "project_shared": [
                _row("c1", "/proj/docs/secret.md", _SECRET_CONTENT),
            ]
        }
        comp = _mock_storage(rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["mem", "rescan", "--scope", "project_shared", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["scope"] == "project_shared"
        assert payload["scanned"] == 1
        assert len(payload["violations"]) == 1
        v = payload["violations"][0]
        assert v["chunk_id"] == "c1"
        assert v["scope"] == "project_shared"
        assert v["decision"] == "blocked"
        assert len(v["hits"]) >= 1
        h = v["hits"][0]
        assert {"pattern_index", "span_start", "span_end"} <= set(h.keys())
        assert isinstance(h["pattern_index"], int)
        assert isinstance(h["span_start"], int)
        assert isinstance(h["span_end"], int)
        assert h["span_end"] > h["span_start"]

    def test_decision_is_blocked_not_blocked_project_shared(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """v1 calls force_unsafe=False, so project_shared secret hits return
        decision='blocked', NOT 'blocked_project_shared' (which requires
        force_unsafe=True per privacy.py:493). Pins the contract.
        """
        rows = {
            "project_shared": [
                _row("c1", "/proj/x.md", _SECRET_CONTENT),
            ]
        }
        comp = _mock_storage(rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["mem", "rescan", "--scope", "project_shared", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["violations"][0]["decision"] == "blocked"
        assert payload["violations"][0]["decision"] != "blocked_project_shared"

    def test_scope_filter_is_honored(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        rows = {
            "user": [_row("u1", "/u/a.md", _SECRET_CONTENT, scope="user")],
            "project_shared": [_row("p1", "/p/a.md", _SECRET_CONTENT, scope="project_shared")],
        }
        comp = _mock_storage(rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["mem", "rescan", "--scope", "user", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["scanned"] == 1
        assert payload["violations"][0]["chunk_id"] == "u1"
        assert payload["violations"][0]["scope"] == "user"

    def test_no_counter_drift_with_record_outcome_false(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI passes record_outcome=False, so a violating rescan must
        not bump privacy._outcomes counters. End-to-end pin.
        """
        rows = {
            "project_shared": [
                _row("c1", "/p/x.md", _SECRET_CONTENT),
            ]
        }
        comp = _mock_storage(rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        before = privacy.snapshot()["outcomes"]
        result = runner.invoke(cli, ["mem", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 1
        after = privacy.snapshot()["outcomes"]
        # No outcome counter — including 'blocked' or 'pass' — may move.
        for key, expected in before.items():
            assert after[key] == expected, f"counter {key!r} drifted: {before} → {after}"


class TestSourceFilter:
    """`--source` cwd-relative resolution and matching contract."""

    def test_source_exact_file_filter(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Two violating chunks; --source picks one.
        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        a.write_text("placeholder")
        b.write_text("placeholder")
        rows = {
            "project_shared": [
                _row("a-id", str(a), _SECRET_CONTENT),
                _row("b-id", str(b), _SECRET_CONTENT),
            ]
        }
        comp = _mock_storage(rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            cli,
            ["mem", "rescan", "--scope", "project_shared", "--source", "a.md", "--json"],
        )
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["scanned"] == 1
        assert payload["violations"][0]["chunk_id"] == "a-id"

    def test_source_prefix_directory_filter(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        docs_dir = tmp_path / "docs"
        docs_dir.mkdir()
        inside = docs_dir / "x.md"
        outside = tmp_path / "y.md"
        inside.write_text("p")
        outside.write_text("p")
        rows = {
            "project_shared": [
                _row("inside", str(inside), _SECRET_CONTENT),
                _row("outside", str(outside), _SECRET_CONTENT),
            ]
        }
        comp = _mock_storage(rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            cli,
            ["mem", "rescan", "--scope", "project_shared", "--source", "docs", "--json"],
        )
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["scanned"] == 1
        assert payload["violations"][0]["chunk_id"] == "inside"

    def test_source_nonexistent_path_exits_two(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        comp = _mock_storage({})
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(
            cli,
            [
                "mem",
                "rescan",
                "--scope",
                "user",
                "--source",
                "does-not-exist.md",
            ],
        )
        assert result.exit_code == 2, result.output
