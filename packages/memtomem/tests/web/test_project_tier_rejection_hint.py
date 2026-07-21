"""Browser tests for the ADR-0011 §5 Gate B rejection-hint flow on
``POST /api/add`` (issue #924 — PR-F slice 4).

The route signals a project-tier rejection with::

    HTTP 403
    {"detail": {"detail": "blocked_project_shared",
                "surface": "web_api_add",
                "scope": "project_shared",
                "message": "scope='project_shared' writes...",
                "cli_hint": "mm mem add --scope project_shared",
                "docs_url": "https://github.com/.../0011-...md"}}

The SPA must parse this nested shape (mirroring the redaction-blocked
parser at ``api()``) and surface the CLI hint + docs URL via the toast
so an operator who hits this path via dev-tools / API client sees the
equivalent invocation rather than an opaque "save failed" error.

These specs pin the wire-level contract — they stub ``/api/add`` and
fire the request through ``page.evaluate`` (the compose form doesn't
currently expose a tier picker, so a UI-driven path would never reach
this code; the dev-tools / API-only path is what we test). When a
future tier-picker UI ships, the assertions on toast contents stay
valid.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.browser


def _install_default_stubs(page) -> None:
    """Mirror the catch-all stub pattern from ``test_redaction_blocked_retry``.

    ``page.route`` is last-registered-wins so the catch-all goes first
    and the spec-specific ``/api/add`` override goes last.
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
    page.route("**/api/privacy/patterns", lambda r: _ok(r, {"patterns": []}))


def test_api_add_403_blocked_project_shared_surfaces_cli_hint_and_docs(
    page, mm_web_url: str
) -> None:
    """``POST /api/add`` 403 with ``blocked_project_shared`` shape →
    error toast carries both the rejection prose and the literal
    ``mm mem add --scope project_shared`` CLI hint + docs URL, so the
    operator can copy/paste the equivalent invocation.

    Fires the request through ``page.evaluate`` (the compose form
    doesn't expose a tier picker today, so dev-tools is the realistic
    caller). When a tier-picker UI ships this spec stays valid — the
    toast contract is independent of the trigger surface.
    """
    _install_default_stubs(page)

    def _add_handler(route):
        # Mirror the FastAPI-nested ``{detail: {detail: ...}}`` shape the
        # backend actually produces (system.py raises ``HTTPException``
        # with a structured ``detail`` dict).
        route.fulfill(
            status=403,
            content_type="application/json",
            body=json.dumps(
                {
                    "detail": {
                        "detail": "blocked_project_shared",
                        "surface": "web_api_add",
                        "scope": "project_shared",
                        "message": (
                            "scope='project_shared' writes to a git-tracked "
                            "directory. Re-submit with confirm_project_shared=true."
                        ),
                        "cli_hint": "mm mem add --scope project_shared",
                        "docs_url": (
                            "https://github.com/memtomem/memtomem/blob/main/docs/adr/"
                            "0011-canonical-artifact-scope-hierarchy.md"
                        ),
                    }
                }
            ),
        )

    page.route("**/api/add", _add_handler)

    page.goto(mm_web_url)

    # Drive the request from the page's JS context so the error path
    # exercises ``api()`` → ``ProjectTierBlockedError`` → catch-arm
    # toast. ``addMemoryFromCompose`` is the SPA's submit handler; we
    # invoke its inner ``apiWithRedactionRetry`` path directly through
    # the global API helper since the compose form has no tier picker.
    page.evaluate(
        """
        (async () => {
          try {
            await window.api('POST', '/api/add', {
              content: 'team note',
              scope: 'project_shared',
            });
          } catch (err) {
            // Drive the actual production formatter — same code path
            // ``addMemoryFromCompose``'s catch-arm uses. Without this
            // shared helper the test would silently pass even if the
            // catch-arm stopped showing cliHint / docsUrl (Codex #924
            // review-pass).
            if (err && err.name === 'ProjectTierBlockedError') {
              window.showToast(window.formatProjectTierBlockedToast(err), 'error');
            } else {
              window.showToast('unexpected error: ' + err.message, 'error');
            }
          }
        })()
        """
    )

    # Toast must render with the literal CLI hint and docs URL. The
    # rejection prose is intentionally not pinned word-for-word — the
    # server-side message can evolve; the actionable pieces (CLI hint
    # + docs URL) are the contract.
    toast = page.wait_for_selector("#toast-container .toast", timeout=2_000)
    text = toast.text_content() or ""
    assert "mm mem add --scope project_shared" in text, f"toast missing CLI hint: {text!r}"
    assert "0011-canonical-artifact-scope-hierarchy" in text, f"toast missing docs URL: {text!r}"


