"""Search results can refresh without discarding a detail editor's draft (#3079)."""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, Route, expect

from .conftest import goto_after_i18n_init, install_default_stubs

pytestmark = pytest.mark.browser

ORIGINAL_ID = "a1b2c3d4-0000-4000-8000-00000000000a"
REPLACEMENT_ID = "a1b2c3d4-0000-4000-8000-00000000000b"
ORIGINAL_FILE = "/notes/original.md"
SOURCE_TEXT = "Original source text"
DRAFT_TEXT = "Unsaved changes to the note"
SEARCH_ROUTE = "**/api/search?**"


def _result(chunk_id: str, source_file: str) -> dict[str, object]:
    return {
        "chunk": {
            "id": chunk_id,
            "source_file": source_file,
            "content": "Indexed excerpt",
            "chunk_type": "markdown_section",
            "start_line": 1,
            "end_line": 2,
            "heading_hierarchy": [],
            "tags": [],
            "namespace": "default",
        },
        "score": 0.5,
        "rank": 1,
        "source": "bm25",
    }


def _open_search_result(page: Page, url: str) -> None:
    install_default_stubs(page)
    page.route(
        SEARCH_ROUTE,
        lambda route: route.fulfill(json={"results": [_result(ORIGINAL_ID, ORIGINAL_FILE)]}),
    )
    page.route(
        f"**/api/chunks/{ORIGINAL_ID}/source",
        lambda route: route.fulfill(json={"text": SOURCE_TEXT, "base_hash": "a" * 64}),
    )
    goto_after_i18n_init(page, url)
    page.locator("#search-input").fill("original")
    expect(page.locator(f'.result-item.selected[data-id="{ORIGINAL_ID}"]')).to_be_visible()


def _write_draft(page: Page) -> None:
    page.locator("#d-view-btn").click()
    editor = page.locator("#d-editor")
    expect(editor).to_have_value(SOURCE_TEXT)
    editor.fill(DRAFT_TEXT)


def test_adjusting_relevance_keeps_the_detail_draft(page: Page, mm_web_url: str) -> None:
    _open_search_result(page, mm_web_url)
    _write_draft(page)

    page.locator("#filter-toggle").click()
    page.locator("#adv-toggle").click()
    threshold = page.locator("#score-threshold")
    threshold.press("ArrowRight")

    expect(threshold).to_have_value("0.05")
    expect(page.locator(f'.result-item.selected[data-id="{ORIGINAL_ID}"]')).to_be_visible()
    expect(page.locator("#d-editor")).to_have_value(DRAFT_TEXT)
    expect(page.locator("#d-save-btn")).to_be_enabled()


@pytest.mark.parametrize("outcome", ["replacement", "empty", "error"])
def test_late_search_keeps_the_detail_draft(page: Page, mm_web_url: str, outcome: str) -> None:
    _open_search_result(page, mm_web_url)
    pending: list[Route] = []
    followup = re.compile(r"/api/search\?(?:[^&]+&)*q=followup(?:&|$)")
    page.route(followup, lambda route: pending.append(route))
    with page.expect_request(followup):
        page.locator("#search-input").fill("followup")

    _write_draft(page)
    # The View toggle previews the draft and leaves the edit session open.
    page.locator("#d-view-btn").click()
    preview = page.locator("#d-code-view")
    expect(preview).to_have_text(DRAFT_TEXT)

    assert len(pending) == 1
    if outcome == "replacement":
        pending[0].fulfill(json={"results": [_result(REPLACEMENT_ID, "/notes/replacement.md")]})
        expect(page.locator(f'.result-item[data-id="{REPLACEMENT_ID}"]')).to_be_visible()
    elif outcome == "empty":
        pending[0].fulfill(json={"results": []})
        expect(page.locator("#results-empty")).to_be_visible()
    else:
        pending[0].fulfill(status=500, json={"detail": "Search unavailable"})
        expect(page.locator("#results-list")).to_contain_text("Search failed")

    expect(page.locator("#d-file")).to_have_text(ORIGINAL_FILE)
    expect(preview).to_be_visible()
    expect(preview).to_have_text(DRAFT_TEXT)
    page.locator("#d-view-btn").click()
    expect(page.locator("#d-editor")).to_have_value(DRAFT_TEXT)
    expect(page.locator("#d-save-btn")).to_be_enabled()
