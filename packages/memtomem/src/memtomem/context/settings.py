"""Canonical → runtime settings.json integration (Phase D).

Phase D of the "memtomem as canonical context gateway" plan.  A project's
canonical settings live at ``.memtomem/settings.json`` with a ``hooks``
record (keyed by event name).  From that single canonical source we fan
out to:

* ``~/.claude/settings.json`` — Claude Code (JSON deep-merge into ``hooks``)

Gemini and Codex have no known settings-file equivalent as of 2026-04-12;
runtimes can be added by implementing :class:`SettingsGenerator` and
registering in :data:`SETTINGS_GENERATORS`.

Hooks format (Claude Code ≥ 2.1.104)
-------------------------------------
``hooks`` is a **record** keyed by event name, not an array::

    {
      "hooks": {
        "PostToolUse": [
          {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "..."}]}
        ]
      }
    }

Merge semantics
---------------
* **Additive-only**: memtomem contributions are appended, never overwritten.
* **Identity key**: ``(event, matcher)`` — rules with the same event and
  matcher string are considered the same rule.  On collision the user's
  existing rule wins and a guided warning is emitted.
* **Formatting**: ``json.dumps(indent=2, sort_keys=False)`` + trailing
  newline.  Byte-for-byte preservation of the user's original formatting is
  explicitly **not** guaranteed — semantic equality is the contract.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

CANONICAL_SETTINGS_FILE = ".memtomem/settings.json"

# Sentinel for malformed JSON detection (identity-compared via ``is``)
_MALFORMED = object()


# ── Protocol + Result ───────────────────────────────────────────────


class SettingsGenerator(Protocol):
    """Protocol for runtime-specific settings generators."""

    name: str

    def is_available(self) -> bool:
        """``True`` if the runtime is installed (e.g. ``~/.claude/`` exists).

        Used to skip generators whose runtime isn't installed.  Generators
        must **not** auto-create the runtime's home directory.
        """
        ...

    def target_file(self, project_root: Path) -> Path:
        """Return the path to the runtime's settings file."""
        ...

    def canonical_source(self, project_root: Path) -> Path:
        """Return the path to ``.memtomem/settings.json``."""
        ...

    def merge(
        self,
        existing: dict | None,
        contributions: dict,
    ) -> tuple[dict, list[str]]:
        """Deep-merge *contributions* into *existing*.

        Returns ``(merged_dict, warning_messages)``.  Each warning must
        contain: (a) the conflicting hook name, (b) the reason for
        skipping, (c) a concrete remediation step.
        """
        ...


@dataclass
class SettingsSyncResult:
    """Result of a settings generate/sync/diff operation."""

    status: str  # "ok", "skipped", "error", "aborted", "in sync", etc.
    reason: str = ""
    warnings: list[str] = field(default_factory=list)
    target: Path | None = None


# ── Generators ──────────────────────────────────────────────────────

SETTINGS_GENERATORS: dict[str, SettingsGenerator] = {}


def _register(gen: SettingsGenerator) -> SettingsGenerator:
    SETTINGS_GENERATORS[gen.name] = gen
    return gen


@dataclass
class ClaudeSettingsGenerator:
    """Fan out canonical hooks to ``~/.claude/settings.json``."""

    name: str = "claude_settings"

    def is_available(self) -> bool:
        return (Path.home() / ".claude").is_dir()

    def target_file(self, project_root: Path) -> Path:
        return Path.home() / ".claude" / "settings.json"

    def canonical_source(self, project_root: Path) -> Path:
        return project_root / CANONICAL_SETTINGS_FILE

    def merge(
        self,
        existing: dict | None,
        contributions: dict,
    ) -> tuple[dict, list[str]]:
        """Additive merge of record-format ``hooks``.  User rules always win."""
        warnings: list[str] = []
        merged = dict(existing) if existing else {}

        contrib_hooks: dict = contributions.get("hooks", {})
        if not isinstance(contrib_hooks, dict):
            contrib_hooks = {}
        existing_hooks: dict = dict(merged.get("hooks", {}))
        if not isinstance(existing_hooks, dict):
            existing_hooks = {}

        for event, rules in contrib_hooks.items():
            if not isinstance(rules, list):
                continue
            if event not in existing_hooks:
                existing_hooks[event] = list(rules)
                continue

            # Index existing rules by matcher for conflict detection
            existing_rules: list = list(existing_hooks[event])
            existing_by_matcher: dict[str, dict] = {}
            for rule in existing_rules:
                if isinstance(rule, dict):
                    existing_by_matcher[rule.get("matcher", "")] = rule

            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                matcher = rule.get("matcher", "")
                if matcher in existing_by_matcher:
                    if existing_by_matcher[matcher] == rule:
                        continue  # already in sync
                    label = f"{event}:{matcher}" if matcher else event
                    warnings.append(
                        f"Hook rule '{label}' already exists in the target "
                        f"settings with different config. To use memtomem's "
                        f"version, remove the existing rule, then re-run "
                        f"`mm context sync --include=settings`."
                    )
                    continue
                existing_rules.append(rule)

            existing_hooks[event] = existing_rules

        merged["hooks"] = existing_hooks
        return merged, warnings


