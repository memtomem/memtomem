"""Browser tests for the Project Scope Matrix and Matrix Sync button.

Pins the following:
* Proper rendering of matrix row badges separating installed vs registered.
* Switching active scope via Select button.
* Scoped sync button confirm, POST fan-out, and loadCtxOverview refresh in finally.
"""

from __future__ import annotations

import json
import re
import time
import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


_MATRIX_PROJECTS = {
    "scopes": [
        {
            "scope_id": "",
            "label": "Server CWD",
            "root": "",
            "tier": "project",
            "sources": ["server-cwd"],
            "experimental": False,
            "missing": False,
            "enabled": True,
            "sync_eligible": True,
            "counts": {"skills": 0, "commands": 0, "agents": 0},
            "runtime_coverage": [],
        },
        {
            "scope_id": "scope-123",
            "label": "My Scoped Project",
            "root": "/fake/scoped/project",
            "tier": "project",
            # Real backend source string is ``known-projects`` (the frontend
            # derives "enrolled" from it). An enrolled + enabled scope is
            # sync-eligible, so the Sync button stays active.
            "sources": ["known-projects"],
            "experimental": False,
            "missing": False,
            "enabled": True,
            "sync_eligible": True,
            "counts": {"skills": 2, "commands": 1, "agents": 3, "mcp-servers": 0},
            "runtime_coverage": [
                {
                    "name": "claude",
                    "available": True,
                    "installed": True,
                    "memtomem_registered": True,
                },
                {
                    "name": "gemini",
                    "available": True,
                    "installed": True,
                    "memtomem_registered": False,
                },
                {
                    "name": "codex",
                    "available": True,
                    "installed": False,
                    "memtomem_registered": False,
                },
                {
                    "name": "kimi",
                    "available": False,
                    "installed": True,
                    "memtomem_registered": True,
                },
            ],
        },
    ]
}


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


def _open_context_gateway(page) -> None:
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


def _stub_overview_with_counter(page, payloads: list[dict]) -> dict:
    state = {"n": 0}

    def _handler(route):
        idx = min(state["n"], len(payloads) - 1)
        state["n"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payloads[idx]),
        )

    # Use regex to match overview endpoint with optional query parameters (like ?scope_id=...)
    page.route(re.compile(r".*/api/context/overview.*"), _handler)
    return state


def test_matrix_rendering_and_badges(page, mm_web_url: str) -> None:
    """Matrix renders runtime badges separating installed from registered."""
    install_default_stubs(page)

    # Override projects to return our matrix mock scope
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_MATRIX_PROJECTS)
        ),
    )
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    page.goto(mm_web_url)
    _open_context_gateway(page)

    # Verify Project Scope Matrix is rendered
    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    # Locate the row for My Scoped Project
    row = page.locator(".ctx-projects-matrix-table tbody tr", has_text="My Scoped Project")
    assert row.count() == 1

    # Verify inventory counts display
    inventory = row.locator(".ctx-matrix-counts")
    assert "🧩2" in (inventory.text_content() or "")

    # Claude column: available=true, installed=true, memtomem_registered=true => Active
    # ("Active" already implies registered, so no redundant " (Reg)" suffix).
    claude_badge = row.locator("td").nth(2).locator(".badge")
    assert (claude_badge.text_content() or "").strip() == "Active"
    assert claude_badge.evaluate("el => el.classList.contains('badge-success')")
    assert claude_badge.get_attribute("title") == "Detected, Installed & Registered"

    # Gemini column: available=true, installed=true, memtomem_registered=false => Detected
    gemini_badge = row.locator("td").nth(3).locator(".badge")
    assert (gemini_badge.text_content() or "").strip() == "Detected"
    assert gemini_badge.evaluate("el => el.classList.contains('badge-warning')")
    assert (
        gemini_badge.get_attribute("title")
        == "Marker folder exists & client installed, but not registered"
    )

    # Codex column: available=true, installed=false, memtomem_registered=false => Available
    codex_badge = row.locator("td").nth(4).locator(".badge")
    assert (codex_badge.text_content() or "").strip() == "Available"
    assert codex_badge.evaluate("el => el.classList.contains('badge-yellow')")
    assert codex_badge.get_attribute("title") == "Marker folder exists, but client not installed"

    # Kimi column: available=false, installed=true, memtomem_registered=true => Client (Reg)
    kimi_badge = row.locator("td").nth(5).locator(".badge")
    assert (kimi_badge.text_content() or "").strip() == "Client (Reg)"
    assert kimi_badge.evaluate("el => el.classList.contains('badge-blue')")
    assert kimi_badge.get_attribute("title") == "Client installed, but no project marker found"


