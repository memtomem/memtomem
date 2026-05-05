"""Regression guards for the vendored cdnjs assets and the locked-down CSP.

The Web UI moved DOMPurify, marked, and Prism (core + 5 language plugins +
the tomorrow theme) from ``cdnjs.cloudflare.com`` to ``web/static/vendor/``
and tightened the Content-Security-Policy from
``script-src 'self' https://cdnjs.cloudflare.com`` back to
``script-src 'self'``. The tests below pin both halves with paired
positive + negative assertions so a future "let's just allow-list this CDN
again" change fails loudly instead of silently re-enabling external script
loading.

Pattern reference: ``feedback_pin_invert_symmetric_assertion.md`` — a
negative-only check would false-pass if the CSP header were dropped
entirely.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.web.app import create_app

_VENDORED_ASSETS = [
    "purify.min.js",
    "marked.umd.js",
    "prism.min.js",
    "prism-python.min.js",
    "prism-typescript.min.js",
    "prism-json.min.js",
    "prism-bash.min.js",
    "prism-yaml.min.js",
    "prism-tomorrow.min.css",
]

_INDEX_HTML = (
    Path(__file__).resolve().parents[1] / "src" / "memtomem" / "web" / "static" / "index.html"
)


@pytest.mark.asyncio
async def test_csp_locks_script_src_to_self_no_external_cdn() -> None:
    """CSP keeps the SPA on first-party scripts only.

    Positive marker (``script-src 'self';``) confirms the header is present
    and tightened. Negative markers catch the regression where someone
    allow-lists an external CDN again — a negative-only assertion would
    false-pass if the header were dropped.
    """
    app = create_app()
    transport = ASGITransport(app=app, client=("127.0.0.1", 0))
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get("/")
    csp = resp.headers["content-security-policy"]

    # positive — header present and tightened to first-party
    assert "default-src 'self';" in csp
    assert "script-src 'self';" in csp

    # negative — guard against re-introducing external CDN allow-lists
    assert "cdnjs.cloudflare.com" not in csp
    assert "jsdelivr" not in csp
    assert "unpkg" not in csp
    # ``script-src 'self' https://...`` smuggles in any external host;
    # block the shape generically instead of name-checking each CDN
    assert "script-src 'self' https" not in csp
    assert "style-src 'self' 'unsafe-inline' https" not in csp


@pytest.mark.asyncio
@pytest.mark.parametrize("asset", _VENDORED_ASSETS)
async def test_vendor_asset_served_locally(asset: str) -> None:
    """Each vendored cdnjs asset is reachable at ``/vendor/<name>``.

    Pairs with the index.html grep guard below: vendor files alone don't
    help if index.html still points at the CDN, and CDN-free index.html
    alone breaks if a vendor file is deleted.
    """
    app = create_app()
    transport = ASGITransport(app=app, client=("127.0.0.1", 0))
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        resp = await c.get(f"/vendor/{asset}")
    assert resp.status_code == 200
    assert len(resp.content) > 0


def test_index_html_has_no_external_cdn_refs() -> None:
    """SPA must not re-introduce cdnjs / jsdelivr / unpkg in <script>/<link>."""
    # Pin encoding="utf-8": index.html ships UTF-8 (em-dashes in banner copy)
    # but Path.read_text() defaults to ``locale.getpreferredencoding()``,
    # which is cp1252 on Windows runners — that codec has no mapping for
    # 0x8f and the read explodes before the assertion ever runs.
    html = _INDEX_HTML.read_text(encoding="utf-8")
    for forbidden in ("cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com"):
        assert forbidden not in html, (
            f"index.html re-introduced an external CDN reference to {forbidden!r}; "
            "vendor the asset under web/static/vendor/ instead."
        )


def test_index_html_references_every_vendor_asset() -> None:
    """Every file in ``_VENDORED_ASSETS`` must be referenced from index.html.

    Catches the inverse of the deletion case: a vendor file is added but
    the SPA never loads it, or a rename drops a script tag.
    """
    html = _INDEX_HTML.read_text(encoding="utf-8")
    missing = [a for a in _VENDORED_ASSETS if f"/vendor/{a}" not in html]
    assert not missing, f"index.html does not reference vendored assets: {missing}"
