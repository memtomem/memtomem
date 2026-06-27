"""Tests for the web UI mode mechanism (prod / dev tier).

Covers the ``create_app(mode=...)`` factory, the ``MEMTOMEM_WEB__MODE``
env resolver, the ``/api/system/ui-mode`` endpoint, and the drift guards
that keep the HTML ``data-ui-tier`` classification in sync with the
Python ``_PROD_ROUTERS`` / ``_DEV_ONLY_ROUTERS`` lists.
"""

from __future__ import annotations

import json
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


def _iter_api_routes(app):
    """Yield ``(path, methods)`` for every *mounted* route, flattening the
    router-inclusion tree.

    This is the one helper that knows fastapi's route-container shape so the
    tier/mode drift guards below don't have to. Through fastapi 0.136
    ``app.routes`` is a flat list of ``APIRoute``s, each carrying its full
    ``.path`` and ``.methods``. fastapi 0.137 turned ``include_router`` into a
    tree of internal ``_IncludedRouter`` nodes whose leaves are reached via
    ``effective_candidates()`` — included routes no longer appear as flat
    ``APIRoute``s (FastAPI's docs now call ``router.routes`` an internal
    detail). We duck-type both shapes and keep walking the *actual* registered
    routes — including ``include_in_schema=False`` ones such as the ``/api``
    catch-all — rather than the narrower OpenAPI projection (which would also
    drop a hidden tiered route silently).
    """

    def walk(routes):
        for route in routes:
            candidates = getattr(route, "effective_candidates", None)
            if callable(candidates):  # fastapi>=0.137 _IncludedRouter node
                yield from walk(candidates())
                continue
            path = getattr(route, "path", None)
            if isinstance(path, str):
                yield path, set(getattr(route, "methods", None) or ())

    yield from walk(app.routes)


def _api_routes(app) -> dict[str, set[str]]:
    """Map each ``/api/`` path to the union of HTTP methods registered on it."""
    out: dict[str, set[str]] = {}
    for path, methods in _iter_api_routes(app):
        if path.startswith("/api/"):
            out.setdefault(path, set()).update(methods)
    return out


