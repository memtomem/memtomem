"""Browser tests for the Context Gateway overview dashboard (Q-PR1, #825).

Pin three pieces of audit-driven behavior that file-scan regression guards
in ``test_i18n.py`` cannot exercise:

* **Bug-1, single-toggle re-render**: lang toggle must re-render the cards
  when the section is mounted. ``I18N.applyDOM`` does not handle inline
  ``t()`` text in innerHTML, so the ``langchange`` listener calls
  ``_renderCtxOverview`` directly.
* **#825, no spinner flash on lang toggle**: a healthy mounted dashboard
  caches its last ``/api/context/overview`` payload in
  ``_ctxOverviewCache``; subsequent lang toggles re-render from the
  cache — no refetch, no ``panelLoading`` spinner flash. Drops the
  round-trip the langchange listener used to issue (also addresses #824
  review P2 about langchange firing one fetch per toggle).
* **Bug-2, zero-state empty badge**: a ``total === 0`` tile must render
  the ``Empty`` badge (``settings.ctx.badge_empty``) with ``badge-gray``,
  not the green ``0/0 synced`` fallthrough.

The harness ``page.route()``-stubs ``/api/context/overview``; the lifespan
is off (see ``conftest.py`` docstring) so no real backend writes happen.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


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
    # Q-PR3 Visual-1: settings carries count fields too (parallel to the
    # other tiles). The empty-state branch fires when total==0 regardless
    # of tile key, so settings reaching the same Empty/badge-gray render
    # as skills/commands/agents is intentional.
    "settings": {
        "total": 0,
        "in_sync": 0,
        "out_of_sync": 0,
        "missing_target": 0,
        "error": 0,
        "status": "in_sync",
    },
}

# A non-empty payload with nothing wrong — used by the lang-toggle specs so
# the cards have real text to compare across locales (the empty-state pin
# covers the all-zero shape separately).
_HEALTHY_OVERVIEW = {
    "skills": {"total": 3, "in_sync": 3},
    "commands": {"total": 1, "in_sync": 1},
    "agents": {"total": 2, "in_sync": 2},
    "settings": {
        "total": 2,
        "in_sync": 2,
        "out_of_sync": 0,
        "missing_target": 0,
        "error": 0,
        "status": "in_sync",
    },
}


def test_zero_total_renders_empty_badge_not_green_synced(page, mm_web_url: str) -> None:
    """Bug-2 pin: ``total === 0`` on a count tile must render the ``Empty``
    badge (``settings.ctx.badge_empty``) with ``badge-gray``, never the
    green ``0/0 synced`` fallthrough.

    Symmetric pair (``feedback_pin_invert_symmetric_assertion.md``):
    positive on the badge text + class, negative on the legacy literal.
    Tile-locator is the skills tile specifically; the Q-PR3 follow-up
    spec ``test_q_pr3_settings_zero_total_renders_empty`` covers the
    settings tile, which also participates in the empty branch after
    Visual-1 (the ``typ.key !== 'settings'`` gate was lifted once the
    backend started sending real ``total`` for settings)."""
    install_default_stubs(page)
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
    new locale. ``I18N.applyDOM`` only walks ``data-i18n*`` attributes;
    the cards are inline-templated, so without the explicit
    ``_renderCtxOverview`` call in the langchange listener the EN→KO
    toggle leaves cards in EN.

    Cross-pinned with #825: the cache-driven re-render path means no
    refetch on toggle — the assertion captures the call count before
    and after to catch a regression that re-introduced the
    fetch-on-langchange behavior."""
    install_default_stubs(page)

    overview_calls: list[str] = []

    def _overview_handler(route):
        overview_calls.append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HEALTHY_OVERVIEW)
        )

    page.route("**/api/context/overview", _overview_handler)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    skills_tile = page.locator(
        "#ctx-overview-content .ctx-overview-stat[data-section='ctx-skills']"
    )
    label = skills_tile.locator(".ctx-overview-label")
    pre = (label.text_content() or "").strip()
    assert pre == "Skills", f"default locale (EN) tile label should be 'Skills', got {pre!r}"
    initial_calls = len(overview_calls)
    assert initial_calls >= 1, (
        f"dashboard mount should fire at least one overview fetch; got {overview_calls!r}"
    )

    # Toggle EN→KO. ``I18N.setLang`` awaits the locale fetch, applyDOM, and
    # then dispatches ``langchange`` (i18n.js L65-76); the ``langchange``
    # listener in context-gateway.js re-renders synchronously from
    # ``_ctxOverviewCache`` — no fetch, no spinner.
    page.evaluate("async () => { await I18N.setLang('ko'); }")
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
    # #825: cache-driven re-render means no extra fetch on toggle.
    assert len(overview_calls) == initial_calls, (
        f"lang toggle must re-render from _ctxOverviewCache, no refetch; "
        f"baseline={initial_calls}, after={len(overview_calls)}, "
        f"calls={overview_calls!r} (#825)"
    )


