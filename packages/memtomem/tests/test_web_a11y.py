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
_CTX_JS = _STATIC_DIR / "context-gateway.js"


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


@pytest.fixture(scope="module")
def ctx_js() -> str:
    assert _CTX_JS.exists(), f"context-gateway.js missing: {_CTX_JS}"
    return _CTX_JS.read_text(encoding="utf-8")


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

    @pytest.mark.parametrize(
        "button_id",
        [
            # ✕ — saved-search delete
            "delete-query-btn",
            # ◀ / ▶ — chunk navigation in detail pane
            "d-prev-btn",
            "d-next-btn",
            # ✕ — edit-history panel close (had no title OR aria-label)
            "history-close-btn",
        ],
    )
    def test_residual_icon_buttons_have_accessible_names(
        self, index_html: str, button_id: str
    ) -> None:
        # Sweep follow-up to #1062 F1/#1065/#1066: the remaining icon-only
        # <button id=...> elements either relied on `title` alone or had no
        # accessible name at all. Same defect class as help-toggle (#1066).
        #
        # Excludes #save-star-btn — that button's label is state-dependent
        # (Save vs Remove) and is JS-owned; see
        # ``test_save_star_label_survives_langchange`` below.
        m = re.search(rf'<button\b[^>]*\bid="{re.escape(button_id)}"[^>]*>', index_html)
        assert m, f"#{button_id} button not found"
        tag = m.group(0)
        assert "data-i18n-aria-label=" in tag and "aria-label=" in tag, (
            f"#{button_id} is icon-only and must expose a translated accessible "
            "name; `title` alone is not reliably announced by VoiceOver"
        )

    def test_save_star_label_survives_langchange(self, index_html: str, app_js: str) -> None:
        # The save-star button is a toggle: ☆ → click saves the current query,
        # ★ → click *removes* it. A static aria-label="Save current search"
        # would announce the wrong action in the starred state — exactly the
        # bug flagged in PR review on #1068. So JS owns the label per-state and
        # the HTML must not declare data-i18n hooks that applyDOM() would
        # clobber back to search.save_title on every langchange. Mirrors the
        # view-toggle pattern.
        m = re.search(r'<button\b[^>]*\bid="save-star-btn"[^>]*>', index_html)
        assert m, "#save-star-btn button not found"
        tag = m.group(0)
        assert "data-i18n-title" not in tag, (
            "#save-star-btn must not declare data-i18n-title — applyDOM() "
            "would clobber the state-dependent label written by _syncSaveStar()"
        )
        assert "data-i18n-aria-label" not in tag, (
            "#save-star-btn must not declare data-i18n-aria-label — applyDOM() "
            "would clobber the state-dependent label written by _syncSaveStar()"
        )
        # The settings-namespaces.js module owns this button; the fixture is
        # app.js so search there is correct only if we read the right file.
        ns_js = (_STATIC_DIR / "settings-namespaces.js").read_text(encoding="utf-8")
        assert "function _syncSaveStar" in ns_js, (
            "_syncSaveStar helper must exist so click and langchange go through "
            "one label-writing code path"
        )
        # Click handler must delegate (not duplicate textContent/aria writes
        # inline — that was the original bug shape).
        click_start = ns_js.index("qs('save-star-btn').addEventListener('click'")
        click_end = ns_js.index("\n});", click_start)
        click_block = ns_js[click_start:click_end]
        assert "_syncSaveStar()" in click_block, (
            "save-star click handler must call _syncSaveStar so the per-state "
            "label is written by the shared helper, not inline"
        )
        assert re.search(
            r"window\.addEventListener\(\s*['\"]langchange['\"]\s*,\s*_syncSaveStar\s*\)",
            ns_js,
        ), (
            "_syncSaveStar must be registered as a langchange listener so the "
            "per-state label is re-translated when the user switches locale"
        )

    def test_tab_help_bar_dismiss_buttons_have_accessible_names(self, index_html: str) -> None:
        # The ✕ dismiss button on each tab's help bar (search, sources, index,
        # tags, timeline) is a class, not an id — sweep all instances and pin
        # that each one carries the shared `common.dismiss_help` aria-label.
        # Five instances exist today; if a new tab grows a help bar without a
        # name, this test fails.
        matches = re.findall(r'<button\b[^>]*\bclass="tab-help-bar-dismiss"[^>]*>', index_html)
        assert len(matches) >= 5, (
            f"expected ≥5 tab-help-bar-dismiss buttons, found {len(matches)}; "
            "if a tab was removed, lower the count — if added, ensure it has "
            "the aria-label below"
        )
        for tag in matches:
            assert 'data-i18n-aria-label="common.dismiss_help"' in tag and "aria-label=" in tag, (
                "every .tab-help-bar-dismiss button must declare "
                'data-i18n-aria-label="common.dismiss_help" + aria-label; '
                f"found a bare instance: {tag}"
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


class TestIssue1073CtxKeyboardSemantics:
    """Pin keyboard/screen-reader semantics on Context Gateway clickable
    rows (issue #1073). Overview tiles, artifact cards, and detail tabs
    were rendered as clickable ``<div>``s with mouse-only handlers — same
    defect class as the search/timeline rows in issue #700 but specific
    to ``context-gateway.js``.
    """

    # ── Overview tiles ────────────────────────────────────────────────
    def test_overview_tile_uses_inner_nav_button(self, ctx_js: str) -> None:
        # PR #1088 review (Codex P2): the navigation control must be a
        # real ``<button class="ctx-overview-stat-nav">`` *inside* the
        # tile, NOT ``role=button`` on the outer ``<div>``. Putting the
        # role on the outer div nests the ``.ctx-overview-pointer``
        # button children inside a button-role ancestor — invalid ARIA
        # (interactive content inside ``role=button`` is forbidden) and
        # inconsistently exposed by assistive tech.
        outer_match = re.search(
            r'`<div class="ctx-overview-stat"([^>]*)>\s*\n\s*'
            r'<button([^`>]*)class="ctx-overview-stat-nav"([^`>]*)>',
            ctx_js,
            re.DOTALL,
        )
        assert outer_match, (
            "tile must render as a plain <div class='ctx-overview-stat'> "
            "containing a <button class='ctx-overview-stat-nav'> — see"
            " PR #1088 review (nested-button regression)."
        )
        outer_attrs = outer_match.group(1)
        # ``data-section`` / ``data-tile-key`` stay on the outer div so
        # existing selectors (test suite, deep-link applier, CSS scoping)
        # keep working — the click handler reads them via
        # ``closest('.ctx-overview-stat')``.
        for marker in ("data-section=", "data-tile-key="):
            assert marker in outer_attrs, (
                f"outer .ctx-overview-stat must carry {marker} — "
                "existing browser-test selectors scope by these attrs"
            )
        nav_attrs = outer_match.group(2) + outer_match.group(3)
        for marker in ('type="button"', "aria-label="):
            assert marker in nav_attrs, (
                f"ctx-overview-stat-nav <button> must declare {marker} "
                "(carries the activation semantics + accessible name)"
            )

    def test_outer_tile_has_no_role_button(self, ctx_js: str) -> None:
        # Negative pin: the outer ``.ctx-overview-stat`` <div> must NOT
        # carry ``role="button"`` / ``tabindex="0"`` — that's what made
        # the pointer buttons nested-interactive (Codex P2 #1088 review).
        outer_match = re.search(r'`<div class="ctx-overview-stat"([^>]*)>', ctx_js)
        assert outer_match, "outer ctx-overview-stat div literal not found"
        outer_attrs = outer_match.group(1)
        assert "role=" not in outer_attrs, (
            "outer .ctx-overview-stat <div> must not carry role= — the "
            "navigation role belongs on the inner .ctx-overview-stat-nav"
            " button so pointer descendants aren't nested interactives"
        )
        assert "tabindex=" not in outer_attrs, (
            "outer .ctx-overview-stat <div> must not be focusable — only "
            "the inner nav button is in the keyboard focus order"
        )

    def test_overview_tile_click_wired_to_nav_button(self, ctx_js: str) -> None:
        # The click handler must target the inner ``.ctx-overview-stat-nav``
        # selector, not the outer ``.ctx-overview-stat``. Otherwise the
        # nested-button regression would silently re-appear if someone
        # restored ``role=button`` on the outer div and re-wired
        # ``.ctx-overview-stat`` as the click target.
        assert "el.querySelectorAll('.ctx-overview-stat-nav').forEach" in ctx_js, (
            "tile click wiring must use the inner .ctx-overview-stat-nav selector (PR #1088 review)"
        )
        # Pointer line stays its own wiring loop (sibling, not child).
        assert "el.querySelectorAll('.ctx-overview-pointer').forEach" in ctx_js, (
            "pointer-line wiring must remain a separate forEach loop on "
            ".ctx-overview-pointer so each pointer button keeps its own "
            "click handler"
        )

    # ── Artifact cards (.ctx-card) ────────────────────────────────────
    def test_clickable_ctx_card_has_button_semantics(self, ctx_js: str) -> None:
        # _ctxRenderItemsHtml gates a11y attrs on ``clickable``; readonly
        # cards (other-scope groups) must NOT get role=button. Anchor on
        # the ternary that builds the attrs.
        render_start = ctx_js.index("function _ctxRenderItemsHtml(")
        render_end = ctx_js.index("\n}\n", render_start)
        body = ctx_js[render_start:render_end]
        assert 'role="button" tabindex="0"' in body, (
            "clickable ctx-card must render with role=button + tabindex=0 "
            "(#1073) so it joins the keyboard focus order"
        )
        # The aria-label must be derived from the artifact name (and flag
        # out-of-sync state) — pinning the substring is enough; an
        # implementation that aria-labels with a stale string would still
        # need to read ``item.name``.
        assert "aria-label=" in body and "item.name" in body, (
            "ctx-card aria-label must include the artifact name so screen "
            "readers announce which item the row activates"
        )

    def test_clickable_ctx_card_aria_label_covers_all_statuses(self, ctx_js: str) -> None:
        # PR #1088 review (Codex P2): the aria-label must surface every
        # distinct non-``in sync`` runtime status, not just the
        # ``out of sync`` case. Otherwise cards with missing target /
        # missing canonical / parse error / runtime-only would announce
        # just the artifact name — and aria-label overrides the visible
        # runtime-badge text for screen readers, so the SR user would
        # lose the status that explains why the card needs action.
        render_start = ctx_js.index("function _ctxRenderItemsHtml(")
        render_end = ctx_js.index("\n}\n", render_start)
        body = ctx_js[render_start:render_end]
        # Pin the loop that collects per-runtime statuses via the shared
        # ``_ctxStatusText`` helper (same source the visible badge uses,
        # so SR string and visual badge cannot drift).
        assert re.search(
            r"for\s*\(\s*const\s+r\s+of\s*\(item\.runtimes.*?\)\s*\)\s*\{[^}]*"
            r"_ctxStatusText\s*\(\s*r\.status\s*\)",
            body,
            re.DOTALL,
        ), (
            "ctx-card aria-label builder must iterate ``item.runtimes`` "
            "and translate each non-in-sync status via ``_ctxStatusText`` "
            "so SR announce mirrors the visible badge (PR #1088 review)"
        )
        # And the runtime-only fallback: ``!item.canonical_path`` must
        # inject the ``missing canonical`` translation into the status
        # set so cards with an empty ``runtimes`` list still announce
        # the runtime-only state.
        assert re.search(
            r"!item\.canonical_path[^}]*?_ctxStatusText\s*\(\s*['\"]missing canonical['\"]\s*\)",
            body,
            re.DOTALL,
        ), (
            "ctx-card aria-label builder must add the localized "
            "``missing canonical`` text when ``item.canonical_path`` is "
            "empty, so runtime-only cards announce their state even "
            "when no per-runtime row carries the status"
        )
        # Negative pin: the old ``outOfSync ? ' — out of sync' : ''``
        # short-circuit MUST be gone — that's what hid the other four
        # status classes from SR users.
        assert "' — out of sync'" not in body, (
            "the legacy single-status aria-label suffix must be removed; "
            "use the per-runtime status set instead (PR #1088 review)"
        )

    def test_clickable_ctx_card_keydown_activates(self, ctx_js: str) -> None:
        # Anchor on the card-wiring loop inside _loadScopeGroupItems
        # (clickable branch) and pin keydown next to click. Without this,
        # a SR user hears "button" but pressing Enter does nothing.
        loop_start = ctx_js.index("container.querySelectorAll('.ctx-card').forEach(card => {")
        # The forEach body ends at the matching ``});`` — slice generously
        # and pin both handlers exist within the same block.
        loop_end = ctx_js.index("\n        });", loop_start)
        block = ctx_js[loop_start:loop_end]
        assert "addEventListener('click'" in block, "ctx-card click handler missing"
        assert "addEventListener('keydown'" in block, (
            "ctx-card must wire a keydown handler so Enter/Space activate "
            "the same path as click (#1073)"
        )

    def test_readonly_ctx_card_skips_button_role(self, ctx_js: str) -> None:
        # Pin that the a11y attrs are gated on ``clickable`` — a readonly
        # row would render a focusable button with no handler, which is
        # worse than a non-focusable div.
        render_start = ctx_js.index("function _ctxRenderItemsHtml(")
        render_end = ctx_js.index("\n}\n", render_start)
        body = ctx_js[render_start:render_end]
        assert re.search(
            r"clickable\s*\?\s*'\s*role=\"button\"\s*tabindex=\"0\"'\s*:\s*''",
            body,
        ), (
            "role=button + tabindex must be gated on ``clickable`` so "
            "readonly other-scope cards don't enter the focus order with "
            "no activation handler"
        )

    # ── Detail tabs ───────────────────────────────────────────────────
    def test_detail_tabs_render_as_tablist(self, ctx_js: str) -> None:
        # The tabs row must declare role=tablist; each tab must be a
        # <button role=tab> with aria-selected + aria-controls so the
        # screen reader announces the tab name AND the panel it controls.
        assert 'class="ctx-detail-tabs" role="tablist"' in ctx_js, (
            "ctx-detail-tabs container must declare role=tablist (#1073)"
        )
        # Both tab buttons must exist, with aria-controls pointing at
        # their pane id (type-qualified, see ``test_detail_ids_qualified_by_type``).
        canonical_tab = re.search(
            r'<button[^>]*class="ctx-detail-tab active"[^>]*data-pane="canonical"[^>]*>',
            ctx_js,
        )
        diff_tab = re.search(
            r'<button[^>]*class="ctx-detail-tab"[^>]*data-pane="diff"[^>]*>',
            ctx_js,
        )
        assert canonical_tab, "canonical tab must render as <button role=tab>"
        assert diff_tab, "diff tab must render as <button role=tab>"
        for label, tag in (("canonical", canonical_tab.group(0)), ("diff", diff_tab.group(0))):
            assert 'role="tab"' in tag, f"{label} tab missing role=tab"
            assert "aria-controls=" in tag and "ctx-pane-" in tag, (
                f"{label} tab must declare aria-controls pointing at its pane id"
            )
            assert "aria-selected=" in tag, (
                f"{label} tab must declare aria-selected so the SR announces the selected state"
            )
            assert "tabindex=" in tag, (
                f"{label} tab must declare tabindex for the roving focus model"
            )

    def test_detail_panes_are_tabpanels(self, ctx_js: str) -> None:
        # Each pane must declare role=tabpanel + aria-labelledby pointing
        # at its tab, so the SR announces "canonical source, tab panel"
        # on focus enter. IDs are type-qualified per ``test_detail_ids_
        # qualified_by_type``, so anchor on the role/aria pair alone.
        canonical_pane = re.search(
            r'id="ctx-pane-\$\{type\}-canonical"[^>]*role="tabpanel"'
            r'[^>]*aria-labelledby="ctx-tab-\$\{type\}-canonical"',
            ctx_js,
        )
        diff_pane = re.search(
            r'id="ctx-pane-\$\{type\}-diff"[^>]*role="tabpanel"'
            r'[^>]*aria-labelledby="ctx-tab-\$\{type\}-diff"',
            ctx_js,
        )
        assert canonical_pane, "canonical pane missing role=tabpanel + aria-labelledby"
        assert diff_pane, "diff pane missing role=tabpanel + aria-labelledby"

    def test_detail_ids_qualified_by_type(self, ctx_js: str) -> None:
        # Regression pin (PR #1088 review): inactive sections (skills /
        # commands / agents) keep their detail DOM mounted, so the new
        # ``ctx-tab-*`` and ``ctx-pane-*`` IDs MUST include ``type`` —
        # otherwise multiple sections share the same id and the
        # ``aria-controls`` / ``aria-labelledby`` references (which
        # resolve via document-wide ``getElementById``) point at an
        # earlier hidden section's pane instead of the active one.
        for fragment in (
            'id="ctx-tab-${type}-canonical"',
            'id="ctx-tab-${type}-diff"',
            'id="ctx-pane-${type}-canonical"',
            'id="ctx-pane-${type}-diff"',
            'aria-controls="ctx-pane-${type}-canonical"',
            'aria-controls="ctx-pane-${type}-diff"',
            'aria-labelledby="ctx-tab-${type}-canonical"',
            'aria-labelledby="ctx-tab-${type}-diff"',
        ):
            assert fragment in ctx_js, (
                f"detail-tab/pane fragment must be type-qualified to avoid "
                f"duplicate-ID collisions across sections (#1088 review): {fragment}"
            )
        # And the negative: the previous un-qualified literals must NOT
        # appear as JS strings or HTML attributes (the only allowed
        # mentions are in the explanatory comment paragraph above the
        # tablist HTML).
        for literal in (
            '"ctx-tab-canonical"',
            '"ctx-pane-canonical"',
            '"ctx-tab-diff"',
            '"ctx-pane-diff"',
        ):
            assert literal not in ctx_js, (
                f"un-qualified ID {literal} must not appear — see the type-qualified version above"
            )

    def test_detail_tab_keyboard_navigation(self, ctx_js: str) -> None:
        # Arrow nav within the tablist + ARIA state update on activation.
        # Pin (a) the helper that mutates aria-selected/tabindex on
        # activation, and (b) a keydown handler that delegates to
        # ``_arrowNavIndex`` so the tablist follows the same convention
        # as the main app's ``.tab-nav``.
        helper_start = ctx_js.index("_activateCtxDetailTab")
        helper_end = ctx_js.index("\n    };", helper_start)
        helper = ctx_js[helper_start:helper_end]
        assert "aria-selected" in helper, (
            "_activateCtxDetailTab must update aria-selected so the SR "
            "announce stays in sync with the visual .active class"
        )
        assert "tabindex" in helper, (
            "_activateCtxDetailTab must update tabindex (roving focus model) "
            "so only the active tab is in the keyboard focus order"
        )
        # The arrow-key listener must call _arrowNavIndex (shared helper
        # in app.js, used by .tab-nav and sources vendor tabs).
        nav_match = re.search(
            r"_ctxTabsContainer\.addEventListener\('keydown'.*?_arrowNavIndex",
            ctx_js,
            re.DOTALL,
        )
        assert nav_match, (
            "ctx-detail-tabs must wire keydown → _arrowNavIndex so Left/Right/"
            "Home/End move focus between tabs (#1073)"
        )
