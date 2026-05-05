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


def test_api_upload_mixed_batch_retries_only_blocked_file(
    page, mm_web_url: str, tmp_path: Path
) -> None:
    """Mixed batch (1 clean + 1 blocked) → retry FormData carries **only**
    the blocked filename, never the clean one (issue #803).

    Before the scope-reduction fix, ``uploadFilesWithRedactionRetry``
    re-sent the original FormData on retry; the server's ``_{mtime_ns}``
    collision suffix at system.py:1121 then silently double-indexed the
    clean file under two filenames. This spec pins the narrowed retry:
    the blocked entry is sent with ``?force_unsafe=true`` while the
    clean entry stays out of the request body, so disk and index reflect
    one copy of each file regardless of mixed-batch confirms.
    """
    _install_default_stubs(page)

    calls: list[tuple[str, str]] = []

    def _upload_handler(route):
        # Capture URL + raw multipart body so the test can inspect both
        # the bypass query-param and the filenames present in each call.
        calls.append((route.request.url, route.request.post_data or ""))
        if len(calls) == 1:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "files": [
                            {
                                "filename": "clean.md",
                                "indexed_chunks": 2,
                                "path": "/home/u/.memtomem/uploads/clean.md",
                            },
                            {
                                "filename": "secret.md",
                                "indexed_chunks": 0,
                                "error": "redaction_blocked (hits=3)",
                            },
                        ],
                        "total_indexed": 2,
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

    page.route(re.compile(r"/api/upload(?:\?.*)?$"), _upload_handler)

    clean = tmp_path / "clean.md"
    clean.write_text("# Clean notes\n\nNo secrets here.\n")
    secret = tmp_path / "secret.md"
    secret.write_text("# Secret notes\n\nsk-ant-test-fake-1234567890abcdef\n")

    page.goto(mm_web_url)
    page.locator("#tabbtn-index").click()
    page.locator("#index-mode-upload").click()
    page.locator("#upload-input").set_input_files([str(clean), str(secret)])
    page.locator("#upload-btn").click()

    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    message = page.locator("#confirm-message").text_content() or ""
    # Hit count is summed over blocked rows only — clean.md contributed 0.
    assert "3" in message, f"hit count missing from dialog: {message!r}"

    with page.expect_request(
        re.compile(r"/api/upload\?force_unsafe=true$"), timeout=5_000
    ) as retry_info:
        page.locator("#confirm-ok-btn").click()
    retry_request = retry_info.value

    # Core invariant: retry body must contain the blocked filename and
    # *not* the clean one. The first call carried both, so this asserts a
    # genuine narrowing rather than coincidence. Multipart bodies encode
    # ``filename="<basename>"`` per part (RFC 7578); a simple substring
    # check is sufficient and avoids parsing the boundary.
    retry_body = retry_request.post_data or ""
    assert 'filename="secret.md"' in retry_body, (
        f"retry must include the blocked file: {retry_body[:500]!r}"
    )
    assert 'filename="clean.md"' not in retry_body, (
        f"retry must NOT re-send the already-persisted clean file (issue #803): "
        f"{retry_body[:500]!r}"
    )
    # First call carried both — sanity-check that the assertion above is
    # not vacuously true because clean.md never made it into any request.
    assert 'filename="clean.md"' in calls[0][1], (
        "first call should have included clean.md; route stub may be misconfigured"
    )

    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    # Final UI must show one row per input file (clean from first pass,
    # secret from retry merged at its original index). Two rows with no
    # duplication of clean.md is the user-visible promise of the fix.
    page.wait_for_selector("#upload-result .upload-result-row", timeout=2_000)
    rows = page.locator("#upload-result .upload-result-row").all_text_contents()
    assert len(rows) == 2, f"expected 2 result rows, got {len(rows)}: {rows!r}"
    assert sum("clean.md" in r for r in rows) == 1, (
        f"clean.md must appear exactly once after merge: {rows!r}"
    )
    assert sum("secret.md" in r for r in rows) == 1, (
        f"secret.md must appear exactly once after merge: {rows!r}"
    )


