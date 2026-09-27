"""Browser tests: the Sources lists say when ``/api/sources`` cut them short (#2566).

The route caps ``limit`` at 10,000 and reports every visible row in
``total``. The tree and the Memory Dirs panel drill-in ask for 10,000, so
past the cap they list fewer files than the per-root badges, which come from
``/api/memory-dirs/status`` and are never cut. Both surfaces now say so.
The stubs send a handful of rows with a larger ``total``.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

import pytest

from .conftest import goto_after_i18n_init, install_default_stubs

pytestmark = pytest.mark.browser

_BASE = "/tmp/mm-2566"
_USER = f"{_BASE}/user"
_CUT = f"{_BASE}/cut"
_PAST = f"{_BASE}/past"
_EMPTY = f"{_BASE}/empty"
_NO_STATUS = f"{_BASE}/no-status"


def _dir(path: str, source_files: int, chunks: int) -> dict[str, object]:
    return {
        "path": path,
        "exists": True,
        "provider": "user",
        "category": "user",
        "kind": "general",
        "tier": "user",
        "file_count": source_files,
        "source_file_count": source_files,
        "chunk_count": chunks,
        "delete_chunk_count": chunks,
        "hidden_source_file_count": 0,
        "hidden_chunk_count": 0,
        "held_source_file_count": 0,
        "held_chunk_count": 0,
    }


def _source(path: str, memory_dir: str, chunks: int = 1) -> dict[str, object]:
    return {
        "path": path,
        "chunk_count": chunks,
        "last_indexed_at": "2026-09-27T00:00:00Z",
        "file_size": 64,
        "namespaces": ["default"],
        "avg_tokens": 10,
        "min_tokens": 10,
        "max_tokens": 10,
        "memory_dir": memory_dir,
        "kind": "general",
        "target_scope": "user",
        "title": None,
        "excerpt": None,
        "ai_summary": None,
        "ai_summary_language": None,
    }


def _sources_body(rows: list[dict[str, object]], omitted: int | None) -> dict[str, object]:
    body: dict[str, object] = {"sources": rows, "offset": 0, "limit": 10000}
    if omitted is not None:
        body["total"] = len(rows) + omitted
    return body


class _Api:
    """One ``**/api/**`` route dispatching on the URL path, so the real
    ``/api/sources?limit=10000`` request is answered (a ``**/api/sources``
    glob would miss the query and fall through to ``{}``)."""

    def __init__(self, status: list[dict[str, object]], sources: dict[str, object]) -> None:
        self.status = status
        self.sources: dict[str, object] | None = sources
        self.hold_sources = False
        self.held: list[Any] = []
        self.stats: dict[str, object] = {}

    def payload(self, path: str) -> dict[str, object]:
        if path == "/api/system/ui-mode":
            return {"mode": "prod"}
        if path == "/api/system/model-readiness":
            return {"ready": True}
        if path == "/api/config":
            return {"indexing": {"memory_dirs": [d["path"] for d in self.status]}}
        if path == "/api/memory-dirs/status":
            return {"dirs": self.status}
        if path == "/api/context/projects":
            return {"scopes": []}
        if path == "/api/stats":
            return self.stats
        return {}

    def handle(self, route) -> None:
        path = urlparse(route.request.url).path
        if path == "/api/sources" and route.request.method == "GET":
            if self.hold_sources:
                self.held.append(route)
                return
            if self.sources is None:
                route.fulfill(status=500, content_type="application/json", body='{"detail":"x"}')
                return
            body: dict[str, object] = self.sources
        else:
            body = self.payload(path)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def install(self, page) -> None:
        page.route("**/api/**", self.handle)


def _t(page, key: str, params: dict[str, object] | None = None) -> str:
    return page.evaluate("([k, p]) => t(k, p)", [key, params or {}])


def _partial_note(page, shown: int, total: int) -> str:
    return _t(
        page,
        "sources.partial_note",
        {"shown": f"{shown:,}", "total": f"{total:,}"},
    )


# ---------------------------------------------------------------------------
# Tree header
# ---------------------------------------------------------------------------

_TREE_STATUS = [_dir(_USER, 5, 5)]
_TREE_ROWS = [_source(f"{_USER}/a.md", _USER), _source(f"{_USER}/b.md", _USER)]


def _open_tree(page, base_url: str, api: _Api) -> None:
    api.install(page)
    # Click Sources only after boot: its hash handling can otherwise start a
    # second load that races the assertions.
    goto_after_i18n_init(page, base_url, probe_key="sources.partial_note")
    page.locator('.tab-btn[data-tab="sources"]').click()
    page.wait_for_function(
        'dir => !!document.querySelector(`#sources-list details.source-group[data-dir="${dir}"]`)',
        arg=_USER,
        timeout=5_000,
    )


def _header(page) -> dict[str, object]:
    return page.evaluate(
        """() => {
          const stats = document.getElementById('sources-stats');
          const note = document.getElementById('sources-partial-note');
          return {
            stats: stats.hidden ? null : stats.textContent,
            note: note.hidden ? null : note.textContent,
            noteVisible: note.checkVisibility(),
            noteHasI18nAttr: note.hasAttribute('data-i18n'),
          };
        }"""
    )


def _click_vendor(page, vendor: str) -> None:
    page.evaluate(
        'v => document.querySelector(`.sources-vendor-tab[data-vendor="${v}"]`).click()',
        vendor,
    )


def test_tree_header_says_the_list_is_partial(page, mm_web_url: str) -> None:
    api = _Api(_TREE_STATUS, _sources_body(_TREE_ROWS, omitted=3))
    _open_tree(page, mm_web_url, api)

    header = _header(page)
    # The files·chunks line still counts the loaded rows, as before.
    assert header["stats"] == _t(page, "header.stat_files_chunks", {"files": 2, "chunks": "2"})
    assert header["note"] == _partial_note(page, 2, 5)
    assert header["noteVisible"] is True
    assert header["noteHasI18nAttr"] is False


@pytest.mark.parametrize("omitted", [0, None], ids=["total-equals-rows", "no-total"])
def test_tree_header_has_no_note_for_a_complete_list(
    page, mm_web_url: str, omitted: int | None
) -> None:
    api = _Api(_TREE_STATUS, _sources_body(_TREE_ROWS, omitted=omitted))
    _open_tree(page, mm_web_url, api)

    header = _header(page)
    assert header["stats"] == _t(page, "header.stat_files_chunks", {"files": 2, "chunks": "2"})
    assert header["note"] is None
    assert header["noteVisible"] is False


def test_note_stays_for_an_empty_vendor_and_a_filter(page, mm_web_url: str) -> None:
    """The cut is across all vendors, so a vendor with no loaded rows, or a
    filter that matches none, still gets the note."""
    api = _Api(_TREE_STATUS, _sources_body(_TREE_ROWS, omitted=3))
    _open_tree(page, mm_web_url, api)
    expected = _partial_note(page, 2, 5)

    _click_vendor(page, "claude")
    page.wait_for_function(
        "() => document.querySelector('.sources-vendor-tab.active')?.dataset.vendor === 'claude'",
        timeout=5_000,
    )
    header = _header(page)
    assert header["stats"] is None  # Claude has no loaded rows.
    assert header["note"] == expected

    _click_vendor(page, "user")
    page.locator("#sources-filter").fill("zzz-no-such-file")
    page.wait_for_function(
        "() => !document.querySelector('#sources-list .source-item')",
        timeout=5_000,
    )
    assert _header(page)["note"] == expected


def test_delete_keeps_the_count_left_out(page, mm_web_url: str) -> None:
    """Deleting a listed row leaves the rows the server never sent, so the
    note's total drops by one with the shown count."""
    api = _Api(_TREE_STATUS, _sources_body(_TREE_ROWS, omitted=3))
    _open_tree(page, mm_web_url, api)

    page.evaluate(
        """async (path) => {
          window.showConfirm = async () => true;
          await _deleteSourceFile(path);
        }""",
        f"{_USER}/a.md",
    )
    assert _header(page)["note"] == _partial_note(page, 1, 4)


