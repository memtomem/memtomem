"""Browser tests for the read-only wiki browser section (ADR-0008 PR-E).

Pins the two integration points only a real browser exercises:

* the ``ctx-wiki`` nav section dispatches to ``loadWiki`` and renders the
  global asset list, and — being a GLOBAL surface — shows NO project/tier
  control bar (``#ctx-control-bar`` stays hidden, like ``ctx-projects``);
* clicking an asset lazily loads its per-vendor diff + lint into the detail
  pane.

Harness mirrors ``test_context_gateway_lists.py``: lifespan off, every
``/api/**`` call intercepted via ``page.route`` (catch-all first, specific
overrides last — last-route-wins).
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


_WIKI_LIST = {
    "wiki_head": "a" * 40,
    "wiki_root": "/home/u/.memtomem-wiki",
    "is_dirty": False,
    "items": [
        {
            "type": "skills",
            "name": "alpha",
            "vendors": [
                {"vendor": "claude", "renderable": True},
                {"vendor": "gemini", "renderable": True},
                {"vendor": "codex", "renderable": True},
                {"vendor": "kimi", "renderable": True},
            ],
        },
        {
            "type": "commands",
            "name": "gamma",
            "vendors": [
                {"vendor": "claude", "renderable": True},
                {"vendor": "gemini", "renderable": True},
                {"vendor": "codex", "renderable": False},
            ],
        },
    ],
}

_DIFF = {
    "override_path": "/w/skills/alpha/overrides/claude.md",
    "exists": True,
    "in_sync": False,
    "diff_lines": [
        "--- skills/alpha: canonical\n",
        "+++ skills/alpha/overrides/claude.md\n",
        "@@ -1 +1 @@\n",
        "-# Alpha\n",
        "+# Alpha MODIFIED\n",
    ],
    "dropped": [],
}

_DIFF_NONE = {
    "override_path": "/w/skills/alpha/overrides/claude.md",
    "exists": False,
    "in_sync": False,
    "diff_lines": [],
    "dropped": [],
}

_LINT = {"asset_type": "skills", "name": "alpha", "ok": True, "findings": []}


def _json(payload):
    return lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(payload)
    )


def _stub_wiki(page) -> None:
    page.route("**/api/wiki", _json(_WIKI_LIST))
    page.route("**/api/wiki/**/diff**", _json(_DIFF))
    page.route("**/api/wiki/**/lint**", _json(_LINT))


def _open_wiki(page) -> None:
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-wiki')")
    page.wait_for_function(
        "() => document.querySelectorAll('#wiki-list .wiki-item').length > 0",
        timeout=5_000,
    )


def test_wiki_section_renders_list_without_control_bar(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_wiki(page)
    page.goto(mm_web_url)
    _open_wiki(page)

    names = page.eval_on_selector_all(
        "#wiki-list .wiki-item", "els => els.map(e => e.dataset.name)"
    )
    assert set(names) == {"alpha", "gamma"}

    # The wiki is a GLOBAL surface: the per-project/tier control bar must stay
    # hidden (ctx-wiki is deliberately absent from _CTX_SECTION_BAR_TYPE).
    assert page.locator("#ctx-control-bar").is_hidden()


def test_wiki_detail_loads_diff_and_lint_on_click(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_wiki(page)
    page.goto(mm_web_url)
    _open_wiki(page)

    page.locator("#wiki-list .wiki-item[data-name='alpha']").click()
    # Detail lazily fetches diff + lint for the default vendor; the out-of-sync
    # diff renders into <pre class="wiki-diff">.
    page.wait_for_function(
        "() => {"
        "  const pre = document.querySelector('#wiki-vendor-view .wiki-diff');"
        "  return pre && pre.textContent.includes('Alpha MODIFIED');"
        "}",
        timeout=5_000,
    )
    # Lint section renders its 'well-formed' badge for an ok report.
    assert page.locator("#wiki-vendor-view .wiki-section").count() >= 2


def test_wiki_override_seed_in_dev_mode(page, mm_web_url: str) -> None:
    """Dev tier (E-2): the seed button appears and POSTs ``force=false`` for a
    fresh override, and the HEAD badge repaints from the response's ``wiki_dirty``.
    Gated on ``body.dev-mode`` → stub ``/api/system/ui-mode`` to ``dev``."""
    install_default_stubs(page)
    page.route("**/api/wiki", _json(_WIKI_LIST))
    page.route("**/api/wiki/**/diff**", _json(_DIFF_NONE))  # no override yet
    page.route("**/api/wiki/**/lint**", _json(_LINT))
    page.route("**/api/system/ui-mode", _json({"mode": "dev"}))

    posted: list[dict] = []

    def _seed(route):
        req = route.request
        if req.method == "POST":
            posted.append(json.loads(req.post_data or "{}"))
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "seeded": True,
                        "vendor": "claude",
                        "forced": False,
                        "dropped": [],
                        "wiki_dirty": True,
                    }
                ),
            )
        else:
            route.fallback()

    page.route("**/api/wiki/**/override", _seed)

    page.goto(mm_web_url)
    _open_wiki(page)
    page.locator("#wiki-list .wiki-item[data-name='alpha']").click()

    # Detail loads; the seed button renders for the default (claude) vendor and,
    # with no override on disk, is the fresh-seed variant (data-exists="0").
    page.wait_for_selector("#wiki-seed-btn", timeout=5_000)
    assert page.locator("#wiki-seed-btn").get_attribute("data-exists") == "0"

    page.locator("#wiki-seed-btn").click()
    # Success repaints the HEAD dirty badge (wiki_dirty=True from the response).
    page.wait_for_function(
        "() => document.querySelector('#wiki-head .badge-warning') !== null",
        timeout=5_000,
    )
    assert posted == [{"vendor": "claude", "force": False}]


def test_wiki_install_action_in_dev_mode(page, mm_web_url: str) -> None:
    """Dev tier (E-3): the install/update action renders with a project picker,
    Install POSTs to the project-scoped route, and — the wiki being host-global —
    the shared project/tier control bar STAYS hidden even with the picker present."""
    install_default_stubs(page)
    page.route("**/api/wiki", _json(_WIKI_LIST))
    page.route("**/api/wiki/**/diff**", _json(_DIFF))
    page.route("**/api/wiki/**/lint**", _json(_LINT))
    page.route("**/api/system/ui-mode", _json({"mode": "dev"}))

    posted: list[str] = []

    def _install(route):
        req = route.request
        if req.method == "POST":
            posted.append(req.url)
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"installed": True, "asset_type": "skills", "name": "alpha"}),
            )
        else:
            route.fallback()

    page.route("**/api/context/skills/alpha/install**", _install)

    page.goto(mm_web_url)
    _open_wiki(page)
    page.locator("#wiki-list .wiki-item[data-name='alpha']").click()
    page.wait_for_selector("#wiki-install-btn", timeout=5_000)

    # Host-global invariant: the shared per-project/tier control bar stays hidden
    # even though the detail pane now carries its OWN local project <select>.
    assert page.locator("#ctx-control-bar").is_hidden()
    assert page.locator("#wiki-install-project").count() == 1

    page.locator("#wiki-install-btn").click()
    page.wait_for_selector("#toast-container .toast-success", timeout=5_000)
    assert len(posted) == 1
    assert "/api/context/skills/alpha/install" in posted[0]


def test_wiki_force_update_confirms(page, mm_web_url: str) -> None:
    """Dev tier (E-3): updating a dirty install is refused (409 ``stale_install``),
    the UI confirms the overwrite, and the confirmed retry POSTs ``force=true``."""
    install_default_stubs(page)
    page.route("**/api/wiki", _json(_WIKI_LIST))
    page.route("**/api/wiki/**/diff**", _json(_DIFF))
    page.route("**/api/wiki/**/lint**", _json(_LINT))
    page.route("**/api/system/ui-mode", _json({"mode": "dev"}))

    calls: list[dict] = []

    def _update(route):
        req = route.request
        if req.method != "POST":
            route.fallback()
            return
        body = json.loads(req.post_data or "{}")
        calls.append(body)
        if not body.get("force"):
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps({"detail": {"reason_code": "stale_install"}}),
            )
        else:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"updated": True, "was_no_op": False}),
            )

    page.route("**/api/context/skills/alpha/update**", _update)

    page.goto(mm_web_url)
    page.evaluate("() => { window.showConfirm = async () => true; }")  # accept overwrite
    _open_wiki(page)
    page.locator("#wiki-list .wiki-item[data-name='alpha']").click()
    page.wait_for_selector("#wiki-update-btn", timeout=5_000)

    page.locator("#wiki-update-btn").click()
    # The confirmed force retry succeeds → success toast; two POSTs recorded:
    # the refused force=false, then the confirmed force=true.
    page.wait_for_selector("#toast-container .toast-success", timeout=5_000)
    assert [c.get("force") for c in calls] == [False, True]


_OVERRIDE_EXISTS = {"vendor": "claude", "content": "# orig\n", "mtime_ns": "111", "exists": True}


def test_wiki_override_edit_in_dev_mode(page, mm_web_url: str) -> None:
    """Dev tier (ADR-0027 Editor-A): the override read pane + Edit toggle render,
    Save PUTs ``{vendor, content, mtime_ns}``, and the HEAD badge repaints dirty."""
    install_default_stubs(page)
    page.route("**/api/wiki", _json(_WIKI_LIST))
    page.route("**/api/wiki/**/diff**", _json(_DIFF))
    page.route("**/api/wiki/**/lint**", _json(_LINT))
    page.route("**/api/system/ui-mode", _json({"mode": "dev"}))

    puts: list[dict] = []

    def _override(route):
        req = route.request
        if req.method == "GET":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_OVERRIDE_EXISTS),
            )
        elif req.method == "PUT":
            puts.append(json.loads(req.post_data or "{}"))
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "vendor": "claude",
                        "mtime_ns": "222",
                        "wiki_dirty": True,
                        "privacy_warning": 0,
                    }
                ),
            )
        else:
            route.fallback()

    page.route("**/api/wiki/**/override**", _override)

    page.goto(mm_web_url)
    _open_wiki(page)
    page.locator("#wiki-list .wiki-item[data-name='alpha']").click()

    # Read pane + Edit toggle render for the dev-tier editor; Edit reveals the
    # textarea seeded from the GET (content + mtime token).
    page.wait_for_selector("#wiki-override-edit-btn", timeout=5_000)
    page.locator("#wiki-override-edit-btn").click()
    ta = page.locator("#wiki-override-content")
    ta.wait_for(timeout=5_000)
    assert ta.input_value() == "# orig\n"

    ta.fill("# edited in browser\n")
    page.locator("#wiki-override-save-btn").click()
    # wiki_dirty=True from the response → HEAD badge repaints without re-listing.
    page.wait_for_function(
        "() => document.querySelector('#wiki-head .badge-warning') !== null",
        timeout=5_000,
    )
    assert puts == [
        {"vendor": "claude", "content": "# edited in browser\n", "mtime_ns": "111", "force": False}
    ]


def test_wiki_override_edit_conflict_banner(page, mm_web_url: str) -> None:
    """A 409 ``stale_mtime`` on Save surfaces the conflict banner with reload/force
    affordances (the in-lock concurrency guard's client side)."""
    install_default_stubs(page)
    page.route("**/api/wiki", _json(_WIKI_LIST))
    page.route("**/api/wiki/**/diff**", _json(_DIFF))
    page.route("**/api/wiki/**/lint**", _json(_LINT))
    page.route("**/api/system/ui-mode", _json({"mode": "dev"}))

    def _override(route):
        req = route.request
        if req.method == "GET":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(_OVERRIDE_EXISTS),
            )
        elif req.method == "PUT":
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps(
                    {"reason_code": "stale_mtime", "mtime_ns": "999", "error_kind": "conflict"}
                ),
            )
        else:
            route.fallback()

    page.route("**/api/wiki/**/override**", _override)

    page.goto(mm_web_url)
    _open_wiki(page)
    page.locator("#wiki-list .wiki-item[data-name='alpha']").click()
    page.wait_for_selector("#wiki-override-edit-btn", timeout=5_000)
    page.locator("#wiki-override-edit-btn").click()
    page.locator("#wiki-override-content").fill("# mine\n")
    page.locator("#wiki-override-save-btn").click()

    # 409 → the conflict banner unhides with both reload and force actions.
    page.wait_for_selector("#wiki-conflict-force-btn", timeout=5_000)
    assert page.locator("#wiki-conflict-banner").is_visible()
    assert page.locator("#wiki-conflict-reload-btn").count() == 1