def test_langchange_uses_cache_no_spinner_flash(page, mm_web_url: str) -> None:
    """#825 pin: lang toggle on a healthy mounted dashboard must re-render
    from ``_ctxOverviewCache`` directly — no refetch and, crucially, no
    ``panelLoading`` spinner flash between the wipe and the response
    render. The cards should feel like an in-place text swap, not a
    panel reload.

    Two regressions this catches:

    * Approach-1 partial fix (``loadCtxOverview({ silent: true })``) that
      keeps the fetch but skips ``panelLoading`` — the ``overview_calls``
      assertion catches the unnecessary round-trip.
    * Reverting to the pre-#825 ``loadCtxOverview()`` call from the
      langchange listener — both the spinner-flash assertion and the
      ``overview_calls`` assertion catch this.

    Three sequential toggles (EN→KO→EN→KO) stress the cache-hit path
    repeatedly so a one-shot cache-fill that then falls back to fetch
    would still surface as ``overview_calls > initial_calls``."""
    install_default_stubs(page)

    overview_calls: list[str] = []

    def _overview_handler(route):
        overview_calls.append(route.request.url)
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HEALTHY_OVERVIEW)
        )

    page.route("**/api/context/overview", _overview_handler)
    page.goto(mm_web_url)
    _open_context_gateway(page)
    initial_calls = len(overview_calls)
    assert initial_calls >= 1, (
        f"dashboard mount should fire at least one overview fetch; got {overview_calls!r}"
    )

    # Spinner-flash detection. ``panelLoading`` injects
    # ``<div class="loading-panel">…</div>`` into ``#ctx-overview-content``;
    # the cache-driven re-render rewrites innerHTML directly with the
    # rendered grid (no intermediate spinner state). A MutationObserver
    # that latches on any ``.loading-panel`` appearance during the
    # toggle window catches both the ``panelLoading`` flash *and* a
    # partial fix that kept the spinner but dropped the fetch.
    page.evaluate(
        """() => {
            window._spinnerSeen = false;
            const el = document.getElementById('ctx-overview-content');
            const obs = new MutationObserver(() => {
                if (el.querySelector('.loading-panel')) {
                    window._spinnerSeen = true;
                }
            });
            obs.observe(el, { childList: true, subtree: true });
            window._spinnerObserver = obs;
        }"""
    )

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.wait_for_function(
        "() => {"
        "  const el = document.querySelector("
        '    \'#ctx-overview-content .ctx-overview-stat[data-section="ctx-skills"] '
        ".ctx-overview-label');"
        "  return el && el.textContent.trim() === '스킬';"
        "}",
        timeout=2_000,
    )
    page.evaluate("async () => { await I18N.setLang('en'); }")
    page.wait_for_function(
        "() => {"
        "  const el = document.querySelector("
        '    \'#ctx-overview-content .ctx-overview-stat[data-section="ctx-skills"] '
        ".ctx-overview-label');"
        "  return el && el.textContent.trim() === 'Skills';"
        "}",
        timeout=2_000,
    )
    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.wait_for_function(
        "() => {"
        "  const el = document.querySelector("
        '    \'#ctx-overview-content .ctx-overview-stat[data-section="ctx-skills"] '
        ".ctx-overview-label');"
        "  return el && el.textContent.trim() === '스킬';"
        "}",
        timeout=2_000,
    )

    assert len(overview_calls) == initial_calls, (
        f"three lang toggles must re-render from _ctxOverviewCache without "
        f"refetching; baseline={initial_calls}, after={len(overview_calls)}, "
        f"calls={overview_calls!r} (#825)"
    )
    spinner_seen = page.evaluate("() => window._spinnerSeen")
    assert spinner_seen is False, (
        "lang toggle on a cached dashboard must not flash panelLoading; the "
        "cache-driven _renderCtxOverview path rewrites innerHTML directly "
        "without an intermediate spinner state (#825)"
    )


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
    install_default_stubs(page)

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
    install_default_stubs(page)

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


