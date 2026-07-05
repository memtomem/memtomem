"""Tests for ``mm wiki {skill,agent,command} {diff, lint}`` — ADR-0008 PR-D.

``diff`` reports how a committed override diverges from the canonical render
(reusing :func:`memtomem.wiki.override.render_seed_bytes`); ``lint`` validates
that a wiki asset is well-formed and installable. The CLI delegates to
:mod:`memtomem.wiki.inspect`; tests exercise the diff states (in-sync /
out-of-sync / no-override), the dropped-field stderr note, the classified
errors (no traceback leaks), and the lint exit-code gate.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from click.testing import CliRunner

from memtomem.cli.wiki_cmd import wiki as wiki_group
from memtomem.wiki.inspect import diff_override, lint_asset
from memtomem.wiki.store import WikiStore


# ── seed helpers (local, mirroring test_wiki_cmd_override.py) ────────────


def _git_commit(wiki_root_path: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", message],
        check=True,
        capture_output=True,
    )


def _initialized_wiki() -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


def _seed_skill(wiki_root_path: Path, name: str, body: bytes = b"# canonical\n") -> None:
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_bytes(body)
    _git_commit(wiki_root_path, f"add {name}")


def _seed_agent(
    wiki_root_path: Path,
    name: str,
    *,
    frontmatter_extra: str = "",
    body: str = "Body of the agent.\n",
) -> None:
    fm_extra = frontmatter_extra
    if fm_extra and not fm_extra.endswith("\n"):
        fm_extra += "\n"
    agent_dir = wiki_root_path / "agents" / name
    agent_dir.mkdir(parents=True)
    canonical = f"---\nname: {name}\ndescription: a test agent\n{fm_extra}---\n\n{body}"
    (agent_dir / "agent.md").write_text(canonical, encoding="utf-8")
    _git_commit(wiki_root_path, f"add agent {name}")


def _seed_command(
    wiki_root_path: Path,
    name: str,
    *,
    frontmatter_extra: str = "",
    body: str = "Command body.\n",
) -> None:
    fm_extra = frontmatter_extra
    if fm_extra and not fm_extra.endswith("\n"):
        fm_extra += "\n"
    cmd_dir = wiki_root_path / "commands" / name
    cmd_dir.mkdir(parents=True)
    canonical = f"---\ndescription: a test command\n{fm_extra}---\n\n{body}"
    (cmd_dir / "command.md").write_text(canonical, encoding="utf-8")
    _git_commit(wiki_root_path, f"add command {name}")


_AGENT_GEMINI_DROPS = "skills:\n  - foo\nisolation: workspace\n"
_COMMAND_GEMINI_DROPS = "argument-hint: <arg>\nallowed-tools: [Read]\nmodel: claude-3-5\n"


# ─────────────────────────────────────────────────────────────────────────
# ── mm wiki <type> diff ──────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────


def test_skill_diff_in_sync(wiki_root: Path) -> None:
    """A freshly seeded override is byte-identical to the canonical render."""
    _initialized_wiki()
    _seed_skill(wiki_root, "hello")
    runner = CliRunner()
    runner.invoke(wiki_group, ["skill", "override", "hello", "--vendor", "claude"])

    result = runner.invoke(wiki_group, ["skill", "diff", "hello", "--vendor", "claude"])

    assert result.exit_code == 0, result.output
    assert "in sync" in result.output


def test_skill_diff_out_of_sync_shows_unified_diff(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_skill(wiki_root, "hello", b"# canonical\nline one\n")
    runner = CliRunner()
    runner.invoke(wiki_group, ["skill", "override", "hello", "--vendor", "claude"])

    target = wiki_root / "skills" / "hello" / "overrides" / "claude.md"
    target.write_bytes(b"# canonical\nline one EDITED\n")

    result = runner.invoke(wiki_group, ["skill", "diff", "hello", "--vendor", "claude"])

    assert result.exit_code == 0, result.output
    out = result.output
    # Unified diff hunk markers + the edited line on the override (+) side.
    assert "@@" in out
    assert "EDITED" in out
    assert "+line one EDITED" in out
    assert "-line one" in out


def test_skill_diff_no_override_prints_seed_hint(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_skill(wiki_root, "hello")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "diff", "hello", "--vendor", "claude"])

    assert result.exit_code == 0, result.output
    out = result.output.replace("\\", "/")
    assert "No override at skills/hello/overrides/claude.md" in out
    assert "mm wiki skill override hello --vendor claude" in out


def test_skill_diff_missing_canonical_classified_error(wiki_root: Path) -> None:
    _initialized_wiki()
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "diff", "ghost", "--vendor", "claude"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "ghost" in result.output


def test_skill_diff_missing_wiki_classified_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(tmp_path / "no-wiki"))
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "diff", "hello", "--vendor", "claude"])

    assert result.exit_code != 0
    assert "wiki not found" in result.output
    assert "Traceback" not in result.output


def test_skill_diff_requires_vendor(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_skill(wiki_root, "hello")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "diff", "hello"])

    assert result.exit_code != 0  # click UsageError: --vendor is required
    assert "vendor" in result.output.lower()


def test_agent_diff_notes_dropped_fields_on_stderr(wiki_root: Path) -> None:
    """Gemini agents drop ``skills`` / ``isolation``. ``diff`` notes them on
    stderr so a reader is not surprised the override omits them."""
    _initialized_wiki()
    _seed_agent(wiki_root, "demo", frontmatter_extra=_AGENT_GEMINI_DROPS)
    runner = CliRunner()
    runner.invoke(wiki_group, ["agent", "override", "demo", "--vendor", "gemini"])

    result = runner.invoke(wiki_group, ["agent", "diff", "demo", "--vendor", "gemini"])

    assert result.exit_code == 0, result.output
    assert "in sync" in result.output  # seed == render
    assert "note:" in result.stderr
    assert "skills" in result.stderr
    assert "isolation" in result.stderr


def test_command_diff_codex_classified_error(wiki_root: Path) -> None:
    """``("commands", "codex")`` has no generator — diff surfaces the
    NotImplementedError as a classified error, not a traceback."""
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "diff", "demo", "--vendor", "codex"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "not yet supported" in result.output


# ─────────────────────────────────────────────────────────────────────────
# ── mm wiki <type> lint ──────────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────


def test_skill_lint_clean_ok(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_skill(wiki_root, "hello")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "lint", "hello"])

    assert result.exit_code == 0, result.output
    assert "skills/hello: OK" in result.output


def test_skill_lint_missing_canonical_errors(wiki_root: Path) -> None:
    _initialized_wiki()
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "lint", "ghost"])

    assert result.exit_code != 0
    assert "error:" in result.output
    assert "missing canonical" in result.output
    assert "lint failed" in result.output


def test_lint_invalid_name_single_error(wiki_root: Path) -> None:
    _initialized_wiki()
    runner = CliRunner()

    # A name with a path separator fails validate_name before any path join.
    result = runner.invoke(wiki_group, ["skill", "lint", "bad/name"])

    assert result.exit_code != 0
    assert "error:" in result.output


def test_agent_lint_unparseable_canonical_errors(wiki_root: Path) -> None:
    """An agent.md with no frontmatter raises AgentParseError → lint error."""
    _initialized_wiki()
    agent_dir = wiki_root / "agents" / "broken"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.md").write_text("no frontmatter here\n", encoding="utf-8")
    _git_commit(wiki_root, "add broken agent")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "lint", "broken"])

    assert result.exit_code != 0
    assert "does not parse" in result.output
    assert "Traceback" not in result.output


def test_agent_lint_vendor_drops_are_warnings_not_errors(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_agent(wiki_root, "demo", frontmatter_extra=_AGENT_GEMINI_DROPS)
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "lint", "demo", "--vendor", "gemini"])

    assert result.exit_code == 0, result.output  # warnings keep exit 0
    assert "warning:" in result.output
    assert "skills" in result.output
    assert "agents/demo: OK" in result.output


def test_lint_without_vendor_scans_existing_overrides(wiki_root: Path) -> None:
    """No ``--vendor`` → lint inspects every override file on disk; a seeded
    gemini override with drops surfaces its warnings."""
    _initialized_wiki()
    _seed_agent(wiki_root, "demo", frontmatter_extra=_AGENT_GEMINI_DROPS)
    runner = CliRunner()
    runner.invoke(wiki_group, ["agent", "override", "demo", "--vendor", "gemini"])

    result = runner.invoke(wiki_group, ["agent", "lint", "demo"])

    assert result.exit_code == 0, result.output
    assert "warning:" in result.output
    assert "'gemini'" in result.output


def test_command_lint_committed_codex_override_errors(wiki_root: Path) -> None:
    """A codex command override can never be rendered/installed → error."""
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    overrides = wiki_root / "commands" / "demo" / "overrides"
    overrides.mkdir(parents=True)
    (overrides / "codex.md").write_text("hand-written codex override\n", encoding="utf-8")
    _git_commit(wiki_root, "add codex override")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "lint", "demo"])

    assert result.exit_code != 0
    assert "error:" in result.output
    assert "lint failed" in result.output


def test_agent_lint_broken_canonical_with_vendor_no_traceback(wiki_root: Path) -> None:
    """Regression: a broken canonical + explicit --vendor must NOT re-enter the
    renderer and leak an AgentParseError traceback — the canonical error is
    reported and the vendor pass is skipped."""
    _initialized_wiki()
    agent_dir = wiki_root / "agents" / "broken"
    agent_dir.mkdir(parents=True)
    (agent_dir / "agent.md").write_text("no frontmatter here\n", encoding="utf-8")
    _git_commit(wiki_root, "add broken agent")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "lint", "broken", "--vendor", "gemini"])

    assert result.exit_code != 0
    assert "does not parse" in result.output
    assert "Traceback" not in result.output


def test_agent_lint_broken_canonical_with_override_no_traceback(wiki_root: Path) -> None:
    """Regression: a broken canonical that already has an override file must
    not crash the per-vendor render pass."""
    _initialized_wiki()
    agent_dir = wiki_root / "agents" / "broken"
    (agent_dir / "overrides").mkdir(parents=True)
    (agent_dir / "agent.md").write_text("still no frontmatter\n", encoding="utf-8")
    (agent_dir / "overrides" / "claude.md").write_text("hand override\n", encoding="utf-8")
    _git_commit(wiki_root, "add broken agent with override")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "lint", "broken"])

    assert result.exit_code != 0
    assert "does not parse" in result.output
    assert "Traceback" not in result.output


def test_command_lint_explicit_codex_vendor_errors(wiki_root: Path) -> None:
    """An explicit --vendor codex asks "can codex represent this command?" — the
    answer is no (no generator), so it must be an error even with no override."""
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "lint", "demo", "--vendor", "codex"])

    assert result.exit_code != 0
    assert "error:" in result.output
    assert "Traceback" not in result.output


def test_lint_flags_stray_override_file(wiki_root: Path) -> None:
    """A wrong-extension override (commands use .toml for gemini) is silently
    ignored by the runtime resolver — lint must flag it as a stray file."""
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    overrides = wiki_root / "commands" / "demo" / "overrides"
    overrides.mkdir(parents=True)
    (overrides / "gemini.md").write_text("misnamed (should be .toml)\n", encoding="utf-8")
    _git_commit(wiki_root, "add stray override")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "lint", "demo"])

    assert result.exit_code != 0
    assert "unexpected file" in result.output
    assert "gemini.md" in result.output


def test_command_lint_missing_wiki_classified_error(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(tmp_path / "no-wiki"))
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "lint", "demo"])

    assert result.exit_code != 0
    assert "wiki not found" in result.output
    assert "Traceback" not in result.output


def test_lint_wrong_case_canonical_warns_agent(wiki_root: Path) -> None:
    """A canonical stored under the wrong case (AGENT.md) draws a warning on
    every platform: on a case-insensitive FS (macOS) the asset works locally
    but git records the wrong case, so case-sensitive clones (Linux) see no
    canonical; there, the missing-canonical error additionally fails lint."""
    _initialized_wiki()
    _seed_agent(wiki_root, "beta")
    agent_dir = wiki_root / "agents" / "beta"
    os.rename(agent_dir / "agent.md", agent_dir / "AGENT.md")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "lint", "beta"])

    assert (
        "canonical filename is case-sensitive: found AGENT.md, expected agent.md" in result.output
    )
    if not (agent_dir / "agent.md").is_file():  # case-sensitive FS
        assert result.exit_code != 0
        assert "missing canonical agents/beta/agent.md" in result.output
    else:  # case-insensitive FS: works locally, warning-only
        assert result.exit_code == 0


def test_lint_wrong_case_canonical_warns_skill(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_skill(wiki_root, "hello")
    skill_dir = wiki_root / "skills" / "hello"
    os.rename(skill_dir / "SKILL.md", skill_dir / "skill.md")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "lint", "hello"])

    assert (
        "canonical filename is case-sensitive: found skill.md, expected SKILL.md" in result.output
    )


def test_lint_missing_asset_dir_stays_classified(wiki_root: Path) -> None:
    """Linting a name with no asset dir must stay a lint finding, never an
    unclassified FileNotFoundError from scanning a nonexistent directory."""
    _initialized_wiki()
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "lint", "ghost"])

    assert result.exit_code != 0
    assert "missing canonical agents/ghost/agent.md" in result.output
    assert not isinstance(result.exception, FileNotFoundError)
    assert "Traceback" not in result.output


# ─────────────────────────────────────────────────────────────────────────
# ── logic layer (memtomem.wiki.inspect) ──────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────


def test_diff_override_in_sync_and_missing(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_skill(wiki_root, "hello")
    store = WikiStore.at_default()

    missing = diff_override(store, "skills", "hello", "claude")
    assert missing.exists is False
    assert missing.in_sync is False
    assert missing.diff_lines == []

    CliRunner().invoke(wiki_group, ["skill", "override", "hello", "--vendor", "claude"])
    synced = diff_override(store, "skills", "hello", "claude")
    assert synced.exists is True
    assert synced.in_sync is True
    assert synced.diff_lines == []


def test_lint_report_ok_false_on_error_true_on_warning(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_agent(wiki_root, "demo", frontmatter_extra=_AGENT_GEMINI_DROPS)
    store = WikiStore.at_default()

    warn_only = lint_asset(store, "agents", "demo", "gemini")
    assert warn_only.ok is True
    assert any(f.level == "warning" for f in warn_only.findings)

    missing = lint_asset(store, "agents", "ghost")
    assert missing.ok is False
    assert any(f.level == "error" for f in missing.findings)