_CANONICAL = {"content": "# canon\n", "mtime_ns": "111"}
_OVERRIDE_NONE = {"vendor": "claude", "content": "", "mtime_ns": "0", "exists": False}


def test_wiki_canonical_edit_in_dev_mode(page, mm_web_url: str) -> None:
    """Dev tier (ADR-0027 Editor-B): the artifact-level canonical read pane + Edit
    toggle render, Save PUTs ``{content, mtime_ns}`` to ``…/canonical``, and the
    HEAD badge repaints dirty. The override is not-seeded so only the canonical
    editor is in play."""
    install_default_stubs(page)
    page.route("**/api/wiki", _json(_WIKI_LIST))
    page.route("**/api/wiki/**/diff**", _json(_DIFF))
    page.route("**/api/wiki/**/lint**", _json(_LINT))
    page.route("**/api/wiki/**/override**", _json(_OVERRIDE_NONE))
    page.route("**/api/system/ui-mode", _json({"mode": "dev"}))

    puts: list[dict] = []

    def _canonical(route):
        req = route.request
        if req.method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_CANONICAL))
        elif req.method == "PUT":
            puts.append(json.loads(req.post_data or "{}"))
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"mtime_ns": "222", "wiki_dirty": True, "privacy_warning": 0}),
            )
        else:
            route.fallback()

    page.route("**/api/wiki/**/canonical**", _canonical)

    page.goto(mm_web_url)
    _open_wiki(page)
    page.locator("#wiki-list .wiki-item[data-name='alpha']").click()

    # The canonical editor mounts in the detail head (artifact-level); Edit reveals
    # the textarea seeded from the GET (content + mtime token).
    page.wait_for_selector("#wiki-canonical-edit-btn", timeout=5_000)
    page.locator("#wiki-canonical-edit-btn").click()
    ta = page.locator("#wiki-canonical-content")
    ta.wait_for(timeout=5_000)
    assert ta.input_value() == "# canon\n"

    ta.fill("# edited canon\n")
    page.locator("#wiki-canonical-save-btn").click()
    page.wait_for_function(
        "() => document.querySelector('#wiki-head .badge-warning') !== null",
        timeout=5_000,
    )
    assert puts == [{"content": "# edited canon\n", "mtime_ns": "111", "force": False}]


