"""ADR-0011 PR-E2 — `mm context init --scope` + Gate A/B + .gitignore tests.

Pins:
- ``_resolve_artifact_cli_scope`` defaults to ``"project_shared"`` (NOT
  ``cfg.hooks.target_scope``).
- Gate B prompt fires only on EXPLICIT ``--scope project_shared``;
  implicit default (no flag) is back-compatible (no prompt).
- ``--scope project_local`` auto-appends a single comment-marker block
  to ``.gitignore`` and is idempotent on repeat invocations.
- Pyproject-only project (no ``.git``) gets a specific warning and the
  init does NOT abort.
- Gate A blocks secret-bearing imports; ``--force-unsafe-import``
  bypasses for user/project_local destinations only and emits an
  audit-log entry; project_shared destinations hard-abort with a
  :class:`click.ClickException`, including under ``--force-unsafe-import``.
- Skills tree walk catches secrets in ``scripts/`` even when SKILL.md
  is clean — atomic skip, no partial copy.
- Gemini commands TOML→Markdown: scan happens on the converted body.
- Codex prompts intentionally not imported even at user scope.
- Unknown decision from ``enforce_write_guard`` raises ``RuntimeError``
  (symmetric assertion guard).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from .helpers import set_home
from memtomem.cli.context_cmd import (
    _GITIGNORE_MARKER,
    _GITIGNORE_PATTERNS,
    _append_gitignore_marker,
    _resolve_artifact_cli_scope,
)
from memtomem.context import _skip_reasons as skip_codes
from memtomem.context.agents import extract_agents_to_canonical
from memtomem.context.commands import extract_commands_to_canonical
from memtomem.context.skills import extract_skills_to_canonical
from memtomem.privacy import WriteGuardResult


_AKIA_SECRET = "AKIAIOSFODNN7EXAMPLE"


# ── Helpers ─────────────────────────────────────────────────────────────


def _make_project(tmp_path: Path, *, git: bool = True, pyproject: bool = False) -> Path:
    """Create a minimal project root under ``tmp_path``."""
    proj = tmp_path / "proj"
    proj.mkdir()
    if git:
        (proj / ".git").mkdir()
    if pyproject:
        (proj / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    return proj


def _seed_user_runtime_agents(home: Path, name: str, content: str) -> Path:
    """Drop a fake Claude user-tier agent at ``$HOME/.claude/agents/<name>.md``."""
    d = home / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


def _seed_project_runtime_agents(proj: Path, name: str, content: str) -> Path:
    d = proj / ".claude" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{name}.md"
    p.write_text(content, encoding="utf-8")
    return p


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# ── _resolve_artifact_cli_scope ─────────────────────────────────────────


def test_resolve_artifact_cli_scope_default_project_shared() -> None:
    """Default is project_shared regardless of cfg.hooks.target_scope."""
    assert _resolve_artifact_cli_scope(None) == "project_shared"


def test_resolve_artifact_cli_scope_explicit_passes_through() -> None:
    assert _resolve_artifact_cli_scope("user") == "user"
    assert _resolve_artifact_cli_scope("project_shared") == "project_shared"
    assert _resolve_artifact_cli_scope("project_local") == "project_local"


def test_resolve_artifact_cli_scope_does_not_read_hooks_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify no Mem2MemConfig() is constructed (default leak guard)."""

    def boom(*a: Any, **kw: Any) -> None:
        raise AssertionError("_resolve_artifact_cli_scope must NOT instantiate Mem2MemConfig")

    monkeypatch.setattr("memtomem.cli.context_cmd.Mem2MemConfig", boom)
    assert _resolve_artifact_cli_scope(None) == "project_shared"


# ── Gate B prompt only on explicit --scope project_shared ──────────────


def test_init_default_no_scope_does_not_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """#1 — implicit default (no --scope) preserves pre-PR-E2 non-interactive shape."""
    proj = _make_project(tmp_path)
    monkeypatch.chdir(proj)
    set_home(monkeypatch, str(tmp_path / "home"))

    result = runner.invoke(cli, ["context", "init"], input="")  # no input on prompts
    # Should succeed without prompting — context.md exists in proj/.memtomem/.
    assert result.exit_code == 0, result.output
    assert (proj / ".memtomem" / "context.md").exists()
    # No "Continue?" prompt from Gate B fired.
    assert "Continue?" not in result.output


