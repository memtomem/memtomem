"""Canonical ⇄ runtime skill directory fan-out.

Phase 1 of the "memtomem as canonical context gateway" plan. A skill lives at
``.memtomem/skills/<name>/SKILL.md`` (plus optional ``scripts/``, ``references/``,
``assets/`` sub-directories). From that single canonical source we fan out to
runtime-specific directories:

* Claude Code → ``.claude/skills/``
* Gemini CLI → ``.gemini/skills/``
* OpenAI Codex CLI → ``.agents/skills/``
* Kimi CLI → ``.kimi/skills/``

Anthropic released the Agent Skills spec as an open standard in 2025-12 and
OpenAI adopted the same SKILL.md format for Codex CLI, so the on-disk payload
is byte-identical across all four runtimes today. We still route everything
through a ``SkillGenerator`` registry so Phase 2+ can introduce per-runtime
frontmatter rewriting without touching callers.
"""

from __future__ import annotations

import errno
import logging
import os
import secrets
import shutil
import time
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context import override as _override
from memtomem.context._atomic import (
    COPY_SKIP_NAMES,
    _file_lock,
    _lock_path_for,
    atomic_write_bytes,
    copy_tree_atomic,
)
from memtomem.context._gate_a import GateABlocked, apply_gate_a
from memtomem.config import TargetScope
from memtomem.context._names import (
    GENERATOR_VENDOR,
    InvalidNameError,
    is_internal_artifact_dir,
    validate_name,
)
from memtomem.context._runtime_targets import (
    DiffRow,
    runtime_artifact_listing,
    runtime_fanout_root,
)
from memtomem.context.privacy_scan import (
    raise_or_collect,
    scan_artifact_tree,
)
from memtomem.context.scope_resolver import canonical_artifact_dir

logger = logging.getLogger(__name__)

CANONICAL_SKILL_ROOT = ".memtomem/skills"
SKILL_MANIFEST = "SKILL.md"
# Whole-call budget for destination sidecar-lock acquisition in
# :func:`generate_all_skills` — one shared deadline across every lock, not a
# fresh bound per destination (N dsts × per-lock bound could overrun the web
# handler's 60s ``asyncio.timeout`` and re-open the orphaned-worker window —
# the #1145 settings review shape; see ``settings._SETTINGS_LOCK_BUDGET_S``).
# The budget applies to EVERY caller (web, CLI, MCP) — matching the settings
# precedent: a CLI run that would otherwise block forever now aborts with a
# typed ``lock_timeout`` skip and a retry hint, and an ``asyncio.to_thread``
# web caller can never be wedged by a stuck cross-process lock holder.
_SKILLS_LOCK_BUDGET_S = 30.0
# Canonical-side subdirectory that holds per-vendor SKILL.md overrides
# (``<canonical>/<name>/overrides/<vendor>.<ext>`` — see
# :mod:`memtomem.context.override`). It is the SOURCE of overrides, never part
# of a runtime fan-out payload, so it is stripped from the staged tree before
# fan-out and excluded from diff comparison.
_OVERRIDES_DIRNAME = "overrides"


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


@dataclass
class KimiSkillsGenerator:
    name: str = "kimi_skills"
    output_root: str = ".kimi/skills"

    def target_dir(
        self,
        project_root: Path,
        skill_name: str,
        *,
        scope: TargetScope = "project_shared",
    ) -> Path | None:
        root = runtime_fanout_root("skills", "kimi", scope, project_root)
        return None if root is None else root / skill_name


_register(ClaudeSkillsGenerator())
_register(GeminiSkillsGenerator())
_register(CodexSkillsGenerator())
_register(KimiSkillsGenerator())


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
            if is_internal_artifact_dir(entry.name):
                # Crash-leftover staging/move-aside trees from our own sync
                # (#1229) — never list them as canonical skills, or the next
                # generate fans the junk out to every runtime.
                logger.debug("skip internal artifact dir %s", entry)
                continue
            try:
                validate_name(entry.name, kind="skill name")
            except InvalidNameError as exc:
                logger.warning("skip canonical skill %r: invalid name (%s)", entry.name, exc)
                continue
            skills.append(entry)
    return skills


# ── Copy primitive ────────────────────────────────────────────────────