def test_wiki_canonical_edit_conflict_banner(page, mm_web_url: str) -> None:
    """A 409 ``stale_mtime`` on a canonical Save surfaces the canonical conflict
    banner with reload/force affordances."""
    install_default_stubs(page)
    page.route("**/api/wiki", _json(_WIKI_LIST))
    page.route("**/api/wiki/**/diff**", _json(_DIFF))
    page.route("**/api/wiki/**/lint**", _json(_LINT))
    page.route("**/api/wiki/**/override**", _json(_OVERRIDE_NONE))
    page.route("**/api/system/ui-mode", _json({"mode": "dev"}))

    def _canonical(route):
        req = route.request
        if req.method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_CANONICAL))
        elif req.method == "PUT":
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps(
                    {"reason_code": "stale_mtime", "mtime_ns": "999", "error_kind": "conflict"}
                ),
            )
        else:
            route.fallback()

    page.route("**/api/wiki/**/canonical**", _canonical)

    page.goto(mm_web_url)
    _open_wiki(page)
    page.locator("#wiki-list .wiki-item[data-name='alpha']").click()
    page.wait_for_selector("#wiki-canonical-edit-btn", timeout=5_000)
    page.locator("#wiki-canonical-edit-btn").click()
    page.locator("#wiki-canonical-content").fill("# mine\n")
    page.locator("#wiki-canonical-save-btn").click()

    # 409 → the canonical conflict banner unhides with both reload and force.
    page.wait_for_selector("#wiki-canonical-conflict-force-btn", timeout=5_000)
    assert page.locator("#wiki-canonical-conflict-banner").is_visible()
    assert page.locator("#wiki-canonical-conflict-reload-btn").count() == 1


