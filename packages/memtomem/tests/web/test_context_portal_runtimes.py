"""Browser tests for the Context Portal per-CLI runtime chips + filter (ADR-0021 PR5).

Drives the real ``context-portal.js`` runtime surface in the SPA: the heading
chip strip (per-client install/registration traffic-lights for the *active*
project), the not-installed chip's install-guide modal, the per-row
traffic-lights ("row UI" deferred from PR4), and the client-side provider
filter with its ``?runtime=`` deep-link.

``GET /api/context/runtimes`` is fetched once per scope (the endpoint resolves
per-scope via ``resolve_scope_root``'s ``scope_id`` query param), so the stub
varies its payload by the ``scope_id`` it sees on the URL. ``page.route``
short-circuits every ``/api`` call before it reaches the server, so no CSRF
middleware / DB is in play — the spec asserts the render + click wiring only.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from .conftest import install_default_stubs

# Marks every test here as browser-dependent: the root conftest auto-skips
# ``@pytest.mark.browser`` when Chromium/pytest-playwright is absent (the CI
# ``test`` lane), and the ``test-browser`` lane selects it via ``-m browser``.
pytestmark = pytest.mark.browser

_PORTAL_SCOPES = {
    "target_scope": "project_shared",
    "scopes": [
        {
            "scope_id": "",
            "project_scope_id": "",
            "label": "Server CWD",
            "root": "/srv",
            "tier": "project",
            "sources": ["server-cwd"],
            "missing": False,
            "stale": False,
            "experimental": False,
            "counts": {"skills": 2, "commands": 1, "agents": 0, "mcp-servers": 0},
        },
        {
            "scope_id": "p-alpha",
            "project_scope_id": "p-alpha",
            "label": "Alpha",
            "root": "/work/alpha",
            "tier": "project",
            "sources": ["known-projects"],
            "missing": False,
            "stale": False,
            "experimental": False,
            "counts": {"skills": 5, "commands": 0, "agents": 0, "mcp-servers": 0},
        },
        {
            "scope_id": "p-beta",
            "project_scope_id": "p-beta",
            "label": "Beta",
            "root": "/work/beta",
            "tier": "project",
            "sources": ["known-projects"],
            "missing": False,
            "stale": True,
            "experimental": False,
            "counts": {"skills": 0, "commands": 0, "agents": 0, "mcp-servers": 0},
        },
        {
            "scope_id": "p-gone",
            "project_scope_id": "p-gone",
            "label": "Ghost",
            "root": "/work/ghost",
            "tier": "project",
            "sources": ["known-projects"],
            "missing": True,
            "stale": False,
            "experimental": False,
            "counts": None,
        },
    ],
}


def _rt(name, *, installed=False, registered=False, paths=None, error=None):
    """One RuntimeStatus.to_dict() entry."""
    return {
        "name": name,
        "installed": installed,
        "memtomem_registered": registered,
        "mms_registered": False,
        "registered_locations": ["user"] if registered else [],
        "config_paths": paths or [],
        "error_kind": error,
    }


# Per-scope runtime payloads, keyed by the scope_id the front-end sends. The
# active scope on a fresh page is the Server CWD ("") — its mix exercises every
# chip state: registered (claude), installed-unregistered (codex), error
# (antigravity → greyed, non-interactive), not-installed (kimi → greyed button).
_RUNTIMES_BY_SCOPE = {
    "": [
        _rt("claude", installed=True, registered=True, paths=["~/.claude.json"]),
        _rt("antigravity", installed=True, error="permission"),
        _rt("codex", installed=True, registered=False),
        _rt("kimi", installed=False),
    ],
    # claude is registered here too, so the "claude" provider filter keeps this row.
    "p-alpha": [
        _rt("claude", installed=True, registered=True, paths=["~/.claude.json"]),
        _rt("antigravity", installed=False),
        _rt("codex", installed=False),
        _rt("kimi", installed=False),
    ],
    # claude is installed but NOT registered → the "claude" filter drops this row
    # (the filter keys on registration, not mere install).
    "p-beta": [
        _rt("claude", installed=True, registered=False),
        _rt("antigravity", installed=False),
        _rt("codex", installed=False),
        _rt("kimi", installed=False),
    ],
}


def _stub_portal(page):
    """Catch-all + multi-scope projects payload + per-scope runtimes. Registered
    AFTER install_default_stubs so they win (page.route is last-route-wins)."""
    install_default_stubs(page)

    def _projects(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_PORTAL_SCOPES))

    page.route("**/api/context/projects**", _projects)

    def _runtimes(route):
        q = parse_qs(urlparse(route.request.url).query)
        sid = (q.get("scope_id") or q.get("project_scope_id") or [""])[0]
        runtimes = _RUNTIMES_BY_SCOPE.get(sid, [])
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"project_root": f"/root/{sid or 'cwd'}", "runtimes": runtimes}),
        )

    # The ``**`` tail also matches the ``?target_scope=&scope_id=`` query variant.
    page.route("**/api/context/runtimes**", _runtimes)


def _open_portal(page, mm_web_url: str) -> None:
    page.goto(mm_web_url)
    page.locator("#tabbtn-context-gateway").click()
    page.locator(".settings-nav-btn[data-section='ctx-projects']").click()
    page.wait_for_selector(".ctx-portal-row", timeout=3_000)
    # Heading chips render before the rows in loadCtxProjects, so once a row is
    # present the active-scope chip strip is painted.
    page.wait_for_selector("#ctx-portal-heading-chips .ctx-runtime-chip", timeout=3_000)


def test_heading_chips_reflect_active_scope_state(page, mm_web_url: str) -> None:
    _stub_portal(page)
    _open_portal(page, mm_web_url)

    chips = "#ctx-portal-heading-chips .ctx-runtime-chip"
    # registered (green) / installed-unregistered (yellow) / not-installed (grey).
    assert "ctx-runtime-chip--registered" in (
        page.locator(f"{chips}[data-runtime='claude']").get_attribute("class") or ""
    )
    assert "ctx-runtime-chip--installed" in (
        page.locator(f"{chips}[data-runtime='codex']").get_attribute("class") or ""
    )
    assert "ctx-runtime-chip--greyed" in (
        page.locator(f"{chips}[data-runtime='kimi']").get_attribute("class") or ""
    )

    # Registered chip surfaces its $HOME-collapsed config path in the tooltip.
    claude_title = page.locator(f"{chips}[data-runtime='claude']").get_attribute("title") or ""
    assert "~/.claude.json" in claude_title

    # The not-installed chip is a real <button> (keyboard-reachable install-guide
    # trigger); the error-state chip (antigravity) is a non-interactive <span>.
    assert page.locator("button.ctx-runtime-chip[data-runtime='kimi']").count() == 1
    antigravity = page.locator(f"{chips}[data-runtime='antigravity']")
    assert antigravity.evaluate("el => el.tagName.toLowerCase()") == "span"
    assert "ctx-runtime-chip--greyed" in (antigravity.get_attribute("class") or "")


def test_not_installed_chip_opens_install_guide(page, mm_web_url: str) -> None:
    _stub_portal(page)
    _open_portal(page, mm_web_url)

    page.locator("button.ctx-runtime-chip[data-runtime='kimi']").click()
    page.wait_for_selector("#ctx-install-guide-modal:not([hidden])", timeout=2_000)

    # Title is localized "Install Guide: {runtime}" with the client name folded in,
    # and the body carries the exact registration command from mcp-clients.md.
    assert "Kimi" in (page.locator("#ctx-install-guide-title").inner_text())
    assert "mm init --mcp kimi" in (page.locator("#ctx-install-guide-body").inner_text())

    # Escape closes the guide (the new app.js Escape branch). The [hidden]
    # attribute makes it display:none, so assert the hidden STATE rather than a
    # [hidden] selector under the default visible wait, which can never resolve.
    page.keyboard.press("Escape")
    page.wait_for_selector("#ctx-install-guide-modal", state="hidden", timeout=2_000)

    # Reopen and dismiss via the OK button (the other close path).
    page.locator("button.ctx-runtime-chip[data-runtime='kimi']").click()
    page.wait_for_selector("#ctx-install-guide-modal:not([hidden])", timeout=2_000)
    page.locator("#ctx-install-guide-ok-btn").click()
    page.wait_for_selector("#ctx-install-guide-modal", state="hidden", timeout=2_000)


def test_provider_filter_sets_deeplink_and_filters_rows(page, mm_web_url: str) -> None:
    _stub_portal(page)
    _open_portal(page, mm_web_url)

    # This test exercises the runtime filter over the FULL scope set, including
    # the stale Beta row the default-on "Initialized only" toggle now hides —
    # turn it off so the baseline is all four scopes.
    page.locator("#ctx-portal-hide-uninit").uncheck()
    page.wait_for_function(
        "() => document.querySelectorAll('.ctx-portal-row').length === 4", timeout=2_000
    )

    # All four scopes render with no filter (one is missing but still listed).
    assert page.locator(".ctx-portal-row").count() == 4

    page.locator(".ctx-portal-filter-group button[data-filter='claude']").click()
    # Deep-link deposited (read-only carrier, replaceState).
    page.wait_for_function("() => location.search.includes('runtime=claude')", timeout=2_000)
    # Only scopes where claude is *registered* survive: Server CWD + Alpha.
    page.wait_for_function(
        "() => document.querySelectorAll('.ctx-portal-row').length === 2",
        timeout=2_000,
    )
    assert "active" in (
        page.locator(".ctx-portal-filter-group button[data-filter='claude']").get_attribute("class")
        or ""
    )

    # "All" clears the filter + strips the deep-link.
    page.locator(".ctx-portal-filter-group button[data-filter='all']").click()
    page.wait_for_function("() => !location.search.includes('runtime=')", timeout=2_000)
    page.wait_for_function(
        "() => document.querySelectorAll('.ctx-portal-row').length === 4",
        timeout=2_000,
    )


def test_runtime_deeplink_applies_filter_on_mount(page, mm_web_url: str) -> None:
    _stub_portal(page)
    # Land directly on a ``?runtime=claude`` URL — the filter applies on mount.
    page.goto(f"{mm_web_url}/?runtime=claude")
    page.locator("#tabbtn-context-gateway").click()
    page.locator(".settings-nav-btn[data-section='ctx-projects']").click()
    page.wait_for_selector(".ctx-portal-row", timeout=3_000)

    page.wait_for_function(
        "() => document.querySelectorAll('.ctx-portal-row').length === 2",
        timeout=2_000,
    )
    assert "active" in (
        page.locator(".ctx-portal-filter-group button[data-filter='claude']").get_attribute("class")
        or ""
    )


def test_rows_render_per_cli_traffic_lights(page, mm_web_url: str) -> None:
    _stub_portal(page)
    _open_portal(page, mm_web_url)

    # Every non-missing row gets a 4-dot traffic-light strip; the missing row
    # gets none (its runtimes are never probed).
    alpha = page.locator(".ctx-portal-row[data-scope-id='p-alpha']")
    dots = alpha.locator(".ctx-portal-row-lights .ctx-portal-row-light")
    assert dots.count() == 4
    # Dots map POSITIONALLY to _CTX_PORTAL_RUNTIME_CLIENTS (claude, antigravity,
    # codex, kimi). Alpha has only claude registered, the rest not installed.
    assert "ctx-portal-row-light--registered" in (dots.nth(0).get_attribute("class") or "")
    assert (dots.nth(0).get_attribute("aria-label") or "").startswith("Claude:")
    assert "ctx-portal-row-light--uninstalled" in (dots.nth(3).get_attribute("class") or "")

    # Server CWD row (pinned first): antigravity (index 1) has error_kind, which —
    # by the error-first precedence shared with the heading chip — stays the
    # uninstalled dot state and surfaces the error tooltip, never installed/registered.
    cwd_dots = page.locator(".ctx-portal-row").first.locator(".ctx-portal-row-light")
    assert "ctx-portal-row-light--uninstalled" in (cwd_dots.nth(1).get_attribute("class") or "")
    assert "Antigravity:" in (cwd_dots.nth(1).get_attribute("aria-label") or "")

    ghost = page.locator(".ctx-portal-row[data-scope-id='p-gone']")
    assert ghost.locator(".ctx-portal-row-lights").count() == 0


def test_active_switch_clears_runtime_filter(page, mm_web_url: str) -> None:
    """Switching the active project (the Use button) clears the runtime filter and
    strips the ?runtime= deep-link, repainting chips for the new active scope —
    the new _ctxPortalSetActive behavior, which couples filter state + the
    active-scope-keyed heading chips + the deep-link clear."""
    _stub_portal(page)
    _open_portal(page, mm_web_url)

    # Full scope set incl. the stale Beta row — disable the default-on
    # "Initialized only" toggle so the post-clear baseline is all four scopes.
    page.locator("#ctx-portal-hide-uninit").uncheck()
    page.wait_for_function(
        "() => document.querySelectorAll('.ctx-portal-row').length === 4", timeout=2_000
    )

    page.locator(".ctx-portal-filter-group button[data-filter='claude']").click()
    page.wait_for_function("() => location.search.includes('runtime=claude')", timeout=2_000)
    page.wait_for_function(
        "() => document.querySelectorAll('.ctx-portal-row').length === 2", timeout=2_000
    )

    # Alpha (visible under the claude filter, not active, not missing) carries a Use button.
    page.locator(".ctx-portal-row[data-scope-id='p-alpha'] .ctx-portal-use").click()

    page.wait_for_function("() => !location.search.includes('runtime=')", timeout=2_000)
    page.wait_for_function(
        "() => document.querySelectorAll('.ctx-portal-row').length === 4", timeout=2_000
    )
    assert "active" in (
        page.locator(".ctx-portal-filter-group button[data-filter='all']").get_attribute("class")
        or ""
    )


def test_install_guide_bodies_carry_verbatim_commands(page, mm_web_url: str) -> None:
    """Each per-client guide body must carry the registration command verbatim from
    docs/guides/mcp-clients.md (the CLAUDE.md/STM SoT invariant). Only kimi renders
    as a not-installed chip in the fixture, so drive the other clients directly."""
    _stub_portal(page)
    _open_portal(page, mm_web_url)

    cases = {
        "claude": ["claude mcp add memtomem", "uvx --from memtomem memtomem-server"],
        "antigravity": ["~/.gemini/antigravity-cli/mcp_config.json", "memtomem-server"],
        "codex": ["[mcp_servers.memtomem]", "memtomem-server"],
        "kimi": ["mm init --mcp kimi"],
    }
    for client, needles in cases.items():
        page.evaluate("(c) => window._ctxPortalShowInstallGuide(c)", client)
        page.wait_for_selector("#ctx-install-guide-modal:not([hidden])", timeout=2_000)
        body = page.locator("#ctx-install-guide-body").inner_text()
        for needle in needles:
            assert needle in body, f"{client} guide body missing {needle!r}"
        page.locator("#ctx-install-guide-ok-btn").click()
        page.wait_for_selector("#ctx-install-guide-modal", state="hidden", timeout=2_000)
