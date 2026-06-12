"""Canonical → runtime settings.json integration (Phase D).

Phase D of the "memtomem as canonical context gateway" plan.  A project's
canonical settings live at ``.memtomem/settings.json`` with a ``hooks``
record (keyed by Claude-style event name).  From that single canonical
source we fan out to each installed runtime's hooks file:

* ``~/.claude/settings.json`` — Claude Code (JSON deep-merge into ``hooks``)
* ``~/.codex/hooks.json`` — Codex CLI (same event names + record shape as
  Claude; matchers ``Bash``/``Edit``/``Write`` are accepted natively, so
  the merge is near-identical — events Codex lacks, ``Notification`` /
  ``SessionEnd``, are dropped with a warning)
* ``~/.gemini/settings.json`` — Gemini CLI (event names AND tool-name
  matchers are remapped — ``PreToolUse``→``BeforeTool``, ``Bash``→
  ``run_shell_command`` etc.; unmappable events/matchers are dropped with a
  warning — see :func:`_remap_for_gemini`)

Codex/Gemini have no ``project_local`` hooks target (``target_file`` returns
``None`` there).  Additional runtimes can be added by implementing
:class:`SettingsGenerator` and registering in :data:`SETTINGS_GENERATORS`.

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

import hashlib
import json
import logging
import re
import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from memtomem.context._atomic import _file_lock, _lock_path_for, atomic_write_text

logger = logging.getLogger(__name__)

CANONICAL_SETTINGS_FILE = ".memtomem/settings.json"

# Sentinel for malformed JSON detection (identity-compared via ``is``)
_MALFORMED = object()


class MalformedSettingsError(ValueError):
    """A settings document carries a structurally unusable value (e.g.
    ``hooks`` as a JSON array instead of a record keyed by event name).

    Raised by the merge layer instead of silently coercing — coercion
    destroyed the user's hook configuration and wrote it back with
    ``status="ok"`` (#1229). Engine callers convert it to a per-target
    ``status="error"`` so the file is never rewritten destructively.
    """


_KIMI_TOML_TEXT_KEY = "__memtomem_kimi_config_toml__"
_KIMI_BEGIN = "# BEGIN memtomem managed hooks"
_KIMI_END = "# END memtomem managed hooks"

# Whole-call budget for sidecar-lock acquisition across ALL targets (#1145
# review). The web handler offloads ``generate_all_settings`` to a worker
# thread under a 60s ``asyncio.timeout``; an unbounded ``portalocker`` wait
# there would leave an un-cancellable thread writing after the handler already
# returned 503. This budget is shared across the per-target locks (claude /
# codex / gemini), NOT per target — a single deadline is computed once and each
# target waits only the remaining time — so the total wait can never approach
# ``N_targets × bound`` and stays comfortably under the handler's 60s no matter
# how many runtimes are registered. On exhaustion a target self-aborts
# (status="aborted"). The CLI path inherits the same budget, strictly better
# than hanging.
_SETTINGS_LOCK_BUDGET_S = 30.0


def resolve_scope_path(project_root: Path, scope: str) -> Path:
    """Resolve ``hooks.target_scope`` to the runtime settings file path.

    Single source of truth for the ADR-0010 §3 path math; shared by
    :class:`ClaudeSettingsGenerator`, the Web ``_claude_target`` helper,
    and the detector. Raises ``ValueError`` on unknown scope so a typo in
    config.json surfaces loudly instead of silently writing to the
    fallback path.
    """
    if scope == "user":
        return Path.home() / ".claude" / "settings.json"
    if scope == "project_shared":
        return project_root / ".claude" / "settings.json"
    if scope == "project_local":
        return project_root / ".claude" / "settings.local.json"
    raise ValueError(f"Unknown target_scope: {scope!r}")


# ── Protocol + Result ───────────────────────────────────────────────


class SettingsGenerator(Protocol):
    """Protocol for runtime-specific settings generators."""

    name: str

    def is_available(self, project_root: Path) -> bool:
        """``True`` if the runtime is installed for this project.

        Per ADR-0010 §3 the probe is "any of the resolved scopes is a
        plausible target dir": ``~/.claude/`` for user-tier, or
        ``<project_root>/.claude/`` for project-tier. Used to skip
        generators whose runtime isn't installed.  Generators must
        **not** auto-create the runtime's home directory.
        """
        ...

    def target_file(self, project_root: Path, scope: str) -> Path | None:
        """Return the runtime's settings file for *scope*.

        ``None`` means "no runtime fan-out for this (runtime, scope)" —
        e.g. Codex/Gemini have no ``project_local`` hooks target. Every
        consumer treats ``None`` as *skipped* (no write, not an error).
        """
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


# ── Hook-rule ownership marker (ADR-0019) ───────────────────────────
#
# memtomem stamps every hook rule it generates with an ownership marker so a
# later ``mm context sync`` can recognize — and *update* — its own previously
# emitted rules instead of treating them as user conflicts (issue #1110). The
# marker uses only **officially documented** handler fields (a custom key risks
# strict-schema rejection of the whole settings file):
#   * Gemini handlers carry ``name`` → prefix ``memtomem-`` (synthesized in
#     :func:`_ensure_gemini_handler_names`).
#   * Claude/Codex command handlers carry ``statusMessage`` → reserved prefix
#     ``"memtomem · "`` (stamped by :func:`_stamp_status_markers`).
# Both prefixes are a *reserved namespace*: a rule whose handler name/
# statusMessage starts with them is memtomem-owned and will be overwritten on
# re-sync. Hand-editing such a rule while keeping the prefix loses the edit.
_MEMTOMEM_NAME_PREFIX = "memtomem-"
_MEMTOMEM_STATUS_PREFIX = "memtomem · "


def _handler_is_memtomem_owned(handler: object) -> bool:
    """True if *handler* carries a memtomem ownership marker (name/statusMessage)."""
    if not isinstance(handler, dict):
        return False
    name = handler.get("name")
    if isinstance(name, str) and name.startswith(_MEMTOMEM_NAME_PREFIX):
        return True
    status = handler.get("statusMessage")
    return isinstance(status, str) and status.startswith(_MEMTOMEM_STATUS_PREFIX)


def _rule_is_memtomem_owned(rule: object) -> bool:
    """True if any handler in *rule* is memtomem-owned."""
    if not isinstance(rule, dict):
        return False
    handlers = rule.get("hooks")
    if not isinstance(handlers, list):
        return False
    return any(_handler_is_memtomem_owned(h) for h in handlers)


def _rule_commands(rule: object) -> set[str]:
    """Set of handler ``command`` strings in *rule* (for the legacy-rule heuristic)."""
    if not isinstance(rule, dict):
        return set()
    handlers = rule.get("hooks")
    if not isinstance(handlers, list):
        return set()
    return {
        h["command"] for h in handlers if isinstance(h, dict) and isinstance(h.get("command"), str)
    }


def _strip_ownership_markers(rule: dict) -> dict:
    """Return a copy of *rule* with the marker-carrier fields removed.

    Used for *functional* comparison. The marker lives in cosmetic handler
    fields — ``name`` (Gemini's ``/hooks disable`` handle) and ``statusMessage``
    (Claude/Codex spinner text) — which do not change what the hook *does*
    (``type``/``command``/``timeout``/``matcher``). Both are dropped **entirely
    and symmetrically** so comparison turns purely on hook function. Stripping
    only the memtomem prefix would be asymmetric: a target with no
    ``statusMessage`` would compare equal to a stamped contribution while a
    user's raw pre-stamp ``statusMessage`` would not — hiding/raising conflicts
    inconsistently (Codex review). A genuine cosmetic-only change to a
    memtomem-owned rule still propagates: such a rule is replaced wholesale by
    the merge's in-place owned-slot pass, not gated on this comparison.
    """
    handlers = rule.get("hooks")
    if not isinstance(handlers, list):
        return rule
    new_handlers: list = []
    for h in handlers:
        if not isinstance(h, dict):
            new_handlers.append(h)
            continue
        nh = {k: v for k, v in h.items() if k not in ("name", "statusMessage")}
        new_handlers.append(nh)
    return {**rule, "hooks": new_handlers}


def _rule_content_equal(a: object, b: object) -> bool:
    """True if *a* and *b* are functionally equal (ignoring marker-carrier fields).

    Shared outside this module by the Web hooks diff and settings-migrate
    planner; keep its cosmetic-field semantics aligned with merge behavior.
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        return a == b
    return _strip_ownership_markers(a) == _strip_ownership_markers(b)


def _stamp_status_markers(contributions: dict) -> dict:
    """Return a deep copy of *contributions* with the ownership marker stamped.

    Sets ``statusMessage`` on every **command** handler to a reserved-prefixed
    string. Author text is preserved after the prefix (so a canonical
    ``statusMessage`` still shows a meaningful spinner message) and an
    already-prefixed value is left unchanged — making the stamp a pure,
    deterministic function of the handler so re-sync stays idempotent. Used by
    the Claude/Codex generators; Gemini marks via ``name`` instead and must not
    receive ``statusMessage`` (not a Gemini handler field).
    """
    src_hooks = contributions.get("hooks", {})
    if not isinstance(src_hooks, dict):
        return contributions
    out_hooks: dict[str, list] = {}
    for event, rules in src_hooks.items():
        if not isinstance(rules, list):
            out_hooks[event] = rules
            continue
        new_rules: list = []
        for rule in rules:
            if not isinstance(rule, dict):
                new_rules.append(rule)
                continue
            new_rule = dict(rule)
            handlers = rule.get("hooks")
            if isinstance(handlers, list):
                new_handlers: list = []
                for handler in handlers:
                    if not isinstance(handler, dict) or handler.get("type") != "command":
                        new_handlers.append(handler)
                        continue
                    new_handler = dict(handler)
                    existing = new_handler.get("statusMessage")
                    if isinstance(existing, str):
                        if not existing.startswith(_MEMTOMEM_STATUS_PREFIX):
                            new_handler["statusMessage"] = f"{_MEMTOMEM_STATUS_PREFIX}{existing}"
                    else:
                        new_handler["statusMessage"] = f"{_MEMTOMEM_STATUS_PREFIX}{event}"
                    new_handlers.append(new_handler)
                new_rule["hooks"] = new_handlers
            new_rules.append(new_rule)
        out_hooks[event] = new_rules
    return {**contributions, "hooks": out_hooks}


def _merge_hooks_record(
    existing: dict | None,
    contributions: dict,
) -> tuple[dict, list[str]]:
    """Additive merge of record-format ``hooks``, ownership-aware (ADR-0019).

    Identity key is ``(event, matcher)``. On a same-key collision:

    * an existing **memtomem-owned** rule (carries the ownership marker) is
      *replaced* by the freshly generated contribution — memtomem updates its
      own rules across releases (issue #1110) — while any *user* rules under
      the same matcher are preserved;
    * an existing **user** rule wins and a guided warning is emitted (the
      long-standing "user rules always win" contract). When the user rule
      shares a command with the contribution it is most likely a memtomem rule
      from before ownership markers existed, so the warning is sharpened to
      guide the one-time cleanup — but the rule is **never** silently
      overwritten.

    Shared by Claude and Codex (identical event names + record shape); Gemini
    remaps event/matcher names via :func:`_remap_for_gemini` first, then merges
    the remapped record through here, so semantics stay identical across
    runtimes. Contributions are expected to be marker-stamped before this call
    (Claude/Codex via :func:`_stamp_status_markers`, Gemini via
    :func:`_ensure_gemini_handler_names`).
    """
    warnings: list[str] = []
    if existing is not None and not isinstance(existing, dict):
        raise MalformedSettingsError(
            f"settings root must be a JSON object, found {type(existing).__name__}"
        )
    merged = dict(existing) if existing else {}

    contrib_hooks: dict = contributions.get("hooks", {})
    if not isinstance(contrib_hooks, dict):
        contrib_hooks = {}
    raw_hooks = merged.get("hooks", {})
    if raw_hooks is None:
        raw_hooks = {}
    if not isinstance(raw_hooks, dict):
        # Type-check BEFORE coercing: ``dict()`` over a list of rule dicts
        # silently turns e.g. [{"matcher": ..., "hooks": [...]}] into the
        # garbage {"matcher": "hooks"}, which then overwrites the user's
        # entire hook configuration with status="ok" (#1229). Refuse loudly
        # — the engine degrades this to a per-target error status.
        raise MalformedSettingsError(
            f"'hooks' must be a record keyed by event name, found {type(raw_hooks).__name__}"
        )
    existing_hooks: dict = dict(raw_hooks)

    for event, rules in contrib_hooks.items():
        if not isinstance(rules, list):
            continue
        if event not in existing_hooks:
            # An empty contribution list only matters for events already in
            # the target (Pass 1 prunes stale owned rules there); copying it
            # into a target that never had the event is pure noise.
            if rules:
                existing_hooks[event] = list(rules)
            continue

        # Same loud-refusal contract one level down: ``list()`` over a dict
        # event value yields its key strings (written back as "rules"), and
        # over a scalar raises TypeError past the MalformedSettingsError
        # catch (Codex review on #1229).
        raw_rules = existing_hooks[event]
        if not isinstance(raw_rules, list):
            raise MalformedSettingsError(
                f"'hooks.{event}' must be a list of rules, found {type(raw_rules).__name__}"
            )
        existing_rules: list = list(raw_rules)
        contrib_rules: list[dict] = [r for r in rules if isinstance(r, dict)]
        # Contributions queued per matcher (FIFO, order preserved) so each
        # memtomem-owned slot consumes exactly one — never double-replacing or
        # dropping a contribution when canonical emits two rules per matcher.
        contrib_by_matcher: dict[str, list[dict]] = {}
        for c in contrib_rules:
            contrib_by_matcher.setdefault(c.get("matcher", ""), []).append(c)

        # Pass 1 — walk existing rules in place: a memtomem-owned rule is
        # replaced by the next unconsumed contribution of the same matcher
        # (memtomem updates its own rule across releases, issue #1110); an
        # owned rule memtomem no longer emits is dropped; user rules are kept
        # verbatim and in position.
        result_rules: list = []
        consumed: dict[str, int] = {}
        placed_ids: set[int] = set()
        replaced_any = False
        for r in existing_rules:
            if isinstance(r, dict) and _rule_is_memtomem_owned(r):
                matcher = r.get("matcher", "")
                queue = contrib_by_matcher.get(matcher, [])
                idx = consumed.get(matcher, 0)
                if idx < len(queue):
                    c = queue[idx]
                    consumed[matcher] = idx + 1
                    placed_ids.add(id(c))
                    result_rules.append(c)
                    replaced_any = True
                # else: no current contribution for this matcher → drop the
                # stale memtomem rule.
                continue
            result_rules.append(r)

        # Pass 2 — contributions not consumed by an owned slot merge additively
        # against the *user* rules. Claude Code allows the same matcher to
        # appear more than once, so user rules stay as a list (not collapsed).
        for c in contrib_rules:
            if id(c) in placed_ids:
                continue
            matcher = c.get("matcher", "")
            same_user = [
                r
                for r in result_rules
                if isinstance(r, dict)
                and r.get("matcher", "") == matcher
                and not _rule_is_memtomem_owned(r)
            ]
            if not same_user:
                result_rules.append(c)
                continue
            if any(_rule_content_equal(u, c) for u in same_user):
                # Already present ignoring our marker — leave the user's copy
                # untouched rather than rewrite it just to add the marker.
                continue
            label = f"{event}:{matcher}" if matcher else event
            contrib_commands = _rule_commands(c)
            if contrib_commands and any(contrib_commands & _rule_commands(u) for u in same_user):
                warnings.append(
                    f"Hook rule '{label}' has the same command as a "
                    f"memtomem-managed rule but no ownership marker. If this "
                    f"is a memtomem-managed rule from a previous version, "
                    f"remove it, then re-run "
                    f"`mm context sync --include=settings` to let memtomem "
                    f"update it."
                )
            else:
                warnings.append(
                    f"Skipped memtomem hook '{label}': the target settings "
                    f"already contain a user-owned rule with the same "
                    f"event+matcher. Change one matcher to keep both rules, "
                    f"or remove the user rule and re-run "
                    f"`mm context sync --include=settings` to replace it."
                )

        if replaced_any:
            logger.debug("Updated memtomem-managed hook rule(s) for event %r", event)
        existing_hooks[event] = result_rules

    # Prune memtomem-owned rules from events the canonical no longer emits at
    # all. The ownership marker makes them safe to remove; user rules under the
    # same event are preserved, and an event left empty is dropped entirely.
    for event in list(existing_hooks):
        if event in contrib_hooks:
            continue
        rules = existing_hooks[event]
        if not isinstance(rules, list):
            continue
        kept = [r for r in rules if not (isinstance(r, dict) and _rule_is_memtomem_owned(r))]
        if len(kept) == len(rules):
            continue
        if kept:
            existing_hooks[event] = kept
        else:
            del existing_hooks[event]

    # Write the worked copy back only when the target already had the key
    # (Pass 1/2 replacements and the stale-owned prune must land) or the merge
    # produced content — injecting a cosmetic ``"hooks": {}`` into a target
    # that never had one made diff report a false "out of sync" and sync
    # rewrite the user's file just to add the empty key (#1229).
    if "hooks" in merged or existing_hooks:
        merged["hooks"] = existing_hooks
    return merged, warnings


@dataclass
class ClaudeSettingsGenerator:
    """Fan out canonical hooks to ``~/.claude/settings.json``."""

    name: str = "claude_settings"

    def is_available(self, project_root: Path) -> bool:
        # Loosened per ADR-0010 §3: tile shows up if Claude Code has any
        # settings home for this project — user-tier or project-tier.
        return (Path.home() / ".claude").is_dir() or (project_root / ".claude").is_dir()

    def target_file(self, project_root: Path, scope: str) -> Path:
        return resolve_scope_path(project_root, scope)

    def canonical_source(self, project_root: Path) -> Path:
        return project_root / CANONICAL_SETTINGS_FILE

    def merge(
        self,
        existing: dict | None,
        contributions: dict,
    ) -> tuple[dict, list[str]]:
        """Ownership-aware merge of record-format ``hooks`` (ADR-0019)."""
        return _merge_hooks_record(existing, _stamp_status_markers(contributions))


_register(ClaudeSettingsGenerator())


# ── Codex + Gemini hook fan-out (ADR-0010 multi-runtime) ─────────────
#
# Canonical hook event names are Claude's. Mappings below were verified
# against official docs this session: code.claude.com/docs/en/hooks,
# developers.openai.com/codex/hooks, and gemini-cli
# docs/hooks/writing-hooks.md (event list + tool-name matchers).

# Events Codex understands. Codex shares Claude's event names and accepts
# Bash/Edit/Write matchers natively, so supported events pass through
# unchanged; canonical events outside this set (Notification, SessionEnd)
# are dropped with a warning.
_CODEX_EVENTS: frozenset[str] = frozenset(
    {
        "SessionStart",
        "SubagentStart",
        "PreToolUse",
        "PermissionRequest",
        "PostToolUse",
        "PreCompact",
        "PostCompact",
        "UserPromptSubmit",
        "SubagentStop",
        "Stop",
    }
)

# Canonical (Claude) event → Gemini event. Canonical events with no entry
# here have no Gemini equivalent and are dropped with a warning.
#
# ``UserPromptSubmit`` and ``Stop`` are **best-effort lifecycle mappings — not
# a verified 1:1**. Gemini's docs define no exact analog for either; we map
# them so memtomem's prompt-time context-injection (UserPromptSubmit) and
# session-close (Stop) hook paths still fire on Gemini — ``BeforeAgent``
# injects context before agent processing, ``AfterAgent`` runs after the agent
# completes — while acknowledging the firing timing is approximate, not
# formally identical (see ADR-0018).
_GEMINI_EVENT_MAP: dict[str, str] = {
    "PreToolUse": "BeforeTool",
    "PostToolUse": "AfterTool",
    "SessionStart": "SessionStart",
    "SessionEnd": "SessionEnd",
    "Notification": "Notification",
    "PreCompact": "PreCompress",
    "UserPromptSubmit": "BeforeAgent",  # best-effort — approximate timing
    "Stop": "AfterAgent",  # best-effort — approximate timing
}

# Gemini events whose matcher is a *tool name* (matcher must be remapped);
# other Gemini events take their matcher through unchanged.
_GEMINI_TOOL_EVENTS: frozenset[str] = frozenset({"BeforeTool", "AfterTool", "BeforeToolSelection"})

# Claude tool token → Gemini built-in tool name. Claude matchers are
# tool-name regexes (e.g. "Edit|Write"); each pipe-delimited token is
# mapped. A token with no entry here makes the hook undeliverable (it could
# never fire), so the rule is dropped with a warning rather than emitted.
_GEMINI_TOOL_MAP: dict[str, str] = {
    "Bash": "run_shell_command",
    # Gemini's in-place edit tool is ``replace``; ``write_file`` is whole-file
    # create/overwrite. Claude Edit/MultiEdit are surgical edits → ``replace``;
    # Claude Write (full file) → ``write_file``. Verified against gemini-cli
    # docs/tools/file-system.md.
    "Edit": "replace",
    "MultiEdit": "replace",
    "Write": "write_file",
    "Read": "read_file",
}


def _codex_target_file(project_root: Path, scope: str) -> Path | None:
    """Codex hooks file per scope. ``project_local`` has no fan-out (``None``)."""
    if scope == "user":
        return Path.home() / ".codex" / "hooks.json"
    if scope == "project_shared":
        return project_root / ".codex" / "hooks.json"
    if scope == "project_local":
        return None
    raise ValueError(f"Unknown target_scope: {scope!r}")


def _gemini_target_file(project_root: Path, scope: str) -> Path | None:
    """Gemini settings file per scope. ``project_local`` has no fan-out (``None``)."""
    if scope == "user":
        return Path.home() / ".gemini" / "settings.json"
    if scope == "project_shared":
        return project_root / ".gemini" / "settings.json"
    if scope == "project_local":
        return None
    raise ValueError(f"Unknown target_scope: {scope!r}")


def _kimi_target_file(project_root: Path, scope: str) -> Path | None:
    """Kimi config file per scope. ``project_local`` has no fan-out."""
    if scope == "user":
        return Path.home() / ".kimi" / "config.toml"
    if scope == "project_shared":
        return project_root / ".kimi" / "config.toml"
    if scope == "project_local":
        return None
    raise ValueError(f"Unknown target_scope: {scope!r}")


def _filter_codex_events(contributions: dict) -> tuple[dict, list[str]]:
    """Drop canonical events Codex doesn't support (Notification, SessionEnd).

    Codex shares Claude's event names and accepts Bash/Edit/Write matchers,
    so supported events pass through verbatim; only the unsupported ones are
    dropped, each with a warning.
    """
    warnings: list[str] = []
    src_hooks = contributions.get("hooks", {})
    if not isinstance(src_hooks, dict):
        return {"hooks": {}}, warnings
    out: dict[str, list] = {}
    for event, rules in src_hooks.items():
        if event not in _CODEX_EVENTS:
            warnings.append(
                f"Hook event '{event}' has no Codex equivalent and was dropped "
                f"from the Codex hooks file. Codex supports: "
                f"{', '.join(sorted(_CODEX_EVENTS))}."
            )
            continue
        out[event] = rules
    return {"hooks": out}, warnings


_KIMI_EVENT_MAP: dict[str, str] = {
    "SessionStart": "SessionStart",
    "PreToolUse": "PreToolUse",
    "PostToolUse": "PostToolUse",
    "UserPromptSubmit": "UserPromptSubmit",
    "Stop": "Stop",
}

_KIMI_TOOL_EVENTS: frozenset[str] = frozenset({"PreToolUse", "PostToolUse"})

_KIMI_TOOL_MAP: dict[str, str] = {
    "Bash": "Shell",
    "Read": "ReadFile",
    "Write": "WriteFile",
    "Edit": "StrReplaceFile",
    "MultiEdit": "StrReplaceFile",
}


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def _map_kimi_matcher(matcher: str) -> tuple[str | None, list[str]]:
    matcher = matcher.strip()
    if not matcher:
        return "*", []
    mapped: list[str] = []
    unmapped: list[str] = []
    for raw in matcher.split("|"):
        token = raw.strip()
        if not token:
            continue
        kimi_tool = _KIMI_TOOL_MAP.get(token)
        if kimi_tool is None:
            unmapped.append(token)
        elif kimi_tool not in mapped:
            mapped.append(kimi_tool)
    if unmapped:
        return None, unmapped
    return "|".join(mapped) if mapped else "*", []


def _render_kimi_hooks(contributions: dict) -> tuple[str, list[str]]:
    warnings: list[str] = []
    src_hooks = contributions.get("hooks", {})
    if not isinstance(src_hooks, dict):
        return "", warnings
    chunks: list[str] = []
    for event, rules in src_hooks.items():
        kimi_event = _KIMI_EVENT_MAP.get(event)
        if kimi_event is None:
            warnings.append(f"Hook event '{event}' has no Kimi equivalent and was dropped.")
            continue
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            matcher = str(rule.get("matcher", ""))
            if kimi_event in _KIMI_TOOL_EVENTS:
                mapped_matcher, unmapped = _map_kimi_matcher(matcher)
                if mapped_matcher is None:
                    warnings.append(
                        f"Hook '{event}:{matcher}' matcher token(s) "
                        f"{', '.join(unmapped)} have no Kimi tool equivalent; rule dropped."
                    )
                    continue
                matcher = mapped_matcher
            handlers = rule.get("hooks", [])
            if not isinstance(handlers, list):
                continue
            for handler in handlers:
                if not isinstance(handler, dict) or handler.get("type") != "command":
                    continue
                command = handler.get("command")
                if not isinstance(command, str) or not command.strip():
                    continue
                rows = [
                    "[[hooks]]",
                    f"event = {_toml_string(kimi_event)}",
                    f"matcher = {_toml_string(matcher)}",
                    f"command = {_toml_string(command)}",
                ]
                timeout = handler.get("timeout")
                if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
                    rows.append(f"timeout = {timeout}")
                chunks.append("\n".join(rows))
    return "\n\n".join(chunks), warnings


def _replace_kimi_managed_block(existing: str, body: str) -> str:
    block_body = body.strip()
    block = f"{_KIMI_BEGIN}\n{block_body}\n{_KIMI_END}\n" if block_body else ""
    pattern = re.compile(
        r"(?ms)^# BEGIN memtomem managed hooks\n.*?^# END memtomem managed hooks\n?"
    )
    if pattern.search(existing):
        # Callable replacement — a plain-string replacement is processed as a
        # template, so the ``\\`` sequences ``_toml_string`` emits for
        # backslash-bearing hook commands (regex ``\b``, Windows paths) get
        # halved and literal ``\n`` becomes a raw newline, corrupting the
        # TOML on the second and every later sync (the first sync takes the
        # concat branch below and was unaffected).
        updated = pattern.sub(lambda _m: block, existing)
    else:
        updated = existing.rstrip()
        if updated and block:
            updated += "\n\n"
        updated += block
    return updated.rstrip() + ("\n" if updated.rstrip() else "")


def _map_gemini_matcher(matcher: str) -> tuple[str | None, list[str]]:
    """Map a Claude tool-name matcher to Gemini tool names.

    Returns ``(mapped_matcher, unmapped_tokens)``. An empty matcher (Claude
    "all tools") maps to ``"*"``. Pipe-delimited tokens are mapped
    individually and de-duplicated. If *any* token has no Gemini equivalent
    the rule is undeliverable, so ``unmapped_tokens`` is returned non-empty
    and ``mapped_matcher`` is ``None`` (the caller drops the rule).
    """
    matcher = matcher.strip()
    if not matcher:
        return "*", []
    mapped: list[str] = []
    unmapped: list[str] = []
    for raw in matcher.split("|"):
        token = raw.strip()
        if not token:
            continue
        gemini_tool = _GEMINI_TOOL_MAP.get(token)
        if gemini_tool is None:
            unmapped.append(token)
        elif gemini_tool not in mapped:
            mapped.append(gemini_tool)
    if unmapped:
        return None, unmapped
    if not mapped:
        # Separator-only matcher (e.g. "|") — no real tokens to map, so
        # treat it the same as an empty matcher: all tools ("*").
        return "*", []
    return "|".join(mapped), []


def _ensure_gemini_handler_names(
    handlers: object, event: str, matcher: str, original_matcher: str
) -> list:
    """Normalize handlers for Gemini: synthesize a ``name`` and rescale ``timeout``.

    Two conversions, both keyed off the canonical (Claude/Codex-shaped) handler:

    * **timeout unit.** Claude/Codex hook timeouts are *seconds*; Gemini's are
      *milliseconds*. A numeric ``timeout`` is multiplied by 1000 so a canonical
      ``timeout: 30`` (30s) doesn't become 30ms on Gemini and kill the hook
      before it can run. ``bool`` (an ``int`` subclass) is left alone.
    * **stable name (ownership marker).** Gemini exposes ``/hooks disable
      <name>``, so each handler needs a *distinct* stable name; the
      ``memtomem-`` prefix is also memtomem's ownership marker (ADR-0019), so a
      re-sync can recognize and update its own Gemini rules (issue #1110). The
      name is **always (re)stamped**, overriding any canonical-provided
      ``name`` — memtomem owns the Gemini name. The slug alone is not enough:
      distinct Claude matchers can collapse to one Gemini tool (``Edit`` and
      ``MultiEdit`` both map to ``replace``), which would make two handlers
      share ``memtomem-<event>-replace``. We hash the handler's **canonical
      identity** — the *original* (pre-remap) matcher, its position in the rule,
      and the command — *not* the remapped Gemini matcher. So ``Edit`` and
      ``MultiEdit`` get distinct names even when they run the *same* command,
      and the hash is content-derived → stable across re-syncs (idempotent).
      Two byte-identical canonical rules intentionally share a name: they are
      the same hook, and ``/hooks disable`` should govern both.
    """
    if not isinstance(handlers, list):
        return []
    out: list = []
    # Sanitize the remapped matcher to the Gemini name charset: lifecycle or
    # multi-tool matchers can carry ``|`` / ``*`` / spaces / punctuation, which
    # would leak into ``memtomem-<event>-<slug>-<digest>`` and break
    # ``/hooks disable <name>``. Collapse any run of non-[A-Za-z0-9_.-] to ``-``.
    # Uniqueness still comes from the digest, so the slug is purely cosmetic.
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", matcher or "all").strip("-") or "all"
    for idx, handler in enumerate(handlers):
        if not isinstance(handler, dict):
            continue
        new_handler = dict(handler)
        timeout = new_handler.get("timeout")
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            new_handler["timeout"] = timeout * 1000
        command = new_handler.get("command")
        command_str = command if isinstance(command, str) else ""
        # NUL-delimited so the three fields can't be confused with each
        # other (e.g. matcher "a" + command "b" vs matcher "a\x00b").
        identity = f"{original_matcher}\x00{idx}\x00{command_str}"
        digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:8]
        new_handler["name"] = f"memtomem-{event}-{slug}-{digest}"
        out.append(new_handler)
    return out


def _remap_for_gemini(contributions: dict) -> tuple[dict, list[str]]:
    """Rewrite canonical (Claude-shaped) hooks into Gemini's settings.json shape.

    Two-stage conversion (verified against gemini-cli writing-hooks.md):
    event names are remapped (``PreToolUse``→``BeforeTool`` …) and, for
    tool-matching events, the matcher tool tokens are remapped
    (``Bash``→``run_shell_command`` …); handlers gain a synthesized
    ``name``. Anything that can't convert faithfully — an event with no
    Gemini equivalent, or a matcher whose tokens don't map to a Gemini tool
    — is dropped and reported in the returned warnings (a hook that can
    never fire is worse than a clear warning).
    """
    warnings: list[str] = []
    src_hooks = contributions.get("hooks", {})
    if not isinstance(src_hooks, dict):
        return {"hooks": {}}, warnings
    out: dict[str, list] = {}
    for event, rules in src_hooks.items():
        gemini_event = _GEMINI_EVENT_MAP.get(event)
        if gemini_event is None:
            warnings.append(
                f"Hook event '{event}' has no Gemini equivalent and was dropped. "
                f"Gemini supports: {', '.join(sorted(_GEMINI_EVENT_MAP))}."
            )
            continue
        if not isinstance(rules, list):
            continue
        for rule in rules:
            if not isinstance(rule, dict):
                continue
            matcher = rule.get("matcher", "")
            new_rule = dict(rule)
            if gemini_event in _GEMINI_TOOL_EVENTS:
                mapped_matcher, unmapped = _map_gemini_matcher(matcher)
                if mapped_matcher is None:
                    warnings.append(
                        f"Hook '{event}:{matcher}' matcher token(s) "
                        f"{', '.join(unmapped)} have no Gemini tool equivalent; "
                        f"rule dropped (would never fire). Known tools: "
                        f"{', '.join(sorted(set(_GEMINI_TOOL_MAP.values())))}."
                    )
                    continue
                new_rule["matcher"] = mapped_matcher
            new_rule["hooks"] = _ensure_gemini_handler_names(
                rule.get("hooks", []), gemini_event, new_rule.get("matcher", ""), matcher
            )
            out.setdefault(gemini_event, []).append(new_rule)
    return {"hooks": out}, warnings


@dataclass
class CodexSettingsGenerator:
    """Fan out canonical hooks to Codex's ``hooks.json`` (ADR-0010 multi-runtime).

    Codex shares Claude's event names + record shape and accepts
    ``Bash``/``Edit``/``Write`` matchers natively, so the merge is the same
    :func:`_merge_hooks_record` Claude uses — only events Codex lacks
    (``Notification`` / ``SessionEnd``) are dropped with a warning. Writes a
    dedicated ``hooks.json`` rather than touching the user's ``config.toml``.
    """

    name: str = "codex_settings"

    def is_available(self, project_root: Path) -> bool:
        return (Path.home() / ".codex").is_dir() or (project_root / ".codex").is_dir()

    def target_file(self, project_root: Path, scope: str) -> Path | None:
        return _codex_target_file(project_root, scope)

    def canonical_source(self, project_root: Path) -> Path:
        return project_root / CANONICAL_SETTINGS_FILE

    def merge(
        self,
        existing: dict | None,
        contributions: dict,
    ) -> tuple[dict, list[str]]:
        filtered, drop_warnings = _filter_codex_events(contributions)
        merged, merge_warnings = _merge_hooks_record(existing, _stamp_status_markers(filtered))
        return merged, drop_warnings + merge_warnings


_register(CodexSettingsGenerator())


@dataclass
class GeminiSettingsGenerator:
    """Fan out canonical hooks to Gemini's ``settings.json`` (ADR-0010 multi-runtime).

    Gemini renames events and matches on its own tool names, so canonical
    hooks are remapped via :func:`_remap_for_gemini` (dropping anything that
    can't convert faithfully, with warnings) before the shared additive
    merge. The merge preserves the user's other ``settings.json`` keys and
    their own hook rules.
    """

    name: str = "gemini_settings"

    def is_available(self, project_root: Path) -> bool:
        return (Path.home() / ".gemini").is_dir() or (project_root / ".gemini").is_dir()

    def target_file(self, project_root: Path, scope: str) -> Path | None:
        return _gemini_target_file(project_root, scope)

    def canonical_source(self, project_root: Path) -> Path:
        return project_root / CANONICAL_SETTINGS_FILE

    def merge(
        self,
        existing: dict | None,
        contributions: dict,
    ) -> tuple[dict, list[str]]:
        remapped, drop_warnings = _remap_for_gemini(contributions)
        merged, merge_warnings = _merge_hooks_record(existing, remapped)
        return merged, drop_warnings + merge_warnings


_register(GeminiSettingsGenerator())


@dataclass
class KimiSettingsGenerator:
    """Fan out canonical hooks into Kimi CLI's ``config.toml``.

    Kimi uses TOML rather than JSON, so the shared settings sync path preserves
    the user's config and replaces only a memtomem-managed hooks block.
    """

    name: str = "kimi_settings"

    def is_available(self, project_root: Path) -> bool:
        return (Path.home() / ".kimi").is_dir() or (project_root / ".kimi").is_dir()

    def target_file(self, project_root: Path, scope: str) -> Path | None:
        return _kimi_target_file(project_root, scope)

    def canonical_source(self, project_root: Path) -> Path:
        return project_root / CANONICAL_SETTINGS_FILE

    def merge(
        self,
        existing: dict | None,
        contributions: dict,
    ) -> tuple[dict, list[str]]:
        existing_text = ""
        if existing and isinstance(existing.get(_KIMI_TOML_TEXT_KEY), str):
            existing_text = existing[_KIMI_TOML_TEXT_KEY]
        hooks_body, warnings = _render_kimi_hooks(contributions)
        merged_text = _replace_kimi_managed_block(existing_text, hooks_body)
        return {_KIMI_TOML_TEXT_KEY: merged_text}, warnings


_register(KimiSettingsGenerator())


# ── Helpers ─────────────────────────────────────────────────────────


def _is_under_project_root(target: Path, project_root: Path) -> bool:
    """``True`` when *target* would write under *project_root*.

    Uses ``resolve(strict=False)`` so a symlink at ``<project>/.claude``
    pointing into ``~/.claude/`` is treated as the host path it actually
    refers to (review item 4 on PR #484). ``strict=False`` keeps the call
    cheap when the target does not exist yet.
    """
    try:
        resolved_target = target.resolve(strict=False)
        resolved_root = project_root.resolve(strict=False)
    except (OSError, RuntimeError):
        return False
    return resolved_target.is_relative_to(resolved_root)


def host_write_targets(project_root: Path, *, scope: str) -> list[Path]:
    """Target paths a settings sync would write *outside* the project root.

    A target counts as a "host write" when its resolved path is not under
    the resolved project root — e.g. ``~/.claude/settings.json`` for the
    ``ClaudeSettingsGenerator``. Used to gate user-scope mutations behind
    confirmation in every front-end (CLI, MCP, Web): a stray
    ``mm context sync --include=settings`` from a worktree must not silently
    edit the real home directory.

    *scope* is the resolved ``hooks.target_scope`` (per ADR-0010 §3); it
    determines which tier each generator's ``target_file`` resolves to.
    Project-tier scopes (``project_shared`` / ``project_local``) yield an
    empty list because writes stay inside the project root.

    Generators whose runtime is unavailable (``is_available()`` is False) or
    that have no canonical source are skipped, so the list mirrors what
    :func:`generate_all_settings` would actually try to write.
    """
    pending: list[Path] = []
    for gen in SETTINGS_GENERATORS.values():
        if not gen.is_available(project_root):
            continue
        if not gen.canonical_source(project_root).exists():
            continue
        target = gen.target_file(project_root, scope)
        if target is None:
            continue  # no runtime fan-out for this (runtime, scope)
        if not _is_under_project_root(target, project_root):
            pending.append(target)
    return pending


def _safe_load_json(path: Path) -> dict | object:
    """Load JSON from *path*, returning :data:`_MALFORMED` on parse error,
    unreadable file, or a valid-JSON root that is not an object.

    The non-dict root case matters: a settings file containing ``[]`` /
    ``"text"`` / ``42`` used to flow into the merge layer and raise
    ``AttributeError`` deep inside it, aborting EVERY runtime instead of
    degrading to one target's ``status="error"`` (#1229).
    ``settings_doctor._load_settings_dict`` and the web ``_compare_hooks``
    already treat non-dict roots as malformed in-band — this brings the
    sync/diff engine in line.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return _MALFORMED
    return raw if isinstance(raw, dict) else _MALFORMED


def _read_with_mtime(path: Path) -> tuple[dict | None | object, int]:
    """Read JSON + capture mtime in nanoseconds for concurrent-write guard.

    Returns ``(None, 0)`` when *path* does not exist and
    ``(_MALFORMED, mtime_ns)`` when the file is not valid JSON. Nanosecond
    precision matches :mod:`memtomem.web.hot_reload` and detects
    sub-second writes that ``st_mtime`` (float seconds) misses.
    """
    if not path.is_file():
        return None, 0
    mtime_ns = path.stat().st_mtime_ns
    data = _safe_load_json(path)
    return data, mtime_ns


def _read_settings_target(name: str, path: Path) -> tuple[dict | None | object, int]:
    """Read a runtime settings target using that runtime's on-disk format."""
    if name != "kimi_settings":
        return _read_with_mtime(path)
    if not path.is_file():
        return None, 0
    mtime_ns = path.stat().st_mtime_ns
    try:
        text = path.read_text(encoding="utf-8")
        tomllib.loads(text)
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        # Unreadable / undecodable / unparseable: degrade to the same
        # per-target error status as malformed JSON targets (#1229).
        return _MALFORMED, mtime_ns
    return {_KIMI_TOML_TEXT_KEY: text}, mtime_ns


def _write_json(path: Path, data: dict) -> None:
    """Write *data* as formatted JSON with trailing newline (atomic)."""
    payload = json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False) + "\n"
    atomic_write_text(path, payload, mode=0o600)


