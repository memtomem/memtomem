"""Browser tests for rank-10 — Sync / Sync-All / Import confirm threading.

The Context Gateway's most frequent actions used to confirm with generic,
count-less prose and only surfaced what/where they touched *after* acting
(rank 10 of the UX audit). These pins assert the confirm dialog now names:

* Section **Sync** — the artifact COUNT being fanned out (threaded from the
  section's ``canonicalCount`` dataset; no fetch).
* **Import** — the resolved DESTINATION (active project · project_shared) plus
  a dry-run PREVIEW (``?dry_run=1``) of how many artifacts would import vs
  already exist, fetched before the modal opens.
* Per-project portal **Sync** — the specific PROJECT label being synced.

``page.route`` short-circuits every ``/api`` call, so no CSRF/DB is in play —
the specs assert the confirm-message wiring only and cancel before any write.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


# Two canonical skills so the Sync confirm has a non-trivial count to name.
_SKILLS_TWO_CANONICAL = {
    "skills": [
        {"name": "alpha", "canonical_path": "/srv/cwd/.memtomem/skills/alpha", "runtimes": []},
        {"name": "beta", "canonical_path": "/srv/cwd/.memtomem/skills/beta", "runtimes": []},
    ],
    "scanned_dirs": ["/srv/cwd/.claude/skills/"],
}

# Dry-run import preview: two would-import, one already-exists skip.
_IMPORT_PREVIEW = {
    "imported": [{"name": "from-claude"}, {"name": "from-gemini"}],
    "skipped": [
        {"name": "already", "reason": "canonical exists", "reason_code": "canonical_exists"}
    ],
    "project_root": "/srv/cwd",
    "scanned_dirs": ["/srv/cwd/.claude/skills/"],
    "dry_run": True,
}


def _stub_skills_list(page) -> None:
    page.route(
        "**/api/context/skills**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_SKILLS_TWO_CANONICAL),
        ),
    )


def _open_skills_list(page) -> None:
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-skills')")
    page.wait_for_function(
        "() => document.querySelectorAll('#ctx-skills-list .ctx-scope-group').length > 0",
        timeout=5_000,
    )


def test_section_sync_confirm_names_artifact_count(page, mm_web_url: str) -> None:
    """Clicking section Sync names the canonical count in the confirm dialog."""
    install_default_stubs(page)
    _stub_skills_list(page)
    page.goto(mm_web_url)
    _open_skills_list(page)

    page.locator("#settings-ctx-skills .ctx-sync-btn[data-type='skills']").click()
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=2_000)
    message = page.locator("#confirm-message").text_content() or ""
    # EN: "Push 2 skills to the runtimes configured for this store?" — the
    # count is the rank-10 win.
    assert "2 skills" in message, f"sync confirm should name the count, got {message!r}"
    page.locator("#confirm-cancel-btn").click()


def test_section_sync_confirm_shows_impact_and_overwrite_warning(page, mm_web_url: str) -> None:
    """U4 (#1229): the sync confirm folds per-item runtime statuses into a
    create/overwrite impact sentence, and out-of-sync overwrites surface in
    the styled #confirm-warning line (hidden when nothing is overwritten)."""
    install_default_stubs(page)
    page.route(
        "**/api/context/skills**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "skills": [
                        {
                            "name": "alpha",
                            "canonical_path": "/srv/cwd/.memtomem/skills/alpha",
                            "runtimes": [
                                {"runtime": "claude_skills", "status": "missing target"},
                                {"runtime": "codex_skills", "status": "out of sync"},
                            ],
                        },
                        {
                            "name": "beta",
                            "canonical_path": "/srv/cwd/.memtomem/skills/beta",
                            "runtimes": [
                                {"runtime": "claude_skills", "status": "in sync"},
                            ],
                        },
                    ],
                    "scanned_dirs": ["/srv/cwd/.claude/skills/"],
                }
            ),
        ),
    )
    page.goto(mm_web_url)
    _open_skills_list(page)

    page.locator("#settings-ctx-skills .ctx-sync-btn[data-type='skills']").click()
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=2_000)
    message = page.locator("#confirm-message").text_content() or ""
    assert "create 1" in message, f"impact sentence missing create count: {message!r}"
    assert "overwrite 1" in message, f"impact sentence missing overwrite count: {message!r}"
    assert "claude, codex" in message, f"impact sentence missing runtimes: {message!r}"
    warning = page.locator("#confirm-warning")
    assert warning.is_visible(), "overwrite warning line must be visible"
    assert "1" in (warning.text_content() or "")
    page.locator("#confirm-cancel-btn").click()
    # Symmetric negative: the warning resets when the dialog closes (cleanup
    # discipline shared with the extra-option row).
    page.wait_for_selector("#confirm-modal", state="hidden", timeout=2_000)
    assert page.locator("#confirm-warning").is_hidden()


