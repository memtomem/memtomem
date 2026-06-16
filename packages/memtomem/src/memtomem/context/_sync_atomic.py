"""Shared atomic-file artifact sync engine (issue #900).

Agents and commands share the same six-stage lifecycle — enumerate canonical →
resolve runtime fan-out target → drop/strict policy → per-vendor override →
sync-side Gate A privacy → atomic write → return summary — implemented today
as two near-identical copies in :mod:`memtomem.context.agents` and
:mod:`memtomem.context.commands`. This module is the extraction: a single
``sync_atomic_artifact`` engine parametrized by an :class:`AtomicSyncAdapter`
that plugs in the per-artifact callables (list_canonical / parse / generators).

Behavior is byte-for-byte identical to the pre-extraction implementations.
The TOCTOU-close (read canonical bytes once, scan and parse from the captured
buffer; same for override bytes) is preserved. The Phase-1-raises-early
``project_shared`` atomicity is preserved — no partial fan-out can land on
disk for the all-or-nothing scope. The strict-drop partial-write boundary
pinned by ``test_strict_drop_preserves_earlier_writes`` (#908) is preserved:
Phase 2 ``StrictDropError`` fires mid-loop, earlier writes remain.

Skills are intentionally out of scope here — their staging-dir promotion
shape is different enough that forcing it into this engine would cost more
than it saves. See :mod:`memtomem.context.skills`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Generic, Literal, Protocol, TypeVar

from memtomem.config import TargetScope
from memtomem.context import _skip_reasons as skip_codes
from memtomem.context import override as _override
from memtomem.context._atomic import atomic_write_bytes
from memtomem.context._names import GENERATOR_VENDOR, Layout
from memtomem.context.privacy_scan import raise_or_collect, scan_text_content
from memtomem.context.versioning import (
    LabelNotFoundError,
    VersionError,
    VersionNotFoundError,
    VersionsDirMissingError,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")
# Contravariant alias used only by ``AtomicGenerator``: the protocol only
# *consumes* the parsed item (in ``render(item)``), never produces it, so
# mypy expects a contravariant marker. ``AtomicSyncAdapter`` keeps using
# invariant ``T`` — it owns both the parse function (produces T) and the
# generators mapping (consumes T).
T_contra = TypeVar("T_contra", contravariant=True)


class StrictDropError(ValueError):
    """Engine-level strict-drop error.

    :mod:`memtomem.context.agents` and :mod:`memtomem.context.commands`
    each subclass this so their public ``StrictDropError`` stays a
    distinct class (``agents.StrictDropError is not commands.StrictDropError``)
    — an ``except agents.StrictDropError`` block does NOT catch a commands
    raise. The engine raises ``adapter.strict_drop_error_type`` which each
    adapter sets to its own subclass.

    ``generated`` carries the ``(runtime, target_file)`` writes that landed
    BEFORE the raise — the #908 partial-write boundary fires mid-Phase-2, so
    surfaces (web 422, #1247 id 47) can report which runtime files already
    changed instead of an opaque error. Keyword-optional with a ``None``
    default: the class is public API (re-exported by both artifact modules)
    and one-arg construction must keep working.
    """

    def __init__(
        self,
        message: str,
        *,
        generated: list[tuple[str, Path]] | None = None,
    ) -> None:
        super().__init__(message)
        self.generated: list[tuple[str, Path]] = list(generated or [])


# Valid severity levels for the ``on_drop`` parameter.
ON_DROP_LEVELS = ("ignore", "warn", "error")


@dataclass
class AtomicSyncResult:
    """Engine-level result shape for atomic-file sync (agents + commands).

    :mod:`memtomem.context.agents` and :mod:`memtomem.context.commands`
    each subclass this so their public ``AgentSyncResult`` /
    ``CommandSyncResult`` stay distinct classes (matching the
    pre-extraction API surface). The engine constructs results via
    ``adapter.result_type`` so each adapter binds its own subclass.
    """

    generated: list[tuple[str, Path]]  # (runtime, target_file)
    dropped: list[tuple[str, str, list[str]]]  # (runtime, item_name, dropped_fields)
    # (runtime_or_item, human_reason, reason_code) — see :mod:`memtomem.context._skip_reasons`.
    skipped: list[tuple[str, str, skip_codes.SkipCode]]


class AtomicGenerator(Protocol[T_contra]):
    """Per-runtime generator protocol — exactly what AGENT_GENERATORS /
    COMMAND_GENERATORS values already implement (``target_file`` returns
    ``None`` for ``NO_FANOUT`` runtime/scope tuples, see
    ``_runtime_targets.RUNTIME_FANOUT_TABLE``).
    """

    def target_file(self, project_root: Path, name: str, *, scope: TargetScope) -> Path | None: ...

    def render(self, item: T_contra) -> tuple[str, list[str]]: ...


@dataclass(frozen=True)
class AtomicSyncAdapter(Generic[T]):
    """Per-artifact plug-in points for :func:`sync_atomic_artifact`.

    Each callable is the exact function the original agents.py / commands.py
    used; the adapter just bundles them so the engine stays artifact-
    agnostic.

    Args:
        kind: Discriminator passed through to ``raise_or_collect`` and used
            in the ``StrictDropError`` message. Must match the literal the
            privacy scan layer expects (``"agent"`` or ``"command"``).
        artifact_label: Plural form passed to ``_override.resolve`` as the
            second positional argument (``"agents"`` or ``"commands"``).
            Also appears in the ``NO_CANONICAL_ROOT`` skip reason.
        list_canonical: ``list_canonical_agents`` / ``list_canonical_commands``
            — returns ``[(path, layout), ...]`` for the canonical root at
            the requested scope.
        parse_canonical_text: ``_parse_canonical_agent_text`` /
            ``_parse_canonical_command_text`` — raises
            ``parse_error_type`` on malformed input.
        parse_error_type: ``AgentParseError`` / ``CommandParseError`` —
            the concrete exception class ``parse_canonical_text`` raises.
        name_of: Extracts the artifact name from a parsed item
            (``lambda a: a.name`` for SubAgent / SlashCommand).
        generators: ``AGENT_GENERATORS`` / ``COMMAND_GENERATORS`` —
            runtime-key → generator mapping.
    """

    kind: Literal["agent", "command"]
    artifact_label: str
    list_canonical: Callable[..., list[tuple[Path, Layout]]]
    parse_canonical_text: Callable[..., T]
    parse_error_type: type[Exception]
    name_of: Callable[[T], str]
    generators: Mapping[str, AtomicGenerator[T]]
    # The engine constructs results via ``result_type`` and raises
    # ``strict_drop_error_type`` so each artifact module's public class
    # identity is preserved (``AgentSyncResult is not CommandSyncResult``,
    # ``agents.StrictDropError is not commands.StrictDropError`` — Codex
    # review on #900 flagged the aliased-class regression). Default to the
    # shared engine types so adapters that don't care don't have to opt in.
    result_type: type[AtomicSyncResult] = AtomicSyncResult
    strict_drop_error_type: type[StrictDropError] = StrictDropError
    # Logger used for ``on_drop="warn"`` messages. Defaults to the engine
    # module's logger; each adapter passes its own so warnings continue to
    # appear under ``memtomem.context.agents`` / ``memtomem.context.commands``
    # for log filters that historically targeted those names.
    logger: logging.Logger = logger
    # ADR-0022: optional label-aware canonical-bytes resolver. ``None``
    # (default) ⇒ the engine reads ``item_path`` directly (today's behavior,
    # byte-for-byte). When set, the engine calls this instead of
    # ``item_path.read_bytes()`` in Phase 1, substituting a versioned snapshot
    # for the working file. Returns ``(bytes, source_path)`` so the Gate A scan
    # attributes to the actual ``versions/vN.md``, not the working file. It may
    # raise the ``VersionError`` family (label/version not found, flat layout,
    # malformed manifest), which the engine isolates as per-item skips.
    resolve_canonical_bytes: Callable[[Path, Layout], tuple[bytes, Path]] | None = None


def sync_atomic_artifact(
    adapter: AtomicSyncAdapter[T],
    project_root: Path,
    runtimes: list[str] | None = None,
    strict: bool = False,
    on_drop: str = "ignore",
    *,
    scope: TargetScope = "project_shared",
    surface: str = "cli_context_sync",
    force_unsafe: bool = False,
) -> AtomicSyncResult:
    """Fan out every canonical artifact to the requested runtimes (atomic-file shape).

    Args:
        adapter: Per-artifact plug-in points. See :class:`AtomicSyncAdapter`.
        project_root: Project root containing ``.memtomem/<artifact_label>/``.
        runtimes: List of generator names. ``None`` means all registered
            generators in ``adapter.generators``.
        on_drop: Severity when fields are dropped during conversion.
            ``"ignore"`` (default) — silently record in ``result.dropped``.
            ``"warn"``  — log a warning per dropped-field set.
            ``"error"`` — raise :class:`StrictDropError` immediately.
        strict: Legacy alias for ``on_drop="error"``. If both are supplied,
            ``on_drop`` takes precedence unless it is still the default.
        scope: ADR-0011 PR-E3 — selects canonical root and runtime fan-out
            destination. Default ``project_shared`` preserves pre-PR-E3
            behavior.
        surface: Audit identifier forwarded verbatim to
            :func:`privacy.enforce_write_guard` via both
            :func:`scan_text_content` sites (canonical bytes AND
            per-vendor override bytes) — it dimensions the privacy
            ``record()`` counter and tags the blocked/bypassed audit
            log line. Callers pass their own literal: the CLI relies on
            the default ``"cli_context_sync"``, the Web sync routes
            pass ``"web_context_<kind>_sync"``, and the MCP tools pass
            ``"mcp_context_generate"`` / ``"mcp_context_sync"`` (#1246
            — sibling of the import-side #1242 fix).
        force_unsafe: Reviewed Gate A bypass (ADR-0011 §5) threaded to
            both :func:`scan_text_content` sites. ``True`` lets a
            reviewed false positive fan out to ``user`` /
            ``project_local`` destinations; ``project_shared`` is
            hard-refused regardless (the engine's Gate A is
            authoritative). Default ``False``.

    Returns:
        :class:`AtomicSyncResult` carrying ``generated``, ``dropped``, and
        ``skipped`` tuples.

    Raises:
        PrivacyBlockedError: Phase 1 Gate A block under ``scope=project_shared``
            — raised by :func:`raise_or_collect`. No filesystem mutation
            has happened at this point (all-or-nothing atomicity). Surfaces
            translate at the boundary (CLI → ``click.ClickException``,
            MCP → tool error, web → HTTP 422); the engine itself does not
            import ``click``.
        StrictDropError: Phase 2, when ``on_drop="error"`` (or legacy
            ``strict=True``) and a render would drop fields. Earlier writes
            in pending order have already landed on disk — this is the
            intentional partial-write boundary pinned by
            ``test_strict_drop_preserves_earlier_writes`` (#908).
    """
    # Resolve legacy ``strict`` flag.
    effective_drop = on_drop if on_drop != "ignore" or not strict else "error"

    generated: list[tuple[str, Path]] = []
    dropped: list[tuple[str, str, list[str]]] = []
    skipped: list[tuple[str, str, skip_codes.SkipCode]] = []

    canonicals = adapter.list_canonical(project_root, scope=scope)
    if not canonicals:
        return adapter.result_type(
            generated=[],
            dropped=[],
            skipped=[
                (
                    "<all>",
                    f"no canonical {adapter.artifact_label}",
                    skip_codes.NO_CANONICAL_ROOT,
                )
            ],
        )

    targets = runtimes if runtimes is not None else list(adapter.generators.keys())

    # ── Phase 1: parse + scan every (target, item) pair — pure read pass. ──
    # No filesystem mutation happens here. For ``scope='project_shared'``
    # the first Gate A hit raises immediately via :func:`raise_or_collect`,
    # so Phase 2 never starts and no partial runtime fan-out can land on
    # disk (#895 P2 review #5). For user / project_local scopes, blocks
    # collect into ``skipped`` as before.
    #
    # ``pending`` is a list of write descriptors: one per (target, item)
    # pair that passed every gate. Each entry carries the immutable
    # snapshot Phase 2 needs (parsed item + override bytes that already
    # passed Gate A) so the write loop never re-reads canonical from
    # disk and the scan→write TOCTOU window stays closed.
    pending: list[tuple[str, str, AtomicGenerator[T], T, Path, bytes | None]] = []

    # Frontmatter-name dedupe (#1247): ``out_path`` is a pure function of
    # (target, name), so two canonicals (different stems) declaring the same
    # ``name:`` would both queue for the SAME runtime file — silent
    # last-writer-wins, with both writes reported in ``generated`` and the
    # loser invisible on every surface (diff keys canonicals by parsed name
    # too). Track the first canonical to claim each name across the whole
    # fan-out; later claimants from a *different* source path get a typed
    # ``DUPLICATE_NAME`` skip per target (matching the per-(target, item)
    # convention of the parse/privacy skips above) plus a once-per-loser
    # warning. First-seen wins: ``list_canonical`` returns sorted order, so
    # the winner is deterministic. Same-name flat-vs-dir pairs never reach
    # here — ``list_canonical`` already collapses those (dir wins).
    name_owner: dict[str, Path] = {}
    duplicates_warned: set[Path] = set()

    for target in targets:
        gen = adapter.generators.get(target)
        if gen is None:
            skipped.append((target, "unknown runtime", skip_codes.UNKNOWN_RUNTIME))
            continue
        for item_path, layout in canonicals:
            # Artifact display name for skip rows: dir layout nests the file as
            # ``<name>/agent.md``, so ``item_path.name`` would read "agent.md"
            # — use the parent dir name instead so per-artifact skips (esp. the
            # new label/version ones) name the artifact, not its filename.
            display_name = item_path.parent.name if layout == "dir" else item_path.stem
            # PR-E3 Codex review fold: read canonical bytes ONCE and use
            # the captured buffer for both Gate A scan AND parse, closing
            # the scan→write TOCTOU window. A concurrent edit between
            # scan and parse would otherwise let an attacker present
            # clean bytes to scan and unsafe bytes to render.
            #
            # ADR-0022: when the adapter carries a label-aware resolver, it
            # substitutes a versioned snapshot's bytes for the working file.
            # Resolution failures isolate per-artifact as typed skips (a
            # missing label on one artifact must not abort the whole fan-out),
            # consistent with the OSError/parse handling below.
            # ``scan_source`` is the path the Gate A scan attributes to — the
            # resolved version file when a label is in play, else the working
            # canonical. Defaults to ``item_path`` for the no-label path.
            scan_source = item_path
            try:
                if adapter.resolve_canonical_bytes is not None:
                    item_bytes, scan_source = adapter.resolve_canonical_bytes(item_path, layout)
                else:
                    item_bytes = item_path.read_bytes()
            except LabelNotFoundError as exc:
                skipped.append((display_name, f"label: {exc}", skip_codes.LABEL_NOT_FOUND))
                continue
            except VersionNotFoundError as exc:
                skipped.append((display_name, f"version: {exc}", skip_codes.VERSION_NOT_FOUND))
                continue
            except VersionsDirMissingError as exc:
                skipped.append(
                    (
                        display_name,
                        f"versioning requires dir layout: {exc}",
                        skip_codes.VERSIONING_REQUIRES_DIR_LAYOUT,
                    )
                )
                continue
            except VersionError as exc:
                # Catch-all for the rest of the family (malformed/tampered
                # manifest → InvalidTagError / InvalidLabelError / base
                # VersionError). Isolate per-artifact as a parse-class skip
                # rather than aborting the whole fan-out with a raw traceback.
                skipped.append((display_name, f"version store: {exc}", skip_codes.PARSE_ERROR))
                continue
            except OSError as exc:
                skipped.append((item_path.name, f"unreadable: {exc}", skip_codes.PARSE_ERROR))
                continue
            item_text = item_bytes.decode("utf-8", errors="replace")
            try:
                item = adapter.parse_canonical_text(item_text, source=item_path, layout=layout)
            except adapter.parse_error_type as exc:
                skipped.append((item_path.name, f"parse error: {exc}", skip_codes.PARSE_ERROR))
                continue
            name = adapter.name_of(item)
            # ADR-0011 PR-E (#891): resolve the runtime target BEFORE render
            # + dropped-field handling. ``None`` means NO_FANOUT per
            # ``_runtime_targets.RUNTIME_FANOUT_TABLE``; emit a typed skip
            # without invoking ``render`` so a strict caller doesn't raise
            # ``StrictDropError`` for a runtime that has no fan-out by
            # design (the fail-quiet contract).
            out_path = gen.target_file(project_root, name, scope=scope)
            if out_path is None:
                skipped.append(
                    (
                        name,
                        f"no fan-out for runtime {target} at this scope",
                        skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME,
                    )
                )
                continue
            # ADR-0011 PR-E3 Gate A — scan the IN-MEMORY canonical bytes
            # (same buffer the parse used). project_shared block raises
            # PrivacyBlockedError; user/project_local block emits
            # PRIVACY_BLOCKED skip and continues to next runtime.
            # ``scan_source`` attributes the scan to the resolved version file
            # when a label is in play (ADR-0022), else the working canonical.
            file_scan = scan_text_content(
                item_text,
                source_path=scan_source,
                surface=surface,
                scope=scope,
                project_root=project_root,
                force_unsafe=force_unsafe,
            )
            if file_scan.decision in ("blocked", "blocked_project_shared"):
                code, reason = raise_or_collect(
                    file_scan,
                    scope=scope,
                    kind=adapter.kind,
                    artifact_name=name,
                )
                skipped.append((name, reason, code))
                continue
            # Frontmatter-name dedupe (#1247) — deliberately AFTER the Gate A
            # scan, so a secret-bearing duplicate loser still trips the
            # project_shared all-or-nothing raise (the dedupe must not become
            # a privacy bypass for canonical bytes), and AFTER the no-fanout
            # check, so that contract's skip rows are unchanged for duplicate
            # losers too. BEFORE override resolution — the loser never
            # writes, and the colliding name resolves the SAME override file
            # the winner's pass already scans, so no scan surface is lost.
            owner = name_owner.setdefault(name, item_path)
            if owner != item_path:
                if item_path not in duplicates_warned:
                    duplicates_warned.add(item_path)
                    adapter.logger.warning(
                        "duplicate %s name %r: %s already provides it; skipping %s",
                        adapter.kind,
                        name,
                        owner,
                        item_path,
                    )
                skipped.append(
                    (
                        name,
                        f"duplicate name {name!r}: already provided by {owner}; "
                        f"skipping {item_path}",
                        skip_codes.DUPLICATE_NAME,
                    )
                )
                continue
            # Resolve per-vendor override and scan its bytes — read once,
            # scan once, hand the bytes to Phase 2. Same TOCTOU close as
            # canonical above.
            vendor = GENERATOR_VENDOR.get(target)
            override_bytes: bytes | None = None
            if vendor is not None:
                # ADR-0011 PR-E3: thread the resolved sync ``scope`` through
                # to override resolution. Same-tier-only lookup (narrow→broad
                # is intentionally NOT used for default sync per ADR §4).
                override_path = _override.resolve(
                    project_root,
                    adapter.artifact_label,
                    name,
                    vendor,
                    scope=scope,
                )
                if override_path is not None:
                    try:
                        override_bytes = override_path.read_bytes()
                    except OSError as exc:
                        skipped.append(
                            (
                                name,
                                f"override unreadable: {exc}",
                                skip_codes.PARSE_ERROR,
                            )
                        )
                        continue
                    override_text = override_bytes.decode("utf-8", errors="replace")
                    file_scan = scan_text_content(
                        override_text,
                        source_path=override_path,
                        surface=surface,
                        scope=scope,
                        project_root=project_root,
                        force_unsafe=force_unsafe,
                    )
                    if file_scan.decision in ("blocked", "blocked_project_shared"):
                        code, reason = raise_or_collect(
                            file_scan,
                            scope=scope,
                            kind=adapter.kind,
                            artifact_name=name,
                        )
                        skipped.append((name, reason, code))
                        continue
            pending.append((target, name, gen, item, out_path, override_bytes))

    # ── Phase 2: render + atomic write every pending pair. ──
    # By construction Phase 1 raised on any project_shared privacy block,
    # so reaching this loop means every queued write is clean. The only
    # remaining mid-loop raise is StrictDropError, which is opt-in
    # (``on_drop="error"`` or legacy ``strict=True``) and is an unrelated
    # atomicity boundary — pre-existing behavior pinned by
    # ``test_strict_drop_preserves_earlier_writes`` (#908).
    #
    # This engine deliberately takes NO cross-process lock — unlike
    # :mod:`memtomem.context.skills`, which holds ``_file_lock`` across its
    # dst→old→staging→dst swap. A lock is unnecessary here (not merely
    # deadlock-free): (a) project_shared all-or-nothing is enforced in Phase 1,
    # which raises before any write; (b) user / project_local partial writes are
    # an intentional contract pinned by ``test_strict_drop_preserves_earlier_writes``
    # (#908); and (c) each ``out_path`` is written exactly once via a single
    # atomic ``os.replace`` (below), so per-file writes are idempotent with
    # no torn cross-file state to protect (#1123 B3-1). The exactly-once
    # part is enforced by the Phase 1 duplicate-name dedupe (#1247) — before
    # it, two canonicals sharing a frontmatter name queued the same
    # ``out_path`` twice.
    for target, name, gen, item, out_path, override_bytes in pending:
        content, dropped_fields = gen.render(item)
        if dropped_fields:
            if effective_drop == "error":
                raise adapter.strict_drop_error_type(
                    f"strict mode: {target} would drop {dropped_fields} from '{name}'",
                    generated=generated,
                )
            if effective_drop == "warn":
                adapter.logger.warning("%s dropped %s from '%s'", target, dropped_fields, name)
        # ADR-0008 Invariant 4: a per-vendor override REPLACES the rendered
        # runtime file, so write the final bytes exactly once. Writing the
        # rendered ``content`` first and then overwriting it with
        # ``override_bytes`` was wasted I/O and left a crash window where
        # ``out_path`` briefly held the non-override rendered bytes a runtime
        # could load (#1123 B3-5). Use the SAME captured override buffer that
        # passed Gate A — never re-read from disk (would re-open the scan→write
        # TOCTOU window).
        final_bytes = override_bytes if override_bytes is not None else content.encode("utf-8")
        atomic_write_bytes(out_path, final_bytes)
        generated.append((target, out_path))
        if dropped_fields:
            dropped.append((target, name, dropped_fields))

    return adapter.result_type(generated=generated, dropped=dropped, skipped=skipped)


__all__ = [
    "AtomicGenerator",
    "AtomicSyncAdapter",
    "AtomicSyncResult",
    "ON_DROP_LEVELS",
    "StrictDropError",
    "sync_atomic_artifact",
]
