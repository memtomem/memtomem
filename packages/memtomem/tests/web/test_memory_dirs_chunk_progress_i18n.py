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
    # ``I18N.init()`` is kicked off from app.js's DOMContentLoaded
    # handler and finishes asynchronously with a tail
    # ``_lang = _detect()`` + ``langchange`` dispatch. If we call
    # ``setLang`` before init has completed, init's tail can clobber
    # our ``_lang`` during the ``_load`` await window — observable on
    # CI runners where ``navigator.language`` defaults to en-US (init
    # resolves to 'en' and overwrites our 'ko'). Local Macs with
    # ko-KR converge on the same value, hiding the race.
    #
    # ``document.documentElement.lang`` is not a usable "init done"
    # signal because index.html ships with ``<html lang="en">``
    # statically — the attribute is truthy before init even starts.
    # ``add_init_script`` runs before any page script, so the
    # listener it installs is guaranteed to catch init's tail
    # ``langchange`` dispatch.
    page.add_init_script(
        """
        window.__i18nInitFired = false;
        window.addEventListener(
          'langchange',
          () => { window.__i18nInitFired = true; },
          { once: true },
        );
        """
    )
    page.goto(mm_web_url)
    page.wait_for_function("() => window.__i18nInitFired === true", timeout=5000)

    result = page.evaluate(
        """
        async () => {
          // Force a known start locale so the test does not depend on
          // the runner's ``navigator.language`` / ``localStorage``
          // state. ``setLang`` is async — await so the locale cache
          // is populated before we proceed.
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
