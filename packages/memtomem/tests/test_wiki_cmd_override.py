"""Tests for ``mm wiki {skill,agent,command} override`` — wiki-side override seeder.

Skills land in PR-C (#624). Agent / command commands and the dropped-field
stderr warning land in PR-D C1b (the helper extraction in commit 1 of that
same PR sets up the shared scaffolding).

The CLI delegates to :mod:`memtomem.wiki.override`. Tests exercise the
happy path, the refuse-vs-force collision UX, the editor flag, the
classified errors, the stdout contract, and (for agents / commands) the
stderr warning when the vendor renderer drops canonical fields.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from memtomem.cli.wiki_cmd import wiki as wiki_group
from memtomem.context._names import override_vendors
from memtomem.wiki.store import WikiStore


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
    """Write ``<wiki>/agents/<name>/agent.md`` with a minimal frontmatter.

    ``frontmatter_extra`` is appended verbatim inside the frontmatter
    block — pass e.g. ``"skills:\\n  - foo\\nisolation: workspace\\n"``
    to trigger gemini's drop set (``skills`` / ``isolation``).
    """
    # Defensive: silent break if a future fixture forgets the trailing
    # newline — closing ``---`` would merge into the previous line and
    # YAML parse fails with a cryptic error.
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
    """Write ``<wiki>/commands/<name>/command.md`` with a minimal frontmatter.

    ``frontmatter_extra`` is appended verbatim — pass e.g.
    ``"argument-hint: <arg>\\nallowed-tools: [Read]\\nmodel: claude-3-5\\n"``
    to trigger gemini's drop set (``argument-hint`` / ``allowed-tools`` /
    ``model``).
    """
    # Defensive: see _seed_agent for the silent-break rationale.
    fm_extra = frontmatter_extra
    if fm_extra and not fm_extra.endswith("\n"):
        fm_extra += "\n"
    cmd_dir = wiki_root_path / "commands" / name
    cmd_dir.mkdir(parents=True)
    canonical = f"---\ndescription: a test command\n{fm_extra}---\n\n{body}"
    (cmd_dir / "command.md").write_text(canonical, encoding="utf-8")
    _git_commit(wiki_root_path, f"add command {name}")


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


# ── happy path ─────────────────────────────────────────────────────────


def test_wiki_skill_override_happy_path(wiki_root: Path) -> None:
    _initialized_wiki()
    canonical_bytes = b"# hello\nbody body body\n"
    _seed_skill(wiki_root, "hello", canonical_bytes)

    runner = CliRunner()
    result = runner.invoke(wiki_group, ["skill", "override", "hello", "--vendor", "claude"])

    assert result.exit_code == 0, result.output
    target = wiki_root / "skills" / "hello" / "overrides" / "claude.md"
    assert target.is_file()
    assert target.read_bytes() == canonical_bytes


def test_wiki_skill_override_no_stderr_warning(wiki_root: Path) -> None:
    """Helper-collapse safety gate: ``_run_seed_override`` adds a stderr
    dropped-fields warning, but skills always seed via byte-copy of the
    canonical (``SeedResult.dropped == []``), so the warning path must
    stay quiescent for skills. The other skill tests only assert on
    stdout via ``result.output``, so a silent regression here would not
    surface elsewhere.

    Click 8.3 keeps ``result.stderr`` separate by default — earlier
    ``mix_stderr=False`` constructor arg was removed.
    """
    _initialized_wiki()
    _seed_skill(wiki_root, "hello")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "override", "hello", "--vendor", "gemini"])

    assert result.exit_code == 0, result.output
    assert result.stderr == ""


# ── refuse vs force ────────────────────────────────────────────────────


def test_wiki_skill_override_refuses_existing(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_skill(wiki_root, "hello")
    runner = CliRunner()
    runner.invoke(wiki_group, ["skill", "override", "hello", "--vendor", "claude"])

    second = runner.invoke(wiki_group, ["skill", "override", "hello", "--vendor", "claude"])

    assert second.exit_code != 0
    assert "already exists" in second.output


def test_wiki_skill_override_force_writes_bak(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_skill(wiki_root, "hello", b"# v1\n")
    runner = CliRunner()
    runner.invoke(wiki_group, ["skill", "override", "hello", "--vendor", "claude"])

    target = wiki_root / "skills" / "hello" / "overrides" / "claude.md"
    target.write_bytes(b"# user-edited content\n")

    # Bump canonical so the seed produces different bytes the second time.
    (wiki_root / "skills" / "hello" / "SKILL.md").write_bytes(b"# v2 canonical\n")

    second = runner.invoke(
        wiki_group, ["skill", "override", "hello", "--vendor", "claude", "--force"]
    )

    assert second.exit_code == 0, second.output
    assert target.read_bytes() == b"# v2 canonical\n"
    backup = target.with_suffix(".md.bak")
    assert backup.is_file()
    assert backup.read_bytes() == b"# user-edited content\n"


# ── error surfaces ─────────────────────────────────────────────────────


def test_wiki_skill_override_unknown_vendor(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_skill(wiki_root, "hello")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "override", "hello", "--vendor", "cursor"])

    assert result.exit_code != 0
    assert "cursor" in result.output  # click.Choice error references the value


def test_wiki_skill_override_missing_skill(wiki_root: Path) -> None:
    _initialized_wiki()
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "override", "ghost", "--vendor", "claude"])

    assert result.exit_code != 0
    # FileNotFoundError surfaced as ClickException — no traceback leaks.
    assert "Traceback" not in result.output
    assert "ghost" in result.output


def test_wiki_skill_override_does_not_create_overrides_dir_on_missing_skill(
    wiki_root: Path,
) -> None:
    """Refused calls (missing skill / collision) MUST NOT leave a
    half-built ``skills/<name>/overrides/`` directory behind. Pre-flight
    the seed bytes BEFORE mkdir so the wiki tree stays clean on refuse."""
    _initialized_wiki()
    runner = CliRunner()

    runner.invoke(wiki_group, ["skill", "override", "ghost", "--vendor", "claude"])

    # The skills/ghost/ subtree must not have been created at all —
    # neither the overrides/ dir nor its parent ghost/ dir.
    assert not (wiki_root / "skills" / "ghost").exists()


def test_wiki_skill_override_missing_wiki(monkeypatch, tmp_path: Path) -> None:
    """No wiki initialized → classified error, not traceback."""
    monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(tmp_path / "no-wiki"))
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "override", "hello", "--vendor", "claude"])

    assert result.exit_code != 0
    assert "wiki not found" in result.output
    assert "Traceback" not in result.output


# ── editor flag ────────────────────────────────────────────────────────


def test_wiki_skill_override_invokes_editor(
    wiki_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialized_wiki()
    _seed_skill(wiki_root, "hello")
    captured: list[str] = []

    def fake_edit(filename: str | None = None, **_kwargs: object) -> None:
        if filename is not None:
            captured.append(filename)

    monkeypatch.setattr("click.edit", fake_edit)
    runner = CliRunner()

    result = runner.invoke(
        wiki_group,
        ["skill", "override", "hello", "--vendor", "claude", "--editor"],
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert Path(captured[0]).as_posix().endswith("/skills/hello/overrides/claude.md")


def test_wiki_skill_override_does_not_invoke_editor_by_default(
    wiki_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialized_wiki()
    _seed_skill(wiki_root, "hello")
    called = False

    def fake_edit(filename: str | None = None, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("click.edit", fake_edit)
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "override", "hello", "--vendor", "claude"])

    assert result.exit_code == 0, result.output
    assert called is False


# ── stdout contract ────────────────────────────────────────────────────


def test_wiki_skill_override_stdout_contract(wiki_root: Path) -> None:
    """Substring/presence assertions — order-independent so the contract
    survives small UX polish changes."""
    _initialized_wiki()
    _seed_skill(wiki_root, "hello")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "override", "hello", "--vendor", "codex"])

    assert result.exit_code == 0, result.output
    # Normalize Windows backslashes so substring assertions on POSIX-style
    # path literals stay portable. Mirrors PR #711 (#643).
    out = result.output.replace("\\", "/")
    assert "Seeded skills/hello/overrides/codex.md" in out
    # Bare absolute path on its own line for shell capture.
    target_path = wiki_root / "skills" / "hello" / "overrides" / "codex.md"
    assert target_path.as_posix() in out
    # Commit hint mentioning git commit.
    assert "git commit" in out
    assert "git add skills/hello/overrides/codex.md" in out


# ─────────────────────────────────────────────────────────────────────────
# ── mm wiki agent override ──────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────


_AGENT_GEMINI_DROPS_FRONTMATTER = "skills:\n  - foo\n  - bar\nisolation: workspace\n"
"""Frontmatter extra that triggers gemini's agent drop set (skills + isolation)."""


