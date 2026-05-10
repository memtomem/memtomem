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

import click

from memtomem import privacy
from memtomem.context import _skip_reasons as skip_codes
from memtomem.context import override as _override
from memtomem.context._atomic import atomic_write_bytes, copy_tree_atomic
from memtomem.context._gate_a import format_project_shared_block_message
from memtomem.config import TargetScope
from memtomem.context._names import GENERATOR_VENDOR, InvalidNameError, validate_name
from memtomem.context._runtime_targets import runtime_fanout_root
from memtomem.context.scope_resolver import canonical_artifact_dir

logger = logging.getLogger(__name__)

CANONICAL_SKILL_ROOT = ".memtomem/skills"
SKILL_MANIFEST = "SKILL.md"


class SkillGenerator(Protocol):
    """Protocol for runtime-specific skill targets.

    ADR-0011 PR-E: ``target_dir`` accepts a ``scope`` keyword (default
    ``project_shared``). Returns ``None`` when no fan-out by design.
    """

    name: str
    output_root: str  # relative to project root, e.g. ".claude/skills"

    def target_dir(
        self,
        project_root: Path,
        skill_name: str,
        *,
        scope: TargetScope = "project_shared",
    ) -> Path | None:
        """Return the directory that should hold the rendered skill (or ``None``)."""
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

    def target_dir(
        self,
        project_root: Path,
        skill_name: str,
        *,
        scope: TargetScope = "project_shared",
    ) -> Path | None:
        root = runtime_fanout_root("skills", "claude", scope, project_root)
        return None if root is None else root / skill_name


@dataclass
class GeminiSkillsGenerator:
    name: str = "gemini_skills"
    output_root: str = ".gemini/skills"

    def target_dir(
        self,
        project_root: Path,
        skill_name: str,
        *,
        scope: TargetScope = "project_shared",
    ) -> Path | None:
        root = runtime_fanout_root("skills", "gemini", scope, project_root)
        return None if root is None else root / skill_name


@dataclass
class CodexSkillsGenerator:
    name: str = "codex_skills"
    # Codex CLI's primary project-scope skill path (also accepted by Gemini CLI
    # as an alias, which is why fanning out to all three runtimes creates a
    # slight amount of on-disk overlap — Gemini will silently de-dup it).
    output_root: str = ".agents/skills"

    def target_dir(
        self,
        project_root: Path,
        skill_name: str,
        *,
        scope: TargetScope = "project_shared",
    ) -> Path | None:
        root = runtime_fanout_root("skills", "codex", scope, project_root)
        return None if root is None else root / skill_name


_register(ClaudeSkillsGenerator())
_register(GeminiSkillsGenerator())
_register(CodexSkillsGenerator())


# ── Canonical helpers ─────────────────────────────────────────────────


def canonical_skills_root(project_root: Path) -> Path:
    return project_root / CANONICAL_SKILL_ROOT


def list_canonical_skills(project_root: Path) -> list[Path]:
    """Return canonical skill directories sorted by name.

    A sub-directory only counts as a skill if it contains ``SKILL.md``. This
    mirrors Gemini CLI's discovery rule and lets users drop auxiliary folders
    next to real skills without them being mistaken for skills.

    Skill directory names are validated; entries that fail
    :func:`memtomem.context._names.validate_name` are skipped with a warning.
    """
    root = canonical_skills_root(project_root)
    if not root.is_dir():
        return []
    skills: list[Path] = []
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and (entry / SKILL_MANIFEST).is_file():
            try:
                validate_name(entry.name, kind="skill name")
            except InvalidNameError as exc:
                logger.warning("skip canonical skill %r: invalid name (%s)", entry.name, exc)
                continue
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

    Individual files are written atomically via
    :func:`memtomem.context._atomic.atomic_write_bytes` so a crash mid-copy
    leaves each file either fully-written or absent, never truncated.
    Directory-level atomicity (the rmtree+mkdir window) is out of scope here.
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
    copy_tree_atomic(src, dst)


# ── Fan-out: canonical → runtimes ─────────────────────────────────────


@dataclass
class ExtractResult:
    """Result of a reverse (runtime → canonical) import."""

    imported: list[Path]
    # (item_name, human_reason, reason_code) — see :mod:`memtomem.context._skip_reasons`.
    skipped: list[tuple[str, str, skip_codes.SkipCode]] = field(default_factory=list)


