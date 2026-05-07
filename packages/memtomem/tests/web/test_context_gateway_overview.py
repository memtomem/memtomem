"""Browser tests for the Context Gateway overview dashboard (Q-PR1).

Pin three pieces of audit-driven behavior that file-scan regression guards
in ``test_i18n.py`` cannot exercise:

* **Bug-1, multi-toggle race**: rapid lang toggles must surface only the
  newest locale on the cards. Without the ``_ctxOverviewSeq`` guard in
  ``loadCtxOverview`` (context-gateway.js), a slow first fetch can land
  *after* a second toggle and clobber the newer locale's cards with stale
  text. The timing is forced by delaying the KO response in the route
  handler so the EN response wins the render race; the late KO arrival
  must then be dropped by the seq guard.
* **Bug-1, single-toggle re-render**: lang toggle must re-render the cards
  when the section is mounted. ``I18N.applyDOM`` does not handle inline
  ``t()`` text in innerHTML, so ``loadCtxOverview`` itself runs on
  ``langchange``.
* **Bug-2, zero-state empty badge**: a ``total === 0`` tile must render
  the ``Empty`` badge (``settings.ctx.badge_empty``) with ``badge-gray``,
  not the green ``0/0 synced`` fallthrough.

The harness ``page.route()``-stubs ``/api/context/overview``; the lifespan
is off (see ``conftest.py`` docstring) so no real backend writes happen.
"""

from __future__ import annotations

import json
import time

import pytest

pytestmark = pytest.mark.browser


def _install_default_stubs(page) -> None:
    """Mirrors ``test_redaction_blocked_retry._install_default_stubs``.

    Last-route-wins: the catch-all goes first; specific overrides go last
    in each spec body.
    """

    def _ok(route, payload):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/**", lambda r: _ok(r, {}))
    page.route("**/api/system/ui-mode", lambda r: _ok(r, {"mode": "prod"}))
    page.route("**/api/system/model-readiness", lambda r: _ok(r, {"ready": True}))
    page.route("**/api/sources", lambda r: _ok(r, {"sources": []}))
    page.route("**/api/namespaces", lambda r: _ok(r, {"namespaces": []}))
    page.route("**/api/stats", lambda r: _ok(r, {}))
    page.route("**/api/privacy/patterns", lambda r: _ok(r, {"patterns": []}))


def _open_context_gateway(page) -> None:
    """Navigate to ``settings-ctx-overview`` so ``loadCtxOverview`` runs.

    The dashboard is reached via the ⚙️ Settings main tab → Context
    Gateway sidebar item. The langchange listener now gates on both
    main-tab activity (``#tab-settings.active``) and sub-section
    activity (``#settings-ctx-overview.active``), so this helper has to
    activate both. ``activateTab`` + ``switchSettingsSection`` are
    invoked from ``page.evaluate`` rather than chasing click coordinates
    — the sidebar layout changed twice in #813 / #816 and a click-based
    path keeps re-breaking.

    Wait via ``wait_for_function`` rather than ``wait_for_selector``:
    the overview tiles render inside a section that may not yet be the
    active visible pane (other settings sections also exist in the DOM
    with ``ctx-overview-stat`` children-of-a-different-tile shape), so
    the visibility check is fragile. The DOM-attach + populated-text
    check is what the assertions actually depend on.
    """
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-overview')")
    page.wait_for_function(
        "() => {"
        "  const tiles = document.querySelectorAll("
        "    '#ctx-overview-content .ctx-overview-stat');"
        "  if (tiles.length === 0) return false;"
        "  return Array.from(tiles).every("
        "    el => (el.textContent || '').trim().length > 0);"
        "}",
        timeout=5_000,
    )


_ZERO_OVERVIEW = {
    "skills": {"total": 0, "in_sync": 0},
    "commands": {"total": 0, "in_sync": 0},
    "agents": {"total": 0, "in_sync": 0},
    "settings": {"status": "in_sync"},
}

# A non-empty payload with nothing wrong — used by the lang-toggle specs so
# the cards have real text to compare across locales (the empty-state pin
# covers the all-zero shape separately).
_HEALTHY_OVERVIEW = {
    "skills": {"total": 3, "in_sync": 3},
    "commands": {"total": 1, "in_sync": 1},
    "agents": {"total": 2, "in_sync": 2},
    "settings": {"status": "in_sync"},
}


