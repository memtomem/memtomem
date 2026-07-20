"""Name validation for context-gateway agent/command/skill identifiers.

The canonical ``name:`` frontmatter on agents and commands, and the directory
name of a canonical skill, are interpolated into a target path such as
``.claude/agents/<name>.md`` or ``.codex/agents/<name>.toml``. Without
validation, ``name: ../../etc/passwd`` would escape the target root. The same
field is also emitted to log lines, so CR/LF injection is a log-injection
vector.

Canonical files *can* be authored by the user directly, but they are also
populated by reverse-import (``extract_agents_to_canonical``) and by MCP
``mem_context_*`` tools whose arguments are LLM-driven — so prompt injection
or plain model mistakes can produce hostile names even in single-user flows.

Validation happens at the dataclass boundary: parsers raise on invalid
input, and fan-out generators route those errors into ``SyncResult.skipped``
rather than aborting.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

__all__ = [
    "GENERATOR_VENDOR",
    "InvalidNameError",
    "Layout",
    "OVERRIDE_FORMATS",
    "internal_artifact_owner",
    "is_internal_artifact_dir",
    "override_vendors",
    "renderable_vendors",
    "validate_name",
]

# ADR-0008 PR-C: agents/commands canonical may live in either the legacy
# flat layout (``<name>.md``) or the directory layout
# (``<name>/agent.md`` / ``<name>/command.md``). Hoisted here so both
# ``agents.py`` and ``commands.py`` share one type definition rather than
# cross-importing.
Layout = Literal["flat", "dir"]

# ``\Z``, never ``$``, in both name patterns here — but for different reasons,
# and only one of them was a live defect.
#
# Python's ``$`` also matches immediately before a trailing newline, and a
# newline is a legal POSIX filename character. Whether that is exploitable
# depends entirely on the CALL SITE: ``.fullmatch()`` still requires the whole
# string, so the trailing newline is left unconsumed and rejected regardless of
# the anchor, while ``.match()`` stops at the assertion and accepts it.
#
# ``_NAME_RE`` is used via ``fullmatch`` below, so ``$`` never actually admitted
# ``"skill\n"``; ``\Z`` here is belt-and-braces so the pattern stays correct if
# a future caller reaches for ``match``. ``_INTERNAL_DIR_RE`` IS matched with
# ``.match()``, where ``$`` classified ``.old-<name>-<pid>-<rand>.tmp\n`` as our
# own leftover and handed it to the reaper — a name we never generate, so
# anything wearing one belongs to somebody else.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+\Z")
_MAX_LEN = 64

# ADR-0008 PR-C: per-(asset_type, vendor) override file extension. The
# tuple is ``(alias, extension)``; ``alias`` is reserved for v2 multi-vendor
# (e.g. cursor sharing claude's surface) and ``extension`` is what
# ``override.resolve`` joins to ``<vendor>.<ext>``.
#
# v1 covers Claude / Gemini / Codex across skills, agents, commands, plus
# Kimi for skills and agents (Kimi has no commands surface). The
# ``("commands", "codex")`` row is a placeholder — there is no
# ``codex_commands`` generator yet (Codex slash prompts are user-scope and
# upstream-deprecated). The matrix entry stays for the day Codex commands
# ship; until then, ``render_seed_bytes`` raises ``NotImplementedError``
# for ``("commands", "codex")``.
OVERRIDE_FORMATS: dict[tuple[str, str], tuple[str, str]] = {
    ("skills", "claude"): ("claude", "md"),
    ("skills", "gemini"): ("gemini", "md"),
    ("skills", "codex"): ("codex", "md"),
    ("skills", "kimi"): ("kimi", "md"),
    ("agents", "claude"): ("claude", "md"),
    ("agents", "gemini"): ("gemini", "md"),
    ("agents", "codex"): ("codex", "toml"),
    ("agents", "kimi"): ("kimi", "yaml"),
    ("commands", "claude"): ("claude", "md"),
    ("commands", "gemini"): ("gemini", "toml"),
    ("commands", "codex"): ("codex", "md"),
}


def override_vendors(asset_type: str) -> list[str]:
    """Vendors with a registered override format for ``asset_type``.

    Returned in :data:`OVERRIDE_FORMATS` insertion order
    (``claude → gemini → codex → kimi``), the deterministic vendor order used
    across fan-out. This is the single source of truth for the ``mm wiki``
    ``--vendor`` Choice, so the CLI can never drift from the matrix — e.g.
    kimi is offered for skills/agents but not commands, which have no kimi row.

    Placeholder rows (``("commands", "codex")``, whose ``render_seed_bytes``
    raises :class:`NotImplementedError`) are still returned: they are valid
    *selections* that fail loudly at render time, matching the behavior from
    when these Choices were hardcoded to ``["claude", "gemini", "codex"]``.
    """
    return [vendor for (at, vendor) in OVERRIDE_FORMATS if at == asset_type]


# Maps generator name (`gen.name` — e.g. ``"claude_skills"``) to the
# vendor key shared with :data:`OVERRIDE_FORMATS`. Centralizing here so
# ``skills.py`` / ``agents.py`` / ``commands.py`` fan-out and PR-D's
# lint/status all reuse the same source of truth. Naming pattern is
# ``<vendor>_<asset_type>`` across the codebase.
GENERATOR_VENDOR: dict[str, str] = {
    "claude_skills": "claude",
    "gemini_skills": "gemini",
    "codex_skills": "codex",
    "kimi_skills": "kimi",
    "claude_agents": "claude",
    "gemini_agents": "gemini",
    "codex_agents": "codex",
    "kimi_agents": "kimi",
    "claude_commands": "claude",
    "gemini_commands": "gemini",
}


def renderable_vendors(asset_type: str) -> list[str]:
    """Subset of :func:`override_vendors` whose override can actually render.

    A vendor is renderable iff a ``<vendor>_<asset_type>`` generator exists in
    :data:`GENERATOR_VENDOR` — the same membership ``render_seed_bytes`` checks
    before raising :class:`NotImplementedError`. So this drops exactly the
    placeholder rows (today only ``("commands", "codex")``) that
    ``override_vendors`` still returns.

    The web wiki browser uses it to disable diff/lint controls for a vendor
    that would only ever fail at render time; the CLI deliberately keeps
    offering the full ``override_vendors`` set (the placeholder fails loudly,
    matching the historical hardcoded ``--vendor`` choices).
    """
    return [v for v in override_vendors(asset_type) if f"{v}_{asset_type}" in GENERATOR_VENDOR]


class InvalidNameError(ValueError):
    """Raised when a context-gateway name fails validation."""


# Skill sync stages into ``.staging-<name>-<pid>-<rand>.tmp`` and moves the
# old tree aside as ``.old-<name>-<pid>-<rand>.tmp`` (``skills._stage_skill``
# / ``skills._promote_staging``; ``<pid>`` is decimal, ``<rand>`` is
# ``token_hex(3)`` = 6 hex chars). A SIGKILL between those steps leaves a
# full skill tree (including SKILL.md) behind, and the composite name passes
# :func:`validate_name` — so every discovery loop must skip these explicitly
# or the leftover shows up as a phantom diff row and even round-trips through
# extract back into canonical (#1229). The pattern pins the EXACT generated
# shape including the pid+rand suffix: ``validate_name`` accepts dot-prefixed
# ``.tmp`` names, so a looser ``.staging-*.tmp`` match would silently hide —
# and let the sync-time reaper delete — a legitimately named user skill like
# ``.staging-notes.tmp`` (Codex review on #1229).
_INTERNAL_DIR_RE = re.compile(r"^\.(?:staging|old)-(?P<owner>.+)-\d+-[0-9a-f]{6}\.tmp\Z")


def internal_artifact_owner(name: str) -> str | None:
    """The destination name an internal artifact belongs to, or ``None``.

    Same predicate as :func:`is_internal_artifact_dir`, but it also answers
    *whose* leftover this is — which a reaper must know, because the
    destination name is not recoverable from a prefix match. ``.old-foo-*``
    matches ``.old-foo-bar-<pid>-<rand>.tmp``, so a reaper working from a glob
    deletes the skill ``foo-bar``'s in-flight trees while holding only
    ``foo``'s lock, and hyphenated skill names are the norm.

    The split is unambiguous because the suffix is both **anchored to the end**
    (``-<decimal pid>-<6 hex>`` then a literal ``.tmp`` and ``\\Z``) and matched
    after a **greedy** ``.+``: the match must consume the whole name and the
    owner takes as much of it as it can, so the suffix is necessarily the LAST
    pid+rand run. So ``.old-foo-bar-123-abc123.tmp`` parses as ``foo-bar``,
    never as ``foo``, and ``.old-foo-123-abc123-456-def789.tmp`` as
    ``foo-123-abc123`` — only producible by a skill genuinely named that, since
    a leftover carries exactly one pid+rand.

    The two properties are **independently sufficient** on that input (dropping
    either alone still parses it correctly; dropping both yields ``foo``), so
    neither is "the" reason on its own. Keep both: the anchor is what rejects
    non-leftover names outright, and greediness is what keeps the parse correct
    if the anchor is ever loosened.
    """
    match = _INTERNAL_DIR_RE.match(name)
    return match.group("owner") if match else None


def is_internal_artifact_dir(name: str) -> bool:
    """True for context-gateway internal staging/move-aside directory names.

    These are *our own* crash artifacts, not user content — discovery loops
    (canonical listing, runtime scans, extract, detect, status) skip them
    silently rather than warning about an invalid name, and
    ``skills._reap_stale_internal_dirs`` deletes them under the destination
    sidecar lock. Both sides MUST use this one predicate so "hidden" and
    "deletable" can never drift apart.

    Hiding is name-shape-only and stays that way: a leftover belonging to
    *another* destination must still be hidden from discovery. Deleting is
    the narrower question, and that is what :func:`internal_artifact_owner`
    is for — which is also why this delegates to it rather than running its
    own match. Two independent matches would be two things to keep in step;
    one makes "hidden" and "deletable" agree by construction.
    """
    return internal_artifact_owner(name) is not None


def validate_name(s: object, *, kind: str = "name") -> str:
    """Return *s* unchanged if it is a valid context-gateway identifier.

    Enforces:

    * type is ``str``,
    * non-empty after ``strip()``,
    * ``1 <= len(s) <= 64``,
    * matches ``^[A-Za-z0-9._-]+$`` (no slash, backslash, null, control chars),
    * not ``"."`` or ``".."`` (path-traversal tokens allowed by the regex),
    * does not start with ``-`` (would collide with CLI flag parsing),
    * ``Path(s).name == s`` (belt-and-suspenders against platform-specific
      path parsing on Windows / weird separators).
    """
    if not isinstance(s, str):
        raise InvalidNameError(f"invalid {kind}: expected str, got {type(s).__name__}")
    if not s or not s.strip():
        raise InvalidNameError(f"invalid {kind} {s!r}: empty")
    if len(s) > _MAX_LEN:
        raise InvalidNameError(f"invalid {kind} {s!r}: length {len(s)} exceeds {_MAX_LEN}")
    if s in (".", ".."):
        raise InvalidNameError(f"invalid {kind} {s!r}: reserved path token")
    if s.startswith("-"):
        raise InvalidNameError(f"invalid {kind} {s!r}: leading dash")
    if not _NAME_RE.fullmatch(s):
        raise InvalidNameError(
            f"invalid {kind} {s!r}: must match [A-Za-z0-9._-]+ "
            f"(no slash / backslash / whitespace / control chars)"
        )
    if Path(s).name != s:
        raise InvalidNameError(f"invalid {kind} {s!r}: contains path separator")
    return s
