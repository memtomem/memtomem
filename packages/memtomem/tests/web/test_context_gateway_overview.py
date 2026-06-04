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


def test_overview_renders_no_projects_matrix(page, mm_web_url: str) -> None:
    """rank 2: the Overview is a tiles-only aggregate dashboard. The per-project
    roster ``projects matrix`` was removed — the roster lives solely on the
    Projects portal now — so no matrix table or matrix row controls render here."""
    install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_HEALTHY_OVERVIEW)
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    # The aggregate stat tiles still render...
    assert page.locator("#ctx-overview-content .ctx-overview-stat").count() > 0
    # ...but the matrix table + its per-row controls are gone everywhere.
    assert page.locator(".ctx-projects-matrix-table").count() == 0
    assert page.locator(".ctx-matrix-sync-btn").count() == 0
    assert page.locator(".ctx-matrix-add-project-btn").count() == 0
    assert page.locator(".ctx-matrix-remove-btn").count() == 0


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


# ---------------------------------------------------------------------------
# Header (issues #830 / #831): project_root path + detected_runtimes chip strip.
# ---------------------------------------------------------------------------

_OVERVIEW_WITH_HEADER = {
    **_HEALTHY_OVERVIEW,
    "project_root": "/tmp/example-project",
    "detected_runtimes": [
        {"name": "claude", "available": True},
        {"name": "gemini", "available": False},
        {"name": "codex", "available": False},
    ],
}


def test_overview_header_renders_project_root_and_runtime_chips(page, mm_web_url: str) -> None:
    """#830/#831 pin: the overview panel header surfaces the registered
    project root path and a per-runtime chip strip, with detected runtimes
    rendered as ``badge-success`` and undetected as ``badge-gray``.

    Undetected chips also carry the
    ``settings.ctx.runtime_undetected_tooltip`` ``title`` attribute so the
    "why is this greyed out" question is one hover away on desktop and
    discoverable via screen reader on assistive tech.
    """
    install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_OVERVIEW_WITH_HEADER),
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    root_text = (
        page.locator("#ctx-overview-content .ctx-overview-root-path").text_content() or ""
    ).strip()
    assert root_text == "/tmp/example-project", (
        f"header must render the stubbed project_root; got {root_text!r}"
    )

    chips = page.locator("#ctx-overview-content .ctx-overview-runtimes [data-runtime]")
    assert chips.count() == 3, (
        f"chip strip must include all KNOWN_RUNTIMES (greyed when undetected); got {chips.count()}"
    )

    claude_chip = page.locator(
        "#ctx-overview-content .ctx-overview-runtimes [data-runtime='claude']"
    )
    claude_classes = (claude_chip.get_attribute("class") or "").split()
    assert "badge-success" in claude_classes, (
        f"detected runtime chip must be badge-success; got {claude_classes!r}"
    )

    gemini_chip = page.locator(
        "#ctx-overview-content .ctx-overview-runtimes [data-runtime='gemini']"
    )
    gemini_classes = (gemini_chip.get_attribute("class") or "").split()
    assert "badge-gray" in gemini_classes, (
        f"undetected runtime chip must be badge-gray; got {gemini_classes!r}"
    )
    # Tooltip pin: undetected chip exposes a ``title`` attribute so hover
    # reveals why it's greyed; the i18n key path is what makes this
    # translatable (EN/KO parity is enforced by ``test_i18n.py``).
    assert gemini_chip.get_attribute("title"), (
        "undetected chip must carry a title attribute (runtime_undetected_tooltip)"
    )