def test_zero_total_renders_empty_badge_not_green_synced(page, mm_web_url: str) -> None:
    """Bug-2 pin: ``total === 0`` on a count tile must render the ``Empty``
    badge (``settings.ctx.badge_empty``) with ``badge-gray``, never the
    green ``0/0 synced`` fallthrough.

    Symmetric pair (``feedback_pin_invert_symmetric_assertion.md``):
    positive on the badge text + class, negative on the legacy literal.
    Settings tile is skipped — it has its own status-driven branch and is
    out of scope for the Bug-2 zero-state intercept (verified via
    ``typ.key !== 'settings'`` guard in production)."""
    _install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_ZERO_OVERVIEW)
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    skills_tile = page.locator(
        "#ctx-overview-content .ctx-overview-stat[data-section='ctx-skills']"
    )
    badge = skills_tile.locator(".ctx-overview-badge .badge")
    badge_text = (badge.text_content() or "").strip()
    assert badge_text == "Empty", f"zero-state tile must show 'Empty' badge, got: {badge_text!r}"

    badge_classes = (badge.get_attribute("class") or "").split()
    assert "badge-gray" in badge_classes, (
        f"zero-state badge must use badge-gray, got classes: {badge_classes!r}"
    )
    # Negative half: the green ``badge-success`` class would falsely
    # signal "in sync" for a fresh / un-imported project (audit Bug-2).
    assert "badge-success" not in badge_classes, (
        f"zero-state must not use badge-success (audit Bug-2 false-OK): {badge_classes!r}"
    )
    # Negative on text: the legacy ``0/0 synced`` literal must not render.
    assert "synced" not in badge_text.lower(), (
        f"zero-state must not render the legacy '0/0 synced' literal: {badge_text!r}"
    )


def test_langchange_rerenders_card_label_text(page, mm_web_url: str) -> None:
    """Bug-1 single-toggle pin: ``langchange`` must re-render
    ``ctx-overview-content`` so inline-templated ``t()`` text picks up the
    new locale. ``I18N.applyDOM`` only walks ``data-i18n*`` attributes; the
    cards are inline-templated, so without the explicit ``loadCtxOverview``
    call in the langchange listener, the EN→KO toggle leaves cards in EN.
    """
    _install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HEALTHY_OVERVIEW)
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    skills_tile = page.locator(
        "#ctx-overview-content .ctx-overview-stat[data-section='ctx-skills']"
    )
    label = skills_tile.locator(".ctx-overview-label")
    pre = (label.text_content() or "").strip()
    assert pre == "Skills", f"default locale (EN) tile label should be 'Skills', got {pre!r}"

    # Toggle EN→KO. ``I18N.setLang`` awaits the locale fetch, applyDOM, and
    # then dispatches ``langchange`` (i18n.js L65-76); the ``langchange``
    # listener in context-gateway.js calls ``loadCtxOverview`` which
    # re-fires ``/api/context/overview``. Wait for the request to land
    # so the assertion below doesn't race the re-render.
    with page.expect_request("**/api/context/overview", timeout=2_000):
        page.evaluate("() => I18N.setLang('ko')")
    # ``loadCtxOverview`` calls ``panelLoading(el)`` before the new fetch
    # resolves; that wipes the inner tiles and replaces them with a
    # spinner. Use a null-safe wait so the polling doesn't trip on the
    # transient empty state between the wipe and the re-render.
    page.wait_for_function(
        "() => {"
        "  const el = document.querySelector("
        '    \'#ctx-overview-content .ctx-overview-stat[data-section="ctx-skills"] '
        ".ctx-overview-label');"
        "  return el && el.textContent.trim() === '스킬';"
        "}",
        timeout=3_000,
    )
    # Re-resolve the label locator: the original ``label`` reference may
    # point at a detached element after the innerHTML rewrite.
    post = (
        page.locator(
            "#ctx-overview-content .ctx-overview-stat[data-section='ctx-skills'] "
            ".ctx-overview-label"
        ).text_content()
        or ""
    ).strip()
    assert post == "스킬", f"after setLang('ko') tile label should be '스킬', got {post!r}"
    # Negative: the EN literal must be gone (otherwise applyDOM-only
    # behavior would also pass the positive assertion above on a partial
    # rerender that updated some attributes but not the inline text).
    assert "Skills" not in post, f"EN literal must not linger after KO toggle: {post!r}"


