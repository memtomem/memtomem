"""CLI tests for ``mm context status --all-projects`` (A-10 #1280).

Single-project ``mm context status`` behavior is pinned by
``test_context_status.py`` / ``test_cli_status.py``; the shared
``collect_project_status`` derivation has its own unit pins there too.
This file covers what the batch verb adds:

- grouped per-project render: drift-only detail rows, runtime drift line,
  per-project summary classification, batch summary counts;
- skip rows (paused / missing / stale) via the shared ``sync_skip_reason``
  derivation, and the zero-eligible informational exit 0;
- the project_shared tier gate (usage error for other ``--scope`` values);
- the ``_find_project_root()`` discovery anchor from a subdirectory
  (the A-9 Codex fold, mirrored);
- exit semantics: drift alone exits 0 (cron chain ``status && sync``),
  corrupt lockfile exits 1, and a crashed collection exits 1 with the
  sibling still reported (per-project isolation — Codex design-gate fold);
- wiki-absent degradation (the #1280 acceptance criterion).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context as context_group
from memtomem.config import ContextGatewayConfig
from memtomem.context.status import collect_project_status
from memtomem.wiki.store import WikiStore

from .helpers import set_home

_AGENT_BODY = """---
name: reviewer
description: Code review agent
tools: [Read, Grep]
---
You are a code review agent.
"""


# ── Fixtures (mirror test_cli_context_sync_all_projects.py) ─────────────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox HOME + the wiki env override so runs are machine-hermetic."""
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.delenv("MEMTOMEM_WIKI_PATH", raising=False)
    return home


@pytest.fixture
def known_projects_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    kp = tmp_path / "kp.json"
    monkeypatch.setattr(
        "memtomem.cli.context_cmd.ContextGatewayConfig",
        lambda: ContextGatewayConfig(
            known_projects_path=kp,
            experimental_claude_projects_scan=False,
        ),
    )
    return kp


def _seed_known_projects(path: Path, entries: list[tuple[Path, bool]]) -> None:
    doc = {
        "version": 1,
        "projects": [
            {"root": str(p), "added_at": "2026-01-01T00:00:00Z", "label": None, "enabled": en}
            for p, en in entries
        ],
    }
    path.write_text(json.dumps(doc), encoding="utf-8")


def _project(
    tmp_path: Path,
    name: str,
    *,
    store: bool = True,
    agent: bool = False,
) -> Path:
    root = tmp_path / name
    root.mkdir()
    (root / ".claude").mkdir()
    if store:
        (root / ".memtomem").mkdir()
    if agent:
        agents = root / ".memtomem" / "agents"
        agents.mkdir(parents=True)
        (agents / "reviewer.md").write_text(_AGENT_BODY, encoding="utf-8")
    return root


# ── Grouped render + summary ─────────────────────────────────────────────


