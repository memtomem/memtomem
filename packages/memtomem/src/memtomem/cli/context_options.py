"""Click options shared by ``mm context`` commands across the ``context_*_cmd`` modules.

They live here rather than in ``context_cmd`` because the split-out command
modules apply them at import time, as decorators, and ``context_cmd`` imports
those modules at its end: an option read from ``context_cmd`` would make each
command module unimportable on its own.
"""

from __future__ import annotations

from typing import get_args

import click

from memtomem.config import TargetScope

# ``sync`` accepts one extra kind (mcp-servers, #1311); its own option carries
# the accurate help so the other commands' --include help is not widened.
_SYNC_INCLUDE_OPTION = click.option(
    "--include",
    "include",
    multiple=True,
    metavar="KIND",
    help=(
        "Additional artifact kinds to fan out (repeatable or comma-separated). "
        "Supported: skills, agents, commands, settings, mcp-servers. "
        "mcp-servers (opt-in) fans canonical MCP server definitions into the "
        "project .mcp.json; it is a push-only kind."
    ),
)

_SCOPE_OPTION = click.option(
    "--scope",
    "scope_flag",
    type=click.Choice(list(get_args(TargetScope))),
    default=None,
    help=(
        "Override hooks.target_scope for this invocation only "
        "(user / project_shared / project_local). "
        "Host-write confirmation is computed against the resolved scope, "
        "so --scope=project_local skips the prompt (writes stay in-project)."
    ),
)

# ADR-0022: fan out a labeled version instead of the working canonical. Applies
# only to the version-eligible kinds (agents / commands); ineligible included
# kinds run label-less (see ``_warn_label_ineligible_kinds``).
_LABEL_OPTION = click.option(
    "--label",
    default=None,
    help=(
        "Push the version at this label (e.g. 'production') or a bare version "
        "tag ('v2') instead of the working canonical. Applies to agents and "
        "commands only; 'latest' / omitted means the working file."
    ),
)