def test_failed_reload_hides_the_note_until_rows_are_redrawn(page, mm_web_url: str) -> None:
    api = _Api(_TREE_STATUS, _sources_body(_TREE_ROWS, omitted=3))
    _open_tree(page, mm_web_url, api)
    expected = _partial_note(page, 2, 5)

    # While the reload is out, the spinner replaces the rows and the note goes.
    api.hold_sources = True
    page.evaluate("() => { loadSources(); }")
    for _ in range(50):
        if api.held:
            break
        page.wait_for_timeout(20)
    assert len(api.held) == 1
    assert _header(page)["note"] is None

    api.held.pop().fulfill(status=500, content_type="application/json", body='{"detail":"x"}')
    page.wait_for_function(
        "() => !!document.querySelector('#sources-list .empty-state')",
        timeout=5_000,
    )
    assert _header(page)["note"] is None

    # A vendor switch redraws the kept rows, and the note comes back with them.
    _click_vendor(page, "claude")
    _click_vendor(page, "user")
    page.wait_for_function(
        'dir => !!document.querySelector(`#sources-list details.source-group[data-dir="${dir}"]`)',
        arg=_USER,
        timeout=5_000,
    )
    assert _header(page)["note"] == expected


def test_home_snapshot_clears_the_note(page, mm_web_url: str) -> None:
    """Home's ``home_sources`` is uncapped. When it replaces the list (a slow
    dashboard load landing after Sources), nothing is left out any more."""
    api = _Api(_TREE_STATUS, _sources_body(_TREE_ROWS, omitted=3))
    _open_tree(page, mm_web_url, api)
    assert _header(page)["note"] is not None

    api.stats = {"home_sources": [*_TREE_ROWS, _source(f"{_USER}/c.md", _USER)]}
    page.evaluate("() => loadDashboard()")
    page.wait_for_function("() => STATE.allSources.length === 3", timeout=5_000)
    _click_vendor(page, "claude")
    _click_vendor(page, "user")
    header = _header(page)
    assert header["stats"] == _t(page, "header.stat_files_chunks", {"files": 3, "chunks": "3"})
    assert header["note"] is None


