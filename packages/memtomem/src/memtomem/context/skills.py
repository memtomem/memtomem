"""Canonical ⇄ runtime skill directory fan-out.

Phase 1 of the "memtomem as canonical context gateway" plan. A skill lives at
``.memtomem/skills/<name>/SKILL.md`` (plus optional ``scripts/``, ``references/``,
``assets/`` sub-directories). From that single canonical source we fan out to
runtime-specific directories:

* Claude Code → ``.claude/skills/``
* Gemini CLI → ``.gemini/skills/``
* OpenAI Codex CLI → ``.agents/skills/``

Anthropic released the Agent Skills spec as an open standard in 2025-12 and
OpenAI adopted the same SKILL.md format for Codex CLI, so the on-disk payload
is byte-identical across all three runtimes today. We still route everything
through a ``SkillGenerator`` registry so Phase 2+ can introduce per-runtime
frontmatter rewriting without touching callers.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

CANONICAL_SKILL_ROOT = ".memtomem/skills"
SKILL_MANIFEST = "SKILL.md"


class SkillGenerator(Protocol):
    """Protocol for runtime-specific skill targets."""

    name: str
    output_root: str  # relative to project root, e.g. ".claude/skills"
    scope: str  # "project" or "user"
    default: bool  # included when scope filter is None (backward compat)

    def target_dir(self, project_root: Path, skill_name: str) -> Path:
        """Return the directory that should hold the rendered skill."""
        ...


# ── Generator registry ────────────────────────────────────────────────

SKILL_GENERATORS: dict[str, SkillGenerator] = {}


def _register(gen: SkillGenerator) -> SkillGenerator:
    SKILL_GENERATORS[gen.name] = gen
    return gen


@dataclass
class ClaudeSkillsGenerator:
    name: str = "claude_skills"
    output_root: str = ".claude/skills"
    scope: str = "project"
    default: bool = True

    def target_dir(self, project_root: Path, skill_name: str) -> Path:
        return project_root / self.output_root / skill_name


@dataclass
class GeminiSkillsGenerator:
    name: str = "gemini_skills"
    output_root: str = ".gemini/skills"
    scope: str = "project"
    default: bool = True

    def target_dir(self, project_root: Path, skill_name: str) -> Path:
        return project_root / self.output_root / skill_name


@dataclass
class CodexSkillsGenerator:
    name: str = "codex_skills"
    # Codex CLI's primary project-scope skill path (also accepted by Gemini CLI
    # as an alias, which is why fanning out to all three runtimes creates a
    # slight amount of on-disk overlap — Gemini will silently de-dup it).
    output_root: str = ".agents/skills"
    scope: str = "project"
    default: bool = True

    def target_dir(self, project_root: Path, skill_name: str) -> Path:
        return project_root / self.output_root / skill_name


@dataclass
class ClaudeUserSkillsGenerator:
    """User-scope: ``~/.claude/skills/<name>/``."""

    name: str = "claude_user_skills"
    output_root: str = "~/.claude/skills"
    scope: str = "user"
    default: bool = False

    def target_dir(self, project_root: Path, skill_name: str) -> Path:
        return Path.home() / ".claude/skills" / skill_name


@dataclass
class GeminiUserSkillsGenerator:
    """User-scope: ``~/.gemini/skills/<name>/``."""

    name: str = "gemini_user_skills"
    output_root: str = "~/.gemini/skills"
    scope: str = "user"
    default: bool = False

    def target_dir(self, project_root: Path, skill_name: str) -> Path:
        return Path.home() / ".gemini/skills" / skill_name


@dataclass
class CodexUserSkillsGenerator:
    """User-scope: ``~/.agents/skills/<name>/``."""

    name: str = "codex_user_skills"
    output_root: str = "~/.agents/skills"
    scope: str = "user"
    default: bool = False

    def target_dir(self, project_root: Path, skill_name: str) -> Path:
        return Path.home() / ".agents/skills" / skill_name


_register(ClaudeSkillsGenerator())
_register(GeminiSkillsGenerator())
_register(CodexSkillsGenerator())
_register(ClaudeUserSkillsGenerator())
_register(GeminiUserSkillsGenerator())
_register(CodexUserSkillsGenerator())


# ── Canonical helpers ─────────────────────────────────────────────────


def canonical_skills_root(project_root: Path) -> Path:
    return project_root / CANONICAL_SKILL_ROOT


def list_canonical_skills(project_root: Path) -> list[Path]:
    """Return canonical skill directories sorted by name.

    A sub-directory only counts as a skill if it contains ``SKILL.md``. This
    mirrors Gemini CLI's discovery rule and lets users drop auxiliary folders
    next to real skills without them being mistaken for skills.
    """
    root = canonical_skills_root(project_root)
    if not root.is_dir():
        return []
    skills: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and (entry / SKILL_MANIFEST).is_file():
            skills.append(entry)
    return skills


# ── Copy primitive ────────────────────────────────────────────────────


def copy_skill(src: Path, dst: Path) -> None:
    """Mirror a skill directory from ``src`` to ``dst``.

    ``src`` MUST contain ``SKILL.md``. If ``dst`` already exists and looks like
    a skill directory (has its own ``SKILL.md``) it is replaced wholesale so
    that removed files on the source side propagate. If ``dst`` exists but
    does NOT look like a skill directory, the copy aborts with ``IsADirectoryError``
    to avoid clobbering something the user put there by hand.
    """
    manifest = src / SKILL_MANIFEST
    if not manifest.is_file():
        raise FileNotFoundError(f"source skill missing {SKILL_MANIFEST}: {src}")

    if dst.exists():
        if not dst.is_dir():
            raise NotADirectoryError(f"target exists and is not a directory: {dst}")
        if not (dst / SKILL_MANIFEST).is_file() and any(dst.iterdir()):
            # Non-empty directory that is NOT a skill — refuse to overwrite.
            raise IsADirectoryError(
                f"refusing to overwrite non-skill directory: {dst} "
                f"(add a SKILL.md or remove the directory first)"
            )
        shutil.rmtree(dst)

    dst.mkdir(parents=True)
    for entry in src.iterdir():
        if entry.is_file():
            shutil.copy2(entry, dst / entry.name)
        elif entry.is_dir():
            shutil.copytree(entry, dst / entry.name)


# ── Fan-out: canonical → runtimes ─────────────────────────────────────


@dataclass
class ExtractResult:
    """Result of a reverse (runtime → canonical) import."""

    imported: list[Path]
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (item_name, reason)


@dataclass
class SkillSyncResult:
    generated: list[tuple[str, Path]]  # (runtime_name, target_path)
    skipped: list[tuple[str, str]]  # (runtime_name, reason)


def _select_generators(
    generators: dict[str, SkillGenerator],
    scope: str | None,
) -> list[str]:
    """Return generator names matching *scope*."""
    if scope is None:
        return [n for n, g in generators.items() if g.default]
    if scope == "all":
        return list(generators.keys())
    return [n for n, g in generators.items() if g.scope == scope]


def generate_all_skills(
    project_root: Path,
    runtimes: list[str] | None = None,
    scope: str | None = None,
) -> SkillSyncResult:
    """Fan out every canonical skill to the requested runtime targets.

    Args:
        project_root: project root containing ``.memtomem/skills/``.
        runtimes: list of generator names. ``None`` means all registered.
        scope: ``"project"`` / ``"user"`` / ``"all"`` / ``None``.
    """
    generated: list[tuple[str, Path]] = []
    skipped: list[tuple[str, str]] = []

    canonicals = list_canonical_skills(project_root)
    if not canonicals:
        return SkillSyncResult(generated=generated, skipped=[("<all>", "no canonical skills")])

    targets = runtimes if runtimes is not None else _select_generators(SKILL_GENERATORS, scope)
    for target in targets:
        gen = SKILL_GENERATORS.get(target)
        if gen is None:
            skipped.append((target, "unknown runtime"))
            continue
        for skill_dir in canonicals:
            dst = gen.target_dir(project_root, skill_dir.name)
            copy_skill(skill_dir, dst)
            generated.append((target, dst))

    return SkillSyncResult(generated=generated, skipped=skipped)


# ── Reverse: runtimes → canonical ─────────────────────────────────────


def extract_skills_to_canonical(
    project_root: Path,
    overwrite: bool = False,
) -> ExtractResult:
    """Import existing runtime skills into ``.memtomem/skills/``.

    When the same skill name appears in multiple runtimes, the first one wins
    (deterministic order: ``claude_skills`` before ``gemini_skills`` per the
    ``SKILL_DIRS`` ordering in :mod:`memtomem.context.detector`).
    Existing canonical entries are preserved unless ``overwrite=True``.

    Returns an :class:`ExtractResult` with both imported paths and skipped
    items so the caller can warn the user about silent deduplication.
    """
    # Lazy import to avoid cycles at module import time.
    from memtomem.context.detector import detect_skill_dirs

    canonical_root = canonical_skills_root(project_root)
    imported: list[Path] = []
    skipped: list[tuple[str, str]] = []
    seen: dict[str, str] = {}  # skill_name → first runtime label

    for detected in detect_skill_dirs(project_root):
        skill_name = detected.path.name
        runtime_label = detected.agent  # e.g. "claude_skills"
        if skill_name in seen:
            reason = f"already imported from {seen[skill_name]}"
            skipped.append((skill_name, reason))
            logger.warning("skip %s from %s: %s", skill_name, runtime_label, reason)
            continue
        dst = canonical_root / skill_name
        if dst.exists() and not overwrite:
            reason = "canonical exists (use --overwrite)"
            skipped.append((skill_name, reason))
            logger.warning("skip %s from %s: %s", skill_name, runtime_label, reason)
            seen[skill_name] = runtime_label
            continue
        copy_skill(detected.path, dst)
        imported.append(dst)
        seen[skill_name] = runtime_label

    return ExtractResult(imported=imported, skipped=skipped)


# ── Diff: canonical ↔ runtimes ────────────────────────────────────────


def _skill_dirs_equal(a: Path, b: Path) -> bool:
    """Shallow structural + byte-level equality between two skill directories."""
    if not (a.is_dir() and b.is_dir()):
        return False
    a_entries = sorted(p.name for p in a.iterdir())
    b_entries = sorted(p.name for p in b.iterdir())
    if a_entries != b_entries:
        return False
    for name in a_entries:
        ap, bp = a / name, b / name
        if ap.is_file() and bp.is_file():
            if ap.read_bytes() != bp.read_bytes():
                return False
        elif ap.is_dir() and bp.is_dir():
            if not _skill_dirs_equal(ap, bp):
                return False
        else:
            return False
    return True


def diff_skills(
    project_root: Path,
    scope: str | None = None,
) -> list[tuple[str, str, str]]:
    """Compare canonical skills against registered runtimes.

    Returns a sorted list of ``(runtime, skill_name, status)`` tuples where
    status is one of:

    * ``"in sync"`` — content matches byte-for-byte.
    * ``"out of sync"`` — both sides exist but differ.
    * ``"missing target"`` — canonical has it, runtime does not.
    * ``"missing canonical"`` — runtime has it, canonical does not.
    """
    results: list[tuple[str, str, str]] = []
    canonical_root = canonical_skills_root(project_root)
    canonical_names = {p.name for p in list_canonical_skills(project_root)}
    selected = _select_generators(SKILL_GENERATORS, scope)

    for gen_name, gen in SKILL_GENERATORS.items():
        if gen_name not in selected:
            continue
        runtime_root = project_root / gen.output_root
        runtime_names: set[str] = set()
        if runtime_root.is_dir():
            for entry in runtime_root.iterdir():
                if entry.is_dir() and (entry / SKILL_MANIFEST).is_file():
                    runtime_names.add(entry.name)

        for name in sorted(canonical_names | runtime_names):
            if name in canonical_names and name not in runtime_names:
                results.append((gen_name, name, "missing target"))
            elif name in runtime_names and name not in canonical_names:
                results.append((gen_name, name, "missing canonical"))
            else:
                src = canonical_root / name
                dst = gen.target_dir(project_root, name)
                if _skill_dirs_equal(src, dst):
                    results.append((gen_name, name, "in sync"))
                else:
                    results.append((gen_name, name, "out of sync"))

    return results


__all__ = [
    "CANONICAL_SKILL_ROOT",
    "ClaudeSkillsGenerator",
    "ExtractResult",
    "CodexSkillsGenerator",
    "GeminiSkillsGenerator",
    "SKILL_GENERATORS",
    "SKILL_MANIFEST",
    "SkillGenerator",
    "SkillSyncResult",
    "canonical_skills_root",
    "copy_skill",
    "diff_skills",
    "extract_skills_to_canonical",
    "generate_all_skills",
    "list_canonical_skills",
]