def _api_paths(app) -> set[str]:
    return set(_api_routes(app))


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
        ("/api/procedures", "GET"),
        ("/api/watchdog/status", "GET"),
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
    mounted-route set (see ``_iter_api_routes``) — this avoids having to wire
    ``app.state.storage`` etc. for every dev-only router just to prove
    the path got mounted."""
    prod_app = create_app(mode="prod")
    dev_app = create_app(mode="dev")

    dev_paths = _api_paths(dev_app)
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
        "/api/context/mcp-servers",
        "/api/context/settings",
        # ADR-0008 PR-E: the read-only wiki browser is prod-tier (no dev gate).
        "/api/wiki",
        "/api/wiki/{asset_type}/{name}/diff",
        "/api/wiki/{asset_type}/{name}/lint",
    ):
        assert expected in prod_paths, (
            f"{expected} is missing from prod — reclassify or the router list"
        )


@pytest.mark.asyncio
async def test_namespaces_list_is_prod_mounted_but_admin_routes_blocked() -> None:
    """ADR-0007 PR-A: the read endpoint and the cosmetic PATCH (color,
    description) live on the prod tier; the structural admin surface
    (GET-by-id, rename, delete) stays dev-only. Cosmetic edit was
    promoted because it doesn't migrate chunks; rename/delete need
    chunk-id stability design (ADR-0005 follow-up) before promotion.

    Backed by ``namespaces_read.router`` in _PROD_ROUTERS (GET list +
    PATCH metadata) and ``namespaces.admin_router`` in
    _DEV_ONLY_ROUTERS (GET info + rename + delete).
    """
    prod_app = create_app(mode="prod")
    prod_paths = _api_paths(prod_app)
    assert "/api/namespaces" in prod_paths, (
        "GET /api/namespaces must be prod-mounted via namespaces_read"
    )

    # Path-level pin: PATCH /api/namespaces/{namespace} is prod (cosmetic
    # edit). Rename and delete share the same path template but live on
    # admin_router, so a path string match alone isn't enough — verify
    # the methods registered for the path.
    patch_methods = _api_routes(prod_app).get("/api/namespaces/{namespace}", set())
    assert "PATCH" in patch_methods, (
        "PATCH /api/namespaces/{namespace} must be prod-mounted via namespaces_read"
    )
    assert "GET" not in patch_methods, (
        "GET /api/namespaces/{namespace} (info) must stay dev-only on admin_router"
    )
    assert "DELETE" not in patch_methods, (
        "DELETE /api/namespaces/{namespace} must stay dev-only on admin_router"
    )

    async with AsyncClient(
        transport=ASGITransport(app=prod_app, client=("127.0.0.1", 0)),
        base_url="http://testserver",
    ) as c:
        # Structural verbs must stay dev-only — these all live on
        # namespaces.admin_router and 404 cleanly without a storage mock.
        for method, path in (
            ("GET", "/api/namespaces/foo"),
            ("POST", "/api/namespaces/foo/rename"),
            ("DELETE", "/api/namespaces/foo"),
        ):
            resp = await c.request(method, path)
            assert resp.status_code == 404, (
                f"{method} {path} leaked into prod: {resp.status_code} "
                "(admin surface must stay dev-only)"
            )


def test_namespaces_list_remains_reachable_in_dev() -> None:
    """The router split must not break the dev path: dev mode mounts both
    ``namespaces_read`` (read.router via _PROD_ROUTERS) and ``namespaces``
    (admin_router via _DEV_ONLY_ROUTERS). Only the read router registers
    ``GET ""`` — re-decorating ``list_namespaces`` on admin_router would
    surface as a duplicate registration (FastAPI accepts it via
    first-match-wins, but the OpenAPI docs would show it twice and the
    dead second registration is a code smell).
    """
    dev_app = create_app(mode="dev")

    # Duplicate-registration guard: re-decorating ``list_namespaces`` on
    # admin_router (or registering any second handler) would mount a second
    # ``GET /api/namespaces``. Count the actual mounted routes —
    # ``_iter_api_routes`` flattens the fastapi>=0.137 inclusion tree — so the
    # invariant holds whether or not the duplicate shares an operation id (the
    # OpenAPI surface would collapse two different handlers into one slot).
    list_handlers = [
        (p, m) for p, m in _iter_api_routes(dev_app) if p == "/api/namespaces" and "GET" in m
    ]
    assert len(list_handlers) == 1, (
        f"Expected exactly one GET /api/namespaces handler in dev; "
        f"found {len(list_handlers)} — admin_router accidentally re-registered the list?"
    )

    dev_paths = _api_paths(dev_app)
    for expected in (
        "/api/namespaces",
        "/api/namespaces/{namespace}",
        "/api/namespaces/{namespace}/rename",
    ):
        assert expected in dev_paths, f"{expected} missing from dev — split broke the admin surface"


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


def test_ctx_overview_has_landing_modifier_for_group_dashboard() -> None:
    """ctx-overview is the Agent Integrations group's dashboard card and must
    carry the ``settings-nav-btn--landing`` modifier so CSS gives it visual
    hierarchy distinct from the leaf rows.

    rank 2/20: the Overview is now the aggregate dashboard (sync status +
    Sync-All + cross-project tiles, no per-project roster) and the cold-visit
    landing, so the ``--landing`` modifier moved here from ctx-projects.

    Post-#962 the Agent Integrations sidebar lives under the top-level
    ``#tab-context-gateway`` panel rather than nested in Settings. The
    landing-modifier contract moves with the button — assert both the
    class and the new tab ancestry.
    """
    html = _read_static("index.html")
    overview_btn = re.search(r'<button[^>]*data-section="ctx-overview"[^>]*>', html)
    assert overview_btn is not None, "ctx-overview button not found in markup"
    assert "settings-nav-btn--landing" in overview_btn.group(0), (
        "ctx-overview must carry settings-nav-btn--landing modifier "
        "(group dashboard, not a leaf); "
        f"got: {overview_btn.group(0)[:200]}"
    )
    # Anchor the button inside the new Gateway tab. ``#tab-context-gateway``
    # comes BEFORE the ctx-overview match site if the move was applied
    # correctly; a regression that re-nests the Gateway under Settings
    # would put ``#tab-settings`` between them.
    head = html[: overview_btn.start()]
    gateway_idx = head.rfind('id="tab-context-gateway"')
    settings_idx = head.rfind('id="tab-settings"')
    assert gateway_idx >= 0, (
        "ctx-overview button is not under #tab-context-gateway — promotion regressed (#962)."
    )
    assert gateway_idx > settings_idx, (
        "ctx-overview must live under #tab-context-gateway, but its closest"
        " ancestor tab id is #tab-settings — restructure regressed (#962)."
    )


def test_other_integration_leaves_lack_landing_modifier() -> None:
    """Symmetric negative pin: only ctx-overview is the landing card. The
    other Agent Integrations leaves (Projects / Skills / Custom Commands / Subagents /
    MCP Servers / Hooks) must not carry the ``--landing`` modifier, otherwise the visual
    hierarchy collapses again.

    Also pins that each leaf lives under ``#tab-context-gateway`` so a
    partial revert doesn't silently leave the section back under Settings.
    """
    html = _read_static("index.html")
    for section in (
        "ctx-projects",
        "ctx-skills",
        "ctx-commands",
        "ctx-agents",
        "ctx-mcp-servers",
        "ctx-wiki",
        "hooks-sync",
    ):
        tag = re.search(rf'<button[^>]*data-section="{section}"[^>]*>', html)
        assert tag is not None, f"{section} button not found in markup"
        assert "settings-nav-btn--landing" not in tag.group(0), (
            f"{section} must NOT carry --landing modifier "
            "(reserved for ctx-overview only); "
            f"got: {tag.group(0)[:200]}"
        )
        head = html[: tag.start()]
        gateway_idx = head.rfind('id="tab-context-gateway"')
        settings_idx = head.rfind('id="tab-settings"')
        assert gateway_idx > settings_idx, (
            f"{section} must live under #tab-context-gateway post-#962, not #tab-settings."
        )


def test_gateway_main_tab_button_exists() -> None:
    """#962: Context Gateway is promoted to a top-level tab. The button
    sits between Sources and Index in the main nav and uses the
    ``data-tab="context-gateway"`` hash-driven activation contract.
    """
    html = _read_static("index.html")
    btn = re.search(
        r'<button[^>]*id="tabbtn-context-gateway"[^>]*>',
        html,
    )
    assert btn is not None, "tabbtn-context-gateway button missing"
    tag = btn.group(0)
    assert 'data-tab="context-gateway"' in tag, (
        f"tabbtn-context-gateway must use data-tab='context-gateway', got: {tag}"
    )
    assert 'data-i18n="nav.context_gateway"' in tag, (
        f"tabbtn-context-gateway must use nav.context_gateway i18n key, got: {tag}"
    )
    # Positional check — the button must be after Sources and before Index
    # so it sits visually next to the related Sources/Index lane.
    sources_idx = html.find('id="tabbtn-sources"')
    gateway_idx = html.find('id="tabbtn-context-gateway"')
    index_idx = html.find('id="tabbtn-index"')
    assert sources_idx < gateway_idx < index_idx, (
        "Gateway tab button must sit between Sources and Index in the main nav"
    )


def test_shortcut_switch_tabs_copy_matches_tab_count() -> None:
    """#962 review P3 fold: the keyboard-shortcuts help row claims
    digits 1-N map to the main tabs. The Gateway tab promotion
    bumped N from 7 to 8 (Home/Search/Sources/Gateway/Index/Tags/
    Timeline/Settings). Pin both the digit range row and the per-locale
    ``shortcut.switch_tabs`` copy so a future tab add/remove can't
    silently leave a stale digit there.
    """
    html = _read_static("index.html")
    en = json.loads(_read_static("locales/en.json"))
    ko = json.loads(_read_static("locales/ko.json"))

    # Count the main-nav tab buttons. The shortcut row's digit range
    # must match the visible tab count so users never see a
    # number that doesn't actually activate anything.
    main_tab_buttons = re.findall(
        r'<button[^>]*class="tab-btn[^"]*"[^>]*data-tab="([^"]+)"',
        html,
    )
    assert len(main_tab_buttons) == 8, (
        f"Expected 8 top-level tabs after #962 Gateway promotion; got "
        f"{len(main_tab_buttons)}: {main_tab_buttons}"
    )

    # Help row literal (rendered fallback before i18n applies).
    row = re.search(
        r"<kbd>1</kbd>[^<]*<kbd>([0-9]+)</kbd>",
        html,
    )
    assert row is not None, "shortcut help row missing in markup"
    assert row.group(1) == "8", (
        f"Help row digit range must end at 8 (one per top-level tab); "
        f"got: <kbd>1</kbd>-<kbd>{row.group(1)}</kbd>"
    )

    for locale_name, locale in (("en", en), ("ko", ko)):
        copy = locale.get("shortcut.switch_tabs", "")
        assert "8" in copy and "7" not in copy, (
            f"{locale_name}.json shortcut.switch_tabs must mention the new "
            f"tab count of 8 (and not retain the stale 7); got: {copy!r}"
        )


def test_gateway_appears_in_default_tab_selector() -> None:
    """#962 review P3 fold: the Settings modal's ``#settings-default-tab``
    dropdown must include the new Gateway tab so users can pick it as
    their landing tab. Without this entry the Gateway is a top-level
    tab the user can navigate to but never default to.
    """
    html = _read_static("index.html")
    select_match = re.search(
        r'<select[^>]*id="settings-default-tab"[^>]*>(.*?)</select>',
        html,
        re.DOTALL,
    )
    assert select_match is not None, "#settings-default-tab select missing"
    select_body = select_match.group(1)
    assert 'value="context-gateway"' in select_body, (
        "#settings-default-tab must offer context-gateway as a selectable "
        f"default; got: {select_body!r}"
    )


def test_settings_sidebar_no_longer_holds_gateway_buttons() -> None:
    """#962 negative pin: after the promotion, none of the Gateway
    sections may still be reachable from the Settings sidebar. A
    regression that left a stale settings-nav-btn under ``#tab-settings``
    would re-create the duplicate-entry confusion the move was meant to
    eliminate.
    """
    html = _read_static("index.html")
    settings_open = html.find('id="tab-settings"')
    settings_close = html.find('id="tab-context-gateway"')
    # Settings tab ends before Gateway tab begins (Settings is below in
    # the file — this guards against the layout being flipped). Use
    # rfind to locate the actual Settings panel close instead.
    if settings_close < settings_open:
        # Layout where Gateway sits above Settings — fall back to a
        # bounded slice around Settings only.
        settings_close = len(html)
    settings_slice = html[settings_open:settings_close]
    for section in (
        "ctx-overview",
        "ctx-skills",
        "ctx-commands",
        "ctx-agents",
        "ctx-mcp-servers",
        "hooks-sync",
    ):
        assert f'data-section="{section}"' not in settings_slice, (
            f"Settings sidebar must not retain {section} button after #962 Gateway promotion."
        )


def test_html_dev_mode_banner_is_present_and_starts_hidden() -> None:
    html = _read_static("index.html")
    assert 'id="dev-mode-banner"' in html, "dev-mode banner removed from markup"
    banner_tag = re.search(r'<div[^>]*id="dev-mode-banner"[^>]*>', html)
    assert banner_tag is not None
    assert "hidden" in banner_tag.group(0), (
        "dev-mode banner must start hidden; JS reveals it in dev mode"
    )


def test_app_js_pins_compose_privacy_warning() -> None:
    """JS-source pin for the compose-mode privacy warning (#580).

    The test suite has no JS runtime, so we grep ``app.js`` for the
    wiring that the integration test would otherwise verify:

    - the boot-time fetch site exists,
    - the cache field on STATE is populated from that fetch,
    - regex objects are constructed from the documented
      ``{pattern, flags}`` shape (a future refactor that drops the
      flags arg would silently make ``(?i)``-lifted patterns
      case-sensitive),
    - the i18n key the confirm dialog reads is present.

    This pin covers wiring only; behaviour parity (Python ``re`` of
    translated body+flags == original pattern) is in
    ``test_privacy.py:TestJsPatternTranslation``.
    """
    js = _read_static("app.js")
    assert "'/api/privacy/patterns'" in js, "privacy patterns fetch site missing"
    assert "STATE.privacyPatterns" in js, "STATE cache field for privacy patterns missing"
    assert "compose.privacy_warning_title" in js, "compose privacy i18n key not wired"
    # Pattern-and-flags constructor — locks the {pattern, flags} shape
    # so a future refactor that drops the flags argument fails this
    # pin instead of silently demoting case-insensitive matches.
    assert "new RegExp(pattern, flags)" in js, (
        "RegExp constructor must use both pattern and flags from the wire shape"
    )
    en = _read_static("locales/en.json")
    ko = _read_static("locales/ko.json")
    assert '"compose.privacy_warning_title"' in en
    assert '"compose.privacy_warning_title"' in ko


def test_app_js_pins_ui_mode_default_and_toast_copy() -> None:
    """JS grep pin — the test suite has no JS runtime. A source scan catches
    regressions in the three behaviors we rely on."""
    js = _read_static("app.js")
    # STATE.uiMode default must stay 'prod' so fetch failures degrade to the
    # polished surface rather than exposing dev pages.
    assert re.search(r"uiMode:\s*'prod'", js), "STATE.uiMode early default changed"
    # Hash-fallback / settings-section redirect toast — routed through i18n.
    assert "toast.dev_only_section" in js, "dev-only redirect toast key missing"
    # Home dashboard must gate the dev-only sessions+scratch fetches behind
    # the mode check so prod users don't see guaranteed 404s on every Home
    # render. The namespaces list endpoint graduated to prod via
    # namespaces_read (#582 4.10a) so it no longer needs a gate.
    assert "if (STATE.uiMode === 'dev')" in js, (
        "Home dashboard lost its dev-only sessions+scratch fetch gate"
    )
    html = _read_static("index.html")
    assert 'id="home-sessions">—</div>' in html
    assert 'id="home-scratch">—</div>' in html
    assert re.search(
        r'<div class="stat-card card" data-ui-tier="dev" hidden>\s*'
        r'<div class="stat-value" id="home-sessions">',
        html,
    ), "Home Sessions card must stay dev-only and hidden by default"
    assert re.search(
        r'<div class="stat-card card" data-ui-tier="dev" hidden>\s*'
        r'<div class="stat-value" id="home-scratch">',
        html,
    ), "Home Working Memory card must stay dev-only and hidden by default"
    css = _read_static("style.css")
    assert ".home-stats-row { display: grid; grid-template-columns: repeat(4, 1fr);" in css
    assert "body.dev-mode .home-stats-row { grid-template-columns: repeat(6, 1fr); }" in css
    # The Context Gateway tab is fully prod — Skills / Custom Commands /
    # Subagents / Hooks all live on the same surface and all render in
    # prod ``mm web``. Custom Commands was previously dev-tier (PR #813)
    # pending an external Anthropic deprecation signal on
    # ``.claude/commands/``; that gate has been removed and the surface
    # is now treated like the other Context Gateway leaves. Pin the
    # absence of any client-side ``STATE.uiMode`` gate in
    # ``context-gateway.js`` so a future refactor doesn't silently
    # reintroduce a tier split that hides artifacts from prod users.
    cg_js = _read_static("context-gateway.js")
    eq_count = cg_js.count("STATE.uiMode === 'dev'")
    ne_count = cg_js.count("STATE.uiMode !== 'dev'")
    assert (eq_count, ne_count) == (0, 0), (
        f"context-gateway.js must not gate any surface by ``STATE.uiMode``; "
        f"found {eq_count} ``=== 'dev'`` and {ne_count} ``!== 'dev'`` site(s). "
        "Custom Commands is no longer dev-tier — adding a new tier gate here "
        "would hide an artifact category from prod users."
    )
    assert "devOnly" not in cg_js, (
        "context-gateway.js must not reintroduce a ``devOnly`` flag on the "
        "overview-tile types list; Custom Commands is prod now."
    )
    assert "const chips = runtimes.map" in cg_js, (
        "Context Gateway runtime tags must render from detected_runtimes."
    )
    assert "ctx-overview-runtimes" in cg_js, (
        "Context Gateway runtime tags disappeared from the prod overview header."
    )
    # rank 11 hoisted the active-project + tier controls out of the overview
    # body into the shared ``#ctx-control-bar`` header, so the old
    # ``html += _ctxTierControls('overview')`` marker is gone — bound the
    # runtime-chips region on the grid open instead (the next thing emitted
    # after the header block).
    runtime_block = cg_js[
        cg_js.find("const runtimes = Array.isArray(data.detected_runtimes)") : cg_js.find(
            "html += '<div class=\"ctx-overview-grid\">'"
        )
    ]
    assert "STATE.uiMode" not in runtime_block, (
        "Context Gateway runtime tags must not be dev-mode gated; prod users "
        "need the claude/gemini/codex detection chips too."
    )
    hooks_js = _read_static("settings-hooks-watchdog.js")
    assert "_hooksScopedUrl('/api/settings-sync')" in hooks_js
    assert "_hooksScopedUrl('/api/context/settings/resolve')" in hooks_js
    # rank 11: the Hooks tier control moved to the shared gateway header bar;
    # loadHooksSync now paints it via _ctxRenderControlBar() (which self-sources
    # the active section) instead of emitting _ctxTierControls inline.
    assert "_ctxRenderControlBar()" in hooks_js
    assert "let _hooksSyncSeq = 0;" in hooks_js
    assert "const requestedScope = _hooksCurrentTargetScope();" in hooks_js
    assert "seq !== _hooksSyncSeq" in hooks_js
    assert "|| requestedScope !== _hooksCurrentTargetScope()" in hooks_js
    css = _read_static("style.css")
    assert (
        ".badge-success" in css
        and "background:" in css.split(".badge-success", 1)[1].split("}", 1)[0]
    ), "Runtime tags use badge-success for detected runtimes; it must define a background."
    assert ".badge-warning" in css and ".badge-muted" in css
    assert ".hooks-rule-detail-header" in css and ".hooks-rule-detail-inner" in css, (
        "Hooks per-rule detail should render as a card with a header and framed body."
    )
    # And the locale entries themselves are pinned so a rename doesn't go
    # unnoticed by the i18n completeness check.
    en = _read_static("locales/en.json")
    ko = _read_static("locales/ko.json")
    assert '"toast.dev_only_section"' in en
    assert '"toast.dev_only_section"' in ko


def test_indexing_guard_rechecks_server_before_blocking_retry() -> None:
    """A stale client-side indexing flag must not permanently block retry.

    Source-tab SSE failures can leave users looking at a model-readiness error
    banner while the server is already idle. The next reindex click should
    confirm ``/api/indexing/active`` before showing "already in progress".
    """
    app_js = _read_static("app.js")
    sources_js = _read_static("sources-memory-dirs.js")

    assert "async function _indexingTryStartOrRefresh()" in app_js
    assert "api('GET', '/api/indexing/active')" in app_js
    assert "_indexingEnd();\n      return _indexingTryStart();" in app_js
    assert "await _indexingTryStartOrRefresh()" in app_js
    assert sources_js.count("await _indexingTryStartOrRefresh()") >= 2


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
    breaking `mm web --dev` discovery or producing phantom prod 404s.

    One direction is deliberately allowed: a section can be dev-tier in the
    HTML while its router stays in ``_PROD_ROUTERS`` (UI hides what the
    backend still serves). That's the deprecation-transition shape — see
    ``ctx-commands`` below. The dangerous direction (UI shows a section
    whose router is dev-only, producing 404s in prod) is what this test
    actually defends against.
    """
    html = _read_static("index.html")
    dev_sections = set(re.findall(r'data-ui-tier="dev"\s+data-section="([^"]+)"', html))
    # Expected dev sections derived from _DEV_ONLY_ROUTERS + naming (SPA
    # section id != router module). Source of truth: whoever edits the
    # router lists must also update the HTML, and this test enforces it.
    expected_dev = {
        # ADR-0007 PR-A promoted Settings → Namespaces to prod (cosmetic
        # CRUD only). Rename and delete buttons inside the panel are
        # dev-gated in JS (settings-namespaces.js _buildNsCard) because
        # their backend verbs stay on admin_router; this HTML check tracks
        # the section visibility, not the per-button gating.
        # ``hooks-sync`` graduated to prod via RFC #761 (ADR-0001 §5
        # readiness criteria); the section's data-ui-tier was flipped
        # together with the router move.
        # ``ctx-commands`` was previously dev-tier (UI-only demote
        # pending Anthropic's deprecation signal on ``.claude/commands/``)
        # and has since been promoted to prod alongside Skills/Subagents
        # — it now lives in the normal Context Gateway surface and is no
        # longer expected to appear here.
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