def test_api_add_403_blocked_project_shared_is_not_redaction_error(page, mm_web_url: str) -> None:
    """Symmetric guardrail — the project-tier 4xx path must NOT trigger
    the redaction confirm dialog. Without strict ``detail.detail``
    discrimination in ``api()``, a future regression that catches both
    error classes through the same branch would silently re-issue the
    request with ``force_unsafe=true`` (which is exactly what Gate B
    is designed to prevent for project_shared writes).
    """
    _install_default_stubs(page)

    page.route(
        "**/api/add",
        lambda route: route.fulfill(
            status=403,
            content_type="application/json",
            body=json.dumps(
                {
                    "detail": {
                        "detail": "blocked_project_shared",
                        "surface": "web_api_add",
                        "scope": "project_shared",
                        "message": "scope='project_shared' write rejected",
                        "cli_hint": "mm mem add --scope project_shared",
                        "docs_url": "https://example.com/adr-0011",
                    }
                }
            ),
        ),
    )

    page.goto(mm_web_url)

    err_name = page.evaluate(
        """
        (async () => {
          try {
            await window.api('POST', '/api/add', {
              content: 'x',
              scope: 'project_shared',
            });
            return 'no-error';
          } catch (err) {
            return err && err.name ? err.name : 'unknown';
          }
        })()
        """
    )
    assert err_name == "ProjectTierBlockedError", (
        f"expected ProjectTierBlockedError, got {err_name!r}"
    )

    # The redaction confirm dialog must stay hidden — its presence would
    # indicate the project-tier 4xx was misclassified as a redaction
    # block.
    assert page.locator("#confirm-modal").get_attribute("hidden") is not None, (
        "redaction confirm dialog must not appear for project-tier 4xx"
    )


def test_format_project_tier_blocked_toast_omits_cli_line_when_hint_absent(
    page, mm_web_url: str
) -> None:
    """The Gate A force-unsafe-on-project_shared 4xx reuses the
    ``blocked_project_shared`` discriminant but does NOT carry
    ``cli_hint`` / ``docs_url`` (there's no actionable CLI form for
    "your write was redaction-blocked on a git-tracked tier"). Pin
    that the shared formatter omits the ``$ undefined`` line and the
    URL line cleanly when those fields are absent — Codex review #924
    Major finding, the original direct interpolation would have
    leaked ``$ undefined`` into the toast.
    """
    _install_default_stubs(page)
    page.goto(mm_web_url)

    rendered = page.evaluate(
        """
        () => {
          // Duck-typed error mirrors what api() builds when the 4xx
          // omits cli_hint / docs_url (Gate A force-unsafe path).
          // The class itself isn't exported on ``window`` because
          // ``class`` declarations don't attach automatically the way
          // top-level ``function`` declarations do — that's why this
          // test uses a plain Error with name + fields rather than
          // ``new ProjectTierBlockedError(...)``. The formatter only
          // reads ``.message`` / ``.cliHint`` / ``.docsUrl`` so duck
          // typing is sufficient.
          const err = Object.assign(
            new Error('force_unsafe rejected on project_shared'),
            { name: 'ProjectTierBlockedError' }
          );
          return window.formatProjectTierBlockedToast(err);
        }
        """
    )
    assert rendered == "force_unsafe rejected on project_shared", (
        f"formatter must omit empty cli/docs lines, got: {rendered!r}"
    )
    # Pin the literal that the original bug would have produced — any
    # regression that drops the truthiness guard re-introduces it.
    assert "undefined" not in rendered
    assert "$ " not in rendered


def test_tier_badge_html_helper_renders_project_tier_tokens_verbatim(page, mm_web_url: str) -> None:
    """Pin the ADR-0016 §7 "no display alias" contract on the SPA helper.

    ``_tierBadgeHtml`` is the single render path for the canonical-residency
    tier badge — the three tokens are rendered verbatim. The user tier
    is suppressed (returns empty string) so the common case stays
    visually quiet; ``project_shared`` and ``project_local`` get their
    own tier classes; ``project_local`` rows on context surfaces carry
    the inline ``(not pushed to runtimes)`` annotation per ADR-0011 §3.
    """
    _install_default_stubs(page)
    page.goto(mm_web_url)

    user_badge = page.evaluate("window._tierBadgeHtml('user')")
    assert user_badge == "", (
        "user-tier badge must render as empty string so the common case stays quiet"
    )

    shared_badge = page.evaluate("window._tierBadgeHtml('project_shared')")
    assert "project_shared" in shared_badge
    assert "badge-tier--project_shared" in shared_badge
    # Display-alias guardrail: the literal token must appear without
    # any of the historical aliases (Personal / Team / Local Draft) the
    # Tiered Context Gateway v2 memory explicitly forbids.
    for alias in ("Personal", "Team", "Local Draft"):
        assert alias not in shared_badge, (
            f"badge HTML must not contain display alias {alias!r}: {shared_badge!r}"
        )

    local_badge = page.evaluate("window._tierBadgeHtml('project_local')")
    assert "project_local" in local_badge
    assert "badge-tier--project_local" in local_badge
    # Memory rows don't carry the fan-out annotation (project_local
    # memory still fans out via memory's own contract per ADR-0011 §3).
    assert "(not pushed to runtimes)" not in local_badge

    # Context rows DO carry the annotation when project_local — ADR-0011
    # §3 zero-fan-out rule for agents/skills/commands.
    local_ctx_badge = page.evaluate(
        "window._tierBadgeHtml('project_local', { isContextRow: true })"
    )
    assert "(not pushed to runtimes)" in local_ctx_badge, (
        f"context-row project_local badge missing fan-out annotation: {local_ctx_badge!r}"
    )

    # Unknown tokens render as empty string — defense against a
    # malformed server response that would otherwise inject arbitrary
    # text into the badge slot.
    bogus = page.evaluate("window._tierBadgeHtml('draft')")
    assert bogus == "", f"unknown tier token must render empty: {bogus!r}"
