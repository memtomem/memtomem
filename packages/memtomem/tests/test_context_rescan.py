"""Tests for ``mm context rescan`` — privacy-only audit over generated context files.

v1 scans the set returned by ``detect_agent_files`` (CLAUDE.md, .cursorrules,
GEMINI.md, AGENTS.md, .github/copilot-instructions.md). Agents/skills/
commands runtime fanout is intentionally out of scope.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem import privacy
from memtomem.cli import cli


# Matches privacy.DEFAULT_PATTERNS (``\b(?:AKIA|ASIA)[0-9A-Z]{16}\b``).
_SECRET_LINE = "API_KEY=AKIAIOSFODNN7EXAMPLE\n"
_SAFE_LINE = "Just a benign instruction line.\n"


def _make_project_root(tmp_path: Path) -> Path:
    """Make ``tmp_path`` look like a project root so ``_find_project_root``
    returns it.

    ``_find_project_root`` (cli/context_cmd.py:105) walks up looking for
    ``.git`` or ``pyproject.toml``. A bare ``pyproject.toml`` is enough.
    """
    (tmp_path / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    return tmp_path


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture(autouse=True)
def _reset_privacy_counters() -> None:
    privacy.reset_for_tests()


class TestContextRescanRegistration:
    def test_rescan_help_describes_command(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["context", "rescan", "--help"])
        assert result.exit_code == 0
        assert "privacy guard" in result.output
        assert "record_outcome=False" in result.output

    def test_scope_is_required(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["context", "rescan"])
        assert result.exit_code != 0
        assert "--scope" in result.output


class TestContextRescanBehaviour:
    def test_clean_artifact_tree_exits_zero(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = _make_project_root(tmp_path)
        (root / "CLAUDE.md").write_text(_SAFE_LINE)
        monkeypatch.chdir(root)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 0, result.output
        assert "0 violations" in result.output

    def test_secret_artifact_exits_one(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = _make_project_root(tmp_path)
        (root / "CLAUDE.md").write_text(_SECRET_LINE)
        monkeypatch.chdir(root)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 1, result.output
        assert "CLAUDE.md" in result.output
        assert "decision=blocked" in result.output
        assert "pattern_index=" in result.output

    def test_secret_artifact_clean_inverse_passes(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Pin-and-invert: same fixture without the secret exits 0."""
        root = _make_project_root(tmp_path)
        (root / "CLAUDE.md").write_text(_SAFE_LINE)
        monkeypatch.chdir(root)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 0, result.output

    def test_json_output_schema(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = _make_project_root(tmp_path)
        (root / "CLAUDE.md").write_text(_SECRET_LINE)
        monkeypatch.chdir(root)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["scope"] == "project_shared"
        assert payload["scanned"] >= 1
        assert len(payload["violations"]) >= 1
        v = payload["violations"][0]
        assert v["path"].endswith("CLAUDE.md")
        assert v["scope"] == "project_shared"
        assert v["decision"] == "blocked"
        h = v["hits"][0]
        assert {"pattern_index", "span_start", "span_end"} <= set(h.keys())

    def test_skip_warn_reports_all_violations(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """``on_blocked='skip_warn'`` is required — fail_fast would only
        surface the first hit, leaving subsequent files unchecked. The
        full audit pins this.
        """
        root = _make_project_root(tmp_path)
        (root / "CLAUDE.md").write_text(_SECRET_LINE)
        (root / ".cursorrules").write_text(_SECRET_LINE)
        monkeypatch.chdir(root)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared", "--json"])
        assert result.exit_code == 1
        payload = json.loads(result.output)
        paths = {v["path"] for v in payload["violations"]}
        assert any(p.endswith("CLAUDE.md") for p in paths)
        assert any(p.endswith(".cursorrules") for p in paths)

    def test_no_counter_drift_with_record_outcome_false(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = _make_project_root(tmp_path)
        (root / "CLAUDE.md").write_text(_SECRET_LINE)
        monkeypatch.chdir(root)

        before = privacy.snapshot()["outcomes"]
        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared"])
        assert result.exit_code == 1
        after = privacy.snapshot()["outcomes"]
        for key, expected in before.items():
            assert after[key] == expected, f"counter {key!r} drifted"


# ---------------------------------------------------------------------------
# Issue #934 — scope-aware target sets
# ---------------------------------------------------------------------------


def _write_secret_artifact(root: Path, *parts: str) -> Path:
    """Scaffold a single-file artifact (e.g. flat-layout agent) with a
    secret payload inside ``root``. Returns the file path so the test can
    assert against it.
    """
    target = root.joinpath(*parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(_SECRET_LINE)
    return target


class TestContextRescanProjectMarkerGate:
    """Issue #934 / AC #2: project tiers refuse to run without a real
    project marker so the rescan cannot accidentally walk a sibling
    worktree via ``_find_project_root``'s cwd fallback.
    """

    def test_project_shared_refused_without_marker(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # tmp_path has neither .git nor pyproject.toml.
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared"])
        assert result.exit_code != 0
        assert "requires a project root" in result.output

    def test_project_local_refused_without_marker(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.chdir(tmp_path)
        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_local"])
        assert result.exit_code != 0
        assert "requires a project root" in result.output

    def test_user_scope_runs_without_marker(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """User tier is global by design — no project context required.
        Point ``$HOME`` at an empty tmp dir so the test doesn't read the
        real ``~/.memtomem``. ``canonical_artifact_dir`` resolves
        ``~/.memtomem`` via ``Path.expanduser()`` which reads ``$HOME``
        on POSIX and ``$USERPROFILE`` on Windows.
        """
        home = tmp_path / "user_home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
        monkeypatch.chdir(tmp_path)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "user"])
        assert result.exit_code == 0, result.output
        assert "0 violations" in result.output


class TestContextRescanScopeTargetSets:
    """Issue #934 / AC #1 + AC #3: ``--scope`` selects WHICH directories
    are walked. Each tier audits a disjoint canonical set so the rescan
    semantics mirror the tier's actual reach.
    """

    def test_user_scope_walks_user_canonical_only(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        home = tmp_path / "user_home"
        home.mkdir()
        monkeypatch.setenv("HOME", str(home))
        # Path.expanduser() reads $USERPROFILE on Windows; HOME alone leaks
        # to the real runner profile and the audit scans 0 files.
        monkeypatch.setenv("USERPROFILE", str(home))
        # Seed a secret under user canonical agents at $HOME/.memtomem/.
        _write_secret_artifact(home, ".memtomem", "agents", "shared", "agent.md")
        # Also seed a project root with a CLAUDE.md secret — must NOT
        # be walked under --scope=user.
        (tmp_path / "proj").mkdir()
        proj = _make_project_root(tmp_path / "proj")
        (proj / "CLAUDE.md").write_text(_SECRET_LINE)
        monkeypatch.chdir(proj)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "user", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        paths = {v["path"] for v in payload["violations"]}
        assert any("agents/shared/agent.md" in p or "agents\\shared\\agent.md" in p for p in paths)
        assert not any(p.endswith("CLAUDE.md") for p in paths)

    def test_project_shared_walks_canonical_plus_scanner_files(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        root = _make_project_root(tmp_path)
        # Canonical project_shared dir + flat-layout agent.
        _write_secret_artifact(root, ".memtomem", "agents", "shared-agent.md")
        # Project-root scanner file — STILL in scope for project_shared
        # because CLAUDE.md is the runtime fan-out of project_shared.
        (root / "CLAUDE.md").write_text(_SECRET_LINE)
        monkeypatch.chdir(root)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        paths = {v["path"] for v in payload["violations"]}
        assert any(p.endswith("CLAUDE.md") for p in paths)
        assert any(
            p.endswith(".memtomem/agents/shared-agent.md")
            or p.endswith(".memtomem\\agents\\shared-agent.md")
            for p in paths
        )

    def test_project_local_walks_local_only_no_fanout(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """ADR-0011 §3 / ADR-0016 §7: project_local has NO runtime
        fan-out. A rescan with --scope=project_local must walk the
        ``*.local/`` canonical dirs and ignore project-root scanner
        files like CLAUDE.md (which belong to project_shared's reach,
        not project_local's).
        """
        root = _make_project_root(tmp_path)
        _write_secret_artifact(root, ".memtomem", "agents.local", "draft.md")
        # Plant a secret CLAUDE.md too — must NOT be walked because
        # project_local has no fan-out into the project root.
        (root / "CLAUDE.md").write_text(_SECRET_LINE)
        monkeypatch.chdir(root)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_local", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        paths = {v["path"] for v in payload["violations"]}
        assert any(
            p.endswith(".memtomem/agents.local/draft.md")
            or p.endswith(".memtomem\\agents.local\\draft.md")
            for p in paths
        )
        assert not any(p.endswith("CLAUDE.md") for p in paths)


class TestContextRescanCrossProjectIsolation:
    """Issue #934 / AC #4: a rescan inside project A must never walk
    files owned by project B even if B's canonical tree happens to
    sit next to A's in the same parent.
    """

    def test_project_local_cross_project_isolation(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        (tmp_path / "proj_a").mkdir()
        (tmp_path / "proj_b").mkdir()
        proj_a = _make_project_root(tmp_path / "proj_a")
        proj_b = _make_project_root(tmp_path / "proj_b")
        _write_secret_artifact(proj_a, ".memtomem", "agents.local", "a.md")
        _write_secret_artifact(proj_b, ".memtomem", "agents.local", "b.md")
        monkeypatch.chdir(proj_a)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_local", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        # proj_b's secret must never appear in the violations list.
        for v in payload["violations"]:
            assert "proj_b" not in v["path"], v
            assert "b.md" != Path(v["path"]).name, v
        # And proj_a's secret must be present.
        assert any("a.md" == Path(v["path"]).name for v in payload["violations"])

    def test_symlink_escape_is_dropped(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Defence in depth: a symlink under .memtomem/agents/ pointing
        into a sibling project's tree is dropped from the audit so a
        misconfigured workspace cannot smuggle foreign files into A's
        decisions list.
        """
        (tmp_path / "proj_a").mkdir()
        (tmp_path / "proj_b").mkdir()
        proj_a = _make_project_root(tmp_path / "proj_a")
        proj_b = _make_project_root(tmp_path / "proj_b")
        _write_secret_artifact(proj_a, ".memtomem", "agents", "self.md")
        _write_secret_artifact(proj_b, ".memtomem", "agents", "stranger.md")
        # Plant the escape link inside A pointing at B's agents tree.
        (proj_a / ".memtomem" / "agents" / "link").symlink_to(proj_b / ".memtomem" / "agents")
        monkeypatch.chdir(proj_a)

        result = runner.invoke(cli, ["context", "rescan", "--scope", "project_shared", "--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        # ``stranger.md`` lives in proj_b; the symlink resolves outside
        # proj_a's root and must be skipped by the defence.
        assert all("stranger.md" != Path(v["path"]).name for v in payload["violations"])
        # ``self.md`` is the only legitimate violation.
        assert any("self.md" == Path(v["path"]).name for v in payload["violations"])
