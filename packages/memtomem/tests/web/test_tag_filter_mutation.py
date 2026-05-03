"""Regression tests for the three ``tag-filter`` mutation sites in ``app.js``.

Issue #751 motivates this file. The cluster:

* ``app.js:_attachResultTagRow`` (~line 1888) — result-item tag label click
  sets ``tag-filter`` and runs ``doSearch()``.
* ``app.js:_attachResultTagRow`` (~line 1903) — the ✕ on a result-item tag
  conditionally clears ``tag-filter`` (only when it equals the removed tag).
* ``app.js:_searchByTag`` (~line 4064) — Tags Cloud / List pill click. PR
  #749 (issue #672) fixed a bug where this also wrote ``search-input``,
  silently double-applying the tag as a BM25 query and a tag filter. The
  bug was caught by code review only — these specs are the missing
  automated guard.

Each spec stubs every ``/api/**`` call with ``page.route()`` so the harness
verifies pure click → DOM-state wiring inside ``app.js``. Real backend
search is exercised by Python-level pytest elsewhere; duplicating it here
just buys flake.

Specs 2 and 3 inject the result-item DOM by calling the actual
``_attachResultTagRow`` global from ``page.evaluate`` rather than going
through ``doSearch()`` → ``renderResults()`` → ``showDetail()``. The
showDetail path reads ~15 chunk fields (``heading_hierarchy``,
``start_line``, ``namespace``, …) and any missing one throws. We don't
care about detail-view rendering — only the chip-click handler — so the
direct call gives us the exact handler under test with no incidental
brittleness.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.browser


def _install_default_stubs(page) -> None:
    """Stub every endpoint the SPA hits during boot so the page renders
    cleanly without any real components wired up.

    Boot fetches not stubbed individually get a generic empty-shape
    response. The pattern is intentionally permissive — specs override
    only the endpoints they assert on.
    """

    def _ok(route, payload):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/system/ui-mode", lambda r: _ok(r, {"mode": "prod"}))
    page.route("**/api/system/model-readiness", lambda r: _ok(r, {"ready": True}))
    page.route("**/api/sources", lambda r: _ok(r, {"sources": []}))
    page.route("**/api/namespaces", lambda r: _ok(r, {"namespaces": []}))
    page.route("**/api/stats", lambda r: _ok(r, {}))
    # Default empty search response — specs that assert on the request
    # shape override this with a capturing handler.
    page.route(
        "**/api/search?**",
        lambda r: _ok(r, {"results": [], "total": 0, "retrieval_stats": {}}),
    )
    # Catch-all for everything else (boot fetches like /api/tags-info,
    # /api/decay/policy, etc. that may exist in newer builds). The SPA's
    # boot paths all use ``catch`` blocks around ``api()``, so an empty
    # object is benign.
    page.route("**/api/**", lambda r: _ok(r, {}))


def test_searchByTag_does_not_pollute_search_input(page, mm_web_url: str) -> None:
    """#672 regression: clicking a Tags Cloud pill must set only
    ``tag-filter`` and never touch ``search-input``.

    The double-write was the original bug — `_searchByTag` set
    ``search-input.value = tag`` *and* ``tag-filter.value = tag``, so the
    same string flowed into both axes of the search and BM25-ranked
    documents that merely *mentioned* the tag in prose on top of the
    tag-filter constraint. PR #749 deleted the search-input write; this
    spec pins it.

    Defence in depth: we also assert the resulting ``/api/search``
    request URL contains ``tag_filter=foo`` and *no* ``q=`` param, so a
    future helper extraction that re-routed the click through a code
    path that injects ``q`` into the URL would still trip this test.
    """
    _install_default_stubs(page)

    captured: dict[str, str] = {}

    def _capture_search(route):
        captured["url"] = route.request.url
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"results": [], "total": 0, "retrieval_stats": {}}),
        )

    page.route(
        "**/api/tags",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"tags": [{"tag": "foo", "count": 3}]}),
        ),
    )
    # Override the default-stub search route. ``page.route`` resolves
    # last-registered-wins, so this takes precedence over the empty
    # default registered by ``_install_default_stubs``.
    page.route("**/api/search?**", _capture_search)

    page.goto(mm_web_url)
    page.locator('[data-tab="tags"]').click()
    # ``STATE.tagsView`` defaults to 'cloud' (see app.js STATE init); the
    # cloud item is the only renderer that exposes a ``data-tag``
    # selector. The ``foo`` pill is positioned by deterministic hash
    # rotation, so explicit visibility/stable-position waiting via the
    # data-tag selector is the right hook.
    page.locator('.tag-cloud-item[data-tag="foo"]').click()

    page.wait_for_function(
        "() => document.getElementById('tag-filter').value === 'foo'",
        timeout=2_000,
    )

    assert page.locator("#search-input").input_value() == ""
    assert page.locator("#tag-filter").input_value() == "foo"
    assert "tag_filter=foo" in captured.get("url", "")
    assert "q=" not in captured.get("url", "")


def test_result_tag_label_click_sets_tag_filter_only(page, mm_web_url: str) -> None:
    """``_attachResultTagRow`` (~app.js:1888): clicking a result-item tag
    label sets ``tag-filter`` to the clicked tag but leaves whatever the
    user typed into ``search-input`` intact.

    This is the dual-axis-when-the-user-typed-it path. #672 did not
    break it — only ``_searchByTag`` was buggy — but a future "make all
    three mutation sites symmetric" refactor could quietly clear
    ``search-input`` here too. Pinning the contract guards that.
    """
    _install_default_stubs(page)

    page.goto(mm_web_url)
    # Pre-populate ``search-input`` to simulate the user mid-search.
    page.locator("#search-input").fill("hello")

    # Build the chip via the actual production helper. Going through
    # ``doSearch`` → ``renderResults`` → ``showDetail`` would force us
    # to mock ~15 fields on ``chunk`` to satisfy the detail panel; the
    # click handler we're testing is bound inside ``_attachResultTagRow``
    # itself, so we exercise it directly.
    page.evaluate(
        """() => {
            const list = document.getElementById('results-list');
            list.innerHTML = '';
            list.hidden = false;
            list.style.display = 'block';
            const empty = document.getElementById('results-empty');
            if (empty) empty.hidden = true;
            const item = document.createElement('div');
            item.className = 'result-item';
            const body = document.createElement('div');
            body.className = 'result-body';
            item.appendChild(body);
            list.appendChild(item);
            window._attachResultTagRow(1, ['bar'], body);
        }"""
    )

    page.locator(".result-tag-label", has_text="bar").click()

    # ``tag-filter`` is set synchronously inside the handler, so the
    # value is observable immediately — no need to wait for the
    # ``doSearch`` fetch to complete.
    assert page.locator("#tag-filter").input_value() == "bar"
    assert page.locator("#search-input").input_value() == "hello"


def test_result_tag_remove_clears_only_matching_tag_filter(page, mm_web_url: str) -> None:
    """``_attachResultTagRow`` (~app.js:1903): clicking the ✕ on a result
    tag must clear ``tag-filter`` *only when* it currently equals the
    removed tag — otherwise the unrelated filter stays.

    A naïve "always clear on remove" refactor would silently break the
    case where the user has filtered by tag *X* and removes tag *Y*
    from a result; the filter on *X* should survive.
    """
    _install_default_stubs(page)
    page.route(
        "**/api/chunks/7/tags",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True}),
        ),
    )

    page.goto(mm_web_url)
    page.evaluate(
        """() => {
            const list = document.getElementById('results-list');
            list.innerHTML = '';
            list.hidden = false;
            list.style.display = 'block';
            const empty = document.getElementById('results-empty');
            if (empty) empty.hidden = true;
            const item = document.createElement('div');
            item.className = 'result-item';
            const body = document.createElement('div');
            body.className = 'result-body';
            item.appendChild(body);
            list.appendChild(item);
            // STATE.lastResults is read inside the remove handler;
            // seed it so the cache lookup doesn't trip.
            window.STATE = window.STATE || {};
            window.STATE.lastResults = [
                {chunk: {id: 7, tags: ['baz']}, score: 0.5},
            ];
            window._attachResultTagRow(7, ['baz'], body);
        }"""
    )

    # Case A: ``tag-filter`` matches the tag being removed → clear.
    # The filters panel is hidden by default, so fill() would fail the
    # visibility check; the handler reads ``.value`` directly, which a
    # plain assignment satisfies.
    page.evaluate("document.getElementById('tag-filter').value = 'baz'")
    page.locator(".result-tag-remove").first.click()
    page.wait_for_function(
        "() => document.getElementById('tag-filter').value === ''",
        timeout=2_000,
    )
    assert page.locator("#tag-filter").input_value() == ""

    # Case B: re-render and remove a different tag → ``tag-filter``
    # holding an unrelated value must survive.
    page.evaluate(
        """() => {
            const list = document.getElementById('results-list');
            list.innerHTML = '';
            list.hidden = false;
            list.style.display = 'block';
            const empty = document.getElementById('results-empty');
            if (empty) empty.hidden = true;
            const item = document.createElement('div');
            item.className = 'result-item';
            const body = document.createElement('div');
            body.className = 'result-body';
            item.appendChild(body);
            list.appendChild(item);
            window.STATE.lastResults = [
                {chunk: {id: 8, tags: ['removeme']}, score: 0.5},
            ];
            window._attachResultTagRow(8, ['removeme'], body);
        }"""
    )
    page.route(
        "**/api/chunks/8/tags",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"ok": True}),
        ),
    )
    page.evaluate("document.getElementById('tag-filter').value = 'unrelated'")
    page.locator(".result-tag-remove").first.click()
    # The handler doesn't touch ``tag-filter`` in this branch; assert it
    # stays after a brief settle window so any async rollback path would
    # have a chance to fire.
    page.wait_for_timeout(200)
    assert page.locator("#tag-filter").input_value() == "unrelated"
