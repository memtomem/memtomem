"""Context gateway — Agents CRUD, diff, sync, import, rendered output, and field map."""

from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

import click
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel, field_validator

from memtomem.config import TargetScope
from memtomem.context import versioning
from memtomem.context._atomic import atomic_write_text
from memtomem.context._names import InvalidNameError, validate_name
from memtomem.context._runtime_targets import KNOWN_RUNTIMES, runtime_fanout_root
from memtomem.context.agents import (
    AGENT_DIR_FILENAME,
    AGENT_GENERATORS,
    CANONICAL_AGENT_ROOT,
    ON_DROP_LEVELS,
    AgentParseError,
    ExtractResult,
    StrictDropError,
    SubAgent,
    _parse_canonical_agent_text,
    canonical_agent_name,
    diff_agents,
    extract_agents_to_canonical,
    generate_all_agents,
    list_canonical_agents,
    parse_canonical_agent,
    resolve_canonical_agent,
)
from memtomem.context.detector import AGENT_DIRS
from memtomem.context.privacy_scan import PrivacyScanError
from memtomem.web.routes.context_gateway import (
    delete_skip_entry,
    expected_vs_runtime_row,
    read_text_lenient,
    sanitize_diff_reason,
)
from memtomem.web.routes.context_projects import (
    resolve_scope_root,
    resolve_scope_root_cascade_gated,
    resolve_writable_scope_root,
)
from memtomem.web.routes.context_versions import include_has, version_summary
from memtomem.web.routes._confirm import host_write_gate
from memtomem.web.routes._errors import _error
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes._sync_phase import SyncPhaseError

# Flat list of project-relative runtime scan paths reported on list / import
# responses so the web UI's empty-state hint can name the exact directories
# the detector inspects without hardcoding them client-side.
_AGENT_SCAN_DIRS: list[str] = [d for paths in AGENT_DIRS.values() for d in paths]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["context-agents"])

# Fields present in the canonical SubAgent that may be dropped per-runtime.
_ALL_OPTIONAL_FIELDS = ("tools", "model", "skills", "isolation", "kind", "temperature")


# ── Helpers ──────────────────────────────────────────────────────────────


def _agents_root(project_root: Path, *, scope: TargetScope = "project_shared") -> Path:
    from memtomem.context.scope_resolver import canonical_artifact_dir

    return canonical_artifact_dir("agents", scope, project_root)


def _resolve_existing_agent(
    project_root: Path, raw_name: str, *, scope: TargetScope = "project_shared"
):
    name = validate_name(raw_name, kind="agent")
    return name, resolve_canonical_agent(project_root, name, scope=scope)


def _reject_project_local_write(target_scope: TargetScope, action: str) -> None:
    """Reject writes on the ``project_local`` tier with HTTP 400.

    See context_skills.py:_reject_project_local_write for the rationale
    (user accepted behind the #1263 host-write confirm; project_local
    deferred per ADR-0011 §3). Mirrored here so each route file stays
    self-contained.
    """
    if target_scope == "project_local":
        raise _error(
            400,
            "validation",
            (
                f"{action} is supported on the project_shared and user tiers; "
                f"project_local is a draft tier with no runtime fan-out "
                f"(ADR-0011 §3)."
            ),
            reason_code="project_local_unsupported",
        )


def _user_scan_dirs() -> list[str]:
    """User-tier runtime roots the import scan reads (absolute, expanded).

    Mirrors context_skills._user_scan_dirs — the project-relative
    ``_AGENT_SCAN_DIRS`` hint would lie on ``target_scope=user``.
    """
    dirs: list[str] = []
    for runtime in KNOWN_RUNTIMES:
        root = runtime_fanout_root("agents", runtime, "user", None)
        if root is not None:
            dirs.append(str(root))
    return sorted(set(dirs))


def _scanned_dirs_for(target_scope: TargetScope) -> list[str]:
    return _user_scan_dirs() if target_scope == "user" else _AGENT_SCAN_DIRS


