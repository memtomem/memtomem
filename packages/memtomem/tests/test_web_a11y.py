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
_INDEX_HTML = _STATIC_DIR / "index.html"
_TIMELINE_JS = _STATIC_DIR / "timeline.js"


@pytest.fixture(scope="module")
def app_js() -> str:
    assert _APP_JS.exists(), f"app.js missing: {_APP_JS}"
    return _APP_JS.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def index_html() -> str:
    assert _INDEX_HTML.exists(), f"index.html missing: {_INDEX_HTML}"
    return _INDEX_HTML.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def timeline_js() -> str:
    assert _TIMELINE_JS.exists(), f"timeline.js missing: {_TIMELINE_JS}"
    return _TIMELINE_JS.read_text(encoding="utf-8")


def _extract_function(source: str, name: str) -> str:
    """Extract a top-level ``[async ]function name(...) { ... }`` body via brace matching."""
    m = re.search(rf"\b(?:async\s+)?function\s+{re.escape(name)}\s*\(", source)
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


def _count_setattr(body: str, attr: str) -> int:
    """Count ``setAttribute('<attr>', ...)`` regardless of formatter line wrap."""
    return len(re.findall(rf"setAttribute\(\s*['\"]{re.escape(attr)}['\"]", body))


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


class TestIssue1062IconButtonNames:
    """Pin source-level labels for icon-only controls checked by issue #1062."""

    def test_modal_close_buttons_have_accessible_names(self, index_html: str) -> None:
        for button_id in (
            "expand-close-btn",
            "source-preview-close",
            "settings-close-btn",
            "shortcuts-close-btn",
        ):
            m = re.search(rf'<button\b[^>]*\bid="{re.escape(button_id)}"[^>]*>', index_html)
            assert m, f"#{button_id} button not found"
            tag = m.group(0)
            assert "data-i18n-aria-label=" in tag and "aria-label=" in tag, (
                f"#{button_id} is icon-only and must expose a translated "
                "accessible name instead of relying on the × glyph"
            )

    def test_help_toggle_has_accessible_name(self, index_html: str) -> None:
        # Regression pin for #1062 F1: help-toggle was the only header icon-only
        # button without aria-label, relying on `title` alone — which VoiceOver
        # does not reliably announce in form-controls rotor mode.
        m = re.search(r'<button\b[^>]*\bid="help-toggle"[^>]*>', index_html)
        assert m, "#help-toggle button not found"
        tag = m.group(0)
        assert "data-i18n-aria-label=" in tag and "aria-label=" in tag, (
            "#help-toggle is icon-only ('?' glyph) and must expose a translated "
            "accessible name; `title` alone is not reliably announced by VoiceOver"
        )

    def test_view_toggle_updates_runtime_aria_label(self, app_js: str) -> None:
        # Bound by intrinsic anchors (the state flip line and the renderResults
        # tail call) rather than a // --- comment delimiter, so a reflow of the
        # surrounding section header doesn't break the test in a confusing way.
        listener_start = app_js.index("qs('view-toggle').addEventListener")
        listener_end = app_js.index("renderResults(STATE.lastResults);", listener_start) + len(
            "renderResults(STATE.lastResults);"
        )
        block = app_js[listener_start:listener_end]
        assert "_syncViewToggle()" in block, (
            "view-toggle click handler must delegate to _syncViewToggle so the "
            "same label logic runs on click and on langchange"
        )

    def test_view_toggle_label_survives_langchange(self, app_js: str, index_html: str) -> None:
        # Regression pin: a previous iteration left data-i18n-title /
        # data-i18n-aria-label on #view-toggle. I18N.applyDOM() (invoked on
        # every langchange) then reset both attributes to the generic
        # search.view_title string, silently undoing the per-state runtime
        # label written by the click handler. JS now owns these attributes,
        # so the HTML element must NOT carry the i18n hooks, and a langchange
        # listener must call the shared sync helper.
        m = re.search(r'<button\b[^>]*\bid="view-toggle"[^>]*>', index_html)
        assert m, "#view-toggle button not found"
        tag = m.group(0)
        assert "data-i18n-title" not in tag, (
            "#view-toggle must not declare data-i18n-title — applyDOM() would "
            "clobber the state-dependent label written by _syncViewToggle()"
        )
        assert "data-i18n-aria-label" not in tag, (
            "#view-toggle must not declare data-i18n-aria-label — applyDOM() "
            "would clobber the state-dependent label written by _syncViewToggle()"
        )
        assert re.search(
            r"window\.addEventListener\(\s*['\"]langchange['\"]\s*,\s*_syncViewToggle\s*\)",
            app_js,
        ), (
            "_syncViewToggle must be registered as a langchange listener so "
            "the per-state label is re-translated when the user switches locale"
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


class TestHomeActivityHeatmapA11y:
    """Pin Home activity heatmap accessibility semantics (issue #986)."""

    def test_heatmap_only_active_dates_are_keyboard_buttons(self, app_js: str) -> None:
        body = _extract_function(app_js, "_renderActivityMap")
        assert "const isInteractive = cell.count > 0;" in body, (
            "Home heatmap must not make zero-activity days keyboard-focusable"
        )
        assert 'role="button" tabindex="0"' in body, (
            "active heatmap dates must remain keyboard-focusable timeline jumps"
        )
        assert 'aria-hidden="true"' in body, (
            "non-interactive heatmap cells should stay out of the screen-reader navigation surface"
        )

    def test_heatmap_has_orientation_summary_and_legend(self, app_js: str) -> None:
        body = _extract_function(app_js, "_renderActivityMap")
        for marker in (
            "activity-summary",
            "activity-weekdays",
            "activity-legend",
            "home.activity.summary_aria",
            "home.activity.intensity_peak",
        ):
            assert marker in body, f"Home heatmap missing a11y/orientation marker: {marker}"


class TestChunkCardA11y:
    """Pin a11y attributes on source-detail ``.chunk-card`` rows (issue #700).

    All cards must have an accessible name; collapsible (toggle-able) cards
    must additionally have ``role=button`` + keyboard activation.
    """

    def test_chunk_card_has_aria_label(self, app_js: str) -> None:
        body = _extract_function(app_js, "browseSource")
        # The aria-label is set on the card before the rAF accordion pass —
        # i.e. unconditionally for every card, not just collapsible ones.
        assert "card.setAttribute" in body, "card.setAttribute missing"
        assert _count_setattr(body, "aria-label") >= 1, (
            "every chunk-card must set aria-label so screen readers announce a "
            "meaningful name (chunk type + line range + heading trail)"
        )

    def test_collapsible_chunk_card_role_button(self, app_js: str) -> None:
        body = _extract_function(app_js, "browseSource")
        # Slice to the accordion activation block — role=button must be set
        # ALONGSIDE the chunk-card-collapsible class, not on every card.
        m = re.search(r"chunk-card-collapsible.*?\}\s*\}\s*\)\s*;", body, re.DOTALL)
        assert m, "chunk-card-collapsible block not found"
        block = m.group(0)
        assert "setAttribute('role', 'button')" in block, (
            "collapsible chunk-cards must expose role=button — they are the "
            "expand/collapse trigger, mirroring home-source-item / result-item"
        )
        assert "setAttribute('tabindex'" in block, (
            "collapsible chunk-cards must be keyboard-focusable"
        )

    def test_collapsible_chunk_card_keydown_handler(self, app_js: str) -> None:
        body = _extract_function(app_js, "browseSource")
        m = re.search(r"chunk-card-collapsible.*?\}\s*\}\s*\)\s*;", body, re.DOTALL)
        assert m, "chunk-card-collapsible block not found"
        block = m.group(0)
        assert "addEventListener('keydown'" in block, (
            "role=button without Enter/Space activation is a lie — pair the "
            "click handler with a keydown handler"
        )
        # Must guard against firing while the user is editing or pressing
        # Enter on an action button — those cases should not toggle the card.
        assert "chunk-card-edit-area" in block, (
            "keydown handler must skip when focus is in .chunk-card-edit-area "
            "(textarea Enter must not collapse the card)"
        )
        assert "chunk-card-actions" in block, (
            "keydown handler must skip when focus is in .chunk-card-actions "
            "(button Enter must not also toggle the parent card)"
        )
