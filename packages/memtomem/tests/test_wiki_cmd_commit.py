"""Tests for ``mm wiki {skill,agent,command} commit`` — the CLI parity of the
web Commit affordance (ADR-0027 §3).

The command server-resolves targets from ``--canonical`` / ``--vendor`` flags and
delegates to the shared :func:`memtomem.wiki.commit.commit_targets` engine, so
these focus on the CLI surface: the isolated commit (an unrelated staged file is
never swept), multi-target single-commit, the no-op message, the friendly errors
(no targets / missing file / absent wiki / invalid name), and the soft privacy
warning on the commit message.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.wiki_cmd import wiki as wiki_group
from memtomem.context._atomic import _file_lock
from memtomem.wiki import commit as wiki_commit
from memtomem.wiki.store import WikiStore

# ``wiki_root`` / ``git_identity`` fixtures come from conftest.py (which imports
# them from _wiki_fixtures), so they need no import here.


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout


def _init_wiki() -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


def _seed_skill(root: Path, name: str = "demo", body: bytes = b"# canonical\n") -> None:
    d = root / "skills" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_bytes(body)
    _git(root, "add", ".")
    _git(root, "commit", "-m", f"add {name}")


def _combined(result) -> str:  # noqa: ANN001
    """stdout + stderr, robust across Click's pre/post-8.2 ``mix_stderr`` change."""
    out = result.output or ""
    try:
        out += result.stderr  # Click ≥8.2 exposes stderr separately
    except ValueError:
        pass  # Click <8.2 already mixed stderr into output
    return out


# ── happy paths ────────────────────────────────────────────────────────────


def test_commit_override_happy(wiki_root: Path) -> None:
    _init_wiki()
    _seed_skill(wiki_root)
    runner = CliRunner()
    # seed an override (uncommitted) then commit just it
    runner.invoke(wiki_group, ["skill", "override", "demo", "--vendor", "claude"])
    head0 = _git(wiki_root, "rev-parse", "HEAD").strip()

    result = runner.invoke(wiki_group, ["skill", "commit", "demo", "-v", "claude"])

    assert result.exit_code == 0, _combined(result)
    assert "Committed" in result.output
    head1 = _git(wiki_root, "rev-parse", "HEAD").strip()
    assert head1 != head0
    files = _git(wiki_root, "show", "--name-only", "--format=", head1).split()
    assert files == ["skills/demo/overrides/claude.md"]
    assert not _git(wiki_root, "status", "--porcelain").strip()  # clean tree


def test_commit_canonical(wiki_root: Path) -> None:
    _init_wiki()
    _seed_skill(wiki_root)
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# canonical v2\n")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "commit", "demo", "--canonical"])

    assert result.exit_code == 0, _combined(result)
    head = _git(wiki_root, "rev-parse", "HEAD").strip()
    assert _git(wiki_root, "show", f"{head}:skills/demo/SKILL.md") == "# canonical v2\n"


def test_multi_target_single_commit(wiki_root: Path) -> None:
    _init_wiki()
    _seed_skill(wiki_root)
    runner = CliRunner()
    runner.invoke(wiki_group, ["skill", "override", "demo", "--vendor", "claude"])
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# canonical v2\n")
    n_before = _git(wiki_root, "rev-list", "--count", "HEAD").strip()

    result = runner.invoke(wiki_group, ["skill", "commit", "demo", "-c", "-v", "claude"])

    assert result.exit_code == 0, _combined(result)
    head = _git(wiki_root, "rev-parse", "HEAD").strip()
    files = sorted(_git(wiki_root, "show", "--name-only", "--format=", head).split())
    assert files == ["skills/demo/SKILL.md", "skills/demo/overrides/claude.md"]
    # exactly ONE new commit, not one per target
    assert int(_git(wiki_root, "rev-list", "--count", "HEAD").strip()) == int(n_before) + 1


def test_default_commit_message(wiki_root: Path) -> None:
    _init_wiki()
    _seed_skill(wiki_root)
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# v2\n")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "commit", "demo", "-c"])

    assert result.exit_code == 0, _combined(result)
    assert _git(wiki_root, "log", "-1", "--format=%s").strip() == "wiki: update skills/demo"


def test_noop_when_unchanged(wiki_root: Path) -> None:
    _init_wiki()
    _seed_skill(wiki_root)
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "commit", "demo", "-c"])

    assert result.exit_code == 0, _combined(result)
    assert "Nothing to commit" in result.output