def _user_sync_host_targets(project_root: Path) -> list[str]:
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
    for agent_path, layout in list_canonical_agents(project_root, scope="user"):
        try:
            text = agent_path.read_bytes().decode("utf-8", errors="replace")
            parsed = _parse_canonical_agent_text(text, source=agent_path, layout=layout)
        except (OSError, AgentParseError):
            continue  # the engine skips these too
        for gen in AGENT_GENERATORS.values():
            dst = gen.target_file(project_root, parsed.name, scope="user")
            if dst is not None:
                targets.add(str(dst))
    return sorted(targets)


def _agent_to_dict(agent: SubAgent) -> dict:
    return {
        "name": agent.name,
        "description": agent.description,
        "tools": agent.tools,
        "model": agent.model,
        "skills": agent.skills,
        "isolation": agent.isolation,
        "kind": agent.kind,
        "temperature": agent.temperature,
    }


# ── List ─────────────────────────────────────────────────────────────────


@router.get("/context/agents")
async def list_agents(
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to list. project_local is shown only "
            "when explicitly requested."
        ),
    ),
    include: str | None = Query(
        None,
        description=(
            "Comma-separated optional enrichments. ``versions`` adds a per-item "
            "``versions`` summary (label pointers + count) to feed the list-card "
            "chips (ADR-0022 PR4); omitted by default so the list stays I/O-free."
        ),
    ),
) -> dict:
    """List canonical agents. Accepts project selector aliases like list_skills."""
    want_versions = include_has(include, "versions")
    canonicals = list_canonical_agents(project_root, scope=target_scope)
    diffs = diff_agents(project_root, scope=target_scope)

    by_name: dict[str, list[dict]] = {}
    for row in diffs:
        entry: dict[str, object] = {"runtime": row[0], "status": row[2]}
        reason = sanitize_diff_reason(getattr(row, "reason", None), project_root)
        if reason:
            entry["reason"] = reason
        by_name.setdefault(row[1], []).append(entry)

    agents: list[dict[str, object]] = []
    canonical_names: set[str] = set()
    for agent_path, layout in canonicals:
        name = canonical_agent_name(agent_path, layout)
        canonical_names.add(name)
        item: dict[str, object] = {
            "name": name,
            "canonical_path": _safe_rel(agent_path, project_root),
            "target_scope": target_scope,
            "runtimes": by_name.get(name, []),
        }
        if want_versions:
            item["versions"] = version_summary(agent_path, layout)
        agents.append(item)

    for agent_name, runtimes in by_name.items():
        if agent_name not in canonical_names:
            item = {
                "name": agent_name,
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
            agents.append(item)

    return {
        "agents": agents,
        "canonical_root": CANONICAL_AGENT_ROOT,
        "scanned_dirs": _AGENT_SCAN_DIRS,
    }


# ── Read ─────────────────────────────────────────────────────────────────


@router.get("/context/agents/{name}")
async def read_agent(
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to read from (ADR-0016).",
    ),
) -> dict:
    name, resolved = _resolve_existing_agent(project_root, name, scope=target_scope)
    if resolved is None:
        raise _error(404, "missing", f"agent {name!r} not found")
    agent_path, layout = resolved

    content = agent_path.read_text(encoding="utf-8")
    # mtime_ns is serialized as a string because JavaScript Number cannot
    # safely represent integers > 2^53; nanosecond epochs exceed that.
    mtime_ns = agent_path.stat().st_mtime_ns

    fields: dict = {}
    try:
        parsed = parse_canonical_agent(agent_path, layout=layout)
        fields = _agent_to_dict(parsed)
    except AgentParseError:
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


