"""Browser tests for the redaction-blocked confirm-and-retry UX (issue #785).

PR #784 unified the trust-boundary redaction guard across every server-side
write surface. The four mutating web routes signal a hit two different ways:

* JSON routes (``POST /api/add``, ``PATCH /api/chunks/{id}``,
  ``POST /api/scratch/{key}/promote``) return ``HTTP 403`` with
  ``{"detail": {"detail": "redaction_blocked", "hits": N, "surface": L}}``.
* ``POST /api/upload`` returns ``HTTP 200`` with per-file
  ``error="redaction_blocked (hits=N)"`` strings (system.py:1161) and accepts
  ``?force_unsafe=true`` as a query param to bypass for the whole batch.

These specs pin the SPA-side wiring: ``api()`` parses the structured 403,
``apiWithRedactionRetry()`` shows the confirm dialog and re-issues with
``force_unsafe=true`` in the body, ``uploadFilesWithRedactionRetry()`` does
the analogous flow for the multipart endpoint with the query param.

Both specs route-stub the backend; the metric-counter assertion the issue
body asks for (``mem_add_redaction_stats[bypassed][by_tool]``) needs a real
backend fixture and is tracked as a follow-up.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.browser


def _install_default_stubs(page) -> None:
    """Stub every endpoint the SPA hits during boot.

    Mirrors the catch-all pattern in ``test_tag_filter_mutation.py``;
    ``page.route`` resolves last-registered-wins so the catch-all goes
    first and specific overrides go last.
    """

    def _ok(route, payload):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/**", lambda r: _ok(r, {}))
    page.route("**/api/system/ui-mode", lambda r: _ok(r, {"mode": "prod"}))
    page.route("**/api/system/model-readiness", lambda r: _ok(r, {"ready": True}))
    page.route("**/api/sources", lambda r: _ok(r, {"sources": []}))
    page.route("**/api/namespaces", lambda r: _ok(r, {"namespaces": []}))
    page.route("**/api/stats", lambda r: _ok(r, {}))
    # Empty privacy patterns disables the client-side pre-check so the
    # spec exercises only the server-driven 403 path. Without this, any
    # ``sk-…`` content would trip the pre-check dialog *before* the
    # request leaves the browser and the server stub would never fire.
    page.route("**/api/privacy/patterns", lambda r: _ok(r, {"patterns": []}))


def test_api_add_403_triggers_confirm_and_retries_with_force_unsafe(page, mm_web_url: str) -> None:
    """``POST /api/add`` 403 → confirm dialog naming hits/surface → click
    proceed → second POST carries ``force_unsafe: true`` in the body and
    the success toast renders.

    The first response uses the FastAPI-nested shape
    ``{"detail": {"detail": "redaction_blocked", "hits": 2, "surface": ...}}``
    that the backend actually returns (system.py:1232-1240); a flat
    ``{"detail": "redaction_blocked"}`` would silently miss the parser
    branch in ``api()``, which is the bug this spec exists to guard.
    """
    _install_default_stubs(page)

    calls: list[dict] = []

    def _add_handler(route):
        # Capture every request to /api/add so the test can assert the
        # *first* call did not carry force_unsafe. The retry request is
        # asserted via ``page.expect_request`` to avoid a race with the
        # route handler's append on the Python side.
        body = route.request.post_data_json or {}
        calls.append(body)
        if len(calls) == 1:
            route.fulfill(
                status=403,
                content_type="application/json",
                body=json.dumps(
                    {
                        "detail": {
                            "detail": "redaction_blocked",
                            "hits": 2,
                            "surface": "web_api_add",
                        }
                    }
                ),
            )
        else:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "indexed_chunks": 1,
                        "file": "/home/u/.memtomem/memories/x.md",
                        "namespace": "default",
                    }
                ),
            )

    page.route("**/api/add", _add_handler)

    page.goto(mm_web_url)
    # Switch Index → Compose mode to reveal the Add form.
    page.locator("#tabbtn-index").click()
    page.locator("#index-mode-compose").click()
    page.locator("#add-content").fill("sk-ant-test-fake-1234567890abcdef")
    page.locator("#add-btn").click()

    # The confirm dialog should appear with the matched-pattern count and
    # the localized surface label. The default locale is English and the
    # surface key ``surface.web_api_add`` resolves to "Add memory".
    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    message = page.locator("#confirm-message").text_content() or ""
    assert "2" in message, f"hit count missing from dialog: {message!r}"
    assert "Add memory" in message, f"surface label missing from dialog: {message!r}"

    # ``page.expect_request`` yields to Playwright's event loop while
    # waiting; a Python-side ``time.sleep`` would block the loop and the
    # route handler would never run. The matcher fires on the *retry*
    # request because the first request already finished before this
    # block opened. Asserting directly on ``retry_info.value`` (Playwright's
    # captured request) avoids a race between the request-dispatched event
    # and the route handler's ``calls.append`` running on the Python side.
    with page.expect_request("**/api/add", timeout=5_000) as retry_info:
        page.locator("#confirm-ok-btn").click()
    retry_request = retry_info.value
    assert retry_request.post_data_json.get("force_unsafe") is True, (
        f"retry must carry force_unsafe=true, got {retry_request.post_data_json!r}"
    )
    assert calls[0].get("force_unsafe") is not True, "first call should not carry force_unsafe"

    # The dialog must dismiss on confirm — guards against a regression where
    # ``showConfirm`` resolves but the modal stays visible (would block the
    # next interaction without surfacing as a request-level failure).
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    # Success toast for the audited bypass should render.
    page.wait_for_selector(
        "#toast-container .toast",
        timeout=2_000,
    )


def test_api_upload_per_file_redaction_triggers_batch_retry_with_query_param(
    page, mm_web_url: str, tmp_path: Path
) -> None:
    """``POST /api/upload`` per-file ``redaction_blocked (hits=N)`` →
    confirm dialog with summed hits → retry sends ``?force_unsafe=true``.

    The upload route's response shape diverges from the JSON routes (200
    + embedded string error vs structured 403); this spec pins that
    ``uploadFilesWithRedactionRetry`` correctly parses the string-shape
    error and adds the bypass via the URL query param, not the body.
    """
    _install_default_stubs(page)

    calls: list[str] = []

    def _upload_handler(route):
        calls.append(route.request.url)
        if len(calls) == 1:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "files": [
                            {
                                "filename": "secret.md",
                                "indexed_chunks": 0,
                                "error": "redaction_blocked (hits=3)",
                            }
                        ],
                        "total_indexed": 0,
                    }
                ),
            )
        else:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "files": [
                            {
                                "filename": "secret.md",
                                "indexed_chunks": 1,
                                "path": "/home/u/.memtomem/uploads/secret.md",
                            }
                        ],
                        "total_indexed": 1,
                    }
                ),
            )

    # Glob ``**/api/upload**`` would also match ``/api/uploads/usage`` (the
    # GET fired on Index-tab activation), splitting the call counter
    # across both endpoints. A regex anchored on ``/api/upload(?:?...)?``
    # excludes the plural ``uploads`` path.
    page.route(re.compile(r"/api/upload(?:\?.*)?$"), _upload_handler)

    # Real file on disk so ``set_input_files`` has something to attach.
    fixture = tmp_path / "secret.md"
    fixture.write_text("# Secret notes\n\nsk-ant-test-fake-1234567890abcdef\n")

    page.goto(mm_web_url)
    page.locator("#tabbtn-index").click()
    page.locator("#index-mode-upload").click()
    page.locator("#upload-input").set_input_files(str(fixture))
    page.locator("#upload-btn").click()

    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    message = page.locator("#confirm-message").text_content() or ""
    assert "3" in message, f"hit count missing from dialog: {message!r}"
    assert "File upload" in message, f"surface label missing from dialog: {message!r}"

    with page.expect_request(
        re.compile(r"/api/upload\?force_unsafe=true$"), timeout=5_000
    ) as retry_info:
        page.locator("#confirm-ok-btn").click()
    retry_request = retry_info.value
    assert "force_unsafe=true" in retry_request.url, (
        f"retry upload must carry ?force_unsafe=true: {retry_request.url!r}"
    )
    assert "force_unsafe=true" not in calls[0]

    # Dialog must dismiss on confirm (mirrors the JSON-route spec above).
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
