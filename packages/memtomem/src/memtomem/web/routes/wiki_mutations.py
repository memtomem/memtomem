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
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter
from pydantic import BaseModel

from memtomem.wiki.override import OverrideExistsError, SeedResult, seed_override
from memtomem.wiki.store import WikiNotFoundError, WikiStore
from memtomem.web.routes._errors import _error
from memtomem.web.routes._wiki_common import (
    AssetType,
    _require_vendor,
    _validate_name_or_error,
    _wiki_absent,
)

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
