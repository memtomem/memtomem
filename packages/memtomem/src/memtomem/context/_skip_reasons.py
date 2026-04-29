"""Machine-readable skip reason codes for context fan-out / import results.

The :class:`~memtomem.context.skills.SkillSyncResult`,
:class:`~memtomem.context.commands.CommandSyncResult`,
:class:`~memtomem.context.agents.AgentSyncResult` and the shared
``ExtractResult`` types record skipped items as ``(name, reason, reason_code)``
tuples. ``reason`` is human-readable (used in CLI output and UI tooltips);
``reason_code`` is stable identifier that the web UI matches on so toast
copy can change without breaking client logic.
"""

from __future__ import annotations

from typing import Final, Literal

# Sync (canonical → runtime) skip codes.
NO_CANONICAL_ROOT: Final = "no_canonical_root"
UNKNOWN_RUNTIME: Final = "unknown_runtime"
PARSE_ERROR: Final = "parse_error"

# Import (runtime → canonical) skip codes.
INVALID_NAME: Final = "invalid_name"
ALREADY_IMPORTED: Final = "already_imported"
CANONICAL_EXISTS: Final = "canonical_exists"
TOML_PARSE_ERROR: Final = "toml_parse_error"

# Closed set of skip codes — typing dataclass `skipped` triples and route
# response builders against `SkipCode` catches typos at the construction site
# instead of letting an arbitrary string slip through to the wire.
SkipCode = Literal[
    "no_canonical_root",
    "unknown_runtime",
    "parse_error",
    "invalid_name",
    "already_imported",
    "canonical_exists",
    "toml_parse_error",
]