def test_ko_locale_renders_the_ko_note(page, mm_web_url: str) -> None:
    api = _Api(_TREE_STATUS, _sources_body(_TREE_ROWS, omitted=3))
    page.add_init_script("try { localStorage.setItem('m2m-lang', 'ko'); } catch (e) {}")
    _open_tree(page, mm_web_url, api)

    note = _header(page)["note"]
    assert page.evaluate("() => I18N.lang()") == "ko"
    assert note == _partial_note(page, 2, 5)
    assert "5개" in note and "2개" in note


def test_language_switch_retranslates_the_note(page, mm_web_url: str) -> None:
    """The note has no ``data-i18n``, so ``langchange`` must redraw it."""
    api = _Api(_TREE_STATUS, _sources_body(_TREE_ROWS, omitted=3))
    page.add_init_script("try { localStorage.setItem('m2m-lang', 'en'); } catch (e) {}")
    _open_tree(page, mm_web_url, api)
    english = _header(page)["note"]

    page.evaluate("() => I18N.setLang('ko')")
    page.wait_for_function("() => I18N.lang() === 'ko'", timeout=5_000)
    korean = _header(page)["note"]
    assert korean == _partial_note(page, 2, 5)
    assert korean != english


# ---------------------------------------------------------------------------
# Memory Dirs panel drill-in
# ---------------------------------------------------------------------------

