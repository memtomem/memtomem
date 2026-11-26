"""Tool: mem_decision_record."""

from __future__ import annotations

import hashlib
import os

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register

#: The privacy-counter surface and the idempotency ledger's tool name.
_SURFACE = "mem_decision_record"

#: Joins the decoded strings for the guard. Not whitespace, so a label in one
#: string cannot pair with a value in the next.
_SCAN_PART_SEPARATOR = "\n---\n"

_REPEATED_ID_NOTE = (
    "A record with this decision_id was written in the last 24 hours. This call's "
    "members were not compared with it and were not written; a changed decision is a "
    "new record with a new decision_id."
)


@mcp.tool()
@tool_handler
@register("decisions")
async def mem_decision_record(
    decision_id: str,
    project: str,
    recorded_by: str,
    question: str,
    options: list[str],
    chosen: str,
    recommended: str | None = None,
    recommended_by: str | None = None,
    agent_reason: str | None = None,
    user_words: str | None = None,
    refs: list[str] | None = None,
    decided_at: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Record one decision point: what an agent offered and recommended, and what the user chose.

    Call it when the user has picked among options an agent offered. The
    record keeps the agent's judgement (``recommended``, ``agent_reason``)
    apart from the user's (``chosen``, ``user_words``). It is written to this
    project's ``project_local`` tier under ``shared:<project>``, with tags
    derived from the record; the caller supplies none.

    ``options``, ``recommended``, ``chosen`` and ``user_words`` are stored
    exactly as given. A changed decision is a new record with a new
    ``decision_id``, which is this write's idempotency key: a repeat is
    handled as ``mem_add`` handles a repeated ``idempotency_key``.

    Args:
        decision_id: Lowercase UUID the caller generates for this decision.
        project: This project's slug; refused if it is not the server's project.
        recorded_by: Runtime label of the calling agent (e.g. claude-code).
        question: One line, at most 120 characters, on what was being decided.
        options: The labels offered, each verbatim; at least two, all different.
        chosen: The label the user picked, or the user's own answer, verbatim;
            not empty and not whitespace only.
        recommended: The recommended label, exactly as in options, if one was.
        recommended_by: Runtime label of the recommending agent (default: recorded_by).
        agent_reason: The agent's reason for the recommendation, at most 240 characters.
        user_words: What the user said about why, verbatim.
        refs: Up to 5 issue, pull request, commit or file identifiers.
        decided_at: ISO 8601 timestamp with a UTC offset (default: now); stored in UTC.
    """
    from memtomem import privacy
    from memtomem.constants import validate_writable_namespace
    from memtomem.decision_record import DecisionRecordError, build_decision_record, project_slug
    from memtomem.indexing.engine import PrivacyRejection
    from memtomem.memory_scope import project_tier_dir, project_tier_is_relinked
    from memtomem.server.tools.memory_crud import _REPLAY_MARKER, _mem_add_core
    from memtomem.server.tools.search import _resolve_project_context_root
    from memtomem.server.validation import MAX_CONTENT_LENGTH
    from memtomem.tools.memory_writer import (
        entry_guard_text,
        entry_open_fence,
        open_fence_refusal,
    )

    try:
        record = build_decision_record(
            decision_id=decision_id,
            project=project,
            recorded_by=recorded_by,
            question=question,
            options=options,
            chosen=chosen,
            recommended=recommended,
            recommended_by=recommended_by,
            agent_reason=agent_reason,
            user_words=user_words,
            refs=refs,
            decided_at=decided_at,
            max_body_chars=MAX_CONTENT_LENGTH,
        )
    except DecisionRecordError as exc:
        return f"Error: {exc}; nothing was written."

    # The destination is the server's project, not the ``project`` argument,
    # so the two are compared before anything is written: a call naming
    # project B on a server in project A would otherwise file a B-labelled
    # record in A's tier. The root is resolved once and pinned below.
    app = await _get_app_initialized(ctx)
    root = _resolve_project_context_root(app)
    if root is None:
        return (
            "Error: this server has no project context, so there is no project to record "
            "the decision in; nothing was written. Register the project's local tier with "
            "`mm mem init --scope project_local` from the project root, or bind this "
            'server to it with mem_do(action="project_bind", '
            'params={"project_root": "<absolute path>"}).'
        )
    server_slug = project_slug(root.name)
    if server_slug != record.project:
        # Each slug is named so the caller can correct the call, but only as
        # it is: this runs before the guard, and the sanitizer replaces a slug
        # shaped like a credential and shortens a long one, neither of which
        # could be passed back.
        given = (
            f"project '{record.project}'"
            if privacy.sanitize_audit_value(record.project) == record.project
            else "the project given"
        )
        if server_slug is None:
            return (
                f"Error: {given} is not this server's project, whose root directory "
                "name gives no usable slug; nothing was written. Record the decision "
                "from a server running in the project it belongs to."
            )
        named = (
            f"'{server_slug}'"
            if privacy.sanitize_audit_value(server_slug) == server_slug
            else "not shown here; it is derived from the project root's directory name"
        )
        return (
            f"Error: {given} is not this server's project ({named}); nothing was "
            "written. Pass this project's slug, or record the decision from a server "
            "running in the other project."
        )
    # The core resolves the tier directory through a link and asks only whether
    # the result is registered, so a linked tier could carry this project's
    # record into another project's.
    if project_tier_is_relinked(project_tier_dir("project_local", root)):
        return (
            "Error: this project's .memtomem/memories.local, or the .memtomem directory "
            "holding it, is a link; nothing was written. Replace the link with a regular "
            "directory."
        )
    namespace = validate_writable_namespace(f"shared:{record.project}")

    # The core refuses an entry that leaves a code fence open (#3618); asked
    # here too, ahead of the scans, as every entry writer asks before its guard.
    # The rendered record closes its own ```json fence, so this is not expected
    # to fire.
    fence = entry_open_fence(record.body, record.title, record.tags)
    if fence is not None:
        return f"Error: {open_fence_refusal(fence)}"

    # Two scans. The first is the text the core's own guard reads for these
    # arguments, so a record that passes here cannot be refused there with
    # advice to use a valve this action does not have. The second is every
    # string as it will be read back: in the rendered entry each one is
    # JSON-escaped, and the guard resolves only so many levels of escaping.
    scans = (
        entry_guard_text(record.body, record.title, record.tags),
        _SCAN_PART_SEPARATOR.join(privacy.value_scan_parts(record.members)),
    )
    for text in scans:
        guard = privacy.enforce_write_guard(
            text, surface=_SURFACE, scope="project_local", record_outcome=False
        )
        if guard.decision != "pass":
            privacy.record("blocked", _SURFACE)
            return (
                f"Error: the record matches {len(guard.hits)} privacy pattern(s); nothing "
                "was written. Remove the secret from the record's members and call again."
            )

    # Scoped to this action by ``ledger_tool`` and to this project root by the
    # key, so neither an ordinary mem_add key nor a same-named project
    # elsewhere can replay this record's result.
    root_digest = hashlib.sha256(os.fsencode(root)).hexdigest()[:16]
    try:
        message, stats = await _mem_add_core(
            content=record.body,
            title=record.title,
            tags=record.tags,
            file=None,
            namespace=namespace,
            template=None,
            ctx=ctx,
            scope="project_local",
            project_root_override=root,
            idempotency_key=f"{root_digest}:{record.decision_id}",
            ledger_tool=_SURFACE,
            event_type="decision_record",
        )
    except PrivacyRejection as exc:
        if exc.mutation_outcome != "appended":
            raise
        return exc.mutation_detail(_SURFACE, outcome="appended", surface="mcp")

    if stats is None:
        if not message.endswith(_REPLAY_MARKER):
            return message
        return f"{message}\n- Decision id: {record.decision_id}\n{_REPEATED_ID_NOTE}"
    return f"{message}\n- Decision id: {record.decision_id}"