def test_init_explicit_project_shared_prompts_and_aborts_on_n(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    proj = _make_project(tmp_path)
    monkeypatch.chdir(proj)
    set_home(monkeypatch, str(tmp_path / "home"))

    result = runner.invoke(
        cli,
        ["context", "init", "--scope", "project_shared"],
        input="n\n",
    )
    assert result.exit_code != 0
    assert "Continue?" in result.output
    # No canonical dirs created on abort.
    assert not (proj / ".memtomem" / "agents").exists()


def test_init_explicit_project_shared_with_confirm_no_prompt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    proj = _make_project(tmp_path)
    monkeypatch.chdir(proj)
    set_home(monkeypatch, str(tmp_path / "home"))

    result = runner.invoke(
        cli,
        ["context", "init", "--scope", "project_shared", "--confirm-project-shared"],
        input="",
    )
    assert result.exit_code == 0, result.output
    assert "Continue?" not in result.output
    for kind in ("agents", "skills", "commands"):
        assert (proj / ".memtomem" / kind).is_dir()
    # Truth table row 2 — explicit --scope project_shared DOES write context.md
    # (artifact-only-scope qualifier is False here, so the project_shared
    # write fires). Pinned per round-3 review nit N1.
    assert (proj / ".memtomem" / "context.md").exists()


# ── --scope user seeds user dirs ───────────────────────────────────────


def test_init_scope_user_seeds_user_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    proj = _make_project(tmp_path)
    monkeypatch.chdir(proj)
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))

    result = runner.invoke(cli, ["context", "init", "--scope", "user"])
    assert result.exit_code == 0, result.output

    for kind in ("agents", "skills", "commands"):
        assert (home / ".memtomem" / kind).is_dir(), f"missing user-tier {kind}"

    # User scope must NOT have written canonical project-tier subdirs.
    assert not (proj / ".memtomem" / "agents").exists()
    assert not (proj / ".memtomem" / "skills").exists()
    assert not (proj / ".memtomem" / "commands").exists()


def test_init_scope_user_with_existing_context_md_keeps_user_dirs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """PR #889 review C1/P2 round 1 + round 2 — ``--scope user`` is
    artifact-only: the project's context.md must NOT be prompted on or
    rewritten. Pre-seeded context.md stays untouched, no prompt fires,
    user-tier dirs are seeded."""
    proj = _make_project(tmp_path)
    monkeypatch.chdir(proj)
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))

    # Pre-seed an existing context.md.
    (proj / ".memtomem").mkdir()
    existing = proj / ".memtomem" / "context.md"
    existing.write_text("# Pre-existing context\n", encoding="utf-8")
    original_bytes = existing.read_bytes()

    # No input piped — the prompt must NOT fire (round 2 fix).
    result = runner.invoke(cli, ["context", "init", "--scope", "user"], input="")
    assert result.exit_code == 0, result.output
    for kind in ("agents", "skills", "commands"):
        assert (home / ".memtomem" / kind).is_dir(), f"user-tier {kind} should be created"
    # context.md untouched (negative pin per
    # feedback_pin_invert_symmetric_assertion.md).
    assert existing.read_bytes() == original_bytes
    # Prompt prose did NOT fire — symmetric prose-side check.
    # Single-keyword grep — robust against prompt-prose reordering
    # ("already exists. Overwrite?" → "Overwrite this file?" wouldn't
    # silently pass). Round-3 review nit N2.
    assert "Overwrite" not in result.output


