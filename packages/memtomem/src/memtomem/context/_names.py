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
    "INTERNAL_ARTIFACT_KINDS",
    "InvalidNameError",
    "Layout",
    "OVERRIDE_FORMATS",
    "REAPABLE_INTERNAL_ARTIFACT_KINDS",
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
#
# The cross-store transfer engine is the third producer, and it stages under a
# kind of its own: ``.migrate-<name>-<pid>-<rand>.tmp``
# (``migrate._stage_move`` / ``transfer._stage_copy``, both through
# ``migrate.transfer_staging_path``). ``mm context move`` / ``copy`` crashing
# between stage and promote leaves one of those in the DESTINATION store, and
# until #2304 no consumer of this predicate matched it (#2304).
#
# The kinds are a named constant and the pattern is built from it, because the
# reaper scans by kind (``skills._iter_own_internal_dirs`` globs
# ``.<kind>-<dst>-*.tmp``) while everything else classifies by this pattern.
# Deriving both from named constants is what keeps "hidden" and "deletable"
# from drifting by accident: a kind hand-spelled into the pattern alone would
# be invisible to discovery and uncollectable, and one added to the scan alone
# would be deleted by a reaper that cannot prove it owns it. ``migrate`` IS
# hidden-and-uncollectable, but by declaration rather than by omission — it is
# absent from the reapable tuple on purpose, for the reason below.
#
# ``re.escape`` on each kind rather than raw interpolation: the whole point of
# the constant is that a third kind is safe to add in one place, and a raw join
# would make that true only for the alphanumeric ones.
#
# **Classified is not the same as deletable, and the two tuples are why.**
# Hiding is safe for every transient: a leftover belonging to anyone is not a
# canonical artifact. Deleting is not. The skills reaper's licence to remove a
# ``.staging-*`` rests on "a staging tree is a copy whose source is still on
# disk" (``skills._recover_and_reap_internal_dirs``) — true for the skills
# copier, FALSE for a transfer move: ``migrate._stage_move`` renames the source
# into staging on the same filesystem, so between that rename and the promote
# the staging tree is the ONLY copy of the artifact. A reaper that swept it
# would turn a recoverable crash into data loss, which is exactly the ADR-0030
# §10 rule that keeps ``.old-*`` alive while its canonical is absent. So
# ``migrate`` is classified (hidden everywhere) and deliberately left out of
# the reapable set; a leaked directory is cheap, a deleted canonical is not.
# Reclaiming those leftovers needs a copy-vs-move provenance the name does not
# carry today (#2304).
INTERNAL_ARTIFACT_KINDS: tuple[str, ...] = ("staging", "old", "migrate")

# The subset the skills reaper may DELETE. A strict subset of
# :data:`INTERNAL_ARTIFACT_KINDS` by construction (pinned by test) — every
# reapable kind must first be a classified one, or the reaper would delete
# something discovery still shows.
REAPABLE_INTERNAL_ARTIFACT_KINDS: tuple[str, ...] = ("staging", "old")

# Random-suffix width, per kind, as a regex quantifier. The widths are NOT the
# same, because the producers never agreed on one: the skills sync and the
# directory swap allocate ``token_hex(3)`` (6 hex), the transfer engine
# ``token_hex(4)`` (8 hex). One shared width cannot serve both: pinning six
# classified none of the eight-hex leftovers already on disk from released
# versions, and narrowing the transfer suffix to match would have cut its
# collision entropy from 32 bits to 24 on a path whose collision handler
# DELETES the colliding entry (#2304).
#
# Every kind is pinned to EXACTLY the width it generates — the #1229 rule
# applied per kind rather than once globally. Being excluded from the reapable
# set buys a kind no slack here, because hiding is not a harmless false
# positive: a wrongly classified directory vanishes from canonical listing and
# resolution, from status, from snapshots, and from the sync fan-out. It is
# merely not deleted while it disappears. A user directory that happens to look
# like transfer staging must not be swallowed just because we would never
# remove it.
#
# So ``migrate`` is eight hex and nothing else. No released version has
# produced a six- or seven-hex ``.migrate-*`` name, and a range would only
# widen what we swallow without recognizing one more real leftover. (The
# reproduction on #2304 hand-wrote a six-hex name; the engine's own name is
# what matters, and it is eight.)
_KIND_RAND_HEX: dict[str, str] = {
    "staging": "{6}",
    "old": "{6}",
    "migrate": "{8}",
}

# One anchored pattern per kind rather than one alternation, because the width
# is per-kind. Built by indexing :data:`_KIND_RAND_HEX` with every classified
# kind, so a kind added without a declared width raises ``KeyError`` at import
# rather than silently matching nothing.
_INTERNAL_DIR_RES: tuple[re.Pattern[str], ...] = tuple(
    re.compile(rf"^\.{re.escape(kind)}-(?P<owner>.+)-\d+-[0-9a-f]{_KIND_RAND_HEX[kind]}\.tmp\Z")
    for kind in INTERNAL_ARTIFACT_KINDS
)


def internal_artifact_owner(name: str) -> str | None:
    """The destination name an internal artifact belongs to, or ``None``.

    Same predicate as :func:`is_internal_artifact_dir`, but it also answers
    *whose* leftover this is — which a reaper must know, because the
    destination name is not recoverable from a prefix match. ``.old-foo-*``
    matches ``.old-foo-bar-<pid>-<rand>.tmp``, so a reaper working from a glob
    deletes the skill ``foo-bar``'s in-flight trees while holding only
    ``foo``'s lock, and hyphenated skill names are the norm.

    The split is unambiguous because the suffix is both **anchored to the end**
    (``-<decimal pid>-<the kind's hex width>`` — six for ``staging`` / ``old``,
    eight for ``migrate``, see :data:`_KIND_RAND_HEX` — then a literal ``.tmp``
    and ``\\Z``) and matched
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
    for pattern in _INTERNAL_DIR_RES:
        match = pattern.match(name)
        if match:
            return match.group("owner")
    return None


def is_internal_artifact_dir(name: str) -> bool:
    """True for context-gateway internal staging/move-aside directory names.

    These are *our own* crash artifacts, not user content — discovery loops
    (canonical listing, runtime scans, extract, detect, status) skip them
    silently rather than warning about an invalid name, and
    ``skills._recover_and_reap_internal_dirs`` deletes the REAPABLE ones under
    the destination sidecar lock. Both sides read from the same kind tuples, so
    "hidden" and "deletable" cannot drift apart — but they are deliberately not
    the same set: this predicate answers for every kind in
    :data:`INTERNAL_ARTIFACT_KINDS`, while the reaper is scoped to
    :data:`REAPABLE_INTERNAL_ARTIFACT_KINDS`. Transfer staging (``migrate``) is
    hidden and never reaped, because a same-filesystem move leaves it holding
    the only copy of the artifact.

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