@router.get("/context/agents/{name}/rendered")
async def rendered_agent(
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to render (ADR-0016).",
    ),
) -> JSONResponse:
    name, resolved = _resolve_existing_agent(project_root, name, scope=target_scope)
    if resolved is None:
        raise _error(404, "missing", f"agent {name!r} not found")
    agent_path, layout = resolved

    content = agent_path.read_text(encoding="utf-8")
    try:
        parsed = parse_canonical_agent(agent_path, layout=layout)
    except AgentParseError as exc:
        return JSONResponse(
            status_code=422,
            content={"detail": {"error_kind": "parse", "message": f"Parse error: {exc}"}},
        )

    diffs = diff_agents(project_root, scope=target_scope)
    status_map: dict[tuple[str, str], str] = {(rt, n): s for rt, n, s in diffs}

    runtimes = []
    field_map: dict[str, dict[str, bool]] = {f: {} for f in _ALL_OPTIONAL_FIELDS}

    for gen_name, gen in AGENT_GENERATORS.items():
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
        # Build field map
        dropped_set = set(dropped_fields)
        for f in _ALL_OPTIONAL_FIELDS:
            field_map[f][gen_name] = f not in dropped_set

    return JSONResponse(
        content={
            "name": name,
            "canonical_content": content,
            "fields": _agent_to_dict(parsed),
            "runtimes": runtimes,
            "field_map": field_map,
        }
    )


# ── Create ───────────────────────────────────────────────────────────────


class AgentCreateRequest(BaseModel):
    name: str
    content: str
    # #1263 host-write opt-in: required true for target_scope=user (the
    # canonical lands under ~/.memtomem/, outside any project root). The
    # first POST without it returns the needs_confirmation envelope.
    allow_host_writes: bool = False


@router.post("/context/agents")
async def create_agent(
    body: AgentCreateRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to create in. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
) -> dict:
    _reject_project_local_write(target_scope, "Create agent")
    name = validate_name(body.name, kind="agent")

    # Unlocked pre-checks so a duplicate is refused (409) rather than
    # confirmed, and the gate discloses the exact artifact dir. The locked
    # re-checks below stay authoritative for create races.
    artifact_dir_unlocked = _agents_root(project_root, scope=target_scope) / name
    if (
        resolve_canonical_agent(project_root, name, scope=target_scope) is not None
        or artifact_dir_unlocked.exists()
    ):
        raise _error(
            409, "conflict", f"Agent '{name}' already exists", reason_code="already_exists"
        )
    gate = host_write_gate(
        target_scope,
        body.allow_host_writes,
        action="Create agent",
        host_targets=[str(artifact_dir_unlocked)],
    )
    if gate is not None:
        return gate

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                if resolve_canonical_agent(project_root, name, scope=target_scope) is not None:
                    raise _error(
                        409,
                        "conflict",
                        f"Agent '{name}' already exists",
                        reason_code="already_exists",
                    )
                # ADR-0022: create in versioned directory layout (agent.md +
                # versions/v1.md + manifest) from the start, so the artifact is
                # immediately versionable in the detail panel instead of a flat
                # file the version UI tells you to ``mm context migrate`` — which
                # then skips it as an unowned manual flat (the split-brain).
                # Mirrored in context_commands.py.
                artifact_dir = _agents_root(project_root, scope=target_scope) / name
                if artifact_dir.exists():
                    # resolve_canonical_agent found no working file above, but a
                    # stale/orphan directory remains — surface a clean 409 rather
                    # than a 500 from mkdir()'s FileExistsError.
                    raise _error(
                        409,
                        "conflict",
                        f"Agent '{name}' already exists",
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
                    raise _error(400, "validation", "Agent content is not valid UTF-8") from exc
                artifact_dir.mkdir(parents=True)
                agent_path = artifact_dir / AGENT_DIR_FILENAME
                try:
                    atomic_write_text(agent_path, body.content)
                    versioning.create_version(
                        artifact_dir,
                        agent_path,
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
        raise _error(503, "busy", "Agent create timed out — another sync may be in progress")
    return {"name": name, "canonical_path": _safe_rel(agent_path, project_root)}


# ── Update ───────────────────────────────────────────────────────────────


class AgentUpdateRequest(BaseModel):
    content: str
    # mtime_ns is transported as a string (JS bigint-unsafe); parsed to int in handler.
    mtime_ns: str
    # Bypass the mtime guard. The Web UI sets this only after the user
    # explicitly chose "Force save" in the conflict resolution dialog
    # (see issue #763); every force-save emits a WARNING with both mtime
    # values for the audit trail.
    force: bool = False
    # #1263 host-write opt-in for target_scope=user (see AgentCreateRequest).
    allow_host_writes: bool = False


_MTIME_CONFLICT_REASON = "File was modified by another process. Reload and retry."


def _mtime_conflict_response(current_mtime_ns: int) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "status": "aborted",
            "reason": _MTIME_CONFLICT_REASON,
            "mtime_ns": str(current_mtime_ns),
            "error_kind": "conflict",
            "reason_code": "stale_mtime",
        },
    )