def test_matrix_counts_null_renders_dash(page, mm_web_url: str) -> None:
    """A scope whose ``counts`` is ``null`` (the API default when the fetch does
    not opt into ``?include=counts``) renders a muted dash, not a misleading
    all-zero inventory row."""
    install_default_stubs(page)

    projects = json.loads(json.dumps(_MATRIX_PROJECTS))
    projects["scopes"][1]["counts"] = None
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(projects)),
    )
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    page.goto(mm_web_url)
    _open_context_gateway(page)
    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    row = page.locator(".ctx-projects-matrix-table tbody tr", has_text="My Scoped Project")
    text = (row.locator(".ctx-matrix-counts").text_content() or "").strip()
    assert text == "—"
    assert "🧩" not in text


def test_matrix_badges_localized_on_langchange(page, mm_web_url: str) -> None:
    """Runtime badge labels/tooltips localize via ``t()`` — a KO langchange swaps
    the English 'Active' for its Korean label (regression: badges were hardcoded
    English literals)."""
    install_default_stubs(page)

    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_MATRIX_PROJECTS)
        ),
    )
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW, _HEALTHY_OVERVIEW])

    page.goto(mm_web_url)
    _open_context_gateway(page)
    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    row = page.locator(".ctx-projects-matrix-table tbody tr", has_text="My Scoped Project")
    claude_badge = row.locator("td").nth(2).locator(".badge")
    assert (claude_badge.text_content() or "").strip() == "Active"

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    # The langchange listener re-renders the overview (and matrix) from cache;
    # the claude badge label flips to its Korean string (활성).
    page.wait_for_function(
        "() => {"
        "  const row = Array.from(document.querySelectorAll("
        "    '.ctx-projects-matrix-table tbody tr'))"
        "    .find(tr => (tr.textContent || '').includes('My Scoped Project'));"
        "  if (!row) return false;"
        "  const b = row.querySelectorAll('td')[2].querySelector('.badge');"
        "  return !!b && b.textContent.trim() === '활성';"
        "}",
        timeout=4_000,
    )
    assert (
        row.locator("td").nth(2).locator(".badge").get_attribute("title")
        == "감지됨, 설치됨 및 등록됨"
    )


def test_matrix_select_changes_active_scope(page, mm_web_url: str) -> None:
    """Clicking Select in matrix changes active scope and triggers reload."""
    install_default_stubs(page)

    # First render with no active scope (active scope is empty/server cwd)
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_MATRIX_PROJECTS)
        ),
    )

    overview_state = _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW, _HEALTHY_OVERVIEW])

    page.goto(mm_web_url)
    _open_context_gateway(page)

    # Wait for table
    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    # Initial overview load calls: 1
    assert overview_state["n"] == 1

    # Locate Select button for My Scoped Project
    select_btn = page.locator('.ctx-matrix-select-btn[data-scope-id="scope-123"]')
    assert select_btn.is_visible()

    # Click Select
    select_btn.click()

    # Verify overview reloaded via Playwright-native wait loop
    deadline = time.monotonic() + 3.0
    while overview_state["n"] < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert overview_state["n"] >= 2


def test_matrix_sync_fires_scoped_posts_and_refreshes_on_aborted(page, mm_web_url: str) -> None:
    """Clicking Sync in matrix confirmed fires scoped POSTs and refreshes overview on aborted."""
    install_default_stubs(page)

    # Ensure target scope is user or project_shared so sync is not disabled
    # (Server CWD starts active scope)
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_MATRIX_PROJECTS)
        ),
    )

    overview_state = _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW, _HEALTHY_OVERVIEW])

    sync_calls = []

    def _record_sync(route):
        sync_calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/context/skills/sync**", _record_sync)
    page.route("**/api/context/commands/sync**", _record_sync)
    page.route("**/api/context/agents/sync**", _record_sync)
    page.route("**/api/context/mcp-servers/sync**", _record_sync)

    # Settings returns aborted (mtime conflict)
    def _settings_aborted(route):
        sync_calls.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"results": [{"name": "claude", "status": "aborted", "reason": "mtime conflict"}]}
            ),
        )

    page.route("**/api/context/settings/sync**", _settings_aborted)

    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    # Click Sync button for scope-123
    sync_btn = page.locator('.ctx-matrix-sync-btn[data-scope-id="scope-123"]')
    assert sync_btn.is_visible()
    sync_btn.click()

    # Confirm dialog appears
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=2_000)
    page.locator("#confirm-ok-btn").click()

    # Toast warning for aborted settings sync
    page.wait_for_selector("#toast-container .toast.toast-warning", timeout=4_000)

    # Verify that the sync POST calls fanned out with scope_id=scope-123
    assert len(sync_calls) == 5
    for url in sync_calls:
        assert "scope_id=scope-123" in url

    # Verify loadCtxOverview was called even though it aborted!
    # n=1 (initial mount), n=2 (after sync refresh)
    deadline = time.monotonic() + 3.0
    while overview_state["n"] < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert overview_state["n"] >= 2


