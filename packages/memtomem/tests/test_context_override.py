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