def test_wiki_agent_override_happy_path_no_drops(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_agent(wiki_root, "demo")  # minimal frontmatter — claude renders without drops
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "override", "demo", "--vendor", "claude"])

    assert result.exit_code == 0, result.output
    target = wiki_root / "agents" / "demo" / "overrides" / "claude.md"
    assert target.is_file()
    body = target.read_bytes()
    # Vendor renderer wrote markdown frontmatter — name is the canonical key.
    assert b"name: demo" in body
    assert b"Body of the agent." in body
    # No drops → no stderr warning.
    assert result.stderr == ""


def test_wiki_agent_override_warns_on_dropped_fields(wiki_root: Path) -> None:
    """Gemini agents drop ``skills`` + ``isolation``. The CLI must warn
    on stderr so the user editing the override knows what the runtime
    won't see."""
    _initialized_wiki()
    _seed_agent(wiki_root, "demo", frontmatter_extra=_AGENT_GEMINI_DROPS_FRONTMATTER)
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "override", "demo", "--vendor", "gemini"])

    assert result.exit_code == 0, result.output
    err = result.stderr
    assert "warning:" in err
    assert "'gemini'" in err
    assert "skills" in err
    assert "isolation" in err
    # Stdout retains the seed summary independently of the warning.
    assert "Seeded agents/demo/overrides/gemini.md" in result.output.replace("\\", "/")


