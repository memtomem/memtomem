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
