"""Browser tests for the per-project Sync button on Projects portal cards.

These rehome the behaviour the (removed) Overview ``projects matrix`` used to
guard — per-project fan-out sync — onto the Projects portal card, the single
roster after the rank-2 consolidation. The matrix-specific render/badge tests
were dropped with the matrix UI; the behaviour that still matters moved here:

* Sync fans out the row's ``scope_id`` to all five sync routes and refreshes
  the **portal** (not the Overview the matrix lived on) afterwards.
* Server CWD (empty effective scope_id) syncs without a ``scope_id`` param.
* Sync syncs the row only — it never mutates the active-project selection
  (only Use does). This is the user-confirmed state-model invariant.
* The button rides the tier write-block sweep (``data-write-blocked``) and is
  eligibility-gated (paused / not-enrolled / project_local / missing) with the
  reason on ``data-i18n-title`` so the sweep can restore it.

``page.route`` short-circuits every ``/api`` call, so no CSRF/DB is in play —
the spec asserts render + click wiring only.
"""

from __future__ import annotations

import json
import time

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


# One scope per eligibility shape (mirrors the matrix #1203 gating fixture):
# Server CWD + an enrolled-enabled control are sync-eligible; ``scope-paused``
# is enrolled-but-paused; ``scope-scan`` is scan-only (never enrolled);
# ``scope-missing`` is enrolled but its root is gone (missing arm wins).
def _scope(scope_id, label, *, sources, enabled, sync_eligible, missing=False, counts=None):
    return {
        "scope_id": scope_id,
        "project_scope_id": scope_id,
        "label": label,
        "root": f"/fake/{scope_id or 'cwd'}",
        "tier": "project",
        "sources": sources,
        "experimental": False,
        "missing": missing,
        "stale": False,
        "enabled": enabled,
        "sync_eligible": sync_eligible,
        "counts": counts
        if counts is not None
        else {"skills": 1, "commands": 0, "agents": 0, "mcp-servers": 0},
    }


_SYNC_SCOPES = {
    "target_scope": "project_shared",
    "scopes": [
        _scope("", "Server CWD", sources=["server-cwd"], enabled=True, sync_eligible=True),
        _scope(
            "scope-on",
            "Enabled Project",
            sources=["known-projects"],
            enabled=True,
            sync_eligible=True,
        ),
        _scope(
            "scope-paused",
            "Paused Project",
            sources=["known-projects"],
            enabled=False,
            sync_eligible=False,
        ),
        _scope(
            "scope-scan",
            "Scanned Project",
            sources=["claude-projects"],
            enabled=True,
            sync_eligible=False,
        ),
        _scope(
            "scope-missing",
            "Missing Project",
            sources=["known-projects"],
            enabled=True,
            sync_eligible=True,
            missing=True,
            counts=None,
        ),
    ],
}

# Minimal overview payload (per-type sync tiles) for the Sync-All gating test,
# which navigates to the Overview after picking an ineligible active scope.
_HEALTHY_OVERVIEW = {
    "skills": {"total": 2, "in_sync": 2},
    "commands": {"total": 1, "in_sync": 1},
    "agents": {"total": 3, "in_sync": 3},
    "settings": {
        "total": 1,
        "in_sync": 1,
        "out_of_sync": 0,
        "missing_target": 0,
        "error": 0,
        "status": "in_sync",
    },
}

_SYNC_ROUTES = (
    "**/api/context/skills/sync**",
    "**/api/context/commands/sync**",
    "**/api/context/agents/sync**",
    "**/api/context/mcp-servers/sync**",
    "**/api/context/settings/sync**",
)


def _stub_portal_sync(page) -> dict:
    """Stub projects + runtimes + the five sync routes. Returns a mutable dict
    with ``sync`` (list of synced URLs) and ``projects`` (fetch count, for the
    post-sync refresh assertion). Registered AFTER install_default_stubs so they
    win (last-route-wins)."""
    install_default_stubs(page)
    state = {"sync": [], "projects": 0}

    def _projects(route):
        state["projects"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_SYNC_SCOPES))

    page.route("**/api/context/projects**", _projects)

    # Minimal per-scope runtimes so the chips/dots render without error.
    page.route(
        "**/api/context/runtimes**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"project_root": "/fake", "runtimes": []}),
        ),
    )

    # Overview payload (only the Sync-All gating test navigates there).
    page.route(
        "**/api/context/overview**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HEALTHY_OVERVIEW)
        ),
    )

    def _record_sync(route):
        state["sync"].append(route.request.url)
        # settings/sync expects a results envelope; the others ignore the body.
        is_settings = "/settings/sync" in route.request.url
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"results": []}) if is_settings else "{}",
        )

    for pattern in _SYNC_ROUTES:
        page.route(pattern, _record_sync)
    return state


