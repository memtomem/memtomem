"""A server restart keeps compose drafts and never replays a write (#2746)."""

import json

import pytest
from playwright.sync_api import expect

from .conftest import goto_after_i18n_init, install_default_stubs

pytestmark = pytest.mark.browser


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_restart_preserves_draft_until_user_retries(page, mm_web_url, refresh_fails):
    install_default_stubs(page)
    session_calls = []
    writes = []

    def session(route):
        session_calls.append(True)
        failed = refresh_fails and len(session_calls) > 1
        route.fulfill(
            status=503 if failed else 200,
            content_type="application/json",
            body=json.dumps({"csrf": "A" if len(session_calls) == 1 else "B"}),
        )

    def add(route):
        writes.append(route.request)
        stale = route.request.headers.get("x-memtomem-csrf") != "B"
        route.fulfill(
            status=403 if stale else 200,
            content_type="application/json",
            body=json.dumps(
                {"code": "csrf_token_mismatch"}
                if stale
                else {"indexed_chunks": 1, "file": "/tmp/example.md"}
            ),
        )

    page.route("**/api/session", session)
    page.route("**/api/add", add)
    goto_after_i18n_init(page, mm_web_url, lang="en")
    page.evaluate("async () => { await ensureCsrfToken(); activateTab('index'); }")
    page.locator("#index-mode-compose").click()
    page.locator("#add-content").fill("My unsaved draft")
    page.locator("#add-title").fill("Draft title")
    page.locator("#add-btn").click()
    message = "could not be refreshed" if refresh_fails else "please retry"
    expect(page.locator(".toast").filter(has_text=message)).to_be_visible()
    expect(page.locator("#add-content")).to_have_value("My unsaved draft")
    expect(page.locator("#add-title")).to_have_value("Draft title")
    expect(page.locator("#add-btn")).to_be_enabled()
    assert len(writes) == 1
    assert len(session_calls) == 2
    if not refresh_fails:
        page.locator("#add-btn").click()
        expect(page.locator("#add-content")).to_have_value("")
        assert len(writes) == 2
        assert writes[1].headers["x-memtomem-csrf"] == "B"
        assert writes[1].post_data_json["content"] == "My unsaved draft"


def test_native_headers_preserve_values_and_replace_caller_token(page, mm_web_url):
    install_default_stubs(page)
    requests = []
    page.route(
        "**/api/session",
        lambda route: route.fulfill(json={"csrf": "fresh"}),
    )

    def capture(route):
        requests.append(route.request.headers)
        route.fulfill(json={"ok": True})

    page.route("**/api/header-probe", capture)
    goto_after_i18n_init(page, mm_web_url, lang="en")
    page.evaluate(
        """async () => {
            const headers = new Headers({'X-Probe': 'preserved', 'X-Memtomem-CSRF': 'old'});
            await csrfFetch('/api/header-probe', {method: 'POST', headers});
        }"""
    )
    assert len(requests) == 1
    assert requests[0]["x-probe"] == "preserved"
    assert requests[0]["x-memtomem-csrf"] == "fresh"


@pytest.mark.parametrize("action", ["shared_sync", "hooks_replace"])
def test_direct_click_handlers_surface_recovery(page, mm_web_url, action):
    install_default_stubs(page)
    errors = []
    writes = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/api/session", lambda route: route.fulfill(json={"csrf": "fresh"}))
    page.route(
        "**/api/settings-sync**",
        lambda route: route.fulfill(
            json={
                "status": "conflicts",
                "hooks": {
                    "pending": [],
                    "synced": [],
                    "conflicts": [
                        {
                            "event": "Stop",
                            "existing": {"command": "old"},
                            "proposed": {"command": "new"},
                        }
                    ],
                },
            }
        ),
    )

    def refuse(route):
        writes.append(route.request.url)
        route.fulfill(status=403, json={"code": "csrf_token_mismatch"})

    endpoint = (
        "/api/context/sync-all" if action == "shared_sync" else "/api/context/settings/resolve"
    )
    page.route(f"**{endpoint}**", refuse)
    goto_after_i18n_init(page, mm_web_url, lang="en")
    page.evaluate(
        """async (action) => {
            showConfirm = async () => true;
            if (action === 'shared_sync') {
                _ctxTargetScope = 'project_shared';
                _ctxSyncAllPreview = async () => ({});
                _ctxSyncAllConfirmCopy = () => ({message: 'Sync', warningText: ''});
                loadCtxOverview = () => {};
                const btn = document.getElementById('ctx-sync-all-btn');
                btn.disabled = false;
                btn.dataset.runtimeOnly = 'false';
                btn.click();
            } else {
                await loadHooksSync();
                document.querySelector('.hooks-resolve-btn').click();
            }
        }""",
        action,
    )
    expect(page.locator(".toast").filter(has_text="please retry")).to_be_visible()
    selector = "#ctx-sync-all-btn" if action == "shared_sync" else ".hooks-resolve-btn"
    expect(page.locator(selector)).to_be_enabled()
    if action == "shared_sync":
        expect(page.locator(".ctx-sync-phase--failed")).to_have_count(5)
        expect(page.locator(".ctx-sync-phase--syncing")).to_have_count(0)
    else:
        expect(page.locator(".hooks-sync-conflict")).to_have_count(1)
    assert len(writes) == 1
    assert errors == []


def test_single_promote_preserves_partial_success_after_delete_refusal(page, mm_web_url):
    install_default_stubs(page)
    writes = []
    page.route("**/api/session", lambda route: route.fulfill(json={"csrf": "fresh"}))

    def mutate(route):
        writes.append(route.request.url)
        refused = "/delete" in route.request.url
        route.fulfill(
            status=403 if refused else 200,
            json={"code": "csrf_token_mismatch"} if refused else {"status": "ok"},
        )

    page.route("**/api/context/settings/rules/**", mutate)
    goto_after_i18n_init(page, mm_web_url, lang="en")
    result = page.evaluate(
        """async () => {
            _hooksRuleRegistry = [{event:'Stop', _bucket:'target-only'}];
            _confirmHooksPromote = async () => ({ok:true, deleteOriginal:true});
            _deleteOriginalAfterPromote = entry => _hooksPostRuleAction('delete', entry, false);
            let refreshed = 0;
            loadHooksSync = async () => { refreshed++; };
            const btn = document.createElement('button');
            await _handleHooksRuleAction('promote', '0', btn);
            return {refreshed, disabled:btn.disabled};
        }"""
    )
    expect(page.locator(".toast").filter(has_text="Saved to memtomem")).to_be_visible()
    expect(page.locator(".toast").filter(has_text="please retry")).to_be_visible()
    assert result == {"refreshed": 1, "disabled": False}
    assert len(writes) == 2
