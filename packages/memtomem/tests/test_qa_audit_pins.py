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
* **C4 / F-C4-1** — embedding reset concurrent with search degrades to
  BM25-only (never 503 / sqlite error) because reset is one atomic
  transaction *and* the dense leg has a defensive exception catch.
* **A11Y-1 / A11Y-2 / A11Y-3** — issue #1053. Deferred a11y items from the
  kind-moth audit: icon-only button + form input accessible names, modal
  ``role="dialog" aria-modal="true"`` consistency, dynamic search result
  live region, skip-to-main link, and the modal-overlay direct-toggle
  antipattern that breaks the global shortcut gate.

The point is to catch a future PR that forgets the guard, not to
re-derive it from scratch — the underlying enforcement code lives in
``web/routes/system.py``, ``web/app.py``, and ``web/static/app.js``.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
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


# ---------------------------------------------------------------------------
# F-C4-1 — embedding reset during search degrades to BM25-only, not 503
# ---------------------------------------------------------------------------


class TestEmbeddingResetBm25Fallback:
    """Pin for F-C4-1 [Positive] in the kind-moth QA audit.

    The 2026-05-15 runtime probe showed that ``POST /api/embedding-reset``
    issued mid-search burst never produces a 503, sqlite error, or stale
    response — search transparently degrades to BM25-only while the dense
    vector table is recreated. Two layered invariants make that true:

    1. ``reset_embedding_meta`` (``storage/sqlite_backend.py``) is a single
       *synchronous* transaction — ``DROP TABLE``, ``CREATE VIRTUAL TABLE``,
       ``db.commit()`` — so a concurrent search never observes a "table
       missing" intermediate state.
    2. The dense leg in ``search/pipeline.py`` is wrapped in a defensive
       ``except Exception`` that resets ``dense_results = []`` and surfaces
       the failure via ``dense_error``; BM25 still returns hits and the
       endpoint stays at HTTP 200.

    The probe also confirmed (2) never actually fires today because (1) is
    atomic — but (2) is the load-bearing safety net the moment reset moves
    to background execution or grows an inter-statement ``await``. If
    either invariant changes the F-C4-1 smoke must be re-run before merge."""

    @pytest.fixture(scope="class")
    def search_method_src(self) -> str:
        # Scope the fixture to ``SearchPipeline.search`` specifically — the
        # main user-facing retrieval method. The runtime probe ran against
        # this method, so the pin must too. A module-wide AST walk (PR
        # #1051 review round 2) passes when *any* dense_search Try in the
        # module has the right shape, which would mask a regression here
        # if a future helper happens to share the F-C4-1 markers.
        from memtomem.search.pipeline import SearchPipeline

        # ``inspect.getsource`` preserves the method's 4-space class
        # indent; dedent so ``ast.parse`` sees ``async def`` at column 0.
        return textwrap.dedent(inspect.getsource(SearchPipeline.search))

    @pytest.fixture(scope="class")
    def reset_body(self) -> str:
        # Extract just the ``reset_embedding_meta`` coroutine so a future
        # refactor that splits the transaction across helpers will trip
        # the per-function shape checks below rather than passing on
        # module-level coincidence.
        from memtomem.storage.sqlite_backend import SqliteBackend

        return inspect.getsource(SqliteBackend.reset_embedding_meta)

    def test_dense_leg_swallows_exception_to_empty_bm25_only(self, search_method_src: str):
        # Find Try nodes inside ``SearchPipeline.search`` that call
        # ``dense_search``. There should be exactly one — the main-path
        # dense leg. Zero means the F-C4-1 structure was removed; more
        # than one means search() grew a second dense call path and the
        # pin needs an explicit decision on which Try to gate.
        tree = ast.parse(search_method_src)

        dense_try_nodes: list[ast.Try] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Try):
                continue
            for stmt in node.body:
                for inner in ast.walk(stmt):
                    if (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Attribute)
                        and inner.func.attr == "dense_search"
                    ):
                        dense_try_nodes.append(node)
                        break
                else:
                    continue
                break

        assert len(dense_try_nodes) == 1, (
            f"SearchPipeline.search no longer has exactly one Try wrapping "
            f"a dense_search call (found {len(dense_try_nodes)}). The "
            "F-C4-1 fallback structure changed; re-run the runtime probe "
            "and update this pin to match the new shape before relying on it."
        )

        try_node = dense_try_nodes[0]
        assert try_node.handlers, (
            "SearchPipeline.search dense leg lost its except handler — "
            "any dense_search exception (incl. embedding-reset race) will "
            "now propagate as HTTP 5xx instead of degrading to BM25-only"
        )
        handler = try_node.handlers[0]
        caught = "BaseException" if handler.type is None else ast.unparse(handler.type)
        assert caught in ("Exception", "BaseException"), (
            f"SearchPipeline.search dense leg narrowed its handler from "
            f"`except Exception` to `except {caught}` — embedding-reset "
            "or dim-mismatch errors not covered by this type will now "
            "surface as HTTP 5xx instead of the BM25-only fallback "
            "documented in the F-C4-1 runtime probe"
        )

        handler_src = "\n".join(ast.unparse(s) for s in handler.body)
        assert "dense_results = []" in handler_src, (
            "SearchPipeline.search dense handler stopped resetting "
            "`dense_results = []` — stale prior results would leak across "
            "queries, breaking the F-C4-1 graceful-degradation contract"
        )
        assert "dense_error = str(exc)" in handler_src, (
            "SearchPipeline.search dense handler stopped surfacing "
            "`dense_error = str(exc)` — the /api/embedding-status "
            "diagnostic depends on this string for reset-detection"
        )

    def test_reset_embedding_meta_is_single_atomic_transaction(self, reset_body: str):
        # The audit invariant: DROP + meta-update + CREATE + commit all
        # share one synchronous transaction inside reset_embedding_meta().
        # If a refactor splits this — e.g. moves the CREATE to a worker
        # or inserts an ``await`` between DROP and commit — search would
        # be able to observe a missing chunks_vec table mid-reset and the
        # F-C4-1 BM25-only smoke must be re-run before merge.
        assert "DROP TABLE IF EXISTS chunks_vec" in reset_body, (
            "reset_embedding_meta no longer DROPs chunks_vec — "
            "verify the new shape against the F-C4-1 probe"
        )
        assert "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vec" in reset_body, (
            "reset_embedding_meta no longer CREATEs chunks_vec — "
            "verify the new shape against the F-C4-1 probe"
        )
        assert reset_body.count("db.commit()") == 1, (
            "reset_embedding_meta lost its single-commit shape — the "
            "F-C4-1 runtime probe relies on DROP+CREATE+commit being one "
            "transaction so search never sees chunks_vec missing"
        )
        # An ``await`` *inside* the coroutine body (anywhere other than
        # the ``async def`` signature) means a sqlite checkpoint could
        # land mid-reset and a concurrent dense_search could observe the
        # table absent. The current implementation has no such await.
        body_only = reset_body.split(":\n", 1)[1] if ":\n" in reset_body else reset_body
        assert "await " not in body_only, (
            "reset_embedding_meta gained an `await` mid-transaction — "
            "re-run the F-C4-1 runtime probe to confirm search still "
            "degrades to BM25-only instead of surfacing a transient 503"
        )