_register(ClaudeSettingsGenerator())


# ── Helpers ─────────────────────────────────────────────────────────


def _safe_load_json(path: Path) -> dict | object:
    """Load JSON from *path*, returning :data:`_MALFORMED` on parse error."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return _MALFORMED


def _read_with_mtime(path: Path) -> tuple[dict | None | object, float]:
    """Read JSON + capture mtime for concurrent-write guard.

    Returns ``(None, 0.0)`` when *path* does not exist and
    ``(_MALFORMED, mtime)`` when the file is not valid JSON.
    """
    if not path.is_file():
        return None, 0.0
    mtime = path.stat().st_mtime
    data = _safe_load_json(path)
    return data, mtime


def _write_json(path: Path, data: dict) -> None:
    """Write *data* as formatted JSON with trailing newline."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


# ── Fan-out: canonical → runtimes ───────────────────────────────────


def generate_all_settings(
    project_root: Path,
) -> dict[str, SettingsSyncResult]:
    """Fan out ``.memtomem/settings.json`` to registered runtimes."""
    results: dict[str, SettingsSyncResult] = {}

    for name, gen in SETTINGS_GENERATORS.items():
        if not gen.is_available():
            results[name] = SettingsSyncResult(
                status="skipped",
                reason=(f"{name} runtime not installed (target dir missing)"),
            )
            continue

        canonical_path = gen.canonical_source(project_root)
        if not canonical_path.exists():
            results[name] = SettingsSyncResult(
                status="skipped",
                reason="no canonical source (.memtomem/settings.json)",
            )
            continue

        raw = _safe_load_json(canonical_path)
        if raw is _MALFORMED:
            results[name] = SettingsSyncResult(
                status="error",
                reason=f"{canonical_path} is not valid JSON. Fix the file manually.",
            )
            continue
        contributions: dict = raw  # type: ignore[assignment]

        target_path = gen.target_file(project_root)

        # Step 1: read existing + capture mtime
        existing_raw, existing_mtime = _read_with_mtime(target_path)
        if existing_raw is _MALFORMED:
            results[name] = SettingsSyncResult(
                status="error",
                reason=f"{target_path} is not valid JSON. "
                f"Fix the file manually, then re-run "
                f"`mm context sync --include=settings`.",
            )
            continue
        existing: dict | None = existing_raw  # type: ignore[assignment]

        # Step 2: merge in memory
        merged, warnings = gen.merge(existing, contributions)

        # Step 3: mtime check (concurrent-write guard)
        if target_path.is_file() and target_path.stat().st_mtime != existing_mtime:
            results[name] = SettingsSyncResult(
                status="aborted",
                reason=f"{target_path} was modified by another "
                f"process during merge. Re-run "
                f"`mm context sync --include=settings` to retry.",
            )
            continue

        # Step 4: write
        _write_json(target_path, merged)
        results[name] = SettingsSyncResult(
            status="ok",
            warnings=warnings,
            target=target_path,
        )

    return results


def diff_settings(
    project_root: Path,
) -> dict[str, SettingsSyncResult]:
    """Dry-run: compute what :func:`generate_all_settings` would do."""
    results: dict[str, SettingsSyncResult] = {}

    for name, gen in SETTINGS_GENERATORS.items():
        if not gen.is_available():
            results[name] = SettingsSyncResult(
                status="skipped",
                reason=f"{name} runtime not installed",
            )
            continue

        canonical_path = gen.canonical_source(project_root)
        if not canonical_path.exists():
            results[name] = SettingsSyncResult(
                status="skipped",
                reason="no canonical source",
            )
            continue

        raw = _safe_load_json(canonical_path)
        if raw is _MALFORMED:
            results[name] = SettingsSyncResult(
                status="error",
                reason=f"{canonical_path} is not valid JSON",
            )
            continue
        contributions: dict = raw  # type: ignore[assignment]

        target_path = gen.target_file(project_root)
        existing_raw, _ = _read_with_mtime(target_path)
        if existing_raw is _MALFORMED:
            results[name] = SettingsSyncResult(
                status="error",
                reason=f"{target_path} is not valid JSON",
            )
            continue
        existing: dict | None = existing_raw  # type: ignore[assignment]

        merged, warnings = gen.merge(existing, contributions)

        if existing is not None:
            existing_norm = json.dumps(existing, sort_keys=True)
            merged_norm = json.dumps(merged, sort_keys=True)
            if existing_norm == merged_norm:
                status = "in sync"
            else:
                status = "out of sync"
        else:
            status = "missing target"

        results[name] = SettingsSyncResult(
            status=status,
            warnings=warnings,
            target=target_path,
        )

    return results


# sync is the same operation as generate for settings
sync_all_settings = generate_all_settings


__all__ = [
    "CANONICAL_SETTINGS_FILE",
    "ClaudeSettingsGenerator",
    "SETTINGS_GENERATORS",
    "SettingsGenerator",
    "SettingsSyncResult",
    "diff_settings",
    "generate_all_settings",
    "sync_all_settings",
]