def test_isolation_unrelated_staged_not_swept(wiki_root: Path) -> None:
    _init_wiki()
    _seed_skill(wiki_root)
    runner = CliRunner()
    runner.invoke(wiki_group, ["skill", "override", "demo", "--vendor", "claude"])
    # stage an unrelated canonical edit in the real index
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# canonical EDITED\n")
    _git(wiki_root, "add", "skills/demo/SKILL.md")

    result = runner.invoke(wiki_group, ["skill", "commit", "demo", "-v", "claude"])

    assert result.exit_code == 0, _combined(result)
    head = _git(wiki_root, "rev-parse", "HEAD").strip()
    files = _git(wiki_root, "show", "--name-only", "--format=", head).split()
    assert files == ["skills/demo/overrides/claude.md"]  # canonical NOT swept in
    assert b"EDITED" in (wiki_root / "skills/demo/SKILL.md").read_bytes()


# ── privacy: soft warning on the commit message (valve, not gate) ──────────


def test_commit_message_privacy_warning_is_soft(wiki_root: Path) -> None:
    _init_wiki()
    _seed_skill(wiki_root)
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# v2\n")
    runner = CliRunner()

    result = runner.invoke(
        wiki_group,
        ["skill", "commit", "demo", "-c", "-m", "leak AKIAIOSFODNN7EXAMPLE oops"],
    )

    assert result.exit_code == 0, _combined(result)  # warned, NOT blocked
    assert "Committed" in result.output
    assert "secret/PII" in _combined(result)
    # the commit still landed with the user's message
    assert "AKIAIOSFODNN7EXAMPLE" in _git(wiki_root, "log", "-1", "--format=%s")


# ── classified errors (no traceback leaks) ─────────────────────────────────


def test_no_targets_errors_when_overrides_exist(wiki_root: Path) -> None:
    # With a registered override on disk, a bare commit must NOT default to the
    # canonical: silently omitting the override would leave the user believing
    # they committed "the asset". The error enumerates what is there.
    _init_wiki()
    _seed_skill(wiki_root)
    runner = CliRunner()
    runner.invoke(wiki_group, ["skill", "override", "demo", "--vendor", "claude"])

    result = runner.invoke(wiki_group, ["skill", "commit", "demo"])

    assert result.exit_code != 0
    assert "nothing to commit: this asset has overrides on disk (claude.md)" in result.output
    assert "pass --canonical and/or --vendor" in result.output


def test_bare_commit_defaults_to_canonical(wiki_root: Path) -> None:
    # First-authoring flow (#1648): no registered overrides on disk → a bare
    # commit selects the canonical, and says so (the note line is the pinned
    # evidence this is an announced default, not a silent behavior change).
    _init_wiki()
    _seed_skill(wiki_root)
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# canonical v2\n")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "commit", "demo"])

    assert result.exit_code == 0, _combined(result)
    assert "no registered vendor overrides on disk" in result.output
    assert "Committed" in result.output
    head = _git(wiki_root, "rev-parse", "HEAD").strip()
    assert _git(wiki_root, "show", f"{head}:skills/demo/SKILL.md") == "# canonical v2\n"
    files = _git(wiki_root, "show", "--name-only", "--format=", head).split()
    assert files == ["skills/demo/SKILL.md"]


def test_bare_commit_ignores_stray_override_files(wiki_root: Path) -> None:
    # Stray files in overrides/ (wrong extension, .bak) are not commit targets
    # (the runtime resolver never loads them), so they must not block the
    # canonical default — same membership rule lint's _scan_overrides uses.
    _init_wiki()
    _seed_skill(wiki_root)
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# canonical v2\n")
    stray_dir = wiki_root / "skills/demo/overrides"
    stray_dir.mkdir()
    (stray_dir / "gemini.bad").write_bytes(b"wrong extension\n")
    (stray_dir / "claude.md.bak").write_bytes(b"backup\n")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "commit", "demo"])

    assert result.exit_code == 0, _combined(result)
    assert "no registered vendor overrides on disk" in result.output


def test_bare_commit_missing_canonical_friendly_error(wiki_root: Path) -> None:
    # The default may select a canonical that was never authored — the existing
    # "create it first" pre-check stays the safety net.
    _init_wiki()
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "commit", "ghost"])

    assert result.exit_code != 0
    out = result.output.replace("\\", "/")
    assert "no such file in the wiki: skills/ghost/SKILL.md" in out


