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
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from memtomem._runtime_paths import scrub_text
from memtomem.context.error_redact import SECRET_REDACTED_MARKER, redact_secret_value
from memtomem.privacy import scan as _privacy_scan
from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    MalformedHookMatcher,
    _find_nonstring_matchers,
    _normalize_matcher,
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


@dataclass(frozen=True)
class UnportableHookCommand:
    """One hook command containing a home-rooted absolute path literal."""

    source: str
    path: Path
    event: str
    rule_index: int
    hook_index: int
    command: str
    unportable_literal: str
    tier: str | None = None


@dataclass(frozen=True)
class UnscannedSettingsFile:
    """A settings file that exists but could not be read as a JSON object."""

    source: str
    path: Path
    reason: str
    tier: str | None = None


def _read_settings(path: Path) -> tuple[dict | None, str | None]:
    """Read a settings.json file as ``(data, failure_reason)``.

    ``(None, None)`` means nothing is at the path. ``failure_reason`` is one of
    ``"broken symlink"``, ``"not a file"``, ``"unreadable"``, ``"invalid JSON"``
    or ``"not a JSON object"`` when something is there but yields no dict.
    """
    try:
        st = path.stat()
    except (FileNotFoundError, NotADirectoryError):
        try:
            path.lstat()
        except (FileNotFoundError, NotADirectoryError):
            return None, None
        except OSError:
            return None, "unreadable"
        return None, "broken symlink"
    except OSError:
        # e.g. a parent directory without search permission: present or not, unverified.
        return None, "unreadable"
    if not stat.S_ISREG(st.st_mode):
        return None, "not a file"
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None, "unreadable"
    try:
        raw = json.loads(text)
    except json.JSONDecodeError:
        return None, "invalid JSON"
    if not isinstance(raw, dict):
        return None, "not a JSON object"
    return raw, None


def _load_settings_dict(path: Path) -> dict | None:
    """Read a settings.json file; return ``None`` on any read failure.

    Self-contained JSON load so this module doesn't depend on
    :func:`memtomem.context.settings._safe_load_json` /
    :data:`memtomem.context.settings._MALFORMED` (private to that
    module). Returns ``None`` when the file is missing, unreadable, or
    not valid JSON, **or** when the parsed root is not a dict —
    callers can treat all three the same way (skip this tier).

    Deliberately not :func:`_read_settings`: that reader also absorbs
    metadata-probe failures, which would silently hide an inaccessible tier
    from callers (Web duplicate checks) that do not report unscanned files.
    """
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    return raw


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
            if matcher is None:
                continue
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


def find_malformed_matchers(project_root: Path) -> list[MalformedHookMatcher]:
    """Find non-string matchers in canonical settings and all tier files.

    This is the standalone doctor's companion diagnostic. Duplicate detection
    keeps its historical return type, while malformed rules are reported
    separately and never participate in signature comparison.
    """
    findings: list[MalformedHookMatcher] = []
    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    canonical = _load_settings_dict(canonical_path)
    if canonical is not None:
        findings.extend(
            _find_nonstring_matchers(
                canonical.get("hooks", {}),
                source="canonical",
                path=canonical_path,
            )
        )

    seen_paths: set[Path] = set()
    for scope in ALL_SCOPES:
        tier_path = resolve_scope_path(project_root, scope)
        resolved = _resolved(tier_path)
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        tier = _load_settings_dict(tier_path)
        if tier is None:
            continue
        findings.extend(
            _find_nonstring_matchers(
                tier.get("hooks", {}),
                source="tier",
                tier=scope,
                path=tier_path,
            )
        )
    return findings


def format_malformed_warning(finding: MalformedHookMatcher) -> str:
    """Human-readable warning string for one malformed matcher row.

    The sibling of :func:`format_warning` for the malformed axis, shared by
    the CLI sync surface and the MCP settings branches so both agree with
    the standalone doctor's wording. Assumes the ``source`` vocabulary
    :func:`find_malformed_matchers` produces (``canonical`` / ``tier``);
    copy and migrate build their own descriptive sources and render through
    :class:`~memtomem.context.settings.MalformedHookMatcherError` instead.

    The copy/migrate hint is deliberately conditional ("may cause a related
    …"): those operations inspect only the files they touch, so a malformed
    rule in a third tier does not block an unrelated migration.

    ``event`` is a dict key read verbatim out of the caller's settings file, so
    a secret-shaped one is scrubbed here rather than at a call site (#2030):
    the MCP surface ships this string to the calling agent's transcript and on
    to the model provider, and redacting only there would desync the wording
    this function exists to keep identical across surfaces. Non-secret events —
    every real hook event — are untouched.
    """
    location = "canonical settings" if finding.tier is None else f"{finding.tier} tier"
    safe_path = scrub_text(str(finding.path))
    safe_event = scrub_text(redact_secret_value(finding.event))
    safe_matcher = scrub_text(finding.matcher_type)
    return (
        f"hook rule with a non-string matcher ({safe_matcher}) in the "
        f"{location} ({safe_path}), event '{safe_event}' rule "
        f"#{finding.rule_index}; `matcher` must be a string — omit it for "
        f"match-all, or quote the value. It is ignored by hook matching until "
        f"fixed, and may cause a related `mm context settings-copy` / "
        f"`settings-migrate` to refuse to run."
    )


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