def test_matrix_sync_server_cwd_empty_scope_id(page, mm_web_url: str) -> None:
    """Clicking Sync on the default Server CWD row (empty scope_id) fires sync POSTs without scope_id param."""
    install_default_stubs(page)

    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_MATRIX_PROJECTS)
        ),
    )

    overview_state = _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW, _HEALTHY_OVERVIEW])

    sync_calls = []

    def _record_sync(route):
        sync_calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/context/skills/sync**", _record_sync)
    page.route("**/api/context/commands/sync**", _record_sync)
    page.route("**/api/context/agents/sync**", _record_sync)
    page.route("**/api/context/mcp-servers/sync**", _record_sync)
    page.route("**/api/context/settings/sync**", _record_sync)

    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    # Server CWD is the first row with empty scope-id
    sync_btn = page.locator('.ctx-matrix-sync-btn[data-scope-id=""]')
    assert sync_btn.is_visible()
    sync_btn.click()

    # Confirm dialog appears
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=2_000)
    page.locator("#confirm-ok-btn").click()

    # Wait for success toast
    page.wait_for_selector("#toast-container .toast.toast-success", timeout=4_000)

    # Verify that the sync POST calls fanned out, but did NOT carry query parameter (or had empty scope_id)
    assert len(sync_calls) == 5
    for url in sync_calls:
        assert "scope_id=" not in url or "scope_id=&" in url or url.endswith("scope_id=")

    # Verify overview reloaded
    deadline = time.monotonic() + 3.0
    while overview_state["n"] < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert overview_state["n"] >= 2


def test_matrix_write_blocked_in_user_tier(page, mm_web_url: str) -> None:
    """Matrix buttons sync, add-project, and remove must carry write-blocked attributes when target tier is user."""
    install_default_stubs(page)

    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_MATRIX_PROJECTS)
        ),
    )
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    # Initially, project_shared is active => data-write-blocked must not exist
    sync_btn = page.locator('.ctx-matrix-sync-btn[data-scope-id="scope-123"]')
    add_btn = page.locator(".ctx-matrix-add-project-btn")
    # Server CWD is not removable, but scope-123 is removable (has remove btn)
    remove_btn = page.locator('.ctx-matrix-remove-btn[data-scope-id="scope-123"]')

    assert sync_btn.get_attribute("data-write-blocked") is None
    assert add_btn.get_attribute("data-write-blocked") is None
    assert remove_btn.get_attribute("data-write-blocked") is None

    # Swap tier filter to "user"
    page.locator('.ctx-tier-filter button[data-scope="user"]').click()

    # Now, all matrix write affordances must carry data-write-blocked="user"
    assert sync_btn.get_attribute("data-write-blocked") == "user"
    assert add_btn.get_attribute("data-write-blocked") == "user"
    assert remove_btn.get_attribute("data-write-blocked") == "user"


def test_matrix_add_project_reloads_overview_and_does_not_redirect(page, mm_web_url: str) -> None:
    """Matrix Add Project button opens picker, adds project, reloads overview, and remains on overview section."""
    install_default_stubs(page)

    # First load
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_MATRIX_PROJECTS)
        ),
    )
    overview_state = _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW, _HEALTHY_OVERVIEW])

    # Intercept known-projects POST
    page.route(
        "**/api/context/known-projects",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"scope_id": "scope-new", "root": "/fake/new/project"}),
        ),
    )

    page.goto(mm_web_url)
    _open_context_gateway(page)
    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    # Mock PathPicker.open to resolve with a path immediately
    page.evaluate("window.PathPicker = { open: (opts) => opts.onSelect('/fake/new/project') }")

    # Locate and click matrix Add Project button
    add_btn = page.locator(".ctx-matrix-add-project-btn")
    assert add_btn.is_visible()
    add_btn.click()

    # Wait for success toast
    page.wait_for_selector("#toast-container .toast.toast-success", timeout=4_000)

    # Verify overview reloaded (calls count goes from 1 to 2)
    deadline = time.monotonic() + 3.0
    while overview_state["n"] < 2 and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert overview_state["n"] >= 2

    # Verify we are still on the overview section, NOT redirected to skills
    assert page.locator("#settings-ctx-overview").evaluate("el => el.classList.contains('active')")
    assert not page.locator("#settings-ctx-skills").evaluate(
        "el => el.classList.contains('active')"
    )


