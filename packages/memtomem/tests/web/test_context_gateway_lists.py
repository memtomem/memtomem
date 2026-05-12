"""Browser tests for the Context Gateway per-type list sections (Q-PR4, #826).

Pin behavioral correctness for the langchange staleness audit follow-up
that PR #824 (Q-PR1) intentionally left narrow. The overview-level
``langchange`` listener PR #824 added is now extended to also cover the
three per-type sections (``settings-ctx-skills`` / ``-commands`` /
``-agents``) where five other inline-``t()`` regions live:

* ``_ctxScopeBadges`` — non-cwd scope badges (experimental, missing)
* ``renderRuntimeBadges`` — per-runtime status labels on each card
* ``_ctxRefreshSectionState`` — runtime-only banner above the list
* ``renderImportResult`` — post-Import status receipt
* ``renderDroppedChips`` — Dropped-field chips inside the Diff pane

The harness mirrors ``test_context_gateway_overview.py``: lifespan is off
(see ``conftest.py`` docstring), every ``/api/**`` call is intercepted via
``page.route()``, and the SPA boots from disk.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


def _open_skills_list(page) -> None:
    """Navigate to the Skills sub-section of Context Gateway.

    Same activation pattern as ``_open_context_gateway`` in the overview
    suite, but lands on ``settings-ctx-skills``. Waits on the populated
    list (``.ctx-scope-group`` rendered) rather than a visibility check —
    the section may carry stale ``.active`` from a previous test in the
    same browser context.
    """
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-skills')")
    page.wait_for_function(
        "() => document.querySelectorAll('#ctx-skills-list .ctx-scope-group').length > 0",
        timeout=5_000,
    )


# Single-scope CWD payload with one out-of-sync canonical skill (so
# ``renderRuntimeBadges`` has something to label) plus a non-cwd scope
# carrying the ``missing`` flag (so ``_ctxScopeBadges`` renders).
_CWD_PROJECTS_WITH_NON_CWD_MISSING = {
    "scopes": [
        {
            "scope_id": "cwd-scope",
            "label": "cwd",
            "root": "/srv/cwd",
            "tier": "user",
            "sources": ["server-cwd"],
            "experimental": False,
            "missing": False,
            "counts": {"skills": 1, "commands": 0, "agents": 0},
        },
        {
            "scope_id": "missing-scope",
            "label": "old-project",
            "root": "/srv/old",
            "tier": "user",
            "sources": ["history"],
            "experimental": False,
            "missing": True,
            "counts": {"skills": 0, "commands": 0, "agents": 0},
        },
    ]
}


# Items shape consumed by ``_ctxRenderItemsHtml`` and ``renderRuntimeBadges``.
_CWD_SKILLS_ITEMS = {
    "skills": [
        {
            "name": "demo-skill",
            "canonical_path": "/srv/cwd/.memtomem/skills/demo-skill.md",
            "runtimes": [{"runtime": "claude_skills", "status": "in sync"}],
        }
    ],
    "scanned_dirs": ["/srv/cwd/.claude/skills/"],
}


# Runtime-only payload: one item with no canonical_path so
# ``_ctxRefreshSectionState`` inserts the banner.
_CWD_SKILLS_RUNTIME_ONLY = {
    "skills": [
        {
            "name": "drift-skill",
            "canonical_path": "",
            "runtimes": [
                {
                    "runtime": "claude_skills",
                    "status": "missing canonical",
                    "runtime_path": "/srv/cwd/.claude/skills/drift-skill.md",
                    "runtime_content": "# Drift skill\n",
                }
            ],
        }
    ],
    "scanned_dirs": ["/srv/cwd/.claude/skills/"],
}


# Diff-endpoint payload for the canonical detail's Diff tab; ``dropped_fields``
# triggers ``renderDroppedChips`` rendering.
_DIFF_WITH_DROPPED = {
    "canonical_content": "name: demo\n",
    "runtimes": [
        {
            "runtime": "claude_skills",
            "status": "out of sync",
            "runtime_path": "/srv/cwd/.claude/skills/demo-skill.md",
            "runtime_content": "name: demo\nextra: yes\n",
            "dropped_fields": ["extra"],
        }
    ],
}


# Runtime-only diff payload (no canonical_content; triggers the
# ``_ctxLoadRuntimeOnlyDetail`` render).
_DIFF_RUNTIME_ONLY = {
    "canonical_content": "",
    "runtimes": [
        {
            "runtime": "claude_skills",
            "status": "missing canonical",
            "runtime_path": "/srv/cwd/.claude/skills/drift-skill.md",
            "runtime_content": "# Drift skill\n",
        }
    ],
}


def _stub_projects(page, payload):
    page.route(
        "**/api/context/projects",
        lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(payload)),
    )


def _stub_skills(page, payload):
    page.route(
        "**/api/context/skills",
        lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(payload)),
    )


def test_q_pr4_langchange_rerenders_scope_badge_inline_text(page, mm_web_url: str) -> None:
    """``_ctxScopeBadges`` renders the ``scope_missing`` chip via inline
    ``t()`` in innerHTML; without the listener's ``loadCtxList`` re-issue
    a language toggle would leave it in the prior locale until the user
    triggered a list reload through some other path.

    EN ``(missing)`` → KO ``(없음)``. The ``scope_experimental`` chip
    shares the same English/Korean string so it can't carry this
    assertion — ``scope_missing`` is the canary."""
    install_default_stubs(page)
    _stub_projects(page, _CWD_PROJECTS_WITH_NON_CWD_MISSING)
    _stub_skills(page, _CWD_SKILLS_ITEMS)
    page.goto(mm_web_url)
    _open_skills_list(page)

    badge = page.locator(
        "#ctx-skills-list "
        "details[data-scope-id='missing-scope'] "
        ".ctx-scope-badge.ctx-scope-badge--missing"
    )
    pre = (badge.text_content() or "").strip()
    assert pre == "(missing)", f"EN scope_missing badge should be '(missing)', got {pre!r}"

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.wait_for_function(
        "() => {"
        "  const el = document.querySelector("
        '    \'#ctx-skills-list details[data-scope-id="missing-scope"] '
        ".ctx-scope-badge--missing');"
        "  return el && el.textContent.trim() === '(없음)';"
        "}",
        timeout=3_000,
    )
    post = (
        page.locator(
            "#ctx-skills-list details[data-scope-id='missing-scope'] "
            ".ctx-scope-badge.ctx-scope-badge--missing"
        ).text_content()
        or ""
    ).strip()
    assert post == "(없음)", f"KO scope_missing badge should be '(없음)', got {post!r}"
    # Symmetric negative: the English literal must not linger.
    assert "(missing)" not in post, f"EN literal must not survive KO toggle: {post!r}"


def test_q_pr4_langchange_rerenders_runtime_badge_label(page, mm_web_url: str) -> None:
    """``renderRuntimeBadges`` resolves status text via ``_ctxStatusText``
    in innerHTML — no ``data-i18n`` walker reaches it. The listener's
    ``loadCtxList`` re-issue must rebuild the badges so EN ``In sync`` →
    KO ``동기화됨``.
    """
    install_default_stubs(page)
    _stub_projects(page, _CWD_PROJECTS_WITH_NON_CWD_MISSING)
    _stub_skills(page, _CWD_SKILLS_ITEMS)
    page.goto(mm_web_url)
    _open_skills_list(page)

    # Wait for the cwd group's items to load (lazy fetch on the open group).
    page.wait_for_function(
        "() => document.querySelectorAll("
        '  \'#ctx-skills-list details[data-scope-id="cwd-scope"] '
        ".ctx-runtime-badge').length > 0",
        timeout=5_000,
    )
    badge = page.locator(
        "#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-runtime-badge"
    ).first
    pre_text = (badge.text_content() or "").strip()
    assert "In sync" in pre_text, f"EN runtime badge should contain 'In sync', got {pre_text!r}"

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.wait_for_function(
        "() => {"
        "  const els = Array.from(document.querySelectorAll("
        '    \'#ctx-skills-list details[data-scope-id="cwd-scope"] '
        ".ctx-runtime-badge'));"
        "  return els.some(el => el.textContent.includes('동기화됨'));"
        "}",
        timeout=3_000,
    )
    post_text = (
        page.locator(
            "#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-runtime-badge"
        ).first.text_content()
        or ""
    ).strip()
    assert "동기화됨" in post_text, f"KO runtime badge should contain '동기화됨', got {post_text!r}"
    # Negative: EN literal must not linger after toggle.
    assert "In sync" not in post_text, f"EN 'In sync' must not survive KO toggle: {post_text!r}"


def test_context_list_card_renders_project_local_tier_badge_with_annotation(
    page, mm_web_url: str
) -> None:
    """Context artifact cards render the literal tier token and the
    project_local zero-runtime-fan-out annotation inline with the name.
    """
    install_default_stubs(page)
    _stub_projects(page, _CWD_PROJECTS_WITH_NON_CWD_MISSING)
    _stub_skills(
        page,
        {
            "skills": [
                {
                    "name": "draft-skill",
                    "canonical_path": ".memtomem/skills.local/draft-skill",
                    "target_scope": "project_local",
                    "runtimes": [],
                }
            ],
            "scanned_dirs": [],
        },
    )
    page.goto(mm_web_url)
    _open_skills_list(page)

    card_name = page.locator("#ctx-skills-list .ctx-card-name").first
    text = card_name.text_content() or ""
    assert "project_local" in text
    assert "no runtime fan-out" in text
    assert card_name.locator(".badge-tier--project_local").count() == 1


def test_context_list_non_shared_tier_click_threads_target_scope(page, mm_web_url: str) -> None:
    """Card click on a non-shared tier hits ``?target_scope=...`` (P1 #940 r3).

    Item-level routes now accept ``target_scope`` (skills/agents/commands
    read/diff/rendered honor every tier; create/update/delete/sync/import
    reject non-shared with HTTP 400 via ``_reject_non_shared_write``). This
    pins the round-trip: a click on a project_local card MUST fire
    ``GET /api/context/skills/draft-skill?target_scope=project_local`` so the
    backend opens the project_local canonical (not the same-name shared one).
    """
    install_default_stubs(page)
    # The shared ``_stub_*`` helpers register patterns without trailing ``**``
    # so they miss URLs that carry ``?target_scope=...``. This test triggers
    # a tier switch which adds that query string, so register wider patterns
    # locally; last-route-wins puts these ahead of the catch-all.
    _skills_payload = {
        "skills": [
            {
                "name": "draft-skill",
                "canonical_path": ".memtomem/skills.local/draft-skill",
                "target_scope": "project_local",
                "runtimes": [],
            }
        ],
        "scanned_dirs": [],
    }
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_CWD_PROJECTS_WITH_NON_CWD_MISSING),
        ),
    )
    page.route(
        "**/api/context/skills**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_skills_payload),
        ),
    )

    detail_calls: list[str] = []

    def _on_detail(route):
        detail_calls.append(route.request.url)
        # Return a minimal valid detail payload so loadCtxDetail doesn't error.
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"name": "draft-skill", "content": "# draft\n", "mtime_ns": "0", "files": []}
            ),
        )

    page.route("**/api/context/skills/draft-skill**", _on_detail)

    page.goto(mm_web_url)
    _open_skills_list(page)

    page.locator("#ctx-skills-list .ctx-tier-filter button[data-scope='project_local']").click()
    page.wait_for_function(
        "() => {"
        "  const b = document.querySelector("
        "    '#ctx-skills-list .ctx-tier-filter button[data-scope=\"project_local\"]');"
        "  return b && b.classList.contains('active');"
        "}",
        timeout=3_000,
    )
    page.wait_for_selector(
        "#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-card",
        timeout=5_000,
    )

    card = page.locator("#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-card").first
    card.click()

    page.wait_for_function(
        "() => window.__lastDetailUrl !== undefined || true",
        timeout=500,
    )
    # The card MUST be clickable on non-shared tiers — readonly-card workaround
    # is gone now that routes honor target_scope.
    card_classes = card.get_attribute("class") or ""
    assert "ctx-card--readonly" not in card_classes, (
        f"non-shared-tier card should be clickable now (#940 r3), got class={card_classes!r}"
    )
    # The detail fetch fires with the tier appended.
    assert len(detail_calls) == 1, f"expected 1 detail call, got {detail_calls}"
    assert "target_scope=project_local" in detail_calls[0], (
        f"detail URL should carry target_scope=project_local, got {detail_calls[0]}"
    )


def test_q_pr4_langchange_rerenders_runtime_only_banner(page, mm_web_url: str) -> None:
    """``_ctxRefreshSectionState`` writes the runtime-only banner via
    ``textContent`` (not innerHTML), but the staleness mechanism is the
    same — ``I18N.applyDOM`` doesn't walk it. The listener's
    ``loadCtxList`` re-issue must rebuild the banner with the new locale.
    """
    install_default_stubs(page)
    # Single cwd-only scope with a runtime-only item triggers the banner.
    _stub_projects(
        page,
        {
            "scopes": [
                {
                    "scope_id": "cwd-scope",
                    "label": "cwd",
                    "root": "/srv/cwd",
                    "tier": "user",
                    "sources": ["server-cwd"],
                    "experimental": False,
                    "missing": False,
                    "counts": {"skills": 1, "commands": 0, "agents": 0},
                },
            ],
        },
    )
    _stub_skills(page, _CWD_SKILLS_RUNTIME_ONLY)
    page.goto(mm_web_url)
    _open_skills_list(page)

    page.wait_for_selector(
        "#ctx-skills-list .ctx-runtime-only-banner",
        timeout=5_000,
    )
    banner_pre = (
        page.locator("#ctx-skills-list .ctx-runtime-only-banner").text_content() or ""
    ).strip()
    # ``runtime_only_banner`` is a templated string; pin the EN-only token
    # ``Click Import`` and the KO-only token ``눌러`` to keep the assertion
    # robust against count/dir interpolation.
    assert "Click Import" in banner_pre, (
        f"EN runtime-only banner should contain 'Click Import': {banner_pre!r}"
    )

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.wait_for_function(
        "() => {"
        "  const el = document.querySelector('#ctx-skills-list .ctx-runtime-only-banner');"
        "  return el && el.textContent.includes('눌러');"
        "}",
        timeout=3_000,
    )
    banner_post = (
        page.locator("#ctx-skills-list .ctx-runtime-only-banner").text_content() or ""
    ).strip()
    assert "눌러" in banner_post, f"KO runtime-only banner should contain '눌러': {banner_post!r}"
    assert "Click Import" not in banner_post, (
        f"EN literal must not survive KO toggle: {banner_post!r}"
    )


def test_q_pr4_langchange_clears_import_status_box(page, mm_web_url: str) -> None:
    """The Import status box (``ctx-skills-status``) is rendered via
    inline ``t()`` in ``renderImportResult`` and would stale on toggle.
    The listener clears it (via the ``loadCtxList`` re-issue, which wipes
    ``statusEl.innerHTML`` near the top of the function) rather than
    caching the receipt — caching would resurrect a stale message in
    misleading form after navigation.
    """
    install_default_stubs(page)
    _stub_projects(page, _CWD_PROJECTS_WITH_NON_CWD_MISSING)
    _stub_skills(page, _CWD_SKILLS_ITEMS)
    page.goto(mm_web_url)
    _open_skills_list(page)

    # Inject a fake import receipt directly into the status box — exercising
    # the real Import button would require modal-handling for the confirm
    # dialog and CSRF dance; the receipt content is what stales, so writing
    # the rendered HTML directly is sufficient to pin the listener's clear
    # behavior.
    page.evaluate(
        """() => {
            const el = document.getElementById('ctx-skills-status');
            el.innerHTML = '<div class="ctx-import-result">'
                + '<div class="ctx-import-priority">EN-RECEIPT-MARKER</div>'
                + '</div>';
        }"""
    )
    pre = page.locator("#ctx-skills-status").inner_html()
    assert "EN-RECEIPT-MARKER" in pre, (
        f"precondition: marker must be in status box before toggle, got {pre!r}"
    )

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    # Wait for the listener's loadCtxList to clear the status box.
    page.wait_for_function(
        "() => {"
        "  const el = document.getElementById('ctx-skills-status');"
        "  return el && el.innerHTML === '';"
        "}",
        timeout=3_000,
    )
    post = page.locator("#ctx-skills-status").inner_html()
    assert post == "", f"Import status box must be cleared on toggle, got {post!r}"
    assert "EN-RECEIPT-MARKER" not in post, (
        f"stale import receipt must not survive language toggle: {post!r}"
    )


def test_q_pr4_langchange_preserves_unsaved_edit_buffer(page, mm_web_url: str) -> None:
    """Review finding (P1, data-loss): when the user has the canonical
    detail open in Edit mode with unsaved textarea changes, a language
    toggle would refetch and silently discard their work. The listener
    must capture the dirty buffer + edit-mode flag *before*
    ``loadCtxList`` resets the panes, then re-apply after
    ``loadCtxDetail``'s re-mount completes.

    This is distinct from the 409-conflict ``_ctxStashDraft`` path —
    that one stashes only when the user enters the conflict dialog, so
    a normal Edit-mode toggle has no protection there.

    Pin: enter Edit mode, type a sentinel into the textarea, toggle
    language, assert the textarea retains the user's content (not the
    server's freshly-fetched canonical_content) and the edit pane
    remains visible.
    """
    install_default_stubs(page)
    _stub_projects(page, _CWD_PROJECTS_WITH_NON_CWD_MISSING)
    _stub_skills(page, _CWD_SKILLS_ITEMS)
    page.route(
        "**/api/context/skills/demo-skill",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "name": "demo-skill",
                    "canonical_path": "/srv/cwd/.memtomem/skills/demo-skill.md",
                    "content": "SERVER-CANONICAL-CONTENT\n",
                    "mtime_ns": "1700000000000000000",
                    "files": [],
                }
            ),
        ),
    )
    page.goto(mm_web_url)
    _open_skills_list(page)
    page.wait_for_function(
        "() => document.querySelectorAll("
        '  \'#ctx-skills-list details[data-scope-id="cwd-scope"] '
        ".ctx-card').length > 0",
        timeout=5_000,
    )
    page.locator("#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-card").first.click()
    page.wait_for_selector("#ctx-skills-detail .ctx-detail-edit-btn", timeout=3_000)
    # Enter Edit mode — flips #ctx-pane-edit visible and hides the tabs
    # (per loadCtxDetail's edit handler).
    page.locator("#ctx-skills-detail .ctx-detail-edit-btn").click()
    page.wait_for_function(
        "() => {"
        "  const ep = document.querySelector('#ctx-skills-detail #ctx-pane-edit');"
        "  return ep && !ep.hidden;"
        "}",
        timeout=2_000,
    )
    # Type a sentinel — distinct from the server canonical_content so a
    # silent refetch-and-overwrite would be deterministically observable.
    page.evaluate(
        """() => {
            const ta = document.querySelector('#ctx-skills-detail #ctx-edit-content');
            ta.value = 'USER-IN-PROGRESS-DRAFT-SENTINEL';
        }"""
    )

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    # The listener should: stash the buffer, call loadCtxList (wipes
    # detail), call loadCtxDetail (refetches canonical), then re-apply
    # the buffer + reopen the edit pane on the freshly-mounted DOM.
    page.wait_for_function(
        "() => {"
        "  const ta = document.querySelector('#ctx-skills-detail #ctx-edit-content');"
        "  const ep = document.querySelector('#ctx-skills-detail #ctx-pane-edit');"
        "  return ta && ep && !ep.hidden && ta.value === 'USER-IN-PROGRESS-DRAFT-SENTINEL';"
        "}",
        timeout=4_000,
    )
    post_value = page.evaluate(
        "() => document.querySelector('#ctx-skills-detail #ctx-edit-content').value"
    )
    assert post_value == "USER-IN-PROGRESS-DRAFT-SENTINEL", (
        f"unsaved edit buffer must survive langchange — got: {post_value!r}"
    )
    # Symmetric negative: the server canonical content must NOT have
    # silently replaced the user's work.
    assert "SERVER-CANONICAL-CONTENT" not in post_value, (
        f"server canonical content must not silently overwrite the user's "
        f"in-progress edits on language toggle (data-loss regression): "
        f"{post_value!r}"
    )


def test_q_pr4_langchange_preserves_mtime_for_dirty_buffer(page, mm_web_url: str) -> None:
    """Review finding (P1, mtime conflict bypass): when a file changes
    on disk while the user has unsaved Edit-mode changes and they
    toggle language, ``loadCtxDetail`` overwrites
    ``detailEl.dataset.mtimeNs`` with the fresh on-disk mtime. If the
    listener didn't restore the *pre-toggle* mtime, the next Save
    would PUT the stale draft with the new mtime and bypass the
    backend's 409 conflict gate (issue #763), silently clobbering the
    external edit.

    Pin: open Edit mode at mtime=A, then route the canonical GET to
    return mtime=B (simulating an external edit during the toggle
    window), toggle language, assert ``detailEl.dataset.mtimeNs``
    still equals A so the next Save's PUT body carries the original
    pre-edit mtime.
    """
    install_default_stubs(page)
    _stub_projects(page, _CWD_PROJECTS_WITH_NON_CWD_MISSING)
    _stub_skills(page, _CWD_SKILLS_ITEMS)
    # Two distinct mtime values — A is the mtime the user started
    # editing against; B simulates an external edit landing during
    # the toggle window. The listener must keep A on the textarea
    # buffer so a Save re-surfaces the 409.
    initial_resp = {
        "name": "demo-skill",
        "canonical_path": "/srv/cwd/.memtomem/skills/demo-skill.md",
        "content": "ORIGINAL\n",
        "mtime_ns": "1700000000000000000",  # A
        "files": [],
    }
    refreshed_resp = {
        "name": "demo-skill",
        "canonical_path": "/srv/cwd/.memtomem/skills/demo-skill.md",
        "content": "EXTERNALLY-EDITED\n",
        "mtime_ns": "1800000000000000000",  # B
        "files": [],
    }
    call_count = {"n": 0}

    def _detail_handler(route):
        idx = call_count["n"]
        call_count["n"] += 1
        body = json.dumps(initial_resp if idx == 0 else refreshed_resp)
        route.fulfill(status=200, content_type="application/json", body=body)

    page.route("**/api/context/skills/demo-skill", _detail_handler)
    page.goto(mm_web_url)
    _open_skills_list(page)
    page.wait_for_function(
        "() => document.querySelectorAll("
        '  \'#ctx-skills-list details[data-scope-id="cwd-scope"] '
        ".ctx-card').length > 0",
        timeout=5_000,
    )
    page.locator("#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-card").first.click()
    page.wait_for_function(
        "() => document.querySelector('#ctx-skills-detail').dataset.mtimeNs "
        "=== '1700000000000000000'",
        timeout=3_000,
    )
    page.locator("#ctx-skills-detail .ctx-detail-edit-btn").click()
    page.wait_for_function(
        "() => {"
        "  const ep = document.querySelector('#ctx-skills-detail #ctx-pane-edit');"
        "  return ep && !ep.hidden;"
        "}",
        timeout=2_000,
    )
    page.evaluate(
        """() => {
            const ta = document.querySelector('#ctx-skills-detail #ctx-edit-content');
            ta.value = 'USER-IN-PROGRESS-DRAFT';
        }"""
    )

    # Toggle language. The detail GET handler returns mtime B on this
    # second call, simulating an external on-disk edit landing during
    # the toggle window.
    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.wait_for_function(
        "() => {"
        "  const ta = document.querySelector('#ctx-skills-detail #ctx-edit-content');"
        "  return ta && ta.value === 'USER-IN-PROGRESS-DRAFT';"
        "}",
        timeout=4_000,
    )
    # The critical assertion: dataset.mtimeNs must be A (pre-toggle),
    # not B (the freshly-read value). Without the listener's restore
    # step, loadCtxDetail's `detailEl.dataset.mtimeNs = data.mtime_ns`
    # write at line ~988 would have stamped B onto the element while
    # the textarea still carried A's draft — exactly the 409-bypass
    # the review flagged.
    final_mtime = page.evaluate(
        "() => document.querySelector('#ctx-skills-detail').dataset.mtimeNs"
    )
    assert final_mtime == "1700000000000000000", (
        f"pre-toggle mtime must be restored when the textarea carries an "
        f"unsaved buffer; got {final_mtime!r}. The freshly-read mtime "
        f"(1800...) on the dirty buffer would let Save bypass the 409 "
        f"conflict gate and silently overwrite the external edit."
    )
    assert final_mtime != "1800000000000000000", (
        f"refreshed mtime ({final_mtime!r}) must not stamp onto a dirty "
        f"buffer — that's the 409 bypass the review flagged"
    )


def test_q_pr4_rapid_toggle_preserves_edit_buffer(page, mm_web_url: str) -> None:
    """Review finding (P2): two back-to-back langchange events while
    a dirty Edit buffer is open must not drop the buffer. The first
    listener invocation captures into ``_ctxPendingEdit`` and kicks off
    detail fetch #1; before that fetch settles, the second invocation
    fires. By then ``loadCtxList`` has already wiped the detail DOM,
    so a closure-local capture would yield ``null`` and the second
    mount would have no buffer to apply — silently dropping the user's
    work. The module-level ``_ctxPendingEdit`` + ``_ctxDetailSeq``
    guard pattern means the latest mount's ``.then()`` consumes the
    stash; older `.then()`s skip via seq mismatch.

    Mutation-validated: removing the module-level stash and
    ``myDetailSeq !== _ctxDetailSeq[type]`` bail makes this spec fail
    with the textarea reverting to the server's freshly-fetched
    canonical content.
    """
    install_default_stubs(page)
    _stub_projects(page, _CWD_PROJECTS_WITH_NON_CWD_MISSING)
    _stub_skills(page, _CWD_SKILLS_ITEMS)
    page.route(
        "**/api/context/skills/demo-skill",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "name": "demo-skill",
                    "canonical_path": "/srv/cwd/.memtomem/skills/demo-skill.md",
                    "content": "SERVER-CANONICAL\n",
                    "mtime_ns": "1700000000000000000",
                    "files": [],
                }
            ),
        ),
    )
    page.goto(mm_web_url)
    _open_skills_list(page)
    page.wait_for_function(
        "() => document.querySelectorAll("
        '  \'#ctx-skills-list details[data-scope-id="cwd-scope"] '
        ".ctx-card').length > 0",
        timeout=5_000,
    )
    page.locator("#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-card").first.click()
    page.wait_for_selector("#ctx-skills-detail .ctx-detail-edit-btn", timeout=3_000)
    page.locator("#ctx-skills-detail .ctx-detail-edit-btn").click()
    page.wait_for_function(
        "() => {"
        "  const ep = document.querySelector('#ctx-skills-detail #ctx-pane-edit');"
        "  return ep && !ep.hidden;"
        "}",
        timeout=2_000,
    )
    page.evaluate(
        """() => {
            const ta = document.querySelector('#ctx-skills-detail #ctx-edit-content');
            ta.value = 'RAPID-BUFFER-SENTINEL';
        }"""
    )

    # Fire TWO langchange events in the same JS task so both listener
    # invocations enter before either's detail fetch can resolve. The
    # second invocation will see a wiped DOM (loadCtxList from the
    # first invocation already cleared detailEl synchronously). With a
    # closure-local capture, the second invocation would have no
    # buffer to apply and the buffer would be lost when the second
    # detail fetch's response paints. With the module-level stash +
    # seq-guard pattern, the buffer survives.
    page.evaluate(
        """() => {
            window.dispatchEvent(new Event('langchange'));
            window.dispatchEvent(new Event('langchange'));
        }"""
    )

    # Wait for the latest mount to complete and (re-)apply the buffer.
    page.wait_for_function(
        "() => {"
        "  const ta = document.querySelector('#ctx-skills-detail #ctx-edit-content');"
        "  const ep = document.querySelector('#ctx-skills-detail #ctx-pane-edit');"
        "  return ta && ep && !ep.hidden && ta.value === 'RAPID-BUFFER-SENTINEL';"
        "}",
        timeout=4_000,
    )
    final_value = page.evaluate(
        "() => document.querySelector('#ctx-skills-detail #ctx-edit-content').value"
    )
    assert final_value == "RAPID-BUFFER-SENTINEL", (
        f"buffer must survive rapid back-to-back langchange events; got {final_value!r}"
    )
    assert "SERVER-CANONICAL" not in final_value, (
        f"server canonical content must not silently overwrite the user's "
        f"in-progress edits across rapid toggles (review P2 race): {final_value!r}"
    )


def test_q_pr4_langchange_rerenders_dropped_chips_in_diff_pane(page, mm_web_url: str) -> None:
    """``renderDroppedChips`` lives inside the Diff pane of the canonical
    detail. Without active-tab capture, a ``loadCtxDetail`` re-mount
    lands on the Canonical pane (``Click Diff tab to load...``) and the
    chips don't re-render until the user re-clicks Diff. The listener
    must capture ``.ctx-detail-tab[data-pane="diff"].active`` and pass
    ``autoOpenDiff: true`` so ``_ctxLoadDiff`` runs on re-mount —
    review finding P1.
    """
    install_default_stubs(page)
    _stub_projects(page, _CWD_PROJECTS_WITH_NON_CWD_MISSING)
    _stub_skills(page, _CWD_SKILLS_ITEMS)
    # Canonical detail GET (any name match).
    page.route(
        "**/api/context/skills/demo-skill",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "name": "demo-skill",
                    "canonical_path": "/srv/cwd/.memtomem/skills/demo-skill.md",
                    "content": "name: demo\n",
                    "mtime_ns": "1700000000000000000",
                    "files": [],
                }
            ),
        ),
    )
    page.route(
        "**/api/context/skills/demo-skill/diff",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_DIFF_WITH_DROPPED)
        ),
    )
    page.goto(mm_web_url)
    _open_skills_list(page)
    page.wait_for_function(
        "() => document.querySelectorAll("
        '  \'#ctx-skills-list details[data-scope-id="cwd-scope"] '
        ".ctx-card').length > 0",
        timeout=5_000,
    )
    page.locator("#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-card").first.click()
    page.wait_for_selector("#ctx-skills-detail .ctx-detail-tab", timeout=3_000)
    # Click the Diff tab to make it the active pane (so the listener
    # captures ``wasDiffActive = true``).
    page.locator("#ctx-skills-detail .ctx-detail-tab[data-pane='diff']").click()
    page.wait_for_function(
        "() => document.querySelectorAll('#ctx-skills-detail .ctx-dropped-chip').length > 0",
        timeout=3_000,
    )
    pre_chip = (
        page.locator("#ctx-skills-detail .ctx-dropped-chip").first.text_content() or ""
    ).strip()
    assert "Dropped" in pre_chip, f"EN dropped chip should contain 'Dropped': {pre_chip!r}"

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    # The listener should:
    # 1. Capture wasDiffActive=true.
    # 2. Call loadCtxList('skills') — wipes detail.
    # 3. Call loadCtxDetail(..., { autoOpenDiff: true }) — re-mounts and
    #    fires _ctxLoadDiff via diffTab.click().
    # End state: chips re-render with KO text.
    page.wait_for_function(
        "() => {"
        "  const els = Array.from(document.querySelectorAll("
        "    '#ctx-skills-detail .ctx-dropped-chip'));"
        "  return els.length > 0 && els.every(el => el.textContent.includes('제거됨'));"
        "}",
        timeout=4_000,
    )
    post_chip = (
        page.locator("#ctx-skills-detail .ctx-dropped-chip").first.text_content() or ""
    ).strip()
    assert "제거됨" in post_chip, f"KO dropped chip should contain '제거됨': {post_chip!r}"
    assert "Dropped" not in post_chip, (
        f"EN literal must not survive KO toggle in dropped chip: {post_chip!r}"
    )


def test_q_pr4_langchange_rerenders_runtime_only_detail(page, mm_web_url: str) -> None:
    """A runtime-only detail pane is mounted by ``_ctxLoadRuntimeOnlyDetail``
    (not ``loadCtxDetail`` — that 404s for items with no canonical). The
    listener must read ``_ctxCurrentDetail.runtimeOnly`` and route the
    re-mount through the runtime-only loader; otherwise the detail
    silently degrades to ``emptyState`` (review finding P2).

    Pin: EN ``Runtime preview — not yet in .memtomem/.`` → KO
    ``런타임 미리보기 — 아직 .memtomem/ 에 없습니다.``
    """
    install_default_stubs(page)
    _stub_projects(
        page,
        {
            "scopes": [
                {
                    "scope_id": "cwd-scope",
                    "label": "cwd",
                    "root": "/srv/cwd",
                    "tier": "user",
                    "sources": ["server-cwd"],
                    "experimental": False,
                    "missing": False,
                    "counts": {"skills": 1, "commands": 0, "agents": 0},
                }
            ]
        },
    )
    _stub_skills(page, _CWD_SKILLS_RUNTIME_ONLY)
    # Canonical GET 404s for runtime-only — match what the real backend
    # does so a future regression that drops the runtimeOnly flag and
    # falls into loadCtxDetail surfaces as emptyState (KO 미리보기 absent).
    page.route(
        "**/api/context/skills/drift-skill",
        lambda r: r.fulfill(
            status=404,
            content_type="application/json",
            body=json.dumps({"detail": "not found"}),
        ),
    )
    page.route(
        "**/api/context/skills/drift-skill/diff",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_DIFF_RUNTIME_ONLY)
        ),
    )
    page.goto(mm_web_url)
    _open_skills_list(page)
    page.wait_for_function(
        "() => document.querySelectorAll("
        '  \'#ctx-skills-list details[data-scope-id="cwd-scope"] '
        ".ctx-card').length > 0",
        timeout=5_000,
    )
    page.locator("#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-card").first.click()
    page.wait_for_function(
        "() => {"
        "  const el = document.querySelector('#ctx-skills-detail');"
        "  return el && el.textContent.includes('Runtime preview');"
        "}",
        timeout=5_000,
    )

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.wait_for_function(
        "() => {"
        "  const el = document.querySelector('#ctx-skills-detail');"
        "  return el && el.textContent.includes('런타임 미리보기');"
        "}",
        timeout=4_000,
    )
    detail_text = (page.locator("#ctx-skills-detail").text_content() or "").strip()
    assert "런타임 미리보기" in detail_text, (
        f"KO runtime-only hint must be present after toggle: {detail_text!r}"
    )
    # Negative: the EN literal must not linger AND the emptyState fallback
    # (which would render if a regression dropped the runtimeOnly flag and
    # the listener fell into loadCtxDetail's 404 path) must not appear.
    assert "Runtime preview" not in detail_text, (
        f"EN literal must not survive KO toggle: {detail_text!r}"
    )


def test_q_pr4_langchange_off_section_does_not_call_loadCtxList(page, mm_web_url: str) -> None:
    """Off-section gate: a language toggle from a non-list page (e.g.,
    Search tab, or Settings → Hooks) must not fire
    ``/api/context/projects``. The dashboard's per-type section elements
    are always present in the DOM, so the listener must gate on
    ``classList.contains('active')`` rather than element existence.
    Mirror of ``test_langchange_off_overview_does_not_refetch_overview``
    in the overview suite.
    """
    install_default_stubs(page)

    projects_calls: list[str] = []

    def _projects_handler(route):
        projects_calls.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_CWD_PROJECTS_WITH_NON_CWD_MISSING),
        )

    page.route("**/api/context/projects", _projects_handler)
    _stub_skills(page, _CWD_SKILLS_ITEMS)
    page.goto(mm_web_url)
    page.locator("#tabbtn-search").click()
    page.wait_for_selector("#tabbtn-search.active", timeout=2_000)
    assert projects_calls == [], (
        f"loadCtxList must not fire on Search tab boot; got {projects_calls!r}"
    )

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.evaluate("async () => { await I18N.setLang('en'); }")
    page.wait_for_timeout(300)
    assert projects_calls == [], (
        f"language toggle from off-section must not fetch projects; "
        f"saw {projects_calls!r} (Q-PR4 off-section gate)"
    )


# NOTE on race-window coverage:
#
# Earlier drafts of this file carried two browser specs that exercised
# the ``_ctxListSeq`` guard via overlapping fetches (success-path "stale
# fetch wins" and catch-path "late failure paints emptyState"). They
# were flaky in this harness — Playwright python's sync route
# dispatcher serializes handlers, so overlapping ``threading.Event``
# gates either deadlocked or scheduled non-deterministically.
#
# Shape parity with PR #824's ``_ctxOverviewSeq`` is enforced by the
# static test ``test_q_pr4_loadCtxList_has_sequence_guard_all_sites`` in
# ``test_i18n.py``: it asserts ≥4 ``_ctxListSeq[type]`` guards
# (loadCtxList success + catch + _loadScopeGroupItems success + catch),
# which is the same family of mechanism PR #824 already proved out at
# the integration level for the overview path. Mutating any of the four
# guards out of the source makes the static test fail; the runtime
# behavior is identical to the (already-tested) overview equivalent.