# ---------------------------------------------------------------------------
# A11Y-1/2/3 — accessibility pins for issue #1053
#
# These are RED until the matching fix PRs land. The plan file lives at
# ``~/.claude/plans/a11y-1-2-3-audit-pass-dreamy-mccarthy.md``. Each test
# encodes "what the fix must look like" using substring oracles on the
# checked-in static assets — no browser, no fixtures — so a future PR that
# removes the guard fails CI before it ships.
# ---------------------------------------------------------------------------


_INDEX_HTML_PATH = _STATIC_DIR / "index.html"


@pytest.fixture(scope="module")
def index_html() -> str:
    return _INDEX_HTML_PATH.read_text(encoding="utf-8")


def _button_block(html: str, button_id: str) -> str:
    """Return the substring from ``<button id="X"`` to the closing ``>``.

    Line-number indexing breaks every time the template shifts; this gives
    each pin a deterministic slice to assert on.
    """
    marker = f'id="{button_id}"'
    idx = html.find(marker)
    assert idx != -1, f"button #{button_id} not found in index.html"
    # Walk back to the opening ``<``.
    start = html.rfind("<", 0, idx)
    end = html.find(">", idx)
    assert start != -1 and end != -1
    return html[start : end + 1]


def _input_block(html: str, input_id: str) -> str:
    marker = f'id="{input_id}"'
    idx = html.find(marker)
    assert idx != -1, f"input/textarea #{input_id} not found in index.html"
    start = html.rfind("<", 0, idx)
    end = html.find(">", idx)
    assert start != -1 and end != -1
    return html[start : end + 1]