def test_api_upload_retry_with_persistent_error_row_does_not_claim_bypassed(
    page, mm_web_url: str, tmp_path: Path
) -> None:
    """Retry returns the same row count but the row still carries
    ``error`` → no ``toast.redaction_bypassed`` (the audit toast lies if
    the bypass didn't actually write), instead the partial-failure toast
    surfaces the succeeded/total counts.

    This is the per-file-error half of the bypass validation: the server
    honored ``?force_unsafe=true`` at the route level (HTTP 200), but the
    write itself reports a different failure (a non-redaction validation
    error, or a class of redaction the bypass query-param didn't cover —
    the SPA can't introspect why, just whether the row landed). Without
    the validation, the operator sees "entry written" while the per-file
    list shows the file unwritten.
    """
    _install_default_stubs(page)

    def _upload_handler(route):
        if "force_unsafe=true" in route.request.url:
            # Retry — server claims success at the HTTP layer but the
            # per-file row still has an error. Same row count as the
            # original blocked set (1) so the length check passes; the
            # error-presence check is what catches this.
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "files": [
                            {
                                "filename": "secret.md",
                                "indexed_chunks": 0,
                                "error": "write_failed: disk full",
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
                                "indexed_chunks": 0,
                                "error": "redaction_blocked (hits=3)",
                            }
                        ],
                        "total_indexed": 0,
                    }
                ),
            )

    page.route(re.compile(r"/api/upload(?:\?.*)?$"), _upload_handler)

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
    with page.expect_request(re.compile(r"/api/upload\?force_unsafe=true$"), timeout=5_000):
        page.locator("#confirm-ok-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    # Wait for *some* toast to appear so the assertions below race-free.
    page.wait_for_selector("#toast-container .toast", timeout=2_000)
    # The success-bypass toast must not fire — that's the misleading
    # claim this test exists to catch.
    bypass_toasts = page.locator("#toast-container .toast", has_text="Bypassed redaction guard")
    assert bypass_toasts.count() == 0, (
        "retry with persistent per-file error must not emit the success bypass toast"
    )
    # The partial toast surfaces 0/1 succeeded so the operator knows the
    # write didn't land despite the confirm.
    partial_toasts = page.locator("#toast-container .toast", has_text="Bypass partial")
    assert partial_toasts.count() == 1, (
        f"partial-retry must emit toast.redaction_bypass_partial, saw {partial_toasts.count()}"
    )
    partial_text = partial_toasts.first.text_content() or ""
    assert "0" in partial_text and "1" in partial_text, (
        f"partial toast must include succeeded/total counts: {partial_text!r}"
    )
    # The audit-relevant promise: caller must not stack a generic "Upload
    # complete" success toast on top of the partial warning. ``showToast``
    # tags success-class messages with ``.toast-success`` (app.js:908), so
    # the upload-mode caller's success branch is detectable as a class
    # presence — independent of locale text. Without the caller-side
    # ``partial`` guard added in PR #805 follow-up, this assertion fails
    # because the success path runs unconditionally after retry.
    success_toasts = page.locator("#toast-container .toast.toast-success")
    assert success_toasts.count() == 0, (
        f"partial bypass must not emit a generic upload-complete success toast; "
        f"saw {success_toasts.count()}: "
        f"{[t.text_content() for t in success_toasts.all()]}"
    )


def test_api_upload_retry_with_truncated_row_count_does_not_claim_bypassed(
    page, mm_web_url: str, tmp_path: Path
) -> None:
    """Retry returns fewer rows than the narrowed FormData carried (e.g.,
    upstream truncation or a server-shape regression) → no
    ``toast.redaction_bypassed``, partial toast instead.

    Two blocked files go up on retry; server returns only one row. The
    merged ``data.files`` keeps the unmatched second row at its original
    ``redaction_blocked`` state, so the per-file UI is honest, but the
    aggregate toast must not claim the bypass succeeded.
    """
    _install_default_stubs(page)

    def _upload_handler(route):
        if "force_unsafe=true" in route.request.url:
            # Truncated retry — only one row back even though we sent two.
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "files": [
                            {
                                "filename": "a.md",
                                "indexed_chunks": 1,
                                "path": "/home/u/.memtomem/uploads/a.md",
                            }
                        ],
                        "total_indexed": 1,
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
                                "filename": "a.md",
                                "indexed_chunks": 0,
                                "error": "redaction_blocked (hits=2)",
                            },
                            {
                                "filename": "b.md",
                                "indexed_chunks": 0,
                                "error": "redaction_blocked (hits=1)",
                            },
                        ],
                        "total_indexed": 0,
                    }
                ),
            )

    page.route(re.compile(r"/api/upload(?:\?.*)?$"), _upload_handler)

    a = tmp_path / "a.md"
    a.write_text("# a\n\nsk-ant-test-fake-1234567890abcdef\n")
    b = tmp_path / "b.md"
    b.write_text("# b\n\nsk-ant-test-fake-2222222222222222\n")

    page.goto(mm_web_url)
    page.locator("#tabbtn-index").click()
    page.locator("#index-mode-upload").click()
    page.locator("#upload-input").set_input_files([str(a), str(b)])
    page.locator("#upload-btn").click()

    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    with page.expect_request(re.compile(r"/api/upload\?force_unsafe=true$"), timeout=5_000):
        page.locator("#confirm-ok-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    page.wait_for_selector("#toast-container .toast", timeout=2_000)
    bypass_toasts = page.locator("#toast-container .toast", has_text="Bypassed redaction guard")
    assert bypass_toasts.count() == 0, (
        "truncated retry response must not emit the success bypass toast"
    )
    partial_toasts = page.locator("#toast-container .toast", has_text="Bypass partial")
    assert partial_toasts.count() == 1, (
        f"truncated retry must emit toast.redaction_bypass_partial, saw {partial_toasts.count()}"
    )
    partial_text = partial_toasts.first.text_content() or ""
    # 1 of 2 succeeded — the single returned row was clean.
    assert "1" in partial_text and "2" in partial_text, (
        f"partial toast must include 1 of 2: {partial_text!r}"
    )
    # Same caller-side guard as the persistent-error spec: a partial
    # bypass must not be followed by the upload-mode caller's generic
    # success toast (would imply "all good" on top of the partial warning).
    success_toasts = page.locator("#toast-container .toast.toast-success")
    assert success_toasts.count() == 0, (
        f"truncated retry must not emit a generic upload-complete success toast; "
        f"saw {success_toasts.count()}"
    )


def test_api_upload_retry_with_extra_row_count_clamps_succeeded_count(
    page, mm_web_url: str, tmp_path: Path
) -> None:
    """Retry returns *more* rows than the narrowed FormData carried (server
    shape regression in the over-long direction) → ``succeededCount`` must
    be clamped to ``blockedRows.length`` so the partial toast cannot show
    impossible counts like "3 of 2 written", and the success bypass toast
    must still be suppressed because the row count mismatch breaks the
    positional alignment the merge depends on.

    This is the symmetric companion to the truncated-retry spec: same
    failure mode (length mismatch invalidates positional merge) from the
    other direction. The narrowed FormData carries one blocked file; the
    server returns three rows. Without the clamp at app.js:434 the
    partial toast would read "3 of 1 written" — nonsensical and worse,
    suggests over-success rather than the underlying contract violation.
    """
    _install_default_stubs(page)

    def _upload_handler(route):
        if "force_unsafe=true" in route.request.url:
            # Over-long retry — three rows back even though we sent one.
            # All three are clean (no error) so a naive
            # ``retryFiles.filter(r => !r.error).length`` would yield 3,
            # then format as "3 of 1" in the partial toast.
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
                            },
                            {
                                "filename": "ghost1.md",
                                "indexed_chunks": 1,
                                "path": "/home/u/.memtomem/uploads/ghost1.md",
                            },
                            {
                                "filename": "ghost2.md",
                                "indexed_chunks": 1,
                                "path": "/home/u/.memtomem/uploads/ghost2.md",
                            },
                        ],
                        "total_indexed": 3,
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
                                "indexed_chunks": 0,
                                "error": "redaction_blocked (hits=2)",
                            }
                        ],
                        "total_indexed": 0,
                    }
                ),
            )

    page.route(re.compile(r"/api/upload(?:\?.*)?$"), _upload_handler)

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
    with page.expect_request(re.compile(r"/api/upload\?force_unsafe=true$"), timeout=5_000):
        page.locator("#confirm-ok-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    page.wait_for_selector("#toast-container .toast", timeout=2_000)
    bypass_toasts = page.locator("#toast-container .toast", has_text="Bypassed redaction guard")
    assert bypass_toasts.count() == 0, (
        "over-long retry response must not emit the success bypass toast — "
        "row count mismatch breaks positional merge"
    )
    partial_toasts = page.locator("#toast-container .toast", has_text="Bypass partial")
    assert partial_toasts.count() == 1, (
        f"over-long retry must emit toast.redaction_bypass_partial, saw {partial_toasts.count()}"
    )
    partial_text = partial_toasts.first.text_content() or ""
    # Clamp pin: the displayed succeeded count must be ≤ total (1 here),
    # so the toast reads "1 of 1" rather than "3 of 1". Match the exact
    # "X of Y" fragment to avoid false positives from other digits in the
    # surrounding template.
    assert "1 of 1" in partial_text, (
        f"partial toast must clamp succeeded to total (1 of 1), got: {partial_text!r}"
    )
    assert "3 of 1" not in partial_text, (
        f"partial toast must not leak the unclamped server-side count: {partial_text!r}"
    )
    success_toasts = page.locator("#toast-container .toast.toast-success")
    assert success_toasts.count() == 0, (
        f"over-long retry must not emit a generic upload-complete success toast; "
        f"saw {success_toasts.count()}"
    )


def test_api_add_403_cancel_does_not_retry_or_emit_bypass_toast(page, mm_web_url: str) -> None:
    """``POST /api/add`` 403 → confirm dialog → click **Cancel** → no
    retry POST, no ``toast.redaction_bypassed``.

    The affirmative spec above pins that confirm triggers a retry; this
    spec pins the symmetric negative per
    ``feedback_pin_invert_symmetric_assertion.md`` — without it a
    regression where ``showConfirm`` always resolves ``true`` (or the
    helper's ``if (!ok) return null`` branch is dropped) would still
    pass the affirmative assertion.
    """
    _install_default_stubs(page)

    calls: list[dict] = []

    def _add_handler(route):
        body = route.request.post_data_json or {}
        calls.append(body)
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

    page.route("**/api/add", _add_handler)

    page.goto(mm_web_url)
    page.locator("#tabbtn-index").click()
    page.locator("#index-mode-compose").click()
    page.locator("#add-content").fill("sk-ant-test-fake-1234567890abcdef")
    page.locator("#add-btn").click()

    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    page.locator("#confirm-cancel-btn").click()

    # Dialog dismisses, helper returns ``null``, the call site's
    # ``if (data === null) return`` short-circuits before the success
    # toast renders. Give the SPA a beat in case a stray retry is in
    # flight, then assert call count stayed at 1.
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    page.wait_for_timeout(300)
    assert len(calls) == 1, f"cancel must not trigger retry, saw {len(calls)} POSTs"

    # No bypass toast — the affirmative spec asserts one renders, this
    # one asserts none does. Together they pin the symmetric pair.
    bypass_toasts = page.locator("#toast-container .toast", has_text="Bypassed")
    assert bypass_toasts.count() == 0, "cancel must not emit toast.redaction_bypassed"


def test_api_upload_mixed_batch_cancel_refreshes_stale_state(
    page, mm_web_url: str, tmp_path: Path
) -> None:
    """Mixed batch (1 clean + 1 blocked) → user cancels the bypass dialog
    → ``loadStats`` / ``loadSourceFilter`` still fire so the rest of the
    UI doesn't lag behind the per-file result list.

    The first POST already persisted/indexed ``clean.md`` before the
    confirm dialog opened. The pre-fix early-return on ``cancelled``
    skipped the staleness refresh, leaving the result list showing a
    fresh row while sources / stats panels stayed pinned to their
    pre-upload state. This spec counts ``GET /api/stats`` calls before
    and after the cancel click; the post-cancel delta must be ≥ 1.
    """
    _install_default_stubs(page)

    stats_calls: list[str] = []
    page.route(
        "**/api/stats",
        lambda r: (
            stats_calls.append(r.request.url),
            r.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({}),
            ),
        )[1],
    )

    def _upload_handler(route):
        # Single fulfillment — the user cancels, so no retry POST fires.
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "files": [
                        {
                            "filename": "clean.md",
                            "indexed_chunks": 2,
                            "path": "/home/u/.memtomem/uploads/clean.md",
                        },
                        {
                            "filename": "secret.md",
                            "indexed_chunks": 0,
                            "error": "redaction_blocked (hits=3)",
                        },
                    ],
                    "total_indexed": 2,
                }
            ),
        )

    page.route(re.compile(r"/api/upload(?:\?.*)?$"), _upload_handler)

    clean = tmp_path / "clean.md"
    clean.write_text("# Clean notes\n\nNo secrets here.\n")
    secret = tmp_path / "secret.md"
    secret.write_text("# Secret notes\n\nsk-ant-test-fake-1234567890abcdef\n")

    page.goto(mm_web_url)
    page.locator("#tabbtn-index").click()
    page.locator("#index-mode-upload").click()
    page.locator("#upload-input").set_input_files([str(clean), str(secret)])

    # Wait for boot ``/api/stats`` calls to settle so the post-cancel
    # delta isn't fighting against late-arriving boot traffic.
    page.wait_for_timeout(200)
    pre_cancel = len(stats_calls)

    page.locator("#upload-btn").click()
    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    page.locator("#confirm-cancel-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    # Cancel toast must render so the operator knows the blocked file did
    # not land. (Drag-upload had no toast at all pre-fix.)
    page.wait_for_selector(
        "#toast-container .toast",
        timeout=2_000,
    )

    # Staleness refresh must fire on cancel — the unified branch below
    # the if/else in the upload-tab handler hits ``loadStats``. Give the
    # SPA a beat for the GET to dispatch.
    page.wait_for_timeout(300)
    post_cancel = len(stats_calls)
    assert post_cancel > pre_cancel, (
        f"cancel must trigger loadStats refresh; saw {pre_cancel} pre, {post_cancel} post"
    )
