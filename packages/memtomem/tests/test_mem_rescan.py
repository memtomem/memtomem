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
        project_root: Path | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[ChunkAuditRow]:
        # Replicates the SQLite backend's exact / prefix split so the CLI's
        # source-filter contract is exercised end-to-end. The
        # ``project_root`` filter (issue #934) is also honoured here so the
        # cross-project isolation case can be reproduced without spinning
        # up a real SQLite DB — when ``project_root`` is set, only rows
        # whose ``project_root`` matches are yielded.
        for row in rows_by_scope.get(scope, []):
            if source_exact is not None and row.source != source_exact:
                continue
            if source_prefix is not None:
                try:
                    row.source.relative_to(source_prefix)
                except ValueError:
                    continue
            if project_root is not None and row.project_root != project_root:
                continue
            yield row

    storage = SimpleNamespace(iter_chunks_for_audit=iter_chunks_for_audit)
    return SimpleNamespace(config=Mem2MemConfig(), storage=storage)


def _patched_cli_components(comp: SimpleNamespace):
    @asynccontextmanager
    async def fake():
        yield comp

    return fake


def _stub_project_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    """Make the issue-#934 project-marker gate believe we're inside a
    real project rooted at ``root``.

    The CLI gate calls ``_find_project_root()`` and then verifies
    ``.git`` / ``pyproject.toml`` exists. Writing ``pyproject.toml`` on
    disk satisfies the second half; patching the lookup satisfies the
    first regardless of cwd, so the test does not have to manage cwd.
    """
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    monkeypatch.setattr("memtomem.cli.mem_cmd._find_project_root", lambda: root)


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