# Sync-enrollment gating (#1203): one scope per eligibility shape. ``scope-on``
# is the eligible control; ``scope-paused`` is enrolled-but-paused; ``scope-scan``
# is a scan-only auto-displayed row that was never enrolled.
_ELIGIBILITY_PROJECTS = {
    "scopes": [
        {
            "scope_id": "",
            "label": "Server CWD",
            "root": "",
            "tier": "project",
            "sources": ["server-cwd"],
            "experimental": False,
            "missing": False,
            "enabled": True,
            "sync_eligible": True,
            "counts": {"skills": 0, "commands": 0, "agents": 0},
            "runtime_coverage": [],
        },
        {
            "scope_id": "scope-on",
            "label": "Enabled Project",
            "root": "/fake/on",
            "tier": "project",
            "sources": ["known-projects"],
            "experimental": False,
            "missing": False,
            "enabled": True,
            "sync_eligible": True,
            "counts": {"skills": 1, "commands": 0, "agents": 0},
            "runtime_coverage": [],
        },
        {
            "scope_id": "scope-paused",
            "label": "Paused Project",
            "root": "/fake/paused",
            "tier": "project",
            "sources": ["known-projects"],
            "experimental": False,
            "missing": False,
            "enabled": False,
            "sync_eligible": False,
            "counts": {"skills": 1, "commands": 0, "agents": 0},
            "runtime_coverage": [],
        },
        {
            "scope_id": "scope-scan",
            "label": "Scanned Project",
            "root": "/fake/scan",
            "tier": "project",
            "sources": ["claude-projects"],
            "experimental": False,
            "missing": False,
            "enabled": True,
            "sync_eligible": False,
            "counts": {"skills": 1, "commands": 0, "agents": 0},
            "runtime_coverage": [],
        },
        {
            # Enrolled + enabled (so sync_eligible) but the root is gone. The
            # missing arm (branch 1) must win over the eligibility arm.
            "scope_id": "scope-missing",
            "label": "Missing Project",
            "root": "/fake/missing",
            "tier": "project",
            "sources": ["known-projects"],
            "experimental": False,
            "missing": True,
            "enabled": True,
            "sync_eligible": True,
            "counts": None,
            "runtime_coverage": [],
        },
    ]
}


def test_matrix_sync_gated_on_eligibility(page, mm_web_url: str) -> None:
    """The matrix Sync button is disabled for an ineligible scope (not enrolled,
    or enrolled-but-paused), with the reason carried on ``data-i18n-title`` — not
    a plain ``title``, which the write-block sweep would strip. An eligible
    scope stays enabled."""
    install_default_stubs(page)
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_ELIGIBILITY_PROJECTS)
        ),
    )
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    page.goto(mm_web_url)
    _open_context_gateway(page)
    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    eligible = page.locator('.ctx-matrix-sync-btn[data-scope-id="scope-on"]')
    paused = page.locator('.ctx-matrix-sync-btn[data-scope-id="scope-paused"]')
    scan = page.locator('.ctx-matrix-sync-btn[data-scope-id="scope-scan"]')

    assert not eligible.is_disabled()

    assert paused.is_disabled()
    assert paused.get_attribute("data-i18n-title") == "settings.ctx.matrix_sync_paused_title"

    assert scan.is_disabled()
    assert scan.get_attribute("data-i18n-title") == "settings.ctx.matrix_sync_not_enrolled_title"


def test_matrix_sync_ineligible_tooltip_survives_write_block_sweep(page, mm_web_url: str) -> None:
    """The ineligibility tooltip rides on ``data-i18n-title``, so a
    ``_ctxRefreshWriteBlockedState`` pass in project_shared restores ``title``
    from it rather than stripping it — the regression a plain ``title`` hits,
    since ``.ctx-matrix-sync-btn`` is in the write-block sweep selector."""
    install_default_stubs(page)
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_ELIGIBILITY_PROJECTS)
        ),
    )
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    page.goto(mm_web_url)
    _open_context_gateway(page)
    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    paused = page.locator('.ctx-matrix-sync-btn[data-scope-id="scope-paused"]')
    expected = page.evaluate("() => t('settings.ctx.matrix_sync_paused_title')")
    assert paused.get_attribute("title") == expected

    # Run the write-block sweep directly (project_shared → the not-blocked branch
    # that restores title from data-i18n-title or strips it).
    page.evaluate("() => _ctxRefreshWriteBlockedState()")

    # Title preserved (restored from data-i18n-title), NOT removed.
    assert paused.get_attribute("title") == expected


