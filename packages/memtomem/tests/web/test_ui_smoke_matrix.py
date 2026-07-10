"""Browser smoke matrix for top-level UI navigation.

This is intentionally broad and shallow: it drives the shipped SPA in a real
Chromium instance, stubs API responses in schema-shaped payloads, and verifies
that every top-level tab plus the main nested menus can be activated without
client-side errors.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.browser


@contextmanager
def _web_server(mode: str) -> Iterator[str]:
    import uvicorn

    from memtomem.web.app import create_app

    app = create_app(lifespan=None, mode=mode)
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)

    def _run() -> None:
        asyncio.run(server.serve())

    thread = threading.Thread(target=_run, daemon=True, name=f"mm-web-smoke-{mode}")
    thread.start()
    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2.0)
        raise RuntimeError(f"uvicorn server did not start for mode={mode}")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)


def _chunk() -> dict[str, object]:
    return {
        "id": "smoke-1",
        "content": "# Smoke note\n\nThis is a smoke-test memory.",
        "source_file": "/tmp/memtomem-smoke/notes.md",
        "chunk_type": "markdown_section",
        "start_line": 1,
        "end_line": 3,
        "heading_hierarchy": ["Smoke note"],
        "tags": ["smoke"],
        "namespace": "default",
        "created_at": "2026-05-15T00:00:00Z",
        "updated_at": "2026-05-15T00:00:00Z",
        "target_scope": "user",
    }


def _source() -> dict[str, object]:
    return {
        "path": "/tmp/memtomem-smoke/notes.md",
        "chunk_count": 1,
        "last_indexed_at": "2026-05-15T00:00:00Z",
        "file_size": 128,
        "namespaces": ["default"],
        "avg_tokens": 20,
        "min_tokens": 20,
        "max_tokens": 20,
        "memory_dir": "/tmp/memtomem-smoke",
        "kind": "memory",
        "target_scope": "user",
        "title": "Smoke note",
        "excerpt": "This is a smoke-test memory.",
        "ai_summary": None,
        "ai_summary_language": None,
    }


def _api_payload(url: str, mode: str) -> dict[str, object]:
    path = urlparse(url).path
    chunk = _chunk()
    source = _source()

    if path == "/api/system/ui-mode":
        return {"mode": mode}
    if path == "/api/system/model-readiness":
        return {"ready": True}
    if path == "/api/session":
        return {"csrf_token": "smoke-token"}
    if path == "/api/config":
        return {
            "embedding": {"provider": "none", "model": "none", "dimension": 0},
            "indexing": {
                "memory_dirs": ["/tmp/memtomem-smoke"],
                "supported_extensions": [".md"],
                "exclude_patterns": [],
                "max_chunk_tokens": 800,
                "chunk_overlap": 80,
            },
            "search": {"top_k": 10, "tokenizer": "unicode61"},
            "storage": {"backend": "sqlite", "sqlite_path": "/tmp/memtomem-smoke.db"},
        }
    if path == "/api/config/defaults":
        return _api_payload("http://test/api/config", mode)
    if path == "/api/indexing/builtin-exclude-patterns":
        return {"secret": ["*.env", ".env.*"], "noise": ["node_modules/", ".git/"]}
    if path == "/api/privacy/patterns":
        return {"patterns": [], "sha": "smoke"}
    if path == "/api/memory-dirs/status":
        return {
            "dirs": [
                {
                    "path": "/tmp/memtomem-smoke",
                    "exists": True,
                    "provider": "user",
                    "category": "user",
                    "kind": "memory",
                    "file_count": 1,
                    "source_file_count": 1,
                    "chunk_count": 1,
                }
            ]
        }
    if path == "/api/sources":
        return {"sources": [source], "total": 1, "offset": 0, "limit": 10000}
    if path.startswith("/api/sources/"):
        return {"path": source["path"], "chunks": [chunk], "total": 1}
    if path == "/api/chunks":
        return {"chunks": [chunk], "total": 1}
    if path == "/api/search":
        return {
            "results": [
                {
                    "chunk": chunk,
                    "score": 0.91,
                    "rank": 1,
                    "source": "fused",
                    "context": None,
                }
            ],
            "total": 1,
            "retrieval_stats": {
                "bm25_candidates": 1,
                "dense_candidates": 1,
                "fused_total": 1,
                "final_total": 1,
            },
        }
    if path == "/api/namespaces":
        return {"namespaces": [{"namespace": "default", "chunk_count": 1, "source_count": 1}]}
    if path == "/api/tags":
        return {"tags": [{"tag": "smoke", "count": 1}], "total": 1, "offset": 0, "limit": 100}
    if path == "/api/stats":
        return {
            "total_chunks": 1,
            "total_sources": 1,
            "chunk_size_distribution": [{"bucket": "0-500", "count": 1}],
            "home_sources": [source],
            "home_recent_sources": [source],
            "home_total_source_size": 128,
            "home_file_type_distribution": [{"file_type": "md", "count": 1}],
        }
    if path.startswith("/api/context/") or path == "/api/settings-sync":
        return {
            "status": "in_sync",
            "items": [],
            "artifacts": [],
            "skills": [],
            "agents": [],
            "commands": [],
            "hooks": {"pending": [], "conflicts": [], "synced": []},
        }
    if path == "/api/timeline":
        return {"chunks": [chunk], "total": 1, "has_more": False}
    if path.startswith("/api/dedup"):
        return {"pairs": [], "candidates": []}
    if path.startswith("/api/decay"):
        return {"matched": 0, "chunks": []}
    return {}


def _install_smoke_stubs(page, mode: str) -> None:
    def _route(route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_api_payload(route.request.url, mode)),
        )

    page.route("**/api/**", _route)


def _install_error_capture(page) -> None:
    page.add_init_script(
        """
        window.__smokeErrors = [];
        window.addEventListener('error', event => {
          window.__smokeErrors.push(event.error?.stack || event.message);
        });
        window.addEventListener('unhandledrejection', event => {
          const reason = event.reason || {};
          window.__smokeErrors.push(reason.stack || reason.message || String(event.reason));
        });
        """
    )


def _ensure_nav_visible(page, container: str, section: str):
    loc = page.locator(f'{container} .settings-nav-btn[data-section="{section}"]')
    if not loc.count() or loc.first.is_visible():
        return loc
    group = loc.first.get_attribute("data-group")
    if group:
        group_loc = page.locator(f'{container} .settings-nav-group[data-group="{group}"]')
        if group_loc.count():
            group_loc.first.click()
            page.wait_for_timeout(100)
    return loc


def _assert_active_panel(page, tab: str) -> None:
    assert page.locator(f'.tab-btn[data-tab="{tab}"]').get_attribute("aria-selected") == "true"
    assert page.locator(f"#tab-{tab}").evaluate(
        "el => el.classList.contains('active') && !el.hidden && getComputedStyle(el).display !== 'none'"
    )


@pytest.mark.parametrize("mode", ["prod", "dev"])
@pytest.mark.parametrize("viewport", [(1440, 1000), (1024, 768), (390, 844)])
def test_ui_smoke_matrix(page, mode: str, viewport: tuple[int, int]) -> None:
    """Walk the main tabs and nested menus in prod/dev on desktop/mobile."""
    page.set_viewport_size({"width": viewport[0], "height": viewport[1]})
    page_errors: list[str] = []
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    _install_error_capture(page)
    _install_smoke_stubs(page, mode)

    with _web_server(mode) as base_url:
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_selector(".tab-nav .tab-btn", timeout=5_000)

        expected_tabs = [
            "home",
            "search",
            "sources",
            "context-gateway",
            "index",
            "tags",
            "timeline",
            "settings",
        ]
        visible_tabs = page.locator(".tab-nav .tab-btn").evaluate_all(
            "els => els"
            ".filter(el => !el.hidden && getComputedStyle(el).display !== 'none')"
            ".map(el => el.dataset.tab)"
        )
        assert visible_tabs == expected_tabs

        for tab in expected_tabs:
            page.locator(f'.tab-btn[data-tab="{tab}"]').click()
            page.wait_for_function(
                f"() => document.querySelector('.tab-btn.active')?.dataset.tab === '{tab}'",
                timeout=4_000,
            )
            _assert_active_panel(page, tab)
            page.wait_for_function(
                """tab => {
                    const el = document.querySelector(`.tab-btn[data-tab="${tab}"]`);
                    if (!el) return false;
                    const r = el.getBoundingClientRect();
                    return r.left >= -1 && r.right <= window.innerWidth + 1;
                }""",
                arg=tab,
                timeout=2_000,
            )

        assert page.locator("#stat-chunks").inner_text() == "1 chunk"
        assert page.locator("#stat-sources").inner_text() == "1 source"

        if viewport[0] <= 390:
            page.locator('.tab-btn[data-tab="home"]').click()
            page.wait_for_function(
                "() => document.querySelector('#home-activity-map .activity-map-frame')"
            )
            assert page.evaluate(
                "() => document.documentElement.scrollWidth <= window.innerWidth + 1"
            )

        page.locator('.tab-btn[data-tab="search"]').focus()
        before_history = page.evaluate("() => history.length")
        page.keyboard.press("ArrowRight")
        assert page.locator(".tab-btn.active").get_attribute("data-tab") == "sources"
        page.keyboard.press("Home")
        assert page.locator(".tab-btn.active").get_attribute("data-tab") == "home"
        assert page.evaluate("() => history.length") == before_history

        page.locator('.tab-btn[data-tab="search"]').click()
        page.locator("#filter-toggle").click()
        assert not page.locator("#search-filters").evaluate("el => el.hidden")
        page.locator("#adv-toggle").click()
        assert not page.locator("#search-advanced").evaluate("el => el.hidden")
        page.locator("#date-range-preset").select_option("custom")
        assert not page.locator("#date-range-custom").evaluate("el => el.hidden")
        page.fill("#search-input", "smoke")
        page.locator("#search-btn").click()
        page.wait_for_function(
            "() => document.querySelectorAll('#results-list .result-item').length > 0",
            timeout=5_000,
        )
        assert page.locator("#results-list .result-debug-meta").first.evaluate(
            "el => getComputedStyle(el).display === 'none'"
        )
        assert (
            page.locator("#results-list .results-debug-details summary").inner_text()
            == "Advanced details"
        )

        page.locator('.tab-btn[data-tab="sources"]').click()
        assert page.locator(".sources-vendor-tab").count() >= 1
        if viewport[0] <= 390:
            assert not page.locator(".sources-layout").evaluate(
                "el => el.classList.contains('mobile-detail')"
            )
            # Exercise the row's production Enter handler as part of the
            # keyboard-only acceptance path. This also avoids racing the
            # source tree's async status repaint with a stale pointer point.
            source_row = page.locator("#sources-list .source-item").first
            source_row.focus()
            page.keyboard.press("Enter")
        page.wait_for_function(
            "() => document.querySelector('#chunks-browser .chunks-browser-header .file-path')?.textContent.includes('notes.md')",
            timeout=5_000,
        )
        if viewport[0] <= 390:
            assert page.locator(".sources-layout").evaluate(
                "el => el.classList.contains('mobile-detail')"
            )
            assert page.locator(".sources-mobile-back").is_visible()
            page.locator(".sources-mobile-back").click()
            assert not page.locator(".sources-layout").evaluate(
                "el => el.classList.contains('mobile-detail')"
            )
        for vendor in ("user", "claude", "openai"):
            tab = page.locator(f'.sources-vendor-tab[data-vendor="{vendor}"]')
            if tab.count():
                tab.click()
                assert tab.get_attribute("aria-selected") == "true"

        page.locator('.tab-btn[data-tab="context-gateway"]').click()
        # ADR-0026 D-F flip: Simple is the production default and hides the gateway
        # section nav on the Overview — switch to Advanced so the nested-nav walk
        # below reaches each section (as a user would via the toggle).
        page.evaluate("() => _ctxSetSimpleMode(false)")
        gateway_sections = [
            "ctx-overview",
            "ctx-skills",
            "ctx-commands",
            "ctx-agents",
            "hooks-sync",
        ]
        for section in gateway_sections:
            nav = _ensure_nav_visible(page, "#tab-context-gateway", section)
            assert nav.count() == 1
            assert nav.first.is_visible()
            nav.first.click()
            assert page.locator(f"#settings-{section}").evaluate(
                "el => el.classList.contains('active')"
            )

        page.locator('.tab-btn[data-tab="settings"]').click()
        settings_sections = ["config", "namespaces", "dedup", "decay", "export", "reset"]
        if mode == "dev":
            settings_sections += [
                "harness-sessions",
                "harness-scratch",
                "harness-procedures",
                "harness-health",
            ]
        for section in settings_sections:
            nav = _ensure_nav_visible(page, "#tab-settings", section)
            assert nav.count() == 1
            assert nav.first.is_visible()
            nav.first.click()
            assert page.locator(f"#settings-{section}").evaluate(
                "el => el.classList.contains('active')"
            )

        if mode == "prod":
            assert page.locator('#tab-settings [data-ui-tier="dev"]:visible').count() == 0
            assert page.locator('#tab-context-gateway [data-ui-tier="dev"]:visible').count() == 0
        else:
            assert page.locator('[data-ui-tier="dev"]:visible').count() > 0

        page.locator("#settings-btn").click()
        assert not page.locator("#settings-modal").evaluate("el => el.hidden")
        page.keyboard.press("Escape")
        assert page.locator("#settings-modal").evaluate("el => el.hidden")

        theme_before = page.locator("html").get_attribute("data-theme")
        page.locator("#theme-toggle").click()
        assert page.locator("html").get_attribute("data-theme") != theme_before
        page.locator("#help-toggle").click()
        assert page.locator("#help-toggle").get_attribute("aria-pressed") in {"true", "false"}

        page.locator('.tab-btn[data-tab="index"]').click()
        for index_mode in ("folder", "upload", "compose"):
            btn = page.locator(f'.index-mode-toggle [data-mode="{index_mode}"]')
            btn.click()
            assert btn.get_attribute("aria-selected") == "true"
            assert page.locator(f'[data-mode-guide="{index_mode}"]').is_visible()
            if viewport[0] <= 390 and index_mode == "folder":
                checkbox_metrics = page.locator("#index-recursive").evaluate(
                    """el => ({
                        inputHeight: el.getBoundingClientRect().height,
                        labelHeight: el.closest('label').getBoundingClientRect().height,
                    })"""
                )
                assert checkbox_metrics["inputHeight"] <= 24
                assert checkbox_metrics["labelHeight"] >= 44

        page.locator('.tab-btn[data-tab="tags"]').click()
        assert page.locator("#tags-search").is_visible()
        page.locator('.tab-btn[data-tab="timeline"]').click()
        assert page.locator("#tl-days").is_visible()
        assert page.locator("#tl-view-chunks").is_visible()
        assert page.locator("#tl-view-files").is_visible()

        captured_errors = page.evaluate("() => window.__smokeErrors || []")

    assert captured_errors == []
    assert page_errors == []


@pytest.mark.parametrize("route", ["#home", "#search", "#settings"])
def test_direct_hash_entry_populates_header_stats(page, route: str) -> None:
    """Direct entry routes keep global header stats hydrated."""
    page.set_viewport_size({"width": 390, "height": 844})
    _install_error_capture(page)
    _install_smoke_stubs(page, "prod")

    with _web_server("prod") as base_url:
        page.goto(f"{base_url}/{route}", wait_until="domcontentloaded")
        page.wait_for_function(
            "() => document.querySelector('#stat-chunks')?.textContent === '1 chunk'",
            timeout=5_000,
        )
        page.wait_for_function(
            "() => document.querySelector('#stat-sources')?.textContent === '1 source'",
            timeout=5_000,
        )

        tab = route.lstrip("#")
        _assert_active_panel(page, tab)

        captured_errors = page.evaluate("() => window.__smokeErrors || []")

    assert captured_errors == []
