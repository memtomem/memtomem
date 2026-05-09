"""Duplicate-tier hook detection (ADR-0010 §4).

Claude Code 2.x merges hook entries from all three settings tiers
(user / project_shared / project_local) additively, so a memtomem-
managed hook duplicated across tiers fires once per tier — silent
double-execution.

This module provides the shared detection used by both surfaces
mandated by ADR-0010 §4:

* Sync-time warning in ``mm context sync --include=settings`` and the
  Web UI hooks panel — runs before write so the user sees the
  duplicate state in their actual workflow.
* Scoped on-demand check via ``mm context settings-doctor`` — same
  logic, callable from CI / scripting.

Detection compares **canonical signatures** (event + normalized
matcher + normalized command) rather than literal equality so a
user's whitespace-variant of a memtomem-authored entry still matches.
A tier counts as a duplicate when it holds a hook entry whose
signature appears in the project's canonical ``.memtomem/settings.json``
**and** the tier is not the active scope.

This module is detection-only. Migration of duplicate entries lives
in :mod:`memtomem.context.settings_migrate`.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    resolve_scope_path,
)

logger = logging.getLogger(__name__)

# All three tier scopes. Listed in the order users typically reason
# about them (user is the v1 default, project tiers are the team-shared
# alternatives) so doctor output is stable.
ALL_SCOPES: tuple[str, ...] = ("user", "project_shared", "project_local")

_WHITESPACE_RUN = re.compile(r"\s+")


@dataclass(frozen=True)
class HookSignature:
    """Normalized identity of a single ``(event, matcher, command)`` hook.

    Two signatures compare equal when the canonical signatures match
    after whitespace normalization — enough to defeat the variants
    ADR-0010 §4 names ("robust to whitespace / matcher variants")
    without going so far as to shlex-tokenize, which would normalize
    away differences a user intentionally introduced (``--top-k 3`` vs
    ``--top-k=3``) and choke on bash one-liners with ``||`` / ``2>>``.
    """

    event: str
    matcher: str
    command_shape: str


@dataclass(frozen=True)
class DuplicateTier:
    """One non-active tier holding canonical-matched hook signatures."""

    tier: str
    path: Path
    entries: tuple[HookSignature, ...]


def _load_settings_dict(path: Path) -> dict | None:
    """Read a settings.json file; return ``None`` on any read failure.

    Self-contained JSON load so this module doesn't depend on
    :func:`memtomem.context.settings._safe_load_json` /
    :data:`memtomem.context.settings._MALFORMED` (private to that
    module). Returns ``None`` when the file is missing, unreadable, or
    not valid JSON, **or** when the parsed root is not a dict —
    callers can treat all three the same way (skip this tier).
    """
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


def _normalize_matcher(value: object) -> str:
    """Strip whitespace + unify missing/empty matcher strings.

    The matcher is a regex source consumed by Claude Code; alpha-sort
    of alternations is **not** applied because ``Edit|Write`` and
    ``Write|Edit`` are not interchangeable from Claude Code's side.
    """
    if not isinstance(value, str):
        return ""
    return value.strip()


def _normalize_command(value: object) -> str:
    """Collapse runs of internal whitespace to single space + strip.

    Sufficient for the variants ADR-0010 §4 names. Stops short of
    shlex-tokenization (see :class:`HookSignature` rationale).
    """
    if not isinstance(value, str):
        return ""
    return _WHITESPACE_RUN.sub(" ", value.strip())


def _iter_signatures(hooks_record: object) -> Iterator[HookSignature]:
    """Yield :class:`HookSignature` for every inner hook in a hooks record.

    Defensive: any non-dict / non-list shape encountered is skipped so a
    malformed sub-tree never crashes the doctor — the user's other
    tiers still classify cleanly. Mirrors the same shape guards in
    :mod:`memtomem.web.routes.settings_sync._compare_hooks`.
    """
    if not isinstance(hooks_record, dict):
        return
    for event, rules in hooks_record.items():
        if not isinstance(event, str) or not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            matcher = _normalize_matcher(rule.get("matcher", ""))
            inner = rule.get("hooks", [])
            if not isinstance(inner, list):
                continue
            for entry in inner:
                if not isinstance(entry, dict):
                    continue
                command = _normalize_command(entry.get("command", ""))
                if not command:
                    continue
                yield HookSignature(
                    event=event,
                    matcher=matcher,
                    command_shape=command,
                )


def load_canonical_signatures(project_root: Path) -> set[HookSignature]:
    """Read ``.memtomem/settings.json`` and return its hook signatures.

    Returns an empty set when the canonical file is missing or
    malformed — a missing canonical means there is nothing for the
    doctor to flag as duplicated; the file is not the doctor's
    responsibility to fix.
    """
    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    raw = _load_settings_dict(canonical_path)
    if raw is None:
        logger.debug("canonical settings at %s missing or unreadable", canonical_path)
        return set()
    hooks = raw.get("hooks", {})
    return set(_iter_signatures(hooks))


def _resolved(path: Path) -> Path:
    """``Path.resolve(strict=False)`` with OSError-tolerance.

    Used to dedupe tier paths that point at the same real file via
    symlink (e.g. ``<project>/.claude`` symlinked into ``~/.claude/``).
    Mirrors :func:`memtomem.context.settings._is_under_project_root`.
    """
    try:
        return path.resolve(strict=False)
    except (OSError, RuntimeError):
        return path


def detect_duplicate_tiers(
    project_root: Path,
    *,
    active_scope: str,
) -> list[DuplicateTier]:
    """Find non-active tiers holding canonical-matched hook entries.

    A tier is reported as a duplicate when **all** hold:

    * It is not the active scope.
    * Its resolved path differs from the active scope's resolved path
      (symlink dedup).
    * It exists and parses as JSON.
    * It contains at least one hook entry whose canonical signature
      appears in ``project_root / .memtomem/settings.json``.

    Returns an empty list when the canonical source has no hooks (the
    doctor has nothing to compare against).
    """
    canonical = load_canonical_signatures(project_root)
    if not canonical:
        return []

    active_resolved = _resolved(resolve_scope_path(project_root, active_scope))
    seen_paths: set[Path] = {active_resolved}
    duplicates: list[DuplicateTier] = []

    for scope in ALL_SCOPES:
        if scope == active_scope:
            continue
        tier_path = resolve_scope_path(project_root, scope)
        tier_resolved = _resolved(tier_path)
        if tier_resolved in seen_paths:
            # Symlinked into a tier we already accounted for; do not
            # report the same real file twice.
            continue
        seen_paths.add(tier_resolved)
        raw = _load_settings_dict(tier_path)
        if raw is None:
            logger.debug("tier %s at %s missing or unreadable; skipping", scope, tier_path)
            continue
        hooks = raw.get("hooks", {})
        matched = tuple(sig for sig in _iter_signatures(hooks) if sig in canonical)
        if matched:
            duplicates.append(
                DuplicateTier(
                    tier=scope,
                    path=tier_path,
                    entries=matched,
                )
            )

    return duplicates


def format_warning(duplicate: DuplicateTier, *, active_scope: str) -> str:
    """Human-readable warning string for one duplicate tier.

    Names the offending tier path and points at the
    ``mm context settings-migrate`` subcommand per ADR-0010 §4. Used by
    both the CLI sync surface and any caller that wants the same
    wording.
    """
    count = len(duplicate.entries)
    plural = "entry" if count == 1 else "entries"
    return (
        f"memtomem-managed hook {plural} ({count}) already exist in the "
        f"{duplicate.tier} tier ({duplicate.path}); run "
        f"`mm context settings-migrate --from={duplicate.tier} "
        f"--to={active_scope}` to move them. Active scope: {active_scope}."
    )