def _write_settings_target(name: str, path: Path, data: dict) -> None:
    if name == "kimi_settings":
        payload = data.get(_KIMI_TOML_TEXT_KEY)
        if not isinstance(payload, str):
            raise TypeError("kimi_settings merge did not return TOML payload")
        atomic_write_text(path, payload, mode=0o600)
        return
    _write_json(path, data)


def _normalize_settings_target(name: str, data: dict) -> str:
    if name == "kimi_settings":
        payload = data.get(_KIMI_TOML_TEXT_KEY)
        return payload if isinstance(payload, str) else ""
    return json.dumps(data, sort_keys=True)


# ── Fan-out: canonical → runtimes ───────────────────────────────────


def generate_all_settings(
    project_root: Path,
    *,
    scope: str,
    allow_host_writes: bool = False,
) -> dict[str, SettingsSyncResult]:
    """Fan out ``.memtomem/settings.json`` to registered runtimes.

    *scope* is the resolved ``hooks.target_scope`` (per ADR-0010 §3) and
    is required keyword-only — every caller (CLI, MCP, Web) is expected
    to resolve it from its own config layer rather than have this
    function lazy-load ``Mem2MemConfig`` (which would trigger the
    auto-discover migration as a side effect, see
    ``feedback_doctor_no_migration_loader``).

    ``allow_host_writes`` defaults to ``False`` so any caller — CLI, MCP
    server, or Web route — that does not first prompt the user is
    automatically refused for targets that resolve outside *project_root*
    (e.g. ``~/.claude/settings.json`` when ``scope="user"``). Refused
    generators come back with ``status="needs_confirmation"`` and
    ``reason`` containing the host path; callers decide how to surface
    that. Passing ``allow_host_writes=True`` after acknowledgement (CLI
    ``--yes``, MCP ``allow_host_writes=True``, Web confirmation modal)
    restores the previous behavior. Project-scope writes (``scope`` is
    ``project_shared`` or ``project_local``) stay inside the project
    root and never trigger the gate.
    """
    results: dict[str, SettingsSyncResult] = {}

    # One shared deadline for ALL per-target sidecar-lock waits, so the whole
    # call — not each target — is bounded by ``_SETTINGS_LOCK_BUDGET_S``. With
    # N runtimes a per-target bound would let the cumulative wait reach
    # ``N × bound`` and overrun the web handler's 60s ``asyncio.timeout``,
    # re-opening the orphaned-worker window (#1145 review). Each target waits
    # only the time left on this budget.
    lock_deadline = time.monotonic() + _SETTINGS_LOCK_BUDGET_S

    for name, gen in SETTINGS_GENERATORS.items():
        if not gen.is_available(project_root):
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
                reason=f"{canonical_path} is not valid JSON (or not a JSON object). "
                f"Fix the file manually.",
            )
            continue
        contributions: dict = raw  # type: ignore[assignment]

        target_path = gen.target_file(project_root, scope)
        if target_path is None:
            results[name] = SettingsSyncResult(
                status="skipped",
                reason=f"{name} has no fan-out target for scope {scope!r}",
            )
            continue
        if not allow_host_writes and not _is_under_project_root(target_path, project_root):
            results[name] = SettingsSyncResult(
                status="needs_confirmation",
                reason=(
                    f"{target_path} is outside the project root; pass "
                    f"allow_host_writes=True after confirming with the user."
                ),
                target=target_path,
            )
            continue

        # Steps 0-4 run under a per-target cross-process lock so a separate
        # process (a CLI ``mm context sync`` / the MCP server) or the Web UI
        # cannot land a write between the mtime recheck (Step 3) and the atomic
        # rename inside ``_write_json`` (Step 4) and silently drop a concurrent
        # writer's hook rule (issue #1123 B3-3). This is the same sidecar-file
        # ``portalocker`` primitive skills uses; ``_write_json`` →
        # ``atomic_write_text`` is itself lock-free, so calling it under the held
        # lock does not self-deadlock. Exactly one lock is held per iteration and
        # released before the next target, so there is no cross-generator
        # lock-ordering cycle. The ``st_mtime_ns`` recheck is KEPT as a second
        # layer: it still catches a non-gateway direct disk edit that bypasses
        # the sidecar lock entirely.
        try:
            # Wait only the time left on the shared budget (not a fresh bound
            # per target). A 0.0 here means the budget is spent → one
            # non-blocking attempt: acquire iff instantly free, else abort.
            lock_timeout = max(0.0, lock_deadline - time.monotonic())
            with _file_lock(_lock_path_for(target_path), timeout=lock_timeout):
                # Step 0: re-read the canonical under the target lock (#1281).
                # The pre-lock read above only decides the no-write early
                # exits (skipped / error / needs_confirmation) WITHOUT
                # touching this target's sidecar lock — a no-canonical or
                # unconfirmed-host-write invocation must never create or
                # contend on a host lock file. The merge, however, must
                # derive its contributions from a read that happens-after
                # the lock acquire: ``settings-copy`` (cross-project per-hook
                # copy) writes the canonical strictly before the tier file
                # while holding both sidecar locks, so without this re-read
                # a sync that captured a stale canonical and then waited on
                # this lock would prune the freshly stamped, owned tier rule
                # as "no longer emitted". Same-literal statuses keep every
                # stable state's result shape unchanged.
                if not canonical_path.exists():
                    results[name] = SettingsSyncResult(
                        status="skipped",
                        reason="no canonical source (.memtomem/settings.json)",
                    )
                    continue
                raw_locked = _safe_load_json(canonical_path)
                if raw_locked is _MALFORMED:
                    results[name] = SettingsSyncResult(
                        status="error",
                        reason=f"{canonical_path} is not valid JSON (or not a JSON object). "
                        f"Fix the file manually.",
                    )
                    continue
                contributions = raw_locked  # type: ignore[assignment]

                # Step 1: read existing + capture mtime (ns)
                existing_raw, existing_mtime_ns = _read_settings_target(name, target_path)
                if existing_raw is _MALFORMED:
                    syntax_label = "TOML" if name == "kimi_settings" else "JSON"
                    results[name] = SettingsSyncResult(
                        status="error",
                        reason=f"{target_path} is not valid {syntax_label} "
                        f"(or not an object). Fix the file manually, then re-run "
                        f"`mm context sync --include=settings`.",
                    )
                    continue
                existing: dict | None = existing_raw  # type: ignore[assignment]

                # Step 2: merge in memory. A structurally unusable target
                # value (e.g. array-format hooks) degrades to this target's
                # error status — the file is never rewritten (#1229).
                try:
                    merged, warnings = gen.merge(existing, contributions)
                except MalformedSettingsError as exc:
                    results[name] = SettingsSyncResult(
                        status="error",
                        reason=f"{target_path}: {exc}. Fix the file manually, "
                        f"then re-run `mm context sync --include=settings`.",
                    )
                    continue

                # Step 3: mtime check (concurrent-write guard)
                if target_path.is_file() and target_path.stat().st_mtime_ns != existing_mtime_ns:
                    results[name] = SettingsSyncResult(
                        status="aborted",
                        reason=f"{target_path} was modified by another "
                        f"process during merge. Re-run "
                        f"`mm context sync --include=settings` to retry.",
                    )
                    continue

                # Step 4: write
                _write_settings_target(name, target_path, merged)
                results[name] = SettingsSyncResult(
                    status="ok",
                    warnings=warnings,
                    target=target_path,
                )
        except TimeoutError:
            # Another process held the lock past the shared budget. Abort this
            # target cleanly so the (possibly thread-offloaded) caller never
            # blocks indefinitely and never orphans a late writer (#1145 review).
            results[name] = SettingsSyncResult(
                status="aborted",
                reason=f"{target_path}: another process held the lock past the "
                f"{_SETTINGS_LOCK_BUDGET_S:g}s acquisition budget. Re-run "
                f"`mm context sync --include=settings` to retry.",
            )
            continue

    return results