def _modal_block(html: str, modal_id: str) -> str:
    marker = f'id="{modal_id}"'
    idx = html.find(marker)
    assert idx != -1, f"modal #{modal_id} not found in index.html"
    start = html.rfind("<", 0, idx)
    end = html.find(">", idx)
    assert start != -1 and end != -1
    return html[start : end + 1]


# Icon-only buttons whose only child content is an emoji / symbol. ``title``
# participates in accessible-name computation but is unreliable across SR /
# browser combinations and gives a poor visible label — the fix pins an
# explicit ``aria-label`` (or ``data-i18n-aria-label`` for translation).
_ICON_ONLY_BUTTONS = (
    "settings-btn",
    "lang-toggle",
    "theme-toggle",
    "group-toggle",
    "view-toggle",
    "similar-close-btn",
    "source-chunks-close-btn",
)


# ``strict=True`` so the marker self-removes when the fix PR lands: once the
# assertion passes, pytest reports XPASS as a failure and forces the developer
# to drop the marker rather than leave a permanent expected-failure that
# silently re-RED'd later.
_A11Y_XFAIL_PR1 = pytest.mark.xfail(
    strict=True, reason="A11Y-2.2 / 2.3 — pending fix in issue #1053 PR #1"
)
_A11Y_XFAIL_PR2 = pytest.mark.xfail(
    strict=True, reason="A11Y-3.4 — pending fix in issue #1053 PR #2"
)
_A11Y_XFAIL_PR3 = pytest.mark.xfail(
    strict=True, reason="A11Y-3.1 — pending modal manager in issue #1053 PR #3"
)
_A11Y_XFAIL_PR4 = pytest.mark.xfail(
    strict=True, reason="A11Y-2.1 — pending fix in issue #1053 PR #4"
)
_A11Y_XFAIL_PR5 = pytest.mark.xfail(
    strict=True, reason="A11Y-1.1 — pending fix in issue #1053 PR #5"
)


@_A11Y_XFAIL_PR1
class TestA11yIconButtonNames:
    """A11Y-2.3 — icon-only buttons in the header / panel chrome must carry
    an explicit accessible name. ``aria-label`` (static) or
    ``data-i18n-aria-label`` (i18n-bound, resolved by ``i18n.js``) both
    satisfy this; ``title`` alone does not."""

    @pytest.mark.parametrize("button_id", _ICON_ONLY_BUTTONS)
    def test_icon_button_has_explicit_accessible_name(self, index_html: str, button_id: str):
        block = _button_block(index_html, button_id)
        has_aria_label = "aria-label=" in block or "data-i18n-aria-label=" in block
        assert has_aria_label, (
            f"icon-only button #{button_id} relies on title/placeholder "
            f"for its accessible name — add aria-label= or "
            f"data-i18n-aria-label= (A11Y-2.3, issue #1053)"
        )


# Inputs that appear without a ``<label for=>`` association or explicit
# aria-* fallback. Placeholders are visual hints, not accessible names.
_ORPHAN_INPUTS = (
    "home-search-input",
    "tag-filter",
    "score-threshold",
    "d-editor",
    "d-tag-input",
    "memory-add-input",
)


@_A11Y_XFAIL_PR1
class TestA11yOrphanInputLabels:
    """A11Y-2.2 — every form control must have one of: ``aria-label``,
    ``aria-labelledby``, or a ``<label for="ID">`` somewhere in the
    document. Otherwise SR users hear only the role + value."""

    @pytest.mark.parametrize("input_id", _ORPHAN_INPUTS)
    def test_input_has_label_association(self, index_html: str, input_id: str):
        block = _input_block(index_html, input_id)
        inline = (
            "aria-label=" in block
            or "data-i18n-aria-label=" in block
            or "aria-labelledby=" in block
        )
        # ``<label for="ID">`` can live anywhere in the document; treat any
        # match as sufficient for this pin.
        external = f'for="{input_id}"' in index_html
        assert inline or external, (
            f"input/textarea #{input_id} has no accessible name — add "
            f"aria-label=, aria-labelledby=, or a sibling <label for=>"
            f" (A11Y-2.2, issue #1053)"
        )