@router.put("/context/agents/{name}")
async def update_agent(
    name: str,
    body: AgentUpdateRequest,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to update. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
) -> JSONResponse:
    _reject_project_local_write(target_scope, "Update agent")
    name, resolved = _resolve_existing_agent(project_root, name, scope=target_scope)
    if resolved is None:
        raise _error(404, "missing", f"agent {name!r} not found")
    agent_path, _layout = resolved

    try:
        body_mtime_ns = int(body.mtime_ns)
    except ValueError:
        raise _error(422, "validation", f"Invalid mtime_ns: {body.mtime_ns!r}")

    # Unlocked pre-check before the host-write gate — a stale request is
    # refused, never confirmed (see context_skills.update_skill).
    pre_mtime_ns = agent_path.stat().st_mtime_ns
    if pre_mtime_ns != body_mtime_ns and not body.force:
        return _mtime_conflict_response(pre_mtime_ns)
    gate = host_write_gate(
        target_scope,
        body.allow_host_writes,
        action="Update agent",
        host_targets=[str(agent_path)],
    )
    if gate is not None:
        return JSONResponse(content=gate)

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                current_mtime_ns = agent_path.stat().st_mtime_ns
                if current_mtime_ns != body_mtime_ns:
                    if not body.force:
                        return _mtime_conflict_response(current_mtime_ns)
                    logger.warning(
                        "force-save bypassed mtime check on %s "
                        "(client_mtime_ns=%s server_mtime_ns=%s)",
                        agent_path,
                        body_mtime_ns,
                        current_mtime_ns,
                    )
                atomic_write_text(agent_path, body.content)
                new_mtime_ns = agent_path.stat().st_mtime_ns
    except TimeoutError:
        raise _error(503, "busy", "Agent update timed out — another sync may be in progress")
    return JSONResponse(content={"name": name, "mtime_ns": str(new_mtime_ns)})


# ── Delete ───────────────────────────────────────────────────────────────


def _safe_rel(p: Path, project_root: Path) -> str:
    """Project-relative path as a POSIX string for API payloads.

    ``.as_posix()`` (not ``str``) so canonical/runtime paths come back
    ``/``-separated on every platform — the Web UI and diff payloads pin POSIX
    separators (#1256). Falls back to the absolute POSIX path for user-tier
    locations outside ``project_root``.
    """
    try:
        return p.relative_to(project_root).as_posix()
    except ValueError:
        return p.as_posix()


