"""Regression guards for the locally vendored Swagger UI.

The FastAPI default ``docs_url="/api/docs"`` page loads
``swagger-ui-bundle.js`` and ``swagger-ui.css`` from
``cdn.jsdelivr.net`` on every render. ``create_app`` overrides that with
a custom ``/api/docs`` route built around ``get_swagger_ui_html``,
pointing at the vendored bundle under ``web/static/vendor/swagger/``.

The tests below pin both halves with paired positive + negative
assertions so a future "let's just allow-list jsdelivr" or "bring back
the FastAPI default" change fails loudly. Pattern reference:
``feedback_pin_invert_symmetric_assertion.md`` — a negative-only check
would false-pass if the route were dropped entirely.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.web.app import create_app

_SWAGGER_ASSETS = [
    ("vendor/swagger/swagger-ui-bundle.js", "application/javascript"),
    ("vendor/swagger/swagger-ui.css", "text/css"),
    ("vendor/swagger/swagger-init.js", "application/javascript"),
]


@pytest.mark.asyncio
async def test_api_docs_serves_vendored_swagger_ui_not_jsdelivr() -> None:
    """``/api/docs`` HTML references the local /vendor/swagger paths only.

    Positive markers (``/vendor/swagger/swagger-ui-bundle.js``,
    ``/vendor/swagger/swagger-ui.css``) confirm the override is wired and
    the page actually points at the vendored bundle. Negative markers
    block the regression where someone reverts to the FastAPI default
    (``cdn.jsdelivr.net``) — a negative-only check would false-pass if
    the route returned 404.
    """
    app = create_app()
    transport = ASGITransport(app=app, client=("127.0.0.1", 0))
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/api/docs")
    assert resp.status_code == 200
    html = resp.text

    # positive — the override is in place and pointing at the local bundle
    assert "/vendor/swagger/swagger-ui-bundle.js" in html
    assert "/vendor/swagger/swagger-ui.css" in html
    # The hand-rolled HTML moves the Swagger UI bootstrap to an external
    # /vendor/swagger/swagger-init.js so the CSP can stay on
    # ``script-src 'self'`` (the FastAPI default helper inlines it,
    # which the strict CSP would block).
    assert "/vendor/swagger/swagger-init.js" in html
    # The custom HTML also reuses the SPA favicon — pin so a regression
    # to the FastAPI default (``fastapi.tiangolo.com/img/favicon.png``,
    # which ``img-src 'self' data:`` blocks) fails here loudly.
    assert 'href="/favicon.svg"' in html

    # negative — guard against regression to the FastAPI/jsdelivr default
    assert "cdn.jsdelivr.net" not in html
    assert "swagger-ui-dist@" not in html
    assert "cdnjs.cloudflare.com" not in html
    assert "unpkg.com" not in html


@pytest.mark.asyncio
@pytest.mark.parametrize("asset,_ctype", _SWAGGER_ASSETS)
async def test_swagger_vendor_asset_served_locally(asset: str, _ctype: str) -> None:
    """Each vendored Swagger UI asset is reachable at ``/<asset>``."""
    app = create_app()
    transport = ASGITransport(app=app, client=("127.0.0.1", 0))
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get(f"/{asset}")
    assert resp.status_code == 200
    assert len(resp.content) > 0


@pytest.mark.asyncio
async def test_redoc_default_route_disabled() -> None:
    """The FastAPI default ``/api/redoc`` page must stay disabled.

    ReDoc duplicates Swagger UI's purpose and pulls
    ``redoc.standalone.js`` from jsdelivr by default — leaving it on
    would re-introduce the same offline / privacy footgun this PR set
    out to fix. The 404 catch-all under ``/api/{path:path}`` is the
    expected response.
    """
    app = create_app()
    transport = ASGITransport(app=app, client=("127.0.0.1", 0))
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/api/redoc")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_openapi_json_still_served() -> None:
    """The custom ``/api/docs`` page is useless without the OpenAPI spec.

    FastAPI auto-mounts ``app.openapi_url`` (default ``/openapi.json``).
    Pin the path so a future cleanup that disables ``openapi_url=None``
    breaks loudly here instead of in the user's browser.
    """
    app = create_app()
    transport = ASGITransport(app=app, client=("127.0.0.1", 0))
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/openapi.json")
    assert resp.status_code == 200
    assert resp.json()["info"]["title"]
