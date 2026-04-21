"""Tests for the web UI mode mechanism (prod / dev tier).

PR 1 introduces the plumbing — ``create_app(mode=...)``, ``/api/system/ui-mode``,
and SPA filtering — without moving any page into the dev-only tier yet.
The route set between prod and dev is therefore identical in this PR; PR 2
is where the classification change lands and this snapshot will diverge.
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


def test_pr1_leaves_dev_only_routers_empty() -> None:
    """PR 1 is mechanism-only: no classification changes yet. When PR 2
    populates ``_DEV_ONLY_ROUTERS`` this test must be updated alongside."""
    assert _DEV_ONLY_ROUTERS == []


def test_route_counts_match_in_pr1_snapshot() -> None:
    """With classification unchanged, prod and dev mount identical routes."""

    def api_paths(app) -> set[str]:
        return {
            getattr(r, "path", "") for r in app.routes if getattr(r, "path", "").startswith("/api/")
        }

    assert api_paths(create_app(mode="prod")) == api_paths(create_app(mode="dev"))


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
    regressions in the two behaviors we rely on."""
    js = _read_static("app.js")
    # STATE.uiMode default must stay 'prod' so fetch failures degrade to the
    # polished surface rather than exposing dev pages.
    assert re.search(r"uiMode:\s*'prod'", js), "STATE.uiMode early default changed"
    # Hash-fallback / settings-section redirect toast — routed through i18n.
    assert "toast.dev_only_section" in js, "dev-only redirect toast key missing"
    # And the locale entries themselves are pinned so a rename doesn't go
    # unnoticed by the i18n completeness check.
    en = _read_static("locales/en.json")
    ko = _read_static("locales/ko.json")
    assert '"toast.dev_only_section"' in en
    assert '"toast.dev_only_section"' in ko