def test_init_scope_project_local_does_not_touch_context_md(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """PR #889 review P2 round 2 — ``--scope project_local`` is the
    gitignored draft tier. Writing to or prompting on the project_shared
    context.md would violate the local-tier contract and bypass
    Gate B."""
    proj = _make_project(tmp_path)
    monkeypatch.chdir(proj)
    set_home(monkeypatch, str(tmp_path / "home"))

    (proj / ".memtomem").mkdir()
    existing = proj / ".memtomem" / "context.md"
    existing.write_text("# Pre-existing context\n", encoding="utf-8")
    original_bytes = existing.read_bytes()

    result = runner.invoke(cli, ["context", "init", "--scope", "project_local"], input="")
    assert result.exit_code == 0, result.output
    # *.local dirs created.
    for kind in ("agents", "skills", "commands"):
        assert (proj / ".memtomem" / f"{kind}.local").is_dir()
    # context.md untouched + no prompt.
    assert existing.read_bytes() == original_bytes
    # Single-keyword grep — robust against prompt-prose reordering
    # ("already exists. Overwrite?" → "Overwrite this file?" wouldn't
    # silently pass). Round-3 review nit N2.
    assert "Overwrite" not in result.output


def test_init_scope_project_local_no_existing_context_md_does_not_create_one(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """``--scope project_local`` on a fresh project must not create
    context.md either — the artifact-only contract holds in both
    directions (no overwrite, no fresh write)."""
    proj = _make_project(tmp_path)
    monkeypatch.chdir(proj)
    set_home(monkeypatch, str(tmp_path / "home"))

    result = runner.invoke(cli, ["context", "init", "--scope", "project_local"])
    assert result.exit_code == 0, result.output
    assert not (proj / ".memtomem" / "context.md").exists(), (
        "project_local must not synthesize project_shared context.md"
    )


def test_init_implicit_no_scope_works_from_fresh_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """PR #889 review C2 — implicit ``mm context init`` (no --scope) must
    still work from a directory without ``.git``/``pyproject.toml``,
    matching pre-PR-E2 behaviour. The scope-sanity raise is restricted
    to EXPLICIT --scope project_*. Round-3 review D-new-1 also surfaces
    a yellow hint here pointing to ``--scope=user`` for the
    cross-project case."""
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    monkeypatch.chdir(fresh)
    set_home(monkeypatch, str(tmp_path / "home"))

    result = runner.invoke(cli, ["context", "init"], input="")
    assert result.exit_code == 0, result.output
    # Implicit scope = project_shared → seeds <cwd>/.memtomem/ (matching
    # pre-PR-E2 fall-through where _find_project_root returned cwd).
    for kind in ("agents", "skills", "commands"):
        assert (fresh / ".memtomem" / kind).is_dir()
    # Round-3 D-new-1 — non-project warning surfaced. We assert on the
    # most stable substring "--scope=user" (the actionable hint) rather
    # than the full prose so future wording polish doesn't break this
    # pin.
    assert "--scope=user" in result.output


# ── --scope project_local + .gitignore append ──────────────────────────


def test_init_scope_project_local_seeds_local_dirs_and_gitignore(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    proj = _make_project(tmp_path)
    monkeypatch.chdir(proj)
    set_home(monkeypatch, str(tmp_path / "home"))

    result = runner.invoke(cli, ["context", "init", "--scope", "project_local"])
    assert result.exit_code == 0, result.output

    for kind in ("agents", "skills", "commands"):
        assert (proj / ".memtomem" / f"{kind}.local").is_dir()

    gi = (proj / ".gitignore").read_text(encoding="utf-8")
    assert _GITIGNORE_MARKER in gi
    for pat in _GITIGNORE_PATTERNS:
        assert pat in gi


def test_init_scope_project_local_gitignore_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    proj = _make_project(tmp_path)
    monkeypatch.chdir(proj)
    set_home(monkeypatch, str(tmp_path / "home"))

    runner.invoke(cli, ["context", "init", "--scope", "project_local"])
    first = (proj / ".gitignore").read_bytes()
    runner.invoke(cli, ["context", "init", "--scope", "project_local"])
    second = (proj / ".gitignore").read_bytes()
    assert first == second, "second invocation must not mutate .gitignore"


def test_init_scope_project_local_pyproject_only_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    """#5 — pyproject.toml present, .git absent: warn but do not abort."""
    proj = _make_project(tmp_path, git=False, pyproject=True)
    monkeypatch.chdir(proj)
    set_home(monkeypatch, str(tmp_path / "home"))

    result = runner.invoke(cli, ["context", "init", "--scope", "project_local"])
    assert result.exit_code == 0, result.output
    assert "git init" in result.output.lower() or "`.git`" in result.output
    assert not (proj / ".gitignore").exists()
    # Dirs still created.
    assert (proj / ".memtomem" / "agents.local").is_dir()


def test_init_scope_project_local_no_signal_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runner: CliRunner,
) -> None:
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    set_home(monkeypatch, str(tmp_path / "home"))

    result = runner.invoke(cli, ["context", "init", "--scope", "project_local"])
    assert result.exit_code != 0
    assert "requires a project root" in result.output


# ── _append_gitignore_marker direct unit tests ──────────────────────────


def test_append_gitignore_marker_idempotent(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    wrote, msg = _append_gitignore_marker(proj)
    assert wrote and msg == "appended"
    wrote2, msg2 = _append_gitignore_marker(proj)
    assert not wrote2 and msg2 == "already_present"


def test_append_gitignore_marker_no_git_repo_pyproject_only(tmp_path: Path) -> None:
    proj = _make_project(tmp_path, git=False, pyproject=True)
    wrote, msg = _append_gitignore_marker(proj)
    assert not wrote and msg == "no_git_repo_pyproject_only"
    assert not (proj / ".gitignore").exists()


def test_append_gitignore_marker_no_project_signal(tmp_path: Path) -> None:
    proj = tmp_path / "bare"
    proj.mkdir()
    wrote, msg = _append_gitignore_marker(proj)
    assert not wrote and msg == "no_project_signal"


def test_append_gitignore_marker_pattern_grep_ignored(tmp_path: Path) -> None:
    """Marker is the comment line, NOT the patterns — users may have the
    patterns elsewhere for unrelated reasons."""
    proj = _make_project(tmp_path)
    (proj / ".gitignore").write_text(".memtomem/.staging/\n", encoding="utf-8")  # pattern only
    wrote, msg = _append_gitignore_marker(proj)
    assert wrote and msg == "appended"  # block written despite pattern present


# ── Gate A on import path ──────────────────────────────────────────────


def test_extract_agents_user_scope_blocks_secret_no_flag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)

    _seed_user_runtime_agents(home, "leak", f"---\nname: leak\n---\nuses {_AKIA_SECRET}\n")

    result = extract_agents_to_canonical(proj, scope="user")
    # Skipped with PRIVACY_BLOCKED, not imported.
    assert result.imported == []
    names = [name for name, _, _ in result.skipped]
    assert "leak" in names
    codes = [code for _, _, code in result.skipped]
    assert skip_codes.PRIVACY_BLOCKED in codes
    # Canonical user-tier dir does not contain leak.
    assert not (home / ".memtomem" / "agents" / "leak").exists()


def test_extract_agents_user_scope_force_unsafe_writes_and_audits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """#15 + ``feedback_force_unsafe_redaction_valve_only.md``:
    --force-unsafe-import must (a) write raw bytes AND (b) emit audit log."""
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)

    src = _seed_user_runtime_agents(home, "leak", f"---\nname: leak\n---\nuses {_AKIA_SECRET}\n")

    with caplog.at_level(logging.INFO, logger="memtomem.privacy"):
        result = extract_agents_to_canonical(proj, scope="user", force_unsafe_import=True)

    # Imported, with raw bytes preserved.
    assert len(result.imported) == 1
    written_path, _layout = result.imported[0]
    assert written_path.read_bytes() == src.read_bytes()

    # Audit log carries the bypass record.
    bypass_lines = [rec for rec in caplog.records if "force_unsafe=True" in rec.getMessage()]
    assert bypass_lines, f"no bypass audit-log line found in {caplog.records}"


def test_extract_agents_project_shared_blocked_hard_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#2 — project_shared destination + blocked → ClickException."""
    set_home(monkeypatch, str(tmp_path / "home"))
    proj = _make_project(tmp_path)
    _seed_project_runtime_agents(proj, "leak", f"---\nname: leak\n---\nuses {_AKIA_SECRET}\n")

    with pytest.raises(click.ClickException) as exc_info:
        extract_agents_to_canonical(proj, scope="project_shared")
    msg = exc_info.value.message
    assert "Gate A" in msg
    assert "project_shared" in msg
    # No file written.
    assert not (proj / ".memtomem" / "agents" / "leak").exists()


def test_extract_agents_project_shared_force_unsafe_still_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B-spec smoke #16 — --force-unsafe-import does NOT bypass project_shared."""
    set_home(monkeypatch, str(tmp_path / "home"))
    proj = _make_project(tmp_path)
    _seed_project_runtime_agents(proj, "leak", f"---\nname: leak\n---\nuses {_AKIA_SECRET}\n")

    with pytest.raises(click.ClickException) as exc_info:
        extract_agents_to_canonical(proj, scope="project_shared", force_unsafe_import=True)
    assert "no force bypass" in exc_info.value.message.lower()


# ── Skills tree walk ───────────────────────────────────────────────────


def test_extract_skills_per_file_walk_blocks_scripts_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B2 — secret in scripts/leak.py blocks the entire skill (atomic)."""
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)

    skill = home / ".claude" / "skills" / "myskill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: myskill\n---\nclean body\n", encoding="utf-8")
    (skill / "scripts" / "leak.py").write_text(f"# uses {_AKIA_SECRET}\n", encoding="utf-8")

    result = extract_skills_to_canonical(proj, scope="user")
    assert result.imported == []
    names = [name for name, _, _ in result.skipped]
    assert "myskill" in names
    # No partial copy — even SKILL.md must NOT exist in canonical.
    assert not (home / ".memtomem" / "skills" / "myskill").exists()


def test_extract_skills_project_shared_blocked_hard_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skills #2 — project_shared destination + blocked → ClickException.

    PR-E follow-up D2 part 2 — skills.py refactor moved Gate A through
    apply_gate_a; the rglob loop now never observes a project_shared
    block (the helper raises before returning). This test pins the
    contract for the skills kind so the atomic-skill semantic is
    preserved across the refactor.
    """
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)

    skill = proj / ".claude" / "skills" / "myskill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: myskill\n---\nclean body\n", encoding="utf-8")
    (skill / "scripts" / "leak.py").write_text(f"# uses {_AKIA_SECRET}\n", encoding="utf-8")

    with pytest.raises(click.ClickException) as exc_info:
        extract_skills_to_canonical(proj, scope="project_shared")
    msg = exc_info.value.message
    assert "Gate A" in msg
    assert "project_shared" in msg
    # No partial copy — even SKILL.md must NOT exist in canonical.
    assert not (proj / ".memtomem" / "skills" / "myskill").exists()


