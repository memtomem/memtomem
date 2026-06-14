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