def _open_portal(page, mm_web_url: str) -> None:
    page.goto(mm_web_url)
    page.locator("#tabbtn-context-gateway").click()
    # ADR-0026 D-F flip: Simple is the default and hides the section nav on the
    # Overview — switch to Advanced so the Projects nav button is clickable.
    page.evaluate("() => _ctxSetSimpleMode(false)")
    page.locator(".settings-nav-btn[data-section='ctx-projects']").click()
    page.wait_for_selector(".ctx-portal-row", timeout=3_000)


def _click_sync_and_confirm(page, scope_id: str) -> None:
    page.locator(f'.ctx-portal-sync[data-scope-id="{scope_id}"]').click()
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=2_000)
    page.locator("#confirm-ok-btn").click()


def test_portal_sync_fires_scoped_posts_for_enrolled_scope(page, mm_web_url: str) -> None:
    """Sync on an enrolled scope fans out scope_id to all five routes and then
    refreshes the portal roster (M4 — not the Overview the matrix used)."""
    state = _stub_portal_sync(page)
    _open_portal(page, mm_web_url)
    projects_before = state["projects"]

    _click_sync_and_confirm(page, "scope-on")

    deadline = time.monotonic() + 4.0
    while len(state["sync"]) < 5 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(state["sync"]) == 5, f"expected 5 scoped sync POSTs, got {state['sync']}"
    for url in state["sync"]:
        assert "scope_id=scope-on" in url, url

    # M4: the visible Projects board re-fetches after the sync settles.
    while state["projects"] <= projects_before and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert state["projects"] > projects_before, "portal did not refresh after sync"


def test_portal_sync_isolates_a_blocked_artifact_phase(page, mm_web_url: str) -> None:
    """#1396: a per-phase privacy-block 422 on the card fan-out must NOT abort
    the run — the other artifact types still sync. Mirrors the Overview Sync All
    isolation fix on the toast-only portal-card path.

    skills → 422 (privacy block); commands/agents/mcp-servers still POST.
    Settings stays gated behind a clean artifact run (no POST after a failure).
    """
    state = _stub_portal_sync(page)

    # Re-route skills to a privacy-block 422 AFTER the default recording stub
    # (last-route-wins); still record the URL so the ordering assertion holds.
    def _skills_block(route):
        state["sync"].append(route.request.url)
        route.fulfill(
            status=422,
            content_type="application/json",
            body=json.dumps({"detail": "A value looks like a secret; remove it and retry."}),
        )

    page.route("**/api/context/skills/sync**", _skills_block)

    _open_portal(page, mm_web_url)
    _click_sync_and_confirm(page, "scope-on")

    # 4 artifact POSTs fire (skills failed, but commands/agents/mcp-servers still
    # ran); settings is gated behind a clean artifact run, so it does NOT fire.
    deadline = time.monotonic() + 4.0
    while len(state["sync"]) < 4 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    synced = [u.split("/api/")[-1].split("?")[0] for u in state["sync"]]
    assert "context/commands/sync" in synced, synced
    assert "context/agents/sync" in synced, synced
    assert "context/mcp-servers/sync" in synced, synced
    assert "context/settings/sync" not in synced, (
        f"settings must stay skipped after an artifact failure, got {synced}"
    )

    # Partial-failure toast (real partial progress, not a hard abort): names a
    # landed phase + the blocked one, rather than the bare "Sync failed".
    page.wait_for_selector("#toast-container .toast.toast-error", timeout=4_000)
    toast_text = (
        page.locator("#toast-container .toast.toast-error .toast-msg").text_content() or ""
    ).strip()
    assert "Commands" in toast_text and "Skills" in toast_text, (
        f"expected a partial-failure toast naming landed + blocked phases, got {toast_text!r}"
    )


