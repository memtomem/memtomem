"""ADR-0011 PR-E3 — canonical → runtime sync-side privacy scan.

Sibling of :mod:`memtomem.context._gate_a` (the runtime → canonical
import-side gate). Both share :func:`memtomem.privacy.enforce_write_guard`
underneath, but the two surfaces differ on:

* ``surface=`` string — caller-supplied on both sides. Sync defaults to
  ``"cli_context_sync"`` (migrate: ``"cli_context_migrate"``); the Web
  sync routes pass ``"web_context_<kind>_sync"`` and the MCP tools pass
  ``"mcp_context_generate"`` / ``"mcp_context_sync"`` /
  ``"mcp_context_artifact_migrate"`` (#1246). The import side defaults
  to ``"cli_context_init"``; the Web import routes pass
  ``"web_context_<kind>_import"`` and the MCP tool passes
  ``"mcp_context_init"`` (#1229).
* ``force_unsafe`` valve — both sides now expose a reviewed bypass, but
  scope-asymmetrically (ADR §5). ``project_shared`` is ALWAYS hard-refused
  regardless of the flag (git history is forever; a forced fan-out is a
  write-amplifying loop into every runtime AND the repo), so
  :func:`enforce_write_guard` returns ``blocked_project_shared`` there.
  Only ``user`` / ``project_local`` destinations honour the bypass —
  ``mm context sync --force-unsafe`` (fan-out) and
  ``mm context init --force-unsafe-import`` (import). Both
  :func:`scan_artifact_tree` and :func:`scan_text_content` take a
  ``force_unsafe`` param (default ``False``) the sync callers thread.
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

from memtomem import privacy
from memtomem.config import TargetScope
from memtomem.context import _skip_reasons as skip_codes
from memtomem.privacy import RedactionHit


class PrivacyScanError(Exception):
    """Umbrella for sync-side privacy scan failures across surfaces.

    Surfaced as a plain ``Exception`` (not ``click.ClickException``)
    so non-CLI callers — web routes, MCP context tool — can catch and
    render in their native error shape (HTTP 422, structured tool
    response) instead of falling through to the generic 500 handler.
    The CLI sub-commands wrap their generator calls to translate this
    back into ``click.ClickException``.

    Concrete subclasses pin which scan stage failed; the message is
    pre-formatted and safe to surface to the user.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PrivacyBlockedError(PrivacyScanError):
    """Raised by :func:`raise_or_collect` for ``project_shared`` privacy hits.

    Carries the formatted user-facing message plus the structured
    ``FileScan`` and scope context so non-CLI surfaces can render
    their own error shape. ADR-0011 §5: ``project_shared`` has no
    force-bypass valve — every caller must hard-refuse.
    """

    def __init__(
        self,
        message: str,
        *,
        blocked: "FileScan",
        scope: TargetScope,
        kind: str,
        artifact_name: str | None,
    ) -> None:
        super().__init__(message)
        self.blocked = blocked
        self.scope = scope
        self.kind = kind
        self.artifact_name = artifact_name


class PrivacyScanReadError(PrivacyScanError):
    """Raised when a file cannot be read during the scan walk.

    Fail-closed counterpart to :class:`PrivacyBlockedError`: an
    unreadable file cannot be inspected for secrets, so the only
    safe move is to abort the sync. Migration callers re-rename
    staging back to ``src``; sync callers remove staging in their
    ``finally`` block.
    """

    def __init__(self, message: str, *, path: Path, scope: TargetScope) -> None:
        super().__init__(message)
        self.path = path
        self.scope = scope


OnBlocked = Literal["fail_fast", "skip_warn"]