@dataclass
class SkillSyncResult:
    generated: list[tuple[str, Path]]  # (runtime_name, target_path)
    # (runtime_name, human_reason, reason_code)
    skipped: list[tuple[str, str, skip_codes.SkipCode]]


def generate_all_skills(
    project_root: Path,
    runtimes: list[str] | None = None,
) -> SkillSyncResult:
    """Fan out every canonical skill to the requested runtime targets.

    Args:
        project_root: project root containing ``.memtomem/skills/``.
        runtimes: list of generator names. ``None`` means all registered
            runtimes (currently ``claude_skills`` + ``gemini_skills``).
    """
    generated: list[tuple[str, Path]] = []
    skipped: list[tuple[str, str, skip_codes.SkipCode]] = []

    canonicals = list_canonical_skills(project_root)
    if not canonicals:
        return SkillSyncResult(
            generated=generated,
            skipped=[("<all>", "no canonical skills", skip_codes.NO_CANONICAL_ROOT)],
        )

    targets = runtimes if runtimes is not None else list(SKILL_GENERATORS.keys())
    for target in targets:
        gen = SKILL_GENERATORS.get(target)
        if gen is None:
            skipped.append((target, "unknown runtime", skip_codes.UNKNOWN_RUNTIME))
            continue
        for skill_dir in canonicals:
            dst = gen.target_dir(project_root, skill_dir.name)
            # ADR-0011 PR-E: target_dir may return None for scopes with no
            # fan-out (default scope=project_shared never None here).
            assert dst is not None, (
                f"{target} target_dir returned None for default project_shared scope"
            )
            copy_skill(skill_dir, dst)
            # ADR-0008 Invariant 4: per-vendor override replaces SKILL.md only.
            # Auxiliary files (scripts/, references/) stay from canonical.
            vendor = GENERATOR_VENDOR.get(target)
            if vendor is not None:
                # ADR-0011 PR-E: pin scope=project_shared (see agents.py for
                # the same rationale — default sync must not see draft
                # project_local overrides).
                override_path = _override.resolve(
                    project_root, "skills", skill_dir.name, vendor, scope="project_shared"
                )
                if override_path is not None:
                    atomic_write_bytes(
                        dst / SKILL_MANIFEST,
                        override_path.read_bytes(),
                    )
            generated.append((target, dst))

    return SkillSyncResult(generated=generated, skipped=skipped)


# ── Reverse: runtimes → canonical ─────────────────────────────────────


