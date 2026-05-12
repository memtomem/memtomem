"""Browser regression tests for the Memory Dirs reindex ``chunk_progress``
consumer (issue #660).

PR #658 silently shipped a no-op because the consumer was wired against
legacy ``.memory-dirs-item`` classes that have been dead since #568. The
gap was caught by a Playwright probe in #662 / #665, not by review. This
file codifies that probe so the same regression class fails in CI.

The asserted contract:

* ``chunk_progress`` events flip the ``.source-group-stats`` badge to
  ``file.md — N/M chunks`` (``common.file_chunk_progress`` template).
* The next ``progress`` (file boundary) event resets the badge to its
  original text so a stream like ``[big, small, ...]`` does not leave
  the big file's chunk label stuck through the rest of the run
  (#654 residue case).
* ``complete`` cleans up — badge restored, button restored, ``STATE.indexing``
  cleared.
* Sub-threshold runs (no ``chunk_progress`` events) leave the badge at
  its original text throughout, so the server-side ``progress_threshold``
  gate keeps its UI invariant.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.browser


def _ok(route, payload) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))


def _install_default_stubs(page) -> None:
    """Memory-dirs scoped stubs — mirrors ``test_sources_reindex_retry.py``.

    The conftest ``install_default_stubs`` helper does not stub
    ``/api/memory-dirs/status`` (its callers don't need the row to
    render); these tests do, so we keep a local helper per the
    explicit carve-out in ``conftest.py``.
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


def _goto_after_i18n_ready(page, mm_web_url: str) -> None:
    """Navigate once the locale cache can render chunk progress labels."""
    page.goto(mm_web_url)
    page.wait_for_function(
        """
        () => typeof t === 'function'
          && t('common.file_chunk_progress', {
            file: 'probe.md',
            done: 1,
            total: 2,
          }).includes('probe.md')
        """,
        timeout=5000,
    )


def _driver(event_script_body: str) -> str:
    """Build a ``page.evaluate`` script that sets up a fake EventSource,
    drives ``mdReindexOne`` against a synthetic ``.source-group`` row,
    and runs the per-test event injection ``event_script_body``.

    The body receives ``emit``, ``meta``, and ``btn`` in scope and must
    assign its observations to ``observed``. ``page.evaluate`` does not
    pass functions across the bridge — only JSON-serialisable data —
    so we interpolate the body as source text rather than passing it
    as an argument.
    """
    return (
        """
  async () => {
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

    // Kick the reindex but do NOT await — it only resolves on stream
    // close. Yield once so the SSE wiring is up.
    const reindexPromise = mdReindexOne('/tmp/memories', btn);
    await new Promise((r) => setTimeout(r, 0));

    const emit = (event) => {
      esInstance.onmessage({ data: JSON.stringify(event) });
    };

    const observed = {};
"""
        + event_script_body
        + """
    await reindexPromise;
    return {
      ...observed,
      finalMeta: meta.textContent,
      finalBtn: btn.textContent,
      btnDisabled: btn.disabled,
      stateIndexing: STATE.indexing,
      sseClosed: esInstance.closed,
    };
  }
"""
    )


def test_chunk_progress_updates_meta_badge_and_resets_on_file_boundary(
    page, mm_web_url: str
) -> None:
    """Happy-path: a ``chunk_progress`` event flips the badge; the next
    ``progress`` resets it. Would have failed CI on PR #658.
    """
    _install_default_stubs(page)
    _goto_after_i18n_ready(page, mm_web_url)

    result = page.evaluate(
        _driver(
            """
    // Final tick (chunks_done >= chunks_total) bypasses the 100ms
    // throttle in ``makeChunkProgressRenderer``, so this single event
    // is enough to assert the DOM write.
    emit({
      type: 'chunk_progress',
      file: '/tmp/memories/BIG.md',
      chunks_done: 64,
      chunks_total: 64,
    });
    observed.duringChunk = meta.textContent;

    emit({ type: 'progress', files_done: 1, files_total: 2 });
    observed.afterBoundary = meta.textContent;

    emit({ type: 'complete', indexed_chunks: 64, errors: [] });
"""
        )
    )

    # The KO locale default ships in en.json + ko.json with " — " and
    # "chunks"/"청크" as the literal between the file and N/M. We assert
    # on the structural fragments so the test is locale-agnostic.
    assert "BIG.md" in result["duringChunk"]
    assert "64/64" in result["duringChunk"]
    assert result["afterBoundary"] == "0/2 files, 0 chunks"
    assert result["finalMeta"] == "0/2 files, 0 chunks"
    assert result["finalBtn"] == "Index"
    assert result["btnDisabled"] is False
    assert result["stateIndexing"] is False
    assert result["sseClosed"] is True


def test_threshold_below_run_leaves_meta_badge_at_original(page, mm_web_url: str) -> None:
    """Sub-threshold run: only ``progress`` events arrive, no
    ``chunk_progress``. The badge must stay at its original text — this
    pins the server-side ``progress_threshold`` gate's UI invariant.
    """
    _install_default_stubs(page)
    _goto_after_i18n_ready(page, mm_web_url)

    result = page.evaluate(
        _driver(
            """
    observed.snapshots = [];
    emit({ type: 'progress', files_done: 1, files_total: 2 });
    observed.snapshots.push(meta.textContent);
    emit({ type: 'progress', files_done: 2, files_total: 2 });
    observed.snapshots.push(meta.textContent);
    emit({ type: 'complete', indexed_chunks: 4, errors: [] });
"""
        )
    )

    assert result["snapshots"] == ["0/2 files, 0 chunks", "0/2 files, 0 chunks"]
    assert result["finalMeta"] == "0/2 files, 0 chunks"
    assert result["sseClosed"] is True
    assert result["stateIndexing"] is False