def test_portal_sync_server_cwd_empty_scope_id(page, mm_web_url: str) -> None:
    """Server CWD's effective scope_id collapses to '' — its Sync fans out with
    no scope_id param (blocking parity with the removed matrix path)."""
    state = _stub_portal_sync(page)
    _open_portal(page, mm_web_url)

    cwd_sync = page.locator('.ctx-portal-sync[data-scope-id=""]')
    assert cwd_sync.is_enabled(), "Server CWD Sync must be enabled (sync_eligible)"
    _click_sync_and_confirm(page, "")

    deadline = time.monotonic() + 4.0
    while len(state["sync"]) < 5 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert len(state["sync"]) == 5, f"expected 5 sync POSTs, got {state['sync']}"
    for url in state["sync"]:
        assert "scope_id=" not in url or url.endswith("scope_id=") or "scope_id=&" in url, url


def test_portal_sync_does_not_change_active_scope(page, mm_web_url: str) -> None:
    """Per-project Sync syncs the row only — it must NOT mutate the global
    active-project selection (only Use does). User-confirmed state invariant."""
    _stub_portal_sync(page)
    _open_portal(page, mm_web_url)

    # Make scope-on the active project via its Use button.
    page.locator('.ctx-portal-use[data-scope-id="scope-on"]').click()
    page.wait_for_function("() => _ctxActiveScopeId === 'scope-on'", timeout=3_000)

    # Sync a DIFFERENT row (Server CWD) — selection must stay scope-on.
    _click_sync_and_confirm(page, "")
    page.locator("#confirm-modal").wait_for(state="hidden", timeout=3_000)
    page.wait_for_timeout(150)  # let the async fan-out + M4 portal refresh settle

    assert page.evaluate("() => _ctxActiveScopeId") == "scope-on", (
        "per-project Sync must not change the active project"
    )


def test_portal_sync_write_blocked_in_user_tier(page, mm_web_url: str) -> None:
    """The moved Sync rides the tier write-block sweep: project_shared → no
    block; user → data-write-blocked='user'. Guards the M1 selector + the
    portal's _ctxRefreshWriteBlockedState() call after row render."""
    _stub_portal_sync(page)
    _open_portal(page, mm_web_url)

    sync_btn = page.locator('.ctx-portal-sync[data-scope-id="scope-on"]')
    assert sync_btn.get_attribute("data-write-blocked") is None

    # Flip the gateway tier to user (the tier control lives on other sections;
    # the portal inherits _ctxTargetScope and re-applies the sweep on render).
    page.evaluate("() => { _ctxTargetScope = 'user'; _ctxRefreshWriteBlockedState(); }")
    assert sync_btn.get_attribute("data-write-blocked") == "user"


def test_portal_sync_gated_on_eligibility(page, mm_web_url: str) -> None:
    """Sync is disabled with a reason tooltip for paused (enrolled-but-disabled)
    and scan-only (never-enrolled) scopes; the reason rides data-i18n-title."""
    _stub_portal_sync(page)
    _open_portal(page, mm_web_url)

    eligible = page.locator('.ctx-portal-sync[data-scope-id="scope-on"]')
    assert eligible.is_enabled()

    paused = page.locator('.ctx-portal-sync[data-scope-id="scope-paused"]')
    assert paused.is_disabled()
    assert paused.get_attribute("data-i18n-title") == "settings.ctx.matrix_sync_paused_title"

    scan = page.locator('.ctx-portal-sync[data-scope-id="scope-scan"]')
    assert scan.is_disabled()
    assert scan.get_attribute("data-i18n-title") == "settings.ctx.matrix_sync_not_enrolled_title"


def test_portal_sync_missing_root_uses_disabled_title_over_eligibility(
    page, mm_web_url: str
) -> None:
    """An enrolled+enabled (sync_eligible) scope whose root is gone shows the
    project_local/missing disabled tooltip, not the paused/not-enrolled arm —
    guards the branch ordering ported from the matrix."""
    _stub_portal_sync(page)
    _open_portal(page, mm_web_url)

    missing = page.locator('.ctx-portal-sync[data-scope-id="scope-missing"]')
    assert missing.is_disabled()
    assert missing.get_attribute("data-i18n-title") == "settings.ctx.matrix_sync_disabled_title"