@router.delete("/context/agents/{name}")
async def delete_agent(
    name: str,
    cascade: bool = Query(False),
    project_root: Path = Depends(resolve_scope_root_cascade_gated),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to delete from. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
    allow_host_writes: bool = Query(
        False,
        description=(
            "#1263 host-write opt-in for target_scope=user. Query parameter "
            "(not a body field) because DELETE bodies are client-hostile; "
            "the needs_confirmation envelope names the same flag."
        ),
    ),
) -> dict:
    _reject_project_local_write(target_scope, "Delete agent")
    name, resolved = _resolve_existing_agent(project_root, name, scope=target_scope)

    # Pending deletions, computed unlocked (see delete_skill): cascade
    # targets resolve AT THIS TIER — scope= is load-bearing for user-tier
    # cascades. Idempotent no-ops skip the gate.
    pending: list[Path] = [resolved[0]] if resolved is not None else []
    if cascade:
        for gen in AGENT_GENERATORS.values():
            target = gen.target_file(project_root, name, scope=target_scope)
            if target is not None and target.is_file():
                pending.append(target)
    gate = host_write_gate(
        target_scope,
        allow_host_writes,
        action="Delete agent",
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
                    agent_path, _layout = resolved
                    try:
                        agent_path.unlink()
                        removed.append(_safe_rel(agent_path, project_root))
                    except OSError as e:
                        skipped.append(delete_skip_entry(agent_path, e, project_root))

                if cascade:
                    for gen in AGENT_GENERATORS.values():
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
        raise _error(503, "busy", "Agent delete timed out — another sync may be in progress")

    return {"deleted": removed, "skipped": skipped}


# ── Diff ─────────────────────────────────────────────────────────────────


@router.get("/context/agents/{name}/diff")
async def diff_agent(
    name: str,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description="Canonical-residency tier to diff against runtime fan-out (ADR-0016).",
    ),
) -> dict:
    name, resolved = _resolve_existing_agent(project_root, name, scope=target_scope)

    canonical_content = None
    canonical_path = None
    parse_error_reason = None
    parsed = None
    if resolved is not None:
        agent_path, layout = resolved
        canonical_path = _safe_rel(agent_path, project_root)
        # Lenient read — a non-UTF-8 canonical must render a diagnosable
        # parse-error pane, not crash the endpoint (#1233 contract).
        canonical_content = read_text_lenient(agent_path)
        # Re-parse so this pane agrees with the list badge: the raw string
        # compare below reported a malformed canonical as "out of sync" /
        # "in sync" while the list said "parse error" (#1229 U7).
        if canonical_content is None:
            # Unreadable canonical (permission, race) — same tolerant path as
            # the engine diff: every runtime row reports a diagnosable parse
            # error instead of a 500 (Codex review).
            parse_error_reason = sanitize_diff_reason(f"unreadable: {agent_path}", project_root)
        else:
            try:
                parsed = _parse_canonical_agent_text(
                    canonical_content, source=agent_path, layout=layout
                )
            except AgentParseError as exc:
                parse_error_reason = sanitize_diff_reason(str(exc), project_root)

    runtimes = []
    for gen_name, gen in AGENT_GENERATORS.items():
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
            # rendered output — NOT the raw canonical text: codex/kimi targets
            # are TOML/YAML, so a raw compare pinned this pane to a permanent
            # "out of sync" under an "in sync" list badge (#1247 id 30).
            assert parsed is not None  # parse failures took the branch above
            agent = parsed
            runtimes.append(
                expected_vs_runtime_row(
                    kind="agents",
                    gen_name=gen_name,
                    render=lambda gen=gen, agent=agent: gen.render(agent)[0],
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


class SyncRequest(BaseModel):
    on_drop: str = "warn"
    # #1263 host-write opt-in for target_scope=user (see AgentCreateRequest).
    allow_host_writes: bool = False
    # Gate A bypass valve for fan-out — the sync-side mirror of
    # ImportRequest.force_unsafe_import (#1379). user-tier only;
    # project_shared hard-refuses regardless (ADR-0011 §5). See
    # context_skills.SyncRequest.
    force_unsafe_sync: bool = False

    # An out-of-vocabulary value used to slip through to the engine, where it
    # silently behaved as "ignore" (#1247 id 47) — reject at the boundary
    # instead (FastAPI renders the ValueError as a native 422). Validates
    # against the engine's ON_DROP_LEVELS so the vocabulary has one owner
    # (no Literal duplication; CLI click.Choice and MCP _validate_on_drop
    # already gate the same way).
    @field_validator("on_drop")
    @classmethod
    def _check_on_drop(cls, value: str) -> str:
        if value not in ON_DROP_LEVELS:
            raise ValueError(f"on_drop must be one of {ON_DROP_LEVELS}, got {value!r}")
        return value


async def _sync_agents_core(
    project_root: Path,
    target_scope: TargetScope,
    *,
    on_drop: str = "warn",
    surface: str = "web_context_agents_sync",
    force_unsafe: bool = False,
) -> dict:
    """Lock-free agents sync core — the caller MUST hold ``_gateway_lock``.

    Shared by the standalone route below and ``POST /context/sync-all``
    (#1278), which runs every per-type core under ONE outer lock
    acquisition — the lock is a non-reentrant ``_LoopLocalLock``, so the
    core must never acquire it itself. The engine call stays a direct
    synchronous call (no worker thread): agents take no cross-process
    file lock — each runtime artifact is one full-content atomic
    ``os.replace`` — so there is no unbounded block to offload (the
    skills/settings cores differ, see ``_sync_skills_core``).

    Engine errors are raised as :class:`SyncPhaseError` — the standalone
    route's historical status/detail pair (privacy 422 keeps its STRING
    detail, issue-pinned; strict-drop keeps its dict detail) plus the
    envelope attributes sync-all renders.
    """
    try:
        result = generate_all_agents(
            project_root,
            on_drop=on_drop,
            scope=target_scope,
            surface=surface,
            force_unsafe=force_unsafe,
        )
    except PrivacyScanError as exc:
        # 422 Unprocessable Entity — request is well-formed but the canonical
        # bytes violate the project_shared privacy gate. ADR-0011 §5: no
        # bypass valve, so the user must remove the secret or migrate the
        # artifact to a writable tier (the message body explains how).
        raise SyncPhaseError(
            422, exc.message, error_kind="validation", reason_code="privacy_blocked"
        ) from exc
    except StrictDropError as exc:
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
        "canonical_root": CANONICAL_AGENT_ROOT,
    }


@router.post("/context/agents/sync")
async def sync_agents(
    body: SyncRequest | None = None,
    project_root: Path = Depends(resolve_writable_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to fan out. user fans out to the host "
            "~/.claude-family roots behind the allow_host_writes confirm "
            "round-trip (#1263); project_local rejected — no runtime fan-out "
            "per ADR-0011 §3."
        ),
    ),
) -> dict:
    _reject_project_local_write(target_scope, "Sync agents")
    on_drop = body.on_drop if body else "warn"
    gate = host_write_gate(
        target_scope,
        body.allow_host_writes if body else False,
        action="Sync agents",
        host_targets=_user_sync_host_targets(project_root),
    )
    if gate is not None:
        return gate
    force_unsafe = body.force_unsafe_sync if body else False
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                return await _sync_agents_core(
                    project_root, target_scope, on_drop=on_drop, force_unsafe=force_unsafe
                )
    except TimeoutError:
        raise _error(503, "busy", "Agents sync timed out — another sync may be in progress")