def test_extract_skills_project_shared_force_unsafe_still_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skills B-spec smoke #16 — --force-unsafe-import does NOT bypass project_shared."""
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)

    skill = proj / ".claude" / "skills" / "myskill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: myskill\n---\nclean body\n", encoding="utf-8")
    (skill / "scripts" / "leak.py").write_text(f"# uses {_AKIA_SECRET}\n", encoding="utf-8")

    with pytest.raises(click.ClickException) as exc_info:
        extract_skills_to_canonical(proj, scope="project_shared", force_unsafe_import=True)
    assert "no force bypass" in exc_info.value.message.lower()


def test_extract_skills_clean_skill_copies_normally(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)

    skill = home / ".claude" / "skills" / "clean"
    (skill / "scripts").mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: clean\n---\nbody\n", encoding="utf-8")
    (skill / "scripts" / "tool.py").write_text("def run(): pass\n", encoding="utf-8")

    result = extract_skills_to_canonical(proj, scope="user")
    assert len(result.imported) == 1
    dst = result.imported[0]
    assert (dst / "SKILL.md").is_file()
    assert (dst / "scripts" / "tool.py").is_file()


# ── Commands two-branch (B3) ───────────────────────────────────────────


def test_extract_commands_gemini_toml_secret_in_prompt_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B3 — Gemini TOML's `prompt` field secret caught after conversion."""
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)

    gem = home / ".gemini" / "commands"
    gem.mkdir(parents=True)
    (gem / "leak.toml").write_text(
        f'description = "leak demo"\nprompt = "uses {_AKIA_SECRET}"\n',
        encoding="utf-8",
    )

    result = extract_commands_to_canonical(proj, scope="user")
    assert result.imported == []
    codes = [code for _, _, code in result.skipped]
    assert skip_codes.PRIVACY_BLOCKED in codes


