"""ADR-0011 PR-E3 — canonical → runtime sync-side privacy scan.

Sibling of :mod:`memtomem.context._gate_a` (the runtime → canonical
import-side gate). Both share :func:`memtomem.privacy.enforce_write_guard`
underneath, but the two surfaces differ on:

* ``surface=`` string — ``"cli_context_sync"`` vs ``"cli_context_init"``.
* ``force_unsafe`` valve — sync has none (ADR §5: canonical → runtime
  fan-out is a write-amplifying loop, so a single bypass-flagged content
  would propagate to every registered runtime; the ADR explicitly
  reserves ``--force-unsafe-import`` for the import direction only).
* Block-message wording — "fan-out … rejected" vs "import … rejected"
  with a remediation hint that points at ``mm context migrate`` (the
  cross-tier move command landing in PR-E4) instead of "retry with a
  different ``--scope``".

A future PR can fold both surfaces onto a single ``gate_a.py`` once the
divergence stabilises (see PR-E3 plan §2d Implementer-side note 9).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal, NamedTuple

import click

from memtomem import privacy
from memtomem.config import TargetScope
from memtomem.context import _skip_reasons as skip_codes

OnBlocked = Literal["fail_fast", "skip_warn"]


class FileScan(NamedTuple):
    """Per-file scan result (one entry per visited file in a tree walk)."""

    path: Path
    decision: str  # "pass" | "blocked" | "blocked_project_shared" | "bypassed"
    hits_count: int


class ScanResult(NamedTuple):
    """Aggregate result of :func:`scan_artifact_tree`.

    ``decisions`` is the full per-file list (every visited file, including
    passes — useful for telemetry). ``blocked`` is the convenience
    subset for callers that only need the failure list.
    """

    decisions: list[FileScan]
    blocked: list[FileScan]


def scan_artifact_tree(
    src: Path,
    *,
    surface: str,
    scope: TargetScope,
    project_root: Path | None,
    on_blocked: OnBlocked = "fail_fast",
) -> ScanResult:
    """Walk ``src`` (file or directory) and run :func:`enforce_write_guard` per file.

    Sync-side privacy gate. ``force_unsafe`` is hardcoded ``False`` —
    sync has no escape valve regardless of ``scope`` (ADR §5).

    Args:
        src: Either a single file (agents/commands canonical entry) or a
            directory tree (skill staging directory). When a directory,
            every regular file under it is scanned in sorted order; binary
            files (``UnicodeDecodeError`` on UTF-8 decode) are recorded
            with ``decision="pass"`` since the regex-based pattern set
            cannot match non-text payloads.
        surface: Audit-log surface tag — typically ``"cli_context_sync"``.
            Used by :func:`enforce_write_guard` to attribute the outcome
            to the calling code path.
        scope: Destination scope. Determines block-vs-skip semantics
            via the caller's policy (this function only emits
            decisions; the caller branches on
            ``project_shared`` → :class:`click.ClickException` vs
            ``user``/``project_local`` → skip-and-warn).
        project_root: Forwarded to :func:`enforce_write_guard` audit
            context only (privacy itself does not need it). May be
            ``None`` for ``scope="user"``.
        on_blocked: ``"fail_fast"`` returns immediately on the first
            blocked file (subsequent files are NOT scanned — useful when
            the caller will raise). ``"skip_warn"`` continues through
            all files and collects the full block list.

    Returns:
        :class:`ScanResult` with the per-file ``decisions`` list and the
        ``blocked`` convenience subset. Callers branch on
        ``result.blocked`` and ``scope`` to decide raise-vs-skip.
    """
    files = [src] if src.is_file() else sorted(p for p in src.rglob("*") if p.is_file())
    decisions: list[FileScan] = []
    audit_context_base: dict[str, object] = {"kind": "sync", "scope": scope}
    if project_root is not None:
        audit_context_base["project_root"] = str(project_root)

    for path in files:
        audit_context: dict[str, object] = {**audit_context_base, "path": str(path)}
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            # Binary asset (PNG, etc.) or transient read failure: out of
            # scope for the regex-based pattern set. Recording as ``pass``
            # is safe-by-default — false negatives are bounded by the
            # pattern set's ASCII-only character classes.
            decisions.append(FileScan(path, "pass", 0))
            continue

        guard = privacy.enforce_write_guard(
            text,
            surface=surface,
            force_unsafe=False,
            scope=scope,
            audit_context=audit_context,
            record_outcome=True,
        )
        scan = FileScan(path, guard.decision, len(guard.hits))
        decisions.append(scan)

        if guard.decision in ("blocked", "blocked_project_shared") and on_blocked == "fail_fast":
            return ScanResult(decisions=decisions, blocked=[scan])

    blocked = [d for d in decisions if d.decision in ("blocked", "blocked_project_shared")]
    return ScanResult(decisions=decisions, blocked=blocked)


def format_scan_block_message(
    blocked: FileScan,
    *,
    scope: TargetScope,
    kind: str,
    artifact_name: str | None = None,
) -> str:
    """User-facing :class:`click.ClickException` message for project_shared sync block.

    Mirrors :func:`memtomem.context._gate_a.format_project_shared_block_message`
    but with sync-side wording and ``mm context migrate`` remediation hint.

    Args:
        blocked: First :class:`FileScan` from
            :attr:`ScanResult.blocked` (caller fail-fasts on first hit).
        scope: Destination scope. Always ``"project_shared"`` in
            practice; other scopes never invoke this helper.
        kind: Singular display noun ("agent" / "skill" / "command").
        artifact_name: The artifact's canonical name when known (e.g.
            "leak"). Used in the remediation hint —
            ``mm context migrate <kind> <artifact_name> ...``. ``None``
            falls back to a generic hint.

    Returns:
        Multi-line string suitable for ``raise click.ClickException(...)``.
    """
    target_hint = (
        f"mm context migrate {kind} {artifact_name} --to project_local"
        if artifact_name is not None
        else f"mm context migrate {kind} <name> --to project_local"
    )
    return (
        f"Gate A: {blocked.path.name} contains {blocked.hits_count} privacy "
        f"pattern hit(s); fan-out to scope='{scope}' rejected. git history "
        f"is forever — no force bypass available for project_shared "
        f"(ADR-0011 §5).\n"
        f"  Move the {kind} to a writable tier first:\n"
        f"    {target_hint}\n"
        f"  Or remove the secret from {blocked.path} before re-running sync."
    )


def raise_or_collect(
    blocked: FileScan,
    *,
    scope: TargetScope,
    kind: str,
    artifact_name: str | None = None,
) -> tuple[skip_codes.SkipCode, str]:
    """Branch on ``scope``: raise for project_shared, return skip tuple otherwise.

    Helper to keep the per-call-site branch concise. ``project_shared``
    always raises :class:`click.ClickException`; ``user`` /
    ``project_local`` return ``(code, reason)`` for the caller to append
    to its ``skipped`` list.
    """
    if scope == "project_shared":
        raise click.ClickException(
            format_scan_block_message(blocked, scope=scope, kind=kind, artifact_name=artifact_name)
        )
    code: skip_codes.SkipCode = (
        skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED
        if blocked.decision == "blocked_project_shared"
        else skip_codes.PRIVACY_BLOCKED
    )
    reason = f"privacy blocked at {blocked.path.name} ({blocked.hits_count} pattern hit(s))"
    return code, reason
