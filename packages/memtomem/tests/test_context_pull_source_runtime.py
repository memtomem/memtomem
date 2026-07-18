"""ADR-0030 §12 — ``source_runtime`` selection in the extract engines.

The campaign's founding bug: when the same artifact name exists in more than
one runtime with divergent bytes, the fixed-order first-wins scan silently
takes the Claude copy even when a fresher copy lives in another runtime
(``.agents/skills`` = Codex). These tests pin the current first-wins default
AND the new explicit ``source_runtime`` override, plus the export-only
validation and its orthogonality to ``only_name`` / ``dry_run``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context.agents import extract_agents_to_canonical
from memtomem.context.commands import extract_commands_to_canonical
from memtomem.context.skills import extract_skills_to_canonical

from .helpers import seed_multi_runtime, set_home


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    set_home(monkeypatch, str(h))
    return h


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    (p / ".git").mkdir()
    return p


def _skill_body(name: str, marker: str) -> str:
    return f"---\nname: {name}\n---\n{marker}\n"


def _canonical_skill_text(canonical_dir: Path) -> str:
    return (canonical_dir / "SKILL.md").read_text(encoding="utf-8")


# ── Regression pin: stale claude beats fresh codex (first-wins default) ──


def test_skills_default_first_wins_claude_over_codex(home: Path, proj: Path) -> None:
    """No ``source_runtime`` → the fixed order still lets a stale Claude copy
    win over a fresher Codex one. This is the bug the campaign fixes; pinning
    it keeps the default behavior byte-compatible until a caller opts in."""
    seed_multi_runtime(
        proj,
        "skills",
        "shared",
        {"claude": _skill_body("shared", "stale v1"), "codex": _skill_body("shared", "fresh v2")},
    )
    result = extract_skills_to_canonical(proj, scope="project_shared")
    assert [p.name for p in result.imported] == ["shared"]
    assert "stale v1" in _canonical_skill_text(result.imported[0])
    assert result.source_runtimes["shared"] == "claude"
    # Both runtimes surfaced as candidates for the picker (PR-D consumes this).
    assert set(result.runtime_candidates["shared"]) == {"claude", "codex"}


def test_skills_source_runtime_codex_selects_fresh(home: Path, proj: Path) -> None:
    """``source_runtime='codex'`` lands the Codex bytes even though Claude is
    earlier in the fixed order — the campaign's core fix."""
    seed_multi_runtime(
        proj,
        "skills",
        "shared",
        {"claude": _skill_body("shared", "stale v1"), "codex": _skill_body("shared", "fresh v2")},
    )
    result = extract_skills_to_canonical(proj, scope="project_shared", source_runtime="codex")
    assert [p.name for p in result.imported] == ["shared"]
    assert "fresh v2" in _canonical_skill_text(result.imported[0])
    assert result.source_runtimes["shared"] == "codex"


def test_skills_source_runtime_missing_dir_is_empty(home: Path, proj: Path) -> None:
    """A selected runtime with no directory on disk imports nothing (no error)."""
    seed_multi_runtime(proj, "skills", "shared", {"claude": _skill_body("shared", "v1")})
    result = extract_skills_to_canonical(proj, scope="project_shared", source_runtime="kimi")
    assert result.imported == []


def test_skills_source_runtime_invalid_raises(home: Path, proj: Path) -> None:
    with pytest.raises(ValueError, match="unknown runtime 'bogus'"):
        extract_skills_to_canonical(proj, scope="project_shared", source_runtime="bogus")


def test_skills_source_runtime_validates_before_project_local(home: Path, proj: Path) -> None:
    """The runtime is validated up front, before the project_local short-circuit,
    so a bad selection is always loud regardless of scope."""
    with pytest.raises(ValueError, match="unknown runtime"):
        extract_skills_to_canonical(proj, scope="project_local", source_runtime="bogus")


