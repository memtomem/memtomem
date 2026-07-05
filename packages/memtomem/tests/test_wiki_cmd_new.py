"""Tests for ``mm wiki {skill,agent,command} new`` — the authoring scaffold (#1648).

``new`` writes the minimal starter template to the asset's canonical path via
:func:`memtomem.wiki.override.create_canonical`. Tests pin: the exact canonical
filename per type (the case-sensitivity contract), the template ↔ parser
agreement (``new`` → ``lint`` green), the flag-free first-authoring flow
(``new`` → bare ``commit``), the refuse-to-overwrite guard (path-free message,
original bytes intact), and the classified errors.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.wiki_cmd import wiki as wiki_group
from memtomem.wiki.override import AssetExistsError, create_canonical
from memtomem.wiki.store import WikiStore

# ``wiki_root`` / ``git_identity`` fixtures come from conftest.py (which imports
# them from _wiki_fixtures), so they need no import here.

_CANONICAL_BY_TYPE = {
    "skill": ("skills", "SKILL.md"),
    "agent": ("agents", "agent.md"),
    "command": ("commands", "command.md"),
}


def _init_wiki() -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


def _combined(result) -> str:  # noqa: ANN001
    """stdout + stderr, robust across Click's pre/post-8.2 ``mix_stderr`` change."""
    out = result.output or ""
    try:
        out += result.stderr  # Click ≥8.2 exposes stderr separately
    except ValueError:
        pass  # Click <8.2 already mixed stderr into output
    return out


# ── happy path (all three types) ─────────────────────────────────────────


@pytest.mark.parametrize("verb", sorted(_CANONICAL_BY_TYPE))
def test_new_scaffolds_canonical(wiki_root: Path, verb: str) -> None:
    _init_wiki()
    plural, filename = _CANONICAL_BY_TYPE[verb]
    runner = CliRunner()

    result = runner.invoke(wiki_group, [verb, "new", "demo"])

    assert result.exit_code == 0, _combined(result)
    assert f"Created {plural}/demo/{filename}" in result.output
    assert f"# next: edit the file, then: mm wiki {verb} commit demo" in result.output
    assert f"mm context install {verb} demo" in result.output
    asset_dir = wiki_root / plural / "demo"
    # The stored filename must match exactly — iterdir (not is_file) so a
    # case-insensitive filesystem cannot mask a wrong-case write.
    assert [p.name for p in asset_dir.iterdir()] == [filename]
    content = (asset_dir / filename).read_text(encoding="utf-8")
    assert content.startswith("---\nname: demo\n")
    assert "TODO" in content


@pytest.mark.parametrize("verb", sorted(_CANONICAL_BY_TYPE))
def test_new_then_lint_is_green(wiki_root: Path, verb: str) -> None:
    # Template ↔ parser pin: the scaffold must parse under the same gate lint
    # and the vendor renderers use, for every asset type.
    _init_wiki()
    runner = CliRunner()
    assert runner.invoke(wiki_group, [verb, "new", "demo"]).exit_code == 0

    result = runner.invoke(wiki_group, [verb, "lint", "demo"])

    assert result.exit_code == 0, _combined(result)
    assert "OK" in result.output


def test_new_then_bare_commit_flows(wiki_root: Path) -> None:
    # The whole #1648 first-authoring path, end to end and flag-free:
    # scaffold → (edit) → bare commit defaults to the canonical.
    _init_wiki()
    runner = CliRunner()
    assert runner.invoke(wiki_group, ["skill", "new", "demo"]).exit_code == 0
    (wiki_root / "skills/demo/SKILL.md").write_bytes(b"# authored\n")

    result = runner.invoke(wiki_group, ["skill", "commit", "demo"])

    assert result.exit_code == 0, _combined(result)
    assert "Committed" in result.output


# ── refuse-to-overwrite ──────────────────────────────────────────────────


def test_new_refuses_existing_asset(wiki_root: Path) -> None:
    _init_wiki()
    runner = CliRunner()
    assert runner.invoke(wiki_group, ["agent", "new", "beta"]).exit_code == 0
    authored = wiki_root / "agents/beta/agent.md"
    authored.write_bytes(b"---\nname: beta\ndescription: authored\n---\n\nBody.\n")

    result = runner.invoke(wiki_group, ["agent", "new", "beta"])

    assert result.exit_code != 0
    assert "agents/beta already has a canonical" in result.output
    assert "edit the file directly" in result.output
    # Path-free contract: the absolute wiki path never appears in the message.
    assert str(wiki_root) not in result.output
    # The refused scaffold left the authored bytes untouched.
    assert authored.read_bytes() == b"---\nname: beta\ndescription: authored\n---\n\nBody.\n"


