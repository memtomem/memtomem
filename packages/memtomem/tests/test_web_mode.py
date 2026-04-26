"""Tests for the web UI mode mechanism (prod / dev tier).

Covers the ``create_app(mode=...)`` factory, the ``MEMTOMEM_WEB__MODE``
env resolver, the ``/api/system/ui-mode`` endpoint, and the drift guards
that keep the HTML ``data-ui-tier`` classification in sync with the
Python ``_PROD_ROUTERS`` / ``_DEV_ONLY_ROUTERS`` lists.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.web.app import (
    _DEV_ONLY_ROUTERS,
    _PROD_ROUTERS,
    _WEB_MODE_ENV,
    create_app,
    resolve_web_mode_from_env,
)


# ---------------------------------------------------------------------------
# Factory + app.state
# ---------------------------------------------------------------------------


def test_create_app_default_mode_is_prod() -> None:
    app = create_app()
    assert app.state.web_mode == "prod"


def test_create_app_dev_mode_propagates() -> None:
    app = create_app(mode="dev")
    assert app.state.web_mode == "dev"


def test_create_app_rejects_invalid_mode() -> None:
    with pytest.raises(ValueError, match="Invalid web mode"):
        create_app(mode="preview")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Env resolver
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [("prod", "prod"), ("dev", "dev"), ("PROD", "prod"), ("  DEV  ", "dev")],
)
def test_resolve_web_mode_accepts_valid(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: str
) -> None:
    monkeypatch.setenv(_WEB_MODE_ENV, raw)
    assert resolve_web_mode_from_env() == expected


def test_resolve_web_mode_unset_returns_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_WEB_MODE_ENV, raising=False)
    assert resolve_web_mode_from_env() == "prod"


def test_resolve_web_mode_strict_rejects_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(_WEB_MODE_ENV, "preview")
    with pytest.raises(ValueError, match="Invalid MEMTOMEM_WEB__MODE"):
        resolve_web_mode_from_env(strict=True)


def test_resolve_web_mode_lenient_falls_back_to_prod(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv(_WEB_MODE_ENV, "preview")
    import logging

    with caplog.at_level(logging.WARNING, logger="memtomem.web.app"):
        assert resolve_web_mode_from_env(strict=False) == "prod"
    assert "Ignoring invalid" in caplog.text
    assert "MEMTOMEM_WEB__MODE" in caplog.text


# ---------------------------------------------------------------------------
# /api/system/ui-mode endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["prod", "dev"])
async def test_ui_mode_endpoint_reflects_app_state(mode: str) -> None:
    """Endpoint echoes ``app.state.web_mode``.

    The endpoint is localhost-guarded (for consistency with other system
    endpoints), so the transport has to spoof the ASGI scope ``client`` as
    a loopback address — the default ``testclient`` host would get a 403.
    """
    app = create_app(mode=mode)  # type: ignore[arg-type]
    transport = ASGITransport(app=app, client=("127.0.0.1", 0))
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/api/system/ui-mode")
    assert resp.status_code == 200
    assert resp.json() == {"mode": mode}


@pytest.mark.asyncio
async def test_ui_mode_endpoint_rejects_non_localhost() -> None:
    """External scanners must not be able to fingerprint dev-mode servers."""
    app = create_app(mode="dev")
    transport = ASGITransport(app=app, client=("203.0.113.7", 0))
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/api/system/ui-mode")
    assert resp.status_code == 403


def test_module_level_app_is_memoized() -> None:
    """Two imports of ``memtomem.web.app.app`` must return the same
    ``FastAPI`` instance. ``__getattr__`` fires on every attribute access
    that isn't already in the module ``__dict__`` — without a singleton
    cache, every caller would get their own router set + state."""
    from memtomem.web import app as app_mod

    # Clear any prior cache so the test is deterministic regardless of
    # import order from the rest of the suite.
    app_mod._app_singleton = None
    first = app_mod.app
    second = app_mod.app
    assert first is second


# ---------------------------------------------------------------------------
# Router classification snapshot (drift guard)
# ---------------------------------------------------------------------------


def test_prod_dev_router_lists_are_disjoint() -> None:
    assert set(_PROD_ROUTERS).isdisjoint(set(_DEV_ONLY_ROUTERS))


def test_dev_only_routers_are_populated() -> None:
    """Classification landed: dev mode must actually extend the prod set."""
    assert _DEV_ONLY_ROUTERS, "_DEV_ONLY_ROUTERS is empty — classification missing"


def _api_paths(app) -> set[str]:
    return {
        getattr(r, "path", "") for r in app.routes if getattr(r, "path", "").startswith("/api/")
    }


def test_dev_routes_extend_prod_routes() -> None:
    prod_paths = _api_paths(create_app(mode="prod"))
    dev_paths = _api_paths(create_app(mode="dev"))
    assert prod_paths < dev_paths, "dev mode must strictly extend prod"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,method",
    [
        ("/api/sessions", "GET"),
        ("/api/scratch", "GET"),
        ("/api/namespaces", "GET"),
        ("/api/procedures", "GET"),
        ("/api/watchdog/status", "GET"),
        ("/api/settings-sync", "GET"),
        ("/api/eval", "GET"),
    ],
)
async def test_dev_only_routes_blocked_in_prod_but_exposed_in_dev(path: str, method: str) -> None:
    """Spot-check a representative dev-only endpoint — prod must not expose
    it, dev must. Asserting both sides catches the bug where a parametrize
    entry with a typo (or wrong method) would trivially "pass" in prod
    because the route doesn't exist in either mode. Route-level filtering
    is the security boundary; the SPA's ``data-ui-tier`` hiding is UX.

    The prod check uses real HTTP so we know the 404 comes from the
    catch-all handler, not route-handler failure. The dev check reads the
    registered ``app.routes`` set directly — this avoids having to wire
    ``app.state.storage`` etc. for every dev-only router just to prove
    the path got mounted."""
    prod_app = create_app(mode="prod")
    dev_app = create_app(mode="dev")

    dev_paths = {getattr(r, "path", "") for r in dev_app.routes}
    assert path in dev_paths, f"{method} {path} is missing in dev too — parametrize entry is wrong"

    async with AsyncClient(
        transport=ASGITransport(app=prod_app, client=("127.0.0.1", 0)),
        base_url="http://testserver",
    ) as c:
        prod_resp = await c.request(method, path)
    assert prod_resp.status_code == 404, (
        f"{method} {path} is still reachable in prod: {prod_resp.status_code}"
    )


def test_prod_keeps_polished_routes_mounted() -> None:
    """Sanity: the dev-only move mustn't brick the polished surface."""
    prod_paths = _api_paths(create_app(mode="prod"))
    for expected in (
        "/api/search",
        "/api/sources",
        "/api/stats",
        "/api/config",
        "/api/context/overview",
        "/api/context/skills",
        "/api/context/commands",
        "/api/context/agents",
    ):
        assert expected in prod_paths, (
            f"{expected} is missing from prod — reclassify or the router list"
        )