# Every modal-overlay container. ``role="dialog"`` + ``aria-modal="true"``
# is the minimum SR contract — VoiceOver / NVDA use it to scope navigation
# and stop announcing background landmarks. Two modals already declare both
# attributes today; the remaining six get the PR #2 xfail marker so the
# regression guard stays GREEN on the fixed ones.
_MODALS_ALREADY_PINNED = frozenset({"ctx-conflict-modal", "path-picker-modal"})

_MODAL_PARAMS = [
    pytest.param(
        modal_id,
        marks=[] if modal_id in _MODALS_ALREADY_PINNED else [_A11Y_XFAIL_PR2],
        id=modal_id,
    )
    for modal_id in (
        "expand-modal",
        "source-preview-modal",
        "settings-modal",
        "shortcuts-modal",
        "cmd-palette",
        "confirm-modal",
        "ctx-conflict-modal",
        "path-picker-modal",
    )
]


class TestA11yModalAriaModal:
    """A11Y-3.4 — every ``.modal-overlay`` must declare itself as a dialog
    so AT scope into it. ``ctx-conflict-modal`` and ``path-picker-modal``
    already pass; the other six are the RED rows this pin tracks."""

    @pytest.mark.parametrize("modal_id", _MODAL_PARAMS)
    def test_modal_declares_dialog_role(self, index_html: str, modal_id: str):
        block = _modal_block(index_html, modal_id)
        assert 'role="dialog"' in block, (
            f'modal #{modal_id} missing role="dialog" — add it to the '
            f"opening tag (A11Y-3.4, issue #1053)"
        )

    @pytest.mark.parametrize("modal_id", _MODAL_PARAMS)
    def test_modal_declares_aria_modal(self, index_html: str, modal_id: str):
        block = _modal_block(index_html, modal_id)
        assert 'aria-modal="true"' in block, (
            f'modal #{modal_id} missing aria-modal="true" — add it to '
            f"the opening tag (A11Y-3.4, issue #1053)"
        )


class TestA11yLiveRegionsPreserved:
    """A11Y-2.6 — regression guard for the four existing live regions that
    announce indexing / model-readiness / upload-usage / toasts. These are
    *already correct* in main; the pin catches a future refactor that
    drops the ``aria-live`` attribute or replaces the container."""

    def test_indexing_indicator_live_region(self, index_html: str):
        assert 'id="indexing-indicator"' in index_html and 'aria-live="polite"' in index_html
        # Stronger pin: the indexing-indicator block itself carries the
        # attribute. Avoid asserting on line numbers — they drift.
        block_start = index_html.find('id="indexing-indicator"')
        opening = index_html.rfind("<", 0, block_start)
        opening_tag = index_html[opening : index_html.find(">", block_start) + 1]
        assert 'aria-live="polite"' in opening_tag, (
            'indexing-indicator lost its aria-live="polite" — restore it (A11Y-2.6, issue #1053)'
        )

    def test_model_readiness_banner_live_region(self, index_html: str):
        block_start = index_html.find('id="model-readiness-banner"')
        assert block_start != -1, "model-readiness-banner removed"
        opening = index_html.rfind("<", 0, block_start)
        opening_tag = index_html[opening : index_html.find(">", block_start) + 1]
        assert 'aria-live="polite"' in opening_tag, (
            'model-readiness-banner lost its aria-live="polite"'
        )

    def test_upload_usage_live_region(self, index_html: str):
        block_start = index_html.find('id="upload-usage"')
        assert block_start != -1, "upload-usage span removed"
        opening = index_html.rfind("<", 0, block_start)
        opening_tag = index_html[opening : index_html.find(">", block_start) + 1]
        assert 'aria-live="polite"' in opening_tag, 'upload-usage lost its aria-live="polite"'

    def test_toast_container_live_region(self, index_html: str):
        block_start = index_html.find('id="toast-container"')
        assert block_start != -1, "toast-container removed"
        opening = index_html.rfind("<", 0, block_start)
        opening_tag = index_html[opening : index_html.find(">", block_start) + 1]
        assert 'aria-live="polite"' in opening_tag, 'toast-container lost its aria-live="polite"'


