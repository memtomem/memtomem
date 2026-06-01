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
    "validate_name",
]

# ADR-0008 PR-C: agents/commands canonical may live in either the legacy
# flat layout (``<name>.md``) or the directory layout
# (``<name>/agent.md`` / ``<name>/command.md``). Hoisted here so both
# ``agents.py`` and ``commands.py`` share one type definition rather than
# cross-importing.
Layout = Literal["flat", "dir"]

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_LEN = 64

# ADR-0008 PR-C: per-(asset_type, vendor) override file extension. The
# tuple is ``(alias, extension)``; ``alias`` is reserved for v2 multi-vendor
# (e.g. cursor sharing claude's surface) and ``extension`` is what
# ``override.resolve`` joins to ``<vendor>.<ext>``.
#
# v1 covers Claude / Gemini / Codex across skills, agents, commands. The
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


class InvalidNameError(ValueError):
    """Raised when a context-gateway name fails validation."""


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
