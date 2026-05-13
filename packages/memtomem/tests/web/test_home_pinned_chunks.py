"""Regression coverage for Home pinned chunk navigation (#991)."""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


PIN_ID = "123e4567-e89b-12d3-a456-426614174000"
SOURCE_PATH = "/tmp/pinned-note.md"


def _ok(route, payload, *, status: int = 200) -> None:
    route.fulfill(
        status=status,
        content_type="application/json",
        body=json.dumps(payload),
    )


def _chunk_payload() -> dict[str, object]:
    return {
        "id": PIN_ID,
        "content": "Pinned body content",
        "source_file": SOURCE_PATH,
        "chunk_type": "markdown",
        "heading_hierarchy": [],
        "start_line": 1,
        "end_line": 3,
        "created_at": "2026-05-13T00:00:00Z",
        "tags": [],
        "namespace": "default",
        "valid_from_unix": None,
        "valid_to_unix": None,
    }


def _source_payload() -> dict[str, object]:
    return {
        "path": SOURCE_PATH,
        "chunk_count": 1,
        "size_bytes": 42,
        "last_indexed_at": "2026-05-13T00:00:00Z",
        "memory_dir": None,
        "namespace": "default",
    }


def _seed_pin(page) -> None:
    pins = json.dumps({PIN_ID: {"source": SOURCE_PATH, "snippet": "Stored pinned preview"}})
    page.add_init_script(f"localStorage.setItem('m2m-pins', {json.dumps(pins)});")


def _install_home_stubs(page) -> None:
    install_default_stubs(page)
    page.route("**/api/sources", lambda r: _ok(r, {"sources": [_source_payload()]}))
    page.route(
        "**/api/chunks?**",
        lambda r: _ok(r, {"chunks": [_chunk_payload()], "total": 1}),
    )


def test_home_pinned_chunk_open_is_keyboard_navigable(page, mm_web_url: str) -> None:
    _seed_pin(page)
    _install_home_stubs(page)
    page.route(f"**/api/chunks/{PIN_ID}", lambda r: _ok(r, _chunk_payload()))

    page.goto(f"{mm_web_url}/#home")
    opener = page.locator("#home-pinned-list .home-pinned-open")
    opener.wait_for()

    # Pin "open" is a real <button> sibling of "Remove" — not a role=button
    # div wrapping a button (that would nest interactives and double-fire
    # Enter/Space on the inner Remove). Pin both invariants here.
    assert opener.evaluate("el => el.tagName") == "BUTTON"
    assert page.locator("#home-pinned-list .home-pinned-item > button").count() == 2, (
        "row should contain exactly two sibling buttons (open + remove)"
    )

    with page.expect_request(f"**/api/chunks/{PIN_ID}"):
        opener.press("Enter")

    page.wait_for_function(
        "() => document.querySelector('#tabbtn-sources')?.classList.contains('active')"
    )


def test_home_pinned_chunk_404_marks_stale_and_remove_does_not_navigate(
    page, mm_web_url: str
) -> None:
    _seed_pin(page)
    _install_home_stubs(page)
    request_count = {"chunk": 0}

    def _missing_chunk(route) -> None:
        request_count["chunk"] += 1
        _ok(route, {"detail": "Chunk not found"}, status=404)

    page.route(f"**/api/chunks/{PIN_ID}", _missing_chunk)

    page.goto(f"{mm_web_url}/#home")
    opener = page.locator("#home-pinned-list .home-pinned-open")
    opener.wait_for()
    opener.click()

    stale = page.locator("#home-pinned-list .home-pinned-stale")
    stale.wait_for()
    assert page.locator(".home-pinned-stale-badge").text_content() == "Missing chunk"

    page.locator("#home-pinned-list .unpin-btn").click()
    page.wait_for_function("() => localStorage.getItem('m2m-pins') === '{}'")
    assert request_count["chunk"] == 1


def test_remove_button_keyboard_activation_does_not_navigate(page, mm_web_url: str) -> None:
    """Pressing Enter on the focused Remove button must unpin only — not also
    fire the open-pin handler. With the prior role=button row wrapping a real
    Remove button, the row's keydown listener would catch the bubbling Enter
    and double-fire navigation. The sibling-buttons structure plus per-button
    native handling makes this impossible; pin both halves here so a future
    re-nesting regresses loudly."""
    _seed_pin(page)
    _install_home_stubs(page)
    request_count = {"chunk": 0}

    def _count_chunk(route) -> None:
        request_count["chunk"] += 1
        _ok(route, _chunk_payload())

    page.route(f"**/api/chunks/{PIN_ID}", _count_chunk)

    page.goto(f"{mm_web_url}/#home")
    remove_btn = page.locator("#home-pinned-list .unpin-btn")
    remove_btn.wait_for()
    remove_btn.focus()
    remove_btn.press("Enter")

    page.wait_for_function("() => localStorage.getItem('m2m-pins') === '{}'")
    assert request_count["chunk"] == 0, (
        "Enter on focused Remove must not also trigger the open-pin fetch"
    )
    assert not page.locator("#tabbtn-sources.active").count(), (
        "Enter on focused Remove must not switch to Sources tab"
    )