def test_overview_header_labels_translate_on_langchange(page, mm_web_url: str) -> None:
    """The header labels use ``data-i18n`` attrs so ``I18N.applyDOM`` (which
    only walks data-attributes) handles the EN→KO swap without needing the
    inline-text re-render path. Cross-pinned with #825: no refetch on toggle.

    The chip names themselves are proper nouns (claude/gemini/codex) and
    must NOT translate — pinning their text after the toggle guards against
    a future "translate runtime names too" footgun.
    """
    install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_OVERVIEW_WITH_HEADER),
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    root_label = page.locator("#ctx-overview-content .ctx-overview-root-label")
    pre = (root_label.text_content() or "").strip()
    assert pre == "Project", f"default (EN) header label should be 'Project'; got {pre!r}"

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.wait_for_function(
        "() => {"
        "  const el = document.querySelector("
        "    '#ctx-overview-content .ctx-overview-root-label');"
        "  return el && el.textContent.trim() === '프로젝트';"
        "}",
        timeout=3_000,
    )

    runtimes_label = (
        page.locator("#ctx-overview-content .ctx-overview-runtimes-label").text_content() or ""
    ).strip()
    assert runtimes_label == "런타임", (
        f"runtimes label must translate to KO; got {runtimes_label!r}"
    )

    # Chip names stay as raw runtime identifiers — proper nouns, not labels.
    claude_text = (
        page.locator(
            "#ctx-overview-content .ctx-overview-runtimes [data-runtime='claude']"
        ).text_content()
        or ""
    ).strip()
    assert claude_text == "claude", (
        f"runtime chip text must stay as the raw identifier on lang toggle; got {claude_text!r}"
    )


# ---------------------------------------------------------------------------
# Tier-aware header (#952): user-tier swap from "Project: <root>" to
# "User canonical: ~/.memtomem/" — project_root would mislead on user tier
# since user-scope canonicals are host-global, not cwd-scoped.
# ---------------------------------------------------------------------------

_OVERVIEW_USER_TIER = {
    **_HEALTHY_OVERVIEW,
    "target_scope": "user",
    "project_root": "/tmp/example-project",
    "detected_runtimes": [
        {"name": "claude", "available": True},
    ],
}