def test_import_confirm_names_destination_and_dry_run_preview(page, mm_web_url: str) -> None:
    """Import fetches a ``?dry_run=1`` preview and names destination + counts."""
    install_default_stubs(page)
    _stub_skills_list(page)

    preview_calls: list[str] = []

    def _import_handler(route):
        preview_calls.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_IMPORT_PREVIEW),
        )

    # Registered AFTER the list stub so the more-specific /import URL wins
    # (last-route-wins); the list stub still serves the bare /skills GET.
    page.route("**/api/context/skills/import**", _import_handler)
    page.goto(mm_web_url)
    _open_skills_list(page)

    page.locator("#settings-ctx-skills .ctx-import-btn[data-type='skills']").click()
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=3_000)
    message = page.locator("#confirm-message").text_content() or ""

    # The preview must actually be fetched as a dry-run (never a blind import).
    assert any("dry_run=1" in u for u in preview_calls), (
        f"import must fetch a dry-run preview first; got {preview_calls!r}"
    )
    # Destination: active project (Server CWD) · project_shared tier.
    assert "Server CWD" in message, f"import confirm should name the destination, got {message!r}"
    # Preview: 2 would-import, 1 already exists.
    assert "2 to import" in message and "1 already exist" in message, (
        f"import confirm should name the dry-run counts, got {message!r}"
    )
    # Since 1 already exists, the preview ties that count to the Overwrite
    # checkbox so "already exist" doesn't read as "will be skipped".
    assert "Overwrite to replace" in message, (
        f"with skips > 0 the confirm should mention Overwrite, got {message!r}"
    )
    page.locator("#confirm-cancel-btn").click()


# Preview whose only skip is a cross-runtime dedup (already_imported), NOT a
# canonical_exists — the overwrite-irrelevant skip must not be miscounted.
_IMPORT_PREVIEW_DEDUP_ONLY = {
    "imported": [{"name": "from-claude"}],
    "skipped": [
        {"name": "dup", "reason": "already imported from claude", "reason_code": "already_imported"}
    ],
    "project_root": "/srv/cwd",
    "scanned_dirs": ["/srv/cwd/.claude/skills/"],
    "dry_run": True,
}


def test_import_confirm_counts_only_canonical_exists_as_already_exist(
    page, mm_web_url: str
) -> None:
    """Only ``canonical_exists`` skips count as 'already exist' / arm the
    Overwrite hint — dedup / invalid-name / parse / privacy skips do not
    (Codex review: skip-reason conflation)."""
    install_default_stubs(page)
    _stub_skills_list(page)
    page.route(
        "**/api/context/skills/import**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_IMPORT_PREVIEW_DEDUP_ONLY),
        ),
    )
    page.goto(mm_web_url)
    _open_skills_list(page)

    page.locator("#settings-ctx-skills .ctx-import-btn[data-type='skills']").click()
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=3_000)
    message = page.locator("#confirm-message").text_content() or ""
    assert "1 to import" in message, f"the importable item should still count, got {message!r}"
    # The lone skip is a dedup, not canonical_exists → 0 already exist, no hint.
    assert "0 already exist" in message, (
        f"a dedup skip must not count as 'already exist', got {message!r}"
    )
    assert "Overwrite to replace" not in message, (
        f"no canonical_exists skip → no Overwrite hint, got {message!r}"
    )
    page.locator("#confirm-cancel-btn").click()


def test_import_confirm_falls_back_when_preview_fails(page, mm_web_url: str) -> None:
    """A failed dry-run preview still opens a destination-named confirm — the
    preview is best-effort and never blocks Import."""
    install_default_stubs(page)
    _stub_skills_list(page)
    page.route(
        "**/api/context/skills/import**",
        lambda r: r.fulfill(status=503, content_type="application/json", body=json.dumps({})),
    )
    page.goto(mm_web_url)
    _open_skills_list(page)

    page.locator("#settings-ctx-skills .ctx-import-btn[data-type='skills']").click()
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=3_000)
    message = page.locator("#confirm-message").text_content() or ""
    # Destination still named; the (failed) preview counts are simply absent.
    assert "Server CWD" in message, f"fallback confirm should still name dest, got {message!r}"
    assert "to import" not in message, f"failed preview must not inject counts, got {message!r}"
    page.locator("#confirm-cancel-btn").click()


def _portal_scope(scope_id, label, *, sources, enabled, sync_eligible):
    return {
        "scope_id": scope_id,
        "project_scope_id": scope_id,
        "label": label,
        "root": f"/fake/{scope_id or 'cwd'}",
        "tier": "project",
        "sources": sources,
        "experimental": False,
        "missing": False,
        "stale": False,
        "enabled": enabled,
        "sync_eligible": sync_eligible,
        "counts": {"skills": 1, "commands": 0, "agents": 0, "mcp-servers": 0},
    }


