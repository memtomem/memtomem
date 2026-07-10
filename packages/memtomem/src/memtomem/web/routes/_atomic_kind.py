"""Shared handler bodies for the atomic-kind route pair (#1514).

``context_commands.py`` and ``context_agents.py`` were ~91-95%
line-identical after normalizing the kind token — contractual mirrors
("Mirrored in context_agents.py") whose hand-maintained copies are where
every hardening fix drifted (#1514). This module owns the handler BODIES
once, parametrized by :class:`AtomicKindSpec`; the kind modules keep the
thin ``@router``-decorated functions (decorator + signature + docstring +
one delegation line). That split is deliberate:

- The AST invariant registry (``test_web_invariants_registry.py``) walks
  non-underscore route modules for ``@router.{post,put,patch,delete}``
  decorators — the decorated per-kind functions keep the CSRF/redaction
  classification surface exactly as before.
- FastAPI endpoint names / OpenAPI operationIds derive from the decorated
  function names — unchanged.
- Tests monkeypatch engine functions on the kind modules
  (``monkeypatch.setattr(context_agents, "generate_all_agents", ...)``),
  so every callable on the spec MUST late-bind the kind module's globals
  (``lambda *a, **kw: generate_all_agents(*a, **kw)``), never capture the
  function object at import time.

Skills and mcp-servers do not fold in here: skills diverge by design
(dir-based canonical, no versioning layer, byte-exact diff, thread
offload for cross-process sidecar locks, the decoupled ``import-to-user``
route), mcp-servers are project_shared-only (ADR-0011 §1). They share the
leaf helpers in ``_artifact_common`` instead.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import click
from fastapi import HTTPException
from fastapi.responses import JSONResponse

from memtomem.config import TargetScope
from memtomem.context import versioning
from memtomem.context._atomic import atomic_write_text
from memtomem.context._names import InvalidNameError, validate_name
from memtomem.context.privacy_scan import PrivacyScanError
from memtomem.context.scope_resolver import ArtifactKind, canonical_artifact_dir
from memtomem.web.routes._artifact_common import (
    ArtifactCreateRequest,
    ArtifactUpdateRequest,
    AtomicSyncRequest,
    ImportRequest,
    mtime_conflict_response,
    reject_project_local_write,
    scanned_dirs_for,
)
from memtomem.web.routes._confirm import host_write_gate
from memtomem.web.routes._errors import PRIVACY_BLOCK_DETAIL, PRIVACY_BLOCK_IMPORT_DETAIL, _error
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes._sync_phase import SyncPhaseError
from memtomem.web.routes.context_gateway import (
    _safe_rel,
    delete_skip_entry,
    expected_vs_runtime_row,
    read_text_lenient,
    sanitize_diff_reason,
)
from memtomem.web.routes.context_versions import include_has, version_summary

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AtomicKindSpec:
    """Everything kind-specific the shared handler bodies need.

    Every ``Callable`` field must be a late-binding lambda defined in the
    kind module (see the module docstring) so ``monkeypatch.setattr`` on
    that module keeps intercepting engine calls. Adding a capability flag
    beyond ``rendered_includes_fields`` is the signal to split the
    diverging route back out into its kind module instead.
    """

    kind: str  # singular, lowercase — messages and validate_name ("command")
    kind_plural: ArtifactKind  # response collection key + scope-resolver kind
    canonical_root: str
    dir_filename: str  # working-file name inside a versioned artifact dir
    scan_dirs: list[str]  # project-relative runtime scan hints (detector)
    optional_fields: tuple[str, ...]  # droppable frontmatter keys (rendered)
    rendered_includes_fields: bool  # agents' rendered response carries "fields"
    parse_error: type[Exception]
    strict_drop_error: type[Exception]
    sync_surface: str
    import_surface: str
    generators: Callable[[], Mapping[str, Any]]
    list_canonicals: Callable[..., Any]
    resolve_canonical: Callable[..., Any]
    diff: Callable[..., Any]
    parse_canonical: Callable[..., Any]
    parse_text: Callable[..., Any]
    canonical_name: Callable[..., str]
    generate_all: Callable[..., Any]
    extract_to_canonical: Callable[..., Any]
    fields_from_parsed: Callable[[Any], dict]


# ── Helpers ──────────────────────────────────────────────────────────────


def _artifacts_root(
    spec: AtomicKindSpec, project_root: Path, *, scope: TargetScope = "project_shared"
) -> Path:
    return canonical_artifact_dir(spec.kind_plural, scope, project_root)


def _resolve_existing(
    spec: AtomicKindSpec,
    project_root: Path,
    raw_name: str,
    *,
    scope: TargetScope = "project_shared",
):
    name = validate_name(raw_name, kind=spec.kind)
    return name, spec.resolve_canonical(project_root, name, scope=scope)


def user_sync_host_targets(spec: AtomicKindSpec, project_root: Path) -> list[str]:
    """Pending user-tier fan-out destinations for the sync confirm gate.

    Upper bound on the confirmed sync's writes, resolved from the PARSED
    frontmatter name: ``sync_atomic_artifact`` fans out under
    ``adapter.name_of(parse(...))``, not the canonical filename, so a
    filename-derived disclosure could confirm one path while the engine
    writes another (#1263 review Blocker). Canonicals the engine would
    skip at read/parse time are excluded the same way; later preflight
    skips (Gate A, override errors, conflicts, lock timeouts) only
    shrink the actual write set below this disclosure.
    """
    targets: set[str] = set()
    for path, layout in spec.list_canonicals(project_root, scope="user"):
        try:
            text = path.read_bytes().decode("utf-8", errors="replace")
            parsed = spec.parse_text(text, source=path, layout=layout)
        except (OSError, spec.parse_error):
            continue  # the engine skips these too
        for gen in spec.generators().values():
            dst = gen.target_file(project_root, parsed.name, scope="user")
            if dst is not None:
                targets.add(str(dst))
    return sorted(targets)


def _import_payload(
    spec: AtomicKindSpec,
    result: Any,
    project_root: Path,
    target_scope: TargetScope,
    dry_run: bool | None,
) -> dict:
    """Wire shape shared by both import routes (and the gate's nested plan).

    ``dry_run=None`` omits the key, ``_safe_rel`` keeps user-tier
    ``~/.memtomem`` paths encodable.
    """
    payload: dict = {
        "imported": [
            {
                "name": name,
                "canonical_path": _safe_rel(p, project_root),
                "source_runtime": result.source_runtimes.get(name),
                "duplicate_candidates": result.runtime_candidates.get(name, []),
            }
            for p, layout in result.imported
            for name in (spec.canonical_name(p, layout),)
        ],
        "skipped": [
            {"name": name, "reason": reason, "reason_code": code}
            for name, reason, code in result.skipped
        ],
        "project_root": str(project_root),
        "scanned_dirs": scanned_dirs_for(
            target_scope, kind=spec.kind_plural, project_scan_dirs=spec.scan_dirs
        ),
    }
    if dry_run is not None:
        payload["dry_run"] = dry_run
    return payload


# ── List ─────────────────────────────────────────────────────────────────


async def list_artifacts(
    spec: AtomicKindSpec,
    project_root: Path,
    target_scope: TargetScope,
    include: str | None,
) -> dict:
    want_versions = include_has(include, "versions")
    canonicals = spec.list_canonicals(project_root, scope=target_scope)
    diffs = spec.diff(project_root, scope=target_scope)

    by_name: dict[str, list[dict]] = {}
    for row in diffs:
        entry: dict[str, object] = {"runtime": row[0], "status": row[2]}
        reason = sanitize_diff_reason(getattr(row, "reason", None), project_root)
        if reason:
            entry["reason"] = reason
        by_name.setdefault(row[1], []).append(entry)

    items: list[dict[str, object]] = []
    canonical_names: set[str] = set()
    for path, layout in canonicals:
        name = spec.canonical_name(path, layout)
        canonical_names.add(name)
        item: dict[str, object] = {
            "name": name,
            "canonical_path": _safe_rel(path, project_root),
            "target_scope": target_scope,
            "runtimes": by_name.get(name, []),
        }
        if want_versions:
            item["versions"] = version_summary(path, layout)
        items.append(item)

    for item_name, runtimes in by_name.items():
        if item_name not in canonical_names:
            item = {
                "name": item_name,
                "canonical_path": None,
                "target_scope": target_scope,
                "runtimes": runtimes,
            }
            if want_versions:
                # Runtime-only: no canonical file → nothing to version and no
                # store to migrate. Keep the four-key shape for the JS reader.
                item["versions"] = {
                    "labels": {},
                    "count": 0,
                    "versionable": False,
                    "migrate_required": False,
                }
            items.append(item)

    return {
        spec.kind_plural: items,
        "canonical_root": spec.canonical_root,
        "scanned_dirs": spec.scan_dirs,
    }


# ── Read ─────────────────────────────────────────────────────────────────


async def read_artifact(
    spec: AtomicKindSpec,
    name: str,
    project_root: Path,
    target_scope: TargetScope,
) -> dict:
    name, resolved = _resolve_existing(spec, project_root, name, scope=target_scope)
    if resolved is None:
        raise _error(404, "missing", f"{spec.kind} {name!r} not found")
    path, layout = resolved

    content = path.read_text(encoding="utf-8")
    # mtime_ns is serialized as a string because JavaScript Number cannot
    # safely represent integers > 2^53; nanosecond epochs exceed that.
    mtime_ns = path.stat().st_mtime_ns

    fields: dict = {}
    try:
        parsed = spec.parse_canonical(path, layout=layout)
        fields = spec.fields_from_parsed(parsed)
    except spec.parse_error:
        pass

    # Issue #962 detail meta header — echo back ``target_scope`` and
    # the resolved ``layout`` so the JS meta-header renderer stays
    # type-agnostic across the three artifact types.
    return {
        "name": name,
        "content": content,
        "mtime_ns": str(mtime_ns),
        "fields": fields,
        "target_scope": target_scope,
        "layout": layout,
    }


# ── Rendered (per-runtime output with dropped fields + field map) ────────


async def rendered_artifact(
    spec: AtomicKindSpec,
    name: str,
    project_root: Path,
    target_scope: TargetScope,
) -> JSONResponse:
    name, resolved = _resolve_existing(spec, project_root, name, scope=target_scope)
    if resolved is None:
        raise _error(404, "missing", f"{spec.kind} {name!r} not found")
    path, layout = resolved

    content = path.read_text(encoding="utf-8")
    try:
        parsed = spec.parse_canonical(path, layout=layout)
    except spec.parse_error as exc:
        # ``str(exc)`` embeds the absolute canonical source path (the parser
        # raises ``... (source: {path})`` / ``missing YAML frontmatter:
        # {path}``); route it through the shared display-sanitize boundary so
        # the loopback 422 detail stays path-free — the #1412 fix class the
        # mcp-servers parse-422 already applies.
        detail = sanitize_diff_reason(str(exc), project_root) or ""
        return JSONResponse(
            status_code=422,
            content={"detail": {"error_kind": "parse", "message": f"Parse error: {detail}"}},
        )

    diffs = spec.diff(project_root, scope=target_scope)
    status_map: dict[tuple[str, str], str] = {(rt, n): s for rt, n, s in diffs}

    # ``field_map[field][runtime] = bool`` — True when the runtime keeps the
    # field, False when it drops it, so the front-end renders one matrix for
    # either surface.
    runtimes = []
    field_map: dict[str, dict[str, bool]] = {f: {} for f in spec.optional_fields}

    for gen_name, gen in spec.generators().items():
        rendered_content, dropped_fields = gen.render(parsed)
        status = status_map.get((gen_name, name), "unknown")
        runtimes.append(
            {
                "runtime": gen_name,
                "content": rendered_content,
                "dropped_fields": dropped_fields,
                "status": status,
            }
        )
        dropped_set = set(dropped_fields)
        for f in spec.optional_fields:
            field_map[f][gen_name] = f not in dropped_set

    payload: dict[str, object] = {"name": name, "canonical_content": content}
    if spec.rendered_includes_fields:
        payload["fields"] = spec.fields_from_parsed(parsed)
    payload["runtimes"] = runtimes
    payload["field_map"] = field_map
    return JSONResponse(content=payload)


# ── Create ───────────────────────────────────────────────────────────────


async def create_artifact(
    spec: AtomicKindSpec,
    body: ArtifactCreateRequest,
    project_root: Path,
    target_scope: TargetScope,
) -> dict:
    reject_project_local_write(target_scope, f"Create {spec.kind}")
    name = validate_name(body.name, kind=spec.kind)

    # Unlocked pre-checks so a duplicate is refused (409) rather than
    # confirmed, and the gate discloses the exact artifact dir. The locked
    # re-checks below stay authoritative for create races.
    artifact_dir_unlocked = _artifacts_root(spec, project_root, scope=target_scope) / name
    if (
        spec.resolve_canonical(project_root, name, scope=target_scope) is not None
        or artifact_dir_unlocked.exists()
    ):
        raise _error(
            409,
            "conflict",
            f"{spec.kind.capitalize()} '{name}' already exists",
            reason_code="already_exists",
        )
    gate = host_write_gate(
        target_scope,
        body.allow_host_writes,
        action=f"Create {spec.kind}",
        host_targets=[str(artifact_dir_unlocked)],
    )
    if gate is not None:
        return gate

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                if spec.resolve_canonical(project_root, name, scope=target_scope) is not None:
                    raise _error(
                        409,
                        "conflict",
                        f"{spec.kind.capitalize()} '{name}' already exists",
                        reason_code="already_exists",
                    )
                # ADR-0022: create in versioned directory layout (working file +
                # versions/v1.md + manifest) from the start, so the artifact is
                # immediately versionable in the detail panel instead of a flat
                # file the version UI tells you to ``mm context migrate`` — which
                # then skips it as an unowned manual flat (the split-brain).
                artifact_dir = _artifacts_root(spec, project_root, scope=target_scope) / name
                if artifact_dir.exists():
                    # resolve_canonical found no working file above, but a
                    # stale/orphan directory remains — surface a clean 409 rather
                    # than a 500 from mkdir()'s FileExistsError.
                    raise _error(
                        409,
                        "conflict",
                        f"{spec.kind.capitalize()} '{name}' already exists",
                        reason_code="already_exists",
                    )
                # Encode the submitted content up front, before anything touches
                # disk. A lone-surrogate body can't be UTF-8 encoded; failing
                # after mkdir would leave an orphan dir that wedges retries on the
                # 409 above. These exact bytes become source_bytes for
                # create_version, so v1.md is byte-identical to the working file
                # with no re-read race (_gateway_lock guards only this process).
                try:
                    content_bytes = body.content.encode("utf-8")
                except UnicodeEncodeError as exc:
                    raise _error(
                        400,
                        "validation",
                        f"{spec.kind.capitalize()} content is not valid UTF-8",
                    ) from exc
                artifact_dir.mkdir(parents=True)
                path = artifact_dir / spec.dir_filename
                try:
                    atomic_write_text(path, body.content)
                    versioning.create_version(
                        artifact_dir,
                        path,
                        note="Initial version (created via web)",
                        source_bytes=content_bytes,
                    )
                except BaseException:
                    # Roll back the partial artifact dir so a transient failure
                    # doesn't leave an empty directory that wedges every future
                    # create of this name on the orphan-dir 409 above.
                    shutil.rmtree(artifact_dir, ignore_errors=True)
                    raise
    except TimeoutError:
        raise _error(
            503,
            "busy",
            f"{spec.kind.capitalize()} create timed out — another sync may be in progress",
        )
    return {"name": name, "canonical_path": _safe_rel(path, project_root)}


# ── Update ───────────────────────────────────────────────────────────────


async def update_artifact(
    spec: AtomicKindSpec,
    name: str,
    body: ArtifactUpdateRequest,
    project_root: Path,
    target_scope: TargetScope,
) -> JSONResponse:
    reject_project_local_write(target_scope, f"Update {spec.kind}")
    name, resolved = _resolve_existing(spec, project_root, name, scope=target_scope)
    if resolved is None:
        raise _error(404, "missing", f"{spec.kind} {name!r} not found")
    path, _layout = resolved

    try:
        body_mtime_ns = int(body.mtime_ns)
    except ValueError:
        raise _error(422, "validation", f"Invalid mtime_ns: {body.mtime_ns!r}")

    # Unlocked pre-check before the host-write gate — a stale request is
    # refused, never confirmed (see context_skills.update_skill).
    pre_mtime_ns = path.stat().st_mtime_ns
    if pre_mtime_ns != body_mtime_ns and not body.force:
        return mtime_conflict_response(pre_mtime_ns)
    gate = host_write_gate(
        target_scope,
        body.allow_host_writes,
        action=f"Update {spec.kind}",
        host_targets=[str(path)],
    )
    if gate is not None:
        return JSONResponse(content=gate)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                current_mtime_ns = path.stat().st_mtime_ns
                if current_mtime_ns != body_mtime_ns:
                    if not body.force:
                        return mtime_conflict_response(current_mtime_ns)
                    logger.warning(
                        "force-save bypassed mtime check on %s "
                        "(client_mtime_ns=%s server_mtime_ns=%s)",
                        path,
                        body_mtime_ns,
                        current_mtime_ns,
                    )
                atomic_write_text(path, body.content)
                new_mtime_ns = path.stat().st_mtime_ns
    except TimeoutError:
        raise _error(
            503,
            "busy",
            f"{spec.kind.capitalize()} update timed out — another sync may be in progress",
        )
    return JSONResponse(content={"name": name, "mtime_ns": str(new_mtime_ns)})


# ── Delete ───────────────────────────────────────────────────────────────


async def delete_artifact(
    spec: AtomicKindSpec,
    name: str,
    cascade: bool,
    project_root: Path,
    target_scope: TargetScope,
    allow_host_writes: bool,
) -> dict:
    reject_project_local_write(target_scope, f"Delete {spec.kind}")
    name, resolved = _resolve_existing(spec, project_root, name, scope=target_scope)

    # Pending deletions, computed unlocked (see delete_skill): cascade
    # targets resolve AT THIS TIER — scope= is load-bearing for user-tier
    # cascades. Idempotent no-ops skip the gate.
    pending: list[Path] = [resolved[0]] if resolved is not None else []
    if cascade:
        for gen in spec.generators().values():
            target = gen.target_file(project_root, name, scope=target_scope)
            if target is not None and target.is_file():
                pending.append(target)
    gate = host_write_gate(
        target_scope,
        allow_host_writes,
        action=f"Delete {spec.kind}",
        host_targets=[str(p) for p in pending],
    )
    if gate is not None:
        return gate

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                removed: list[str] = []
                skipped: list[dict[str, str]] = []

                if resolved is not None:
                    path, _layout = resolved
                    try:
                        path.unlink()
                        removed.append(_safe_rel(path, project_root))
                    except OSError as e:
                        skipped.append(delete_skip_entry(path, e, project_root))

                # Sibling of the canonical branch, not nested inside it — a
                # runtime-only artifact (no canonical) + cascade=true must still
                # remove the runtime copies; the nested shape silently no-opped
                # (#1247 id 46). Matches delete_skill.
                if cascade:
                    for gen in spec.generators().values():
                        target = gen.target_file(project_root, name, scope=target_scope)
                        if target is None:
                            continue
                        if not target.is_file():
                            continue
                        try:
                            target.unlink()
                            removed.append(_safe_rel(target, project_root))
                        except OSError as e:
                            skipped.append(delete_skip_entry(target, e, project_root))
    except TimeoutError:
        raise _error(
            503,
            "busy",
            f"{spec.kind.capitalize()} delete timed out — another sync may be in progress",
        )

    return {"deleted": removed, "skipped": skipped}


# ── Diff ─────────────────────────────────────────────────────────────────


async def diff_artifact(
    spec: AtomicKindSpec,
    name: str,
    project_root: Path,
    target_scope: TargetScope,
) -> dict:
    name, resolved = _resolve_existing(spec, project_root, name, scope=target_scope)

    canonical_content = None
    canonical_path = None
    parse_error_reason = None
    parsed = None
    if resolved is not None:
        path, layout = resolved
        canonical_path = _safe_rel(path, project_root)
        # Lenient read + re-parse — the pane must agree with the list badge
        # instead of raw-comparing a malformed canonical into "out of sync" /
        # "in sync" (#1229 U7); a non-UTF-8 canonical must render a
        # diagnosable parse-error pane, not crash the endpoint (#1233).
        canonical_content = read_text_lenient(path)
        if canonical_content is None:
            # Unreadable canonical (permission, race) — same tolerant path as
            # the engine diff: every runtime row reports a diagnosable parse
            # error instead of a 500 (Codex review).
            parse_error_reason = sanitize_diff_reason(f"unreadable: {path}", project_root)
        else:
            try:
                parsed = spec.parse_text(canonical_content, source=path, layout=layout)
            except spec.parse_error as exc:
                parse_error_reason = sanitize_diff_reason(str(exc), project_root)

    runtimes = []
    for gen_name, gen in spec.generators().items():
        # Match the canonical side's tier — see context_skills.py diff_skill
        # for the rationale (#1229). NO_FANOUT tiers return None → skipped.
        target = gen.target_file(project_root, name, scope=target_scope)
        if target is None:
            continue
        if parse_error_reason is not None:
            entry: dict[str, object] = {
                "runtime": gen_name,
                "status": "parse error",
                "reason": parse_error_reason,
            }
            if target.is_file():
                runtime_preview = read_text_lenient(target)
                if runtime_preview is not None:
                    entry["runtime_content"] = runtime_preview
            runtimes.append(entry)
            continue
        if canonical_content is None and not target.is_file():
            continue
        elif canonical_content is not None and not target.is_file():
            runtimes.append({"runtime": gen_name, "status": "missing target"})
        elif canonical_content is None and target.is_file():
            runtimes.append(
                {
                    "runtime": gen_name,
                    "status": "missing canonical",
                    "runtime_content": read_text_lenient(target),
                }
            )
        else:
            # Compare on the engine's basis — vendor override bytes, else
            # rendered output — NOT the raw canonical text: some targets are
            # TOML/YAML, so a raw compare pinned this pane to a permanent
            # "out of sync" under an "in sync" list badge (#1247 id 30).
            assert parsed is not None  # parse failures took the branch above
            artifact = parsed
            runtimes.append(
                expected_vs_runtime_row(
                    kind=spec.kind_plural,
                    gen_name=gen_name,
                    render=lambda gen=gen, artifact=artifact: gen.render(artifact)[0],
                    target=target,
                    name=name,
                    project_root=project_root,
                    scope=target_scope,
                )
            )

    return {
        "name": name,
        "canonical_content": canonical_content,
        "canonical_path": canonical_path,
        "runtimes": runtimes,
    }


# ── Sync ─────────────────────────────────────────────────────────────────


async def sync_core(
    spec: AtomicKindSpec,
    project_root: Path,
    target_scope: TargetScope,
    *,
    on_drop: str = "warn",
    surface: str | None = None,
    force_unsafe: bool = False,
) -> dict:
    """Lock-free atomic-kind sync core — the caller MUST hold ``_gateway_lock``.

    Shared by the standalone per-kind route and ``POST /context/sync-all``
    (#1278), which runs every per-type core under ONE outer lock
    acquisition — the lock is a non-reentrant ``_LoopLocalLock``, so the
    core must never acquire it itself. The engine call stays a direct
    synchronous call (no worker thread): atomic kinds take no
    cross-process file lock — each runtime artifact is one full-content
    atomic ``os.replace`` — so there is no unbounded block to offload
    (the skills/settings cores differ, see ``_sync_skills_core``).

    Engine errors are raised as :class:`SyncPhaseError` — the standalone
    route's historical status/detail pair (privacy 422 keeps its STRING
    detail, issue-pinned; strict-drop keeps its dict detail) plus the
    envelope attributes sync-all renders.
    """
    try:
        result = spec.generate_all(
            project_root,
            on_drop=on_drop,
            scope=target_scope,
            surface=surface if surface is not None else spec.sync_surface,
            force_unsafe=force_unsafe,
        )
    except PrivacyScanError as exc:
        # Path-free detail — ``exc.message`` embeds the absolute canonical path
        # (#1385 finding 1). The chained ``exc`` keeps the full text for logs.
        raise SyncPhaseError(
            422, PRIVACY_BLOCK_DETAIL, error_kind="validation", reason_code="privacy_blocked"
        ) from exc
    except spec.strict_drop_error as exc:
        # on_drop="error" aborts mid-Phase-2 with earlier writes persisted
        # (the #908 partial-write boundary). Surface the partial fan-out
        # instead of an opaque 500 (#1247 id 47): the detail dict follows
        # the #1210 ``{reason_code, message}`` shape the JS error path
        # already renders, plus the writes that landed before the abort.
        # API-only reachability — the UI never sends on_drop="error".
        raise SyncPhaseError(
            422,
            detail={
                "reason_code": "strict_drop",
                "message": str(exc),
                "generated": [
                    {"runtime": rt, "path": _safe_rel(p, project_root)} for rt, p in exc.generated
                ],
            },
            error_kind="validation",
            reason_code="strict_drop",
        ) from exc
    return {
        "generated": [
            {"runtime": rt, "path": _safe_rel(p, project_root)} for rt, p in result.generated
        ],
        "dropped": [
            {"runtime": rt, "name": name, "fields": fields} for rt, name, fields in result.dropped
        ],
        "skipped": [
            {"runtime": rt, "reason": reason, "reason_code": code}
            for rt, reason, code in result.skipped
        ],
        "canonical_root": spec.canonical_root,
    }


async def sync_artifacts(
    spec: AtomicKindSpec,
    body: AtomicSyncRequest | None,
    project_root: Path,
    target_scope: TargetScope,
) -> dict:
    reject_project_local_write(target_scope, f"Sync {spec.kind_plural}")
    on_drop = body.on_drop if body else "warn"
    gate = host_write_gate(
        target_scope,
        body.allow_host_writes if body else False,
        action=f"Sync {spec.kind_plural}",
        host_targets=user_sync_host_targets(spec, project_root),
    )
    if gate is not None:
        return gate
    force_unsafe = body.force_unsafe_sync if body else False
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                return await sync_core(
                    spec, project_root, target_scope, on_drop=on_drop, force_unsafe=force_unsafe
                )
    except TimeoutError:
        raise _error(
            503,
            "busy",
            f"{spec.kind_plural.capitalize()} sync timed out — another sync may be in progress",
        )


# ── Import ───────────────────────────────────────────────────────────────


async def import_artifacts(
    spec: AtomicKindSpec,
    body: ImportRequest | None,
    project_root: Path,
    target_scope: TargetScope,
    dry_run: bool,
) -> dict:
    reject_project_local_write(target_scope, f"Import {spec.kind_plural}")
    overwrite = body.overwrite if body else False
    allow_host_writes = body.allow_host_writes if body else False
    force_unsafe_import = body.force_unsafe_import if body else False

    async def _run(dry: bool) -> Any:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                return spec.extract_to_canonical(
                    project_root,
                    overwrite=overwrite,
                    dry_run=dry,
                    scope=target_scope,
                    force_unsafe_import=force_unsafe_import,
                    surface=spec.import_surface,
                )

    try:
        if not dry_run and target_scope == "user" and not allow_host_writes:
            # Gate disclosure needs the engine's scan — dry-run preview,
            # nested as ``plan`` (see context_skills.import_skills).
            preview = await _run(dry=True)
            gate = host_write_gate(
                target_scope,
                allow_host_writes,
                action=f"Import {spec.kind_plural}",
                host_targets=[str(p) for p, _layout in preview.imported],
                plan=_import_payload(spec, preview, project_root, target_scope, dry_run=True),
            )
            if gate is not None:
                return gate
        result = await _run(dry=dry_run)
    except TimeoutError:
        raise _error(
            503,
            "busy",
            f"{spec.kind_plural.capitalize()} import timed out — another sync may be in progress",
        )
    except click.ClickException as exc:
        # project_shared Gate A privacy block → 422 (see context_skills.import_skills).
        raise HTTPException(422, PRIVACY_BLOCK_IMPORT_DETAIL) from exc
    return _import_payload(spec, result, project_root, target_scope, dry_run=dry_run)


async def import_artifact(
    spec: AtomicKindSpec,
    name: str,
    body: ImportRequest | None,
    project_root: Path,
    target_scope: TargetScope,
) -> dict:
    reject_project_local_write(target_scope, f"Import {spec.kind}")
    try:
        validate_name(name, kind=f"{spec.kind} name")
    except InvalidNameError as exc:
        raise _error(400, "validation", f"Invalid {spec.kind} name: {exc}")
    overwrite = body.overwrite if body else False
    allow_host_writes = body.allow_host_writes if body else False
    force_unsafe_import = body.force_unsafe_import if body else False

    async def _run(dry: bool) -> Any:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                return spec.extract_to_canonical(
                    project_root,
                    overwrite=overwrite,
                    only_name=name,
                    dry_run=dry,
                    scope=target_scope,
                    force_unsafe_import=force_unsafe_import,
                    surface=spec.import_surface,
                )

    try:
        if target_scope == "user" and not allow_host_writes:
            preview = await _run(dry=True)
            if not preview.imported and not preview.skipped:
                raise _error(404, "missing", f"No runtime {spec.kind} named {name!r} to import")
            gate = host_write_gate(
                target_scope,
                allow_host_writes,
                action=f"Import {spec.kind}",
                host_targets=[str(p) for p, _layout in preview.imported],
                plan=_import_payload(spec, preview, project_root, target_scope, dry_run=None),
            )
            if gate is not None:
                return gate
        result = await _run(dry=False)
    except TimeoutError:
        raise _error(
            503,
            "busy",
            f"{spec.kind.capitalize()} import timed out — another sync may be in progress",
        )
    except click.ClickException as exc:
        # project_shared Gate A privacy block → 422 (see context_skills.import_skills).
        raise HTTPException(422, PRIVACY_BLOCK_IMPORT_DETAIL) from exc
    if not result.imported and not result.skipped:
        raise _error(404, "missing", f"No runtime {spec.kind} named {name!r} to import")
    return _import_payload(spec, result, project_root, target_scope, dry_run=None)