def extract_skills_to_canonical(
    project_root: Path,
    overwrite: bool = False,
    only_name: str | None = None,
    *,
    scope: TargetScope = "project_shared",
    force_unsafe_import: bool = False,
) -> ExtractResult:
    """Import existing runtime skills into the scoped canonical directory.

    When the same skill name appears in multiple runtimes, the first one wins
    (deterministic order: claude → gemini → codex). Existing canonical
    entries are preserved unless ``overwrite=True``.

    ADR-0011 PR-E2: ``scope`` selects both the canonical destination
    (:func:`canonical_artifact_dir`) and the source runtime root
    (:func:`runtime_fanout_root` per scope — ``user`` reads
    ``~/.claude/skills`` etc.). ``project_local`` short-circuits to an
    empty result with ``NO_PROJECT_FANOUT_FOR_RUNTIME``.

    Gate A walks every file in the source skill tree (``rglob``) — secrets
    routinely live in ``scripts/*.py`` and ``references/*.md`` rather
    than just ``SKILL.md``. The skill is **atomic**: a single blocked
    file aborts that skill's import without copying any of its files.
    ``project_shared`` destinations hard-abort via :class:`click.ClickException`
    on the first hit (with or without ``force_unsafe_import``).

    Threat model — Gate A walks the source tree once and :func:`copy_skill`
    re-reads the same files when the import proceeds; an adversarial
    filesystem could swap bytes between the two reads (a TOCTOU window).
    The current threat model is "accidental leak", not "adversarial
    filesystem", so this gap is accepted: ``--force-unsafe-import`` is
    not the path to bypass Gate A regardless, and ``project_shared``
    hard-refuses without any bypass valve. Hardening to single-read +
    in-memory copy is out of scope until a concrete adversarial-FS
    threat appears.

    When ``only_name`` is set, every runtime entry with a different name is
    silently skipped before any validation/dedupe work.
    """
    if scope == "project_local":
        return ExtractResult(
            imported=[],
            skipped=[
                (
                    "<all>",
                    "project_local has no runtime fan-out (ADR-0011 §3)",
                    skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME,
                )
            ],
        )

    canonical_root = canonical_artifact_dir("skills", scope, project_root)
    imported: list[Path] = []
    skipped: list[tuple[str, str, skip_codes.SkipCode]] = []
    seen: dict[str, str] = {}  # skill_name → first runtime label

    for runtime in ("claude", "gemini", "codex"):
        try:
            runtime_dir = runtime_fanout_root("skills", runtime, scope, project_root)
        except KeyError:
            continue
        if runtime_dir is None or not runtime_dir.is_dir():
            continue
        runtime_label = f"{runtime} ({runtime_dir})"
        for skill_dir in sorted(runtime_dir.iterdir()):
            if not skill_dir.is_dir() or not (skill_dir / SKILL_MANIFEST).is_file():
                continue
            skill_name = skill_dir.name
            if only_name is not None and skill_name != only_name:
                continue
            try:
                validate_name(skill_name, kind="skill name")
            except InvalidNameError as exc:
                skipped.append((skill_name, f"invalid name: {exc}", skip_codes.INVALID_NAME))
                logger.warning("skip %r from %s: invalid name", skill_name, runtime_label)
                continue
            if skill_name in seen:
                reason = f"already imported from {seen[skill_name]}"
                skipped.append((skill_name, reason, skip_codes.ALREADY_IMPORTED))
                logger.warning("skip %s from %s: %s", skill_name, runtime_label, reason)
                continue
            dst = canonical_root / skill_name
            if dst.exists() and not overwrite:
                reason = "canonical exists (use --overwrite)"
                skipped.append((skill_name, reason, skip_codes.CANONICAL_EXISTS))
                logger.warning("skip %s from %s: %s", skill_name, runtime_label, reason)
                seen[skill_name] = runtime_label
                continue

            # Gate A — walk every file in the skill tree before copying.
            # One blocked file aborts the whole skill (atomic — never
            # leave a partial copy in canonical).
            blocked_file: Path | None = None
            blocked_decision: str | None = None
            blocked_hits: int = 0
            for src_file in sorted(skill_dir.rglob("*")):
                if not src_file.is_file():
                    continue
                try:
                    content_text = src_file.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    # Truly unreadable file — skip the scan; copy_skill
                    # will surface the OSError during the actual copy if
                    # it is real, otherwise the file passes through as
                    # a binary asset.
                    continue
                guard = privacy.enforce_write_guard(
                    content_text,
                    surface="cli_context_init",
                    force_unsafe=force_unsafe_import,
                    scope=scope,
                    audit_context={
                        "source_file": str(src_file),
                        "skill_name": skill_name,
                        "kind": "skills",
                    },
                    record_outcome=True,
                )
                if guard.decision in ("blocked", "blocked_project_shared"):
                    blocked_file = src_file
                    blocked_decision = guard.decision
                    blocked_hits = len(guard.hits)
                    break
                if guard.decision not in ("pass", "bypassed"):
                    raise RuntimeError(
                        f"enforce_write_guard returned unexpected decision: {guard.decision!r}"
                    )

            if blocked_file is not None:
                if scope == "project_shared":
                    raise click.ClickException(
                        format_project_shared_block_message(
                            blocked_file,
                            hits_count=blocked_hits,
                            scope=scope,
                            kind="skill",
                            imported_so_far=len(imported),
                        )
                    )
                code: skip_codes.SkipCode = (
                    skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED
                    if blocked_decision == "blocked_project_shared"
                    else skip_codes.PRIVACY_BLOCKED
                )
                hint = (
                    " — pass --force-unsafe-import to bypass"
                    if blocked_decision == "blocked"
                    else ""
                )
                skipped.append(
                    (
                        skill_name,
                        f"blocked: {blocked_file.name} hit {blocked_hits} pattern(s){hint}",
                        code,
                    )
                )
                seen[skill_name] = runtime_label
                continue

            # All files clean — copy the whole tree (atomic at directory level).
            dst.parent.mkdir(parents=True, exist_ok=True)
            copy_skill(skill_dir, dst)
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


def diff_skills(project_root: Path) -> list[tuple[str, str, str]]:
    """Compare canonical skills against every registered runtime.

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

    for gen_name, gen in SKILL_GENERATORS.items():
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
                assert dst is not None  # ADR-0011 PR-E: default scope=project_shared never None
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
