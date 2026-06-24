"""Browser tests for the app-level Simple/Advanced toggle (S2.2).

S2.2 promotes the gateway's Simple-mode pattern to app level: a persisted
``m2m-app-simple`` flag (Simple by default) demotes the Tags + Timeline tabs and
the Settings → Data group behind an Advanced toggle in the global ``<header>``.

These specs cover what the jsdom unit suite
(``tests-js/app-simple-mode.test.mjs``) cannot: real-browser CSS visibility
(``display:none`` from the linked stylesheet), and — the highest-risk property —
that no entry path strands the user. The toggle must stay reachable on a
deep-linked non-Home tab, and a deep link to a now-hidden advanced tab must not
leave an orphaned panel (#1358).

The autouse ``_returning_install`` fixture seeds ``m2m-app-simple='0'``
(Advanced) so the rest of the browser suite keeps the full surface; each test
here overrides it with its own ``add_init_script`` (registered after the
fixture's, so last-write-wins) to pin the mode it exercises.
"""

from __future__ import annotations

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser

_ADVANCED_TABS = ("tags", "timeline")
_ALWAYS_TABS = ("home", "search", "sources", "context-gateway", "index", "settings")


def _force_app_mode(page, *, simple: bool) -> None:
    """Pin ``m2m-app-simple`` on every navigation, overriding the autouse seed."""
    value = "1" if simple else "0"
    page.add_init_script(
        f"try {{ localStorage.setItem('m2m-app-simple', '{value}'); }} catch (e) {{}}"
    )


def test_simple_default_hides_tags_and_timeline_tabs(page, mm_web_url: str) -> None:
    """In Simple mode the advanced tabs are CSS-hidden (present in the DOM,
    ``display:none``) while the always-on tabs and the toggle stay visible."""
    install_default_stubs(page)
    _force_app_mode(page, simple=True)
    page.goto(mm_web_url)
    page.wait_for_selector(".tab-nav .tab-btn", timeout=5_000)

    for tab in _ADVANCED_TABS:
        assert page.locator(f'.tab-btn[data-tab="{tab}"]').is_hidden(), (
            f"{tab} tab must be hidden in Simple mode"
        )
    for tab in _ALWAYS_TABS:
        assert page.locator(f'.tab-btn[data-tab="{tab}"]').is_visible(), (
            f"{tab} tab must stay visible in Simple mode"
        )

    toggle = page.locator("#app-mode-toggle")
    assert toggle.is_visible()
    assert toggle.get_attribute("aria-pressed") == "false"
    assert (toggle.locator(".app-mode-label").text_content() or "").strip() == "Simple"

    # The demoted Settings → Data group header is hidden too (checked on the
    # Settings tab; the group header is never collapsed, so this is unambiguous).
    page.evaluate("() => activateTab('settings')")
    assert page.locator('.settings-nav-group[data-group="data"]').is_hidden()

    # Home quick-actions that shortcut to advanced destinations are hidden too,
    # so Simple users never click a button that just redirects with a false
    # "Opened …" toast (Codex review). The always-on shortcuts stay.
    page.evaluate("() => activateTab('home')")
    for btn in ("home-export-btn", "home-dedup-btn", "home-tags-btn"):
        assert page.locator(f"#{btn}").is_hidden(), f"{btn} must be hidden in Simple"
    assert page.locator("#home-search-btn").is_visible()
    assert page.locator("#home-index-btn").is_visible()


def test_toggle_reveals_advanced_surface(page, mm_web_url: str) -> None:
    """Clicking the header toggle from Simple flips to Advanced: the advanced
    tabs + the Data group come back, and the toggle reports its pressed state."""
    install_default_stubs(page)
    _force_app_mode(page, simple=True)
    page.goto(mm_web_url)
    page.wait_for_selector(".tab-nav .tab-btn", timeout=5_000)

    page.locator("#app-mode-toggle").click()
    page.wait_for_function("() => !document.body.classList.contains('app-simple')", timeout=3_000)

    for tab in _ADVANCED_TABS:
        assert page.locator(f'.tab-btn[data-tab="{tab}"]').is_visible(), (
            f"{tab} tab must be revealed in Advanced mode"
        )
    toggle = page.locator("#app-mode-toggle")
    assert toggle.get_attribute("aria-pressed") == "true"
    assert (toggle.locator(".app-mode-label").text_content() or "").strip() == "Advanced"

    page.evaluate("() => activateTab('settings')")
    assert page.locator('.settings-nav-group[data-group="data"]').is_visible()

    page.evaluate("() => activateTab('home')")
    assert page.locator("#home-tags-btn").is_visible()
    assert page.locator("#home-export-btn").is_visible()


def test_deeplink_to_advanced_tab_in_simple_does_not_strand(page, mm_web_url: str) -> None:
    """A shared deep link to #timeline while in Simple must not orphan the user
    on a hidden panel: the timeline panel never activates, a visible tab stays
    active, and the toggle (the escape hatch) is still on screen."""
    install_default_stubs(page)
    _force_app_mode(page, simple=True)
    page.goto(mm_web_url + "#timeline")
    page.wait_for_selector(".tab-nav .tab-btn", timeout=5_000)

    # The hidden tab's panel must not be the active one.
    assert not page.locator("#tab-timeline").evaluate("el => el.classList.contains('active')")
    active_tab = page.evaluate("() => document.querySelector('.tab-btn.active')?.dataset.tab")
    assert active_tab is not None and active_tab != "timeline"
    assert active_tab in _ALWAYS_TABS

    # #1358: the escape hatch is reachable even on this bypassing entry path.
    assert page.locator("#app-mode-toggle").is_visible()


def test_toggle_reachable_on_deeplinked_non_home_tab(page, mm_web_url: str) -> None:
    """The header toggle lives in global chrome, so a deep link to a *visible*
    non-Home tab in Simple mode still shows a working toggle that reveals the
    advanced surface — the core #1358 reachability guarantee."""
    install_default_stubs(page)
    _force_app_mode(page, simple=True)
    page.goto(mm_web_url + "#sources")
    page.wait_for_function(
        "() => document.querySelector('.tab-btn.active')?.dataset.tab === 'sources'",
        timeout=5_000,
    )

    toggle = page.locator("#app-mode-toggle")
    assert toggle.is_visible() and toggle.is_enabled()
    assert page.locator('.tab-btn[data-tab="timeline"]').is_hidden()

    toggle.click()
    page.wait_for_selector('.tab-btn[data-tab="timeline"]:visible', timeout=3_000)
    # Revealing Advanced does not yank the user off their current tab.
    assert (
        page.evaluate("() => document.querySelector('.tab-btn.active')?.dataset.tab") == "sources"
    )


def test_hidden_settings_section_redirects_within_settings(page, mm_web_url: str) -> None:
    """Opening a now-hidden Data-group section in Simple must redirect to a
    Settings-tab section (Config), not bounce to the Gateway tab — the Gateway
    sidebar shares .settings-nav-btn but belongs to a different tab (Codex)."""
    install_default_stubs(page)
    _force_app_mode(page, simple=True)
    page.goto(mm_web_url)
    page.wait_for_selector(".tab-nav .tab-btn", timeout=5_000)
    page.evaluate("() => activateTab('settings')")

    page.evaluate("() => switchSettingsSection('dedup')")

    assert (
        page.evaluate("() => document.querySelector('.tab-btn.active')?.dataset.tab") == "settings"
    )
    assert (
        page.evaluate("() => document.querySelector('.settings-nav-btn.active')?.dataset.section")
        == "config"
    )
