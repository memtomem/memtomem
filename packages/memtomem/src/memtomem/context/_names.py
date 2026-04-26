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

__all__ = ["InvalidNameError", "validate_name"]

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_LEN = 64


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
