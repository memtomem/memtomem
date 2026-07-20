"""ADR-0011 PR-E2 — Gate A helpers shared across extract paths.

Centralises both the user-facing error message format for
``project_shared`` Gate A hard-abort and the proceed-gate decision logic
so ``extract_agents_to_canonical``, ``extract_skills_to_canonical``, and
``extract_commands_to_canonical`` cannot drift on either the wording or
the per-decision branching. The message deliberately echoes only the hit
count and source path — never the matched bytes themselves
(``feedback_force_unsafe_redaction_valve_only.md`` and the
``RedactionHit`` docstring on the privacy module both pin the
"never echo secrets" contract).

The proceed-gate helper :func:`apply_gate_a` keeps the audit_context
dictionary opaque — callers continue to supply their own per-kind
field shape (``agent_name`` vs ``command_name`` vs ``skill_name``;
``source`` vs ``source_file``; ``runtime``; etc.) — so a future
"normalise the audit shape" refactor cannot silently break SOC-pipeline
grep on those fields. The display-side singular noun is a separate
``message_kind`` parameter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import click

from memtomem import privacy
from memtomem.config import TargetScope
from memtomem.context import _skip_reasons as skip_codes
from memtomem.context import remediation

# Pull-preview Gate A vocabulary (ADR-0030 §4). The gate outcome as a
# non-raising status, distinct from :class:`GateAOutcome` (the write-path
# proceed/skip/abort union). Lives with the gate so preview and import can't
# spell the tokens differently. ``requires_unsafe_confirmation`` is a
# force-bypassable tier (user / project_local); ``blocked`` is the hard-refuse
# tier (project_shared) — the same split ``apply_gate_a`` enforces by raising.
GateStatus = Literal["ok", "blocked", "requires_unsafe_confirmation"]


def format_project_shared_block_message(
    src: Path,
    *,
    hits_count: int,
    scope: TargetScope,
    kind: str,
    imported_so_far: int = 0,
    remediation_hint: str | None = None,
) -> str:
    """User-facing ``ClickException`` message for project_shared Gate A hard-abort.

    Args:
        src: Source file (or skill directory's offending file) that hit Gate A.
        hits_count: Number of pattern hits — count only, never echo bytes.
        scope: The destination scope. Always ``"project_shared"`` in practice;
            other scopes never invoke this helper.
        kind: Singular noun for the artifact kind ("agent", "skill", "command").
        imported_so_far: Files already imported in this run (clean ones that
            passed Gate A before this hit). Surface for cleanup hint.
        remediation_hint: The calling surface's spelling of "retry in another
            tier" (``remediation.action_hint``), prefixed to the neutral retry
            line. ``None`` keeps the message surface-neutral — this helper
            builds a COMPLETE message and raises it, so a downstream surface
            cannot decorate it afterwards; the hint has to arrive here (#1869,
            same shape as ``privacy_scan.format_scan_block_message``).

    Returns:
        A multi-line string suitable for ``raise click.ClickException(...)``.
    """
    tail = (
        f"\n  {imported_so_far} clean {kind}(s) already pulled in this run "
        f"remain in canonical — review or remove manually."
        if imported_so_far > 0
        else ""
    )
    hint = f" {remediation_hint}" if remediation_hint else ""
    return (
        f"Gate A: {src.name} contains {hits_count} privacy pattern hit(s); "
        f"pull to scope='{scope}' was rejected. git history is forever — "
        f"no force bypass available for project_shared (ADR-0011 §5).\n"
        f"  Remove the secret from {src} first, or retry in the user "
        f"scope.{hint}{tail}"
    )


@dataclass(frozen=True)
class GateAProceed:
    """:func:`apply_gate_a` outcome — content passed Gate A; caller should write."""


@dataclass(frozen=True)
class GateABlocked:
    """:func:`apply_gate_a` outcome — content was blocked at Gate A.

    Only emitted for non-``project_shared`` scopes. ``project_shared``
    block hits raise :class:`click.ClickException` inside the helper.

    Attributes:
        code: ``PRIVACY_BLOCKED`` or ``PRIVACY_BLOCKED_PROJECT_SHARED``.
        hits_count: Number of privacy pattern hits — count only, never
            echo bytes.

    Carries no remediation clause: the ``code`` travels with the skip row all
    the way to CLI / web / MCP, and each renders its own bypass spelling from
    :mod:`memtomem.context.remediation` (#1869). A ``hint`` field here used to
    hard-code ``--force-unsafe-import``, which is not a thing an MCP client or
    the browser can pass.
    """

    code: skip_codes.SkipCode
    hits_count: int


# Discriminated union — callers narrow with ``isinstance(outcome, GateABlocked)``.
GateAOutcome = GateAProceed | GateABlocked


def apply_gate_a(
    *,
    content_text: str,
    src: Path,
    scope: TargetScope,
    force_unsafe_import: bool,
    audit_context: dict[str, object],
    message_kind: str,
    imported_so_far: int,
    surface: str = "cli_context_init",
) -> GateAOutcome:
    """Run :func:`privacy.enforce_write_guard` and decide proceed / skip / abort.

    Outcomes:
      * ``decision in ("pass", "bypassed")`` — return
        :class:`GateAProceed`.
      * ``decision in ("blocked", "blocked_project_shared")`` AND
        ``scope == "project_shared"`` — raise :class:`click.ClickException`
        via :func:`format_project_shared_block_message`. The
        ``project_shared`` ban is hard regardless of
        ``force_unsafe_import`` (ADR-0011 §5).
      * ``decision in ("blocked", "blocked_project_shared")`` AND
        ``scope != "project_shared"`` — return :class:`GateABlocked`
        with the matching ``code`` / ``hits_count``.
      * Anything else — :class:`RuntimeError` (fail-loud on enum drift).

    Args:
        content_text: Bytes-decoded content to scan. Caller decodes with
            ``errors="replace"`` so non-UTF8 bytes cannot mask an
            embedded ASCII secret.
        src: The source file the content was read from. Surfaced in the
            ``project_shared`` ClickException only — never echoed for
            non-project_shared skips.
        scope: Destination scope.
        force_unsafe_import: Caller's bypass flag — the value of the
            CLI's ``--force-unsafe-import`` flag (or its MCP equivalent).
            Forwarded to :func:`privacy.enforce_write_guard` as the
            ``force_unsafe`` kwarg; the kept-distinct names mark the
            "this is the import-side bypass valve" call site
            (``project_shared`` ignores it regardless).
        audit_context: Caller-supplied dict, passed verbatim to
            :func:`privacy.enforce_write_guard`. Helper does not inject
            or rename keys (so SOC-pipeline grep on per-kind fields like
            ``agent_name`` / ``source_file`` / ``runtime`` is preserved).
            Typed ``dict[str, object]`` to match the chokepoint signature
            (``privacy.enforce_write_guard``); current callers pass
            ``dict[str, str]`` literals which mypy widens at the call
            site. Non-string values are supported by
            :func:`privacy._sanitize_audit_value`.
        message_kind: Singular display noun for the ClickException
            ("agent" / "skill" / "command"). Distinct from the plural
            ``kind`` field that callers usually carry inside
            ``audit_context``.
        surface: Audit identifier forwarded verbatim to
            :func:`privacy.enforce_write_guard` — it dimensions the
            privacy ``record()`` counter and tags the force-unsafe
            bypass audit log line, distinguishing every ingress surface.
            Callers pass their own literal: the CLI relies on the
            default ``"cli_context_init"``, the Web import routes pass
            ``"web_context_<kind>_import"``, and the MCP tool passes
            ``"mcp_context_init"`` (#1229 — previously every surface
            was misattributed to the CLI literal).
        imported_so_far: Number of artifacts already imported in this
            run; passed through to the cleanup hint in the
            ClickException message. **Invariant**: callers must compute
            this from a list that is appended to only AFTER ``apply_gate_a``
            returns ``GateAProceed`` (i.e. no mid-scan mutation). A future
            partial-copy refactor that appends to the imported list
            inside the scan loop would silently change the cleanup-hint
            count and must update this contract.
    """
    guard = privacy.enforce_write_guard(
        content_text,
        surface=surface,
        force_unsafe=force_unsafe_import,
        scope=scope,
        audit_context=audit_context,
        record_outcome=True,
    )
    if guard.decision in ("blocked", "blocked_project_shared"):
        if scope == "project_shared":
            # The calling surface is already named by the privacy attribution
            # string — reuse it rather than threading a second parameter down
            # every ingress entrypoint. Unclassifiable ⇒ neutral text (#1869).
            hint_surface = remediation.hint_surface_for(surface)
            hint = (
                remediation.action_hint(remediation.GATE_A_PROJECT_SHARED_ABORT, hint_surface)
                if hint_surface is not None
                else ""
            )
            raise click.ClickException(
                format_project_shared_block_message(
                    src,
                    hits_count=len(guard.hits),
                    scope=scope,
                    kind=message_kind,
                    imported_so_far=imported_so_far,
                    remediation_hint=hint or None,
                )
            )
        code: skip_codes.SkipCode = (
            skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED
            if guard.decision == "blocked_project_shared"
            else skip_codes.PRIVACY_BLOCKED
        )
        return GateABlocked(code=code, hits_count=len(guard.hits))
    if guard.decision not in ("pass", "bypassed"):
        # Symmetric assertion — fail-loud on unknown decision so a
        # future privacy enum addition surfaces here rather than
        # silently dropping the write.
        raise RuntimeError(f"enforce_write_guard returned unexpected decision: {guard.decision!r}")
    return GateAProceed()


def classify_gate_status(
    content_text: str,
    *,
    scope: TargetScope,
    surface: str,
) -> GateStatus:
    """Side-effect-free Gate A classification for the pull preview (ADR-0030 §4).

    Unlike :func:`apply_gate_a` (the write path), this NEVER writes, NEVER
    raises for ``project_shared``, and NEVER mutates privacy counters or emits
    an audit line — it answers "what would the gate decide for a plain Pull
    (no ``--force-unsafe-import``) into ``scope``?" so a preview can show a
    ``blocked`` row instead of a 500.

    It calls :func:`privacy.enforce_write_guard` with ``force_unsafe=False``
    and ``record_outcome=False``. With ``force_unsafe=False`` the guard only
    ever returns ``"pass"`` or ``"blocked"`` (``"bypassed"`` /
    ``"blocked_project_shared"`` require the bypass flag), so:

      * ``pass``    → ``"ok"``
      * ``blocked`` → ``"blocked"`` for ``project_shared`` (hard-refuse tier,
        ADR-0011 §5), else ``"requires_unsafe_confirmation"`` (a real Pull
        could force-bypass this tier).

    Any other decision is a privacy-enum drift and fails loud (mirrors
    :func:`apply_gate_a`), rather than silently mislabeling a gate outcome.
    """
    guard = privacy.enforce_write_guard(
        content_text,
        surface=surface,
        force_unsafe=False,
        scope=scope,
        audit_context=None,
        record_outcome=False,
    )
    if guard.decision == "pass":
        return "ok"
    if guard.decision == "blocked":
        return "blocked" if scope == "project_shared" else "requires_unsafe_confirmation"
    raise RuntimeError(
        f"enforce_write_guard(force_unsafe=False) returned unexpected decision: {guard.decision!r}"
    )