def test_skills_source_runtime_with_dry_run_writes_nothing(home: Path, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "shared", {"codex": _skill_body("shared", "v2")})
    result = extract_skills_to_canonical(
        proj, scope="project_shared", source_runtime="codex", dry_run=True
    )
    # The preview lists the would-import dest but nothing is on disk.
    assert [p.name for p in result.imported] == ["shared"]
    assert not (proj / ".memtomem" / "skills" / "shared").exists()


def test_skills_source_runtime_with_only_name_narrows_both(home: Path, proj: Path) -> None:
    seed_multi_runtime(
        proj,
        "skills",
        "keep",
        {"claude": _skill_body("keep", "c"), "codex": _skill_body("keep", "x")},
    )
    seed_multi_runtime(proj, "skills", "drop", {"codex": _skill_body("drop", "y")})
    result = extract_skills_to_canonical(
        proj, scope="project_shared", source_runtime="codex", only_name="keep"
    )
    names = [p.name for p in result.imported]
    assert names == ["keep"]
    assert "x" in _canonical_skill_text(result.imported[0])


# ── Agents / commands: export-only validation ───────────────────────────


def test_agents_source_runtime_codex_is_export_only(home: Path, proj: Path) -> None:
    with pytest.raises(ValueError, match="export-only for agents"):
        extract_agents_to_canonical(proj, scope="project_shared", source_runtime="codex")


def test_agents_source_runtime_gemini_selects_gemini(home: Path, proj: Path) -> None:
    seed_multi_runtime(
        proj,
        "agents",
        "bot",
        {"claude": "---\nname: bot\n---\nclaude body\n", "gemini": "---\nname: bot\n---\ngem\n"},
    )
    result = extract_agents_to_canonical(proj, scope="project_shared", source_runtime="gemini")
    assert result.source_runtimes["bot"] == "gemini"


def test_commands_source_runtime_gemini_imports_converted_md(home: Path, proj: Path) -> None:
    """The gemini branch converts TOML → Markdown; selecting it lands the
    converted body, and the claude branch is not scanned."""
    seed_multi_runtime(
        proj,
        "commands",
        "greet",
        {"gemini": 'description = "g"\nprompt = "hello from gemini"\n'},
    )
    result = extract_commands_to_canonical(proj, scope="project_shared", source_runtime="gemini")
    assert result.source_runtimes["greet"] == "gemini"
    dst = result.imported[0][0]
    assert "hello from gemini" in dst.read_text(encoding="utf-8")


def test_commands_source_runtime_claude_skips_toml(home: Path, proj: Path) -> None:
    """``source_runtime='claude'`` must not touch the gemini TOML branch."""
    seed_multi_runtime(
        proj,
        "commands",
        "greet",
        {
            "claude": "---\nname: greet\n---\nclaude body\n",
            "gemini": 'description = "g"\nprompt = "gemini body"\n',
        },
    )
    result = extract_commands_to_canonical(proj, scope="project_shared", source_runtime="claude")
    assert result.source_runtimes["greet"] == "claude"
    assert "gemini" not in result.runtime_candidates.get("greet", [])


def test_commands_source_runtime_kimi_unsupported_message(home: Path, proj: Path) -> None:
    """Kimi has no command fan-out at all — the error says 'no support', not
    'export-only' (which would wrongly imply Kimi renders commands)."""
    with pytest.raises(ValueError, match="has no commands support"):
        extract_commands_to_canonical(proj, scope="project_shared", source_runtime="kimi")


def test_seed_multi_runtime_uses_real_suffix_for_codex_agents(home: Path, proj: Path) -> None:
    """The fixture writes the filename the runtime actually uses (codex agents
    are .toml, not .md) so a seeded fixture is one the engine would read."""
    written = seed_multi_runtime(
        proj, "agents", "bot", {"codex": "x = 1\n"}, scope="project_shared"
    )
    assert written["codex"].suffix == ".toml"