# ``_CUT``: 3 listed, 1 loaded. ``_PAST``: 2 listed, none loaded (all past the
# cut). ``_EMPTY``: nothing to list. ``_NO_STATUS``: configured, no status.
_PANEL_STATUS = [_dir(_CUT, 3, 3), _dir(_PAST, 2, 2), _dir(_EMPTY, 0, 0)]
_PANEL_ROWS = [_source(f"{_CUT}/a.md", _CUT)]
_PANEL_DIRS = [_CUT, _PAST, _EMPTY, _NO_STATUS]


def _mount_panel(page, base_url: str, api: _Api) -> None:
    install_default_stubs(page)
    api.install(page)
    goto_after_i18n_init(page, base_url, probe_key="sources.memory_dirs.files_partial")
    page.evaluate(
        """(dirs) => {
          const host = document.createElement('div');
          host.id = 'test-md-panel';
          document.body.appendChild(host);
          host.appendChild(_buildMemoryDirsPanel(dirs));
        }""",
        _PANEL_DIRS,
    )
    # Wait for the status fetch's re-render: rows drawn before it are
    # replaced, and a drill-in opened on one would be detached with it.
    page.wait_for_function(
        """dirs => dirs.every(dir => {
          const btn = document.querySelector(`#test-md-panel .memory-dirs-path[title="${dir}"]`);
          const meta = btn && btn.closest('.memory-dirs-item').querySelector('.memory-dirs-item-meta');
          return meta && meta.textContent.trim();
        })""",
        arg=[d["path"] for d in api.status],
        timeout=5_000,
    )


def _drill(page, directory: str) -> dict[str, object]:
    """Expand ``directory``'s row and return what the drill-in shows."""
    page.evaluate(
        """dir => document.querySelector(
          `#test-md-panel .memory-dirs-path[title="${dir}"]`).click()""",
        directory,
    )
    page.wait_for_function(
        """dir => {
          const item = document.querySelector(
            `#test-md-panel .memory-dirs-path[title="${dir}"]`).closest('.memory-dirs-item');
          const wrap = item.querySelector('.memory-dirs-files');
          return wrap && !wrap.querySelector('.memory-dirs-files-loading');
        }""",
        arg=directory,
        timeout=5_000,
    )
    return page.evaluate(
        """dir => {
          const item = document.querySelector(
            `#test-md-panel .memory-dirs-path[title="${dir}"]`).closest('.memory-dirs-item');
          const wrap = item.querySelector('.memory-dirs-files');
          const one = (sel) => { const e = wrap.querySelector(sel); return e ? e.textContent : null; };
          return {
            partial: one('.memory-dirs-files-partial'),
            partialFirst: wrap.firstElementChild?.classList.contains('memory-dirs-files-partial'),
            empty: one('.memory-dirs-files-empty'),
            files: [...wrap.querySelectorAll('.memory-dirs-file-name')].map(e => e.textContent),
          };
        }""",
        directory,
    )


def _collapse(page, directory: str) -> None:
    page.evaluate(
        """dir => document.querySelector(
          `#test-md-panel .memory-dirs-path[title="${dir}"]`).click()""",
        directory,
    )


def test_drill_in_flags_only_dirs_the_cut_reached(page, mm_web_url: str) -> None:
    api = _Api(_PANEL_STATUS, _sources_body(_PANEL_ROWS, omitted=4))
    _mount_panel(page, mm_web_url, api)

    cut = _drill(page, _CUT)
    assert cut["files"] == ["a.md"]
    assert cut["partial"] == _t(
        page, "sources.memory_dirs.files_partial", {"shown": "1", "total": "3"}
    )
    assert cut["partialFirst"] is True
    assert cut["empty"] is None

    # Every file past the cut: the note replaces the "no indexed files" text.
    past = _drill(page, _PAST)
    assert past["files"] == []
    assert past["partial"] == _t(
        page, "sources.memory_dirs.files_partial", {"shown": "0", "total": "2"}
    )
    assert past["empty"] is None

    # Nothing to list: a genuine empty state, no note.
    empty = _drill(page, _EMPTY)
    assert empty["partial"] is None
    assert empty["empty"] == _t(page, "sources.memory_dirs.files_empty")

    # No status entry: this dir's share is unknown, so the overall numbers.
    no_status = _drill(page, _NO_STATUS)
    assert no_status["partial"] == _partial_note(page, 1, 5)
    assert no_status["empty"] is None