def test_portal_sync_project_local_tier_wins_over_eligibility(page, mm_web_url: str) -> None:
    """In project_local tier (no runtime fan-out) the disabled-tooltip arm wins
    over the eligibility arm: an enrolled-but-paused scope shows the
    project_local/missing reason (matrix_sync_disabled_title), NOT the paused
    one. Guards the branch ordering ported from the matrix. The reason rides
    data-i18n-title, which the write-block sweep preserves."""
    _stub_portal_sync(page)
    _open_portal(page, mm_web_url)

    # Flip the gateway tier to project_local and re-render the rows.
    page.evaluate("() => { _ctxTargetScope = 'project_local'; _ctxPortalRenderRows(); }")
    paused = page.locator('.ctx-portal-sync[data-scope-id="scope-paused"]')
    assert paused.is_disabled()
    assert paused.get_attribute("data-i18n-title") == "settings.ctx.matrix_sync_disabled_title"


def test_portal_sync_ineligible_tooltip_survives_write_block_sweep(page, mm_web_url: str) -> None:
    """The eligibility reason (on data-i18n-title) must survive a tier flip to
    user and back to project_shared — the sweep restores title from
    data-i18n-title rather than dropping it."""
    _stub_portal_sync(page)
    _open_portal(page, mm_web_url)

    paused = page.locator('.ctx-portal-sync[data-scope-id="scope-paused"]')
    expected = page.evaluate("() => t('settings.ctx.matrix_sync_paused_title')")
    assert paused.get_attribute("title") == expected

    page.evaluate("() => { _ctxTargetScope = 'user'; _ctxRefreshWriteBlockedState(); }")
    page.evaluate("() => { _ctxTargetScope = 'project_shared'; _ctxRefreshWriteBlockedState(); }")
    assert paused.get_attribute("title") == expected


def test_portal_legend_decodes_dots_and_inventory(page, mm_web_url: str) -> None:
    """rank 13: the portal heading carries a legend decoding the traffic-light
    dot colors (install state) and the inventory emoji (artifact type), reusing
    the app's .graph-legend language."""
    _stub_portal_sync(page)
    _open_portal(page, mm_web_url)

    legend = page.locator(".ctx-portal-legend-row .graph-legend")
    assert legend.count() == 1
    # 3 install-state swatches + 4 inventory-type items.
    assert legend.locator(".graph-legend-item").count() == 7
    # The colored swatches reuse the EXACT row-dot classes (color match).
    assert legend.locator(".ctx-portal-row-light--registered").count() == 1


def test_sync_all_gated_when_active_scope_ineligible(page, mm_web_url: str) -> None:
    """Picking a paused (ineligible) project as the active scope disables the
    Overview Sync-All button with the paused reason. Rehomed from the matrix —
    the active scope is now chosen via the portal Use button; the gating logic
    lives unchanged on the Overview."""
    state = _stub_portal_sync(page)
    _open_portal(page, mm_web_url)

    # Make the paused (ineligible) project the active scope via Use.
    page.locator('.ctx-portal-use[data-scope-id="scope-paused"]').click()
    page.wait_for_function("() => _ctxActiveScopeId === 'scope-paused'", timeout=3_000)

    # Switch to the Overview; its Sync-All button must carry the paused reason.
    page.locator(".settings-nav-btn[data-section='ctx-overview']").click()
    sync_all = page.locator("#ctx-sync-all-btn")
    deadline = time.monotonic() + 4.0
    val = None
    while time.monotonic() < deadline:
        val = sync_all.get_attribute("data-sync-ineligible")
        if val:
            break
        page.wait_for_timeout(50)
    assert val == "settings.ctx.sync_all_paused_tooltip", f"got {val!r}"

    # Click-bail is the real guard: clicking the gated button must NOT open a
    # confirm modal and must NOT fire any sync POSTs (an ineligible active
    # project can never fan out).
    state["sync"].clear()
    # dispatch_event (not click) — the button is aria-disabled, so a real click
    # would just wait for actionability; we want to fire the handler and prove
    # it bails.
    sync_all.dispatch_event("click")
    page.wait_for_timeout(300)
    assert page.locator("#confirm-modal:not([hidden])").count() == 0, (
        "gated Sync-All must not open a confirm modal"
    )
    assert len(state["sync"]) == 0, f"gated Sync-All must not fire sync POSTs, got {state['sync']}"