def test_agent_bare_commit_defaults_to_canonical(wiki_root: Path) -> None:
    # Type parity: the default lives in the shared _run_commit helper.
    _init_wiki()
    d = wiki_root / "agents" / "beta"
    d.mkdir(parents=True)
    (d / "agent.md").write_text(
        "---\nname: beta\ndescription: a test agent\n---\n\nBody.\n", encoding="utf-8"
    )
    _git(wiki_root, "add", ".")
    _git(wiki_root, "commit", "-m", "add agent beta")
    (d / "agent.md").write_text(
        "---\nname: beta\ndescription: edited\n---\n\nBody v2.\n", encoding="utf-8"
    )
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "commit", "beta"])

    assert result.exit_code == 0, _combined(result)
    assert "no registered vendor overrides on disk" in result.output
    assert "edited" in _git(wiki_root, "show", "HEAD:agents/beta/agent.md")


def test_missing_file_friendly_error(wiki_root: Path) -> None:
    _init_wiki()
    _seed_skill(wiki_root)
    runner = CliRunner()

    # gemini override was never seeded → friendly "no such file"
    result = runner.invoke(wiki_group, ["skill", "commit", "demo", "-v", "gemini"])

    assert result.exit_code != 0
    out = result.output.replace("\\", "/")
    assert "no such file in the wiki: skills/demo/overrides/gemini.md" in out
    assert "seed an override before committing" in out


def test_absent_wiki_errors(wiki_root: Path) -> None:
    # wiki_root sets MEMTOMEM_WIKI_PATH but we never init → require_exists fails
    runner = CliRunner()
    result = runner.invoke(wiki_group, ["skill", "commit", "demo", "-c"])
    assert result.exit_code != 0
    assert "wiki not found" in result.output


def test_invalid_name_errors(wiki_root: Path) -> None:
    _init_wiki()
    runner = CliRunner()
    result = runner.invoke(wiki_group, ["skill", "commit", "bad/name", "-c"])
    assert result.exit_code != 0
    assert "invalid skill name" in result.output


def test_detached_head_is_friendly(wiki_root: Path) -> None:
    # A detached-HEAD wiki has no branch to commit onto. The engine's classified
    # WikiDetachedHeadError must surface as a clean, actionable message (the
    # push/pull precedent) — never git's raw "symbolic-ref … failed" string.
    _init_wiki()
    _seed_skill(wiki_root)
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# edited\n")
    _git(wiki_root, "checkout", "--detach")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "commit", "demo", "-c"])

    assert result.exit_code != 0
    out = _combined(result)
    assert "detached HEAD" in out
    assert "check out a branch" in out
    assert "symbolic-ref" not in out  # no raw git gibberish


def test_busy_lock_is_classified(wiki_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A concurrent committer holding the shared cross-process wiki lock makes
    # ``_file_lock`` raise the builtin ``TimeoutError`` (an OSError, NOT a
    # RuntimeError) — it must surface as a clean ClickException, never a traceback.
    _init_wiki()
    _seed_skill(wiki_root)
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# edited\n")
    monkeypatch.setattr(wiki_commit, "_COMMIT_LOCK_TIMEOUT", 0.1)  # fail fast
    runner = CliRunner()
    with _file_lock(wiki_commit.wiki_commit_lock_path(wiki_root), timeout=None):
        result = runner.invoke(wiki_group, ["skill", "commit", "demo", "-c"])
    assert result.exit_code != 0
    assert not isinstance(result.exception, TimeoutError)  # classified, not raw
    assert "timed out" in result.output


# ── agent / command parity (the shared helper covers all three types) ──────


def test_agent_commit_canonical(wiki_root: Path) -> None:
    _init_wiki()
    d = wiki_root / "agents" / "beta"
    d.mkdir(parents=True)
    (d / "agent.md").write_text(
        "---\nname: beta\ndescription: a test agent\n---\n\nBody.\n", encoding="utf-8"
    )
    _git(wiki_root, "add", ".")
    _git(wiki_root, "commit", "-m", "add agent beta")
    (d / "agent.md").write_text(
        "---\nname: beta\ndescription: edited\n---\n\nBody v2.\n", encoding="utf-8"
    )
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "commit", "beta", "-c"])

    assert result.exit_code == 0, _combined(result)
    assert "Committed" in result.output
    assert "edited" in _git(wiki_root, "show", "HEAD:agents/beta/agent.md")