def test_extract_commands_project_shared_blocked_hard_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commands #2 — project_shared destination + blocked → ClickException.

    Mirrors the agents counterpart at the top of this file. PR-E follow-up
    D2 — apply_gate_a centralised the hard-abort path; this test pins the
    contract for the commands kind so a future helper drift cannot silently
    let a project_shared command write through.
    """
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)

    runtime = proj / ".claude" / "commands"
    runtime.mkdir(parents=True)
    (runtime / "leak.md").write_text(
        f"---\nname: leak\n---\nuses {_AKIA_SECRET}\n", encoding="utf-8"
    )

    with pytest.raises(click.ClickException) as exc_info:
        extract_commands_to_canonical(proj, scope="project_shared")
    msg = exc_info.value.message
    assert "Gate A" in msg
    assert "project_shared" in msg
    assert not (proj / ".memtomem" / "commands" / "leak.md").exists()


def test_extract_commands_project_shared_force_unsafe_still_aborts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Commands B-spec smoke #16 — --force-unsafe-import does NOT bypass project_shared."""
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)

    runtime = proj / ".claude" / "commands"
    runtime.mkdir(parents=True)
    (runtime / "leak.md").write_text(
        f"---\nname: leak\n---\nuses {_AKIA_SECRET}\n", encoding="utf-8"
    )

    with pytest.raises(click.ClickException) as exc_info:
        extract_commands_to_canonical(proj, scope="project_shared", force_unsafe_import=True)
    assert "no force bypass" in exc_info.value.message.lower()


