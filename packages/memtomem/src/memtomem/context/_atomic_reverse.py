"""Shared reverse/diff/listing engine for atomic-file artifacts (issue #1515).

Issue #900 extracted the FORWARD sync (canonical → runtime fan-out) into
:mod:`memtomem.context._sync_atomic`. This module is the same extraction for
the remaining near-identical halves of :mod:`memtomem.context.agents` and
:mod:`memtomem.context.commands`:

- canonical name/layout dispatch (:func:`canonical_artifact_name`),
- flat-vs-dir canonical resolution and enumeration
  (:func:`resolve_artifact_under_root` / :func:`list_canonical_artifacts` /
  :func:`resolve_artifact_extract_target`),
- the byte-passthrough reverse-import branch shared by the agents
  claude+gemini importers and the commands claude importer
  (:func:`import_passthrough_runtime`) — the commands gemini branch stays in
  :mod:`memtomem.context.commands` (TOML→Markdown conversion, different
  glob/error-code/writer),
- the canonical ↔ runtime diff (:func:`diff_atomic_artifact`), parametrized
  by the same :class:`~memtomem.context._sync_atomic.AtomicSyncAdapter` the
  forward engine uses.

Behavior is byte-for-byte identical to the pre-extraction implementations —
result tuples, skip rows and reason codes, dedupe semantics, and RENDERED
log text are unchanged. (Format strings are parametrized, so ``record.msg``
/ ``record.args`` differ from the per-module originals; every rendered
message is identical.) Log calls go through the per-artifact module logger
passed by each wrapper, so filtering / ``caplog`` capture by
``memtomem.context.agents`` and ``memtomem.context.commands`` keeps working.

Skills stay out of scope for the same reason as in ``_sync_atomic`` — their
staging-dir promotion and tree-shaped canonical don't fit the atomic-file
shape. See :mod:`memtomem.context.skills`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar, cast

from memtomem.config import TargetScope
from memtomem.context import _skip_reasons as skip_codes
from memtomem.context import override as _override
from memtomem.context._canonical_txn import (
    _CANONICAL_LOCK_BUDGET_S,
    SnapshotError,
    write_canonical_locked,
)
from memtomem.context._gate_a import GateABlocked, apply_gate_a
from memtomem.context._names import GENERATOR_VENDOR, InvalidNameError, Layout, validate_name
from memtomem.context._runtime_targets import (
    DiffRow,
    runtime_artifact_listing,
    runtime_fanout_root,
)
from memtomem.context._sync_atomic import AtomicSyncAdapter
from memtomem.context.scope_resolver import ArtifactKind, canonical_artifact_dir

T = TypeVar("T")


def canonical_artifact_name(path: Path, layout: Layout) -> str:
    """Single source of truth for canonical path → name dispatch.

    Avoids the brittle ``path.name == "<kind>.md"`` heuristic — callers must
    pass the layout tag they got from :func:`list_canonical_artifacts` or the
    extract functions.
    """
    return path.parent.name if layout == "dir" else path.stem


def resolve_artifact_under_root(
    canonical_root: Path,
    name: str,
    *,
    artifact_label: ArtifactKind,
    dir_filename: str,
    logger: logging.Logger,
) -> tuple[Path, Layout] | None:
    """Resolve ``name`` under ``canonical_root`` across both layouts.

    Directory layout wins when both the legacy flat file and the ADR-0008
    directory layout are present (with a WARNING so the silently divergent
    flat file stays visible).
    """
    dir_target = canonical_root / name / dir_filename
    flat_target = canonical_root / f"{name}.md"
    has_dir = dir_target.is_file()
    has_flat = flat_target.is_file()
    if has_dir and has_flat:
        logger.warning(
            "%s/%s: reverse-sync updates dir layout (%s/%s); the flat "
            "file (%s.md) is now silently divergent. Remove it or run "
            "`mm context migrate` (PR-D).",
            artifact_label,
            name,
            name,
            dir_filename,
            name,
        )
        return dir_target, "dir"
    if has_dir:
        return dir_target, "dir"
    if has_flat:
        return flat_target, "flat"
    return None


def list_canonical_artifacts(
    project_root: Path,
    *,
    artifact_label: ArtifactKind,
    dir_filename: str,
    logger: logging.Logger,
    scope: TargetScope = "project_shared",
) -> list[tuple[Path, Layout]]:
    """Enumerate canonical artifacts in both flat and directory layouts.

    Flat layout (legacy): ``<label>/<name>.md``. Directory layout (ADR-0008
    PR-C+): ``<label>/<name>/<dir_filename>``. When the same name has both
    forms, the directory layout wins and a WARNING is logged so the silent
    flat file is visible.

    ADR-0011 PR-E3: ``scope`` selects the canonical root via
    :func:`canonical_artifact_dir` (default ``project_shared`` preserves
    pre-PR-E3 behavior).
    """
    root = canonical_artifact_dir(artifact_label, scope, project_root)
    if not root.is_dir():
        return []

    flat: dict[str, Path] = {p.stem: p for p in sorted(root.glob("*.md")) if p.is_file()}
    dirs: dict[str, Path] = {}
    for entry in sorted(root.iterdir()):
        if entry.is_dir():
            item_md = entry / dir_filename
            if item_md.is_file():
                dirs[entry.name] = item_md

    for name in sorted(set(flat) & set(dirs)):
        logger.warning(
            "%s/%s: both flat (%s.md) and dir (%s/%s) layouts present; "
            "using dir. Remove the flat file or run `mm context migrate` (PR-D).",
            artifact_label,
            name,
            name,
            name,
            dir_filename,
        )

    merged_paths = {**flat, **dirs}  # dir overrides flat on collision
    layouts: dict[str, Layout] = {**dict.fromkeys(flat, "flat"), **dict.fromkeys(dirs, "dir")}
    return [(merged_paths[k], layouts[k]) for k in sorted(merged_paths)]


def resolve_artifact_extract_target(
    canonical_root: Path,
    name: str,
    *,
    artifact_label: ArtifactKind,
    dir_filename: str,
    logger: logging.Logger,
) -> tuple[Path, Layout]:
    """Decide where reverse-sync writes the canonical for ``name``.

    Truth table (ADR-0008 PR-C):
      dir+flat both → dir wins, flat is silently divergent → WARN
      dir only      → dir
      flat only     → flat (preserve existing layout; PR-C does not migrate)
      neither       → dir (ADR-0008 layout for new artifacts)
    """
    resolved = resolve_artifact_under_root(
        canonical_root,
        name,
        artifact_label=artifact_label,
        dir_filename=dir_filename,
        logger=logger,
    )
    if resolved is not None:
        return resolved
    return canonical_root / name / dir_filename, "dir"


def import_passthrough_runtime(
    runtime: str,
    *,
    artifact_label: ArtifactKind,
    dir_filename: str,
    name_kind: str,
    message_kind: str,
    audit_context: Callable[[Path, Path, str], dict[str, object]],
    canonical_root: Path,
    project_root: Path,
    overwrite: bool,
    scope: TargetScope,
    force_unsafe_import: bool,
    dry_run: bool,
    surface: str,
    only_name: str | None,
    imported: list[tuple[Path, Layout]],
    skipped: list[tuple[str, str, skip_codes.SkipCode]],
    seen: dict[str, str],
    logger: logging.Logger,
    source_runtimes: dict[str, str] | None = None,
    runtime_candidates: dict[str, list[str]] | None = None,
    lock_remaining: Callable[[], float] | None = None,
) -> None:
    """Import one runtime's ``*.md`` files into the canonical dir, byte-exact.

    The shared passthrough branch of the reverse import: fan-out-root lookup
    (a missing table entry is a defensive no-op — every shipped
    (label, runtime, scope) tuple is in the table, so ``KeyError`` only
    fires for a future runtime added without table churn), ``*.md`` glob,
    name validation, cross-runtime dedupe, extract-target resolution,
    overwrite gate, Gate A re-scan of the source bytes, and — inside the
    canonical sidecar lock — an authoritative re-resolve + atomic byte write
    (skipped under ``dry_run``). Appends to the caller's ``imported`` /
    ``skipped`` and mutates ``seen`` in place so successive calls (agents:
    claude then gemini) and sibling inline branches (commands: gemini TOML)
    dedupe against each other.

    ADR-0030 §6: the write goes through
    :func:`memtomem.context._canonical_txn.write_canonical_locked`, which holds
    the cross-process name-keyed canonical sidecar lock and **re-resolves the
    destination inside the lock** — so a concurrent flat→dir migrate cannot
    make this write a now-stale path. The lock-free resolve/exists checks above
    stay as a fast path (skip early without reading bytes / scanning), mirroring
    the skills importer's pre-lock preflight + under-lock re-check.
    ``lock_remaining`` (set by the extract wrapper) is the whole-call
    acquisition budget; ``None`` blocks indefinitely (CLI default). An
    overwrite-import (PR-B2b) is snapshot-first for a dir-layout canonical (the
    pre-image is copied into ``versions/`` before the replace, inside the same
    lock); a byte-identical overwrite is a no-op (``identical``), and a
    flat-layout canonical is refused (``snapshot_requires_dir_layout``) since it
    has no version store — see the pre-lock flat fast path above.

    ``seen`` mutation contract (pre-extraction semantics, byte-identical):
    a name is marked seen on CANONICAL_EXISTS (pre-lock or under-lock), on a
    Gate A block, and on successful import — NOT on invalid-name, unreadable,
    or lock-timeout skips (contention is transient and source-runtime-specific,
    so a later runtime's copy keeps its fallback chance).

    ``audit_context`` builds the per-file Gate A audit dict from
    ``(src, dst, name)`` — the shapes intentionally differ per artifact
    (agents carry ``agent_name`` and no ``runtime`` key; commands carry
    ``runtime`` + ``command_name``), so the builder preserves them exactly.

    Gate A notes (pre-extraction comments, still binding): the source bytes
    are re-scanned before any write with ``errors="replace"`` so non-UTF8
    bytes cannot mask an embedded ASCII secret (the replacement char ``�``
    does not overlap with any provider-token alphanumeric pattern).
    ``project_shared`` hard-abort (git-history-is-forever) is raised inside
    :func:`apply_gate_a`; files imported earlier in the run that passed
    Gate A stay (each was scanned independently).
    """
    try:
        runtime_dir = runtime_fanout_root(artifact_label, runtime, scope, project_root)
    except KeyError:
        return
    if runtime_dir is None or not runtime_dir.is_dir():
        return
    runtime_label = f"{runtime} ({runtime_dir})"
    for md_file in sorted(runtime_dir.glob("*.md")):
        name = md_file.stem
        if only_name is not None and name != only_name:
            continue
        if runtime_candidates is not None:
            runtime_candidates.setdefault(name, []).append(runtime)
        try:
            validate_name(name, kind=name_kind)
        except InvalidNameError as exc:
            skipped.append((name, f"invalid name: {exc}", skip_codes.INVALID_NAME))
            logger.warning("skip %r from %s: invalid name", name, runtime_label)
            continue
        if name in seen:
            reason = f"already pulled from {seen[name]}"
            skipped.append((name, reason, skip_codes.ALREADY_IMPORTED))
            logger.warning("skip %s from %s: %s", name, runtime_label, reason)
            continue
        dst, layout = resolve_artifact_extract_target(
            canonical_root,
            name,
            artifact_label=artifact_label,
            dir_filename=dir_filename,
            logger=logger,
        )
        if dst.exists() and not overwrite:
            reason = "canonical exists (use --overwrite)"
            skipped.append((name, reason, skip_codes.CANONICAL_EXISTS))
            logger.warning("skip %s from %s: %s", name, runtime_label, reason)
            seen[name] = runtime_label
            continue
        if dst.exists() and overwrite and layout == "flat":
            # An overwrite-import snapshots the current canonical into its
            # per-artifact ``versions/`` store first (ADR-0030 §6) — a flat
            # ``<name>.md`` has no such store, so refuse rather than clobber
            # unsnapshotted. Fires here (pre-lock) so a ``dry_run`` preview
            # reports the same refusal a real run would (preview/real parity);
            # ``write_canonical_locked`` re-checks under the lock as a backstop
            # against a concurrent dir→flat migrate.
            reason = (
                "cannot overwrite a flat-layout canonical (no version store to "
                "snapshot into) — run `mm context migrate` to convert it to "
                "directory layout first"
            )
            skipped.append((name, reason, skip_codes.SNAPSHOT_REQUIRES_DIR_LAYOUT))
            logger.warning("skip %s from %s: %s", name, runtime_label, reason)
            seen[name] = runtime_label
            continue
        try:
            content_bytes = md_file.read_bytes()
        except OSError as exc:
            skipped.append((name, f"unreadable: {exc}", skip_codes.PARSE_ERROR))
            continue
        content_text = content_bytes.decode("utf-8", errors="replace")
        outcome = apply_gate_a(
            content_text=content_text,
            src=md_file,
            scope=scope,
            force_unsafe_import=force_unsafe_import,
            surface=surface,
            audit_context=audit_context(md_file, dst, name),
            message_kind=message_kind,
            imported_so_far=len(imported),
        )
        if isinstance(outcome, GateABlocked):
            skipped.append(
                (
                    name,
                    f"blocked: {outcome.hits_count} privacy pattern hit(s){outcome.hint}",
                    outcome.code,
                )
            )
            seen[name] = runtime_label
            continue
        # outcome is GateAProceed — write. ``dry_run`` records the
        # would-import target but skips the write so the preview never
        # mutates disk (rank-10).
        if not dry_run:

            def _resolve() -> tuple[Path, Layout]:
                # Re-resolve under the lock (ADR-0030 §6): the layout may have
                # changed (flat→dir migrate) since the pre-lock resolve above.
                return resolve_artifact_extract_target(
                    canonical_root,
                    name,
                    artifact_label=artifact_label,
                    dir_filename=dir_filename,
                    logger=logger,
                )

            try:
                write_outcome, dst, layout = write_canonical_locked(
                    canonical_root,
                    name,
                    content_bytes,
                    resolve_target=_resolve,
                    overwrite=overwrite,
                    snapshot_note=f"pre-overwrite snapshot (import from {runtime})",
                    lock_timeout=None if lock_remaining is None else lock_remaining(),
                )
            except TimeoutError:
                reason = (
                    "another process held the canonical destination lock (or its "
                    f"version store) past the {_CANONICAL_LOCK_BUDGET_S:g}s "
                    "acquisition budget — re-run the pull to retry"
                )
                skipped.append((name, reason, skip_codes.LOCK_TIMEOUT))
                logger.warning("skip %s from %s: %s", name, runtime_label, reason)
                continue
            except SnapshotError as exc:
                # The pre-image snapshot failed, so the overwrite was aborted
                # (fail-closed — never clobber unsnapshotted). Destination-side
                # failure, so ``seen`` is marked: a later runtime's copy would
                # hit the same version-store error.
                reason = f"could not snapshot the current canonical before overwrite: {exc}"
                skipped.append((name, reason, skip_codes.SNAPSHOT_FAILED))
                logger.warning("skip %s from %s: %s", name, runtime_label, reason)
                seen[name] = runtime_label
                continue
            if write_outcome == "exists":
                # Under-lock re-check: a parallel importer landed dst between
                # our lock-free preflight and the lock acquisition.
                reason = "canonical exists (use --overwrite)"
                skipped.append((name, reason, skip_codes.CANONICAL_EXISTS))
                logger.warning("skip %s from %s: %s", name, runtime_label, reason)
                seen[name] = runtime_label
                continue
            if write_outcome == "flat_refused":
                # Under-lock backstop for a concurrent dir→flat migrate — the
                # pre-lock fast path above covers the common case and the
                # dry-run preview.
                reason = (
                    "cannot overwrite a flat-layout canonical (no version store to "
                    "snapshot into) — run `mm context migrate` first"
                )
                skipped.append((name, reason, skip_codes.SNAPSHOT_REQUIRES_DIR_LAYOUT))
                logger.warning("skip %s from %s: %s", name, runtime_label, reason)
                seen[name] = runtime_label
                continue
            # ``created`` / ``overwritten`` / ``identical`` all mean the Store
            # now holds the requested bytes → imported. A future ``WriteOutcome``
            # variant must be handled explicitly rather than silently reported
            # as imported — raise (not ``assert``, which ``python -O`` strips).
            if write_outcome not in ("created", "overwritten", "identical"):
                raise RuntimeError(f"unhandled canonical write outcome: {write_outcome!r}")
        imported.append((dst, layout))
        seen[name] = runtime_label
        if source_runtimes is not None:
            source_runtimes[name] = runtime


def diff_atomic_artifact(
    adapter: AtomicSyncAdapter[T],
    project_root: Path,
    *,
    scope: TargetScope = "project_shared",
) -> list[tuple[str, str, str]]:
    """Compare canonical artifacts against every registered runtime.

    Returns ``(runtime, name, status)`` rows where status is one of
    ``"in sync"``, ``"out of sync"``, ``"missing target"``,
    ``"missing canonical"``, ``"parse error"``, or ``"invalid name"``.
    Diagnostic rows are :class:`DiffRow` instances carrying ``reason``.

    Adapter key contract (#1515 design gate): the loop iterates
    ``adapter.generators`` by ``gen_name`` (e.g. ``"claude_agents"``);
    ``runtime = gen_name.split("_", 1)[0]`` feeds ``runtime_fanout_root``,
    ``runtime_artifact_listing``, and the ``adapter.runtime_suffixes``
    lookup, while ``gen_name`` itself labels the result rows and keys
    ``GENERATOR_VENDOR``.

    ADR-0011 PR-E3: ``scope`` selects both the canonical source and the
    runtime fan-out roots.
    """
    artifact_label = cast(ArtifactKind, adapter.artifact_label)
    results: list[tuple[str, str, str]] = []
    # Key canonicals by the parsed frontmatter ``name:`` — the identity sync
    # targets (``_sync_atomic`` Phase 1: bytes → decode errors="replace" →
    # parse → ``name_of``). Keying by file stem reported permanent phantom
    # drift after a successful sync whenever stem and frontmatter name
    # disagree: ('<stem>', 'missing target') + ('<name>', 'missing
    # canonical') on every runtime, forever (#1229). The lenient decode also
    # keeps a stray non-UTF-8 byte from aborting the whole diff with an
    # uncaught UnicodeDecodeError while sync handles the same file fine.
    # Unreadable / unparseable canonicals keep the stem / dir-name key
    # (``None`` value) so their "parse error" row still names the file.
    canonical_index: dict[str, T | None] = {}
    # Parse failures keep the exception text (it embeds the source path) so
    # the "parse error" row can carry a diagnostic reason — pre-#1229 the
    # exception was swallowed here without even a log line.
    parse_failures: dict[str, str] = {}
    for path, layout in adapter.list_canonical(project_root, scope=scope):
        fallback_name = canonical_artifact_name(path, layout)
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
            parsed = adapter.parse_canonical_text(text, source=path, layout=layout)
        except OSError as exc:
            # Same first-parsed-wins precedence as below (#1247 Codex impl
            # round): a fallback-name entry must not SHADOW an earlier
            # successfully parsed canonical that claimed the same effective
            # name — sync writes that name fine (the unreadable file never
            # claims a name there), so a None overwrite here would report
            # permanent phantom "parse error" drift no sync can clear. The
            # warning below still fires, so the broken file stays loud.
            if canonical_index.get(fallback_name) is None:
                canonical_index[fallback_name] = None
                parse_failures[fallback_name] = f"unreadable: {exc}"
            adapter.logger.warning("canonical %s %s unreadable: %s", adapter.kind, path, exc)
        except adapter.parse_error_type as exc:
            if canonical_index.get(fallback_name) is None:
                canonical_index[fallback_name] = None
                parse_failures[fallback_name] = str(exc)
            adapter.logger.warning("canonical %s %s failed to parse: %s", adapter.kind, path, exc)
        else:
            # First-parsed-wins on a frontmatter-name collision, matching the
            # sync engine's duplicate-name dedupe (#1247) — last-wins here
            # would diff the runtime file against the canonical sync never
            # wrote, reporting permanent phantom drift. The loser surfaces in
            # the sync result as a ``duplicate_name`` skip, not as a diff row
            # (flat-vs-dir precedent: log-only in diff). A ``None`` entry
            # (parse-failure fallback name) stays overwritable — pre-existing
            # interplay, unchanged.
            parsed_name = adapter.name_of(parsed)
            if canonical_index.get(parsed_name) is not None:
                adapter.logger.warning(
                    "duplicate %s name %r: keeping first-seen canonical, ignoring %s",
                    adapter.kind,
                    parsed_name,
                    path,
                )
            else:
                canonical_index[parsed_name] = parsed
    canonical_names = set(canonical_index)

    for gen_name, gen in adapter.generators.items():
        # ADR-0011 PR-E3 cleanup item #1: query the table directly via
        # ``runtime_fanout_root``. Earlier code probed with a fixed artifact
        # name (``__probe_891__``) which leaked the table-shape assumption
        # into the call shape — call-shape fragility, not name-independence.
        runtime = gen_name.split("_", 1)[0]
        if runtime_fanout_root(artifact_label, runtime, scope, project_root) is None:
            continue
        suffix = adapter.runtime_suffixes.get(runtime, ".md")
        runtime_names, invalid_runtime_names = runtime_artifact_listing(
            artifact_label, runtime, project_root, scope, file_suffix=suffix
        )
        # Invalid-named runtime files used to vanish from diff entirely
        # (log-only) — the dashboard read fully in-sync while an unmanaged
        # runtime artifact existed (#1229). Surface them as a dedicated row;
        # a canonical "parse error" row of the same fallback name wins
        # (only possible when the canonical stem itself is invalid).
        for raw_name, invalid_reason in invalid_runtime_names:
            if raw_name not in canonical_names:
                results.append(DiffRow(gen_name, raw_name, "invalid name", invalid_reason))
        for name in sorted(canonical_names | runtime_names):
            # Unparseable canonical (including an invalid *effective* name —
            # frontmatter name, or stem fallback) checked BEFORE the
            # missing-target branch: "missing target" implied sync would
            # create the runtime file, which it never can — the row showed
            # a permanent drift no sync clears (#1229).
            if name in canonical_names and canonical_index[name] is None:
                results.append(DiffRow(gen_name, name, "parse error", parse_failures.get(name)))
                continue
            if name in canonical_names and name not in runtime_names:
                results.append((gen_name, name, "missing target"))
                continue
            if name in runtime_names and name not in canonical_names:
                results.append((gen_name, name, "missing canonical"))
                continue

            item = canonical_index[name]
            if item is None:
                raise RuntimeError(f"canonical parse state changed unexpectedly for {name!r}")
            # Cleanup item #2: the upstream ``runtime_fanout_root`` guard
            # above guarantees this runtime+scope has a fan-out root, so
            # ``gen.target_file`` cannot return ``None`` for any name.
            # Earlier defensive ``if target is None: continue`` removed.
            target = gen.target_file(project_root, name, scope=scope)
            if target is None:
                raise RuntimeError(f"runtime fan-out target unavailable for {gen_name}:{name}")

            # A per-vendor override replaces the rendered runtime file at sync
            # time (``_sync_atomic`` Phase 2 / ADR-0008 Invariant 4) via
            # ``atomic_write_bytes`` — raw bytes, no strip. Compare byte-exact
            # against the same source so an override-carrying artifact isn't
            # reported permanently "out of sync"; decoding as text would crash
            # on a non-UTF-8 override and would mask whitespace-only byte drift.
            vendor = GENERATOR_VENDOR.get(gen_name)
            override_path = (
                _override.resolve(project_root, artifact_label, name, vendor, scope=scope)
                if vendor is not None
                else None
            )
            if override_path is not None:
                try:
                    expected_bytes = override_path.read_bytes()
                except OSError:
                    # Sync skips an unreadable override (no effective fan-out),
                    # so we can't assert parity — report drift, never mask it.
                    results.append((gen_name, name, "out of sync"))
                    continue
                try:
                    actual_bytes = target.read_bytes() if target.is_file() else b""
                except OSError:
                    # Unreadable runtime file — same contract as above.
                    results.append((gen_name, name, "out of sync"))
                    continue
                status = "in sync" if expected_bytes == actual_bytes else "out of sync"
                results.append((gen_name, name, status))
                continue

            expected, _ = gen.render(item)
            # Byte-exact compare against what sync would write (Phase 2
            # ``atomic_write_bytes`` of ``content.encode("utf-8")`` verbatim)
            # — a ``.strip()`` compare reported whitespace-padded runtime
            # files "in sync" while sync would rewrite them (#1229), and a
            # lenient text decode collapses distinct invalid byte sequences
            # to the same U+FFFD. Bytes cannot raise UnicodeDecodeError. An
            # unreadable runtime file can't assert parity — report drift,
            # never mask it.
            try:
                actual_bytes = target.read_bytes() if target.is_file() else b""
            except OSError:
                results.append((gen_name, name, "out of sync"))
                continue
            status = "in sync" if expected.encode("utf-8") == actual_bytes else "out of sync"
            results.append((gen_name, name, status))

    return results


__all__ = [
    "canonical_artifact_name",
    "diff_atomic_artifact",
    "import_passthrough_runtime",
    "list_canonical_artifacts",
    "resolve_artifact_extract_target",
    "resolve_artifact_under_root",
]