# ---------------------------------------------------------------------------
# SPA markup / JS source pins
# ---------------------------------------------------------------------------

_STATIC = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "web" / "static"


def _read_static(name: str) -> str:
    return (_STATIC / name).read_text(encoding="utf-8")


def test_html_main_tabs_all_carry_ui_tier_attr() -> None:
    html = _read_static("index.html")
    tab_buttons = re.findall(r'<button[^>]*class="tab-btn[^"]*"[^>]*>', html)
    assert tab_buttons, "no tab-btn elements found — markup drift"
    for tag in tab_buttons:
        assert "data-ui-tier=" in tag, f"tab-btn missing data-ui-tier: {tag[:120]}"


def test_html_settings_nav_btns_all_carry_ui_tier_attr() -> None:
    html = _read_static("index.html")
    settings_buttons = re.findall(r'<button[^>]*class="settings-nav-btn[^"]*"[^>]*>', html)
    assert settings_buttons, "no settings-nav-btn elements found — markup drift"
    for tag in settings_buttons:
        assert "data-ui-tier=" in tag, f"settings-nav-btn missing data-ui-tier: {tag[:120]}"


def test_html_dev_mode_banner_is_present_and_starts_hidden() -> None:
    html = _read_static("index.html")
    assert 'id="dev-mode-banner"' in html, "dev-mode banner removed from markup"
    banner_tag = re.search(r'<div[^>]*id="dev-mode-banner"[^>]*>', html)
    assert banner_tag is not None
    assert "hidden" in banner_tag.group(0), (
        "dev-mode banner must start hidden; JS reveals it in dev mode"
    )


