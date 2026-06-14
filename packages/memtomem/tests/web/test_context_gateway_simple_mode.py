"""Browser tests for the Context Gateway Simple mode (ADR-0026 P1a, #1353).

Simple mode is a progressive-disclosure layer over the Overview: a
``localStorage`` flag (default OFF = Advanced, per ADR-0026 D-F's staged
rollout) that toggles a ``.ctx-simple`` class on ``#tab-context-gateway``.
``.ctx-simple`` hides the section nav, the hoisted control bar, and the tile
grid (CSS) while the Overview renders a one-line verdict + a read-only per-type
row list (3-state display remap).

These specs cover what the jsdom unit suite
(``tests-js/ctx-simple-mode.test.mjs``) cannot — real-browser CSS visibility
(``display:none`` from the linked stylesheet), keyboard focus, and the
return-to-Advanced navigation. The harness ``page.route()``-stubs
``/api/context/overview`` and the lifespan is off (see ``conftest.py``).
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


# skills→needs_sync, commands→in_tools, agents→not_saved, mcp→in_tools. No
# attention state, so the aggregate verdict is the "sync to push out" line.
_OVERVIEW = {
    "skills": {"total": 2, "in_sync": 1, "missing_target": 1},
    "commands": {"total": 1, "in_sync": 1},
    "agents": {"total": 1, "in_sync": 0, "missing_canonical": 1},
    "mcp_servers": {"total": 1, "in_sync": 1},
    "settings": {"total": 1, "in_sync": 1, "status": "in_sync"},
    "detected_runtimes": [],
    "project_root": "/srv",
    "target_scope": "project_shared",
}

_EMPTY_OVERVIEW = {
    "skills": {"total": 0, "in_sync": 0},
    "commands": {"total": 0, "in_sync": 0},
    "agents": {"total": 0, "in_sync": 0},
    "mcp_servers": {"total": 0, "in_sync": 0},
    "settings": {"total": 0, "in_sync": 0, "status": "in_sync"},
    "detected_runtimes": [],
    "project_root": "/srv",
    "target_scope": "project_shared",
}


def _stub_overview(page, payload=_OVERVIEW) -> None:
    install_default_stubs(page)
    page.route(
        "**/api/context/overview**",
        lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(payload)),
    )


def _open_context_gateway(page) -> None:
    """Land on ``settings-ctx-overview`` and wait for the overview to mount."""
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-overview')")
    page.wait_for_function(
        "() => {"
        "  const tiles = document.querySelectorAll("
        "    '#ctx-overview-content .ctx-overview-stat');"
        "  return tiles.length > 0;"
        "}",
        timeout=5_000,
    )


def test_default_is_advanced_grid_and_nav_visible(page, mm_web_url: str) -> None:
    """Default (no flag) is Advanced — today's UI verbatim: the tile grid + the
    section nav render, no ``.ctx-simple`` class, no Simple body, and the toggle
    reports its un-pressed state."""
    _stub_overview(page)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    tab = page.locator("#tab-context-gateway")
    assert "ctx-simple" not in (tab.get_attribute("class") or "")
    assert page.locator("#ctx-overview-content .ctx-overview-grid").is_visible()
    assert page.locator("#tab-context-gateway .settings-nav").is_visible()
    assert page.locator(".ctx-overview-simple").count() == 0
    assert page.locator("#ctx-mode-toggle").get_attribute("aria-pressed") == "false"


def test_toggle_enters_simple_hides_nav_control_bar_grid(page, mm_web_url: str) -> None:
    """Toggling Simple flips the class + aria-pressed and, via the linked CSS,
    hides the nav, the hoisted control bar, and the tile grid — while the Simple
    verdict + per-type rows become visible with text-bearing status (never
    color-only, ADR-0026 D-G)."""
    _stub_overview(page)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.locator("#ctx-mode-toggle").click()
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)

    tab = page.locator("#tab-context-gateway")
    assert "ctx-simple" in (tab.get_attribute("class") or "")
    assert page.locator("#ctx-mode-toggle").get_attribute("aria-pressed") == "true"

    # CSS hides the Advanced surfaces (present in the DOM, display:none).
    assert page.locator("#tab-context-gateway .settings-nav").is_hidden()
    assert page.locator("#ctx-control-bar").is_hidden()
    assert page.locator("#ctx-overview-content .ctx-overview-grid").is_hidden()

    # One read-only row per artifact type (hooks excluded), each with text.
    rows = page.locator(".ctx-simple-row")
    assert rows.count() == 4
    assert page.locator(".ctx-simple-row[data-section='hooks-sync']").count() == 0
    needs_sync = page.locator(".ctx-simple-row[data-section='ctx-skills']")
    assert needs_sync.get_attribute("data-state") == "needs_sync"
    status_text = (needs_sync.locator(".ctx-simple-status-text").text_content() or "").strip()
    assert status_text == "Needs sync", f"3-state label must render as text; got {status_text!r}"

    verdict = (page.locator(".ctx-simple-verdict").text_content() or "").strip()
    assert verdict == "Some items aren't in your tools yet — sync to push them out.", (
        f"aggregate verdict must surface the sync-direction line; got {verdict!r}"
    )


def test_toggle_is_keyboard_focusable(page, mm_web_url: str) -> None:
    """D-G: the toggle is a real, keyboard-reachable button — focusing it lands
    the document focus on it (so its focus-visible ring + Enter/Space activation
    come for free)."""
    _stub_overview(page)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.locator("#ctx-mode-toggle").focus()
    assert page.evaluate("() => document.activeElement && document.activeElement.id") == (
        "ctx-mode-toggle"
    )
    # Activating via keyboard enters Simple mode (button semantics, no shim).
    page.keyboard.press("Enter")
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)
    assert "ctx-simple" in (page.locator("#tab-context-gateway").get_attribute("class") or "")


def test_manage_row_returns_to_advanced_and_navigates(page, mm_web_url: str) -> None:
    """A Simple row's Manage button leaves Simple mode (nav restored) and
    deep-links into the Advanced section that owns the type (P1a routes to
    Advanced; the inline Sync/Import action lands in P1b)."""
    _stub_overview(page)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.locator("#ctx-mode-toggle").click()
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)

    page.locator(".ctx-simple-row[data-section='ctx-skills'] .ctx-simple-manage").click()
    page.wait_for_function(
        "() => {"
        "  const sec = document.getElementById('settings-ctx-skills');"
        "  return sec && sec.classList.contains('active');"
        "}",
        timeout=3_000,
    )
    tab = page.locator("#tab-context-gateway")
    assert "ctx-simple" not in (tab.get_attribute("class") or "")
    assert page.locator("#tab-context-gateway .settings-nav").is_visible()


def test_empty_state_shows_hint_and_open_advanced_cta(page, mm_web_url: str) -> None:
    """An all-empty active tier surfaces the read-only empty hint + an
    Open-Advanced CTA (D-D, light form); the CTA leaves Simple mode."""
    _stub_overview(page, _EMPTY_OVERVIEW)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.locator("#ctx-mode-toggle").click()
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)

    assert page.locator(".ctx-simple-empty-hint").is_visible()
    cta = page.locator(".ctx-simple-advanced-cta")
    assert cta.is_visible()
    verdict = (page.locator(".ctx-simple-verdict").text_content() or "").strip()
    assert verdict == "Nothing is stored for this project yet."

    cta.click()
    page.wait_for_function(
        "() => !document.getElementById('tab-context-gateway')"
        "        .classList.contains('ctx-simple')",
        timeout=3_000,
    )


def test_simple_mode_persists_across_reload(page, mm_web_url: str) -> None:
    """The flag is persisted (localStorage), so a reload re-enters Simple mode
    without the user re-toggling — the Advanced toggle stays the visible
    rollback signal (ADR-0026 D-F)."""
    _stub_overview(page)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.locator("#ctx-mode-toggle").click()
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)
    assert page.evaluate("() => localStorage.getItem('memtomem_ctx_simple_mode')") == "1"

    page.goto(mm_web_url)
    _open_context_gateway(page)
    # Re-entered Simple mode on its own: class applied + Simple body present.
    assert "ctx-simple" in (page.locator("#tab-context-gateway").get_attribute("class") or "")
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)
    assert page.locator(".ctx-simple-row").count() == 4


def test_persisted_simple_on_non_overview_section_keeps_nav_visible(page, mm_web_url: str) -> None:
    """Lifecycle trap (Codex review): a persisted Simple flag must not strip the
    Advanced navigation off a non-Overview leaf. ``.ctx-simple`` stays on the
    tab, but the nav + control bar are hidden only ``:has(#settings-ctx-overview
    .active)`` — so deep-linking to ``ctx-skills`` with Simple on must still
    show the nav (no trap with the toggle out of view)."""
    _stub_overview(page)
    page.goto(mm_web_url)
    # Persist the flag as if a prior session enabled Simple, then reload.
    page.evaluate("() => localStorage.setItem('memtomem_ctx_simple_mode', '1')")
    page.goto(mm_web_url)
    page.evaluate("() => activateTab('settings')")
    # Deep-link straight to a non-Overview leaf (the trap entry path).
    page.evaluate("() => switchSettingsSection('ctx-skills')")
    page.wait_for_function(
        "() => {"
        "  const sec = document.getElementById('settings-ctx-skills');"
        "  return sec && sec.classList.contains('active');"
        "}",
        timeout=5_000,
    )

    # Flag is still on (sticky) ...
    assert page.evaluate("() => localStorage.getItem('memtomem_ctx_simple_mode')") == "1"
    assert "ctx-simple" in (page.locator("#tab-context-gateway").get_attribute("class") or "")
    # ... but off the Overview the nav stays reachable — no trap.
    assert page.locator("#tab-context-gateway .settings-nav").is_visible()