# Home roots: /home/<user>, /Users/<user>, <drive>:\Users\<user> (either slash, any case).
# <user> stops at a path separator, whitespace, quote, or shell metacharacter, so
# /home/$USER/x and /home/*/x name no user and are not roots.
_HOME_ROOT_RE = re.compile(
    r"(?:[A-Za-z]:[\\/]+users[\\/]+|/(?:home|users)/+)"
    r"(?P<user>[^\s/\\\"'`$;&|<>(){}\[\]*?:,=]+)",
    re.IGNORECASE,
)
# A root right after one of these (or an alphanumeric) is part of a longer word
# or path: /opt/home/x, ./home/x, ~/home/x, $HOME/home/x, https://host/home/x.
_BLOCKED_BEFORE = frozenset("_./\\~")
_HOST_HOME_END = r"(?![\w.-])"


def _root_is_embedded(command: str, start: int) -> bool:
    if start == 0:
        return False
    prev = command[start - 1]
    if not (prev.isalnum() or prev in _BLOCKED_BEFORE):
        return False
    # file:///home/alice still names a local path; the URI's own slashes do not embed it.
    return not (prev == "/" and command[max(0, start - 7) : start].lower() == "file://")


def _host_home_re(home: str) -> re.Pattern[str]:
    """Match a host home as written; Windows homes also in any slash or case spelling."""
    unc = home[:2] in ("\\\\", "//")
    if not (unc or home[1:2] == ":"):
        return re.compile(re.escape(home) + _HOST_HOME_END)
    parts = [re.escape(part) for part in re.split(r"[\\/]+", home) if part]
    lead = r"[\\/]{2}" if unc else ""
    return re.compile(lead + r"[\\/]+".join(parts) + _HOST_HOME_END, re.IGNORECASE)


def _collect_roots(pattern: re.Pattern[str], command: str, found: list[str]) -> None:
    pos = 0
    while (match := pattern.search(command, pos)) is not None:
        start = match.start()
        if _root_is_embedded(command, start):
            # ``bin:/Users/bob`` in a PATH list is a word then a colon, not drive
            # ``n:`` -- retry after the colon. ``./x/C:/Users/bob`` stays embedded.
            is_drive = command[start + 1 : start + 2] == ":"
            retry = is_drive and command[start - 1] not in _BLOCKED_BEFORE
            pos = start + 2 if retry else match.end()
            continue
        if match.groupdict().get("user") not in (".", ".."):
            found.append(match.group(0))
        pos = match.end()


def _extract_unportable_literals(command: str, host_homes: tuple[str, ...]) -> list[str]:
    """Return the home roots written in a hook command, as written, deduplicated.

    This is a lexical scan, not a shell parser, and the warning it feeds is
    advisory (#2407). A root counts wherever its text appears -- inside quotes,
    heredocs, ``${VAR:-default}`` fallbacks, comments, or inert data alike --
    unless the character before it makes it part of a longer word or path.
    Reported literals are roots (``/home/alice``), not full paths.

    No guarantee either way for paths assembled at run time
    (``DIR=/home; $DIR/alice``), roots split by a line continuation, doubled
    leading slashes, or backslash-escaped slashes. Known false positives:
    HOME-anchored concatenations such as ``"$HOME"/home/bob``, and remote specs
    such as ``host:/home/bob``. A clean result does not mean a hook is portable.

    ``host_homes`` entries that are not already standard roots (``/srv/jenkins``,
    ``D:\\Profiles\\alice``) are matched too.
    """
    found: list[str] = []
    _collect_roots(_HOME_ROOT_RE, command, found)
    for home in host_homes:
        home = home.rstrip("/\\")
        if home and not _HOME_ROOT_RE.fullmatch(home):
            _collect_roots(_host_home_re(home), command, found)
    return list(dict.fromkeys(found))


def _get_host_homes() -> tuple[str, ...]:
    """Return local host home directory representations for matching."""
    homes: list[str] = []
    try:
        home = str(Path.home())
        if home:
            homes.append(home)
            try:
                resolved = str(Path.home().resolve())
                if resolved and resolved != home:
                    homes.append(resolved)
            except (OSError, RuntimeError):
                pass
    except Exception:
        pass
    return tuple(homes)


