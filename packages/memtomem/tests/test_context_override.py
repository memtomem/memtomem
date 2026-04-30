"""Tests for ``memtomem.context.override`` — vendor override resolution.

Covers ADR-0008 PR-C: Invariant 4 (full-file replacement) plus the
byte-identical regression guard that PR-C must not break.

The byte-identical regression test is intentionally written FIRST as a
guard for the PR-B fan-out semantics: with no overrides present, every
vendor's ``SKILL.md`` MUST equal the canonical byte-for-byte. PR-C only
ever diverges that equality when a real override file is staged.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from memtomem.context import override
from memtomem.context.agents import generate_all_agents
from memtomem.context.commands import generate_all_commands
from memtomem.context.install import install_skill
from memtomem.context.skills import SKILL_GENERATORS, generate_all_skills
from memtomem.wiki.store import WikiStore


# ── helpers ──────────────────────────────────────────────────────────────


def _seed_skill(wiki_root_path: Path, name: str, files: dict[str, bytes]) -> None:
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True)
    for relpath, data in files.items():
        target = skill_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
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


def _sha256(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ── byte-identical regression — PR-C MUST NOT break PR-B fan-out ────────


def test_skills_fanout_byte_identical_without_override(wiki_root: Path, tmp_path: Path) -> None:
    """ADR-0008 Invariant 4 regression guard.

    With no overrides present, all three vendor SKILL.md outputs MUST equal
    canonical byte-for-byte. PR-C must not regress this.
    """
    _initialized_wiki()
    skill_bytes = b"# hello\n\nbody line one\nbody line two\n"
    _seed_skill(wiki_root, "hello", {"SKILL.md": skill_bytes})
    project = tmp_path

    install_skill(project, "hello")
    generate_all_skills(project)

    canonical = project / ".memtomem" / "skills" / "hello" / "SKILL.md"
    canonical_sha = _sha256(canonical)
    assert canonical.read_bytes() == skill_bytes

    for gen_name, gen in SKILL_GENERATORS.items():
        vendor_skill = gen.target_dir(project, "hello") / "SKILL.md"
        assert vendor_skill.is_file(), f"{gen_name} did not write SKILL.md"
        assert _sha256(vendor_skill) == canonical_sha, (
            f"{gen_name} SKILL.md diverged from canonical without any override "
            f"(canonical={canonical_sha[:12]}, vendor={_sha256(vendor_skill)[:12]})"
        )


def test_skills_fanout_byte_identical_preserves_aux_files(wiki_root: Path, tmp_path: Path) -> None:
    """Auxiliary files (scripts/, references/) must also fan out byte-identical
    when no override is in play. Same regression as the SKILL.md case but for
    the rest of the skill directory tree."""
    _initialized_wiki()
    _seed_skill(
        wiki_root,
        "hello",
        {
            "SKILL.md": b"# hello\n",
            "scripts/run.sh": b"#!/bin/bash\necho hi\n",
            "references/notes.md": b"see also: https://example.com\n",
        },
    )
    project = tmp_path

    install_skill(project, "hello")
    generate_all_skills(project)

    for gen_name, gen in SKILL_GENERATORS.items():
        vendor_dir = gen.target_dir(project, "hello")
        for relpath in ["SKILL.md", "scripts/run.sh", "references/notes.md"]:
            canonical = (project / ".memtomem" / "skills" / "hello" / relpath).read_bytes()
            vendor = (vendor_dir / relpath).read_bytes()
            assert vendor == canonical, (
                f"{gen_name} {relpath} diverged from canonical without override"
            )


# ── override.resolve unit tests ─────────────────────────────────────────


def test_resolve_returns_none_when_no_override(tmp_path: Path) -> None:
    (tmp_path / ".memtomem" / "skills" / "foo").mkdir(parents=True)
    assert override.resolve(tmp_path, "skills", "foo", "claude") is None


def test_resolve_returns_path_when_override_present(tmp_path: Path) -> None:
    overrides_dir = tmp_path / ".memtomem" / "skills" / "foo" / "overrides"
    overrides_dir.mkdir(parents=True)
    (overrides_dir / "claude.md").write_bytes(b"# claude override\n")
    result = override.resolve(tmp_path, "skills", "foo", "claude")
    assert result == overrides_dir / "claude.md"


def test_resolve_unknown_vendor_returns_none(tmp_path: Path) -> None:
    """Vendors outside ``OVERRIDE_FORMATS`` (cursor, copilot, ...) get
    None — the matrix is the trust boundary, not a stringly-typed lookup."""
    overrides_dir = tmp_path / ".memtomem" / "skills" / "foo" / "overrides"
    overrides_dir.mkdir(parents=True)
    (overrides_dir / "cursor.md").write_bytes(b"# stray cursor file\n")
    assert override.resolve(tmp_path, "skills", "foo", "cursor") is None


def test_resolve_invariant_1_works_without_wiki(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """ADR-0008 Invariant 1: resolver reads project tree only — no wiki
    needed at sync time. Make sure no wiki env var is consulted by
    pointing the wiki at a non-existent path."""
    monkeypatch.setenv("MEMTOMEM_WIKI_PATH", str(tmp_path / "no-such-wiki"))
    overrides_dir = tmp_path / ".memtomem" / "skills" / "foo" / "overrides"
    overrides_dir.mkdir(parents=True)
    (overrides_dir / "gemini.md").write_bytes(b"# gemini override\n")
    result = override.resolve(tmp_path, "skills", "foo", "gemini")
    assert result == overrides_dir / "gemini.md"


def test_resolve_returns_path_for_agents_override(tmp_path: Path) -> None:
    """Agents override resolution active post-PR-D gate removal.

    Pin-and-invert from ``test_resolve_skips_agents_in_pr_c`` (carry-forward
    from PR-C #624). PR-D-prep #627 added fan-out application sites; PR-D
    C1a removes the ``_PR_C_ACTIVE_TYPES`` gate. Series PR archive.
    """
    overrides_dir = tmp_path / ".memtomem" / "agents" / "bar" / "overrides"
    overrides_dir.mkdir(parents=True)
    (overrides_dir / "claude.md").write_bytes(b"# agent override\n")
    assert override.resolve(tmp_path, "agents", "bar", "claude") == overrides_dir / "claude.md"


def test_resolve_returns_path_for_commands_override(tmp_path: Path) -> None:
    """Commands override resolution active post-PR-D gate removal.

    Pin-and-invert from ``test_resolve_skips_commands_in_pr_c`` (carry-forward
    from PR-C #624). PR-D-prep #627 added fan-out application sites; PR-D
    C1a removes the ``_PR_C_ACTIVE_TYPES`` gate. Series PR archive.
    """
    overrides_dir = tmp_path / ".memtomem" / "commands" / "baz" / "overrides"
    overrides_dir.mkdir(parents=True)
    (overrides_dir / "gemini.toml").write_bytes(b"# command override\n")
    assert override.resolve(tmp_path, "commands", "baz", "gemini") == overrides_dir / "gemini.toml"


# ── fan-out applies overrides for agents/commands (PR-D C1a)

_SAMPLE_AGENT_BODY = """---
name: bar
description: test agent
---

Body of the agent.
"""

_SAMPLE_COMMAND_BODY = """---
description: test command
---

Command body.
"""


def test_agents_fanout_applies_claude_override(tmp_path: Path) -> None:
    """Agents fan-out applies override (full-file replacement, Invariant 4).

    Pin-and-invert from ``test_agents_fanout_does_not_apply_override_under_gate``
    (PR-D-prep #627). PR-D C1a removes ``_PR_C_ACTIVE_TYPES`` so the fan-out
    application site lights up. 3-assertion marker pattern (per
    ``feedback_pin_invert_symmetric_assertion``): canonical body absent +
    override marker present + byte-equality.
    """
    canonical_dir = tmp_path / ".memtomem" / "agents"
    canonical_dir.mkdir(parents=True)
    (canonical_dir / "bar.md").write_text(_SAMPLE_AGENT_BODY, encoding="utf-8")

    overrides_dir = tmp_path / ".memtomem" / "agents" / "bar" / "overrides"
    overrides_dir.mkdir(parents=True)
    override_body = b"# OVERRIDE_MARKER_AGENT\nclaude full replacement body\n"
    (overrides_dir / "claude.md").write_bytes(override_body)

    generate_all_agents(tmp_path, runtimes=["claude_agents"])

    runtime_file = tmp_path / ".claude" / "agents" / "bar.md"
    assert runtime_file.is_file()
    body = runtime_file.read_bytes()
    assert b"Body of the agent." not in body, (
        "canonical body still present despite gate removed — override not applied?"
    )
    assert b"OVERRIDE_MARKER_AGENT" in body, (
        "override marker absent — override file not seeded correctly?"
    )
    assert body == override_body


def test_commands_fanout_applies_claude_override(tmp_path: Path) -> None:
    """Commands fan-out applies override (full-file replacement, Invariant 4).

    Pin-and-invert from ``test_commands_fanout_does_not_apply_override_under_gate``
    (PR-D-prep #627). PR-D C1a removes ``_PR_C_ACTIVE_TYPES`` so the fan-out
    application site lights up. 3-assertion marker pattern (per
    ``feedback_pin_invert_symmetric_assertion``): canonical body absent +
    override marker present + byte-equality.
    """
    canonical_dir = tmp_path / ".memtomem" / "commands"
    canonical_dir.mkdir(parents=True)
    (canonical_dir / "baz.md").write_text(_SAMPLE_COMMAND_BODY, encoding="utf-8")

    overrides_dir = tmp_path / ".memtomem" / "commands" / "baz" / "overrides"
    overrides_dir.mkdir(parents=True)
    override_body = b"# OVERRIDE_MARKER_COMMAND\nclaude full replacement body\n"
    (overrides_dir / "claude.md").write_bytes(override_body)

    generate_all_commands(tmp_path, runtimes=["claude_commands"])

    runtime_file = tmp_path / ".claude" / "commands" / "baz.md"
    assert runtime_file.is_file()
    body = runtime_file.read_bytes()
    assert b"Command body." not in body, (
        "canonical body still present despite gate removed — override not applied?"
    )
    assert b"OVERRIDE_MARKER_COMMAND" in body, (
        "override marker absent — override file not seeded correctly?"
    )
    assert body == override_body


# ── skills fan-out applies overrides correctly ─────────────────────────


def test_skills_fanout_applies_claude_override_only(wiki_root: Path, tmp_path: Path) -> None:
    """Stage a claude override; .claude SKILL.md must equal the override
    bytes while .gemini and .agents/codex SKILL.md keep canonical bytes."""
    _initialized_wiki()
    canonical_bytes = b"# hello\nbody from canonical\n"
    _seed_skill(wiki_root, "hello", {"SKILL.md": canonical_bytes})
    project = tmp_path
    install_skill(project, "hello")

    overrides_dir = project / ".memtomem" / "skills" / "hello" / "overrides"
    overrides_dir.mkdir(parents=True)
    claude_override = b"# hello\nclaude-only override body\n"
    (overrides_dir / "claude.md").write_bytes(claude_override)

    generate_all_skills(project)

    claude_path = SKILL_GENERATORS["claude_skills"].target_dir(project, "hello") / "SKILL.md"
    gemini_path = SKILL_GENERATORS["gemini_skills"].target_dir(project, "hello") / "SKILL.md"
    codex_path = SKILL_GENERATORS["codex_skills"].target_dir(project, "hello") / "SKILL.md"

    assert claude_path.read_bytes() == claude_override
    assert gemini_path.read_bytes() == canonical_bytes
    assert codex_path.read_bytes() == canonical_bytes


def test_skills_fanout_applies_all_three_overrides(wiki_root: Path, tmp_path: Path) -> None:
    _initialized_wiki()
    _seed_skill(wiki_root, "hello", {"SKILL.md": b"# canonical\n"})
    project = tmp_path
    install_skill(project, "hello")

    overrides_dir = project / ".memtomem" / "skills" / "hello" / "overrides"
    overrides_dir.mkdir(parents=True)
    claude_bytes = b"# claude\n"
    gemini_bytes = b"# gemini\n"
    codex_bytes = b"# codex\n"
    (overrides_dir / "claude.md").write_bytes(claude_bytes)
    (overrides_dir / "gemini.md").write_bytes(gemini_bytes)
    (overrides_dir / "codex.md").write_bytes(codex_bytes)

    generate_all_skills(project)

    assert (
        SKILL_GENERATORS["claude_skills"].target_dir(project, "hello") / "SKILL.md"
    ).read_bytes() == claude_bytes
    assert (
        SKILL_GENERATORS["gemini_skills"].target_dir(project, "hello") / "SKILL.md"
    ).read_bytes() == gemini_bytes
    assert (
        SKILL_GENERATORS["codex_skills"].target_dir(project, "hello") / "SKILL.md"
    ).read_bytes() == codex_bytes


def test_override_only_touches_skill_md_not_scripts(wiki_root: Path, tmp_path: Path) -> None:
    """Invariant 4 says ``byte-copy that file`` (singular). Auxiliary
    files (``scripts/``, ``references/``) MUST stay from canonical even
    when an override is staged for the SKILL.md."""
    _initialized_wiki()
    _seed_skill(
        wiki_root,
        "hello",
        {
            "SKILL.md": b"# canonical\n",
            "scripts/run.sh": b"#!/bin/bash\necho canonical\n",
        },
    )
    project = tmp_path
    install_skill(project, "hello")

    overrides_dir = project / ".memtomem" / "skills" / "hello" / "overrides"
    overrides_dir.mkdir(parents=True)
    (overrides_dir / "claude.md").write_bytes(b"# claude only\n")

    generate_all_skills(project)

    claude_dir = SKILL_GENERATORS["claude_skills"].target_dir(project, "hello")
    assert (claude_dir / "SKILL.md").read_bytes() == b"# claude only\n"
    # The script came from canonical and was NOT replaced by any override.
    assert (claude_dir / "scripts" / "run.sh").read_bytes() == (b"#!/bin/bash\necho canonical\n")
