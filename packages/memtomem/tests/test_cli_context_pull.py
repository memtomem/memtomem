"""ADR-0030 PR-C — the ``mm context pull`` CLI surface.

Pins the CLI translation of the prepare/commit engine (engine semantics live
in ``test_context_pull_apply.py``): the dry-run preview + ``--diff`` / ``--json``,
the flag-combination guards, the §5 ``source_conflict`` rendering, the
scope-explicitness + Gate B confirmation, and the pre-confirmation early
refusals.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context
from memtomem.context.scope_resolver import canonical_artifact_dir

from .helpers import seed_multi_runtime

_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    (p / ".memtomem").mkdir()
    monkeypatch.chdir(p)
    return p


def _agent(name: str, marker: str) -> str:
    return f"---\nname: {name}\ndescription: t\n---\n{marker}\n"


def _invoke(args: list[str], **kw: object):
    return CliRunner().invoke(context, args, **kw)  # type: ignore[arg-type]


def _canonical_agent_text(proj: Path, name: str) -> str:
    d = canonical_artifact_dir("agents", "project_shared", proj) / name
    return (d / "agent.md").read_text(encoding="utf-8")


# ── dry-run preview ───────────────────────────────────────────────────────────


def test_preview_default_no_writes(proj: Path) -> None:
    seed_multi_runtime(
        proj, "agents", "a", {"claude": _agent("a", "c"), "gemini": _agent("a", "g")}
    )
    res = _invoke(["pull", "agents", "a"])
    assert res.exit_code == 0
    assert "ambiguous" in res.output
    assert "Run with --apply" in res.output
    # No canonical written.
    assert not (canonical_artifact_dir("agents", "project_shared", proj) / "a").exists()


def test_preview_diff_shows_unified(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "hello world")})
    res = _invoke(["pull", "agents", "a", "--diff"])
    assert res.exit_code == 0
    assert "hello world" in res.output
    assert "+++" in res.output


def test_preview_from_narrows_to_source(proj: Path) -> None:
    """`--from gemini` narrows the preview + diff to gemini (not all candidates)."""
    seed_multi_runtime(
        proj, "agents", "a", {"claude": _agent("a", "CLAUDE"), "gemini": _agent("a", "GEM")}
    )
    res = _invoke(["pull", "agents", "a", "--from", "gemini", "--diff"])
    assert res.exit_code == 0
    assert "gemini" in res.output
    assert "claude" not in res.output  # filtered out
    assert "source: gemini" in res.output


def test_preview_json_shape(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    res = _invoke(["pull", "agents", "a", "--json"])
    assert res.exit_code == 0
    data = json.loads(res.output)
    assert data["kind"] == "agents"
    assert data["name"] == "a"
    assert "candidates" in data and data["candidates"][0]["runtime"] == "claude"


# ── flag guards (exit 2) ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "args",
    [
        ["pull", "agents", "a", "--yes"],
        ["pull", "agents", "a", "--diff", "--apply"],
        ["pull", "agents", "a", "--json", "--apply"],
        ["pull", "agents", "a", "--diff", "--json"],
    ],
)
def test_flag_combo_usage_errors(proj: Path, args: list[str]) -> None:
    res = _invoke(args)
    assert res.exit_code == 2


def test_project_local_refused_both_modes(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    for extra in ([], ["--apply"]):
        res = _invoke(["pull", "agents", "a", "--scope", "project_local", *extra])
        assert res.exit_code != 0
        assert "project_local" in res.output


# ── apply ─────────────────────────────────────────────────────────────────────


def test_apply_single_candidate(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "only")})
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared", "--yes"])
    assert res.exit_code == 0, res.output
    assert "Pulled agents/a from claude" in res.output
    assert "only" in _canonical_agent_text(proj, "a")


def test_apply_source_conflict_refuses(proj: Path) -> None:
    seed_multi_runtime(
        proj, "agents", "a", {"claude": _agent("a", "c"), "gemini": _agent("a", "g")}
    )
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared", "--yes"])
    assert res.exit_code != 0
    assert "pass --from" in res.output
    assert not (canonical_artifact_dir("agents", "project_shared", proj) / "a").exists()


def test_apply_from_lands_chosen(proj: Path) -> None:
    seed_multi_runtime(
        proj, "agents", "a", {"claude": _agent("a", "CLAUDE"), "gemini": _agent("a", "GEM")}
    )
    res = _invoke(
        ["pull", "agents", "a", "--apply", "--from", "gemini", "--scope", "project_shared", "--yes"]
    )
    assert res.exit_code == 0, res.output
    assert "GEM" in _canonical_agent_text(proj, "a")


def test_from_codex_agents_is_export_only(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    res = _invoke(["pull", "agents", "a", "--from", "codex"])
    assert res.exit_code != 0
    assert "codex" in res.output.lower()


def test_from_unknown_runtime(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    res = _invoke(["pull", "agents", "a", "--from", "bogus"])
    # click.Choice on --from? No: --from is a free RUNTIME; engine rejects it.
    assert res.exit_code != 0


# ── overwrite / identical ─────────────────────────────────────────────────────


def test_apply_overwrite_refused_without_flag(proj: Path) -> None:
    # Seed a Store agent, then a divergent runtime copy.
    d = canonical_artifact_dir("agents", "project_shared", proj) / "a"
    d.mkdir(parents=True)
    (d / "agent.md").write_text(_agent("a", "STORE"), encoding="utf-8")
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "RUNTIME")})
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared", "--yes"])
    assert res.exit_code != 0
    assert "--overwrite" in res.output


def test_apply_identical_noop(proj: Path) -> None:
    body = _agent("a", "same")
    d = canonical_artifact_dir("agents", "project_shared", proj) / "a"
    d.mkdir(parents=True)
    (d / "agent.md").write_text(body, encoding="utf-8")
    seed_multi_runtime(proj, "agents", "a", {"claude": body})
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared", "--yes"])
    assert res.exit_code == 0, res.output
    assert "already identical" in res.output


def test_skills_overwrite_message(proj: Path) -> None:
    d = canonical_artifact_dir("skills", "project_shared", proj) / "s"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: s\n---\nold\n", encoding="utf-8")
    seed_multi_runtime(proj, "skills", "s", {"claude": "---\nname: s\n---\nnew\n"})
    res = _invoke(
        ["pull", "skills", "s", "--apply", "--overwrite", "--scope", "project_shared", "--yes"]
    )
    assert res.exit_code != 0
    assert "not yet supported" in res.output or "delete the canonical" in res.output


# ── scope explicitness + Gate B (R1 Blocker 2) ────────────────────────────────


def test_apply_inferred_scope_refused(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    res = _invoke(["pull", "agents", "a", "--apply", "--yes"])
    assert res.exit_code != 0
    assert "explicit --scope project_shared" in res.output


def test_project_shared_prompt_decline_writes_nothing(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared"], input="n\n")
    assert res.exit_code != 0  # aborted
    assert not (canonical_artifact_dir("agents", "project_shared", proj) / "a").exists()


def test_project_shared_prompt_accept_writes(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", "c")})
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared"], input="y\n")
    assert res.exit_code == 0, res.output
    assert "c" in _canonical_agent_text(proj, "a")


# ── Gate A ────────────────────────────────────────────────────────────────────


def test_project_shared_secret_hard_refused(proj: Path) -> None:
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", f"tok {_SECRET}")})
    res = _invoke(
        [
            "pull",
            "agents",
            "a",
            "--apply",
            "--scope",
            "project_shared",
            "--yes",
            "--force-unsafe-import",
        ]
    )
    assert res.exit_code != 0
    assert "Gate A" in res.output
    assert not (canonical_artifact_dir("agents", "project_shared", proj) / "a").exists()


def test_gate_blocked_refused_before_prompt(proj: Path) -> None:
    """A project_shared secret is refused BEFORE the confirmation prompt runs
    (input 'y' would otherwise proceed) — Codex Minor 2."""
    seed_multi_runtime(proj, "agents", "a", {"claude": _agent("a", f"tok {_SECRET}")})
    res = _invoke(["pull", "agents", "a", "--apply", "--scope", "project_shared"], input="y\n")
    assert res.exit_code != 0
    assert "Continue?" not in res.output  # never reached the prompt