def test_multi_toggle_race_keeps_newest_locale_on_cards(page, mm_web_url: str) -> None:
    """Bug-1 multi-toggle race pin: when a slow first fetch lands *after*
    a second lang toggle, the newer locale's cards must remain — the
    older response's stale render must be dropped by the
    ``_ctxOverviewSeq`` guard in ``loadCtxOverview``.

    Drive the race deterministically: the first ``/api/context/overview``
    response is held until after a second ``setLang`` has fired its own
    request and rendered. Then we release the first response. Without
    the seq guard the first (KO) response would clobber the second (EN)
    render and the assertion would catch it.

    Mutation check: temporarily removing the
    ``if (seq !== _ctxOverviewSeq) return`` line in ``loadCtxOverview``
    fails this spec because the held KO response wins on arrival order.
    """
    _install_default_stubs(page)

    # Three calls hit ``/api/context/overview`` during this spec:
    #   1) initial EN load by ``_open_context_gateway``     (immediate)
    #   2) KO toggle dispatched (delayed in stub so it races EN)
    #   3) EN toggle dispatched after, returns immediately
    # The KO response is delayed inside the route handler — Playwright
    # runs each handler on a worker thread, so a sync ``time.sleep`` here
    # parks call 2's response without stalling the page event loop or
    # blocking call 3 from being served on a separate worker thread.
    # Awaiting each ``setLang`` promise from Python guarantees call 2 is
    # the KO request and call 3 is the EN one (without the await, the
    # second toggle's ``langchange`` could be dispatched before the
    # first's, swapping which request is delayed).
    call_seq = {"calls": 0}
    KO_DELAY = 0.6  # seconds; long enough that EN fully renders before KO arrives

    def _overview_handler(route):
        call_seq["calls"] += 1
        if call_seq["calls"] == 2:
            time.sleep(KO_DELAY)
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HEALTHY_OVERVIEW)
        )

    page.route("**/api/context/overview", _overview_handler)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    # First toggle EN→KO. Awaiting the setLang promise guarantees the KO
    # ``langchange`` (and its ``loadCtxOverview`` fetch) has been dispatched
    # before the EN toggle starts. ``loadCtxOverview`` itself is
    # fire-and-forget from inside the listener, so the await returns
    # while the KO fetch is still in flight — which is exactly the race
    # window we want.
    page.evaluate("async () => { await I18N.setLang('ko'); }")
    # Belt-and-braces: don't proceed to EN until the KO request is
    # actually parked in the route handler. Use ``page.wait_for_timeout``
    # to poll — a pure-Python ``time.sleep`` would hold the sync_playwright
    # main thread and starve Playwright's own callback dispatch (the
    # very route handler whose ``calls`` we're polling).
    deadline = time.monotonic() + 2.0
    while call_seq["calls"] < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(20)
    assert call_seq["calls"] >= 2, "KO toggle's overview fetch did not dispatch"

    # Second toggle KO→EN. Wait for the EN response to render. The EN
    # request is call 3 and fulfills immediately (no delay branch), so
    # the cards land on Skills well within timeout while the KO response
    # is still parked in its delay sleep.
    page.evaluate("async () => { await I18N.setLang('en'); }")
    page.wait_for_function(
        "() => {"
        "  const el = document.querySelector("
        '    \'#ctx-overview-content .ctx-overview-stat[data-section="ctx-skills"] '
        ".ctx-overview-label');"
        "  return el && el.textContent.trim() === 'Skills';"
        "}",
        timeout=3_000,
    )

    # Wait long enough for the delayed KO response to land (or be dropped
    # by the seq guard). KO_DELAY is the upper bound on its arrival; pad
    # a bit so the assertion is sampled after the late response has
    # had its chance to clobber the EN render.
    page.wait_for_timeout(int(KO_DELAY * 1000) + 300)

    label = page.locator(
        "#ctx-overview-content .ctx-overview-stat[data-section='ctx-skills'] .ctx-overview-label"
    )
    final = (label.text_content() or "").strip()
    assert final == "Skills", (
        f"newest locale (EN) must remain after late KO arrival; got {final!r}. "
        "Without _ctxOverviewSeq the KO render would overwrite EN."
    )
    # Negative: the KO literal must not be present anywhere on the
    # tile, even partially — a half-clobber is still a regression.
    assert "스킬" not in final, f"old locale must not bleed through after race: {final!r}"