def test_new_refuses_directory_squatting_on_canonical_path(wiki_root: Path) -> None:
    # A DIRECTORY at the canonical path is also a collision — it must classify,
    # never surface a raw IsADirectoryError (or leak the absolute wiki path).
    _init_wiki()
    (wiki_root / "skills" / "demo" / "SKILL.md").mkdir(parents=True)
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "new", "demo"])

    assert result.exit_code != 0
    assert "skills/demo already has a canonical" in result.output
    assert "Traceback" not in result.output
    assert str(wiki_root) not in result.output


def test_new_refuses_file_squatting_on_asset_dir(wiki_root: Path) -> None:
    # A FILE at <type>/<name> would make the scaffold's mkdir raise a raw
    # FileExistsError — it must classify the same way.
    _init_wiki()
    (wiki_root / "skills").mkdir(exist_ok=True)
    (wiki_root / "skills" / "demo").write_bytes(b"not a directory\n")
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "new", "demo"])

    assert result.exit_code != 0
    assert "skills/demo already exists in the wiki but is not an asset directory" in result.output
    assert "Traceback" not in result.output
    assert str(wiki_root) not in result.output


# ── classified errors ────────────────────────────────────────────────────


def test_new_absent_wiki_errors(wiki_root: Path) -> None:
    # wiki_root sets MEMTOMEM_WIKI_PATH but we never init → require_exists fails
    runner = CliRunner()
    result = runner.invoke(wiki_group, ["skill", "new", "demo"])
    assert result.exit_code != 0
    assert "wiki not found" in result.output


def test_new_invalid_name_errors(wiki_root: Path) -> None:
    _init_wiki()
    runner = CliRunner()
    result = runner.invoke(wiki_group, ["skill", "new", "bad/name"])
    assert result.exit_code != 0
    assert "invalid skill name" in result.output


def test_create_canonical_rejects_unknown_asset_type(wiki_root: Path) -> None:
    # Library-level guard: the public primitive must not derive paths from an
    # arbitrary asset_type string (CLI callers pass literals, direct callers
    # may not).
    store = _init_wiki()
    with pytest.raises(ValueError, match="unsupported asset_type"):
        create_canonical(store, "bogus", "demo")


def test_create_canonical_repairs_half_authored_dir(wiki_root: Path) -> None:
    # An asset dir without a canonical (e.g. an abandoned hand-authoring
    # attempt) is scaffoldable — only an existing canonical refuses.
    store = _init_wiki()
    (wiki_root / "skills" / "demo").mkdir(parents=True)

    path = create_canonical(store, "skills", "demo")

    assert path == wiki_root / "skills/demo/SKILL.md"
    assert path.is_file()


def test_create_canonical_exists_error_is_path_free(wiki_root: Path) -> None:
    store = _init_wiki()
    create_canonical(store, "skills", "demo")
    with pytest.raises(AssetExistsError) as excinfo:
        create_canonical(store, "skills", "demo")
    assert str(wiki_root) not in str(excinfo.value)


# ── editor flag ──────────────────────────────────────────────────────────


def test_new_invokes_editor(wiki_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_wiki()
    opened: list[str] = []

    def fake_edit(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        opened.append(kwargs.get("filename") or args[0])

    monkeypatch.setattr("click.edit", fake_edit)
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "new", "demo", "--editor"])

    assert result.exit_code == 0, _combined(result)
    assert opened == [str(wiki_root / "skills/demo/SKILL.md")]


def test_new_does_not_invoke_editor_by_default(
    wiki_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_wiki()
    opened: list[str] = []

    def fake_edit(*args, **kwargs) -> None:  # noqa: ANN002, ANN003
        opened.append("called")

    monkeypatch.setattr("click.edit", fake_edit)
    runner = CliRunner()

    result = runner.invoke(wiki_group, ["skill", "new", "demo"])

    assert result.exit_code == 0, _combined(result)
    assert opened == []