def test_wiki_agent_override_refuses_existing(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_agent(wiki_root, "demo")
    runner = CliRunner()
    runner.invoke(wiki_group, ["agent", "override", "demo", "--vendor", "claude"])

    second = runner.invoke(wiki_group, ["agent", "override", "demo", "--vendor", "claude"])

    assert second.exit_code != 0
    assert "already exists" in second.output


def test_wiki_agent_override_force_writes_bak(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_agent(wiki_root, "demo")
    runner = CliRunner()
    runner.invoke(wiki_group, ["agent", "override", "demo", "--vendor", "claude"])

    target = wiki_root / "agents" / "demo" / "overrides" / "claude.md"
    target.write_bytes(b"# user-edited content\n")

    second = runner.invoke(
        wiki_group, ["agent", "override", "demo", "--vendor", "claude", "--force"]
    )

    assert second.exit_code == 0, second.output
    backup = target.with_suffix(".md.bak")
    assert backup.is_file()
    assert backup.read_bytes() == b"# user-edited content\n"
    # After force, target was rewritten from the canonical seed.
    assert target.read_bytes() != b"# user-edited content\n"


def test_wiki_agent_override_unknown_vendor(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_agent(wiki_root, "demo")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "override", "demo", "--vendor", "cursor"])

    assert result.exit_code != 0
    assert "cursor" in result.output  # click.Choice error references the value


def test_wiki_agent_override_missing_agent(wiki_root: Path) -> None:
    _initialized_wiki()
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "override", "ghost", "--vendor", "claude"])

    assert result.exit_code != 0
    # FileNotFoundError surfaced as ClickException — no traceback leaks.
    assert "Traceback" not in result.output
    assert "ghost" in result.output
    # Refused call must not leave a half-built overrides/ directory behind.
    assert not (wiki_root / "agents" / "ghost").exists()


def test_wiki_agent_override_missing_wiki(monkeypatch, tmp_path: Path) -> None:
    """No wiki initialized → classified error, not traceback."""
    monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(tmp_path / "no-wiki"))
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "override", "demo", "--vendor", "claude"])

    assert result.exit_code != 0
    assert "wiki not found" in result.output
    assert "Traceback" not in result.output