def test_drill_in_has_no_note_for_a_complete_list(page, mm_web_url: str) -> None:
    api = _Api(_PANEL_STATUS, _sources_body(_PANEL_ROWS, omitted=0))
    _mount_panel(page, mm_web_url, api)

    shown = {directory: _drill(page, directory) for directory in _PANEL_DIRS}
    assert all(s["partial"] is None for s in shown.values()), shown
    # A dir with no loaded rows reads as empty, as before.
    assert shown[_PAST]["empty"] == _t(page, "sources.memory_dirs.files_empty")


def test_drill_in_ignores_a_response_from_before_a_reindex(page, mm_web_url: str) -> None:
    """A request already out when a reindex invalidates the cache must not
    publish its older answer over the newer one."""
    api = _Api(_PANEL_STATUS, _sources_body(_PANEL_ROWS, omitted=0))
    _mount_panel(page, mm_web_url, api)

    # 1. Drill in with the response held back. Keep a handle on this
    # drill-in: the reindex re-render detaches it, but it still renders
    # whatever its own request resolves to.
    api.hold_sources = True
    page.evaluate(
        """dir => {
          const btn = document.querySelector(`#test-md-panel .memory-dirs-path[title="${dir}"]`);
          btn.click();
          window.__staleWrap = btn.closest('.memory-dirs-item').querySelector('.memory-dirs-files');
        }""",
        _CUT,
    )
    for _ in range(50):
        if api.held:
            break
        page.wait_for_timeout(20)
    assert len(api.held) == 1
    stale_route = api.held.pop()

    # 2. Reindex invalidates the cache and re-renders the rows.
    page.evaluate(
        """dir => document.querySelector(
          `#test-md-panel .memory-dirs-item:has(.memory-dirs-path[title="${dir}"]) .memory-dirs-reindex-btn`
        ).click()""",
        _CUT,
    )
    page.wait_for_function(
        """dir => !document.querySelector(
          `#test-md-panel .memory-dirs-path[title="${dir}"]`).closest('.memory-dirs-item')
          .classList.contains('memory-dirs-item-expanded')""",
        arg=_CUT,
        timeout=5_000,
    )

    # 3. A new drill-in gets the newer answer.
    api.hold_sources = False
    newer = [_source(f"{_CUT}/b.md", _CUT)]
    api.sources = _sources_body(newer, omitted=4)
    assert _drill(page, _CUT)["files"] == ["b.md"]

    # 4. The stale answer lands last.
    stale_route.fulfill(
        status=200,
        content_type="application/json",
        body=json.dumps(_sources_body(_PANEL_ROWS, omitted=0)),
    )
    page.wait_for_function(
        "() => !window.__staleWrap.querySelector('.memory-dirs-files-loading')",
        timeout=5_000,
    )
    # The drill-in that sent the stale request shows the newer answer too.
    stale_files = page.evaluate(
        """() => [...window.__staleWrap.querySelectorAll('.memory-dirs-file-name')]
            .map(e => e.textContent)"""
    )
    assert stale_files == ["b.md"]

    # The cache still holds the newer answer: rows and its note.
    _collapse(page, _CUT)
    again = _drill(page, _CUT)
    assert again["files"] == ["b.md"]
    assert again["partial"] == _t(
        page, "sources.memory_dirs.files_partial", {"shown": "1", "total": "3"}
    )