# ── Import ───────────────────────────────────────────────────────────────


class ImportRequest(BaseModel):
    overwrite: bool = False
    # #1263 host-write opt-in for target_scope=user (the canonical
    # destination is ~/.memtomem/agents/, outside any project root).
    allow_host_writes: bool = False
    # Gate A bypass valve — mirrors the CLI's --force-unsafe-import and the
    # ``force_unsafe`` field the upload/memory/chunk web write surfaces already
    # expose. Lets a reviewed false positive proceed on the only bypassable web
    # import tier: ``user``. ``project_local`` is rejected outright and
    # ``project_shared`` hard-refuses regardless of this flag (ADR-0011 §5),
    # enforced in the import engine. See context_skills.ImportRequest.
    force_unsafe_import: bool = False


def _import_payload(
    result: ExtractResult,
    project_root: Path,
    target_scope: TargetScope,
    dry_run: bool | None,
) -> dict:
    """Wire shape shared by both import routes (and the gate's nested plan).

    Mirrors context_skills._import_payload — ``dry_run=None`` omits the
    key, ``_safe_rel`` keeps user-tier ``~/.memtomem`` paths encodable.
    """
    payload: dict = {
        "imported": [
            {
                "name": canonical_agent_name(p, layout),
                "canonical_path": _safe_rel(p, project_root),
            }
            for p, layout in result.imported
        ],
        "skipped": [
            {"name": name, "reason": reason, "reason_code": code}
            for name, reason, code in result.skipped
        ],
        "project_root": str(project_root),
        "scanned_dirs": _scanned_dirs_for(target_scope),
    }
    if dry_run is not None:
        payload["dry_run"] = dry_run
    return payload