def test_extract_commands_codex_not_imported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex prompts intentionally not imported even at user scope."""
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)

    codex = home / ".codex" / "prompts"
    codex.mkdir(parents=True)
    (codex / "foo.md").write_text("---\nname: foo\n---\nbody\n", encoding="utf-8")

    result = extract_commands_to_canonical(proj, scope="user")
    # The runtime fan-out table reserves ("commands", "codex", "user") but the
    # extract path is Claude+Gemini only.
    assert all(name != "foo" for name, _, _ in result.skipped)
    assert all("foo" not in str(p) for p, _ in result.imported)


# ── project_local short-circuit ─────────────────────────────────────────


def test_extract_agents_project_local_returns_no_fanout_skip(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    result = extract_agents_to_canonical(proj, scope="project_local")
    assert result.imported == []
    assert any(code == skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME for _, _, code in result.skipped)


def test_extract_skills_project_local_returns_no_fanout_skip(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    result = extract_skills_to_canonical(proj, scope="project_local")
    assert result.imported == []
    assert any(code == skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME for _, _, code in result.skipped)


def test_extract_commands_project_local_returns_no_fanout_skip(tmp_path: Path) -> None:
    proj = _make_project(tmp_path)
    result = extract_commands_to_canonical(proj, scope="project_local")
    assert result.imported == []
    assert any(code == skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME for _, _, code in result.skipped)


# ── B1 — symmetric assertion on unknown decision ───────────────────────


def test_unknown_decision_raises_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """B1 — unexpected enforce_write_guard decision is fail-loud."""
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)
    _seed_user_runtime_agents(home, "agt", "---\nname: agt\n---\nbody\n")

    def fake_guard(*a: Any, **kw: Any) -> WriteGuardResult:
        return WriteGuardResult("nonsense", [])

    # Gate A apply lives in _gate_a now (PR-E follow-up D2 — apply_gate_a
    # helper). The chokepoint stayed privacy.enforce_write_guard; only
    # the call site moved.
    monkeypatch.setattr("memtomem.context._gate_a.privacy.enforce_write_guard", fake_guard)
    with pytest.raises(RuntimeError, match="unexpected decision"):
        extract_agents_to_canonical(proj, scope="user")


# ── D2 — audit_context shape pins (PR-E follow-up) ─────────────────────


def _capture_guard_audit(monkeypatch: pytest.MonkeyPatch) -> dict[str, dict[str, object]]:
    """Spy ``privacy.enforce_write_guard`` and capture the first call's audit_context.

    Returns a dict the caller can read after the extract function returns.
    The spy still has to return a real ``WriteGuardResult`` so the extract
    pipeline proceeds normally — we want the audit-context capture, not
    the proceed/block decision.

    Spy signature mirrors the chokepoint (``dict[str, object]``) so a
    future producer that legitimately routes a non-string field (e.g.
    an ``item_idx: int`` from a batch ingress surface) flows through
    this spy without a type mismatch. Current producers all pass
    string-only values; the assertions still pin those shapes.
    """
    captured: dict[str, dict[str, object]] = {}

    def spy(content_text: str, *, audit_context: dict[str, object], **kw: Any) -> WriteGuardResult:
        if "first" not in captured:
            captured["first"] = dict(audit_context)
        return WriteGuardResult("pass", [])

    # All three kinds (agents / skills / commands) now route through
    # _gate_a.apply_gate_a, which dereferences ``privacy.enforce_write_guard``
    # at call time — patching the helper module's namespace catches every
    # caller.
    monkeypatch.setattr("memtomem.context._gate_a.privacy.enforce_write_guard", spy)
    return captured


def test_extract_agents_audit_context_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2 — agents audit_context keeps {source, target, kind, agent_name}.

    PR #889 review carry-over D1 was a sibling-parity gap on the
    commands' audit_context — the source/target/runtime fields were
    missing. Pinning the per-kind shape prevents a future "let's
    normalise audit_context" refactor from silently breaking
    SOC-pipeline grep on per-kind fields.
    """
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)
    _seed_user_runtime_agents(home, "agt", "---\nname: agt\n---\nbody\n")

    captured = _capture_guard_audit(monkeypatch)
    extract_agents_to_canonical(proj, scope="user")
    assert captured["first"].keys() == {"source", "target", "kind", "agent_name"}
    assert captured["first"]["kind"] == "agents"  # plural, intentionally
    assert captured["first"]["agent_name"] == "agt"