def test_overview_header_user_tier_shows_user_canonical_label(page, mm_web_url: str) -> None:
    """#952 pin: when ``target_scope=user`` the header must swap from
    ``Project: <root>`` to ``User canonical: ~/.memtomem/``. User-scope
    canonicals live under ``~/.memtomem/`` (host-global), so the
    ``Project:`` framing was misleading on this tier.

    Four pins:

    * positive — label text matches the new ``user_canonical_label`` key
    * positive — path text matches the new ``user_canonical_path`` key
    * tier attr — ``data-target-scope="user"`` on ``.ctx-overview-root``
      so CSS / selector-based tests can pin tier state without parsing
      visible text (mirrors PR #945's ``data-write-blocked`` shape)
    * negative — ``project_root`` from the payload must NOT render
      (defense against a future regression that drops the tier branch
      but keeps the payload field around).
    """
    install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_OVERVIEW_USER_TIER),
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    root_label = (
        page.locator("#ctx-overview-content .ctx-overview-root-label").text_content() or ""
    ).strip()
    assert root_label == "User canonical", (
        f"user-tier header label must read 'User canonical'; got {root_label!r}"
    )

    root_path = (
        page.locator("#ctx-overview-content .ctx-overview-root-path").text_content() or ""
    ).strip()
    assert root_path == "~/.memtomem/", (
        f"user-tier header path must read '~/.memtomem/'; got {root_path!r}"
    )

    root_block = page.locator("#ctx-overview-content .ctx-overview-root")
    assert root_block.get_attribute("data-target-scope") == "user", (
        "user-tier header must mark .ctx-overview-root with data-target-scope=user "
        "(mirrors data-write-blocked from PR #945)"
    )

    # Negative: project_root from the payload must not leak into the rendered
    # path. The field is still in the response (route doesn't strip it on
    # user tier), but the renderer must ignore it on this tier.
    full_header = page.locator("#ctx-overview-content .ctx-overview-header").text_content() or ""
    assert "/tmp/example-project" not in full_header, (
        f"user-tier header must not leak project_root path; got {full_header!r}"
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


# ---------------------------------------------------------------------------
# Issue #833 — ADR-0009 §2 sync-direction pointers.
# ---------------------------------------------------------------------------
#
# ADR-0009 §2 keeps the dashboard's mutation surface push-only but adds
# inline pointers per tile so the dashboard can tell the user *which* action
# resolves a partial-sync state. The pointer block is derived from existing
# ``/api/context/overview`` per-status counts — no new wire fields. Three
# pointer phrasings, fixed priority order:
#
#   1. ``missing_target > 0``  → "Run Sync All to push N missing entries."
#                                 (data-action=sync-all)
#   2. ``out_of_sync > 0``     → "Open <leaf> to resolve N differences."
#                                 (data-action=leaf, direction-neutral)
#   3. ``missing_canonical > 0`` → "N runtime entries are not in canonical
#                                 — open <leaf> to import."
#                                 (data-action=leaf; settings tile NEVER
#                                 emits this one, ADR-0009 §2 last paragraph)
#
# Pointer click handlers ``stopPropagation`` so the outer ``.ctx-overview-stat``
# navigate-to-leaf handler doesn't double-fire. For ``data-action=sync-all``
# this is load-bearing: without it the user would (1) start a Sync All AND
# (2) get pulled off the dashboard mid-fan-out.

_POINTER_SKILLS_MISSING_TARGET = {
    "skills": {"total": 3, "in_sync": 0, "missing_target": 3},
    "commands": {"total": 0, "in_sync": 0},
    "agents": {"total": 0, "in_sync": 0},
    "settings": {
        "total": 2,
        "in_sync": 2,
        "out_of_sync": 0,
        "missing_target": 0,
        "error": 0,
        "status": "in_sync",
    },
}


_POINTER_SKILLS_OUT_OF_SYNC = {
    "skills": {"total": 2, "in_sync": 0, "out_of_sync": 2},
    "commands": {"total": 0, "in_sync": 0},
    "agents": {"total": 0, "in_sync": 0},
    "settings": {
        "total": 2,
        "in_sync": 2,
        "out_of_sync": 0,
        "missing_target": 0,
        "error": 0,
        "status": "in_sync",
    },
}


_POINTER_SKILLS_ALL_THREE = {
    # Two runtimes: one with three drafts that need to be pushed
    # (missing_target=3), the same three drafted but with differences
    # (out_of_sync=2), plus two runtime-only artifacts not yet in canonical
    # (missing_canonical=2). The per-status counts can sum above ``total``
    # in multi-runtime payloads — see _renderCtxOverview's existing comment
    # block on (runtime, name) triples. ``total`` here is the count of
    # distinct names, not the sum of the per-status counts.
    "skills": {
        "total": 5,
        "in_sync": 0,
        "missing_target": 3,
        "out_of_sync": 2,
        "missing_canonical": 2,
    },
    "commands": {"total": 0, "in_sync": 0},
    "agents": {"total": 0, "in_sync": 0},
    "settings": {
        "total": 2,
        "in_sync": 2,
        "out_of_sync": 0,
        "missing_target": 0,
        "error": 0,
        "status": "in_sync",
    },
}


_POINTER_SETTINGS_WITH_MISSING_CANONICAL = {
    "skills": {"total": 0, "in_sync": 0},
    "commands": {"total": 0, "in_sync": 0},
    "agents": {"total": 0, "in_sync": 0},
    # Settings cannot legitimately produce ``missing_canonical`` — the
    # additive merge that owns settings sync cannot distinguish canonical-
    # authored from user-authored entries (ADR-0001 §5). The defense-in-
    # depth pin still stubs it: even if a future backend bug surfaced a
    # non-zero ``missing_canonical`` for settings, the frontend must
    # suppress the pointer rather than render an unreachable
    # "open Hooks to import" hyperlink.
    "settings": {
        "total": 2,
        "in_sync": 1,
        "out_of_sync": 1,
        "missing_target": 0,
        "missing_canonical": 5,
        "error": 0,
        "status": "out_of_sync",
    },
}


def test_pointer_missing_target_renders_and_triggers_sync_all(page, mm_web_url: str) -> None:
    """ADR-0009 §2 pin: ``missing_target > 0`` surfaces the push-intent
    pointer text AND, when clicked, programmatically clicks the Sync All
    button. ``stopPropagation`` on the pointer's click handler is what
    keeps the outer ``.ctx-overview-stat`` navigate-to-leaf handler from
    pulling the user off the dashboard mid-fan-out.

    The spy is a passive ``addEventListener`` on the Sync All button —
    it does not block the existing handler, so the in-app
    ``showConfirm`` dialog will surface as a side effect. That's fine
    for the assertion (the listener fires synchronously when
    ``btn.click()`` runs, before the confirm dialog even renders); the
    dialog itself stays unattached at teardown because we never resolve
    it, and the test exits before any async user-confirm gating fires.
    """
    install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_POINTER_SKILLS_MISSING_TARGET),
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    skills_tile = page.locator(
        "#ctx-overview-content .ctx-overview-stat[data-section='ctx-skills']"
    )
    pointer = skills_tile.locator(".ctx-overview-pointer[data-action='sync-all']")
    pointer_text = (pointer.text_content() or "").strip()
    assert "3" in pointer_text and "Sync All" in pointer_text, (
        f"missing_target pointer must surface the count and 'Sync All'; got: {pointer_text!r}"
    )

    # Spy on Sync All clicks. The pointer's click handler calls
    # ``syncAllBtn.click()`` synchronously; ``addEventListener`` listeners
    # fire on dispatch, so our counter increments before the existing
    # async confirm-dialog chain even resolves.
    page.evaluate(
        """() => {
            window._syncAllClickSpy = 0;
            document.getElementById('ctx-sync-all-btn')
              .addEventListener('click', () => { window._syncAllClickSpy++; });
        }"""
    )
    pointer.click()
    spy = page.evaluate("() => window._syncAllClickSpy")
    assert spy == 1, (
        f"missing_target pointer click must trigger exactly one Sync All "
        f"click via programmatic dispatch; got {spy}"
    )

    # Negative half (stopPropagation pin): the outer tile handler must
    # NOT have fired — if it did, switchSettingsSection would have moved
    # the active settings section away from the overview, hiding the
    # dashboard mid-fan-out. The ``#settings-ctx-overview`` section's
    # ``.active`` class is the observable signal.
    section_active = page.evaluate(
        "() => document.getElementById('settings-ctx-overview')"
        "        .classList.contains('active')"
    )
    assert section_active is True, (
        "stopPropagation regression — the outer tile handler fired after "
        "the pointer click, navigating away from the overview while a "
        "Sync All was in flight (ADR-0009 §2 mutation-surface invariant)"
    )