def test_app_js_pins_ui_mode_default_and_toast_copy() -> None:
    """JS grep pin — the test suite has no JS runtime. A source scan catches
    regressions in the three behaviors we rely on."""
    js = _read_static("app.js")
    # STATE.uiMode default must stay 'prod' so fetch failures degrade to the
    # polished surface rather than exposing dev pages.
    assert re.search(r"uiMode:\s*'prod'", js), "STATE.uiMode early default changed"
    # Hash-fallback / settings-section redirect toast — routed through i18n.
    assert "toast.dev_only_section" in js, "dev-only redirect toast key missing"
    # Home dashboard must gate dev-only endpoints behind the mode check so
    # prod users don't see guaranteed 404s on every Home render.
    assert "const devMode = STATE.uiMode === 'dev'" in js, (
        "Home dashboard lost its dev-only fetch gate"
    )
    # The Context Gateway (Artifact Sync) tab graduated to prod, but the
    # settings_sync router stays dev-only — so the "Sync All" button and
    # the overview's settings card must self-gate, otherwise prod users
    # would see "Settings sync failed" after a successful artifact fanout.
    cg_js = _read_static("context-gateway.js")
    assert "STATE.uiMode === 'dev'" in cg_js, (
        "context-gateway.js lost the dev-only gate around settings_sync"
    )
    # And the locale entries themselves are pinned so a rename doesn't go
    # unnoticed by the i18n completeness check.
    en = _read_static("locales/en.json")
    ko = _read_static("locales/ko.json")
    assert '"toast.dev_only_section"' in en
    assert '"toast.dev_only_section"' in ko


def test_html_main_tabs_all_stay_prod() -> None:
    """Main top-nav tabs (Home / Search / Sources / Index / Tags / Timeline /
    Settings) should all be prod today. Flipping a main tab to dev would be
    a large UX decision — if it ever happens, update this assertion to an
    explicit expected set so the intent is reviewable."""
    html = _read_static("index.html")
    dev_tabs = set(re.findall(r'data-ui-tier="dev"\s+data-tab="([^"]+)"', html))
    assert dev_tabs == set(), (
        f"Main tabs should all be prod; found dev: {dev_tabs}. "
        "If intentional, replace this assertion with an explicit expected set."
    )


def test_html_classification_matches_router_lists() -> None:
    """HTML ``data-ui-tier`` values must agree with the Python router lists
    — drift between the two would hide/show a tab whose route disagrees,
    breaking `mm web --dev` discovery or producing phantom prod 404s."""
    html = _read_static("index.html")
    dev_sections = set(re.findall(r'data-ui-tier="dev"\s+data-section="([^"]+)"', html))
    # Expected dev sections derived from _DEV_ONLY_ROUTERS + naming (SPA
    # section id != router module). Source of truth: whoever edits the
    # router lists must also update the HTML, and this test enforces it.
    expected_dev = {
        "namespaces",
        "hooks-sync",
        "harness-sessions",
        "harness-scratch",
        "harness-procedures",
        "harness-health",
    }
    assert dev_sections == expected_dev, (
        f"HTML dev-tier sections drifted from expected set. "
        f"Only in HTML: {dev_sections - expected_dev}. "
        f"Missing: {expected_dev - dev_sections}."
    )