def _stub_canonical_save(page) -> None:
    """List/diff/lint/override + a canonical GET/PUT (Save) so a pending commit
    target and the HEAD-row Commit button appear."""
    install_default_stubs(page)
    page.route("**/api/wiki", _json(_WIKI_LIST))
    page.route("**/api/wiki/**/diff**", _json(_DIFF))
    page.route("**/api/wiki/**/lint**", _json(_LINT))
    page.route("**/api/wiki/**/override**", _json(_OVERRIDE_NONE))
    page.route("**/api/system/ui-mode", _json({"mode": "dev"}))

    def _canonical(route):
        req = route.request
        if req.method == "GET":
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_CANONICAL))
        elif req.method == "PUT":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"mtime_ns": "222", "wiki_dirty": True, "privacy_warning": 0}),
            )
        else:
            route.fallback()

    page.route("**/api/wiki/**/canonical**", _canonical)


def test_wiki_commit_happy_path(page, mm_web_url: str) -> None:
    """Dev tier (ADR-0027 §3): after a Save, the Commit button opens a message
    modal; committing POSTs the server-resolved target + expected_head, and on
    success the HEAD advances, the dirty badge clears, and the button disappears."""
    _stub_canonical_save(page)

    posted: list[dict] = []

    def _commit(route):
        req = route.request
        if req.method != "POST":
            route.fallback()
            return
        posted.append(json.loads(req.post_data or "{}"))
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "committed": True,
                    "wiki_head": "b" * 40,
                    "wiki_dirty": False,
                    "privacy_warning": 0,
                }
            ),
        )

    page.route("**/api/wiki/**/commit**", _commit)

    page.goto(mm_web_url)
    _open_wiki(page)
    page.locator("#wiki-list .wiki-item[data-name='alpha']").click()
    page.wait_for_selector("#wiki-canonical-edit-btn", timeout=5_000)
    page.locator("#wiki-canonical-edit-btn").click()
    page.locator("#wiki-canonical-content").fill("# edited canon\n")
    page.locator("#wiki-canonical-save-btn").click()
    page.wait_for_selector("#wiki-commit-btn", timeout=5_000)

    page.locator("#wiki-commit-btn").click()
    page.wait_for_selector("#wiki-commit-modal:not([hidden])", timeout=5_000)
    page.locator("#wiki-commit-ok-btn").click()

    # Success → HEAD advances to the new SHA, dirty badge clears, button gone.
    page.wait_for_function(
        "() => document.querySelector('#wiki-head').textContent.includes('bbbbbbbbbbbb')",
        timeout=5_000,
    )
    assert page.locator("#wiki-head .badge-warning").count() == 0
    assert page.locator("#wiki-commit-btn").count() == 0
    assert len(posted) == 1
    assert posted[0]["expected_head"] == "a" * 40
    assert posted[0]["force"] is False
    assert posted[0]["targets"] == [{"kind": "canonical", "mtime_ns": "222"}]