def diff_settings(
    project_root: Path,
    *,
    scope: str,
) -> dict[str, SettingsSyncResult]:
    """Dry-run: compute what :func:`generate_all_settings` would do.

    *scope* is the resolved ``hooks.target_scope`` (ADR-0010 §3); see
    :func:`generate_all_settings` for the rationale on why callers pass
    this in rather than having it lazy-loaded here.
    """
    results: dict[str, SettingsSyncResult] = {}

    for name, gen in SETTINGS_GENERATORS.items():
        if not gen.is_available(project_root):
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
                reason=f"{canonical_path} is not valid JSON (or not a JSON object)",
            )
            continue
        contributions: dict = raw  # type: ignore[assignment]

        target_path = gen.target_file(project_root, scope)
        if target_path is None:
            results[name] = SettingsSyncResult(
                status="skipped",
                reason=f"{name} has no fan-out target for scope {scope!r}",
            )
            continue
        existing_raw, _ = _read_settings_target(name, target_path)
        if existing_raw is _MALFORMED:
            syntax_label = "TOML" if name == "kimi_settings" else "JSON"
            results[name] = SettingsSyncResult(
                status="error",
                reason=f"{target_path} is not valid {syntax_label} (or not an object)",
            )
            continue
        existing: dict | None = existing_raw  # type: ignore[assignment]

        try:
            merged, warnings = gen.merge(existing, contributions)
        except MalformedSettingsError as exc:
            results[name] = SettingsSyncResult(
                status="error",
                reason=f"{target_path}: {exc}",
            )
            continue

        if existing is not None:
            existing_norm = _normalize_settings_target(name, existing)
            merged_norm = _normalize_settings_target(name, merged)
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
    "CodexSettingsGenerator",
    "GeminiSettingsGenerator",
    "KimiSettingsGenerator",
    "MalformedSettingsError",
    "SETTINGS_GENERATORS",
    "SettingsGenerator",
    "SettingsSyncResult",
    "resolve_scope_path",
    "diff_settings",
    "generate_all_settings",
    "host_write_targets",
    "sync_all_settings",
]
