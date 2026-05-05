"""Pin tests for list-row accessibility attributes in the static web UI.

Search / source-detail / timeline list-rows are constructed in JS, so behavior
tests would require a browser. These source-scan tests pin the a11y semantics
so removing ``role=`` / ``tabindex`` / ``aria-label`` silently can't slip past
review (issue #700).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "web" / "static"
_APP_JS = _STATIC_DIR / "app.js"
_TIMELINE_JS = _STATIC_DIR / "timeline.js"


@pytest.fixture(scope="module")
def app_js() -> str:
    assert _APP_JS.exists(), f"app.js missing: {_APP_JS}"
    return _APP_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def timeline_js() -> str:
    assert _TIMELINE_JS.exists(), f"timeline.js missing: {_TIMELINE_JS}"
    return _TIMELINE_JS.read_text(encoding="utf-8")


def _extract_function(source: str, name: str) -> str:
    """Extract a top-level ``function name(...) { ... }`` body via brace matching."""
    m = re.search(rf"\bfunction\s+{re.escape(name)}\s*\(", source)
    assert m, f"function {name} not found"
    i = source.index("{", m.end())
    depth = 0
    for j in range(i, len(source)):
        c = source[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[i : j + 1]
    raise AssertionError(f"unterminated function body for {name}")


class TestSearchResultItemA11y:
    """Pin a11y attributes on ``.result-item`` rows.

    Already-implemented in ``main`` — this class is regression protection so
    the role/tabindex/aria-label/keydown quad can't be silently dropped.
    """

    def test_result_item_role_button(self, app_js: str) -> None:
        body = _extract_function(app_js, "_buildResultItem")
        assert "setAttribute('role', 'button')" in body, (
            "result-item must expose role=button so screen readers announce it "
            "as a selectable row, not a generic group"
        )

    def test_result_item_aria_label(self, app_js: str) -> None:
        body = _extract_function(app_js, "_buildResultItem")
        assert "setAttribute('aria-label'" in body, (
            "result-item must set aria-label from filename/lines/namespace/age "
            "so screen readers announce a meaningful name"
        )

    def test_result_item_keydown_handler(self, app_js: str) -> None:
        body = _extract_function(app_js, "_buildResultItem")
        assert "addEventListener('keydown'" in body, (
            "result-item with role=button must respond to Enter/Space — "
            "otherwise screen-reader announce promises behavior that doesn't "
            "fire from the keyboard"
        )


class TestChunkCardA11y:
    """Pin a11y attributes on ``.chunk-card`` rows in the source-detail view.

    aria-label is set unconditionally; role=button + tabindex + keydown are
    added only on the collapsible branch (cards taller than 120px). Both
    surfaces are pinned.
    """

    def test_chunk_card_always_aria_label(self, app_js: str) -> None:
        # The card creation site is in an anonymous closure inside the source
        # detail loader; we anchor on the literal construction sequence so
        # the test fails loudly if someone reshuffles the lines.
        anchor = "card.className = 'chunk-card';"
        idx = app_js.find(anchor)
        assert idx != -1, f"chunk-card construction anchor not found: {anchor!r}"
        # Look at the next 600 chars — generous slack for future small edits,
        # tight enough that aria-label has to be at the construction site.
        window = app_js[idx : idx + 600]
        assert "setAttribute(\n          'aria-label'" in window or (
            "setAttribute('aria-label'" in window
        ), (
            "chunk-card must set aria-label at construction time so every "
            "card — collapsible or not — has an accessible name"
        )

    def test_chunk_card_collapsible_role_and_tabindex(self, app_js: str) -> None:
        # Pin the collapsible branch by anchoring on the existing
        # `chunk-card-collapsible` className guard.
        anchor = "card.classList.add('chunk-card-collapsible');"
        idx = app_js.find(anchor)
        assert idx != -1, "chunk-card-collapsible branch not found"
        # Window covers the full collapsible wiring (mousedown + click + keydown).
        window = app_js[idx : idx + 1200]
        assert "setAttribute('role', 'button')" in window, (
            "collapsible chunk-card must expose role=button so screen readers "
            "announce it as a togglable control"
        )
        assert "setAttribute('tabindex', '0')" in window, (
            "collapsible chunk-card must be keyboard-focusable (tabindex=0)"
        )

    def test_chunk_card_collapsible_keydown_handler(self, app_js: str) -> None:
        anchor = "card.classList.add('chunk-card-collapsible');"
        idx = app_js.find(anchor)
        assert idx != -1
        window = app_js[idx : idx + 1200]
        assert "addEventListener('keydown'" in window, (
            "collapsible chunk-card must respond to Enter/Space so the role=button "
            "promise is real for keyboard users"
        )


class TestTimelineRowA11y:
    """Pin a11y attributes on Timeline rows — chunks view, files view outer
    row, files view inner chunk row, and the heatmap column buttons.
    """

    def test_timeline_item_chunks_view_role_button(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderChunkView")
        assert "setAttribute('role', 'button')" in body, (
            "chunks-view .timeline-item must expose role=button — the row is the "
            "expand/collapse trigger, not a generic group"
        )

    def test_timeline_item_chunks_view_aria_label(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderChunkView")
        assert "setAttribute('aria-label'" in body, (
            "chunks-view .timeline-item must set aria-label — at minimum filename "
            "and time, which are already on the row"
        )

    def test_timeline_item_chunks_view_keydown_handler(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderChunkView")
        assert "addEventListener('keydown'" in body, (
            "chunks-view .timeline-item with role=button must respond to "
            "Enter/Space — otherwise screen-reader announce is a lie"
        )

    def test_timeline_file_item_files_view_role_button(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderFileView")
        assert "setAttribute('role', 'button')" in body, (
            "files-view rows (.timeline-file-item / .tl-file-chunk-item) must expose role=button"
        )

    def test_timeline_file_item_files_view_aria_label(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderFileView")
        # Files view has two row classes (.timeline-file-item + .tl-file-chunk-item),
        # both clickable; both need aria-label. Pin via count >= 2.
        assert body.count("setAttribute(") >= 2 and "aria-label" in body, (
            "files-view must aria-label both the outer .timeline-file-item and "
            "each inner .tl-file-chunk-item"
        )

    def test_timeline_file_item_files_view_keydown(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderFileView")
        # Both row types need keydown — outer toggles expand, inner navigates.
        assert body.count("addEventListener('keydown'") >= 2, (
            "files-view must wire keydown on both the outer expand row and the "
            "inner navigate row — Enter/Space activation parity with click"
        )

    def test_heatmap_col_role_button(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderTimeline")
        assert 'role="button"' in body, (
            ".tl-heatmap-col is a clickable date-jump button — must expose "
            "role=button so screen readers announce it as such"
        )

    def test_heatmap_col_aria_label(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderTimeline")
        assert "aria-label=" in body, (
            ".tl-heatmap-col must set aria-label so the date and chunk count "
            "are announced — bar chart is otherwise opaque to assistive tech"
        )

    def test_heatmap_col_keydown_handler(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderTimeline")
        assert "addEventListener('keydown'" in body, (
            ".tl-heatmap-col with role=button must respond to Enter/Space — "
            "tabindex=0 makes it focusable, so keyboard activation must work"
        )