def test_matrix_sync_missing_root_uses_disabled_title_over_eligibility(
    page, mm_web_url: str
) -> None:
    """A missing root disables Sync via the project_local/missing arm (branch 1),
    which takes precedence over the eligibility arm — even though the scope is
    enrolled + enabled (sync_eligible). Guards the branch ordering."""
    install_default_stubs(page)
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_ELIGIBILITY_PROJECTS)
        ),
    )
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    page.goto(mm_web_url)
    _open_context_gateway(page)
    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    missing = page.locator('.ctx-matrix-sync-btn[data-scope-id="scope-missing"]')
    assert missing.is_disabled()
    # The missing arm wins: NOT the paused/not-enrolled tooltip.
    assert missing.get_attribute("data-i18n-title") == "settings.ctx.matrix_sync_disabled_title"


def test_matrix_sync_project_local_tier_wins_over_eligibility(page, mm_web_url: str) -> None:
    """In the project_local tier every Sync button is disabled via branch 1
    (no fan-out). It must win over the eligibility arm: an enrolled-but-paused
    scope shows the project_local/missing tooltip, NOT the paused one — proving
    the branch ordering is not inverted."""
    install_default_stubs(page)
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_ELIGIBILITY_PROJECTS)
        ),
    )
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW, _HEALTHY_OVERVIEW])

    page.goto(mm_web_url)
    _open_context_gateway(page)
    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    page.locator('.ctx-tier-filter button[data-scope="project_local"]').click()

    # scope-paused is ineligible AND in project_local. Correct ordering → the
    # disabled-title arm. An inverted order would surface the paused title.
    paused = page.locator('.ctx-matrix-sync-btn[data-scope-id="scope-paused"]')
    paused_title = paused.get_attribute("data-i18n-title")
    deadline = time.monotonic() + 3.0
    while paused_title != "settings.ctx.matrix_sync_disabled_title" and time.monotonic() < deadline:
        page.wait_for_timeout(50)
        paused_title = paused.get_attribute("data-i18n-title")
    assert paused_title == "settings.ctx.matrix_sync_disabled_title"


def test_sync_all_gated_when_active_scope_ineligible(page, mm_web_url: str) -> None:
    """Selecting a paused (ineligible) project as the active scope disables the
    top-level Sync All button with the paused reason, and clicking it bails with
    no sync fan-out. Without this gate an ineligible active scope is still
    syncable via Sync All, since the sync routes accept any resolvable scope
    (#1203 review P2): row-level gating alone is insufficient."""
    install_default_stubs(page)
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_ELIGIBILITY_PROJECTS)
        ),
    )
    # Initial mount + the re-render triggered by selecting a new active scope.
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW, _HEALTHY_OVERVIEW])

    sync_calls: list[str] = []

    def _record_sync(route):
        sync_calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    for kind in ("skills", "commands", "agents", "mcp-servers", "settings"):
        page.route(f"**/api/context/{kind}/sync**", _record_sync)

    page.goto(mm_web_url)
    _open_context_gateway(page)
    page.wait_for_selector(".ctx-projects-matrix-table", timeout=5_000)

    # Make the paused (sync_eligible:false, enrolled) scope the active project.
    page.locator('.ctx-matrix-select-btn[data-scope-id="scope-paused"]').click()

    sync_all = page.locator("#ctx-sync-all-btn")
    # Wait for the post-select overview re-render to apply the eligibility gate.
    deadline = time.monotonic() + 3.0
    while sync_all.get_attribute("data-runtime-only") != "true" and time.monotonic() < deadline:
        page.wait_for_timeout(50)
    assert sync_all.get_attribute("data-runtime-only") == "true"
    assert sync_all.get_attribute("aria-disabled") == "true"
    # dataset.syncIneligible → data-sync-ineligible; carries the paused reason.
    assert sync_all.get_attribute("data-sync-ineligible") == "settings.ctx.sync_all_paused_tooltip"

    # Firing the click on the gated button bails: info toast, NO confirm dialog,
    # NO syncs. dispatch_event bypasses Playwright's actionability check (the
    # button is aria-disabled) to exercise the handler's bail path directly.
    sync_all.dispatch_event("click")
    page.wait_for_selector("#toast-container .toast", timeout=3_000)
    assert page.locator("#confirm-modal:not([hidden])").count() == 0
    page.wait_for_timeout(300)
    assert sync_calls == []