def test_extract_skills_audit_context_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2 — skills audit_context keeps {source_file, skill_name, kind}."""
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)
    skill = home / ".claude" / "skills" / "myskill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: myskill\n---\nbody\n", encoding="utf-8")

    captured = _capture_guard_audit(monkeypatch)
    extract_skills_to_canonical(proj, scope="user")
    assert captured["first"].keys() == {"source_file", "skill_name", "kind"}
    assert captured["first"]["kind"] == "skills"  # plural
    assert captured["first"]["skill_name"] == "myskill"


def test_extract_commands_audit_context_shape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D2 — commands audit_context keeps {source, target, kind, runtime, command_name}."""
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)
    runtime = home / ".claude" / "commands"
    runtime.mkdir(parents=True)
    (runtime / "cmd.md").write_text("---\nname: cmd\n---\nbody\n", encoding="utf-8")

    captured = _capture_guard_audit(monkeypatch)
    extract_commands_to_canonical(proj, scope="user")
    assert captured["first"].keys() == {"source", "target", "kind", "runtime", "command_name"}
    assert captured["first"]["kind"] == "commands"  # plural
    assert captured["first"]["runtime"] == "claude"
    assert captured["first"]["command_name"] == "cmd"


# ── #1229 — Gate A surface attribution ──────────────────────────────────


def _capture_guard_surfaces(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spy ``privacy.enforce_write_guard`` and record every call's ``surface``.

    The surface string dimensions the privacy ``record()`` counter and tags
    the force-unsafe bypass audit line — pre-#1229 every ingress surface was
    hard-coded to the CLI literal, misattributing Web/MCP imports.
    """
    surfaces: list[str] = []

    def spy(content_text: str, *, surface: str, **kw: Any) -> WriteGuardResult:
        surfaces.append(surface)
        return WriteGuardResult("pass", [])

    monkeypatch.setattr("memtomem.context._gate_a.privacy.enforce_write_guard", spy)
    return surfaces


def test_extract_default_surface_is_cli_literal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``surface`` kwarg → the CLI literal (back-compat: ``mm context
    init`` relies on the default)."""
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)
    _seed_user_runtime_agents(home, "agt", "---\nname: agt\n---\nbody\n")

    surfaces = _capture_guard_surfaces(monkeypatch)
    extract_agents_to_canonical(proj, scope="user")
    assert surfaces == ["cli_context_init"]


