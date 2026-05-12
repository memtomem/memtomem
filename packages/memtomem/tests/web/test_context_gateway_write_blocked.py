"""Browser tests for ADR-0011 §5 / #943 tier-aware web write affordances.

PR #940 wired ``target_scope`` through every Context Gateway artifact
route and added ``_reject_non_shared_write`` to 400-block create / update
/ delete / sync / import on the ``user`` and ``project_local`` tiers.
The route-level decision is correct but invisible to a user clicking
the Web UI: pressing Create on a user-tier list produced a generic
toast error rather than an explicit "this tier is read-only" cue.

#943 closes that gap by making the SPA dim every write button and
insert a tier-aware banner when the canonical-tier filter is set to a
non-shared tier. These specs pin the UX contract:

  * Per-section Create / Import / Sync buttons carry
    ``data-write-blocked="<tier>"`` + ``aria-disabled="true"`` + an
    i18n-aware ``title``.
  * A ``.ctx-write-blocked-banner[data-tier="<tier>"]`` sits at the top
    of the list when the filter is non-shared.
  * Clicks on the dim buttons are intercepted at the document level —
    no fetch reaches the server; a toast surfaces the explanation
    instead.
  * Switching back to ``project_shared`` clears the gate state.
  * Per-item Edit / Delete (and the runtime-only Import-this CTA)
    inside the detail panel pick up the same gate when they mount.
  * Locale toggles re-translate the dim button tooltips so the hover
    text stays in sync with the active locale.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_PROJECTS_SINGLE_CWD = {
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
}


_SKILLS_ONE_ITEM = {
    "skills": [
        {
            "name": "demo-skill",
            "canonical_path": "/srv/cwd/.memtomem/skills/demo-skill.md",
            "runtimes": [{"runtime": "claude_skills", "status": "in sync"}],
        }
    ],
    "scanned_dirs": ["/srv/cwd/.claude/skills/"],
}


def _stub_projects_and_skills(page, *, skills_payload=None):
    """Register the projects + skills stubs with patterns wide enough to
    match the ``?target_scope=...`` / ``?scope_id=...`` query strings the
    SPA appends when the tier filter is non-shared."""
    skills_payload = skills_payload or _SKILLS_ONE_ITEM
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_PROJECTS_SINGLE_CWD),
        ),
    )
    page.route(
        "**/api/context/skills**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(skills_payload),
        ),
    )


def _open_skills(page):
    """Land on Settings → Skills with the list populated."""
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-skills')")
    page.wait_for_function(
        "() => document.querySelectorAll('#ctx-skills-list .ctx-scope-group').length > 0",
        timeout=5_000,
    )


def _switch_tier(page, scope: str) -> None:
    """Click the tier-filter button for ``scope`` inside the Skills section
    and wait for the active class to flip — proxy for "the SPA has
    observed the click and started the re-render."""
    page.locator(f"#ctx-skills-list .ctx-tier-filter button[data-scope='{scope}']").click()
    page.wait_for_function(
        f"() => {{ const b = document.querySelector("
        f"'#ctx-skills-list .ctx-tier-filter button[data-scope=\"{scope}\"]'); "
        f"return b && b.classList.contains('active'); }}",
        timeout=3_000,
    )


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


def test_user_tier_gates_section_write_buttons_and_inserts_banner(page, mm_web_url: str) -> None:
    """Tier filter = user → Create / Import / Sync buttons carry the
    full gate state, and the read-only banner appears at the top of
    the list with the user-tier copy.
    """
    install_default_stubs(page)
    _stub_projects_and_skills(page)
    page.goto(mm_web_url)
    _open_skills(page)
    _switch_tier(page, "user")

    page.wait_for_selector(
        "#settings-ctx-skills .ctx-create-btn[data-write-blocked='user']",
        timeout=3_000,
    )

    for cls in ("ctx-create-btn", "ctx-import-btn", "ctx-sync-btn"):
        btn = page.locator(f"#settings-ctx-skills .{cls}")
        assert btn.get_attribute("data-write-blocked") == "user", (
            f"{cls!r} must be gated on user tier; got "
            f"data-write-blocked={btn.get_attribute('data-write-blocked')!r}"
        )
        assert btn.get_attribute("aria-disabled") == "true", (
            f"{cls!r} must announce aria-disabled on user tier"
        )
        # Tooltip carries the i18n-resolved EN copy (default locale).
        title = btn.get_attribute("title") or ""
        assert "read-only" in title.lower() or "manage these" in title.lower(), (
            f"{cls!r} title must surface the user-tier read-only copy; got {title!r}"
        )

    banner = page.locator("#ctx-skills-list .ctx-write-blocked-banner[data-tier='user']")
    assert banner.count() == 1, "user-tier banner must be present exactly once"
    banner_text = (banner.text_content() or "").strip()
    assert "user-tier" in banner_text.lower() or "read-only" in banner_text.lower(), (
        f"banner must surface the user-tier read-only copy; got {banner_text!r}"
    )


def test_project_local_tier_uses_project_local_specific_copy(page, mm_web_url: str) -> None:
    """Tier filter = project_local → the tier-aware copy is the
    project_local variant, not the generic user-tier one. The
    distinction matters because the project_local message points
    users at project_shared as the publish path (ADR-0011 §3
    zero-fan-out), while the user-tier message points them at the CLI.
    """
    install_default_stubs(page)
    _stub_projects_and_skills(page)
    page.goto(mm_web_url)
    _open_skills(page)
    _switch_tier(page, "project_local")

    page.wait_for_selector(
        "#settings-ctx-skills .ctx-create-btn[data-write-blocked='project_local']",
        timeout=3_000,
    )

    create_btn = page.locator("#settings-ctx-skills .ctx-create-btn")
    title = create_btn.get_attribute("title") or ""
    assert "project_local" in title or "fan-out" in title.lower(), (
        f"project_local-tier title must surface the no-fan-out copy; got {title!r}"
    )

    banner = page.locator("#ctx-skills-list .ctx-write-blocked-banner[data-tier='project_local']")
    assert banner.count() == 1, "project_local-tier banner must be present exactly once"
    banner_text = (banner.text_content() or "").strip()
    assert "project_local" in banner_text or "fan-out" in banner_text.lower(), (
        f"banner must surface the project_local-tier copy; got {banner_text!r}"
    )

    # Negative: the user-tier banner must NOT also be present — the two
    # are mutually exclusive per the ``_ctxTargetScope`` source of truth.
    user_banner = page.locator("#ctx-skills-list .ctx-write-blocked-banner[data-tier='user']")
    assert user_banner.count() == 0, (
        "user-tier banner must not coexist with the project_local banner"
    )


def test_project_shared_tier_clears_write_blocked_state(page, mm_web_url: str) -> None:
    """Symmetric guardrail: flipping from user → project_shared must
    clear ``data-write-blocked`` + ``aria-disabled`` + tooltip override
    and remove the read-only banner. Without this, the gate state would
    persist across tier flips and the user would never see the write
    buttons re-enable.
    """
    install_default_stubs(page)
    _stub_projects_and_skills(page)
    page.goto(mm_web_url)
    _open_skills(page)
    _switch_tier(page, "user")

    # Precondition: gate is on.
    page.wait_for_selector(
        "#settings-ctx-skills .ctx-create-btn[data-write-blocked='user']",
        timeout=3_000,
    )

    # Flip back to project_shared.
    _switch_tier(page, "project_shared")
    page.wait_for_function(
        "() => {"
        "  const b = document.querySelector('#settings-ctx-skills .ctx-create-btn');"
        "  return b && !b.hasAttribute('data-write-blocked');"
        "}",
        timeout=3_000,
    )

    for cls in ("ctx-create-btn", "ctx-import-btn", "ctx-sync-btn"):
        btn = page.locator(f"#settings-ctx-skills .{cls}")
        assert btn.get_attribute("data-write-blocked") is None, (
            f"{cls!r} must have data-write-blocked cleared on project_shared"
        )
        assert btn.get_attribute("aria-disabled") is None, (
            f"{cls!r} must clear aria-disabled on project_shared"
        )

    assert page.locator("#ctx-skills-list .ctx-write-blocked-banner").count() == 0, (
        "read-only banner must be removed on project_shared"
    )


def test_blocked_create_click_fires_toast_and_skips_post(page, mm_web_url: str) -> None:
    """Clicking a dim Create button on a non-shared tier must NOT
    issue ``POST /api/context/skills`` (the route would 400 with
    ``Create skill rejected: ...``). The document-level capture-phase
    intercept stops the event before the per-button handler runs.
    """
    install_default_stubs(page)
    _stub_projects_and_skills(page)

    create_post_calls: list[str] = []

    def _on_create(route):
        if route.request.method == "POST":
            create_post_calls.append(route.request.url)
        route.fulfill(
            status=400,
            content_type="application/json",
            body=json.dumps({"detail": "Create skill rejected: target_scope='user'"}),
        )

    page.route("**/api/context/skills", _on_create)

    page.goto(mm_web_url)
    _open_skills(page)
    _switch_tier(page, "user")

    page.wait_for_selector(
        "#settings-ctx-skills .ctx-create-btn[data-write-blocked='user']",
        timeout=3_000,
    )
    # ``force=True`` bypasses Playwright's actionability check, which
    # treats ``aria-disabled="true"`` as "not enabled". A real browser
    # still dispatches the click on such elements — that's exactly
    # the path the document-level capture-phase intercept must catch.
    page.locator("#settings-ctx-skills .ctx-create-btn").click(force=True)

    # Toast must surface, and no POST must have fired.
    toast = page.wait_for_selector("#toast-container .toast", timeout=2_000)
    text = toast.text_content() or ""
    assert "read-only" in text.lower() or "user-tier" in text.lower(), (
        f"toast must surface the read-only explanation; got {text!r}"
    )
    assert create_post_calls == [], (
        f"blocked Create click must not issue a POST; saw {create_post_calls!r}"
    )

    # The Create form (.ctx-create-form) must not have been injected —
    # that would prove the per-button handler ran despite the gate.
    assert page.locator("#ctx-skills-list .ctx-create-form").count() == 0, (
        "blocked Create click must not inject the create form"
    )


def test_user_tier_gates_per_item_edit_and_delete_buttons(page, mm_web_url: str) -> None:
    """Per-item Edit / Delete buttons are minted by ``loadCtxDetail``
    AFTER the section list lands, so the gate must re-apply on detail
    mount. Pin: open a card on the user tier and assert the detail
    pane's edit / delete buttons are in the blocked state.
    """
    install_default_stubs(page)
    _stub_projects_and_skills(page)
    # Detail endpoint stub — return a minimal canonical payload so
    # ``loadCtxDetail`` mounts edit / diff / delete buttons.
    page.route(
        "**/api/context/skills/demo-skill**",
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
    page.goto(mm_web_url)
    _open_skills(page)
    _switch_tier(page, "user")

    # Card click → detail mount.
    page.wait_for_selector(
        "#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-card",
        timeout=5_000,
    )
    page.locator("#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-card").first.click()

    page.wait_for_selector(
        "#ctx-skills-detail .ctx-detail-edit-btn[data-write-blocked='user']",
        timeout=3_000,
    )

    for cls in ("ctx-detail-edit-btn", "ctx-detail-delete-btn"):
        btn = page.locator(f"#ctx-skills-detail .{cls}")
        assert btn.get_attribute("data-write-blocked") == "user", (
            f"detail {cls!r} must be gated on user tier; got "
            f"data-write-blocked={btn.get_attribute('data-write-blocked')!r}"
        )
        assert btn.get_attribute("aria-disabled") == "true", (
            f"detail {cls!r} must announce aria-disabled on user tier"
        )


def test_langchange_re_translates_write_blocked_tooltips(page, mm_web_url: str) -> None:
    """The ``title`` attribute on dim write buttons is set via inline
    ``t()`` (not ``data-i18n-title``), so ``I18N.applyDOM`` does not
    re-translate it on locale flip. The ``langchange`` listener must
    call ``_ctxRefreshWriteBlockedState`` to rewrite the tooltip with
    the new locale. Without it, EN → KO would leave the EN title
    stale until the next list re-render.
    """
    install_default_stubs(page)
    _stub_projects_and_skills(page)
    page.goto(mm_web_url)
    _open_skills(page)
    _switch_tier(page, "user")

    create_btn = page.locator("#settings-ctx-skills .ctx-create-btn")
    pre_title = create_btn.get_attribute("title") or ""
    assert "read-only" in pre_title.lower() or "manage these" in pre_title.lower(), (
        f"EN precondition: title must be EN copy; got {pre_title!r}"
    )

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.wait_for_function(
        "() => {"
        "  const b = document.querySelector('#settings-ctx-skills .ctx-create-btn');"
        "  return b && (b.title || '').includes('읽기 전용');"
        "}",
        timeout=3_000,
    )
    post_title = create_btn.get_attribute("title") or ""
    assert "읽기 전용" in post_title, (
        f"KO title must replace EN copy after langchange; got {post_title!r}"
    )
    # Symmetric negative: the EN string must not linger.
    assert "read-only" not in post_title.lower(), (
        f"EN copy must not survive langchange; got {post_title!r}"
    )