def test_wiki_agent_override_invokes_editor(
    wiki_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialized_wiki()
    _seed_agent(wiki_root, "demo")
    captured: list[str] = []

    def fake_edit(filename: str | None = None, **_kwargs: object) -> None:
        if filename is not None:
            captured.append(filename)

    monkeypatch.setattr("click.edit", fake_edit)
    runner = CliRunner()

    result = runner.invoke(
        wiki_group,
        ["agent", "override", "demo", "--vendor", "claude", "--editor"],
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert Path(captured[0]).as_posix().endswith("/agents/demo/overrides/claude.md")


def test_wiki_agent_override_does_not_invoke_editor_by_default(
    wiki_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialized_wiki()
    _seed_agent(wiki_root, "demo")
    called = False

    def fake_edit(filename: str | None = None, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("click.edit", fake_edit)
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "override", "demo", "--vendor", "claude"])

    assert result.exit_code == 0, result.output
    assert called is False


def test_wiki_agent_override_stdout_contract(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_agent(wiki_root, "demo")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "override", "demo", "--vendor", "codex"])

    assert result.exit_code == 0, result.output
    # Normalize Windows backslashes so substring assertions on POSIX-style
    # path literals stay portable. Mirrors PR #711 (#643).
    out = result.output.replace("\\", "/")
    # Codex agents extension is .toml (not .md) — verifies extension propagated
    # through OVERRIDE_FORMATS into the helper's stdout summary.
    assert "Seeded agents/demo/overrides/codex.toml" in out
    target_path = wiki_root / "agents" / "demo" / "overrides" / "codex.toml"
    assert target_path.as_posix() in out
    assert "git commit" in out
    assert "git add agents/demo/overrides/codex.toml" in out


# ─────────────────────────────────────────────────────────────────────────
# ── mm wiki command override ────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────


_COMMAND_GEMINI_DROPS_FRONTMATTER = (
    "argument-hint: <arg>\nallowed-tools: [Read, Write]\nmodel: claude-3-5-sonnet\n"
)
"""Frontmatter extra that triggers gemini's command drop set."""


def test_wiki_command_override_happy_path_no_drops(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "override", "demo", "--vendor", "claude"])

    assert result.exit_code == 0, result.output
    target = wiki_root / "commands" / "demo" / "overrides" / "claude.md"
    assert target.is_file()
    assert b"Command body." in target.read_bytes()
    assert result.stderr == ""


def test_wiki_command_override_warns_on_dropped_fields(wiki_root: Path) -> None:
    """Gemini commands drop ``argument-hint`` / ``allowed-tools`` / ``model``."""
    _initialized_wiki()
    _seed_command(wiki_root, "demo", frontmatter_extra=_COMMAND_GEMINI_DROPS_FRONTMATTER)
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "override", "demo", "--vendor", "gemini"])

    assert result.exit_code == 0, result.output
    err = result.stderr
    assert "warning:" in err
    assert "'gemini'" in err
    assert "argument-hint" in err
    assert "allowed-tools" in err
    assert "model" in err
    assert "Seeded commands/demo/overrides/gemini.toml" in result.output.replace("\\", "/")


def test_wiki_command_override_codex_classified_error(wiki_root: Path) -> None:
    """``("commands", "codex")`` is a permanent placeholder row in
    ``OVERRIDE_FORMATS`` — no ``codex_commands`` generator. The CLI must
    surface ``NotImplementedError`` from ``seed_override`` as a classified
    ClickException, not a traceback."""
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "override", "demo", "--vendor", "codex"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "not yet supported" in result.output
    # Refused call must not leave a half-built overrides/ directory behind.
    assert not (wiki_root / "commands" / "demo" / "overrides").exists()


def test_wiki_command_override_refuses_existing(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    runner = CliRunner()
    runner.invoke(wiki_group, ["command", "override", "demo", "--vendor", "claude"])

    second = runner.invoke(wiki_group, ["command", "override", "demo", "--vendor", "claude"])

    assert second.exit_code != 0
    assert "already exists" in second.output


def test_wiki_command_override_force_writes_bak(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    runner = CliRunner()
    runner.invoke(wiki_group, ["command", "override", "demo", "--vendor", "claude"])

    target = wiki_root / "commands" / "demo" / "overrides" / "claude.md"
    target.write_bytes(b"# user-edited content\n")

    second = runner.invoke(
        wiki_group, ["command", "override", "demo", "--vendor", "claude", "--force"]
    )

    assert second.exit_code == 0, second.output
    backup = target.with_suffix(".md.bak")
    assert backup.is_file()
    assert backup.read_bytes() == b"# user-edited content\n"
    assert target.read_bytes() != b"# user-edited content\n"


def test_wiki_command_override_unknown_vendor(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "override", "demo", "--vendor", "cursor"])

    assert result.exit_code != 0
    assert "cursor" in result.output


def test_wiki_command_override_missing_command(wiki_root: Path) -> None:
    _initialized_wiki()
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "override", "ghost", "--vendor", "claude"])

    assert result.exit_code != 0
    assert "Traceback" not in result.output
    assert "ghost" in result.output
    assert not (wiki_root / "commands" / "ghost").exists()


def test_wiki_command_override_missing_wiki(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(tmp_path / "no-wiki"))
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "override", "demo", "--vendor", "claude"])

    assert result.exit_code != 0
    assert "wiki not found" in result.output
    assert "Traceback" not in result.output


