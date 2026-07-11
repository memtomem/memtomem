"""Browser regression tests for Sources-tab reindex retry state.

The reported flow is: Sources tab reindex hits a model-load failure, the
model-readiness banner says "Model failed to load", and later reindex attempts
do not start. The backend active counter already has Python coverage; this
spec pins the client-side retry bridge that rechecks server truth before
honouring a stale ``STATE.indexing`` flag.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.browser


def _ok(route, payload) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))


def _install_default_stubs(page) -> None:
    page.route("**/api/**", lambda r: _ok(r, {}))
    page.route("**/api/system/ui-mode", lambda r: _ok(r, {"mode": "prod"}))
    page.route("**/api/system/model-readiness", lambda r: _ok(r, {"ready": True}))
    page.route("**/api/sources?**", lambda r: _ok(r, {"sources": []}))
    page.route("**/api/stats", lambda r: _ok(r, {}))
    page.route(
        "**/api/memory-dirs/status",
        lambda r: _ok(
            r,
            {
                "dirs": [
                    {
                        "path": "/tmp/memories",
                        "exists": True,
                        "file_count": 1,
                        "source_file_count": 0,
                        "chunk_count": 0,
                        "category": "user",
                        "provider": "user",
                    }
                ]
            },
        ),
    )


def test_sources_reindex_retries_when_local_indexing_flag_is_stale(page, mm_web_url: str) -> None:
    """Stale client ``STATE.indexing`` must not block a new Sources reindex.

    The server reports idle via ``/api/indexing/active``; ``mdReindexOne`` must
    clear the local flag and proceed far enough to construct the SSE stream.
    """
    _install_default_stubs(page)
    page.route("**/api/indexing/active", lambda r: _ok(r, {"active": False}))
    page.goto(mm_web_url)

    constructed = page.evaluate(
        """async () => {
          // Fake the CSRF-protected POST SSE transport
          // (``app.js:fetchIndexStream``). ``mdReindexOne`` no longer opens an
          // ``EventSource`` with the path in the query string — it POSTs
          // ``fetchIndexStream({ path, ... })``. Capture the request body and
          // keep the stream in-flight (the returned promise never resolves) so
          // the post-preflight indexing / disabled-button state stays
          // observable instead of being torn down by cleanup.
          let streamBody = null;
          window.fetchIndexStream = (body, opts = {}) => {
            streamBody = body;
            return new Promise(() => {});
          };
          STATE.indexing = true;

          const group = document.createElement('details');
          group.className = 'source-group';
          const meta = document.createElement('span');
          meta.className = 'source-group-stats';
          meta.textContent = '1/1 files, 0 chunks';
          group.appendChild(meta);
          const btn = document.createElement('button');
          btn.textContent = 'Index';
          group.appendChild(btn);
          document.body.appendChild(group);

          // Do NOT await — the transport promise never resolves. Poll until the
          // stale ``STATE.indexing`` preflight clears and the POST is issued.
          mdReindexOne('/tmp/memories', btn);
          for (let i = 0; i < 100 && streamBody === null; i++) {
            await new Promise((r) => setTimeout(r, 5));
          }

          return {
            streamPath: streamBody ? streamBody.path : null,
            stateIndexing: STATE.indexing,
            buttonDisabled: btn.disabled,
          };
        }"""
    )

    assert constructed["streamPath"] == "/tmp/memories"
    assert constructed["stateIndexing"] is True
    assert constructed["buttonDisabled"] is True
