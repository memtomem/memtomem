"""Browser regression test for locale toggling mid-stream during a
Memory Dirs reindex (issue #660 — slice 3 of the priority-bump comment).

``makeChunkProgressRenderer`` (``chunk-progress.js``) calls
``t(formatKey, ...)`` on every emit, so a ``setLang`` between two
``chunk_progress`` events must flip the next badge render to the new
locale's ``common.file_chunk_progress`` template (KO ``청크`` ↔
EN ``chunks``). The contract is implicit — there is no test of it
today, and the renderer extraction (#659 / PR #959) made the call-site
shared between Index tab + Memory Dirs, so a regression here would
silently span two surfaces.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.browser


def _ok(route, payload) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))


def _install_default_stubs(page) -> None:
    """Memory-dirs scoped stubs — mirrors ``test_memory_dirs_chunk_progress``.

    Locales (``/locales/*.json``) are intentionally NOT intercepted —
    the static file routes serve the real JSON so this spec asserts
    against the shipping ``common.file_chunk_progress`` templates,
    not a synthetic stub.
    """
    page.route("**/api/**", lambda r: _ok(r, {}))
    page.route("**/api/system/ui-mode", lambda r: _ok(r, {"mode": "prod"}))
    page.route("**/api/system/model-readiness", lambda r: _ok(r, {"ready": True}))
    page.route("**/api/sources?**", lambda r: _ok(r, {"sources": []}))
    page.route("**/api/stats", lambda r: _ok(r, {}))
    page.route("**/api/indexing/active", lambda r: _ok(r, {"active": False}))
    page.route(
        "**/api/memory-dirs/status",
        lambda r: _ok(
            r,
            {
                "dirs": [
                    {
                        "path": "/tmp/memories",
                        "exists": True,
                        "file_count": 2,
                        "source_file_count": 0,
                        "chunk_count": 0,
                        "category": "user",
                        "provider": "user",
                    }
                ]
            },
        ),
    )


def test_locale_toggle_mid_stream_flips_chunk_progress_template(page, mm_web_url: str) -> None:
    """A ``setLang`` between two ``chunk_progress`` events must flip the
    next badge render to the new locale's template.

    Pins the renderer extraction (#659 / PR #959) — ``t(formatKey, ...)``
    is called on every emit, so locale changes propagate without a
    page reload. A regression that hoisted the template to
    construction-time would silently freeze the badge in the start
    locale.
    """
    _install_default_stubs(page)
    page.goto(mm_web_url)

    result = page.evaluate(
        """
        async () => {
          // Force a known start locale so the test does not depend on
          // the browser's navigator.language fallback (which varies
          // across CI runners + local dev). I18N.setLang is async —
          // await so the locale cache is populated before we proceed.
          await I18N.setLang('ko');

          let esInstance = null;
          class FakeEventSource {
            constructor(url) {
              this.url = String(url);
              this.onmessage = null;
              this.onerror = null;
              this.closed = false;
              esInstance = this;
            }
            close() { this.closed = true; }
          }
          window.EventSource = FakeEventSource;

          const group = document.createElement('details');
          group.className = 'source-group';
          const meta = document.createElement('span');
          meta.className = 'source-group-stats';
          meta.textContent = '0/2 files, 0 chunks';
          group.appendChild(meta);
          const btn = document.createElement('button');
          btn.textContent = 'Index';
          group.appendChild(btn);
          document.body.appendChild(group);

          const reindexPromise = mdReindexOne('/tmp/memories', btn);
          await new Promise((r) => setTimeout(r, 0));

          const emit = (event) => {
            esInstance.onmessage({ data: JSON.stringify(event) });
          };

          // Final-tick (chunks_done >= chunks_total) bypasses the 100ms
          // throttle, so a single event renders the badge synchronously.
          emit({
            type: 'chunk_progress',
            file: '/tmp/memories/A.md',
            chunks_done: 64,
            chunks_total: 64,
          });
          const koBadge = meta.textContent;
          const koLang = I18N.lang();

          // Switch locale mid-stream. ``setLang`` is async (it may
          // fetch the locale JSON); await so the cache is hot before
          // the next emit.
          await I18N.setLang('en');

          emit({
            type: 'chunk_progress',
            file: '/tmp/memories/B.md',
            chunks_done: 32,
            chunks_total: 32,
          });
          const enBadge = meta.textContent;
          const enLang = I18N.lang();

          emit({ type: 'complete', indexed_chunks: 96, errors: [] });
          await reindexPromise;

          return { koBadge, koLang, enBadge, enLang };
        }
        """
    )

    assert result["koLang"] == "ko"
    assert "A.md" in result["koBadge"]
    assert "64/64" in result["koBadge"]
    assert "청크" in result["koBadge"]
    assert "chunks" not in result["koBadge"]

    assert result["enLang"] == "en"
    assert "B.md" in result["enBadge"]
    assert "32/32" in result["enBadge"]
    assert "chunks" in result["enBadge"]
    assert "청크" not in result["enBadge"]