def test_wiki_command_override_invokes_editor(
    wiki_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    captured: list[str] = []

    def fake_edit(filename: str | None = None, **_kwargs: object) -> None:
        if filename is not None:
            captured.append(filename)

    monkeypatch.setattr("click.edit", fake_edit)
    runner = CliRunner()

    result = runner.invoke(
        wiki_group,
        ["command", "override", "demo", "--vendor", "claude", "--editor"],
    )

    assert result.exit_code == 0, result.output
    assert len(captured) == 1
    assert Path(captured[0]).as_posix().endswith("/commands/demo/overrides/claude.md")


def test_wiki_command_override_does_not_invoke_editor_by_default(
    wiki_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    called = False

    def fake_edit(filename: str | None = None, **_kwargs: object) -> None:
        nonlocal called
        called = True

    monkeypatch.setattr("click.edit", fake_edit)
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "override", "demo", "--vendor", "claude"])

    assert result.exit_code == 0, result.output
    assert called is False


def test_wiki_command_override_stdout_contract(wiki_root: Path) -> None:
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "override", "demo", "--vendor", "gemini"])

    assert result.exit_code == 0, result.output
    # Normalize Windows backslashes so substring assertions on POSIX-style
    # path literals stay portable. Mirrors PR #711 (#643).
    out = result.output.replace("\\", "/")
    assert "Seeded commands/demo/overrides/gemini.toml" in out
    target_path = wiki_root / "commands" / "demo" / "overrides" / "gemini.toml"
    assert target_path.as_posix() in out
    assert "git commit" in out
    assert "git add commands/demo/overrides/gemini.toml" in out


# ─────────────────────────────────────────────────────────────────────────
# ── kimi vendor — derived Choice (ADR-0008 vendor matrix) ────────────────
# ─────────────────────────────────────────────────────────────────────────


def test_wiki_skill_override_kimi_accepted(wiki_root: Path) -> None:
    """kimi is a registered skills vendor; the CLI accepts ``--vendor kimi``
    and seeds ``overrides/kimi.md`` (skills byte-copy the canonical)."""
    _initialized_wiki()
    canonical_bytes = b"# hello kimi\nbody\n"
    _seed_skill(wiki_root, "hello", canonical_bytes)
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "override", "hello", "--vendor", "kimi"])

    assert result.exit_code == 0, result.output
    target = wiki_root / "skills" / "hello" / "overrides" / "kimi.md"
    assert target.is_file()
    assert target.read_bytes() == canonical_bytes


def test_wiki_agent_override_kimi_accepted(wiki_root: Path) -> None:
    """kimi agents render to ``overrides/kimi.yaml`` via the ``kimi_agents``
    generator — the CLI accepts ``--vendor kimi`` for agents."""
    _initialized_wiki()
    _seed_agent(wiki_root, "demo")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["agent", "override", "demo", "--vendor", "kimi"])

    assert result.exit_code == 0, result.output
    target = wiki_root / "agents" / "demo" / "overrides" / "kimi.yaml"
    assert target.is_file()


def test_wiki_command_override_kimi_rejected(wiki_root: Path) -> None:
    """commands have no kimi row in OVERRIDE_FORMATS, so the derived Choice
    rejects ``--vendor kimi`` as a click usage error — kimi never reaches the
    renderer for commands."""
    _initialized_wiki()
    _seed_command(wiki_root, "demo")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["command", "override", "demo", "--vendor", "kimi"])

    assert result.exit_code != 0
    assert "kimi" in result.output  # click.Choice error references the rejected value


# ── vendor Choice derives from the matrix (anti-re-hardcode guard) ───────


def _vendor_choices(cmd: click.Command) -> tuple[str, ...]:
    for param in cmd.params:
        if param.name == "vendor":
            assert isinstance(param.type, click.Choice)
            return tuple(param.type.choices)
    raise AssertionError(f"{cmd.name!r} has no --vendor option")


@pytest.mark.parametrize("verb", ["override", "diff", "lint"])
@pytest.mark.parametrize(
    "group_name, asset_type",
    [("skill", "skills"), ("agent", "agents"), ("command", "commands")],
)
def test_wiki_vendor_choices_derive_from_matrix(
    group_name: str, asset_type: str, verb: str
) -> None:
    """Every ``mm wiki <type> {override,diff,lint}`` ``--vendor`` Choice must
    equal ``override_vendors(asset_type)`` — a guard against re-hardcoding the
    list (the regression that originally dropped kimi for skills/agents)."""
    group = wiki_group.commands[group_name]
    assert isinstance(group, click.Group)
    cmd = group.commands[verb]
    assert _vendor_choices(cmd) == tuple(override_vendors(asset_type))