def test_langchange_after_tab_switch_does_not_refetch_overview(page, mm_web_url: str) -> None:
    """PR #824 second-pass review pin: ``activateTab`` hides the Settings
    panel but does not remove ``.active`` from
    ``#settings-ctx-overview``, so a section-only gate would still let
    off-Settings language toggles re-issue ``/api/context/overview``.
    The fix gates on both the Settings main tab AND the Context Gateway
    sub-section being active; this spec mounts the dashboard, switches
    to the Search main tab, then toggles language and asserts no
    overview refetch.

    Mutation check: dropping the ``#tab-settings`` gate (section gate
    alone) makes this spec fail with one extra ``/api/context/overview``
    call per ``setLang`` invocation."""
    _install_default_stubs(page)

    overview_calls: list[str] = []

    def _overview_handler(route):
        overview_calls.append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HEALTHY_OVERVIEW)
        )

    page.route("**/api/context/overview", _overview_handler)
    page.goto(mm_web_url)
    # Mount the Context Gateway dashboard — adds .active to
    # #settings-ctx-overview and fires at least one
    # /api/context/overview load. The exact mount-time call count
    # varies between local + CI (boot-path differences around
    # ``activateTab('settings')`` → ``switchSettingsSection`` cascade);
    # this spec's invariant is "no *additional* refetch after the
    # main-tab switch", so capture the post-mount baseline rather than
    # pinning a specific number.
    _open_context_gateway(page)
    initial_calls = len(overview_calls)
    assert initial_calls >= 1, (
        f"dashboard mount should fire at least one overview fetch; got {overview_calls!r}"
    )

    # Switch to a different main tab. ``activateTab`` flips
    # ``#tab-settings.active`` off + ``hidden`` on, but the section
    # ``.active`` class on ``#settings-ctx-overview`` stays.
    page.locator("#tabbtn-search").click()
    page.wait_for_selector("#tabbtn-search.active", timeout=2_000)
    section_state = page.evaluate(
        """() => {
          const tab = document.getElementById('tab-settings');
          const sec = document.getElementById('settings-ctx-overview');
          return {
            settingsTabActive: !!(tab && tab.classList.contains('active')),
            sectionActive: !!(sec && sec.classList.contains('active')),
          };
        }"""
    )
    # Sanity-pin the precondition the bug depends on: the section keeps
    # its .active class even after the main tab switch. If this ever
    # changes (someone teaches activateTab to clean up sub-section
    # classes) the bug evaporates and this spec becomes redundant —
    # the explicit precondition makes that situation surface.
    assert section_state == {"settingsTabActive": False, "sectionActive": True}, (
        f"precondition broken — expected #tab-settings inactive but section "
        f"still .active; got {section_state!r}"
    )

    # Now toggle language. With both gates in place, no overview refetch.
    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.evaluate("async () => { await I18N.setLang('en'); }")
    page.wait_for_timeout(300)
    assert len(overview_calls) == initial_calls, (
        f"language toggle from a non-Settings main tab must not refetch the "
        f"overview; baseline={initial_calls}, after-toggles={len(overview_calls)}, "
        f"calls={overview_calls!r} (PR #824 second-pass review)"
    )


def test_langchange_off_overview_does_not_refetch_overview(page, mm_web_url: str) -> None:
    """PR #824 review P2 pin: language toggles from a non-overview page
    must not fire ``/api/context/overview``. The dashboard's
    ``#settings-ctx-overview`` element is always present in the DOM, so
    the listener must gate on ``classList.contains('active')`` rather
    than on element existence; without the gate, every toggle from
    Search/Index/etc. issues an unnecessary backend round-trip."""
    _install_default_stubs(page)

    overview_calls: list[str] = []

    def _overview_handler(route):
        overview_calls.append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HEALTHY_OVERVIEW)
        )

    page.route("**/api/context/overview", _overview_handler)
    page.goto(mm_web_url)
    # Land on the Search tab — the SPA's default landing page already
    # exercises the non-overview path, but click explicitly so a future
    # default change doesn't silently make this spec passive.
    page.locator("#tabbtn-search").click()
    page.wait_for_selector("#tabbtn-search.active", timeout=2_000)
    # No overview fetch should have happened yet — boot didn't visit
    # the dashboard. Confirm the baseline before toggling so the post-
    # toggle assertion has a clean reference.
    assert overview_calls == [], (
        f"overview must not be fetched during boot on Search tab; got {overview_calls!r}"
    )

    # Toggle KO and back to EN. Each setLang dispatches ``langchange``;
    # a missing active-section gate would fire two ``/api/context/overview``
    # requests here. Await each promise so the assertion samples after
    # both listeners have run.
    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.evaluate("async () => { await I18N.setLang('en'); }")
    # Give the page a beat for any racing fetch to dispatch.
    page.wait_for_timeout(300)

    assert overview_calls == [], (
        f"language toggle from a non-overview page must not fetch the overview; "
        f"saw {overview_calls!r} (PR #824 review P2)"
    )