@router.post("/context/agents/import")
async def import_agents(
    body: ImportRequest | None = None,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to import into. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
    dry_run: bool = Query(
        False,
        description=(
            "Preview the import without writing to canonical (rank-10): runs the "
            "full scan + privacy walk + dedup and returns the would-import / would-"
            "skip counts, leaving disk untouched. Returned regardless of "
            "confirmation flags (mirrors the transfer route's dry_run)."
        ),
    ),
) -> dict:
    _reject_project_local_write(target_scope, "Import agents")
    overwrite = body.overwrite if body else False
    allow_host_writes = body.allow_host_writes if body else False
    force_unsafe_import = body.force_unsafe_import if body else False

    async def _run(dry: bool) -> ExtractResult:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                return extract_agents_to_canonical(
                    project_root,
                    overwrite=overwrite,
                    dry_run=dry,
                    scope=target_scope,
                    force_unsafe_import=force_unsafe_import,
                    surface="web_context_agents_import",
                )

    try:
        if not dry_run and target_scope == "user" and not allow_host_writes:
            # Gate disclosure needs the engine's scan — dry-run preview,
            # nested as ``plan`` (see context_skills.import_skills).
            preview = await _run(dry=True)
            gate = host_write_gate(
                target_scope,
                allow_host_writes,
                action="Import agents",
                host_targets=[str(p) for p, _layout in preview.imported],
                plan=_import_payload(preview, project_root, target_scope, dry_run=True),
            )
            if gate is not None:
                return gate
        result = await _run(dry=dry_run)
    except TimeoutError:
        raise _error(503, "busy", "Agents import timed out — another sync may be in progress")
    except click.ClickException as exc:
        # project_shared Gate A privacy block → 422 (see context_skills.import_skills).
        raise HTTPException(422, exc.message) from exc
    return _import_payload(result, project_root, target_scope, dry_run=dry_run)


@router.post("/context/agents/{name}/import")
async def import_agent(
    name: str,
    body: ImportRequest | None = None,
    project_root: Path = Depends(resolve_scope_root),
    target_scope: TargetScope = Query(
        "project_shared",
        description=(
            "Canonical-residency tier to import into. user requires the "
            "allow_host_writes confirm round-trip; project_local rejected (ADR-0011 §3)."
        ),
    ),
) -> dict:
    """Import a single runtime agent into the scoped canonical dir.

    Same response shape as the section-level import so the web UI can reuse
    its rendering. 404 when no runtime file matches the name (the section
    import would silently report 0 imported, which is the wrong shape of
    feedback for "you clicked a specific item that doesn't exist") — pinned
    on the gate's dry-run preview too.
    """
    _reject_project_local_write(target_scope, "Import agent")
    try:
        validate_name(name, kind="agent name")
    except InvalidNameError as exc:
        raise _error(400, "validation", f"Invalid agent name: {exc}")
    overwrite = body.overwrite if body else False
    allow_host_writes = body.allow_host_writes if body else False
    force_unsafe_import = body.force_unsafe_import if body else False

    async def _run(dry: bool) -> ExtractResult:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                return extract_agents_to_canonical(
                    project_root,
                    overwrite=overwrite,
                    only_name=name,
                    dry_run=dry,
                    scope=target_scope,
                    force_unsafe_import=force_unsafe_import,
                    surface="web_context_agents_import",
                )

    try:
        if target_scope == "user" and not allow_host_writes:
            preview = await _run(dry=True)
            if not preview.imported and not preview.skipped:
                raise _error(404, "missing", f"No runtime agent named {name!r} to import")
            gate = host_write_gate(
                target_scope,
                allow_host_writes,
                action="Import agent",
                host_targets=[str(p) for p, _layout in preview.imported],
                plan=_import_payload(preview, project_root, target_scope, dry_run=None),
            )
            if gate is not None:
                return gate
        result = await _run(dry=False)
    except TimeoutError:
        raise _error(503, "busy", "Agent import timed out — another sync may be in progress")
    except click.ClickException as exc:
        # project_shared Gate A privacy block → 422 (see context_skills.import_skills).
        raise HTTPException(422, exc.message) from exc
    if not result.imported and not result.skipped:
        raise _error(404, "missing", f"No runtime agent named {name!r} to import")
    return _import_payload(result, project_root, target_scope, dry_run=None)