def test_q_pr3_settings_tile_renders_count_not_glyph(page, mm_web_url: str) -> None:
    """Q-PR3 Visual-1: the settings tile's big-number slot must render
    the same ``${total}`` text as the other tiles, never the legacy
    ✔ / ⚠ glyph. Static-source ``test_q_pr3_settings_tile_count_not_glyph``
    in ``test_i18n.py`` catches a source revert; this spec catches a
    rendering regression that compiles/parses but emits the wrong DOM
    (e.g., a CSS or template change that revives the per-tile branch)."""
    install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HEALTHY_OVERVIEW)
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    # The settings tile in the overview grid lives at data-section="hooks-sync"
    # because the dashboard reuses the existing hooks panel as the
    # destination — see the ``types`` array in ``loadCtxOverview``
    # (context-gateway.js).
    settings_tile = page.locator(
        "#ctx-overview-content .ctx-overview-stat[data-section='hooks-sync']"
    )
    count = settings_tile.locator(".ctx-overview-count")
    text = (count.text_content() or "").strip()
    assert text == "2", (
        f"settings tile must render the count {{total}} (here: 2 from "
        f"_HEALTHY_OVERVIEW), not a glyph; got: {text!r}"
    )
    # Symmetric negative: the legacy glyphs must not survive the render.
    assert text not in {"✔", "⚠"}, (
        f"settings tile rendered legacy glyph instead of count; got: {text!r}"
    )


def test_q_pr3_settings_zero_total_renders_empty(page, mm_web_url: str) -> None:
    """Q-PR3 Visual-1 isEmpty extension: with the new ``total`` field on
    the settings response, a settings tile with ``total === 0`` must
    render the gray ``Empty`` badge (same as skills/commands/agents),
    not the green ``in sync`` status badge that pre-Q-PR3 fired by
    default when no runtime had a canonical source.

    Pre-Q-PR3 the dashboard skipped the empty-state branch for settings
    via ``typ.key !== 'settings'`` and fell through to the
    ``_SETTINGS_STATUS_I18N`` lookup; the backend then collapsed
    ``all skipped`` to ``status: "in_sync"``, producing a green badge
    on a project with no installed runtime (false-OK, mirror of the
    Bug-2 pattern from Q-PR1). This pin catches a regression in either
    half: missing ``total`` on the response or a re-introduction of
    the ``typ.key !== 'settings'`` gate."""
    install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_ZERO_OVERVIEW)
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    settings_tile = page.locator(
        "#ctx-overview-content .ctx-overview-stat[data-section='hooks-sync']"
    )
    badge = settings_tile.locator(".ctx-overview-badge .badge")
    badge_text = (badge.text_content() or "").strip()
    assert badge_text == "Empty", (
        f"zero-total settings tile must show 'Empty' badge (badge_empty), got: {badge_text!r}"
    )

    badge_classes = (badge.get_attribute("class") or "").split()
    assert "badge-gray" in badge_classes, (
        f"zero-total settings badge must use badge-gray, got classes: {badge_classes!r}"
    )
    # Symmetric negative: green ``badge-success`` would be the false-OK
    # state — exactly the Bug-2 class regression that Q-PR1 caught for
    # the count tiles, now also pinned for settings.
    assert "badge-success" not in badge_classes, (
        f"zero-total settings must not show badge-success (false-OK "
        f"regression); got: {badge_classes!r}"
    )
    # Settings tile keeps the count form even at zero — verifies the
    # Visual-1 glyph→count change applied to the empty case too.
    count = settings_tile.locator(".ctx-overview-count")
    assert (count.text_content() or "").strip() == "0", (
        "settings tile big-number slot must show the count even at zero "
        "(Visual-1 glyph→count alignment)"
    )
