"""Cross-project copy for canonical MCP server definitions (#1282, campaign #1270 A-12).

MCP server canonicals (``.memtomem/mcp-servers/<name>.json``) are
single-tier by design (project_shared only — ADR-0016 §3 note), so the
ADR-0023 tier-move matrix never applies to them; propagating one
definition from project A to project B is the only transfer they need.
This module is that adapter: :func:`copy_mcp_server` drives the transfer
engine's stage → Gate A → promote sequence over one flat JSON file
WITHOUT widening :data:`memtomem.context.scope_resolver.ArtifactKind` —
the engine primitives are reused (``_acquire_pair_lock``, ``_stage_copy``,
``TransferCollisionError``, ``ArtifactNotFoundError``), the
``transfer_artifact`` entry point is not.

Deliberate divergences from the artifact engine, each load-bearing:

- **The staged bytes are parse-validated, not just scanned.** The
  artifact engine never validates content; this adapter refuses to
  promote a definition that fails ``parse_mcp_server_text`` (stdio-only
  schema), because ``generate_all_mcp_servers`` aborts on the FIRST bad
  canonical — copying broken bytes into B would break B's entire
  mcp-servers sync phase, not just the copied server. Validation runs on
  the staged bytes inside the pair lock (the same text Gate A scanned),
  so a source edit racing the pre-flight check cannot smuggle invalid
  bytes past it (Codex design-gate fold).
- **Promotion refuses to clobber, atomically.** The mcp web CRUD routes
  serialize on the in-process gateway lock only — they do not take the
  per-file sidecar locks this adapter takes — so the engine's
  ``exists() → os.replace()`` promote could overwrite a canonical that a
  web create landed between the two calls. ``os.link`` refuses an
  existing target atomically (EEXIST → the engine's collision error),
  closing that window for this single-file copy (Codex design-gate
  blocker). Filesystems without hard-link support fall back to the
  engine's re-check + replace semantics, no worse than every artifact
  transfer today.
- **Symlinked canonicals are refused.** The engine preserves symlinks
  by contract; here a link would break the scanned-bytes ==
  promoted-bytes invariant (reads follow the link, and the hard-link
  promote would alias the out-of-project TARGET inode into the
  destination's git-tracked tree — Codex review blocker). Refusal is
  two-layered: a pre-flight check on the source, and an in-lock check
  on the staging entry for a source that turned into a link mid-copy.
- **``sync_command`` is the cd-prefixed CLI fan-out; ``sync_hint`` mirrors
  it as prose.** Since #1311 ``mm context sync --include=mcp-servers`` fans
  the canonical into the destination's ``.mcp.json`` (opt-in, sync-only), so
  the result carries a runnable command (``cd <dst> && mm context sync
  --include=mcp-servers --scope project_shared``) — cd-prefixed because the
  destination is a different project (the cross-project ``--project`` selector
  is A-9 #1279). ``sync_hint`` is the same instruction as prose for surfaces
  that render the hint instead of the command, and names the web panel / API
  (``POST /api/context/mcp-servers/sync``, Sync All) as equivalents.

Gate A always runs (the destination tier IS project_shared; ``env``
blocks are the expected hotspot) through the standard
``scan_text_content`` → ``raise_or_collect`` envelope every sync surface
emits, with a source-anchored remediation hint — the scan attribution
names the source file the user can actually edit, not the transient
``.migrate-*.tmp`` staging entry. A Gate A or parse refusal leaves zero
residue at the destination: copy staging never consumed the source, so
rollback is plain staging removal.

:class:`McpServerCopyResult` implements the FULL
:class:`memtomem.context.transfer.TransferResult` attribute surface
(pinned by test) so the CLI renderer and the web serializer work on
either result without engine-type casts.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import click

from memtomem.context._names import validate_name
from memtomem.context.mcp_servers import (
    PROJECT_MCP_CONFIG,
    McpServerParseError,
    _parse_project_mcp_text,
    canonical_mcp_server_path,
    parse_mcp_server_text,
)
from memtomem.context.migrate import ArtifactNotFoundError, _acquire_pair_lock
from memtomem.context.privacy_scan import raise_or_collect, scan_text_content
from memtomem.context.projects import compute_scope_id
from memtomem.context.transfer import (
    TransferCollisionError,
    _remove_staging,
    _stage_copy,
)

logger = logging.getLogger(__name__)

__all__ = ["McpServerCopyResult", "copy_mcp_server"]


@dataclass(frozen=True)
class McpServerCopyResult:
    """Outcome of one mcp-servers copy plan or apply.

    Field-for-field superset of
    :class:`memtomem.context.transfer.TransferResult` (the duck contract
    the CLI renderer and web serializer rely on; pinned by
    ``test_result_field_surface_superset_of_transfer_result``), with the
    constant values a single-tier flat-file copy implies: both scopes
    ``project_shared``, ``layout="flat"``, no fan-out cleanup (copy never
    touches the source, and destination fan-out stays sync's job), no
    rename, and ``provenance="not_applicable"`` (mcp-servers are not
    wiki-installed assets — there is no ``lock.json`` lineage to carry).

    Since #1311 ``sync_command`` is the runnable cd-prefixed CLI fan-out
    (``cd <dst> && mm context sync --include=mcp-servers``); ``sync_hint``
    is its prose mirror for surfaces that render the hint instead of the
    command. ``notes`` carries the destination ``.mcp.json`` disclosure
    (same-name runtime entry that the destination's next sync will
    overwrite, or a broken ``.mcp.json`` its sync will refuse on).
    """

    kind: str  # always "mcp-servers" — deliberately NOT ArtifactKind (#1282)
    name: str
    dst_name: str
    mode: Literal["copy"]
    from_scope: Literal["project_shared"]
    to_scope: Literal["project_shared"]
    src_project_root: Path
    dst_project_root: Path
    src_path: Path
    dst_path: Path
    layout: Literal["flat"]
    transferred: bool
    fanout_cleaned: list[Path] = field(default_factory=list)
    fanout_backed_up: list[Path] = field(default_factory=list)
    fanout_planned: list[Path] = field(default_factory=list)
    needs_sync: bool = True
    sync_command: str | None = None
    sync_hint: str | None = None
    notes: tuple[str, ...] = ()
    provenance: Literal["not_applicable"] = "not_applicable"
    provenance_reason: str | None = None
    provenance_reason_code: str | None = None


def _sync_command(dst_root: Path) -> str:
    """Runnable follow-up: fan the copied canonical into the destination ``.mcp.json``.

    cd-prefixed because the destination is a different project and sync runs
    per-project (the cross-project ``--project`` selector is A-9 #1279, not yet
    available); mirrors the artifact transfer's ``_sync_followup`` format.
    Since #1311 ``mm context sync`` has an mcp-servers leg, so this is a real
    command, not the web-only prose the result used to carry.
    """
    return (
        f"cd {shlex.quote(str(dst_root))} && "
        "mm context sync --include=mcp-servers --scope project_shared"
    )


def _sync_hint(dst_root: Path) -> str:
    """Prose mirror of :func:`_sync_command` for surfaces that render the hint.

    Since #1311 the runnable path is ``sync_command``; this prose carries the
    same instruction and names the web panel / API as equivalents (the scope_id
    keeps the API call copy-pasteable).
    """
    return (
        f"run `mm context sync --include=mcp-servers --scope project_shared` in "
        f"the destination project, or fan out from its web panel (mm web → "
        f"Context Gateway → MCP Servers → Sync) / `POST /api/context/mcp-servers/"
        f"sync?project_scope_id={compute_scope_id(dst_root)}`."
    )


def _dst_mcp_json_notes(dst_root: Path, name: str) -> tuple[str, ...]:
    """Disclosure notes derived from the copy's own resolution path.

    The copy lands a canonical; the destination's next mcp-servers sync
    resolves it into ``.mcp.json`` with a canonical-wins-per-name merge.
    Two states the user cannot see from the copy result alone are
    disclosed up front: a same-name runtime entry that sync will
    OVERWRITE, and a broken ``.mcp.json`` that sync will refuse on
    entirely. Best-effort reads — a disclosure probe must never fail or
    block the copy itself.
    """
    target = dst_root / PROJECT_MCP_CONFIG
    if not target.is_file():
        return ()
    try:
        raw = target.read_bytes().decode("utf-8", errors="replace")
    except OSError as exc:
        return (
            f"destination {PROJECT_MCP_CONFIG} could not be read ({exc}); "
            f"its mcp-servers sync will fail until the file is readable",
        )
    try:
        # The sync's own parser, not a bare json.loads: every shape sync
        # would refuse (syntax, top-level non-object, non-object
        # ``mcpServers``) must produce the warning, or the note goes
        # quiet exactly when the user most needs it (Codex review fold).
        config = _parse_project_mcp_text(raw)
    except McpServerParseError as exc:
        return (
            f"destination {PROJECT_MCP_CONFIG} cannot be parsed ({exc}); "
            f"its mcp-servers sync will fail until the file is fixed",
        )
    servers = config.get("mcpServers") or {}
    if name in servers:
        return (
            f"destination {PROJECT_MCP_CONFIG} already defines '{name}'; the "
            f"destination's next mcp-servers sync will overwrite that entry "
            f"with the copied canonical",
        )
    return ()


def _gate_a_hint(src_path: Path) -> str:
    """Source-anchored remediation hint (engine ``_offending_file_hint`` parity)."""
    return (
        f"Offending file: {src_path}\n"
        f"  Remove the secret from the source definition (the env block is "
        f"the usual location), then re-run the copy. There is no force "
        f"valve: the destination canonical lands in the git-tracked "
        f"project_shared tier."
    )


def _parse_definition_text(text: str, *, name: str, src_path: Path) -> None:
    """Validate *text* as a stdio MCP server definition (refusal detail in docstring).

    Raises :class:`memtomem.context.mcp_servers.McpServerParseError` with
    the source path in the message — the destination would reject the
    same bytes at sync time, except there the failure takes B's whole
    mcp-servers sync phase down with it (``generate_all_mcp_servers``
    aborts on the first bad canonical). Refusing at copy time keeps the
    blast radius at one file in one project.
    """
    parse_mcp_server_text(text, name=name, source=src_path)


def _promote_no_clobber(staging: Path, dst: Path) -> None:
    """Promote *staging* to *dst*, atomically refusing an existing *dst*.

    ``os.link`` fails with EEXIST instead of replacing, so a canonical
    created by a writer outside our sidecar pair lock (the mcp web CRUD
    routes hold only the in-process gateway lock) cannot be silently
    overwritten between a re-check and a rename — the Codex design-gate
    blocker on reusing the engine's ``exists() → os.replace()`` promote
    here. On filesystems without hard links the fallback keeps the
    engine's promote semantics (re-check + replace; the residual
    check-to-replace window is the same one every artifact transfer
    carries today). Success consumes staging on both paths.
    """
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(staging, dst)
    except FileExistsError:
        raise TransferCollisionError(f"destination appeared during lock acquire: {dst}.") from None
    except OSError as exc:
        logger.debug("hard-link promote unavailable for %s (%s); using os.replace", dst, exc)
        if dst.exists():
            raise TransferCollisionError(
                f"destination appeared during lock acquire: {dst}."
            ) from None
        os.replace(staging, dst)
        return
    with contextlib.suppress(OSError):
        staging.unlink()


def copy_mcp_server(
    name: str,
    *,
    src_project_root: Path | str,
    dst_project_root: Path | str,
    apply_: bool,
    surface: str = "cli_context_copy",
    lock_timeout: float | None = None,
) -> McpServerCopyResult:
    """Copy one canonical MCP server definition from project A to project B.

    Pure engine-style entry point — no prompts, no stdout writes;
    surfaces own Gate B confirmation (the destination is always the
    git-tracked project_shared tier) and error translation. Errors raise
    :class:`click.ClickException` subclasses
    (:class:`memtomem.context.migrate.ArtifactNotFoundError` for a
    missing source, :class:`memtomem.context.transfer.TransferCollisionError`
    for a destination collision — pre-flight AND inside the lock window),
    :class:`memtomem.context.mcp_servers.McpServerParseError` for an
    invalid source definition, and the standard
    :class:`memtomem.context.privacy_scan.PrivacyBlockedError` envelope
    for a Gate A hit on the staged bytes.

    Args:
        name: Server name (``validate_name``-constrained; also the
            destination name — rename is out of scope for #1282).
        src_project_root: Project root owning the source canonical.
        dst_project_root: Project root receiving the copy. Must differ
            from the source root (within one project the canonical
            already exists; there is no second tier to copy to).
        apply_: ``False`` returns the dry-run plan without touching disk.
        surface: Gate A audit identifier (CLI passes
            ``cli_context_copy``, the web route ``web_context_transfer``).
        lock_timeout: Whole-call acquisition budget shared across both
            sidecar locks (``_acquire_pair_lock``); ``None`` blocks
            indefinitely (CLI behavior), the web route passes its bound.

    Apply sequence: pre-flight checks (source exists + parses, no
    destination collision) → pair lock (sorted, shared deadline) →
    in-lock collision re-check → ``_stage_copy`` (source never consumed)
    → Gate A scan of the staged bytes (standard project_shared block
    envelope, source-anchored hint) → strict parse of the SAME staged
    bytes → no-clobber promote. Any raise removes staging; the source is
    intact by construction. Destination ``.mcp.json`` fan-out is NOT
    generated — ``needs_sync`` + ``sync_hint`` carry the follow-up
    (ADR-0023: sync stays the single writer of runtime trees).
    """
    name = validate_name(name, kind="MCP server")
    src_root = Path(src_project_root).expanduser().resolve()
    dst_root = Path(dst_project_root).expanduser().resolve()
    src_path = canonical_mcp_server_path(src_root, name)
    dst_path = canonical_mcp_server_path(dst_root, name)

    if src_root == dst_root:
        raise click.ClickException(
            f"source and destination are the same project ({src_root}); "
            f"mcp-servers copy is cross-project only — the canonical is "
            f"single-tier, so within one project there is nothing to copy to."
        )
    if not src_path.is_file():
        raise ArtifactNotFoundError(
            f"mcp-servers/{name} not found at the source project: {src_path}"
        )
    if src_path.is_symlink():
        # A symlinked canonical breaks the scanned-bytes == promoted-bytes
        # invariant (staging would stay a link; reads follow it, and the
        # hard-link promote would alias the out-of-project TARGET inode
        # into the destination's git-tracked tree — Codex review blocker).
        # The engine preserves links by contract; this adapter refuses
        # them. Loud refusal over silently materializing the target.
        raise click.ClickException(
            f"{src_path} is a symlink; mcp-servers copy refuses symlinked "
            f"canonicals — the destination must be a regular git-tracked "
            f"file. Replace the symlink with a regular file holding the "
            f"definition, then re-run the copy."
        )

    # Early parse signal for dry-run and fast pre-flight failure. The
    # authoritative validation runs on the STAGED bytes inside the lock —
    # a source edit between this check and staging cannot bypass it.
    try:
        src_text = src_path.read_bytes().decode("utf-8")
    except OSError as exc:
        raise click.ClickException(f"cannot read {src_path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise click.ClickException(
            f"{src_path} is not valid UTF-8; fix the source definition before copying."
        ) from exc
    _parse_definition_text(src_text, name=name, src_path=src_path)

    # Pre-flight conflict check (re-checked inside the lock; the promote
    # itself refuses a late arrival atomically). Message literal mirrors
    # the engine's collision wording minus the artifact-tier specifics.
    if dst_path.exists():
        raise TransferCollisionError(
            f"destination already exists: {dst_path}. "
            "Resolve manually or remove the existing entry first "
            "(no --force overwrite; ADR-0023 §6 collision policy)."
        )

    notes = _dst_mcp_json_notes(dst_root, name)
    sync_command = _sync_command(dst_root)
    sync_hint = _sync_hint(dst_root)

    def _result(*, transferred: bool) -> McpServerCopyResult:
        return McpServerCopyResult(
            kind="mcp-servers",
            name=name,
            dst_name=name,
            mode="copy",
            from_scope="project_shared",
            to_scope="project_shared",
            src_project_root=src_root,
            dst_project_root=dst_root,
            src_path=src_path,
            dst_path=dst_path,
            layout="flat",
            transferred=transferred,
            needs_sync=True,
            sync_command=sync_command,
            sync_hint=sync_hint,
            notes=notes,
        )

    if not apply_:
        return _result(transferred=False)

    with _acquire_pair_lock(src_path, dst_path, timeout=lock_timeout):
        if dst_path.exists():
            raise TransferCollisionError(f"destination appeared during lock acquire: {dst_path}.")

        staging = _stage_copy(src_path, dst_path.parent, name_hint=name)
        try:
            if staging.is_symlink():
                # The source turned into a symlink between the pre-flight
                # check and staging (``_stage_copy`` preserves links by
                # contract). Everything below assumes a regular staging
                # file — reads would follow the link and the hard-link
                # promote would alias its target — so refuse here, same
                # wording as the pre-flight gate.
                raise click.ClickException(
                    f"{src_path} is a symlink; mcp-servers copy refuses "
                    f"symlinked canonicals — the destination must be a "
                    f"regular git-tracked file. Replace the symlink with a "
                    f"regular file holding the definition, then re-run the "
                    f"copy."
                )
            # One read feeds both gates: the scan and the parse cover the
            # exact bytes about to be promoted (scan_text_content's
            # scan→write TOCTOU contract). Scan first — fail-closed on
            # secrets even when the bytes are also unparseable; the
            # replace-decode keeps an ASCII secret visible inside
            # otherwise-undecodable bytes (#895 P1 lineage), while the
            # parse below re-decodes strictly and refuses what the
            # destination's sync would refuse.
            staged_bytes = staging.read_bytes()
            scan = scan_text_content(
                staged_bytes.decode("utf-8", errors="replace"),
                source_path=src_path,
                surface=surface,
                scope="project_shared",
                project_root=dst_root,
            )
            if scan.decision in ("blocked", "blocked_project_shared"):
                raise_or_collect(
                    scan,
                    scope="project_shared",
                    kind="MCP server",
                    artifact_name=name,
                    remediation_hint=_gate_a_hint(src_path),
                )
            try:
                staged_text = staged_bytes.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise click.ClickException(
                    f"{src_path} is not valid UTF-8; fix the source definition before copying."
                ) from exc
            _parse_definition_text(staged_text, name=name, src_path=src_path)
            _promote_no_clobber(staging, dst_path)
        except BaseException:
            # Copy staging never consumed the source — dropping staging is
            # always safe (zero residue at the destination, source intact).
            _remove_staging(staging)
            raise

    return _result(transferred=True)
