"""Pin tests for list-row accessibility attributes in the static web UI.

Search/source/timeline list-rows are constructed in JS, so behavior tests would
require a browser. These source-scan tests pin the a11y semantics so removing
``role=`` or ``aria-label`` silently can't slip past review (issue #700).
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
    """Pin a11y attributes on ``.result-item`` rows (issue #700)."""

    def test_buildresultitem_sets_role_button(self, app_js: str) -> None:
        body = _extract_function(app_js, "_buildResultItem")
        assert "setAttribute('role', 'button')" in body, (
            "result-item must expose role=button so screen readers announce it "
            "as a selectable row, not a generic group"
        )

    def test_buildresultitem_sets_aria_label(self, app_js: str) -> None:
        body = _extract_function(app_js, "_buildResultItem")
        assert "setAttribute('aria-label'" in body, (
            "result-item must set aria-label from filename/lines/namespace/age "
            "so screen readers announce a meaningful name"
        )


class TestTimelineRowA11y:
    """Pin a11y attributes on Timeline rows — chunks view, files view outer
    row, and files view inner chunk row (issue #700).
    """

    @staticmethod
    def _count_setattr(body: str, attr: str) -> int:
        # ``setAttribute`` may be split across lines by a formatter, so match
        # the call-form rather than a single literal string.
        return len(re.findall(rf"setAttribute\(\s*['\"]{re.escape(attr)}['\"]", body))

    def test_timeline_item_chunks_view_role_button(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderChunkView")
        assert "setAttribute('role', 'button')" in body, (
            "chunks-view .timeline-item must expose role=button — the row is the "
            "expand/collapse trigger, not a generic group"
        )

    def test_timeline_item_chunks_view_aria_label(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderChunkView")
        assert self._count_setattr(body, "aria-label") >= 1, (
            "chunks-view .timeline-item must set aria-label — at minimum filename "
            "and time, which are already on the row"
        )

    def test_timeline_item_chunks_view_keydown_handler(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderChunkView")
        assert "addEventListener('keydown'" in body, (
            "chunks-view .timeline-item with role=button must respond to "
            "Enter/Space — otherwise the screen-reader announce is a lie"
        )

    def test_timeline_file_item_files_view_role_button(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderFileView")
        # Files view has two row classes (.timeline-file-item + .tl-file-chunk-item),
        # both clickable; both need role=button. Pin count ≥ 2.
        assert self._count_setattr(body, "role") >= 2, (
            "files-view rows (.timeline-file-item AND each .tl-file-chunk-item) "
            "must both expose role=button"
        )

    def test_timeline_file_item_files_view_aria_label(self, timeline_js: str) -> None:
        body = _extract_function(timeline_js, "renderFileView")
        assert self._count_setattr(body, "aria-label") >= 2, (
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
