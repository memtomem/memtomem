"""Tool: mem_update_coverage (#2918).

The ``update`` workflow reviews the latest commits of a project and saves
durable learnings. Before this tool it kept no record of which commits an
earlier run had reviewed, so every run re-inspected the same window. The
workflow now hands the server the SHAs it read and asks which are new
(``record=False``), and after its last memory write records the ones it read
in full (``record=True``).

Coverage is a per-project set of SHAs, never a cursor (see
``storage/mixins/update_coverage.py``). The server does not run ``git``: the
SHAs are the caller's statement of what it read, validated for shape only.

Plugin hosts expose this tool individually through ``MEMTOMEM_EXTRA_TOOLS`` so
a skill can name it in its own allowlist instead of being granted ``mem_do``;
``mem_do(action="update_coverage")`` reaches it everywhere else.
"""

from __future__ import annotations

import re

from mcp.types import ToolAnnotations

from memtomem.runtime.project_context import _resolve_project_context_root
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

# The workflow reads ``git log -n 10``; a longer list is a caller mistake, not a
# wider window.
_MAX_SHAS = 10
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")


def _validate_shas(shas: object) -> list[str] | str:
    """Return the list, or an ``Error:`` string. ``mem_do`` passes params unvalidated."""
    if not isinstance(shas, list):
        return "Error: shas must be a list of full commit SHAs."
    if not shas:
        return "Error: shas must not be empty."
    if len(shas) > _MAX_SHAS:
        return f"Error: shas holds {len(shas)} entries; the update window is at most {_MAX_SHAS}."
    for sha in shas:
        if not isinstance(sha, str) or not _SHA_RE.fullmatch(sha):
            return f"Error: not a full lowercase hexadecimal commit SHA: {sha!r}."
    if len(set(shas)) != len(shas):
        return "Error: shas contains a duplicate."
    return shas


# On the wire: ``readOnlyHint: false, destructiveHint: false`` — a writer that
# never deletes memories, so a caching proxy must not replay a check.
@mcp.tool(annotations=ToolAnnotations(read_only_hint=False, destructive_hint=False))
@tool_handler
@register("maintenance")
async def mem_update_coverage(
    shas: list[str],
    record: bool = False,
    run_id: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Report or record which commits the update workflow has reviewed for this project.

    Coverage is a per-project set of commit SHAs. With record=false the
    result lists the given SHAs that no earlier run recorded, in the order
    given. With record=true the given SHAs are recorded as reviewed; pass
    only commits whose full message was read, after the run's last mem_add.
    Rows older than 90 days are pruned on record.

    Args:
        shas: Full 40-character lowercase commit SHAs from `git log -n 10`; at most 10, no duplicates.
        record: false (default) reports the unreviewed subset; true records the SHAs as reviewed.
        run_id: The run's canonical lowercase UUID; required with record=true, stored as provenance.
    """
    checked = _validate_shas(shas)
    if isinstance(checked, str):
        return checked
    run_key: str | None = None
    if record:
        if not isinstance(run_id, str) or not _UUID_RE.fullmatch(run_id):
            return "Error: record=true needs run_id, a canonical lowercase UUID (8-4-4-4-12)."
        run_key = run_id
    elif run_id is not None and not isinstance(run_id, str):
        return "Error: run_id must be a string."

    app = await _get_app_initialized(ctx)
    root = _resolve_project_context_root(app)
    if root is None:
        return (
            "Error: no project context. Coverage is per registered project; run from inside a "
            "project with a registered .memtomem tier, or bind one with project_bind."
        )
    project_root = str(root)

    if run_key is not None:
        recorded, already = await app.storage.update_coverage_record(project_root, checked, run_key)
        return f"project_root: {project_root}\nrecorded: {recorded}\nalready_recorded: {already}"

    unreviewed = await app.storage.update_coverage_unreviewed(project_root, checked)
    lines = [
        f"project_root: {project_root}",
        f"reviewed: {len(checked) - len(unreviewed)}",
        f"unreviewed: {len(unreviewed)}",
    ]
    if unreviewed:
        lines.append("unreviewed_shas:")
        lines.extend(unreviewed)
    return "\n".join(lines)