def test_pointer_out_of_sync_renders_and_navigates_to_leaf(page, mm_web_url: str) -> None:
    """ADR-0009 §2 pin: ``out_of_sync > 0`` surfaces the leaf-navigation
    pointer and clicking it navigates to the leaf section. The pointer
    text includes the leaf label (``Skills`` here, the tile's ``typ.label``)
    so the user reads which leaf they're heading to before the click.
    """
    install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_POINTER_SKILLS_OUT_OF_SYNC),
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    skills_tile = page.locator(
        "#ctx-overview-content .ctx-overview-stat[data-section='ctx-skills']"
    )
    pointer = skills_tile.locator(".ctx-overview-pointer[data-action='leaf']")
    pointer_text = (pointer.text_content() or "").strip()
    assert "2" in pointer_text and "Skills" in pointer_text, (
        f"out_of_sync pointer must mention the count and the leaf label; got: {pointer_text!r}"
    )

    pointer.click()
    # Navigation succeeded when the per-type list section toggles to .active
    # and the overview section toggles off — switchSettingsSection's
    # observable effect.
    page.wait_for_function(
        "() => {"
        "  const skills = document.getElementById('settings-ctx-skills');"
        "  return skills && skills.classList.contains('active');"
        "}",
        timeout=3_000,
    )
    overview_active = page.evaluate(
        "() => document.getElementById('settings-ctx-overview')"
        "        .classList.contains('active')"
    )
    assert overview_active is False, (
        "pointer click must navigate away from the overview into the "
        "skills leaf (ADR-0009 §2 — 'open <leaf>' is a leaf-bound action)"
    )