class FileScan(NamedTuple):
    """Per-file scan result (one entry per visited file in a tree walk).

    ``hits`` carries the raw :class:`RedactionHit` list when the caller
    needs to render ``pattern_index`` / ``span`` (e.g. the rescan audit
    output). Defaults to ``()`` so positional construction at existing
    sync-side call sites stays source-compatible — the sync path only
    reads ``decision`` and ``hits_count``.
    """

    path: Path
    decision: str  # "pass" | "blocked" | "blocked_project_shared" | "bypassed"
    hits_count: int
    hits: tuple[RedactionHit, ...] = ()


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
    record_outcome: bool = True,
    force_unsafe: bool = False,
) -> ScanResult:
    """Walk ``src`` (file or directory) and run :func:`enforce_write_guard` per file.

    Sync-side privacy gate. ``force_unsafe`` is the caller's reviewed
    Gate A bypass valve (ADR-0011 §5) — defaults ``False`` so every
    non-forced sync keeps blocking on a hit. ``force_unsafe=True`` is
    NOT a blanket escape: :func:`privacy.enforce_write_guard` still
    hard-refuses it for ``scope == "project_shared"`` (returns a
    ``blocked_project_shared`` decision and emits the bypass-attempt
    audit line), so git-tracked content can never be forced out. Only
    ``user`` / ``project_local`` destinations honour the bypass — the
    same asymmetry the import side exposes via ``--force-unsafe-import``.

    Args:
        src: Either a single file (agents/commands canonical entry) or a
            directory tree (skill staging directory). When a directory,
            every regular file under it is scanned in sorted order.
            Bytes are decoded with ``errors="replace"`` (mirrors the
            import side, ``_gate_a.gate_a_text_content``) so an ASCII
            secret embedded in an otherwise-undecodable blob still
            blocks — replacement chars (U+FFFD) cannot themselves
            match the ASCII-only regex pattern set, so this does not
            create false positives on benign binary assets.
        surface: Audit-log surface tag — e.g. ``"cli_context_sync"``,
            ``"web_context_skills_sync"``, ``"mcp_context_artifact_migrate"``.
            Used by :func:`enforce_write_guard` to attribute the outcome
            to the calling code path.
        scope: Destination scope. Determines block-vs-skip semantics
            via the caller's policy (this function only emits
            decisions; the caller branches on
            ``project_shared`` → :class:`PrivacyBlockedError` vs
            ``user``/``project_local`` → skip-and-warn).
        project_root: Forwarded to :func:`enforce_write_guard` audit
            context only (privacy itself does not need it). May be
            ``None`` for ``scope="user"``.
        on_blocked: ``"fail_fast"`` returns immediately on the first
            blocked file (subsequent files are NOT scanned — useful when
            the caller will raise). ``"skip_warn"`` continues through
            all files and collects the full block list.
        record_outcome: Forwarded to :func:`enforce_write_guard`.
            Default ``True`` matches the sync-side write-amplifying
            contract (every scan is a real audit event). Read-only
            audit callers (``mm context rescan``) pass ``False`` so the
            re-check does not double-count outcomes or re-emit bypass
            audit lines.
        force_unsafe: Reviewed Gate A bypass forwarded verbatim to
            :func:`enforce_write_guard`. ``True`` flips a ``user`` /
            ``project_local`` hit from ``blocked`` to ``bypassed`` (the
            file is no longer in ``ScanResult.blocked`` and the caller
            promotes it); ``project_shared`` is hard-refused regardless
            (``blocked_project_shared``). Default ``False``.

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
            # Decode with ``errors="replace"`` so non-UTF8 bytes cannot
            # mask an embedded ASCII secret. A binary blob that happens
            # to carry ``AKIA...`` / ``sk-...`` bytes between
            # undecodable garbage would otherwise short-circuit to
            # ``pass`` (#895 P1 review #4); the regex pattern set is
            # ASCII-only so individual replacement characters (U+FFFD)
            # cannot themselves match, and ASCII byte values survive
            # the decode unchanged. Mirrors the import-side contract
            # documented in ``_gate_a.gate_a_text_content``.
            text = path.read_bytes().decode("utf-8", errors="replace")
        except OSError as exc:
            # Read failure (permissions, transient I/O, missing file).
            # Pre-PR-E4 review this was conflated with UnicodeDecodeError
            # and recorded as ``pass`` — but ``UnicodeDecodeError`` reads
            # the bytes (regex would have nothing to match), while
            # ``OSError`` means we never even saw the bytes. PR-E4's
            # ``_stage_move`` can rename a ``chmod 000`` canonical file
            # into staging without reading it, so silent-pass would have
            # let an unreadable file containing a secret promote into
            # ``project_shared`` with Gate A never inspecting it.
            #
            # Fail closed: hard-abort the scan with a
            # :class:`PrivacyScanReadError`. The exception propagates through callers'
            # rollback paths (``migrate_scope`` re-renames staging back
            # to src; ``generate_all_skills`` removes staging in its
            # finally block) so no half-promoted state survives.
            raise PrivacyScanReadError(
                f"Gate A: cannot read {path} (errno={exc.errno}); refusing to "
                f"fan-out / migrate to scope='{scope}'. An unreadable file "
                "cannot be scanned for secrets — fix the permission or "
                "remove the file before re-running.",
                path=path,
                scope=scope,
            ) from exc

        guard = privacy.enforce_write_guard(
            text,
            surface=surface,
            force_unsafe=force_unsafe,
            scope=scope,
            audit_context=audit_context,
            record_outcome=record_outcome,
        )
        scan = FileScan(path, guard.decision, len(guard.hits), tuple(guard.hits))
        decisions.append(scan)

        if guard.decision in ("blocked", "blocked_project_shared") and on_blocked == "fail_fast":
            return ScanResult(decisions=decisions, blocked=[scan])

    blocked = [d for d in decisions if d.decision in ("blocked", "blocked_project_shared")]
    return ScanResult(decisions=decisions, blocked=blocked)


def scan_text_content(
    text: str,
    *,
    source_path: Path,
    surface: str,
    scope: TargetScope,
    project_root: Path | None,
    force_unsafe: bool = False,
) -> FileScan:
    """Scan an already-loaded content string against :func:`enforce_write_guard`.

    Distinct from :func:`scan_artifact_tree` (which opens the path
    itself): callers that have already read the bytes pass the in-memory
    text here and use the SAME bytes for the downstream parse / write.
    Closes the scan→read TOCTOU window flagged by Codex review on the
    PR-E3 commit (concurrent edit between scan and write would otherwise
    let unscanned bytes fan out).

    ``force_unsafe`` is the caller's reviewed Gate A bypass (ADR-0011
    §5), forwarded verbatim to :func:`enforce_write_guard`. It honours
    the same scope asymmetry as :func:`scan_artifact_tree`:
    ``project_shared`` stays hard-refused (``blocked_project_shared``)
    even when ``True``; only ``user`` / ``project_local`` flip a hit to
    ``bypassed``. Default ``False`` keeps every non-forced sync blocking.

    Returns a :class:`FileScan` with the path attribution preserved for
    downstream messaging; caller branches on
    ``decision in ("blocked", "blocked_project_shared")`` and feeds the
    result through :func:`raise_or_collect`.
    """
    audit_context: dict[str, object] = {
        "kind": "sync",
        "scope": scope,
        "path": str(source_path),
    }
    if project_root is not None:
        audit_context["project_root"] = str(project_root)
    guard = privacy.enforce_write_guard(
        text,
        surface=surface,
        force_unsafe=force_unsafe,
        scope=scope,
        audit_context=audit_context,
        record_outcome=True,
    )
    return FileScan(source_path, guard.decision, len(guard.hits), tuple(guard.hits))


def format_scan_block_message(
    blocked: FileScan,
    *,
    scope: TargetScope,
    kind: str,
    artifact_name: str | None = None,
    remediation_hint: str | None = None,
) -> str:
    """User-facing message body for project_shared sync block.

    Mirrors :func:`memtomem.context._gate_a.format_project_shared_block_message`
    but with sync-side wording and ``mm context migrate`` remediation hint.

    Args:
        blocked: First :class:`FileScan` from
            :attr:`ScanResult.blocked` (caller fail-fasts on first hit).
        scope: Destination scope. Always ``"project_shared"`` in
            practice; other scopes never invoke this helper.
        kind: Singular display noun ("agent" / "skill" / "command").
            Used as-is in the prose; pluralised to "<kind>s" inside
            the embedded ``mm context migrate`` command because that
            CLI only accepts the plural choices ``agents`` /
            ``commands`` / ``skills`` / ``memory`` (#895 P2 review #3
            — the pre-fix hint produced ``mm context migrate agent ...``
            and tripped Click's invalid-choice error when users
            followed the remediation).
        artifact_name: The artifact's canonical name when known (e.g.
            "leak"). Used in the remediation hint —
            ``mm context migrate <kind>s <artifact_name> ...``. ``None``
            falls back to a generic hint.
        remediation_hint: Caller-supplied replacement for the default
            sync-side remediation lines. The stock hint prescribes
            ``mm context migrate ... --to project_local`` — wrong for
            ingress surfaces where the secret lives upstream (wiki
            install/update: fix the wiki asset; settings promote: fix
            the hook rule) rather than in a canonical the user could
            migrate (#1247). ``None`` keeps the historical message
            byte-identical for the existing sync/migrate callers.

    Returns:
        Multi-line string carried by :class:`PrivacyBlockedError`.
    """
    if remediation_hint is not None:
        return (
            f"Gate A: {blocked.path.name} contains {blocked.hits_count} privacy "
            f"pattern hit(s); write to scope='{scope}' rejected. git history "
            f"is forever — no force bypass available for project_shared "
            f"(ADR-0011 §5).\n"
            f"  {remediation_hint}"
        )
    cli_kind = f"{kind}s"  # singular → plural for the migrate CLI choice
    target_hint = (
        f"mm context migrate {cli_kind} {artifact_name} --to project_local"
        if artifact_name is not None
        else f"mm context migrate {cli_kind} <name> --to project_local"
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
    remediation_hint: str | None = None,
) -> tuple[skip_codes.SkipCode, str]:
    """Branch on ``scope``: raise for project_shared, return skip tuple otherwise.

    Helper to keep the per-call-site branch concise. ``project_shared``
    always raises :class:`PrivacyBlockedError`; ``user`` /
    ``project_local`` return ``(code, reason)`` for the caller to append
    to its ``skipped`` list. Each calling surface owns the translation
    (CLI → :class:`click.ClickException`, web → HTTP 422, MCP →
    structured tool error) so deep generators stay surface-agnostic.
    """
    if scope == "project_shared":
        message = format_scan_block_message(
            blocked,
            scope=scope,
            kind=kind,
            artifact_name=artifact_name,
            remediation_hint=remediation_hint,
        )
        raise PrivacyBlockedError(
            message,
            blocked=blocked,
            scope=scope,
            kind=kind,
            artifact_name=artifact_name,
        )
    code: skip_codes.SkipCode = (
        skip_codes.PRIVACY_BLOCKED_PROJECT_SHARED
        if blocked.decision == "blocked_project_shared"
        else skip_codes.PRIVACY_BLOCKED
    )
    reason = f"privacy blocked at {blocked.path.name} ({blocked.hits_count} pattern hit(s))"
    return code, reason
