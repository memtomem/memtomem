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
import os
import secrets
import shutil
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context import override as _override
from memtomem.context._atomic import (
    _file_lock,
    _lock_path_for,
    atomic_write_bytes,
    copy_tree_atomic,
)
from memtomem.context._gate_a import GateABlocked, apply_gate_a
from memtomem.config import TargetScope
from memtomem.context._names import GENERATOR_VENDOR, InvalidNameError, validate_name
from memtomem.context._runtime_targets import runtime_artifact_names, runtime_fanout_root
from memtomem.context.privacy_scan import (
    raise_or_collect,
    scan_artifact_tree,
)
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


def canonical_skills_root(
    project_root: Path,
    *,
    scope: TargetScope = "project_shared",
) -> Path:
    """Return the canonical skills root for ``scope`` (default ``project_shared``).

    Pre-PR-E3 callers (no scope kwarg) keep the old behavior of
    ``<project_root>/.memtomem/skills`` because that's exactly
    :func:`canonical_artifact_dir` for ``scope="project_shared"``.
    """
    return canonical_artifact_dir("skills", scope, project_root)


def list_canonical_skills(
    project_root: Path,
    *,
    scope: TargetScope = "project_shared",
) -> list[Path]:
    """Return canonical skill directories sorted by name.

    A sub-directory only counts as a skill if it contains ``SKILL.md``. This
    mirrors Gemini CLI's discovery rule and lets users drop auxiliary folders
    next to real skills without them being mistaken for skills.

    Skill directory names are validated; entries that fail
    :func:`memtomem.context._names.validate_name` are skipped with a warning.

    ADR-0011 PR-E3: ``scope`` selects the canonical root via
    :func:`canonical_skills_root`.
    """
    root = canonical_skills_root(project_root, scope=scope)
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


def _stage_skill(src: Path, dst: Path) -> Path:
    """Mirror ``src`` into a same-fs staging directory under ``dst.parent``.

    Picks ``dst.parent / .staging-<dst.name>-<pid>-<rand>.tmp`` so the
    eventual promote-step (:func:`_promote_staging`) is a same-fs atomic
    rename via :func:`os.replace`. Caller is responsible for cleanup on
    failure (either by promoting into ``dst`` or by ``shutil.rmtree``-ing
    the staging path).

    ``src`` MUST contain ``SKILL.md``. ``dst.parent`` is created if it
    does not yet exist.
    """
    manifest = src / SKILL_MANIFEST
    if not manifest.is_file():
        raise FileNotFoundError(f"source skill missing {SKILL_MANIFEST}: {src}")

    dst.parent.mkdir(parents=True, exist_ok=True)
    suffix = f"{os.getpid()}-{secrets.token_hex(3)}"
    staging = dst.parent / f".staging-{dst.name}-{suffix}.tmp"
    if staging.exists():
        # Crashed prior run — collision is unlikely (pid+rand) but if it
        # happens, the leftover tree is from us; safe to remove.
        shutil.rmtree(staging)
    staging.mkdir()
    # Codex review fold: if ``copy_tree_atomic`` raises after partial
    # copy, the caller would never see ``staging`` (no return value),
    # leaving an unscanned partial tree under the runtime fan-out root.
    # Clean up here before re-raising so Gate A's staging-dir-first
    # contract holds even on copy failure.
    try:
        copy_tree_atomic(src, staging)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _promote_staging(staging: Path, dst: Path) -> None:
    """Atomic-replace ``dst`` with ``staging`` (same-fs precondition).

    Cross-platform via :func:`os.replace`. When ``dst`` already exists,
    moves it aside first then renames staging into place; rolls back on
    any failure during the swap window (``feedback_stage_before_mutation_revert.md``).
    """
    if dst.exists():
        if not dst.is_dir():
            raise NotADirectoryError(f"target exists and is not a directory: {dst}")
        if not (dst / SKILL_MANIFEST).is_file() and any(dst.iterdir()):
            raise IsADirectoryError(
                f"refusing to overwrite non-skill directory: {dst} "
                f"(add a SKILL.md or remove the directory first)"
            )
        # Move-aside name uses the same {pid}-{rand} discipline as staging
        # so concurrent runs (different pids) cannot collide.
        suffix = f"{os.getpid()}-{secrets.token_hex(3)}"
        old = dst.parent / f".old-{dst.name}-{suffix}.tmp"
        os.replace(dst, old)
        try:
            os.replace(staging, dst)
        except BaseException:
            # Roll back: put the original tree back.
            os.replace(old, dst)
            raise
        shutil.rmtree(old, ignore_errors=True)
    else:
        os.replace(staging, dst)