def test_pointer_priority_order_with_all_three_counts(page, mm_web_url: str) -> None:
    """ADR-0009 §2 priority order pin: when a tile reports non-zero
    counts for all three direction-bearing states, the pointer lines
    render in fixed order — ``missing_target`` first (push unambiguous),
    then ``out_of_sync`` (direction-neutral resolve), then
    ``missing_canonical`` (pull unambiguous). The order encodes the
    least-ambiguous-action-first heuristic; flipping it would surface
    the leaf-import line above the dashboard-Sync-All line and re-
    introduce the "which action do I take" ambiguity ADR-0009 was
    written to remove.
    """
    install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_POINTER_SKILLS_ALL_THREE),
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    skills_tile = page.locator(
        "#ctx-overview-content .ctx-overview-stat[data-section='ctx-skills']"
    )
    pointers = skills_tile.locator(".ctx-overview-pointer")
    assert pointers.count() == 3, (
        f"all three direction states must each surface their pointer; "
        f"got {pointers.count()} pointer(s)"
    )

    # data-action signals the kind of action each pointer takes. The
    # priority order is missing_target (sync-all) → out_of_sync (leaf) →
    # missing_canonical (leaf). The two ``leaf`` actions need their text
    # to disambiguate, which the count-string assertion below covers.
    actions = [pointers.nth(i).get_attribute("data-action") for i in range(3)]
    assert actions == ["sync-all", "leaf", "leaf"], (
        f"pointer data-action sequence must encode the ADR-0009 §2 priority; got {actions!r}"
    )

    texts = [(pointers.nth(i).text_content() or "").strip() for i in range(3)]
    # missing_target = 3 → "Run Sync All to push 3 missing entries."
    assert "3" in texts[0] and "Sync All" in texts[0], (
        f"first pointer must be missing_target (count=3, Sync All); got: {texts[0]!r}"
    )
    # out_of_sync = 2 → "Open Skills to resolve 2 differences."
    assert "2" in texts[1] and "Skills" in texts[1] and "differences" in texts[1], (
        f"second pointer must be out_of_sync (count=2, Skills, differences); got: {texts[1]!r}"
    )
    # missing_canonical = 2 → "2 runtime entries are not in canonical — open Skills to import."
    assert "2" in texts[2] and "Skills" in texts[2] and "import" in texts[2].lower(), (
        f"third pointer must be missing_canonical (count=2, Skills, import); got: {texts[2]!r}"
    )


def test_pointer_missing_canonical_omitted_on_settings_tile(page, mm_web_url: str) -> None:
    """ADR-0009 §2 last paragraph + ADR-0001 §5 pin: even when the
    response stubs ``missing_canonical > 0`` on the settings tile (which
    the backend cannot legitimately produce — additive merge has no way
    to extract canonical settings from a runtime-merged file), the
    frontend MUST NOT render the ``missing_canonical`` pointer for that
    tile. ``out_of_sync`` still renders since it's direction-neutral.
    """
    install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_POINTER_SETTINGS_WITH_MISSING_CANONICAL),
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    settings_tile = page.locator(
        "#ctx-overview-content .ctx-overview-stat[data-section='hooks-sync']"
    )
    pointers = settings_tile.locator(".ctx-overview-pointer")
    # Exactly one pointer (out_of_sync) should render — the missing_canonical
    # gate keeps the unreachable "import" pointer off the settings tile.
    assert pointers.count() == 1, (
        f"settings tile must render only the out_of_sync pointer; got {pointers.count()} pointer(s)"
    )
    pointer_text = (pointers.nth(0).text_content() or "").strip()
    assert "1" in pointer_text and "differences" in pointer_text, (
        f"sole settings pointer must be out_of_sync (count=1); got: {pointer_text!r}"
    )
    # Negative half: the missing_canonical phrasing must be absent from
    # the entire tile, even though the count was non-zero in the response.
    tile_text = (settings_tile.text_content() or "").lower()
    assert "import" not in tile_text, (
        f"settings tile must not surface an import pointer (ADR-0001 §5 "
        f"unidirectional readiness contract); tile text: {tile_text!r}"
    )


def test_pointer_absent_on_in_sync_tile(page, mm_web_url: str) -> None:
    """Negative-pin (``feedback_pin_invert_symmetric_assertion.md``):
    a healthy tile with only ``in_sync`` counts must render no pointer
    block. The dashboard's "glance" property depends on the pointer
    surface being silent when there's nothing actionable to surface —
    a regression that always renders an empty ``.ctx-overview-pointers``
    div would still satisfy ``pointers.count() == 0``, so this pin also
    asserts the wrapper element itself is absent.
    """
    install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_HEALTHY_OVERVIEW),
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    skills_tile = page.locator(
        "#ctx-overview-content .ctx-overview-stat[data-section='ctx-skills']"
    )
    assert skills_tile.locator(".ctx-overview-pointer").count() == 0, (
        "healthy in_sync tile must render no pointer lines"
    )
    assert skills_tile.locator(".ctx-overview-pointers").count() == 0, (
        "healthy in_sync tile must not render the empty pointer-wrapper "
        "either — the dashboard 'glance' property requires the block to "
        "be entirely absent, not just empty"
    )


