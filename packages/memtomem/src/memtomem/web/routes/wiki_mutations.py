"""Wiki override-seed mutation endpoint (ADR-0008 PR-E E-2, dev tier).

The read-only browser in ``wiki.py`` ships in the prod tier; this companion
router carries the dev-tier mutating verbs — seeding a vendor override, the
in-browser editor save, and the isolated commit (the latter two detailed
below) — and mounts only when ``MEMTOMEM_WEB__MODE=dev``
(``_DEV_ONLY_ROUTERS``). It is the web parity of ``mm wiki <type> override``:
it renders the canonical baseline into
``~/.memtomem-wiki/<type>/<name>/overrides/<vendor>.<ext>`` for the user to
edit and then commit (via the §3 affordance below). Seeding never commits —
ADR-0008 makes seeding a staging step, so the new file is left dirty in the
wiki working tree (the
``wiki_dirty`` flag in the response lets the UI repaint the HEAD badge without
re-listing).

The wiki is host-global, so there is no project-scope resolver, ``target_scope``,
or host-write confirm round-trip (those guard the per-project ``context_*``
surfaces). The dev-tier mount plus the CSRF/Origin/Host guard — POST is an
unsafe method, so the middleware enforces it automatically — are the access
controls; overwrite (``force``) is gated client-side because it clobbers an
existing override (a ``.bak`` sibling keeps the previous content recoverable).

ADR-0027 Editor-A adds the in-browser override **editor** alongside the seed
verb: ``GET …/override`` reads a vendor override's working-tree bytes for the
read pane, and ``PUT …/override`` replaces them with user content under an
optimistic ``mtime_ns`` guard (the ctx skill-editor pattern). Both are dev-tier
(the editor read pane is part of the editor, §D-F); Save writes + leaves the
tree dirty but **never commits**.

ADR-0027 §3 adds the explicit, opt-in **commit** affordance: ``POST …/commit``
builds an *isolated* commit of only the server-resolved target paths
(:meth:`memtomem.wiki.store.WikiStore.commit_paths` — out-of-worktree temp index
→ ``commit-tree`` → ref compare-and-swap), guarded by the in-process
``_gateway_lock``, a wiki-root cross-process file lock, a per-target ``mtime_ns``
token, and the ref CAS against the client's ``expected_head``. Save and commit
stay two acts — auto-commit-on-save is never introduced.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memtomem import privacy
from memtomem.context._names import OVERRIDE_FORMATS, renderable_vendors
from memtomem.wiki.commit import (
    ResolvedTarget,
    WikiTargetChangedError,
    commit_targets,
)
from memtomem.wiki.inspect import (
    CanonicalParseError,
    read_canonical,
    read_override,
    validate_canonical_text,
)
from memtomem.wiki.override import (
    OverrideExistsError,
    SeedResult,
    canonical_asset_file,
    seed_override,
    write_canonical,
    write_override,
)
from memtomem.wiki.store import (
    WikiDetachedHeadError,
    WikiHeadMovedError,
    WikiNotFoundError,
    WikiStore,
    WikiUnbornHeadError,
)
from memtomem.web.routes._errors import _error
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes._wiki_common import (
    AssetType,
    _require_vendor,
    _validate_name_or_error,
    _wiki_absent,
    _wiki_unborn,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/wiki", tags=["wiki-mutations"])


class OverrideSeedRequest(BaseModel):
    """Body for ``POST /api/wiki/{asset_type}/{name}/override``.

    No ``content`` field: unlike skill/agent create, override-seed never takes
    user bytes — it renders the canonical already in the wiki, so there is no
    inbound payload to privacy-scan (Gate A guards the wiki→project install
    direction, not wiki→wiki seeding). ``force`` mirrors the CLI ``--force``.
    """

    vendor: str
    force: bool = False


@router.post("/{asset_type}/{name}/override")
async def seed_wiki_override(asset_type: AssetType, name: str, body: OverrideSeedRequest) -> dict:
    """Seed (or, with ``force``, re-seed) a vendor override from canonical.

    Renders the canonical baseline into ``overrides/<vendor>.<ext>``. ``force``
    overwrites an existing override after writing a ``.bak`` sibling. Never
    commits. Error parity with the read-only routes: wiki absent → 404,
    unrenderable vendor (the ``("commands", "codex")`` placeholder) → 400,
    missing canonical → 404, collision without ``force`` → 409. ``vendor`` and
    ``asset_type``/``name`` are validated before any disk touch.
    """
    _validate_name_or_error(asset_type, name)
    _require_vendor(asset_type, body.vendor)
    store = WikiStore.at_default()

    def _seed() -> SeedResult:
        return seed_override(store, asset_type, name, body.vendor, force=body.force)

    try:
        # Force-seed mutates ``overrides/<vendor>.<ext>`` (and a ``.bak`` on
        # re-seed); hold ``_gateway_lock`` under the same 60s budget as the
        # other three mutators (PUT override/canonical, POST commit) so a seed
        # concurrent with an editor PUT to the same file serialises instead of
        # last-writer-wins (#1385 finding 2). ``_seed`` runs SYNCHRONOUSLY
        # inside the lock — the ``_locks.py`` convention the editor writers
        # follow. A ``to_thread`` offload would reintroduce an await inside the
        # lock: on an ``asyncio.timeout`` the await is cancelled and the lock
        # released, yet the worker thread keeps writing past it, so a second
        # mutator could acquire the lock and race the still-running seed (Codex
        # review gate). With no await between acquire and release, the timeout
        # can only fire while CONTENDING for the lock — never mid-write.
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = _seed()
        # Seeding always leaves the working tree dirty (new/changed file); read
        # it back rather than assume so an identical-bytes re-seed reports
        # clean. The git-status subprocess runs off the event loop AFTER the
        # lock releases (#1518) — the flag is advisory, so a concurrent mutator
        # sneaking in between the write and this read is acceptable (the value
        # still reflects a real repo state).
        wiki_dirty = await asyncio.to_thread(store.is_dirty)
    except WikiNotFoundError as exc:
        raise _wiki_absent(exc) from exc
    except OverrideExistsError as exc:
        # Clean message (no absolute path leak) — the UI already knows the
        # vendor; this 409 is the safety net for a race / direct API call.
        raise _error(
            409,
            "conflict",
            f"override for {body.vendor!r} already exists; re-seed with force to overwrite",
            reason_code="override_exists",
        ) from exc
    except NotImplementedError as exc:
        raise _error(400, "validation", str(exc), reason_code="vendor_unsupported") from exc
    except FileNotFoundError as exc:
        raise _error(
            404,
            "missing",
            f"{asset_type}/{name} has no canonical to seed from",
            reason_code="canonical_absent",
        ) from exc
    except TimeoutError as exc:
        raise _error(
            503, "busy", "wiki override seed timed out — another sync may be in progress"
        ) from exc
    return {
        "seeded": True,
        "override_path": result.path.as_posix(),
        "vendor": body.vendor,
        "forced": body.force,
        "dropped": result.dropped,
        "wiki_dirty": wiki_dirty,
    }


# ── Editor (ADR-0027 Editor-A) ─────────────────────────────────────────────


@router.get("/{asset_type}/{name}/override")
async def read_wiki_override(
    asset_type: AssetType,
    name: str,
    vendor: str = Query(..., description="Override vendor whose bytes to read."),
) -> dict:
    """Read a vendor override's working-tree bytes for the in-browser editor.

    Returns ``{content, mtime_ns, exists}``. ``mtime_ns`` is a string (JS
    bigint-unsafe) — the optimistic-concurrency token the editor echoes back on
    ``PUT``. ``exists=False`` (empty content, ``mtime_ns="0"``) means no override
    has been seeded yet, so the editor opens a blank pane to author one. The
    canonical asset must exist (else 404 ``canonical_absent``); the wiki must
    exist (else 404 ``wiki_absent``). Dev-tier, like the seed verb.
    """
    _validate_name_or_error(asset_type, name)
    _require_vendor(asset_type, vendor)
    store = WikiStore.at_default()
    try:
        override = await asyncio.to_thread(read_override, store, asset_type, name, vendor)
    except WikiNotFoundError as exc:
        raise _wiki_absent(exc) from exc
    except FileNotFoundError as exc:
        raise _error(
            404,
            "missing",
            f"{asset_type}/{name} has no canonical to override",
            reason_code="canonical_absent",
        ) from exc
    return {
        "vendor": vendor,
        "content": override.content,
        "mtime_ns": str(override.mtime_ns),
        "exists": override.exists,
    }


class OverrideEditRequest(BaseModel):
    """Body for ``PUT /api/wiki/{asset_type}/{name}/override``.

    Unlike the content-free seed ``POST``, the editor sends the user's own
    ``content``. ``mtime_ns`` is the token last read from ``GET …/override`` (a
    string — JS bigint-unsafe; ``"0"`` for a not-yet-seeded override). ``force``
    bypasses a stale-mtime conflict — it is the conflict-resolution re-PUT, NOT
    the seed verb's "clobber an existing override" flag (those are different
    concepts; see :func:`memtomem.wiki.override.write_override`).
    """

    vendor: str
    content: str
    mtime_ns: str
    force: bool = False


def _override_mtime_conflict(current_mtime_ns: int) -> JSONResponse:
    # Same 409 shape as the ctx editor (context_skills._mtime_conflict_response)
    # so the SPA's existing conflict-resolution flow is reused verbatim.
    return JSONResponse(
        status_code=409,
        content={
            "status": "aborted",
            "reason": "override was modified by another writer; reload and retry",
            "mtime_ns": str(current_mtime_ns),
            "error_kind": "conflict",
            "reason_code": "stale_mtime",
        },
    )


@router.put("/{asset_type}/{name}/override")
async def edit_wiki_override(
    asset_type: AssetType, name: str, body: OverrideEditRequest
) -> JSONResponse:
    """Replace a vendor override's bytes with user content (mtime-guarded).

    Save semantics (ADR-0027 Editor-A): write the override and leave the wiki
    working tree dirty; **never commit** (parity with the E-2 seed contract).
    Optimistic concurrency mirrors the ctx skill editor: an unlocked pre-check
    and an authoritative re-check inside ``_gateway_lock`` both return the ctx
    editor's 409 ``stale_mtime`` envelope; a 60s lock-acquire timeout → 503.

    Concurrency honesty: ``_gateway_lock`` is an in-process ``asyncio.Lock`` — it
    serializes concurrent *browser* PUTs only. An external writer (a CLI
    ``mm wiki`` or a desktop ``$EDITOR`` on ``~/.memtomem-wiki``) is caught by the
    ``mtime_ns`` token (the re-stat sees the changed mtime → 409), NOT by the
    lock. Editor-A adds no cross-process file lock; the ref-CAS a commit needs is
    the deferred §3 PR.

    Privacy posture (§D-E): ``content`` is scanned with the soft, scope-less
    ``privacy.scan`` and a non-blocking ``privacy_warning`` count is returned —
    the write is never refused (the handler is ``_REDACTION_EXEMPT``, not
    ``_REDACTION_PROTECTED``: a single-curator host-global store).
    """
    _validate_name_or_error(asset_type, name)
    _require_vendor(asset_type, body.vendor)
    if body.vendor not in renderable_vendors(asset_type):
        # No renderer → no canonical baseline to diff against; editing such an
        # override would create state that breaks diff/lint (parity with the seed
        # verb mapping the ``("commands", "codex")`` placeholder to 400).
        raise _error(
            400,
            "validation",
            f"vendor {body.vendor!r} has no renderer for {asset_type}",
            reason_code="vendor_unsupported",
        )
    try:
        body_mtime_ns = int(body.mtime_ns)
    except ValueError as exc:
        raise _error(
            422, "validation", f"invalid mtime_ns: {body.mtime_ns!r}", reason_code="invalid_mtime"
        ) from exc

    store = WikiStore.at_default()
    content_bytes = body.content.encode("utf-8")
    privacy_warning = len(privacy.scan(body.content))

    # Unlocked pre-check (mirrors update_skill): enforce the wiki/canonical gates
    # (→ 404) and refuse a stale-mtime save before taking the lock.
    try:
        pre = read_override(store, asset_type, name, body.vendor)
    except WikiNotFoundError as exc:
        raise _wiki_absent(exc) from exc
    except FileNotFoundError as exc:
        raise _error(
            404,
            "missing",
            f"{asset_type}/{name} has no canonical to override",
            reason_code="canonical_absent",
        ) from exc
    if pre.mtime_ns != body_mtime_ns and not body.force:
        return _override_mtime_conflict(pre.mtime_ns)

    # Locked write: authoritative re-stat inside the lock, then write. The file
    # ops run synchronously inside the lock per the _locks.py convention.
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                current = pre.override_path.stat().st_mtime_ns if pre.override_path.is_file() else 0
                if current != body_mtime_ns:
                    if not body.force:
                        return _override_mtime_conflict(current)
                    logger.warning(
                        "force-save bypassed wiki override mtime check on %s/%s/%s "
                        "(client_mtime_ns=%s server_mtime_ns=%s)",
                        asset_type,
                        name,
                        body.vendor,
                        body_mtime_ns,
                        current,
                    )
                try:
                    target = write_override(store, asset_type, name, body.vendor, content_bytes)
                except FileNotFoundError as exc:
                    # The canonical was removed between the pre-check and this write —
                    # return a fixed-message 404 rather than let write_override's
                    # FileNotFoundError, which embeds the absolute wiki path, escape to
                    # the 500 handler and leak it. (Incidental hardening: the same gap
                    # shipped in Editor-A; fixed here alongside the canonical editor.)
                    raise _error(
                        404,
                        "missing",
                        f"{asset_type}/{name} has no canonical to override",
                        reason_code="canonical_absent",
                    ) from exc
                new_mtime_ns = target.stat().st_mtime_ns
        # Trailing dirty-read: a git-status subprocess, so it runs off the
        # event loop after the lock releases (#1518). Advisory flag — a
        # concurrent mutator between the write and this read is acceptable.
        wiki_dirty = await asyncio.to_thread(store.is_dirty)
    except TimeoutError as exc:
        raise _error(
            503, "busy", "wiki override save timed out — another sync may be in progress"
        ) from exc
    return JSONResponse(
        content={
            "vendor": body.vendor,
            "mtime_ns": str(new_mtime_ns),
            "wiki_dirty": wiki_dirty,
            "privacy_warning": privacy_warning,
        }
    )


# ── Canonical editor (ADR-0027 Editor-B) ────────────────────────────────────


@router.get("/{asset_type}/{name}/canonical")
async def read_wiki_canonical(asset_type: AssetType, name: str) -> dict:
    """Read an asset's base canonical bytes for the in-browser editor's read pane.

    Returns ``{content, mtime_ns}``. ``mtime_ns`` is a string (JS bigint-unsafe) —
    the optimistic-concurrency token the editor echoes back on ``PUT``. Unlike the
    override GET, the canonical **must exist** (Editor-B edits an existing asset);
    a missing canonical → 404 ``canonical_absent``, a missing wiki → 404
    ``wiki_absent``. Dev-tier, like the override verbs and the seed verb.
    """
    _validate_name_or_error(asset_type, name)
    store = WikiStore.at_default()
    try:
        canonical = await asyncio.to_thread(read_canonical, store, asset_type, name)
    except WikiNotFoundError as exc:
        raise _wiki_absent(exc) from exc
    except FileNotFoundError as exc:
        raise _error(
            404,
            "missing",
            f"{asset_type}/{name} has no canonical",
            reason_code="canonical_absent",
        ) from exc
    return {
        "content": canonical.content,
        "mtime_ns": str(canonical.mtime_ns),
    }


class CanonicalEditRequest(BaseModel):
    """Body for ``PUT /api/wiki/{asset_type}/{name}/canonical``.

    The editor sends the user's own canonical ``content``. ``mtime_ns`` is the
    token last read from ``GET …/canonical`` (a string — JS bigint-unsafe).
    ``force`` bypasses a stale-mtime conflict (the conflict-resolution re-PUT).
    There is no ``vendor`` — the canonical is the artifact, vendor-independent.
    """

    content: str
    mtime_ns: str
    force: bool = False


def _canonical_mtime_conflict(current_mtime_ns: int) -> JSONResponse:
    # Same 409 shape as the ctx editor / the override editor so the SPA's existing
    # conflict-resolution flow is reused verbatim.
    return JSONResponse(
        status_code=409,
        content={
            "status": "aborted",
            "reason": "canonical was modified by another writer; reload and retry",
            "mtime_ns": str(current_mtime_ns),
            "error_kind": "conflict",
            "reason_code": "stale_mtime",
        },
    )


@router.put("/{asset_type}/{name}/canonical")
async def edit_wiki_canonical(
    asset_type: AssetType, name: str, body: CanonicalEditRequest
) -> JSONResponse:
    """Replace an asset's base canonical with user content (parse- + mtime-guarded).

    Save semantics (ADR-0027 Editor-B / Decision 2): in fixed
    precondition-before-mutation order so a refused save leaves the prior bytes
    intact —

    1. validate name (the ``AssetType`` Literal already 422s a hostile type);
    2. **parse the new canonical** (agents / commands, ``layout="dir"``): a parse
       failure → 400 ``canonical_unparseable`` and writes nothing — an
       unparseable canonical would break ``render_seed_bytes`` / fan-out for every
       vendor. Skills have no structured parse (byte-copy);
    3. re-check ``mtime_ns`` inside ``_gateway_lock``; a stale value → 409
       ``stale_mtime`` (``force`` overwrites with a WARNING audit). A 60s
       lock-acquire timeout → 503 ``busy``;
    4. write a ``.bak`` sibling, then the canonical, via ``write_canonical``;
    5. read ``store.is_dirty()`` back and return ``wiki_dirty``;
    6. **never** commit (parity with the E-2 seed + the override editor — the
       commit affordance is the deferred ADR-0027 §3 PR).

    Privacy posture (§D-E): ``content`` is scanned with the soft, scope-less
    ``privacy.scan`` and a non-blocking ``privacy_warning`` count is returned —
    the write is never refused (the handler is ``_REDACTION_EXEMPT``, not
    ``_REDACTION_PROTECTED``).
    """
    _validate_name_or_error(asset_type, name)
    try:
        body_mtime_ns = int(body.mtime_ns)
    except ValueError as exc:
        raise _error(
            422, "validation", f"invalid mtime_ns: {body.mtime_ns!r}", reason_code="invalid_mtime"
        ) from exc

    # Parse gate (Decision 6) — pure over the new bytes, so it runs unlocked
    # before the lock and before any disk touch. The message is path-safe
    # (validate_canonical_text feeds a relative source), so it is safe to surface.
    try:
        validate_canonical_text(asset_type, name, body.content)
    except CanonicalParseError as exc:
        raise _error(400, "validation", str(exc), reason_code="canonical_unparseable") from exc

    store = WikiStore.at_default()
    content_bytes = body.content.encode("utf-8")
    privacy_warning = len(privacy.scan(body.content))

    # Unlocked pre-check (mirrors update_skill / the override editor): enforce the
    # wiki/canonical gates (→ 404) and refuse a stale-mtime save before the lock.
    try:
        pre = read_canonical(store, asset_type, name)
    except WikiNotFoundError as exc:
        raise _wiki_absent(exc) from exc
    except FileNotFoundError as exc:
        raise _error(
            404,
            "missing",
            f"{asset_type}/{name} has no canonical to edit",
            reason_code="canonical_absent",
        ) from exc
    if pre.mtime_ns != body_mtime_ns and not body.force:
        return _canonical_mtime_conflict(pre.mtime_ns)

    # Locked write: authoritative re-stat inside the lock, then write.
    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                current = (
                    pre.canonical_path.stat().st_mtime_ns if pre.canonical_path.is_file() else 0
                )
                if current != body_mtime_ns:
                    if not body.force:
                        return _canonical_mtime_conflict(current)
                    logger.warning(
                        "force-save bypassed wiki canonical mtime check on %s/%s "
                        "(client_mtime_ns=%s server_mtime_ns=%s)",
                        asset_type,
                        name,
                        body_mtime_ns,
                        current,
                    )
                try:
                    target = write_canonical(store, asset_type, name, content_bytes)
                except FileNotFoundError as exc:
                    # An external writer removed the canonical between the pre-check
                    # and this write (the in-lock re-stat closes the mtime race but
                    # not a deletion on the force path). Return a fixed-message 404 —
                    # never let write_canonical's FileNotFoundError, which embeds the
                    # absolute wiki path, reach the 500 handler and leak it.
                    raise _error(
                        404,
                        "missing",
                        f"{asset_type}/{name} canonical was removed during the save",
                        reason_code="canonical_absent",
                    ) from exc
                new_mtime_ns = target.stat().st_mtime_ns
        # Trailing dirty-read: a git-status subprocess, so it runs off the
        # event loop after the lock releases (#1518). Advisory flag — a
        # concurrent mutator between the write and this read is acceptable.
        wiki_dirty = await asyncio.to_thread(store.is_dirty)
    except TimeoutError as exc:
        raise _error(
            503, "busy", "wiki canonical save timed out — another sync may be in progress"
        ) from exc
    return JSONResponse(
        content={
            "mtime_ns": str(new_mtime_ns),
            "wiki_dirty": wiki_dirty,
            "privacy_warning": privacy_warning,
        }
    )


# ── Commit affordance (ADR-0027 §3 / D-G) ───────────────────────────────────


class CommitTarget(BaseModel):
    """One typed, server-resolved target in a commit request.

    ``kind`` selects the artifact; ``vendor`` is required for ``override`` and
    ignored for ``canonical``. ``mtime_ns`` is the token the editor last saw for
    that file (from the Save response) — re-``stat``ed under the lock so an
    external same-path edit between Save and Commit is caught (→409). A **raw
    client path is never accepted** — the server resolves the path from the typed
    fields, so the commit can never name anything outside the validated targets.
    """

    kind: Literal["canonical", "override"]
    vendor: str | None = None
    mtime_ns: str


class WikiCommitRequest(BaseModel):
    """Body for ``POST /api/wiki/{asset_type}/{name}/commit``.

    ``expected_head`` is the ``wiki_head`` the client last saw — enforced as the
    atomic compare-and-swap on the ref update (a commit that landed underneath →
    409, never a clobber). ``message`` is user-supplied with a generated default.
    ``force`` bypasses a stale per-target ``mtime_ns`` (a WARNING-audited
    conflict re-commit) — it does **not** bypass the ``expected_head`` CAS.
    """

    expected_head: str
    targets: list[CommitTarget]
    message: str | None = None
    force: bool = False


def _commit_target_conflict(current_mtime_ns: int) -> JSONResponse:
    # Same conflict envelope as the editor (stale_mtime), with a commit-specific
    # reason_code so the SPA can tell a per-file race from a HEAD race.
    return JSONResponse(
        status_code=409,
        content={
            "status": "aborted",
            "reason": "a target file changed on disk since you saved; reload and retry",
            "mtime_ns": str(current_mtime_ns),
            "error_kind": "conflict",
            "reason_code": "stale_target",
        },
    )


def _commit_head_conflict(fresh_head: str) -> JSONResponse:
    return JSONResponse(
        status_code=409,
        content={
            "status": "aborted",
            "reason": "the wiki moved on disk since you loaded it; refresh and retry",
            "wiki_head": fresh_head,
            "error_kind": "conflict",
            "reason_code": "stale_head",
        },
    )


def _resolve_commit_target(
    store: WikiStore, asset_type: str, name: str, target: CommitTarget
) -> tuple[str, Path]:
    """Resolve a typed target to ``(wiki_relative_posix, absolute_path)``.

    Reuses :func:`canonical_asset_file` / :data:`OVERRIDE_FORMATS` so the path
    matches exactly what the writers produce. Raises an HTTP error envelope for
    a bad vendor / unknown override format (never leaks an absolute path).
    """
    if target.kind == "canonical":
        path = canonical_asset_file(store, asset_type, name)
    else:
        if not target.vendor:
            raise _error(
                422, "validation", "override target requires a vendor", reason_code="invalid_target"
            )
        _require_vendor(asset_type, target.vendor)
        fmt = OVERRIDE_FORMATS.get((asset_type, target.vendor))
        if fmt is None:
            raise _error(
                400,
                "validation",
                f"no override format registered for {target.vendor!r}",
                reason_code="vendor_unsupported",
            )
        _, ext = fmt
        path = store.root / asset_type / name / "overrides" / f"{target.vendor}.{ext}"
    return path.relative_to(store.root).as_posix(), path


def _do_commit_blocking(
    store: WikiStore,
    resolved: list[tuple[str, Path, int]],
    *,
    message: str,
    expected_head: str,
    force: bool,
) -> dict:
    """Synchronous commit body — runs in a worker thread so the event loop is
    never blocked by the cross-process lock poll or the git subprocesses.

    A thin adapter over :func:`memtomem.wiki.commit.commit_targets`, the shared
    engine the ``mm wiki {skill,agent,command} commit`` CLI also calls. The web
    supplies the client's per-target ``mtime_ns`` token (``expected_mtime_ns``)
    and its ``expected_head`` CAS value; the engine holds the wiki-root file lock
    for the whole read → commit → ``.bak``-cleanup window and may raise
    :class:`~memtomem.wiki.commit.WikiTargetChangedError` (→ 409 ``stale_target``)
    or :class:`~memtomem.wiki.store.WikiHeadMovedError` (→ 409 ``stale_head``).
    """
    targets = [
        ResolvedTarget(rel=rel, path=path, expected_mtime_ns=token) for rel, path, token in resolved
    ]
    outcome = commit_targets(
        store,
        targets,
        message=message,
        expected_head=expected_head,
        force=force,
    )
    return {
        "committed": outcome.committed,
        "wiki_head": outcome.wiki_head,
        "wiki_dirty": outcome.wiki_dirty,
    }


@router.post("/{asset_type}/{name}/commit")
async def commit_wiki(asset_type: AssetType, name: str, body: WikiCommitRequest) -> JSONResponse:
    """Commit the editor's saved changes to an asset as an isolated git commit.

    Server-resolves the typed ``targets`` to validated wiki-relative paths
    (canonical / vendor overrides for this asset), then under the in-process
    ``_gateway_lock`` runs the blocking commit in a worker thread (so the event
    loop is never blocked by the cross-process lock poll or the git subprocesses).
    The commit is isolated to those paths and CAS-guarded on ``expected_head``.

    Responses: ``{committed: true, wiki_head, wiki_dirty, privacy_warning}`` on
    success; ``committed: false`` (200) when the saved bytes already match HEAD
    (no new history, ``.bak`` still cleaned); 409 ``stale_head`` (HEAD moved) /
    ``stale_target`` (a file changed since Save) / ``detached_head`` (no branch
    checked out — a wiki-state conflict, not a git failure); 503 ``busy`` on
    lock timeout; 500 ``commit_failed`` with a **fixed** message (raw git
    stderr — which embeds ``$HOME`` — is logged server-side only, never
    returned).

    Privacy (§D-E): the commit *message* is new persisted user text, so it is
    scanned with the soft, scope-less ``privacy.scan`` and a non-blocking
    ``privacy_warning`` count is returned — the commit is never refused (the
    handler is ``_REDACTION_EXEMPT``, a valve not a gate).
    """
    _validate_name_or_error(asset_type, name)
    if not body.targets:
        raise _error(422, "validation", "no commit targets supplied", reason_code="no_targets")

    store = WikiStore.at_default()
    message = (body.message or "").strip() or f"wiki: update {asset_type}/{name}"
    privacy_warning = len(privacy.scan(message))

    # Pure resolution + 404 gates before the lock (mirrors the editors). The
    # canonical must exist for the asset (an override needs its canonical too).
    try:
        store.require_exists()
    except WikiNotFoundError as exc:
        raise _wiki_absent(exc) from exc
    if not canonical_asset_file(store, asset_type, name).is_file():
        raise _error(
            404, "missing", f"{asset_type}/{name} has no canonical", reason_code="canonical_absent"
        )

    resolved: list[tuple[str, Path, int]] = []
    for target in body.targets:
        rel, path = _resolve_commit_target(store, asset_type, name, target)
        try:
            token = int(target.mtime_ns)
        except ValueError as exc:
            raise _error(
                422,
                "validation",
                f"invalid mtime_ns: {target.mtime_ns!r}",
                reason_code="invalid_mtime",
            ) from exc
        resolved.append((rel, path, token))

    try:
        async with asyncio.timeout(60):
            async with _gateway_lock:
                result = await asyncio.to_thread(
                    _do_commit_blocking,
                    store,
                    resolved,
                    message=message,
                    expected_head=body.expected_head,
                    force=body.force,
                )
    except WikiTargetChangedError as exc:
        return _commit_target_conflict(exc.current_mtime_ns)
    except WikiHeadMovedError:
        try:
            fresh = store.current_commit()
        except (WikiNotFoundError, WikiUnbornHeadError):
            # The wiki vanished — or its branch was force-reset to empty —
            # between the failed CAS and this freshness probe. The commit
            # still failed because HEAD moved; report the conflict with an
            # unknown fresh head rather than letting the probe's own error
            # escape as a 500.
            fresh = ""
        return _commit_head_conflict(fresh)
    except WikiNotFoundError as exc:
        raise _wiki_absent(exc) from exc
    except WikiDetachedHeadError as exc:
        # Wiki-state precondition, not a git failure: a detached HEAD has no
        # branch ref to CAS-advance, so the 500 ``commit_failed`` catch-all
        # would misclassify it. 409 conflict like the sibling wiki-state
        # envelopes (``override_exists``); the engine message is fixed and
        # deliberately path-free (see WikiDetachedHeadError in wiki/store.py).
        raise _error(409, "conflict", str(exc), reason_code="detached_head") from exc
    except WikiUnbornHeadError as exc:
        # Precede the broad RuntimeError arm like the detached-HEAD arm above:
        # a commit-less wiki is a precise user-fixable state (409 + remedy),
        # not an internal git failure (500).
        raise _wiki_unborn(exc) from exc
    except TimeoutError as exc:
        raise _error(
            503, "busy", "wiki commit timed out — another wiki operation may be in progress"
        ) from exc
    except RuntimeError as exc:
        # git failure: the raw stderr (store.py:_git) embeds the absolute wiki
        # path ($HOME/.memtomem-wiki) — log it server-side, return a fixed message.
        logger.warning("wiki commit failed for %s/%s: %s", asset_type, name, exc)
        raise _error(
            500,
            "internal",
            "git commit failed; check the wiki repo git config and state",
            reason_code="commit_failed",
        ) from exc

    return JSONResponse(content={**result, "privacy_warning": privacy_warning})
