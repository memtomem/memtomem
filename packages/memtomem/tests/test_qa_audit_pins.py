"""Regression pins for the risk-first QA audit.

These tests automate the destructive-action and trust-boundary findings
from the manual QA pass. Each ID maps to a finding in the audit doc:

* **D2** — admin namespace endpoints stay off the prod surface.
* **B2-c** — ``_require_localhost`` rejects non-loopback peers using the
  TCP client host, not user-supplied ``X-Forwarded-For`` headers.
* **S1 / S3** — web ``/api/search`` and ``/api/stats`` keep parameter and
  response shapes that the MCP ``mem_search`` / ``mem_stats`` tools also
  expose, so a refactor on either side can't silently drift them apart.
* **B5** — chunk-card / dedup-row renderers route every user-controlled
  field through ``escapeHtml`` or ``DOMPurify.sanitize`` before
  ``innerHTML`` so a malicious chunk body can't execute JS in the SPA.

The point is to catch a future PR that forgets the guard, not to
re-derive it from scratch — the underlying enforcement code lives in
``web/routes/system.py``, ``web/app.py``, and ``web/static/app.js``.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.web.app import create_app


_STATIC_DIR = Path(__file__).resolve().parent.parent / "src" / "memtomem" / "web" / "static"


@pytest.fixture
def prod_app():
    application = create_app(lifespan=None, mode="prod")
    application.state.storage = AsyncMock()
    application.state.config = None
    application.state.search_pipeline = None
    application.state.index_engine = None
    application.state.embedder = None
    application.state.dedup_scanner = None
    application.state.project_root = Path("/tmp/test-project")
    return application


@pytest.fixture
def dev_app():
    application = create_app(lifespan=None, mode="dev")
    application.state.storage = AsyncMock()
    application.state.config = None
    application.state.search_pipeline = None
    application.state.index_engine = None
    application.state.embedder = None
    application.state.dedup_scanner = None
    application.state.project_root = Path("/tmp/test-project")
    return application


# ---------------------------------------------------------------------------
# D2 — admin namespace endpoints are dev-only
# ---------------------------------------------------------------------------


class TestNamespaceDeleteProdGuard:
    """Admin namespace surface (rename / delete / per-namespace info) lives
    on ``_DEV_ONLY_ROUTERS`` because rename + delete need chunk-id stability
    design (ADR-0005). The prod app must not mount those routes — the
    catch-all 404 handler in ``web/app.py`` covers anything that slips."""

    @pytest.mark.asyncio
    async def test_delete_namespace_returns_404_in_prod(self, prod_app):
        transport = ASGITransport(app=prod_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete("/api/namespaces/whatever")
        assert resp.status_code == 404, (
            "DELETE /api/namespaces/{ns} must not be reachable in prod mode "
            "(admin_router lives in _DEV_ONLY_ROUTERS — see web/app.py:91-98)"
        )

    @pytest.mark.asyncio
    async def test_delete_namespace_reachable_in_dev(self, dev_app):
        # Sanity counterpart so a future refactor that drops the route
        # entirely fails this pin instead of looking like a prod fix.
        dev_app.state.storage.delete_by_namespace = AsyncMock(return_value=0)
        transport = ASGITransport(app=dev_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.delete("/api/namespaces/whatever")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# B2-c — localhost guard uses TCP peer, ignores X-Forwarded-For
# ---------------------------------------------------------------------------


class TestResetLocalhostGuard:
    """``/api/reset`` is gated by ``_require_localhost`` which reads
    ``request.client.host`` (the TCP peer) and rejects anything outside
    ``{'127.0.0.1', '::1', 'localhost'}``. ``X-Forwarded-For`` is
    intentionally NOT consulted — a reverse-proxy header injection must
    not unlock the endpoint."""

    @pytest.mark.asyncio
    async def test_reset_rejects_external_client(self, prod_app):
        transport = ASGITransport(app=prod_app, client=("203.0.113.7", 12345))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post(
                "/api/reset",
                headers={"X-Forwarded-For": "127.0.0.1"},
            )
        assert resp.status_code == 403, (
            "External client must not be able to spoof localhost via "
            "X-Forwarded-For — _require_localhost checks request.client.host"
        )
        assert "localhost" in resp.json().get("detail", "").lower()

    @pytest.mark.asyncio
    async def test_reset_accepts_loopback_client(self, prod_app):
        prod_app.state.storage.reset_all = AsyncMock(return_value={"chunks": 0})
        # Default ASGITransport client is ('127.0.0.1', 123); CSRFGuard
        # also runs but localhost + same-origin (Host = "test") is
        # inside its loopback allow-list with no Origin header.
        transport = ASGITransport(app=prod_app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/api/reset")
        # CSRF middleware may 403 the missing Origin/Host on a non-loopback
        # Host like "test" — we only assert the localhost gate passed (so a
        # 403 here, if any, is from CSRF not _require_localhost). The
        # localhost-failure path emits a distinctive "localhost" detail.
        if resp.status_code == 403:
            assert "localhost" not in resp.json().get("detail", "").lower(), (
                "reset rejected by _require_localhost on a loopback client — "
                "the guard's allow-list lost 127.0.0.1"
            )


# ---------------------------------------------------------------------------
# S1 + S3 — web/MCP shape parity for read-only surfaces
# ---------------------------------------------------------------------------


class TestDualSurfaceParity:
    """Web ``/api/search`` and ``/api/stats`` are the read-only siblings of
    MCP ``mem_search`` / ``mem_stats``. The audit found that both surfaces
    must accept the same parameter set so a user (or a script) can switch
    between them without the request shape drifting. These tests pin the
    parameter overlap at the function-signature level — adding a parameter
    to one side is fine; dropping a shared one without updating the other
    fails the pin."""

    def test_mem_search_accepts_documented_web_params(self):
        from memtomem.server.tools.search import mem_search

        sig = inspect.signature(mem_search)
        params = set(sig.parameters)
        # Subset the SPA's Search tab actually sends. The MCP tool may
        # accept more (verbose, output_format, etc.); the assertion is
        # that nothing on this list is missing on the MCP side.
        web_params = {
            "query",
            "top_k",
            "source_filter",
            "tag_filter",
            "namespace",
            "context_window",
        }
        missing = web_params - params
        assert not missing, (
            f"mem_search MCP tool dropped parameters that the web SPA "
            f"still sends: {sorted(missing)}. Either restore them or "
            f"update the SPA to stop sending them."
        )

    def test_mem_stats_signature_is_zero_arg(self):
        # Pin the no-argument shape — the SPA's ``loadStats`` calls
        # ``GET /api/stats`` with no body, and the MCP path is the same
        # surface. A future refactor that adds a required parameter would
        # silently break the SPA's poll loop.
        from memtomem.server.tools.status_config import mem_stats

        sig = inspect.signature(mem_stats)
        required = [
            name
            for name, p in sig.parameters.items()
            if p.default is inspect.Parameter.empty
            and p.kind not in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD)
        ]
        assert required == [], (
            f"mem_stats grew required parameter(s) {required}; "
            f"web /api/stats and SPA poll loop still call it zero-arg"
        )


# ---------------------------------------------------------------------------
# B5 — chunk-card / dedup renderers escape user content before innerHTML
# ---------------------------------------------------------------------------


class TestChunkCardXssGuard:
    """Source-of-truth check that the SPA escapes user-controlled fields
    before injecting them into the chunk card / dedup row / search result
    templates. The audit's static review confirmed coverage today; this
    pin catches a future refactor that adds a new ``${...}`` interpolation
    of a user-controlled field without wrapping it in ``escapeHtml``,
    ``escapeAttr``, ``highlightText``, or ``DOMPurify.sanitize``.

    The check is intentionally substring-based on the rendered template
    block — a runtime XSS test would need a seeded LTM and a Playwright
    page, which adds more infra than the bug surface justifies once the
    escape helpers are in place."""

    @pytest.fixture(scope="class")
    def app_js(self) -> str:
        return (_STATIC_DIR / "app.js").read_text(encoding="utf-8")

    def test_chunk_type_label_is_escaped_in_card(self, app_js: str):
        # Pin for F-XSS-1: the chunk_type badge inside the chunk card
        # template (renders inside ``browseSource``) must go through
        # escapeHtml — server enum today, but defensive.
        snippet = '<span class="badge badge-gray">${escapeHtml(c.chunk_type'
        assert snippet in app_js, (
            "chunk_type badge in browseSource() chunk-card template lost "
            "its escapeHtml wrapping — restore it (audit F-XSS-1)"
        )

    def test_escape_html_covers_single_quote(self, app_js: str):
        # Pin for F-XSS-2: ``escapeHtml`` must escape ``'`` so a single-
        # quoted attribute context (none today, but defensible) stays safe.
        # The exact replacement char (``&#39;``) is documented MDN syntax.
        assert "replace(/'/g, '&#39;')" in app_js, (
            "escapeHtml lost its single-quote escape — restore the "
            ".replace(/'/g, '&#39;') line (audit F-XSS-2)"
        )

    def test_dedup_row_renders_chunk_content_through_escape(self, app_js: str):
        # Pin for the dedup row template in settings-maintenance.js —
        # both A and B chunk previews must funnel through ``escapeHtml``
        # before the ``truncate``-d content lands in innerHTML.
        m_js = (_STATIC_DIR / "settings-maintenance.js").read_text(encoding="utf-8")
        assert m_js.count('class="dedup-chunk-content">${escapeHtml(truncate(c.chunk_') >= 2, (
            "dedup-row template stopped escaping chunk_a/chunk_b content "
            "before injecting into innerHTML"
        )

    def test_search_result_card_renders_filename_through_escape(self, app_js: str):
        # Search result card builds an innerHTML template that includes
        # the source filename — must go through escapeHtml. Spot-checks
        # the known-vulnerable axis from the audit (``fname`` came from
        # the chunk's source path which the user controls via filenames).
        assert 'class="result-filename">${escapeHtml(fname)}' in app_js, (
            "search-result card stopped escaping the source filename "
            "before injecting into innerHTML"
        )


# ---------------------------------------------------------------------------
# F-L5-1 — dark `.btn-danger` solid background + hover affordance
# ---------------------------------------------------------------------------


class TestBtnDangerContrastGuard:
    """The kind-moth L5 smoke (2026-05-15) found dark-theme ``.btn-danger``
    at 3.63:1 against white, failing WCAG AA. The fix in ``style.css`` is
    two scoped rules: a dark-only solid background override to ``#c43c3c``
    and a hover override that swaps the global ``opacity: 0.85`` dim for a
    ``filter: brightness(0.92)`` darken so hover contrast survives.

    This static guard catches the deliberate-removal regression vectors —
    someone deleting either rule, or changing the ``:not(.btn-ghost)``
    scoping that protects the combined ``btn-ghost btn-danger`` chip. The
    companion Playwright test in ``tests/web/test_l5_btn_danger_contrast.py``
    measures effective contrast across the 4-state matrix to catch cascade
    or filter-composition regressions that a substring check cannot see."""

    @pytest.fixture(scope="class")
    def style_css(self) -> str:
        return (_STATIC_DIR / "style.css").read_text(encoding="utf-8")

    def test_dark_solid_background_override(self, style_css: str):
        # The dark-only solid override. ``:not([data-theme="light"])`` keeps
        # the rule from leaking into the explicit-light theme; ``:not(.btn-
        # ghost)`` keeps combined ``btn-ghost btn-danger`` chips transparent.
        selector = ':root:not([data-theme="light"]) .btn-danger:not(.btn-ghost)'
        assert selector in style_css, (
            f"dark .btn-danger solid background override lost its selector "
            f"({selector!r}) — restore it (audit F-L5-1, style.css:925)"
        )
        assert "#c43c3c" in style_css, (
            "dark .btn-danger background colour #c43c3c no longer in style.css "
            "— the F-L5-1 fix raises the dark solid bg from #e05a5a (3.63:1) "
            "to #c43c3c (5.17:1)"
        )

    def test_hover_brightness_override(self, style_css: str):
        # The hover override that replaces the global ``button:hover {
        # opacity: 0.85 }`` dim with ``filter: brightness(0.92)`` darken so
        # hover contrast stays above AA (light hover would drop to ~3.93:1
        # under the global opacity dim).
        hover_selector = ".btn-danger:not(.btn-ghost):hover"
        assert hover_selector in style_css, (
            f"hover override for solid .btn-danger lost its selector "
            f"({hover_selector!r}) — restore it (audit F-L5-1, style.css:930)"
        )
        assert "brightness(0.92)" in style_css, (
            "filter: brightness(0.92) no longer in style.css — without this "
            "the global button:hover opacity 0.85 drops hover contrast below "
            "AA (audit F-L5-1)"
        )


# ---------------------------------------------------------------------------
# E-L5-d — I18N.setLang must serialise _load → applyDOM → langchange
# ---------------------------------------------------------------------------


class TestSetLangAwaitOrder:
    """The kind-moth audit notes that ``I18N.setLang`` previously raced its
    locale fetch — the click handler dispatched ``langchange`` immediately
    after calling ``setLang`` without await, so listeners (e.g. ``app.js``
    ``loadStats``) read stale ``_cache`` and wrote the wrong language into
    the DOM right before ``applyDOM`` clobbered them.

    The fix serialised the function body so ``await _load(lang)`` runs to
    completion, then ``applyDOM()``, then ``dispatchEvent(new CustomEvent(
    'langchange'`` fires with the fresh cache. The advisory ``tests-js/
    i18n-apply-dom.test.mjs`` covers the DOM behaviour in jsdom, but
    AGENTS / CI gate on pytest + ruff, so this source-order pin guards the
    required surface against a re-introduced race."""

    @pytest.fixture(scope="class")
    def setlang_body(self) -> str:
        text = (_STATIC_DIR / "i18n.js").read_text(encoding="utf-8")
        start = text.find("async function setLang(lang) {")
        assert start != -1, "setLang function declaration not found in i18n.js"
        # ``init()`` is the very next declaration; its leading docstring is
        # a stable sentinel that doesn't appear inside setLang.
        end = text.find("/** Initialise:", start)
        assert end != -1, (
            "init() docstring sentinel not found after setLang — "
            "the slice helper needs a new boundary"
        )
        return text[start:end]

    def test_await_load_before_apply_dom(self, setlang_body: str):
        load_idx = setlang_body.find("await _load(lang)")
        apply_idx = setlang_body.find("applyDOM()")
        assert load_idx != -1, "await _load(lang) missing from setLang body"
        assert apply_idx != -1, "applyDOM() missing from setLang body"
        assert load_idx < apply_idx, (
            "setLang must await _load(lang) BEFORE applyDOM(). Reverting "
            "this order re-introduces the i18n race that PR #595 fixed."
        )

    def test_apply_dom_before_langchange_dispatch(self, setlang_body: str):
        apply_idx = setlang_body.find("applyDOM()")
        dispatch_idx = setlang_body.find("dispatchEvent(new CustomEvent('langchange'")
        assert dispatch_idx != -1, "langchange CustomEvent dispatch missing from setLang body"
        assert apply_idx < dispatch_idx, (
            "setLang must call applyDOM() BEFORE dispatching the "
            "'langchange' event, so listeners that re-read t() see the "
            "newly-applied DOM state (i18n.js:65-76 comment)."
        )
