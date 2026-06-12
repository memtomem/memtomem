"""Browser tests for the tier-aware web write affordances (#943, #1263).

PR #940 wired ``target_scope`` through every Context Gateway artifact
route; #943 made the SPA dim every write button on non-shared tiers.
#1263/#1302 then opened the skills/commands/agents write routes to the
``user`` tier behind the ``allow_host_writes`` disclose-then-confirm
round-trip, so the gate is now selective. These specs pin the contract:

  * ``user`` tier: skills/commands/agents Create / Import / Sync (and
    per-item Edit / Delete) stay LIVE — the server's
    ``needs_confirmation`` envelope owns the consent step. MCP Server
    buttons stay dim (their routes remain project_shared-only by
    design, ADR-0011 §1), and a banner explains the confirm-first
    contract instead of the old read-only claim.
  * ``project_local`` tier: everything stays dim
    (``data-write-blocked="project_local"`` + ``aria-disabled`` + i18n
    ``title``), the draft-tier banner renders, dim-button clicks are
    intercepted at the document level (no fetch, toast instead), and
    locale flips re-translate the tooltips.
  * Switching back to ``project_shared`` clears the gate state.
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
    """Click the tier-filter button for ``scope`` in the shared gateway control
    bar (rank 11: hoisted out of the per-section content) and wait for the
    active class to flip — proxy for "the SPA has observed the click and started
    the re-render."""
    page.locator(f"#ctx-control-bar .ctx-tier-filter button[data-scope='{scope}']").click()
    page.wait_for_function(
        f"() => {{ const b = document.querySelector("
        f"'#ctx-control-bar .ctx-tier-filter button[data-scope=\"{scope}\"]'); "
        f"return b && b.classList.contains('active'); }}",
        timeout=3_000,
    )


# ---------------------------------------------------------------------------
# Specs
# ---------------------------------------------------------------------------


def test_user_tier_keeps_open_family_buttons_live_and_gates_mcp(page, mm_web_url: str) -> None:
    """Tier filter = user (#1263) → the skills section's Create / Import /
    Sync stay LIVE (the server's needs_confirmation envelope owns consent
    now), the MCP Servers section's buttons stay gated (their routes are
    project_shared-only by design), Sync All stays gated, and the banner
    explains the confirm-first contract instead of claiming read-only.
    """
    install_default_stubs(page)
    _stub_projects_and_skills(page)
    page.goto(mm_web_url)
    _open_skills(page)
    _switch_tier(page, "user")

    # The sweep is synchronous with the tier click; wait on the banner
    # (inserted by the list re-render) as the settled signal.
    page.wait_for_selector(
        "#ctx-skills-list .ctx-write-blocked-banner[data-tier='user']",
        timeout=3_000,
    )

    for cls in ("ctx-create-btn", "ctx-import-btn", "ctx-sync-btn"):
        btn = page.locator(f"#settings-ctx-skills .{cls}")
        assert btn.get_attribute("data-write-blocked") is None, (
            f"{cls!r} must stay live on user tier (#1263); got "
            f"data-write-blocked={btn.get_attribute('data-write-blocked')!r}"
        )
        assert btn.get_attribute("aria-disabled") is None, (
            f"{cls!r} must not announce aria-disabled on user tier"
        )

    # The write-block sweep is document-wide, so the (hidden) MCP Servers
    # toolbar reflects the gate without navigating to the section. No
    # Import button there — _CTX_TOOLBAR_CAPS['mcp-servers'].import=false.
    for cls in ("ctx-create-btn", "ctx-sync-btn"):
        btn = page.locator(f"#settings-ctx-mcp-servers .{cls}")
        assert btn.get_attribute("data-write-blocked") == "user", (
            f"mcp-servers {cls!r} must stay gated on user tier; got "
            f"data-write-blocked={btn.get_attribute('data-write-blocked')!r}"
        )
        title = btn.get_attribute("title") or ""
        assert "project_shared-only" in title.lower(), (
            f"mcp-servers {cls!r} title must surface the shared-only copy; got {title!r}"
        )

    sync_all = page.locator("#ctx-sync-all-btn")
    assert sync_all.get_attribute("data-write-blocked") == "user", (
        "Sync All must stay gated on user tier (multi-phase run hits "
        "settings + mcp-servers)"
    )

    banner = page.locator("#ctx-skills-list .ctx-write-blocked-banner[data-tier='user']")
    assert banner.count() == 1, "user-tier banner must be present exactly once"
    banner_text = (banner.text_content() or "").strip()
    assert "confirmation" in banner_text.lower(), (
        f"banner must surface the confirm-first copy; got {banner_text!r}"
    )
    assert "read-only" not in banner_text.lower(), (
        f"banner must not claim read-only on the now-writable user tier; got {banner_text!r}"
    )
    remediation = page.locator(
        "#ctx-skills-list .ctx-missing-canonical-remediation[data-tier='user']"
    )
    assert remediation.count() == 0, (
        "canonical-present user-tier list must not show missing-canonical remediation"
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
    # The copy-polish pass replaced the "no runtime fan-out (ADR-0011 §3)"
    # jargon with plain draft-tier language; pin the new markers (draft
    # framing + never-pushed direction) — still distinct from the user-tier
    # read-only/CLI copy, which the negative below also guards.
    assert "draft" in banner_text.lower() and "never pushed" in banner_text.lower(), (
        f"banner must surface the project_local draft-tier copy; got {banner_text!r}"
    )

    # Negative: the user-tier banner must NOT also be present — the two
    # are mutually exclusive per the ``_ctxTargetScope`` source of truth.
    user_banner = page.locator("#ctx-skills-list .ctx-write-blocked-banner[data-tier='user']")
    assert user_banner.count() == 0, (
        "user-tier banner must not coexist with the project_local banner"
    )


def test_project_shared_tier_clears_write_blocked_state(page, mm_web_url: str) -> None:
    """Symmetric guardrail: flipping from project_local → project_shared
    must clear ``data-write-blocked`` + ``aria-disabled`` + tooltip
    override and remove the tier banner. Without this, the gate state
    would persist across tier flips and the user would never see the
    write buttons re-enable. (project_local is the fully-blocked tier
    since #1263 opened the user tier for these sections.)
    """
    install_default_stubs(page)
    _stub_projects_and_skills(page)
    page.goto(mm_web_url)
    _open_skills(page)
    _switch_tier(page, "project_local")

    # Precondition: gate is on.
    page.wait_for_selector(
        "#settings-ctx-skills .ctx-create-btn[data-write-blocked='project_local']",
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
        "tier banner must be removed on project_shared"
    )


def test_blocked_create_click_fires_toast_and_skips_post(page, mm_web_url: str) -> None:
    """Clicking a dim Create button on project_local must NOT issue
    ``POST /api/context/skills`` (the route would 400). The
    document-level capture-phase intercept stops the event before the
    per-button handler runs. (user-tier clicks are no longer blocked —
    they ride the needs_confirmation round-trip instead, #1263.)
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
            body=json.dumps({"detail": "Create skill rejected: target_scope='project_local'"}),
        )

    page.route("**/api/context/skills", _on_create)

    page.goto(mm_web_url)
    _open_skills(page)
    _switch_tier(page, "project_local")

    page.wait_for_selector(
        "#settings-ctx-skills .ctx-create-btn[data-write-blocked='project_local']",
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
    assert "draft tier" in text.lower(), (
        f"toast must surface the draft-tier explanation; got {text!r}"
    )
    assert create_post_calls == [], (
        f"blocked Create click must not issue a POST; saw {create_post_calls!r}"
    )

    # The Create form (.ctx-create-form) must not have been injected —
    # that would prove the per-button handler ran despite the gate.
    assert page.locator("#ctx-skills-list .ctx-create-form").count() == 0, (
        "blocked Create click must not inject the create form"
    )


def test_per_item_edit_and_delete_buttons_follow_tier_gate(page, mm_web_url: str) -> None:
    """Per-item Edit / Delete buttons are minted by ``loadCtxDetail``
    AFTER the section list lands, so the gate must re-apply on detail
    mount. Pin both sides of the #1263 contract: on the user tier the
    mounted detail buttons stay LIVE; flipping to project_local sweeps
    the already-mounted buttons into the blocked state.
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

    page.wait_for_selector("#ctx-skills-detail .ctx-detail-edit-btn", timeout=3_000)

    for cls in ("ctx-detail-edit-btn", "ctx-detail-delete-btn"):
        btn = page.locator(f"#ctx-skills-detail .{cls}")
        assert btn.get_attribute("data-write-blocked") is None, (
            f"detail {cls!r} must stay live on user tier (#1263); got "
            f"data-write-blocked={btn.get_attribute('data-write-blocked')!r}"
        )

    # Tier flip re-renders the list and WIPES the mounted detail
    # (loadCtxList hides + clears it), so the blocked state is pinned on
    # the re-mount path: open the card again on the blocked tier and the
    # freshly-minted detail buttons must come up gated.
    _switch_tier(page, "project_local")
    page.wait_for_selector(
        "#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-card",
        timeout=5_000,
    )
    page.locator("#ctx-skills-list details[data-scope-id='cwd-scope'] .ctx-card").first.click()
    page.wait_for_selector(
        "#ctx-skills-detail .ctx-detail-edit-btn[data-write-blocked='project_local']",
        timeout=3_000,
    )
    for cls in ("ctx-detail-edit-btn", "ctx-detail-delete-btn"):
        btn = page.locator(f"#ctx-skills-detail .{cls}")
        assert btn.get_attribute("data-write-blocked") == "project_local", (
            f"detail {cls!r} must be gated on project_local; got "
            f"data-write-blocked={btn.get_attribute('data-write-blocked')!r}"
        )
        assert btn.get_attribute("aria-disabled") == "true", (
            f"detail {cls!r} must announce aria-disabled on project_local"
        )


def test_write_blocked_banner_sits_above_runtime_only_banner(page, mm_web_url: str) -> None:
    """Ordering invariant when a non-shared tier list contains only
    runtime-only items: the read-only banner (``.ctx-write-blocked-banner``)
    must sit ABOVE the runtime-only "Click Import to canonicalize"
    banner (``.ctx-runtime-only-banner``). Both inserters use
    ``listEl.insertBefore(_, listEl.firstChild)`` by default — without
    the explicit anchor in ``_ctxRefreshSectionState``, the
    later-firing runtime-only banner wins position 0 and tells the
    user to click an Import button that the write-block gate has
    already disabled. PR #945 review (P3).
    """
    install_default_stubs(page)
    # Cwd scope with one runtime-only item (canonical_path="") triggers
    # the runtime-only banner via ``_ctxRefreshSectionState``.
    runtime_only_skills = {
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
    _stub_projects_and_skills(page, skills_payload=runtime_only_skills)
    page.goto(mm_web_url)
    _open_skills(page)
    _switch_tier(page, "user")

    # Wait for both banners to be in the DOM. ``_ctxRefreshSectionState``
    # runs inside ``_loadScopeGroupItems`` after the cwd scope's items
    # fetch resolves, so the runtime-only banner is the lagging signal.
    page.wait_for_selector("#ctx-skills-list .ctx-runtime-only-banner", timeout=5_000)
    page.wait_for_selector("#ctx-skills-list .ctx-write-blocked-banner", timeout=2_000)
    remediation_text = (
        page.locator(
            "#ctx-skills-list .ctx-missing-canonical-remediation[data-tier='user']"
        ).text_content()
        or ""
    )
    # #1263: the web Import path works on the user tier now (behind the
    # host-write confirm), so the remediation names it — and must not
    # carry the old read-only claim.
    assert "import" in remediation_text.lower(), (
        f"user-tier remediation must point at the Import path; got {remediation_text!r}"
    )
    assert "read-only" not in remediation_text.lower(), (
        f"user-tier remediation must not claim read-only anymore; got {remediation_text!r}"
    )
    assert "mm context init --include=agents,commands,skills --scope user" in remediation_text
    assert "mm context sync --include=agents,commands,skills --scope user" in remediation_text

    # Compare DOM positions. ``compareDocumentPosition`` returns 4
    # (DOCUMENT_POSITION_FOLLOWING) when the right argument follows the
    # left in tree order. We assert the write-blocked banner precedes
    # the runtime-only banner.
    relation = page.evaluate(
        """() => {
            const wb = document.querySelector('#ctx-skills-list .ctx-write-blocked-banner');
            const ro = document.querySelector('#ctx-skills-list .ctx-runtime-only-banner');
            if (!wb || !ro) return 'missing';
            // Node.DOCUMENT_POSITION_FOLLOWING === 4
            return (wb.compareDocumentPosition(ro) & 4) ? 'wb_first' : 'ro_first';
        }"""
    )
    assert relation == "wb_first", (
        f"write-blocked banner must precede runtime-only banner; got relation={relation!r}. "
        f"A runtime-only 'Click Import' banner above the read-only banner would tell "
        f"users to press a button the gate has disabled."
    )


def test_project_local_missing_canonical_remediation_shows_draft_cli_flow(
    page, mm_web_url: str
) -> None:
    """project_local runtime-only lists use the draft/no-fan-out remediation,
    including the explicit CLI commands for the draft tier.
    """
    install_default_stubs(page)
    runtime_only_skills = {
        "skills": [
            {
                "name": "draft-only",
                "canonical_path": "",
                "runtimes": [
                    {
                        "runtime": "claude_skills",
                        "status": "missing canonical",
                        "runtime_path": "/srv/cwd/.claude/skills/draft-only.md",
                        "runtime_content": "# Draft only\n",
                    }
                ],
            }
        ],
        "scanned_dirs": ["/srv/cwd/.claude/skills/"],
    }
    _stub_projects_and_skills(page, skills_payload=runtime_only_skills)
    page.goto(mm_web_url)
    _open_skills(page)
    _switch_tier(page, "project_local")

    remediation = page.locator(
        "#ctx-skills-list .ctx-missing-canonical-remediation[data-tier='project_local']"
    )
    remediation.wait_for(timeout=5_000)
    text = remediation.text_content() or ""
    assert "draft" in text.lower() or "fan-out" in text.lower(), (
        f"project_local remediation must explain draft/no-fan-out semantics; got {text!r}"
    )
    assert "mm context init --include=agents,commands,skills --scope project_local" in text
    assert "mm context sync --include=agents,commands,skills --scope project_local" in text


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
    _switch_tier(page, "project_local")

    create_btn = page.locator("#settings-ctx-skills .ctx-create-btn")
    pre_title = create_btn.get_attribute("title") or ""
    assert "draft tier" in pre_title.lower(), (
        f"EN precondition: title must be EN copy; got {pre_title!r}"
    )

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.wait_for_function(
        "() => {"
        "  const b = document.querySelector('#settings-ctx-skills .ctx-create-btn');"
        "  return b && (b.title || '').includes('초안');"
        "}",
        timeout=3_000,
    )
    post_title = create_btn.get_attribute("title") or ""
    assert "초안" in post_title, (
        f"KO title must replace EN copy after langchange; got {post_title!r}"
    )
    # Symmetric negative: the EN string must not linger.
    assert "draft tier" not in post_title.lower(), (
        f"EN copy must not survive langchange; got {post_title!r}"
    )