def test_extract_surface_kwarg_reaches_gate_a_for_all_kinds(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``surface`` kwarg threads through every Gate A call site —
    including BOTH command branches (Claude ``.md`` and Gemini ``.toml``);
    missing the second would silently misattribute only Gemini-sourced
    commands."""
    home = tmp_path / "home"
    set_home(monkeypatch, str(home))
    proj = _make_project(tmp_path)
    _seed_user_runtime_agents(home, "agt", "---\nname: agt\n---\nbody\n")
    skill = home / ".claude" / "skills" / "myskill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: myskill\n---\nbody\n", encoding="utf-8")
    claude_cmds = home / ".claude" / "commands"
    claude_cmds.mkdir(parents=True)
    (claude_cmds / "cmd.md").write_text("---\nname: cmd\n---\nbody\n", encoding="utf-8")
    gemini_cmds = home / ".gemini" / "commands"
    gemini_cmds.mkdir(parents=True)
    (gemini_cmds / "gcmd.toml").write_text(
        'description = "g"\nprompt = "gemini prompt"\n', encoding="utf-8"
    )

    surfaces = _capture_guard_surfaces(monkeypatch)
    extract_agents_to_canonical(proj, scope="user", surface="probe_agents")
    assert surfaces == ["probe_agents"]

    surfaces.clear()
    extract_skills_to_canonical(proj, scope="user", surface="probe_skills")
    assert surfaces == ["probe_skills"]

    surfaces.clear()
    extract_commands_to_canonical(proj, scope="user", surface="probe_commands")
    assert surfaces == ["probe_commands", "probe_commands"]  # claude + gemini branches


# ── `mm context init --only NAME` (#1520 item 4) ────────────────────────


class TestInitOnly:
    """Single-name import-only mode: the CLI twin of the web single-name
    import route. Engine ``only_name=`` narrowing already existed; these
    pin the CLI wiring — option validation, side-effect skipping, and the
    not-found exit (empty ``imported`` + ``skipped`` per engine contract).
    """

    def _proj_with_two_skills(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        proj = _make_project(tmp_path)
        monkeypatch.chdir(proj)
        set_home(monkeypatch, str(tmp_path / "home"))
        for name in ("alpha", "beta"):
            skill = proj / ".claude" / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")
        return proj

    def test_only_imports_named_skill_and_skips_seeding(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
    ) -> None:
        proj = self._proj_with_two_skills(tmp_path, monkeypatch)

        result = runner.invoke(cli, ["context", "init", "--include", "skills", "--only", "alpha"])

        assert result.exit_code == 0, result.output
        assert (proj / ".memtomem" / "skills" / "alpha" / "SKILL.md").is_file()
        # Sibling skill not imported; import-only mode: no context.md, no
        # sibling-kind dir seeding.
        assert not (proj / ".memtomem" / "skills" / "beta").exists()
        assert not (proj / ".memtomem" / "context.md").exists()
        assert not (proj / ".memtomem" / "agents").exists()
        assert not (proj / ".memtomem" / "commands").exists()

    def test_only_not_found_exits_1_with_kind_and_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
    ) -> None:
        self._proj_with_two_skills(tmp_path, monkeypatch)

        result = runner.invoke(cli, ["context", "init", "--include", "skills", "--only", "ghost"])

        assert result.exit_code == 1, result.output
        assert "No runtime skills entry named 'ghost'" in result.output
        assert "scope='project_shared'" in result.output

    @pytest.mark.parametrize(
        "include_args",
        [
            [],  # zero kinds
            ["--include", "skills,agents"],  # two kinds
            ["--include", "settings"],  # settings has no import branch
        ],
    )
    def test_only_requires_exactly_one_artifact_kind(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        runner: CliRunner,
        include_args: list[str],
    ) -> None:
        proj = self._proj_with_two_skills(tmp_path, monkeypatch)

        result = runner.invoke(cli, ["context", "init", *include_args, "--only", "alpha"])

        assert result.exit_code == 2, result.output
        assert "--only requires exactly one --include kind" in result.output
        # Fail-fast: nothing written.
        assert not (proj / ".memtomem").exists()

    def test_only_invalid_name_rejected_before_any_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
    ) -> None:
        proj = self._proj_with_two_skills(tmp_path, monkeypatch)

        result = runner.invoke(cli, ["context", "init", "--include", "skills", "--only", "../evil"])

        assert result.exit_code == 1, result.output
        assert not (proj / ".memtomem").exists()

    def test_only_rejects_project_local_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
    ) -> None:
        """Codex review: project_local has no runtime fan-out to import from
        and --only disables all seeding side effects — reject up front rather
        than exiting 0 having done nothing."""
        proj = self._proj_with_two_skills(tmp_path, monkeypatch)

        result = runner.invoke(
            cli,
            [
                "context",
                "init",
                "--include",
                "skills",
                "--only",
                "alpha",
                "--scope",
                "project_local",
            ],
        )

        assert result.exit_code == 2, result.output
        assert "--only cannot pull into --scope=project_local" in result.output
        assert not (proj / ".memtomem").exists()

    def test_only_agents_happy_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
    ) -> None:
        proj = _make_project(tmp_path)
        monkeypatch.chdir(proj)
        set_home(monkeypatch, str(tmp_path / "home"))
        _seed_project_runtime_agents(proj, "helper", "---\nname: helper\n---\nbody\n")
        _seed_project_runtime_agents(proj, "other", "---\nname: other\n---\nbody\n")

        result = runner.invoke(cli, ["context", "init", "--include", "agents", "--only", "helper"])

        assert result.exit_code == 0, result.output
        assert (proj / ".memtomem" / "agents" / "helper").exists()
        assert not (proj / ".memtomem" / "agents" / "other").exists()

    def test_only_commands_happy_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
    ) -> None:
        proj = _make_project(tmp_path)
        monkeypatch.chdir(proj)
        set_home(monkeypatch, str(tmp_path / "home"))
        cmds = proj / ".claude" / "commands"
        cmds.mkdir(parents=True)
        (cmds / "ship.md").write_text("Ship it.\n", encoding="utf-8")
        (cmds / "hold.md").write_text("Hold it.\n", encoding="utf-8")

        result = runner.invoke(cli, ["context", "init", "--include", "commands", "--only", "ship"])

        assert result.exit_code == 0, result.output
        assert (proj / ".memtomem" / "commands" / "ship").exists()
        assert not (proj / ".memtomem" / "commands" / "hold").exists()

    def test_only_without_flag_preserves_full_init(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runner: CliRunner
    ) -> None:
        """No --only → pre-#1520 behavior intact (context.md + dir seeding)."""
        proj = self._proj_with_two_skills(tmp_path, monkeypatch)

        result = runner.invoke(cli, ["context", "init", "--include", "skills"], input="")

        assert result.exit_code == 0, result.output
        assert (proj / ".memtomem" / "context.md").exists()
        assert (proj / ".memtomem" / "skills" / "alpha").exists()
        assert (proj / ".memtomem" / "skills" / "beta").exists()
        assert (proj / ".memtomem" / "agents").exists()