def copy_skill(src: Path, dst: Path) -> None:
    """Mirror a skill directory from ``src`` to ``dst`` via staging-then-promote.

    Thin wrapper kept for callers that don't care about the staging step
    (no privacy scan, no override merge — pure file copy). E3
    sync-side flow uses :func:`_stage_skill` + :func:`_promote_staging`
    directly so it can scan + override-apply between the two halves.

    Individual files are written atomically via
    :func:`memtomem.context._atomic.atomic_write_bytes`. Directory-level
    atomicity is now provided by the staging+promote pair.
    """
    staging = _stage_skill(src, dst)
    try:
        _promote_staging(staging, dst)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


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
    *,
    scope: TargetScope = "project_shared",
) -> SkillSyncResult:
    """Fan out every canonical skill to the requested runtime targets.

    Args:
        project_root: project root containing ``.memtomem/skills/``.
        runtimes: list of generator names. ``None`` means all registered
            runtimes (currently ``claude_skills`` + ``gemini_skills``).
        scope: ADR-0011 PR-E3 — selects canonical root and runtime
            fan-out destination. Default ``project_shared`` preserves
            pre-PR-E3 behavior.
    """
    generated: list[tuple[str, Path]] = []
    skipped: list[tuple[str, str, skip_codes.SkipCode]] = []

    canonicals = list_canonical_skills(project_root, scope=scope)
    if not canonicals:
        return SkillSyncResult(
            generated=generated,
            skipped=[("<all>", "no canonical skills", skip_codes.NO_CANONICAL_ROOT)],
        )

    targets = runtimes if runtimes is not None else list(SKILL_GENERATORS.keys())

    # ``project_shared`` is a hard-refusal surface: if any skill or
    # runtime override fails Gate A, no runtime fan-out should be
    # promoted. Hold all destination locks, stage+scan every final tree,
    # then promote only after the full batch passes.
    if scope == "project_shared":
        work: list[tuple[str, SkillGenerator, Path, Path]] = []
        for target in targets:
            gen = SKILL_GENERATORS.get(target)
            if gen is None:
                skipped.append((target, "unknown runtime", skip_codes.UNKNOWN_RUNTIME))
                continue
            for skill_dir in canonicals:
                dst = gen.target_dir(project_root, skill_dir.name, scope=scope)
                if dst is None:
                    skipped.append(
                        (
                            skill_dir.name,
                            f"no fan-out for runtime {target} at this scope",
                            skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME,
                        )
                    )
                    continue
                work.append((target, gen, skill_dir, dst))

        staged: list[tuple[str, Path, Path]] = []
        try:
            with ExitStack() as stack:
                for lock_path in sorted({_lock_path_for(dst) for _, _, _, dst in work}, key=str):
                    stack.enter_context(_file_lock(lock_path))
                for target, _gen, skill_dir, dst in work:
                    staging = _stage_skill(skill_dir, dst)
                    staged.append((target, staging, dst))

                    vendor = GENERATOR_VENDOR.get(target)
                    if vendor is not None:
                        override_path = _override.resolve(
                            project_root, "skills", skill_dir.name, vendor, scope=scope
                        )
                        if override_path is not None:
                            atomic_write_bytes(
                                staging / SKILL_MANIFEST,
                                override_path.read_bytes(),
                            )

                    scan = scan_artifact_tree(
                        staging,
                        surface="cli_context_sync",
                        scope=scope,
                        project_root=project_root,
                        on_blocked="fail_fast",
                    )
                    if scan.blocked:
                        raise_or_collect(
                            scan.blocked[0],
                            scope=scope,
                            kind="skill",
                            artifact_name=skill_dir.name,
                        )

                for target, staging, dst in staged:
                    _promote_staging(staging, dst)
                    generated.append((target, dst))
        finally:
            for _target, staging, _dst in staged:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)

        return SkillSyncResult(generated=generated, skipped=skipped)

    for target in targets:
        gen = SKILL_GENERATORS.get(target)
        if gen is None:
            skipped.append((target, "unknown runtime", skip_codes.UNKNOWN_RUNTIME))
            continue
        for skill_dir in canonicals:
            dst = gen.target_dir(project_root, skill_dir.name, scope=scope)
            # ADR-0011 PR-E (#891): None means NO_FANOUT per
            # ``_runtime_targets.RUNTIME_FANOUT_TABLE``. Emit a typed skip
            # so E3 scope wiring sees graceful behavior. The table is the
            # contract source-of-truth.
            if dst is None:
                skipped.append(
                    (
                        skill_dir.name,
                        f"no fan-out for runtime {target} at this scope",
                        skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME,
                    )
                )
                continue
            # ADR-0011 PR-E3 staging-dir-first scan flow:
            #   1. _stage_skill — mirror canonical bytes into a same-fs
            #      staging dir under dst.parent.
            #   2. Apply vendor SKILL.md override IF any (per-scope, single-
            #      tier lookup) — read override bytes ONCE and reuse them
            #      so the scan sees the exact bytes that get promoted.
            #      Auxiliary files (scripts/, references/, assets/) stay
            #      from canonical — preserves the
            #      ``test_override_only_touches_skill_md_not_scripts``
            #      invariant.
            #   3. scan_artifact_tree — privacy walk against the FINAL
            #      bytes (canonical + applied override). project_shared
            #      block raises ClickException; user/project_local block
            #      collects a skip.
            #   4. On pass — _promote_staging atomic-replaces dst with
            #      staging via os.replace (same-fs).
            #   5. On block or any exception — finally clause removes
            #      the staging tree without touching dst.
            #
            # Concurrency (PR-E3 Codex review fold): the entire
            # stage+scan+promote sequence runs inside a sidecar flock at
            # ``_lock_path_for(dst)`` so two parallel ``mm context sync``
            # invocations cannot interleave their dst→old→staging→dst
            # swaps. Without the lock, a second invocation could
            # recreate ``dst`` between the move-aside and the rename-in,
            # leaving the rollback path with no clean dst to restore.
            with _file_lock(_lock_path_for(dst)):
                staging = _stage_skill(skill_dir, dst)
                promoted = False
                try:
                    # 2. Override apply (BEFORE scan — scan must see the bytes
                    # that will be promoted).
                    vendor = GENERATOR_VENDOR.get(target)
                    if vendor is not None:
                        override_path = _override.resolve(
                            project_root, "skills", skill_dir.name, vendor, scope=scope
                        )
                        if override_path is not None:
                            atomic_write_bytes(
                                staging / SKILL_MANIFEST,
                                override_path.read_bytes(),
                            )
                    # 3. Scan.
                    scan = scan_artifact_tree(
                        staging,
                        surface="cli_context_sync",
                        scope=scope,
                        project_root=project_root,
                        on_blocked="fail_fast",
                    )
                    if scan.blocked:
                        # raise_or_collect raises ClickException for project_shared;
                        # otherwise returns (code, reason) and falls through to
                        # the skip append.
                        code, reason = raise_or_collect(
                            scan.blocked[0],
                            scope=scope,
                            kind="skill",
                            artifact_name=skill_dir.name,
                        )
                        skipped.append((skill_dir.name, reason, code))
                    else:
                        # 4. Promote — atomic os.replace into dst.
                        _promote_staging(staging, dst)
                        promoted = True
                        generated.append((target, dst))
                finally:
                    # 5. Cleanup. Promote consumes staging via rename, so we
                    # only remove it when something else (block/exception)
                    # left it behind.
                    if not promoted and staging.exists():
                        shutil.rmtree(staging, ignore_errors=True)

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
            # leave a partial copy in canonical). The project_shared
            # hard-abort path raises ClickException inside apply_gate_a;
            # the rglob loop only sees ``proceed=False`` for non-
            # project_shared scopes.
            blocked: tuple[Path, GateABlocked] | None = None
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
                outcome = apply_gate_a(
                    content_text=content_text,
                    src=src_file,
                    scope=scope,
                    force_unsafe_import=force_unsafe_import,
                    audit_context={
                        "source_file": str(src_file),
                        "skill_name": skill_name,
                        "kind": "skills",
                    },
                    message_kind="skill",
                    imported_so_far=len(imported),
                )
                if isinstance(outcome, GateABlocked):
                    blocked = (src_file, outcome)
                    break

            if blocked is not None:
                blocked_file, blocked_outcome = blocked
                skipped.append(
                    (
                        skill_name,
                        (
                            f"blocked: {blocked_file.name} hit "
                            f"{blocked_outcome.hits_count} pattern(s){blocked_outcome.hint}"
                        ),
                        blocked_outcome.code,
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


def diff_skills(
    project_root: Path,
    *,
    scope: TargetScope = "project_shared",
) -> list[tuple[str, str, str]]:
    """Compare canonical skills against every registered runtime.

    Returns a sorted list of ``(runtime, skill_name, status)`` tuples where
    status is one of:

    * ``"in sync"`` — content matches byte-for-byte.
    * ``"out of sync"`` — both sides exist but differ.
    * ``"missing target"`` — canonical has it, runtime does not.
    * ``"missing canonical"`` — runtime has it, canonical does not.

    ADR-0011 PR-E3: ``scope`` selects both the canonical root and the
    runtime fan-out roots (default ``project_shared``).
    """
    results: list[tuple[str, str, str]] = []
    canonical_root = canonical_skills_root(project_root, scope=scope)
    canonical_names = {p.name for p in list_canonical_skills(project_root, scope=scope)}

    for gen_name, gen in SKILL_GENERATORS.items():
        # ADR-0011 PR-E3 cleanup item #1: query the table directly via
        # ``runtime_fanout_root``. Earlier code probed with a fixed skill
        # name (``__probe_891__``) which leaked the table-shape assumption
        # into the call shape — call-shape fragility, not name-independence.
        runtime = gen_name.split("_", 1)[0]
        if runtime_fanout_root("skills", runtime, scope, project_root) is None:
            continue
        runtime_names = runtime_artifact_names(
            "skills", runtime, project_root, scope, dir_manifest=SKILL_MANIFEST
        )

        for name in sorted(canonical_names | runtime_names):
            if name in canonical_names and name not in runtime_names:
                results.append((gen_name, name, "missing target"))
            elif name in runtime_names and name not in canonical_names:
                results.append((gen_name, name, "missing canonical"))
            else:
                src = canonical_root / name
                # Cleanup item #2: the upstream ``runtime_fanout_root`` guard
                # above guarantees this runtime+scope has a fan-out root, so
                # ``gen.target_dir`` cannot return ``None`` for any name.
                # Earlier defensive ``if dst is None: continue`` removed.
                dst = gen.target_dir(project_root, name, scope=scope)
                assert dst is not None  # narrowed by upstream NO_FANOUT guard
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
