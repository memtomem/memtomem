"""Regression guards for the locally vendored Swagger UI.

FastAPI's default docs page (``docs_url="/docs"``) loads
``swagger-ui-bundle.js`` and ``swagger-ui.css`` from
``cdn.jsdelivr.net`` on every render. ``create_app`` disables it and
serves its own ``/api/docs`` page instead, pointing at the vendored
bundle under ``web/static/vendor/swagger/``.

The tests below pin both halves with paired positive + negative
assertions so a future "let's just allow-list jsdelivr" or "bring back
the FastAPI default" change fails loudly. Pattern reference:
``feedback_pin_invert_symmetric_assertion.md`` — a negative-only check
would false-pass if the route were dropped entirely.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
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
    """The FastAPI default ``/redoc`` page must stay disabled.

    ReDoc duplicates Swagger UI's purpose and pulls
    ``redoc.standalone.js`` from jsDelivr by default. The API catch-all
    returns 404 for ``/api/redoc`` even when the default page is enabled.
    """
    app = create_app()
    transport = ASGITransport(app=app, client=("127.0.0.1", 0))
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/redoc", follow_redirects=False)
    assert resp.status_code == 404


_SCHEMA_URL = "/api/openapi.json"
_SWAGGER_INIT = (
    Path(__file__).resolve().parents[1] / "src/memtomem/web/static/vendor/swagger/swagger-init.js"
)


@pytest.fixture
def enforcing_app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """An app with the production CSRF posture.

    The suite's autouse ``_csrf_observe_only_default`` turns enforcement
    off, under which the guard only logs and a hostile Host gets through.
    """
    monkeypatch.delenv("MEMTOMEM_WEB__CSRF_ENFORCE", raising=False)
    application = create_app(lifespan=None)
    assert application.state.csrf_enforce is True
    return application


def test_openapi_schema_is_served_under_api(enforcing_app: FastAPI) -> None:
    """The custom ``/api/docs`` page is useless without the OpenAPI spec.

    Pinned with the guard enforcing, plain and with the ``Referer`` the
    Swagger page's own fetch sends, so a cleanup that sets
    ``openapi_url=None`` or moves it back out of ``/api/`` fails here.
    """
    assert enforcing_app.openapi_url == _SCHEMA_URL
    client = TestClient(enforcing_app, base_url="http://127.0.0.1:8080")
    for headers in ({}, {"Referer": "http://127.0.0.1:8080/api/docs"}):
        resp = client.get(_SCHEMA_URL, headers=headers)
        assert resp.status_code == 200, (headers, resp.text[:200])
        assert resp.json()["info"]["title"] == enforcing_app.title
    # No alias left at FastAPI's default, not even for loopback.
    old = client.get("/openapi.json", follow_redirects=False)
    assert old.status_code == 404, old.text[:200]


def test_openapi_schema_refuses_a_hostile_host(enforcing_app: FastAPI) -> None:
    """The schema answers only the Host the ``/api/*`` guard accepts (#3272).

    FastAPI's default ``/openapi.json`` sat outside the guarded prefix and
    answered a rebound hostname with the full schema.
    """
    client = TestClient(enforcing_app, base_url="http://evil.example:8080")
    assert client.get(_SCHEMA_URL).status_code == 403
    old = client.get("/openapi.json", follow_redirects=False)
    assert old.status_code == 404, old.text[:200]


def test_swagger_init_fetches_the_schema_url_the_app_serves() -> None:
    """The bootstrap's ``url:`` and ``app.openapi_url`` are one value."""
    urls = re.findall(r'^\s*url:\s*"([^"]*)",\s*$', _SWAGGER_INIT.read_text(), re.MULTILINE)
    assert urls == [create_app(lifespan=None).openapi_url]