class TestRescanFiles:
    @staticmethod
    def _configure(monkeypatch: pytest.MonkeyPatch, roots: list[Path]) -> None:
        cfg = Mem2MemConfig()
        cfg.indexing.memory_dirs = roots
        monkeypatch.setattr("memtomem.config.Mem2MemConfig", lambda: cfg)
        monkeypatch.setattr("memtomem.config.load_config_d", lambda *a, **k: None)
        monkeypatch.setattr("memtomem.config.load_config_overrides", lambda *a, **k: None)

    def test_historical_files_are_scanned_read_only(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._configure(monkeypatch, [tmp_path])
        safe = tmp_path / "_fetched" / "safe.md"
        blocked = tmp_path / "sessions" / "2026-07" / "blocked.md"
        safe.parent.mkdir(parents=True)
        blocked.parent.mkdir(parents=True)
        safe.write_text(_SAFE_CONTENT)
        blocked.write_text(_SECRET_CONTENT)
        before = blocked.read_bytes()

        result = runner.invoke(cli, ["mem", "rescan-files", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["scanned"] == 2
        assert payload["violations"][0]["path"] == str(blocked)
        assert blocked.read_bytes() == before
        assert _SECRET_CONTENT not in result.output

    def test_all_managed_roots_are_scanned_once_when_root_repeats(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._configure(monkeypatch, [tmp_path, tmp_path])
        files = []
        for managed in ("_imported", "_fetched", "sessions"):
            path = tmp_path / managed / f"{managed}.md"
            path.parent.mkdir(parents=True)
            path.write_text(_SAFE_CONTENT)
            files.append(path)
        before = {path: path.read_bytes() for path in files}

        result = runner.invoke(cli, ["mem", "rescan-files", "--json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == {"errors": [], "scanned": 3, "violations": []}
        assert {path: path.read_bytes() for path in files} == before

    def test_enumeration_error_is_reported_fail_closed(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._configure(monkeypatch, [tmp_path])
        base = tmp_path / "_fetched"
        base.mkdir()

        def fail_walk(root: Path):
            raise OSError(13, "denied", str(root / "unreadable"))

        monkeypatch.setattr("memtomem.cli.mem_cmd._iter_audit_files_fail_closed", fail_walk)
        result = runner.invoke(cli, ["mem", "rescan-files", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["scanned"] == 0
        assert payload["errors"] == [
            {"path": str(base / "unreadable"), "error": "enumeration_failed"}
        ]

    def test_read_error_is_reported_without_counting_file(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        self._configure(monkeypatch, [tmp_path])
        unreadable = tmp_path / "sessions" / "unreadable.md"
        unreadable.parent.mkdir()
        unreadable.write_text(_SAFE_CONTENT)
        original_read_text = Path.read_text

        def fail_selected(path: Path, *args, **kwargs):
            if path == unreadable:
                raise OSError("simulated read failure")
            return original_read_text(path, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", fail_selected)
        result = runner.invoke(cli, ["mem", "rescan-files", "--json"])

        assert result.exit_code == 1
        assert json.loads(result.output) == {
            "errors": [{"path": str(unreadable), "error": "read_failed"}],
            "scanned": 0,
            "violations": [],
        }

    @pytest.mark.requires_symlinks
    def test_managed_base_symlink_is_rejected_without_scanning_target(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root, external = tmp_path / "root", tmp_path / "external"
        root.mkdir()
        external.mkdir()
        (external / "blocked.md").write_text(_SECRET_CONTENT)
        (root / "_imported").symlink_to(external, target_is_directory=True)
        self._configure(monkeypatch, [root])

        result = runner.invoke(cli, ["mem", "rescan-files", "--json"])

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["scanned"] == 0
        assert payload["violations"] == []
        assert payload["errors"] == [{"path": str(root / "_imported"), "error": "base_symlink"}]

    @pytest.mark.requires_symlinks
    def test_nested_symlinks_are_skipped_and_never_escape_base(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        base, external = tmp_path / "_imported", tmp_path / "external"
        base.mkdir()
        external.mkdir()
        (base / "safe.md").write_text(_SAFE_CONTENT)
        (external / "blocked.md").write_text(_SECRET_CONTENT)
        (base / "linked").symlink_to(external, target_is_directory=True)
        (base / "linked-file.md").symlink_to(external / "blocked.md")
        self._configure(monkeypatch, [tmp_path])

        result = runner.invoke(cli, ["mem", "rescan-files", "--json"])

        assert result.exit_code == 0
        assert json.loads(result.output) == {"errors": [], "scanned": 1, "violations": []}


class TestRescanBehaviour:
    """Privacy-only audit semantics."""

    @pytest.fixture(autouse=True)
    def _project_marker(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Issue #934: ``--scope=project_shared`` requires a project root.
        These tests focus on the audit pipeline, not the gate, so we
        always stub a marker. The user-scope test in this class still
        works because the gate is short-circuited for ``--scope=user``.
        """
        _stub_project_root(monkeypatch, tmp_path)

    def test_clean_chunks_exit_zero(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        proj = str(tmp_path)
        rows = {
            "project_shared": [
                _row("c1", "/proj/docs/a.md", _SAFE_CONTENT, project_root=proj),
                _row("c2", "/proj/docs/b.md", _SAFE_CONTENT, project_root=proj),
            ]
        }
        comp = _mock_storage(rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["mem", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 0, result.output
        assert "0 violations" in result.output
        assert "2 chunks scanned" in result.output

    def test_secret_chunk_exits_one(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rows = {
            "project_shared": [
                _row("c1", "/proj/docs/secret.md", _SECRET_CONTENT, project_root=str(tmp_path)),
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
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Pin-and-invert: same fixture shape without the secret exits 0."""
        rows = {
            "project_shared": [
                _row("c1", "/proj/docs/safe.md", _SAFE_CONTENT, project_root=str(tmp_path)),
            ]
        }
        comp = _mock_storage(rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["mem", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 0, result.output

    def test_json_output_schema(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        rows = {
            "project_shared": [
                _row("c1", "/proj/docs/secret.md", _SECRET_CONTENT, project_root=str(tmp_path)),
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
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """v1 calls force_unsafe=False, so project_shared secret hits return
        decision='blocked', NOT 'blocked_project_shared' (which requires
        force_unsafe=True per privacy.py:493). Pins the contract.
        """
        rows = {
            "project_shared": [
                _row("c1", "/proj/x.md", _SECRET_CONTENT, project_root=str(tmp_path)),
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
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The CLI passes record_outcome=False, so a violating rescan must
        not bump privacy._outcomes counters. End-to-end pin.
        """
        rows = {
            "project_shared": [
                _row("c1", "/p/x.md", _SECRET_CONTENT, project_root=str(tmp_path)),
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

    @pytest.fixture(autouse=True)
    def _project_marker(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        _stub_project_root(monkeypatch, tmp_path)

    def test_source_exact_file_filter(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Two violating chunks; --source picks one.
        a = tmp_path / "a.md"
        b = tmp_path / "b.md"
        a.write_text("placeholder")
        b.write_text("placeholder")
        proj = str(tmp_path)
        rows = {
            "project_shared": [
                _row("a-id", str(a), _SECRET_CONTENT, project_root=proj),
                _row("b-id", str(b), _SECRET_CONTENT, project_root=proj),
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
        proj = str(tmp_path)
        rows = {
            "project_shared": [
                _row("inside", str(inside), _SECRET_CONTENT, project_root=proj),
                _row("outside", str(outside), _SECRET_CONTENT, project_root=proj),
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


class TestProjectMarkerGate:
    """Issue #934 ADR-0011 / ADR-0016 cross-project isolation gate.

    The CLI refuses ``--scope=project_shared`` / ``--scope=project_local``
    without a real project marker (``.git`` or ``pyproject.toml``) so a
    shared SQLite DB can't accidentally leak chunks across worktrees.
    ``--scope=user`` is unaffected because user-tier rows are global by
    design.
    """

    def test_project_shared_refused_without_project_marker(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # No pyproject.toml, no .git → marker check fails.
        monkeypatch.setattr("memtomem.cli.mem_cmd._find_project_root", lambda: tmp_path)

        result = runner.invoke(cli, ["mem", "rescan", "--scope", "project_shared"])
        assert result.exit_code != 0
        assert "requires a project root" in result.output
        # Make sure storage was never touched — the gate runs before
        # ``cli_components`` is opened.

    def test_project_local_refused_without_project_marker(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        monkeypatch.setattr("memtomem.cli.mem_cmd._find_project_root", lambda: tmp_path)

        result = runner.invoke(cli, ["mem", "rescan", "--scope", "project_local"])
        assert result.exit_code != 0
        assert "requires a project root" in result.output

    def test_user_scope_does_not_require_project_marker(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        # No marker, no patch. User tier short-circuits the gate.
        monkeypatch.setattr("memtomem.cli.mem_cmd._find_project_root", lambda: tmp_path)
        comp = _mock_storage({"user": []})
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["mem", "rescan", "--scope", "user"])
        assert result.exit_code == 0, result.output

    def test_project_shared_isolated_from_other_project(
        self,
        runner: CliRunner,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
    ) -> None:
        """Two ``project_shared`` chunks in one mock DB — one tagged to
        the current project, one to a sibling. The CLI passes the
        current project_root into the audit; the mock honours it; the
        sibling row never appears in the output.
        """
        proj_a = tmp_path / "proj_a"
        proj_b = tmp_path / "proj_b"
        proj_a.mkdir()
        proj_b.mkdir()
        _stub_project_root(monkeypatch, proj_a)

        rows = {
            "project_shared": [
                _row(
                    "a-secret",
                    str(proj_a / "x.md"),
                    _SECRET_CONTENT,
                    project_root=str(proj_a),
                ),
                _row(
                    "b-secret",
                    str(proj_b / "x.md"),
                    _SECRET_CONTENT,
                    project_root=str(proj_b),
                ),
            ]
        }
        comp = _mock_storage(rows)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["mem", "rescan", "--scope", "project_shared", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["scanned"] == 1
        assert payload["violations"][0]["chunk_id"] == "a-secret"
        assert all("b-secret" not in str(v) for v in payload["violations"])
