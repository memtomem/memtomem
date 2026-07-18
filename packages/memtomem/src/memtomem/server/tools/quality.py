"""Quality Lab replay MCP tool (#1802, PR-5)."""

from __future__ import annotations

from memtomem.quality.replay import MAX_AS_OF_UNIX, replay_cases, serialize_report
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("analytics")
async def mem_quality_replay(
    cases: list[str] | None = None,
    as_of_unix: int | None = None,
    ctx: CtxType = None,
) -> str:
    """Replay stored evaluation cases into a deterministic retrieval-quality report.

    Runs each selected case through the search pipeline in no-side-effects mode
    and returns the canonical JSON replay report (advisory). Retrieved rows
    carry no chunk text and no absolute paths — content hashes + scores +
    metrics only. The report DOES include each case's name (secret-scanned at
    promotion, and defensively redacted at emit if a legacy row carries a secret
    or path) and its raw query text, which is NOT sanitized: a report is only as
    sensitive as the queries promoted into it, so treat it accordingly.
    Also reachable as ``mem_do(action="quality_replay")``.

    Args:
        cases: Case ids or names to replay (default: all active cases).
        as_of_unix: Pin temporal validity + decay to this unix time (default: now).
    """
    # mem_do dispatches to this raw function with unvalidated params, so the
    # annotations above are NOT enforced there — validate the runtime types by
    # hand and return "Error: ..." strings rather than raising.
    if cases is not None:
        if not isinstance(cases, list):
            return "Error: cases must be a list of case ids or names."
        if any(not isinstance(c, str) or not c.strip() for c in cases):
            return "Error: cases must be case ids or names (non-empty strings)."
    if as_of_unix is not None:
        # bool is an int subclass — reject it explicitly so True/False can't
        # sneak in as 1/0.
        if isinstance(as_of_unix, bool) or not isinstance(as_of_unix, int):
            return "Error: as_of_unix must be an integer unix timestamp."
        if not 0 <= as_of_unix <= MAX_AS_OF_UNIX:
            return (
                f"Error: as_of_unix must be between 0 and {MAX_AS_OF_UNIX} "
                f"(a representable unix timestamp), got {as_of_unix}."
            )

    selectors = [c.strip() for c in cases] or None if cases is not None else None

    app = await _get_app_initialized(ctx)
    report = await replay_cases(
        app.storage,
        app.search_pipeline,
        app.config,
        case_ids=selectors,
        as_of_unix=as_of_unix,
    )
    return serialize_report(report)