def _stage_skill(src: Path, dst: Path, *, strip_overrides: bool = False) -> Path:
    """Mirror ``src`` into a same-fs staging directory under ``dst.parent``.

    Picks ``dst.parent / .staging-<dst.name>-<pid>-<rand>.tmp`` so the
    eventual promote-step (:func:`_promote_staging`) is a same-fs atomic
    rename via :func:`os.replace`. Caller is responsible for cleanup on
    failure (either by promoting into ``dst`` or by ``shutil.rmtree``-ing
    the staging path).

    ``src`` MUST contain ``SKILL.md``. ``dst.parent`` is created if it
    does not yet exist.

    ``strip_overrides`` removes the top-level ``overrides/`` directory from
    the staged tree. Runtime fan-out passes ``True`` so the canonical
    override SOURCE never lands in a runtime tree (which would leak every
    other vendor's override bytes into this vendor's tree, and let one
    vendor's override secret block the whole fan-out at scan time). Pure
    canonical→canonical / runtime→canonical copies keep the default
    (``False``) so the override source survives.
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
    # ``strip_overrides`` excludes the top-level ``overrides/`` source DURING
    # the copy (via ``skip_top_level``) so those bytes never reach the runtime
    # staging tree — no crash window where they exist, no silent leak if a
    # post-copy delete failed. The override itself is applied separately from
    # the canonical source via ``_override.resolve`` (which reads canonical,
    # not staging), so the skip never affects override application; and the
    # scan therefore never sees (and cannot block on) an unrelated vendor's
    # override.
    skip = frozenset({_OVERRIDES_DIRNAME}) if strip_overrides else None
    # Codex review fold: if ``copy_tree_atomic`` raises after partial
    # copy, the caller would never see ``staging`` (no return value),
    # leaving an unscanned partial tree under the runtime fan-out root.
    # Clean up here before re-raising so Gate A's staging-dir-first
    # contract holds even on copy failure.
    try:
        copy_tree_atomic(src, staging, skip_top_level=skip)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return staging


def _reap_stale_internal_dirs(dst: Path) -> None:
    """Remove crash-leftover staging/move-aside trees for ``dst``.

    A SIGKILL between :func:`_stage_skill` and the cleanup in
    :func:`_promote_staging` leaves ``.staging-<name>-*.tmp`` /
    ``.old-<name>-*.tmp`` trees behind that no later run reaps (the
    staging-exists check only matches the new pid+rand suffix) (#1229).

    Safe ONLY while holding ``_lock_path_for(dst)``: every gateway writer
    for the same destination holds that lock across its stage→promote
    sequence, so no live staging tree for this dst can exist while we do.
    Both the sync fan-out paths and the reverse-import path
    (:func:`extract_skills_to_canonical`, #1247 id 18) hold that lock, so
    runtime-side AND canonical-side leftovers get reaped.
    """
    parent = dst.parent
    if not parent.is_dir():
        return
    for pattern in (f".staging-{dst.name}-*.tmp", f".old-{dst.name}-*.tmp"):
        for stale in parent.glob(pattern):
            # The glob narrows by destination name; the shared predicate makes
            # the kill decision — a user skill named e.g.
            # ``.staging-<dst>-notes.tmp`` matches the glob but not the
            # pid+rand shape and must never be deleted (Codex review, #1229).
            if not is_internal_artifact_dir(stale.name):
                continue
            logger.debug("reaping stale internal artifact dir %s", stale)
            shutil.rmtree(stale, ignore_errors=True)


def _target_conflict(dst: Path) -> OSError | None:
    """Why :func:`_promote_staging` would refuse to replace ``dst``, or ``None``.

    Single source of truth for the refusal predicate, shared by the promote
    itself and the sync/import preflights that convert the refusal into a
    typed ``TARGET_CONFLICT`` skip — so the preflights can never drift from
    what the promote actually enforces (#1229). An existing but EMPTY
    non-skill directory is not a conflict (the promote replaces it).
    """
    if not dst.exists():
        return None
    if not dst.is_dir():
        return NotADirectoryError(f"target exists and is not a directory: {dst}")
    if not (dst / SKILL_MANIFEST).is_file() and any(dst.iterdir()):
        return IsADirectoryError(
            f"refusing to overwrite non-skill directory: {dst} "
            f"(add a SKILL.md or remove the directory first)"
        )
    return None


def _promote_race_conflict(exc: OSError) -> bool:
    """Whether a :func:`_promote_staging` ``OSError`` is a destination race.

    ``True`` only for the shapes a NON-gateway writer (manual shell, editor)
    can produce by landing content at ``dst`` mid-swap — the
    :func:`_target_conflict` refusal pair, and ENOTEMPTY/EEXIST from the
    rename-in hitting a recreated destination. Callers convert those into a
    typed ``target_conflict`` skip and keep going.

    Everything else stays ``False`` so it RE-RAISES: ENOSPC, permission
    errors, and — critically — the rollback-failure chain from #1123
    (``raise promote_exc from rollback_exc``), where the only surviving copy
    of the original tree is stranded in ``.old-*``. A non-``None``
    ``__cause__`` is that chain's marker (promote errors are otherwise raised
    bare), and demoting it to a skip would bury the operator breadcrumb.
    """
    if exc.__cause__ is not None:
        return False
    if isinstance(exc, (IsADirectoryError, NotADirectoryError)):
        return True
    return exc.errno in (errno.ENOTEMPTY, errno.EEXIST)


def _promote_staging(staging: Path, dst: Path) -> None:
    """Atomic-replace ``dst`` with ``staging`` (same-fs precondition).

    Cross-platform via :func:`os.replace`. When ``dst`` already exists,
    moves it aside first then renames staging into place; rolls back on
    any failure during the swap window (``feedback_stage_before_mutation_revert.md``).
    Raises the :func:`_target_conflict` refusal (``NotADirectoryError`` /
    ``IsADirectoryError``) when ``dst`` holds non-skill content.
    """
    conflict = _target_conflict(dst)
    if conflict is not None:
        raise conflict
    if dst.exists():
        # Move-aside name uses the same {pid}-{rand} discipline as staging
        # so concurrent runs (different pids) cannot collide.
        suffix = f"{os.getpid()}-{secrets.token_hex(3)}"
        old = dst.parent / f".old-{dst.name}-{suffix}.tmp"
        os.replace(dst, old)
        try:
            os.replace(staging, dst)
        except BaseException as promote_exc:
            # Roll back: put the original tree back. If the rollback rename
            # ITSELF fails (e.g. ``dst`` was recreated by a racing writer, or an
            # FS error), do not let it mask the original promotion error and do
            # not leave ``old`` — the only surviving copy of the original tree —
            # orphaned without a trace. Log a breadcrumb naming ``old`` and
            # ``dst`` so an operator can recover the tree manually, then re-raise
            # the ORIGINAL error with the rollback failure chained (#1123 B3-4).
            # The ``from`` chain is load-bearing: ``_promote_race_conflict``
            # reads ``__cause__`` to refuse demoting THIS state to a skip.
            try:
                os.replace(old, dst)
            except BaseException as rollback_exc:
                logger.error(
                    "skill promote rollback failed: %s is now missing; the "
                    "original tree is preserved at %s — restore it manually",
                    dst,
                    old,
                )
                raise promote_exc from rollback_exc
            raise
        shutil.rmtree(old, ignore_errors=True)
    else:
        os.replace(staging, dst)


def copy_skill(src: Path, dst: Path) -> None:
    """Mirror a skill directory from ``src`` to ``dst`` via staging-then-promote.

    Thin public wrapper (``__all__``) kept for external callers that don't
    care about the staging step (no privacy scan, no override merge, no
    destination lock — pure file copy). The gateway's own flows use
    :func:`_stage_skill` + :func:`_promote_staging` directly: sync scans +
    override-applies between the two halves, and the reverse import
    (#1247 id 18) converts each half's failure into its own typed skip
    while holding the destination sidecar lock.

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
    surface: str = "cli_context_sync",
    force_unsafe: bool = False,
) -> SkillSyncResult:
    """Fan out every canonical skill to the requested runtime targets.

    Args:
        project_root: project root containing ``.memtomem/skills/``.
        runtimes: list of generator names. ``None`` means all registered
            runtimes (currently ``claude_skills`` + ``gemini_skills``).
        scope: ADR-0011 PR-E3 — selects canonical root and runtime
            fan-out destination. Default ``project_shared`` preserves
            pre-PR-E3 behavior.
        force_unsafe: Reviewed Gate A bypass (ADR-0011 §5) threaded to
            both :func:`scan_artifact_tree` sites. ``True`` lets a
            reviewed false positive (e.g. an ``api_key: str`` type
            annotation in a skill doc) fan out to ``user`` /
            ``project_local`` destinations; ``project_shared`` stays
            hard-refused regardless (the engine's Gate A is
            authoritative — same contract as ``--force-unsafe-import``).
            Default ``False``.
        surface: Audit identifier forwarded verbatim to
            :func:`privacy.enforce_write_guard` via both
            :func:`scan_artifact_tree` sites (the project_shared batch
            and the per-destination path) — it dimensions the privacy
            ``record()`` counter and tags the blocked/bypassed audit
            log line. Callers pass their own literal: the CLI relies on
            the default ``"cli_context_sync"``, the Web sync route
            passes ``"web_context_skills_sync"``, and the MCP tools
            pass ``"mcp_context_generate"`` / ``"mcp_context_sync"``
            (#1246 — previously every surface was misattributed to the
            CLI literal; sibling of the import-side #1242 fix).
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

    # One shared deadline for ALL destination sidecar-lock waits — the whole
    # call, not each destination, is bounded by ``_SKILLS_LOCK_BUDGET_S``.
    # A timed-out acquisition becomes a typed ``lock_timeout`` skip instead
    # of blocking forever, so a thread-offloaded web caller can never be
    # wedged (or orphaned past its own timeout) by a stuck cross-process
    # holder (#1145 shape).
    lock_deadline = time.monotonic() + _SKILLS_LOCK_BUDGET_S

    def _lock_timeout() -> float:
        return max(0.0, lock_deadline - time.monotonic())

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
                # Locks are acquired before any staging work, so a budget
                # overrun here aborts the batch with nothing to roll back —
                # all-or-nothing is preserved (this scope is the hard-refusal
                # surface; promoting a partial batch is never acceptable).
                try:
                    for lock_path in sorted(
                        {_lock_path_for(dst) for _, _, _, dst in work}, key=str
                    ):
                        stack.enter_context(_file_lock(lock_path, timeout=_lock_timeout()))
                except TimeoutError:
                    skipped.append(
                        (
                            "<all>",
                            "another process held a destination lock past the "
                            f"{_SKILLS_LOCK_BUDGET_S:g}s acquisition budget — "
                            "re-run sync to retry",
                            skip_codes.LOCK_TIMEOUT,
                        )
                    )
                    return SkillSyncResult(generated=generated, skipped=skipped)
                # All destination locks held — safe point to reap crash
                # leftovers before they collide with fresh staging work.
                for stale_dst in sorted({dst for _, _, _, dst in work}, key=str):
                    _reap_stale_internal_dirs(stale_dst)
                for target, _gen, skill_dir, dst in work:
                    # Preflight the promote refusal predicate while the
                    # destination locks are held, BEFORE anything stages: a
                    # pre-existing non-skill dst would otherwise make the
                    # promote loop below raise mid-batch AFTER earlier
                    # destinations were already promoted — an uncaught crash
                    # AND a broken all-or-nothing contract (#1229). Typed
                    # per-destination skip; the rest of the batch proceeds
                    # (same isolation as the PARSE_ERROR skips below).
                    conflict = _target_conflict(dst)
                    if conflict is not None:
                        skipped.append((skill_dir.name, str(conflict), skip_codes.TARGET_CONFLICT))
                        continue
                    # Unreadable canonical: typed PARSE_ERROR skip rather than
                    # an exception bubbling up — symmetric with agents.py /
                    # commands.py read_bytes failure handling. Privacy block
                    # is the only failure that still aborts the batch.
                    try:
                        staging = _stage_skill(skill_dir, dst, strip_overrides=True)
                    except OSError as exc:
                        skipped.append(
                            (skill_dir.name, f"unreadable: {exc}", skip_codes.PARSE_ERROR)
                        )
                        continue
                    staged.append((target, staging, dst))

                    vendor = GENERATOR_VENDOR.get(target)
                    if vendor is not None:
                        override_path = _override.resolve(
                            project_root, "skills", skill_dir.name, vendor, scope=scope
                        )
                        if override_path is not None:
                            try:
                                override_bytes = override_path.read_bytes()
                            except OSError as exc:
                                skipped.append(
                                    (
                                        skill_dir.name,
                                        f"override unreadable: {exc}",
                                        skip_codes.PARSE_ERROR,
                                    )
                                )
                                # Drop this pair from the promote queue and
                                # clean its orphaned staging tree. The pop
                                # targets the entry we just appended.
                                staged.pop()
                                shutil.rmtree(staging, ignore_errors=True)
                                continue
                            atomic_write_bytes(staging / SKILL_MANIFEST, override_bytes)

                    scan = scan_artifact_tree(
                        staging,
                        surface=surface,
                        scope=scope,
                        project_root=project_root,
                        on_blocked="fail_fast",
                        force_unsafe=force_unsafe,
                    )
                    if scan.blocked:
                        raise_or_collect(
                            scan.blocked[0],
                            scope=scope,
                            kind="skill",
                            artifact_name=skill_dir.name,
                        )

                for target, staging, dst in staged:
                    try:
                        _promote_staging(staging, dst)
                    except OSError as exc:
                        # Residual race: only a NON-gateway writer (manual
                        # shell, editor) can recreate conflicting content at
                        # dst after the preflight above — the sidecar lock
                        # held since before staging serializes every gateway
                        # writer. Loud typed skip for the verified race
                        # shapes (refusal pair + ENOTEMPTY/EEXIST, #1247
                        # id 18); the remaining destinations still promote
                        # (the finally below reaps the unconsumed staging
                        # tree). Anything else — ENOSPC, permissions, the
                        # #1123 rollback-failure chain — re-raises loud.
                        if not _promote_race_conflict(exc):
                            raise
                        skipped.append((dst.name, str(exc), skip_codes.TARGET_CONFLICT))
                        continue
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
            # Acquisition is bounded by the shared call budget; a timed-out
            # destination becomes a typed per-item skip (the other
            # destinations still proceed — non-shared scopes have no
            # all-or-nothing batch contract).
            dst_lock = ExitStack()
            try:
                dst_lock.enter_context(_file_lock(_lock_path_for(dst), timeout=_lock_timeout()))
            except TimeoutError:
                skipped.append(
                    (
                        skill_dir.name,
                        "another process held the destination lock past the "
                        f"{_SKILLS_LOCK_BUDGET_S:g}s acquisition budget — "
                        "re-run sync to retry",
                        skip_codes.LOCK_TIMEOUT,
                    )
                )
                continue
            with dst_lock:
                # Lock held — safe point to reap crash leftovers for this dst.
                _reap_stale_internal_dirs(dst)
                # Preflight the promote refusal predicate (same conversion to
                # a typed skip as the project_shared batch above, #1229) —
                # before staging so a conflicted destination wastes no
                # stage+scan work.
                conflict = _target_conflict(dst)
                if conflict is not None:
                    skipped.append((skill_dir.name, str(conflict), skip_codes.TARGET_CONFLICT))
                    continue
                # Unreadable canonical: typed PARSE_ERROR skip rather than
                # an exception bubbling up — symmetric with agents.py /
                # commands.py read_bytes failure handling.
                try:
                    staging = _stage_skill(skill_dir, dst, strip_overrides=True)
                except OSError as exc:
                    skipped.append((skill_dir.name, f"unreadable: {exc}", skip_codes.PARSE_ERROR))
                    continue
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
                            try:
                                override_bytes = override_path.read_bytes()
                            except OSError as exc:
                                skipped.append(
                                    (
                                        skill_dir.name,
                                        f"override unreadable: {exc}",
                                        skip_codes.PARSE_ERROR,
                                    )
                                )
                                # promoted stays False; finally clause rmtrees
                                # the staging tree.
                                continue
                            atomic_write_bytes(staging / SKILL_MANIFEST, override_bytes)
                    # 3. Scan.
                    scan = scan_artifact_tree(
                        staging,
                        surface=surface,
                        scope=scope,
                        project_root=project_root,
                        on_blocked="fail_fast",
                        force_unsafe=force_unsafe,
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
                        # 4. Promote — atomic os.replace into dst. The
                        # conflict refusal (or a recreated-destination
                        # ENOTEMPTY/EEXIST) can still fire here if a
                        # NON-gateway writer recreated content at dst after
                        # the preflight (the held sidecar lock serializes
                        # gateway writers only) — same typed-skip conversion.
                        # Non-race OSErrors re-raise loud (#1247 id 18).
                        try:
                            _promote_staging(staging, dst)
                        except OSError as exc:
                            if not _promote_race_conflict(exc):
                                raise
                            skipped.append((skill_dir.name, str(exc), skip_codes.TARGET_CONFLICT))
                        else:
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
    source_scope: TargetScope | None = None,
    force_unsafe_import: bool = False,
    dry_run: bool = False,
    surface: str = "cli_context_init",
) -> ExtractResult:
    """Import existing runtime skills into the scoped canonical directory.

    When the same skill name appears in multiple runtimes, the first one wins
    (deterministic order: claude → gemini → codex → kimi). Existing canonical
    entries are preserved unless ``overwrite=True``.

    ``dry_run`` (rank-10 import preview) runs the full scan + name validation
    + Gate A privacy walk + cross-runtime dedup + canonical-exists check, then
    **skips only the directory copy**: the returned ``imported`` lists the
    destinations that *would* be written and ``skipped`` carries the same
    reasons a real run would, but nothing touches disk — including the
    destination sidecar lockfile, which only a real run creates. The skip
    decisions are identical to a real run because both evaluate
    ``dst.exists()`` before any write, so the preview is accurate (modulo
    the documented TOCTOU window).

    Concurrency (#1247 id 18): the write phase (reap → re-check → stage →
    promote) runs inside the same per-destination sidecar flock the sync
    paths hold (``_lock_path_for(dst)``), so parallel gateway writers cannot
    interleave their ``dst → .old-* → staging → dst`` swaps and strand the
    canonical tree in ``.old-*``. The Gate A scan stays OUTSIDE the lock
    (it reads only the source tree); acquisition is bounded by one
    whole-call ``_SKILLS_LOCK_BUDGET_S`` budget and a timed-out destination
    becomes a typed ``lock_timeout`` skip. Only non-gateway writers (manual
    shell, editor) can still race the promote — those surface as typed
    ``target_conflict`` skips for the verified race shapes and re-raise
    loud otherwise.

    ADR-0011 PR-E2: ``scope`` selects both the canonical destination
    (:func:`canonical_artifact_dir`) and the source runtime root
    (:func:`runtime_fanout_root` per scope — ``user`` reads
    ``~/.claude/skills`` etc.). ``project_local`` short-circuits to an
    empty result with ``NO_PROJECT_FANOUT_FOR_RUNTIME``.

    ``source_scope`` decouples the SOURCE runtime root from the destination
    when set (default ``None`` keeps them coupled — the historical
    behavior). It exists for one sanctioned cross-tier flow: importing a
    *project* runtime skill (``source_scope="project_shared"`` →
    ``<project>/.claude/skills``) into the *user* library
    (``scope="user"`` → ``~/.memtomem/skills``), the only web path for a
    project-runtime skill that trips Gate A's false-positive secret
    heuristic (``project_shared`` dest is hard-blocked with no bypass;
    ``user`` dest is force-bypassable). The Gate A block decision keys off
    the DESTINATION ``scope`` (so a ``user`` dest stays force-bypassable),
    never ``source_scope``.

    Gate A walks every file in the source skill tree (``rglob``) — secrets
    routinely live in ``scripts/*.py`` and ``references/*.md`` rather
    than just ``SKILL.md``. The skill is **atomic**: a single blocked
    file aborts that skill's import without copying any of its files.
    ``project_shared`` destinations hard-abort via :class:`click.ClickException`
    on the first hit (with or without ``force_unsafe_import``).

    Threat model — Gate A walks the source tree once and :func:`_stage_skill`
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
    # Source runtime root scope — decoupled from the destination only when the
    # caller asks (default keeps them equal, the historical coupling). See the
    # ``source_scope`` docstring for the one sanctioned project→user flow.
    source_scope_eff: TargetScope = source_scope if source_scope is not None else scope
    imported: list[Path] = []
    skipped: list[tuple[str, str, skip_codes.SkipCode]] = []
    seen: dict[str, str] = {}  # skill_name → first runtime label

    # One shared deadline for ALL destination sidecar-lock waits — the whole
    # call, not each destination, is bounded by ``_SKILLS_LOCK_BUDGET_S``
    # (mirror of ``generate_all_skills``; #1145 shape — a thread-offloaded
    # web/MCP caller must never be wedged by a stuck cross-process holder).
    lock_deadline = time.monotonic() + _SKILLS_LOCK_BUDGET_S

    def _lock_timeout() -> float:
        return max(0.0, lock_deadline - time.monotonic())

    for runtime in ("claude", "gemini", "codex", "kimi"):
        try:
            runtime_dir = runtime_fanout_root("skills", runtime, source_scope_eff, project_root)
        except KeyError:
            continue
        if runtime_dir is None or not runtime_dir.is_dir():
            continue
        runtime_label = f"{runtime} ({runtime_dir})"
        for skill_dir in sorted(runtime_dir.iterdir()):
            if not skill_dir.is_dir() or not (skill_dir / SKILL_MANIFEST).is_file():
                continue
            if is_internal_artifact_dir(skill_dir.name):
                # Our own crash-leftover staging/move-aside trees — never
                # import them as skills.
                logger.debug("skip internal artifact dir %s", skill_dir)
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
            # ``--overwrite`` onto a canonical dst holding non-skill content
            # (or a plain file) would make copy_skill's promote raise
            # mid-import — typed skip instead (#1229). Checked for dry-run
            # too, so the preview's skip decisions match the real run.
            conflict = _target_conflict(dst)
            if conflict is not None:
                skipped.append((skill_name, str(conflict), skip_codes.TARGET_CONFLICT))
                logger.warning("skip %s from %s: %s", skill_name, runtime_label, conflict)
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
                    # Truly unreadable file — skip the scan; _stage_skill
                    # will surface the OSError during the actual copy as a
                    # typed ``parse_error`` skip if it is real, otherwise
                    # the file passes through as a binary asset.
                    continue
                outcome = apply_gate_a(
                    content_text=content_text,
                    src=src_file,
                    scope=scope,
                    force_unsafe_import=force_unsafe_import,
                    surface=surface,
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
            # ``dry_run`` records the would-import destination but skips the
            # mkdir + lock + copy so the preview never mutates disk (rank-10).
            if not dry_run:
                dst.parent.mkdir(parents=True, exist_ok=True)
                # Stage→promote runs inside the destination sidecar lock —
                # mirror of the sync paths (#1247 id 18). Without it, two
                # parallel importers interleave their dst→.old-*→staging→dst
                # swaps and a racing promote can strand the only copy of the
                # canonical tree in ``.old-*``.
                dst_lock = ExitStack()
                try:
                    dst_lock.enter_context(_file_lock(_lock_path_for(dst), timeout=_lock_timeout()))
                except TimeoutError:
                    reason = (
                        "another process held the canonical destination lock "
                        f"past the {_SKILLS_LOCK_BUDGET_S:g}s acquisition "
                        "budget — re-run the import to retry"
                    )
                    # No ``seen`` mark: contention is transient and
                    # destination-lock-specific, so a later runtime's copy of
                    # the same name keeps its fallback chance (and, with the
                    # per-call budget exhausted, fails fast into this same
                    # skip rather than blocking).
                    skipped.append((skill_name, reason, skip_codes.LOCK_TIMEOUT))
                    logger.warning("skip %s from %s: %s", skill_name, runtime_label, reason)
                    continue
                with dst_lock:
                    # Lock held — safe point to reap crash leftovers for this
                    # canonical dst (previously unreachable for the import
                    # path, which relied on discovery filtering alone).
                    _reap_stale_internal_dirs(dst)
                    # Re-check the no-overwrite contract under the lock: a
                    # parallel importer can land dst between the lock-free
                    # preflight above and our acquisition, and replacing its
                    # fresh import would violate ``overwrite=False``.
                    if dst.exists() and not overwrite:
                        reason = "canonical exists (use --overwrite)"
                        skipped.append((skill_name, reason, skip_codes.CANONICAL_EXISTS))
                        logger.warning("skip %s from %s: %s", skill_name, runtime_label, reason)
                        seen[skill_name] = runtime_label
                        continue
                    # Inlined ``_stage_skill`` + ``_promote_staging`` (rather
                    # than ``copy_skill``) so each half converts to its own
                    # typed skip — sync-path parity. No ``seen`` mark on a
                    # stage failure: unreadability is source-runtime-specific
                    # (agents/commands parity), so a later runtime's clean
                    # copy of the same name still imports.
                    try:
                        staging = _stage_skill(skill_dir, dst)
                    except OSError as exc:
                        skipped.append((skill_name, f"unreadable: {exc}", skip_codes.PARSE_ERROR))
                        logger.warning("skip %s from %s: %s", skill_name, runtime_label, exc)
                        continue
                    try:
                        _promote_staging(staging, dst)
                    except OSError as exc:
                        # Verified destination races (refusal pair +
                        # ENOTEMPTY/EEXIST from a NON-gateway writer — the
                        # held lock serializes gateway writers) become typed
                        # skips; anything else, including the #1123
                        # rollback-failure chain, re-raises loud.
                        shutil.rmtree(staging, ignore_errors=True)
                        if not _promote_race_conflict(exc):
                            raise
                        skipped.append((skill_name, str(exc), skip_codes.TARGET_CONFLICT))
                        logger.warning("skip %s from %s: %s", skill_name, runtime_label, exc)
                        seen[skill_name] = runtime_label
                        continue
                    except BaseException:
                        # Non-OSError escape (KeyboardInterrupt, …): keep
                        # ``copy_skill``'s staging hygiene before propagating.
                        shutil.rmtree(staging, ignore_errors=True)
                        raise
            imported.append(dst)
            seen[skill_name] = runtime_label

    return ExtractResult(imported=imported, skipped=skipped)


# ── Diff: canonical ↔ runtimes ────────────────────────────────────────


def _skill_effective_equal(
    canonical: Path,
    runtime: Path,
    override_bytes: bytes | None,
    *,
    top_level: bool = True,
) -> bool:
    """Whether ``runtime`` equals the tree ``generate_all_skills`` produces.

    Must mirror :func:`copy_tree_atomic` exactly, else a skill that synced
    perfectly reports false drift:

    * :data:`COPY_SKIP_NAMES` (``.git`` / ``.DS_Store`` / ``__pycache__``) and
      symlinks are excluded at EVERY depth — sync never copies them, so they
      are ignored on both sides (a stray cache on either side is not drift).
    * the top-level ``overrides/`` SOURCE directory is excluded from the
      canonical side only (a leaked ``overrides/`` on the runtime side IS drift,
      so re-sync is prompted to clean it).
    * the top-level ``SKILL.md`` is replaced by ``override_bytes`` when a
      per-vendor override exists; everything else is byte-compared verbatim.

    Comparing the raw canonical directory instead (as the previous code did)
    reported any override-carrying skill as permanently "out of sync".
    """
    if not (canonical.is_dir() and runtime.is_dir()):
        return False

    def _entries(d: Path, *, is_canonical: bool) -> list[str]:
        names = []
        for p in d.iterdir():
            if p.name in COPY_SKIP_NAMES or p.is_symlink():
                continue
            if top_level and is_canonical and p.name == _OVERRIDES_DIRNAME:
                continue
            names.append(p.name)
        return sorted(names)

    can_entries = _entries(canonical, is_canonical=True)
    if can_entries != _entries(runtime, is_canonical=False):
        return False
    for name in can_entries:
        cp, rp = canonical / name, runtime / name
        if cp.is_file() and rp.is_file():
            if top_level and name == SKILL_MANIFEST and override_bytes is not None:
                expected = override_bytes
            else:
                expected = cp.read_bytes()
            if expected != rp.read_bytes():
                return False
        elif cp.is_dir() and rp.is_dir():
            # Aux subtrees (scripts/, references/, assets/) compare verbatim;
            # only the top-level SKILL.md / overrides/ get special handling.
            if not _skill_effective_equal(cp, rp, None, top_level=False):
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
    * ``"invalid name"`` — a skill-shaped directory exists (either side)
      whose name fails :func:`memtomem.context._names.validate_name`;
      sync/extract will never touch it.

    ADR-0011 PR-E3: ``scope`` selects both the canonical root and the
    runtime fan-out roots (default ``project_shared``).
    """
    results: list[tuple[str, str, str]] = []
    canonical_root = canonical_skills_root(project_root, scope=scope)
    canonical_names = {p.name for p in list_canonical_skills(project_root, scope=scope)}
    # Canonical-side invalid names: list_canonical_skills filters them out
    # for SYNC (generate must never fan out an invalid dir), which made them
    # fully invisible — no diff row anywhere (#1229). Enumerate them once
    # here for the dedicated "invalid name" status.
    invalid_canonical_names: list[tuple[str, str]] = []
    if canonical_root.is_dir():
        for entry in sorted(canonical_root.iterdir()):
            if not entry.is_dir() or not (entry / SKILL_MANIFEST).is_file():
                continue
            if is_internal_artifact_dir(entry.name):
                continue
            try:
                validate_name(entry.name, kind="skill name")
            except InvalidNameError as exc:
                invalid_canonical_names.append((entry.name, str(exc)))

    for gen_name, gen in SKILL_GENERATORS.items():
        # ADR-0011 PR-E3 cleanup item #1: query the table directly via
        # ``runtime_fanout_root``. Earlier code probed with a fixed skill
        # name (``__probe_891__``) which leaked the table-shape assumption
        # into the call shape — call-shape fragility, not name-independence.
        runtime = gen_name.split("_", 1)[0]
        if runtime_fanout_root("skills", runtime, scope, project_root) is None:
            continue
        runtime_names, invalid_runtime_names = runtime_artifact_listing(
            "skills", runtime, project_root, scope, dir_manifest=SKILL_MANIFEST
        )
        # One "invalid name" row per (runtime, name) — runtime-side entries
        # that failed validation plus the canonical-side rejects above
        # (#1229; deduplicated when the same invalid name exists on both
        # sides).
        invalid_by_name = dict(invalid_canonical_names)
        invalid_by_name.update(dict(invalid_runtime_names))
        for raw_name in sorted(invalid_by_name):
            results.append(DiffRow(gen_name, raw_name, "invalid name", invalid_by_name[raw_name]))

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
                # Resolve the per-vendor override the sync path applies so the
                # comparison reflects the effective fan-out tree, not the raw
                # canonical (which would always report override skills as drift).
                override_bytes: bytes | None = None
                vendor = GENERATOR_VENDOR.get(gen_name)
                if vendor is not None:
                    override_path = _override.resolve(
                        project_root, "skills", name, vendor, scope=scope
                    )
                    if override_path is not None:
                        try:
                            override_bytes = override_path.read_bytes()
                        except OSError:
                            # Sync skips an unreadable override (typed PARSE_ERROR,
                            # no effective fan-out), so we cannot claim parity.
                            # Report drift rather than comparing against the
                            # un-overridden canonical (which could mask it).
                            results.append((gen_name, name, "out of sync"))
                            continue
                # An unreadable file inside either tree (PermissionError etc.)
                # must not abort the whole diff — we can't assert parity, so
                # report drift, never mask it (same contract as the override
                # read above; #1229).
                try:
                    equal = _skill_effective_equal(src, dst, override_bytes)
                except OSError:
                    equal = False
                if equal:
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
    "KimiSkillsGenerator",
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