# ---------------------------------------------------------------------------
# Issue #832 — ADR-0009 §1.c last-sync freshness indicator.
# ---------------------------------------------------------------------------
#
# The dashboard header surfaces "Last sync: <relative>" with the full ISO
# timestamp on hover. Source: canonical-source mtime (ADR §1.c). Null on
# empty project — the line is suppressed so the user doesn't see
# "Last sync: 56 years ago" on a fresh install (epoch-zero fallthrough).


def _build_iso(seconds_ago: int) -> str:
    """Return an ISO8601 UTC string ``seconds_ago`` ago. Uses ``Z`` trailer
    to match the backend (avoiding the ``+00:00`` ambiguity that some
    browsers parse inconsistently).
    """
    import datetime as _dt

    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=seconds_ago)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def test_overview_last_sync_renders_relative_text_and_iso_tooltip(page, mm_web_url: str) -> None:
    """ADR-0009 §1.c pin: header surfaces ``last_synced_at`` as a relative
    string ("5m ago") with the full ISO timestamp exposed via ``title=`` on
    the row container. The raw ISO is also reflected on the value span's
    ``data-iso`` attribute so screen-reader / scraping consumers don't have
    to parse the localized relative form.
    """
    install_default_stubs(page)
    iso = _build_iso(seconds_ago=300)  # ~5 minutes ago
    payload = {
        **_OVERVIEW_WITH_HEADER,
        "last_synced_at": iso,
    }
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    row = page.locator("#ctx-overview-content .ctx-overview-last-sync")
    assert row.count() == 1, (
        f"freshness row must render when last_synced_at is set; got {row.count()}"
    )
    # ``title`` carries the unredacted ISO for the diagnose case.
    assert row.get_attribute("title") == iso, (
        f"freshness row title= must carry the raw ISO; got {row.get_attribute('title')!r}"
    )
    value = row.locator(".ctx-overview-last-sync-value")
    assert value.get_attribute("data-iso") == iso, (
        f"value span must mirror the raw ISO on data-iso for non-tooltip "
        f"consumers; got {value.get_attribute('data-iso')!r}"
    )
    # #1076: the label carries its own ``title=`` explaining that the value
    # is canonical-file mtime, not a recorded sync event. Without this
    # tooltip the renamed "Canonical updated" label still leaves the
    # diagnose-case user guessing why edits also bump the value.
    label = row.locator(".ctx-overview-last-sync-label")
    label_title = label.get_attribute("title") or ""
    assert "mtime" in label_title, (
        f"label title= must explain the canonical-mtime data source; got {label_title!r}"
    )
    # Relative formatter routes through ``t('time.relative.*')``; for ~5
    # minutes ago we expect the minutes-ago branch in EN ("5m ago").
    text = (value.text_content() or "").strip()
    assert text.endswith("m ago"), (
        f"~300s ago must render via the minutes_ago branch of relativeTime(); got {text!r}"
    )


def test_overview_last_sync_absent_when_null(page, mm_web_url: str) -> None:
    """Negative pin (``feedback_pin_invert_symmetric_assertion.md``
    symmetric): a fresh project returns ``last_synced_at: null`` and the
    dashboard must suppress the row entirely — both the wrapper and any
    inner element. Rendering "Last sync: never" or an epoch-zero relative
    ("56 years ago") would mislead the user about empty-project state.
    """
    install_default_stubs(page)
    payload = {
        **_OVERVIEW_WITH_HEADER,
        "last_synced_at": None,
    }
    page.route(
        "**/api/context/overview",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    assert page.locator("#ctx-overview-content .ctx-overview-last-sync").count() == 0, (
        "null last_synced_at must suppress the entire row wrapper"
    )
    # The label key must NOT bleed into the header via some other path
    # (e.g. a label rendered without a value). Cross-locale-safe: search
    # by the data-i18n attribute name in case the locale flips around test
    # execution.
    header_text = page.locator("#ctx-overview-content .ctx-overview-header").text_content() or ""
    # Label was renamed from "Last sync" → "Canonical updated" in #1076 to
    # stop overstating the mtime-based data source as a sync-event log.
    assert "Canonical updated" not in header_text, (
        f"label must not leak into header when value is null; got: {header_text!r}"
    )