def _iter_unportable_commands_in_hooks(
    hooks_record: object,
    *,
    source: str,
    path: Path,
    tier: str | None = None,
    host_homes: tuple[str, ...],
) -> list[UnportableHookCommand]:
    findings: list[UnportableHookCommand] = []
    if not isinstance(hooks_record, dict):
        return findings
    for event, rules in hooks_record.items():
        if not isinstance(event, str) or not isinstance(rules, list):
            continue
        for rule_idx, rule in enumerate(rules):
            if not isinstance(rule, dict):
                continue
            inner = rule.get("hooks", [])
            if not isinstance(inner, list):
                continue
            for hook_idx, entry in enumerate(inner):
                if not isinstance(entry, dict):
                    continue
                cmd = entry.get("command")
                if not isinstance(cmd, str):
                    continue
                literals = _extract_unportable_literals(cmd, host_homes)
                for lit in literals:
                    findings.append(
                        UnportableHookCommand(
                            source=source,
                            path=path,
                            event=event,
                            rule_index=rule_idx,
                            hook_index=hook_idx,
                            command=cmd,
                            unportable_literal=lit,
                            tier=tier,
                        )
                    )
    return findings


def _iter_scan_targets(
    project_root: Path,
) -> Iterator[tuple[str, str | None, Path, dict | None, str | None]]:
    """Yield ``(source, tier, path, data, failure_reason)`` for every settings file.

    Canonical settings first, then each tier, skipping a tier path that
    resolves to one already visited (symlinked tiers).
    """
    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    yield ("canonical", None, canonical_path, *_read_settings(canonical_path))

    seen_paths: set[Path] = set()
    for scope in ALL_SCOPES:
        tier_path = resolve_scope_path(project_root, scope)
        resolved = _resolved(tier_path)
        if resolved in seen_paths:
            continue
        seen_paths.add(resolved)
        yield ("tier", scope, tier_path, *_read_settings(tier_path))


def find_unportable_hook_commands(
    project_root: Path,
    *,
    host_homes: tuple[str, ...] | None = None,
) -> list[UnportableHookCommand]:
    """Find hook commands containing home-rooted absolute path literals.

    Checks canonical settings and all tier files (user, project_shared, project_local).
    Skips missing or unparseable files -- :func:`find_unscanned_settings_files`
    reports the unparseable ones. Dedupes symlinked tier paths.
    """
    if host_homes is None:
        host_homes = _get_host_homes()

    findings: list[UnportableHookCommand] = []
    for source, tier, path, data, _reason in _iter_scan_targets(project_root):
        if data is None:
            continue
        findings.extend(
            _iter_unportable_commands_in_hooks(
                data.get("hooks", {}),
                source=source,
                tier=tier,
                path=path,
                host_homes=host_homes,
            )
        )
    return findings


def find_unscanned_settings_files(project_root: Path) -> list[UnscannedSettingsFile]:
    """Settings files that exist but could not be read, so no check covered them.

    A missing file is not reported: there is nothing to scan. Advisory, like
    the unportable-command check -- a clean doctor run must not hide that a
    file was skipped.
    """
    return [
        UnscannedSettingsFile(source=source, path=path, reason=reason, tier=tier)
        for source, tier, path, _data, reason in _iter_scan_targets(project_root)
        if reason is not None
    ]


def format_unscanned_settings_warning(unscanned: UnscannedSettingsFile) -> str:
    """Human-readable warning string for one settings file no check could read."""
    location = "canonical settings" if unscanned.tier is None else f"{unscanned.tier} tier"
    return (
        f"{location} file ({scrub_text(str(unscanned.path))}) was not checked: "
        f"{unscanned.reason}. Hook duplicates, matchers and commands in it are unverified."
    )


def redact_unportable_command_fields(command: str, literal: str) -> tuple[str, str]:
    """Redact hook command and its derived path literal for safe display.

    If the source command has a secret shape (e.g. from an assignment like
    ``API_KEY=...``) or has already been redacted, the derived literal is also
    redacted to `<redacted: secret-shape>` to avoid leaking credential values
    whose context was stripped during path extraction.
    """
    safe_command = scrub_text(redact_secret_value(command))
    if safe_command == SECRET_REDACTED_MARKER or _privacy_scan(command):
        return SECRET_REDACTED_MARKER, SECRET_REDACTED_MARKER
    safe_literal = scrub_text(redact_secret_value(literal))
    return safe_command, safe_literal


def format_unportable_command_warning(finding: UnportableHookCommand) -> str:
    """Human-readable warning string for one unportable hook command row.

    Names the offending location, event, rule and hook indices, the literal
    and the command, and recommends portable alternatives ($HOME, ~, or relative).
    Event, literal, and command are redacted if secret-shaped and scrubbed of
    terminal control characters.
    """
    location = "canonical settings" if finding.tier is None else f"{finding.tier} tier"
    safe_path = scrub_text(str(finding.path))
    safe_event = scrub_text(redact_secret_value(finding.event))
    safe_command, safe_literal = redact_unportable_command_fields(
        finding.command, finding.unportable_literal
    )
    return (
        f"hook command in {location} ({safe_path}), event "
        f"'{safe_event}' rule #{finding.rule_index} hook "
        f"#{finding.hook_index} contains non-portable absolute home path "
        f"'{safe_literal}': "
        f"'{safe_command}'; rewrite with "
        f"$HOME, ~, or a repo-relative path so settings can be safely shared "
        f"across different machines and users."
    )
