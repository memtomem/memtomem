"""Wiki override-seed mutation endpoint (ADR-0008 PR-E E-2, dev tier).

The read-only browser in ``wiki.py`` ships in the prod tier; this companion
router carries the single mutating verb — seeding a vendor override file — and
mounts only when ``MEMTOMEM_WEB__MODE=dev`` (``_DEV_ONLY_ROUTERS``). It is the
web parity of ``mm wiki <type> override``: it renders the canonical baseline
into ``~/.memtomem-wiki/<type>/<name>/overrides/<vendor>.<ext>`` for the user
to edit and commit themselves. Seeding never commits — ADR-0008 makes seeding
a staging step, so the new file is left dirty in the wiki working tree (the
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
tree dirty but **never commits** (the commit affordance is the deferred §3 PR).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from memtomem import privacy
from memtomem.context._names import renderable_vendors
from memtomem.wiki.inspect import (
    CanonicalParseError,
    read_canonical,
    read_override,
    validate_canonical_text,
)
from memtomem.wiki.override import (
    OverrideExistsError,
    SeedResult,
    seed_override,
    write_canonical,
    write_override,
)
from memtomem.wiki.store import WikiNotFoundError, WikiStore
from memtomem.web.routes._errors import _error
from memtomem.web.routes._locks import _gateway_lock
from memtomem.web.routes._wiki_common import (
    AssetType,
    _require_vendor,
    _validate_name_or_error,
    _wiki_absent,
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

    def _seed() -> tuple[SeedResult, bool]:
        result = seed_override(store, asset_type, name, body.vendor, force=body.force)
        # Seeding always leaves the working tree dirty (new/changed file); read
        # it back rather than assume so an identical-bytes re-seed reports clean.
        return result, store.is_dirty()

    try:
        result, wiki_dirty = await asyncio.to_thread(_seed)
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
                wiki_dirty = store.is_dirty()
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
                wiki_dirty = store.is_dirty()
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