def test_wiki_commit_stale_head_refreshes(page, mm_web_url: str) -> None:
    """A 409 ``stale_head`` (HEAD moved underneath) refreshes the displayed HEAD
    and CLEARS the pending target — the captured tokens predate the new HEAD, so a
    fresh Save is required before another commit (Codex M1: no stale-token retry)."""
    _stub_canonical_save(page)

    def _commit(route):
        req = route.request
        if req.method != "POST":
            route.fallback()
            return
        route.fulfill(
            status=409,
            content_type="application/json",
            body=json.dumps(
                {"reason_code": "stale_head", "wiki_head": "c" * 40, "error_kind": "conflict"}
            ),
        )

    page.route("**/api/wiki/**/commit**", _commit)

    page.goto(mm_web_url)
    _open_wiki(page)
    page.locator("#wiki-list .wiki-item[data-name='alpha']").click()
    page.wait_for_selector("#wiki-canonical-edit-btn", timeout=5_000)
    page.locator("#wiki-canonical-edit-btn").click()
    page.locator("#wiki-canonical-content").fill("# edited canon\n")
    page.locator("#wiki-canonical-save-btn").click()
    page.wait_for_selector("#wiki-commit-btn", timeout=5_000)

    page.locator("#wiki-commit-btn").click()
    page.wait_for_selector("#wiki-commit-modal:not([hidden])", timeout=5_000)
    page.locator("#wiki-commit-ok-btn").click()

    # HEAD repaints to the moved SHA; the Commit button disappears (pending
    # cleared — a fresh Save is required before committing against the new HEAD).
    page.wait_for_function(
        "() => document.querySelector('#wiki-head').textContent.includes('cccccccccccc')",
        timeout=5_000,
    )
    assert page.locator("#wiki-commit-btn").count() == 0
