"""Browser tests for the Context Gateway Simple mode (ADR-0026 P1a/P1b, #1353).

Simple mode is a progressive-disclosure layer over the Overview: a
``localStorage`` flag (default ON = Simple since the D-F flip 2026-06-18, a
reversible experiment; Advanced via the toggle) that toggles a ``.ctx-simple``
class on ``#tab-context-gateway``. ``.ctx-simple`` hides the section nav, the
hoisted control bar, and the tile grid (CSS) while the Overview renders a
one-line verdict + a per-type row list (3-state display remap). P1b adds one
inline control per row (Sync / Import / a check / Manage) and a counted
cross-tier summary on an empty active tier (D-D).

These specs cover what the jsdom unit suite
(``tests-js/ctx-simple-mode.test.mjs``) cannot — real-browser CSS visibility
(``display:none`` from the linked stylesheet), keyboard focus, the
return-to-Advanced navigation, the inline Sync confirm flow, and the async
cross-tier fan-out. The harness ``page.route()``-stubs ``/api/context/overview``
and the lifespan is off (see ``conftest.py``).
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

# P1b: mcp-servers runtime-only (not_saved). mcp-servers has no /import route, so
# its row must fall back to Manage rather than show an inline Import button.
_MCP_RUNTIME_ONLY = {
    **_OVERVIEW,
    "mcp_servers": {"total": 1, "in_sync": 0, "missing_canonical": 1},
}

# P1b D-D: the active tier (project_shared) is empty while the User tier holds 3.
_USER_TIER_OVERVIEW = {
    "skills": {"total": 3, "in_sync": 3},
    "commands": {"total": 0},
    "agents": {"total": 0},
    "mcp_servers": {"total": 0},
    "settings": {"total": 0, "in_sync": 0, "status": "in_sync"},
    "detected_runtimes": [],
    "project_root": "/srv",
    "target_scope": "user",
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


def _switch_tier(page, scope: str) -> None:
    """Click the tier filter for ``scope`` in the shared control bar (Advanced)
    and wait for the sweep to settle (button reports its pressed state)."""
    page.locator(f"#ctx-control-bar .ctx-tier-filter button[data-scope='{scope}']").click()
    page.wait_for_function(
        "(s) => {"
        "  const b = document.querySelector("
        '    `#ctx-control-bar .ctx-tier-filter button[data-scope="${s}"]`);'
        "  return b && b.getAttribute('aria-pressed') === 'true';"
        "}",
        arg=scope,
        timeout=3_000,
    )


def test_default_is_simple_verdict_and_rows_visible(page, mm_web_url: str) -> None:
    """Default (no flag) is Simple (ADR-0026 D-F flipped 2026-06-18, reversible):
    the ``.ctx-simple`` class is on, the Simple verdict + per-type rows render,
    the Advanced nav / control bar / tile grid are CSS-hidden, and the toggle
    reports its pressed state (Advanced stays one click away as the rollback)."""
    _stub_overview(page)
    page.goto(mm_web_url)
    _open_context_gateway(page)
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)

    tab = page.locator("#tab-context-gateway")
    assert "ctx-simple" in (tab.get_attribute("class") or "")
    assert page.locator("#ctx-mode-toggle").get_attribute("aria-pressed") == "true"

    # CSS hides the Advanced surfaces (present in the DOM, display:none).
    assert page.locator("#tab-context-gateway .settings-nav").is_hidden()
    assert page.locator("#ctx-control-bar").is_hidden()
    assert page.locator("#ctx-overview-content .ctx-overview-grid").is_hidden()

    # One row per artifact type including hooks/settings (in_sync here → a fifth
    # in_tools row), each with text — a settings-only drift must reach the verdict.
    rows = page.locator(".ctx-simple-row")
    assert rows.count() == 5
    assert page.locator(".ctx-simple-row[data-section='hooks-sync']").count() == 1
    needs_sync = page.locator(".ctx-simple-row[data-section='ctx-skills']")
    assert needs_sync.get_attribute("data-state") == "needs_sync"
    status_text = (needs_sync.locator(".ctx-simple-status-text").text_content() or "").strip()
    assert status_text == "Needs sync", f"3-state label must render as text; got {status_text!r}"

    verdict = (page.locator(".ctx-simple-verdict").text_content() or "").strip()
    assert verdict == "Some items aren't in your tools yet — sync to push them out.", (
        f"aggregate verdict must surface the sync-direction line; got {verdict!r}"
    )


def test_toggle_exits_to_advanced_then_back(page, mm_web_url: str) -> None:
    """From the default Simple view, the toggle flips to Advanced (nav + control
    bar + tile grid restored, no Simple body) and back to Simple — Advanced is
    the reversible rollback (ADR-0026 D-F)."""
    _stub_overview(page)
    page.goto(mm_web_url)
    _open_context_gateway(page)
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)

    # Default Simple → click exits to Advanced: nav / control bar / grid restored.
    page.locator("#ctx-mode-toggle").click()
    page.wait_for_function(
        "() => !document.getElementById('tab-context-gateway').classList.contains('ctx-simple')",
        timeout=3_000,
    )
    assert page.locator("#ctx-mode-toggle").get_attribute("aria-pressed") == "false"
    # The toggle re-renders the Overview async (loadCtxOverview), so wait for the
    # Advanced grid to come back rather than asserting before the render lands.
    page.locator("#ctx-overview-content .ctx-overview-grid").wait_for(
        state="visible", timeout=3_000
    )
    assert page.locator("#tab-context-gateway .settings-nav").is_visible()
    assert page.locator(".ctx-overview-simple").count() == 0

    # Click again → back to Simple: class on, verdict + rows return.
    page.locator("#ctx-mode-toggle").click()
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)
    assert "ctx-simple" in (page.locator("#tab-context-gateway").get_attribute("class") or "")
    assert page.locator("#ctx-mode-toggle").get_attribute("aria-pressed") == "true"
    assert page.locator(".ctx-simple-row").count() == 5


def test_toggle_is_keyboard_focusable(page, mm_web_url: str) -> None:
    """D-G: the toggle is a real, keyboard-reachable button — focusing it lands
    the document focus on it (so its focus-visible ring + Enter/Space activation
    come for free)."""
    _stub_overview(page)
    page.goto(mm_web_url)
    # Start from Advanced (the non-default now) so the Enter press enters Simple.
    page.evaluate("() => localStorage.setItem('memtomem_ctx_simple_mode', '0')")
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


def test_mcp_not_saved_falls_back_to_manage_and_navigates(page, mm_web_url: str) -> None:
    """P1b: a fixable row now acts inline, so the Manage deep-link is asserted on
    a row with no safe one-click fix — an mcp-servers ``not_saved`` row, which has
    no ``/import`` route and keeps the read-only Manage button. Clicking it leaves
    Simple mode (nav restored) and deep-links into the mcp-servers section."""
    _stub_overview(page, _MCP_RUNTIME_ONLY)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    # Simple is the default — land straight on the verdict + rows, no toggle.
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)

    row = page.locator(".ctx-simple-row[data-section='ctx-mcp-servers']")
    assert row.get_attribute("data-state") == "not_saved"
    assert row.locator("[data-ctx-action]").count() == 0, "mcp not_saved has no inline Import"
    row.locator(".ctx-simple-manage").click()
    page.wait_for_function(
        "() => {"
        "  const sec = document.getElementById('settings-ctx-mcp-servers');"
        "  return sec && sec.classList.contains('active');"
        "}",
        timeout=3_000,
    )
    tab = page.locator("#tab-context-gateway")
    assert "ctx-simple" not in (tab.get_attribute("class") or "")
    assert page.locator("#tab-context-gateway .settings-nav").is_visible()


def test_inline_sync_button_focusable_and_opens_confirm(page, mm_web_url: str) -> None:
    """P1b: a needs_sync row carries a keyboard-reachable inline Sync button
    (D-G); clicking it runs the SAME flow as the Advanced toolbar — the impact
    preview falls back to the count-only confirm under the empty stub, so the
    shared confirm modal appears with the Sync title. The not_saved row carries an
    inline Import button alongside."""
    _stub_overview(page)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    # Simple is the default — land straight on the verdict + rows, no toggle.
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)

    sync = page.locator(".ctx-simple-row[data-section='ctx-skills'] [data-ctx-action='sync']")
    assert sync.count() == 1
    # Keyboard-reachable (D-G): focusing lands document focus on the button.
    sync.focus()
    assert (
        page.evaluate("() => document.activeElement && document.activeElement.dataset.ctxAction")
        == "sync"
    )
    # The importable not_saved row exposes an inline Import button.
    assert (
        page.locator(
            ".ctx-simple-row[data-section='ctx-agents'] [data-ctx-action='import']"
        ).count()
        == 1
    )

    sync.click()
    page.locator("#confirm-modal").wait_for(state="visible", timeout=3_000)
    assert (page.locator("#confirm-title").text_content() or "").strip() == "Sync"
    page.locator("#confirm-cancel-btn").click()


def test_empty_tier_names_items_in_another_tier(page, mm_web_url: str) -> None:
    """P1b D-D: an all-empty active tier fans out a read to the other tiers and,
    when one holds items, replaces the generic empty hint with a counted summary
    that names it ("Stored elsewhere: 3 in User"). The Overview route
    summarizes one tier per call, so the summary is keyed off the ``target_scope``
    query param — User holds 3, the active + local tiers are empty."""
    install_default_stubs(page)

    def _overview(route) -> None:
        payload = (
            _USER_TIER_OVERVIEW if "target_scope=user" in route.request.url else _EMPTY_OVERVIEW
        )
        route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))

    page.route("**/api/context/overview**", _overview)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    # Simple is the default — land straight on the verdict + rows, no toggle.
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)

    # The generic hint is patched with the cross-tier summary once the fan-out
    # lands (async, best-effort) — poll until the User tier is named.
    page.wait_for_function(
        "() => {"
        "  const s = document.querySelector('.ctx-simple-empty-hint > span');"
        "  return s && s.textContent.includes('User') && s.textContent.includes('3');"
        "}",
        timeout=3_000,
    )


def test_empty_state_shows_hint_and_open_advanced_cta(page, mm_web_url: str) -> None:
    """An all-empty active tier surfaces the read-only empty hint + the two
    first-action CTAs (Import from tools / Create a skill); either one leaves
    Simple mode and deep-links into the Advanced skills section."""
    _stub_overview(page, _EMPTY_OVERVIEW)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    # Simple is the default — land straight on the empty-state hint, no toggle.
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)

    assert page.locator(".ctx-simple-empty-hint").is_visible()
    ctas = page.locator(".ctx-simple-advanced-cta")
    assert ctas.count() == 2, "empty state offers Import and Create CTAs"
    labels = [(ctas.nth(i).text_content() or "").strip() for i in range(2)]
    assert labels == ["Import from tools", "Create a skill"]
    verdict = (page.locator(".ctx-simple-verdict").text_content() or "").strip()
    assert verdict == "Nothing is stored for this project yet."

    ctas.first.click()
    page.wait_for_function(
        "() => !document.getElementById('tab-context-gateway')"
        "        .classList.contains('ctx-simple')",
        timeout=3_000,
    )


def test_advanced_choice_persists_across_reload(page, mm_web_url: str) -> None:
    """The flag is persisted (localStorage), so a user's explicit switch to
    Advanced survives a reload (it does not snap back to the Simple default) —
    the toggle is a durable per-user rollback (ADR-0026 D-F)."""
    _stub_overview(page)
    page.goto(mm_web_url)
    _open_context_gateway(page)
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)

    # Default is Simple; switch to Advanced and confirm the flag persists as '0'.
    page.locator("#ctx-mode-toggle").click()
    page.wait_for_function(
        "() => !document.getElementById('tab-context-gateway').classList.contains('ctx-simple')",
        timeout=3_000,
    )
    assert page.evaluate("() => localStorage.getItem('memtomem_ctx_simple_mode')") == "0"

    page.goto(mm_web_url)
    _open_context_gateway(page)
    # Stayed Advanced on its own: no .ctx-simple class, the tile grid is visible.
    assert "ctx-simple" not in (page.locator("#tab-context-gateway").get_attribute("class") or "")
    assert page.locator("#ctx-overview-content .ctx-overview-grid").is_visible()
    assert page.locator(".ctx-overview-simple").count() == 0


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


def test_project_local_inline_sync_is_write_blocked(page, mm_web_url: str) -> None:
    """P1b (Codex review): the Simple-mode inline Sync/Import buttons ride the same
    tier write-block sweep as the Advanced toolbar. On the no-write project_local
    tier the inline Sync button is dimmed (``data-write-blocked`` + ``aria-disabled``)
    and a click is intercepted by the capture-phase guard — a toast, never a confirm
    dialog or a doomed /sync POST."""
    # A needs_sync skills row forces a Sync button to render so the block is
    # observable; the stub serves it for the project_local overview fetch too.
    payload = {**_OVERVIEW, "target_scope": "project_local"}
    sync_posts: list[str] = []
    install_default_stubs(page)
    page.route(
        "**/api/context/overview**",
        lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(payload)),
    )

    def _record_sync(route) -> None:
        sync_posts.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/context/skills/sync**", _record_sync)
    page.goto(mm_web_url)
    # Start from Advanced so the control bar (Simple-hidden) is reachable to
    # switch tiers; then toggle into Simple to observe the inline write-block.
    page.evaluate("() => localStorage.setItem('memtomem_ctx_simple_mode', '0')")
    page.goto(mm_web_url)
    _open_context_gateway(page)
    _switch_tier(page, "project_local")

    page.locator("#ctx-mode-toggle").click()
    page.locator(".ctx-overview-simple").wait_for(timeout=3_000)

    sync = page.locator(".ctx-simple-row[data-section='ctx-skills'] [data-ctx-action='sync']")
    assert sync.count() == 1
    assert sync.get_attribute("data-write-blocked") == "project_local"
    assert sync.get_attribute("aria-disabled") == "true"

    # Capture-phase guard fires a toast; no confirm modal, no /sync POST.
    # ``force=True`` bypasses Playwright's actionability wait (it treats
    # ``aria-disabled`` as disabled) so the real click still reaches the
    # document capture listener under test.
    sync.click(force=True)
    page.wait_for_timeout(300)
    assert page.locator("#confirm-modal").is_hidden()
    assert sync_posts == [], f"blocked Sync must issue no POST; got {sync_posts!r}"
