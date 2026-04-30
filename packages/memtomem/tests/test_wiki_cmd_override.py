"""Tests for ``mm wiki skill override`` — wiki-side override seeder (PR-C[3/3]).

The CLI delegates to :mod:`memtomem.wiki.override`. Tests exercise the
happy path, the refuse-vs-force collision UX, the editor flag, the
classified errors, and the stdout contract.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.wiki_cmd import wiki as wiki_group
from memtomem.wiki.store import WikiStore


def _seed_skill(wiki_root_path: Path, name: str, body: bytes = b"# canonical\n") -> None:
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_bytes(body)
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"add {name}"],
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
    assert captured[0].endswith("/skills/hello/overrides/claude.md")


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
    out = result.output
    assert "Seeded skills/hello/overrides/codex.md" in out
    # Bare absolute path on its own line for shell capture.
    target_path = wiki_root / "skills" / "hello" / "overrides" / "codex.md"
    assert str(target_path) in out
    # Commit hint mentioning git commit.
    assert "git commit" in out
    assert "git add skills/hello/overrides/codex.md" in out