def test_aggregate_groups_drift_and_clean(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One drifted + one clean project: grouped blocks, runtime drift line
    from the shared enumeration, batch summary counts — and exit 0 (drift
    alone must keep the cron chain ``status && sync`` viable)."""
    a = _project(tmp_path, "proj-a", agent=True)  # canonical never synced → drift
    b = _project(tmp_path, "proj-b")  # empty store → clean
    _seed_known_projects(known_projects_path, [(a, True), (b, True)])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(context_group, ["status", "--all-projects"])

    assert result.exit_code == 0, result.output
    assert "2 project scope(s) discovered:" in result.output
    assert f"{a}" in result.output and f"{b}" in result.output
    # The drifted project's runtime line uses the engine status vocabulary
    # (snake keys un-snaked for display) and names the kind.
    assert "runtime drift:" in result.output
    assert "missing target" in result.output
    # The clean project renders the in-sync line.
    assert "runtime: in sync" in result.output
    assert "Summary: 1 with drift, 1 clean, 0 error(s), 0 skipped." in result.output


def test_untracked_counts_in_header_not_rows(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Untracked canonicals are header inventory (+ N untracked), never
    detail rows — detail rows are reserved for DRIFT_STATES."""
    a = _project(tmp_path, "proj-a", agent=True)
    _seed_known_projects(known_projects_path, [(a, True)])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(context_group, ["status", "--all-projects"])

    assert result.exit_code == 0, result.output
    assert "(+ 1 untracked)" in result.output
    assert "agents/reviewer" not in result.output  # no detail row for untracked


def test_scope_gate_usage_error(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _project(tmp_path, "proj-a")
    _seed_known_projects(known_projects_path, [(a, True)])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(context_group, ["status", "--all-projects", "--scope", "user"])

    assert result.exit_code != 0
    assert "project_shared tier only" in result.output


def test_skip_rows_paused_missing_stale(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ineligible scopes are reported skip rows — the shared
    ``sync_skip_reason`` codes with the CLI remediation prose — and never
    classified (no crash on a missing root: the #1280 acceptance
    criterion)."""
    a = _project(tmp_path, "proj-a")
    paused = _project(tmp_path, "paused")
    gone = _project(tmp_path, "gone")
    stale = _project(tmp_path, "stale", store=False)
    _seed_known_projects(
        known_projects_path,
        [(a, True), (paused, False), (gone, True), (stale, True)],
    )
    shutil.rmtree(gone)
    monkeypatch.chdir(a)

    result = CliRunner().invoke(context_group, ["status", "--all-projects"])

    assert result.exit_code == 0, result.output
    assert "paused — `mm context projects resume" in result.output
    assert "missing — root no longer exists" in result.output
    assert "stale — no .memtomem/ store" in result.output
    assert "Summary: 0 with drift, 1 clean, 0 error(s), 3 skipped." in result.output


def test_subdirectory_run_anchors_at_project_root(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery anchors at ``_find_project_root()`` — from a subdir the
    PARENT project is the cwd scope (raw ``Path.cwd()`` would make the
    subdir a stale scope and report the project as skipped)."""
    a = _project(tmp_path, "proj-a")
    (a / "pyproject.toml").write_text('[project]\nname = "a"\nversion = "0"\n', encoding="utf-8")
    subdir = a / "src" / "pkg"
    subdir.mkdir(parents=True)
    _seed_known_projects(known_projects_path, [])
    monkeypatch.chdir(subdir)

    result = CliRunner().invoke(context_group, ["status", "--all-projects"])

    assert result.exit_code == 0, result.output
    assert f"{a}" in result.output
    assert "stale — no .memtomem/ store" not in result.output
    assert "Summary: 0 with drift, 1 clean, 0 error(s), 0 skipped." in result.output


# ── Error isolation + exit semantics ─────────────────────────────────────


def test_corrupt_lockfile_exits_one_sibling_reported(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = _project(tmp_path, "proj-a")
    (a / ".memtomem" / "lock.json").write_text("{not json", encoding="utf-8")
    b = _project(tmp_path, "proj-b")
    _seed_known_projects(known_projects_path, [(a, True), (b, True)])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(context_group, ["status", "--all-projects"])

    assert result.exit_code == 1, result.output
    assert "lock.json" in result.output
    assert f"{b}" in result.output  # sibling still rendered
    assert "Summary: 0 with drift, 1 clean, 1 error(s), 0 skipped." in result.output


def test_collector_crash_isolated_and_exits_one(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex design-gate fold: a real collection crash (not a lockfile
    error) prints a red row, the sibling still renders, exit 1. The
    monkeypatched collector pins the pass-through kwargs — a wrapper that
    swallowed ``wiki``/``target_scope`` would false-pass the batch tier
    pin."""
    a = _project(tmp_path, "proj-a")
    b = _project(tmp_path, "proj-b")
    _seed_known_projects(known_projects_path, [(a, True), (b, True)])
    monkeypatch.chdir(a)

    seen_kwargs: list[dict] = []

    def _exploding(root, **kwargs):
        seen_kwargs.append(kwargs)
        if root == a:
            raise OSError("boom: unreadable tree")
        return collect_project_status(root, **kwargs)

    monkeypatch.setattr("memtomem.cli.context_cmd.collect_project_status", _exploding)

    result = CliRunner().invoke(context_group, ["status", "--all-projects"])

    assert result.exit_code == 1, result.output
    assert "status collection failed: boom: unreadable tree" in result.output
    assert "Summary: 0 with drift, 1 clean, 1 error(s), 0 skipped." in result.output
    assert len(seen_kwargs) == 2
    for kwargs in seen_kwargs:
        assert kwargs["target_scope"] == "project_shared"
        assert isinstance(kwargs["wiki"], WikiStore)


def test_diff_probe_error_exits_one_not_drift(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1692: a per-kind diff probe that RAISES is an error, not drift — the
    row renders red, the batch exits 1, and it is counted under errors (not
    drifted). A failed probe cannot establish the sync state, so the old exit-0
    "drift" classification was actively misleading."""
    a = _project(tmp_path, "proj-a")  # clean store, but its settings diff will explode
    b = _project(tmp_path, "proj-b")  # genuinely clean sibling
    _seed_known_projects(known_projects_path, [(a, True), (b, True)])
    monkeypatch.chdir(a)

    def _boom(project_root, *, scope):
        if Path(project_root) == a:
            raise RuntimeError("settings diff exploded")
        return {}

    monkeypatch.setattr("memtomem.context.settings.diff_settings", _boom)

    result = CliRunner().invoke(context_group, ["status", "--all-projects"])

    assert result.exit_code == 1, result.output
    assert "settings diff failed: settings diff exploded" in result.output
    assert f"{b}" in result.output  # sibling still rendered
    # Counted as an error, NOT drift; drift stays 0 (no row drift, no count drift).
    assert "Summary: 0 with drift, 1 clean, 1 error(s), 0 skipped." in result.output


def test_zero_eligible_informational_exit_zero(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """All-skipped batch exits 0 (cron safety, the batch precedent). The
    cwd anchor must NOT be inside any project for this to be reachable."""
    paused = _project(tmp_path, "paused")
    _seed_known_projects(known_projects_path, [(paused, False)])
    neutral = tmp_path / "neutral"
    neutral.mkdir()
    monkeypatch.chdir(neutral)

    result = CliRunner().invoke(context_group, ["status", "--all-projects"])

    assert result.exit_code == 0, result.output
    assert "Summary: 0 with drift, 0 clean, 0 error(s)," in result.output


def test_wiki_absent_degradation_note(
    tmp_path: Path, fake_home: Path, known_projects_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#1280 acceptance: wiki-unreachable degrades per single-project
    semantics — a clean installed entry renders as a stale-pin detail row
    under the project block, header notes the unchecked pins, exit 0."""
    a = _project(tmp_path, "proj-a")
    dest = a / ".memtomem" / "skills" / "pinned"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_bytes(b"# s\n")
    from memtomem.context._atomic import installed_at_from_dest
    from memtomem.context.lockfile import Lockfile

    Lockfile.at(a).upsert_entry(
        "skills", "pinned", wiki_commit="0" * 40, installed_at=installed_at_from_dest(dest)
    )
    _seed_known_projects(known_projects_path, [(a, True)])
    monkeypatch.chdir(a)

    result = CliRunner().invoke(context_group, ["status", "--all-projects"])

    assert result.exit_code == 0, result.output
    assert "wiki unavailable; pin reachability not checked" in result.output
    assert "skills/pinned" in result.output
    assert "(wiki not present)" in result.output
    assert "Summary: 1 with drift, 0 clean, 0 error(s), 0 skipped." in result.output