@_A11Y_XFAIL_PR4
class TestA11yResultsLiveRegion:
    """A11Y-2.1 — ``renderResults()`` rewrites the results list every time
    a search returns; SR users get no notification of the count change.
    The fix wraps the results-list (or an adjacent summary node) in an
    ``aria-live="polite"`` region. Either placement satisfies the pin."""

    def test_results_region_is_announced(self, index_html: str):
        # Look at the opening tag for results-list and its immediate
        # surroundings (results-summary lives next to it). One of the two
        # containers should carry aria-live=polite.
        for marker in ('id="results-list"', 'id="results-summary"'):
            idx = index_html.find(marker)
            if idx == -1:
                continue
            opening = index_html.rfind("<", 0, idx)
            opening_tag = index_html[opening : index_html.find(">", idx) + 1]
            if 'aria-live="polite"' in opening_tag:
                return
        raise AssertionError(
            "neither #results-list nor #results-summary carries "
            'aria-live="polite" — search count changes are silent for '
            "SR users (A11Y-2.1, issue #1053)"
        )


@_A11Y_XFAIL_PR5
class TestSkipLinkPresent:
    """A11Y-1.1 — first focusable element in the document should be a
    skip-to-main anchor so keyboard users bypass the header chrome (~6
    Tabs today)."""

    def test_skip_link_jumps_to_main(self, index_html: str):
        # The link is visually-hidden until focused but must exist in the
        # DOM and target a #main landmark.
        assert 'class="skip-link"' in index_html or 'id="skip-to-main"' in index_html, (
            'no skip-to-main link found — add an <a> with class="skip-link"'
            ' (or id="skip-to-main") near the top of <body> (A11Y-1.1, '
            "issue #1053)"
        )
        assert 'href="#main"' in index_html, (
            "skip link does not target #main — the landmark must exist and "
            "the anchor href must point at it"
        )
        # And there has to actually be a main landmark to jump to.
        assert 'id="main"' in index_html, 'no element with id="main" to be the skip-link target'


@_A11Y_XFAIL_PR3
class TestA11yModalToggleAntipattern:
    """A11Y-3 enforcement — once the modal manager lands, every modal
    open/close path must funnel through ``openModal(el)`` / ``closeModal(el)``
    so the active-modal set stays accurate and the global shortcut gate
    works. Direct ``modal().hidden = false/true`` or ``show(qs('X-modal'))``
    on a ``.modal-overlay`` element re-introduces the bug at A11Y-3.1.

    The pin is intentionally narrow — it only fires on the two known modal
    files (``path-picker.js``, ``context-gateway.js``) plus any future
    file that toggles ``modal().hidden``. ``app.js`` ``show()/hide()``
    calls are still allowed because they go through the modal-manager
    wrapper once the fix lands.
    """

    def test_path_picker_uses_modal_manager(self):
        js = (_STATIC_DIR / "path-picker.js").read_text(encoding="utf-8")
        # Today the file directly toggles ``modal().hidden = false/true`` —
        # this assert is RED until PR #3 (modal manager) migrates it.
        assert "modal().hidden = false" not in js and "modal().hidden = true" not in js, (
            "path-picker.js still toggles modal().hidden directly — route "
            "through the modal manager's openModal/closeModal so the global "
            "shortcut gate sees the picker (A11Y-3.1, issue #1053)"
        )

    def test_ctx_conflict_uses_modal_manager(self):
        js = (_STATIC_DIR / "context-gateway.js").read_text(encoding="utf-8")
        # ``_ctxResolveConflict`` currently calls ``show(modal)`` / ``hide(modal)``
        # which are the generic helpers at app.js:527-528. After the modal
        # manager lands the modal-overlay path must use openModal/closeModal.
        in_resolver = "_ctxResolveConflict" in js
        assert in_resolver, "_ctxResolveConflict() vanished — re-locate pin"
        # Slice around the resolver body.
        start = js.find("function _ctxResolveConflict")
        end = js.find("\nfunction ", start + 1)
        body = js[start:end] if end != -1 else js[start:]
        assert "show(modal)" not in body and "hide(modal)" not in body, (
            "_ctxResolveConflict still uses generic show()/hide() on the "
            "modal — switch to openModal(modal)/closeModal(modal) so the "
            "modal manager tracks it (A11Y-3.1, issue #1053)"
        )