def test_portal_sync_confirm_names_project(page, mm_web_url: str) -> None:
    """Per-project portal Sync names the specific project in its confirm."""
    install_default_stubs(page)
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "target_scope": "project_shared",
                    "scopes": [
                        _portal_scope(
                            "",
                            "Server CWD",
                            sources=["server-cwd"],
                            enabled=True,
                            sync_eligible=True,
                        ),
                        _portal_scope(
                            "proj-a",
                            "Alpha",
                            sources=["known-projects"],
                            enabled=True,
                            sync_eligible=True,
                        ),
                    ],
                }
            ),
        ),
    )
    page.route(
        "**/api/context/runtimes**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"project_root": "/fake", "runtimes": []}),
        ),
    )
    page.goto(mm_web_url)
    page.locator("#tabbtn-context-gateway").click()
    # ADR-0026 D-F flip: Simple is the default and hides the section nav on the
    # Overview — switch to Advanced so the Projects nav button is clickable.
    page.evaluate("() => _ctxSetSimpleMode(false)")
    page.locator(".settings-nav-btn[data-section='ctx-projects']").click()
    page.wait_for_selector(".ctx-portal-row", timeout=3_000)

    page.locator('.ctx-portal-sync[data-scope-id="proj-a"]').click()
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=2_000)
    message = page.locator("#confirm-message").text_content() or ""
    assert "Alpha" in message, f"portal sync confirm should name the project, got {message!r}"
    page.locator("#confirm-cancel-btn").click()


def test_section_sync_lock_timeout_skip_toasts_warning_not_success(page, mm_web_url: str) -> None:
    """#1229: when the engine aborts on a held destination lock, the response
    is HTTP 200 with ``skipped=[{reason_code: 'lock_timeout'}]`` and zero
    ``generated``. The handler previously special-cased only
    ``no_canonical_root`` and ``dropped``, so a lock-timeout run fell through
    to the "Sync completed" success toast — reporting a sync that never ran.
    """
    install_default_stubs(page)
    _stub_skills_list(page)
    page.route(
        "**/api/context/skills/sync**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated": [],
                    "skipped": [
                        {
                            "runtime": "<all>",
                            "reason": (
                                "another process held a destination lock past "
                                "the 30s acquisition budget — re-run sync to retry"
                            ),
                            "reason_code": "lock_timeout",
                        }
                    ],
                    "canonical_root": ".memtomem/skills",
                }
            ),
        ),
    )
    page.goto(mm_web_url)
    _open_skills_list(page)

    page.locator("#settings-ctx-skills .ctx-sync-btn[data-type='skills']").click()
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=2_000)
    page.locator("#confirm-ok-btn").click()

    toast = page.wait_for_selector("#toast-container .toast.toast-warning", timeout=4_000)
    text = toast.text_content() or ""
    assert "lock" in text.lower(), f"warning toast should explain the lock, got {text!r}"
    assert page.locator("#toast-container .toast.toast-success").count() == 0, (
        "a lock_timeout run must not toast 'Sync completed'"
    )


def test_section_sync_target_conflict_skip_toasts_warning_not_success(
    page, mm_web_url: str
) -> None:
    """#1229: a destination holding non-skill content is now a typed
    ``target_conflict`` skip (previously the engine crashed mid-batch with
    IsADirectoryError and the route returned HTTP 500). Same toast contract
    as ``lock_timeout``: an HTTP-200 response whose only outcome is the
    typed skip must warn — falling through to "Sync completed" would hide
    the skipped destination."""
    install_default_stubs(page)
    _stub_skills_list(page)
    page.route(
        "**/api/context/skills/sync**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "generated": [],
                    "skipped": [
                        {
                            "runtime": "claude_skills",
                            "reason": (
                                "refusing to overwrite non-skill directory: "
                                "/fake/.claude/skills/foo (add a SKILL.md or "
                                "remove the directory first)"
                            ),
                            "reason_code": "target_conflict",
                        }
                    ],
                    "canonical_root": ".memtomem/skills",
                }
            ),
        ),
    )
    page.goto(mm_web_url)
    _open_skills_list(page)

    page.locator("#settings-ctx-skills .ctx-sync-btn[data-type='skills']").click()
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=2_000)
    page.locator("#confirm-ok-btn").click()

    toast = page.wait_for_selector("#toast-container .toast.toast-warning", timeout=4_000)
    text = toast.text_content() or ""
    assert "non-skill" in text, f"warning toast should carry the conflict reason, got {text!r}"
    assert page.locator("#toast-container .toast.toast-success").count() == 0, (
        "a target_conflict run must not toast 'Sync completed'"
    )
