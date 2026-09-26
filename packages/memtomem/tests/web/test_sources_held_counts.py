"""Browser tests: Sources badges count what the tree lists (#2561).

``GET /api/sources`` hides held and pending sources, while
``/api/memory-dirs/status`` totals still include them. The status now also
reports the hidden part (``held_source_file_count`` / ``held_chunk_count``),
and the badges show totals minus held. The Sources tree explains the held
part in the group's first line and in the badge tooltip, not in the header,
which has no width to spare for the path. The Memory Dirs panel appends
"N hidden" to its meta line. A server without the held fields renders as
before. State (Discovered,
pending, Index/Reindex) keeps reading the totals, so a root whose sources are
all held is still an indexed root, not a Discovered one.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from .conftest import goto_after_i18n_init, install_default_stubs

pytestmark = pytest.mark.browser

_BASE = "/tmp/mm-2561"
_MIXED = f"{_BASE}/mixed"
_ALL_HELD = f"{_BASE}/all-held"
_OLD = f"{_BASE}/old-server"


def _dir(path: str, **counts: int) -> dict[str, object]:
    return {
        "path": path,
        "exists": True,
        "provider": "user",
        "category": "user",
        "kind": "general",
        "tier": "user",
        **counts,
    }


# ``_MIXED``: 1 listed source (2 chunks) + 2 hidden sources (5 chunks).
# ``_ALL_HELD``: every indexed source is hidden and gone from disk.
# ``_OLD``: an older server's entry, no held fields.
_STATUS = [
    _dir(
        _MIXED,
        file_count=1,
        source_file_count=3,
        chunk_count=7,
        held_source_file_count=2,
        held_chunk_count=5,
        delete_chunk_count=7,
    ),
    _dir(
        _ALL_HELD,
        file_count=0,
        source_file_count=2,
        chunk_count=4,
        held_source_file_count=2,
        held_chunk_count=4,
        delete_chunk_count=4,
    ),
    _dir(_OLD, file_count=1, source_file_count=1, chunk_count=3, delete_chunk_count=3),
]


def _source(path: str, memory_dir: str, chunks: int) -> dict[str, object]:
    return {
        "path": path,
        "chunk_count": chunks,
        "last_indexed_at": "2026-09-26T00:00:00Z",
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


_SOURCES = [_source(f"{_MIXED}/a.md", _MIXED, 2), _source(f"{_OLD}/o.md", _OLD, 3)]


def _payload(url: str) -> dict[str, object]:
    path = urlparse(url).path
    if path == "/api/system/ui-mode":
        return {"mode": "prod"}
    if path == "/api/system/model-readiness":
        return {"ready": True}
    if path == "/api/config":
        return {"indexing": {"memory_dirs": [_MIXED, _ALL_HELD, _OLD]}}
    if path == "/api/memory-dirs/status":
        return {"dirs": _STATUS}
    if path == "/api/sources":
        return {"sources": _SOURCES, "total": len(_SOURCES), "offset": 0, "limit": 10000}
    return {}


def _open_sources_tab(page, base_url: str) -> None:
    page.route(
        "**/api/**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_payload(r.request.url))
        ),
    )
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator('.tab-btn[data-tab="sources"]').click()
    page.wait_for_function(
        'dir => !!document.querySelector(`#sources-list details.source-group[data-dir="${dir}"]`)',
        arg=_MIXED,
        timeout=5_000,
    )


def _group(page, directory: str) -> dict[str, object] | None:
    return page.evaluate(
        """dir => {
          const g = document.querySelector(`#sources-list details.source-group[data-dir="${dir}"]`);
          if (!g) return null;
          const header = g.querySelector(':scope > summary');
          const stats = header.querySelector('.source-group-stats');
          const note = g.querySelector(':scope > .source-group-held-note');
          return {
            stats: stats ? stats.textContent : null,
            pending: stats ? stats.classList.contains('pending') : null,
            statsTitle: stats ? stats.title : null,
            note: note ? note.textContent : null,
            noteIsFirstLine: !!note && header.nextElementSibling === note,
            open: g.open,
            noteVisible: !!note && note.checkVisibility(),
            headerClasses: [...header.children].map(c => c.className)
              .filter(c => !c.includes('pill')),
            reindex: header.querySelector('.source-group-actions button:nth-child(2)').textContent,
          };
        }""",
        directory,
    )


def _t(page, key: str, params: dict[str, object]) -> str:
    return page.evaluate("([k, p]) => t(k, p)", [key, params])


def test_badges_show_listed_counts_and_a_hidden_note(page, mm_web_url: str) -> None:
    _open_sources_tab(page, mm_web_url)

    mixed = _group(page, _MIXED)
    assert mixed is not None
    assert mixed["stats"] == _t(
        page, "sources.memory_dirs.status_group", {"files": 1, "indexed": 1, "chunks": 2}
    )
    sentence = _t(page, "sources.memory_dirs.status_held_title", {"count": 2, "chunks": 5})
    # Explained in the group's first line and in the badge tooltip.
    assert mixed["note"] == sentence
    assert mixed["noteIsFirstLine"] is True
    assert mixed["statsTitle"] == sentence

    # All held: an indexed root (Reindex, not Discovered) that lists nothing.
    all_held = _group(page, _ALL_HELD)
    assert all_held is not None
    assert all_held["stats"] == _t(
        page, "sources.memory_dirs.status_group", {"files": 0, "indexed": 0, "chunks": 0}
    )
    assert all_held["note"] == _t(
        page, "sources.memory_dirs.status_held_title", {"count": 2, "chunks": 4}
    )
    assert all_held["pending"] is False
    # Folder mode opens a group only to activate a file; a root with no
    # listed file opens anyway, so its first line is on screen. A root with
    # listed files keeps the old default and explains itself in the tooltip.
    assert all_held["open"] is True and all_held["noteVisible"] is True
    assert mixed["open"] is False and mixed["noteVisible"] is False
    assert all_held["reindex"] == _t(page, "sources.memory_dirs.action_reindex", {})

    # Older server: no held fields, totals as before, no note.
    old = _group(page, _OLD)
    assert old is not None
    assert old["stats"] == _t(
        page, "sources.memory_dirs.status_group", {"files": 1, "indexed": 1, "chunks": 3}
    )
    assert old["note"] is None
    assert old["statsTitle"] == ""
    # The note adds nothing to the header: same elements as a root without it.
    assert mixed["headerClasses"] == old["headerClasses"]


@pytest.mark.parametrize("layout", ["filter", "chunks", "size", "recent"])
def test_headerless_layouts_list_nothing_for_an_all_held_root(
    page, mm_web_url: str, layout: str
) -> None:
    """A filter or a Chunks/Size/Recent sort renders source cards without
    group headers, so no root has a badge there. An all-held root has no
    card to render and does not appear; the mixed root's listed source
    does."""
    _open_sources_tab(page, mm_web_url)
    if layout == "filter":
        page.locator("#sources-filter").fill("a.md")
    else:
        page.evaluate("(by) => { STATE.sourcesSortBy = by; loadSources(); }", layout)
    page.wait_for_function(
        """() => !document.querySelector('#sources-list details.source-group')
            && !!document.querySelector('#sources-list .source-item')""",
        timeout=5_000,
    )
    assert _group(page, _ALL_HELD) is None
    listed = page.evaluate(
        "() => [...document.querySelectorAll('#sources-list .source-item')].map(e => e.title)"
    )
    assert f"{_MIXED}/a.md" in listed
    assert not any(p.startswith(_ALL_HELD) for p in listed)


def test_reindex_progress_keeps_the_hidden_note(page, mm_web_url: str) -> None:
    """``mdReindexOne`` writes progress into ``.source-group-stats`` and
    restores it with ``textContent`` at each file boundary. The badge
    tooltip and the group's note line must survive that."""
    _open_sources_tab(page, mm_web_url)
    result = page.evaluate(
        """async (dir) => {
          let onEvent = null;
          let finish = () => {};
          window.fetchIndexStream = (body, opts = {}) => {
            onEvent = opts.onEvent;
            return new Promise((resolve) => { finish = resolve; });
          };
          const group = document.querySelector(
            `#sources-list details.source-group[data-dir="${dir}"]`);
          const header = group.querySelector(':scope > summary');
          const btn = header.querySelector('.source-group-actions button:nth-child(2)');
          const stats = header.querySelector('.source-group-stats');
          const before = stats.textContent;
          const done = mdReindexOne(dir, btn);
          for (let i = 0; i < 100 && onEvent === null; i++) {
            await new Promise((r) => setTimeout(r, 5));
          }
          onEvent({ type: 'chunk_progress', file: dir + '/a.md', chunks_done: 2, chunks_total: 2 });
          const during = stats.textContent;
          onEvent({ type: 'progress', files_done: 1, files_total: 1 });
          const note = group.querySelector(':scope > .source-group-held-note');
          const after = {
            stats: stats.textContent,
            title: stats.title,
            note: note ? note.textContent : null,
          };
          onEvent({ type: 'complete', indexed_chunks: 2, errors: [] });
          finish();
          await done;
          return { before, during, after };
        }""",
        _MIXED,
    )
    assert result["during"] != result["before"]
    assert result["after"]["stats"] == result["before"]
    sentence = _t(page, "sources.memory_dirs.status_held_title", {"count": 2, "chunks": 5})
    assert result["after"]["title"] == sentence
    assert result["after"]["note"] == sentence


def test_memory_dirs_panel_counts_listed_sources(page, mm_web_url: str) -> None:
    """The legacy Memory Dirs panel is not on a fresh page; mount it and
    check its row meta and group badge."""
    install_default_stubs(page)
    missing = {
        **_dir(f"{_BASE}/missing", source_file_count=1, chunk_count=2),
        "exists": False,
        "held_source_file_count": 1,
        "held_chunk_count": 2,
    }
    page.route(
        "**/api/memory-dirs/status",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"dirs": [*_STATUS, missing]}),
        ),
    )
    goto_after_i18n_init(page, mm_web_url, probe_key="sources.memory_dirs.status_held")
    page.evaluate(
        """(dirs) => {
          const host = document.createElement('div');
          host.id = 'test-md-panel';
          document.body.appendChild(host);
          host.appendChild(_buildMemoryDirsPanel(dirs));
        }""",
        [_MIXED, _ALL_HELD, _OLD, f"{_BASE}/missing"],
    )
    page.wait_for_function(
        """() => document.querySelectorAll('#test-md-panel .memory-dirs-item-meta').length === 4
            && [...document.querySelectorAll('#test-md-panel .memory-dirs-item-meta')]
                 .every(e => e.textContent.trim())""",
        timeout=5_000,
    )
    metas = page.evaluate(
        """() => [...document.querySelectorAll('#test-md-panel .memory-dirs-item')].map(i => [
          i.querySelector('.memory-dirs-path, [title]')?.title || i.textContent,
          i.querySelector('.memory-dirs-item-meta').textContent,
        ])"""
    )
    by_dir = {}
    for label, meta in metas:
        for d in (_MIXED, _ALL_HELD, _OLD, f"{_BASE}/missing"):
            if d in label:
                by_dir[d] = meta
    hidden2 = _t(page, "sources.memory_dirs.status_held", {"count": 2})
    hidden1 = _t(page, "sources.memory_dirs.status_held", {"count": 1})
    mixed_counts = _t(
        page, "sources.memory_dirs.status_group", {"files": 1, "indexed": 1, "chunks": 2}
    )
    assert by_dir[_MIXED].endswith(f"{mixed_counts} · {hidden2}")
    assert by_dir[_ALL_HELD].endswith(hidden2)
    assert "held" not in by_dir[_OLD] and hidden1 not in by_dir[_OLD]
    assert by_dir[f"{_BASE}/missing"] == (
        _t(page, "sources.memory_dirs.status_missing", {}) + " · " + hidden1
    )

    badge = page.locator("#test-md-panel .memory-dirs-status-group").first.text_content()
    # Sum over the four roots: listed 1+0+1+0 of 1+0+1+0 files on disk,
    # listed chunks 2+0+3+0, hidden 2+2+0+1.
    assert badge == (
        _t(page, "sources.memory_dirs.status_group", {"files": 2, "indexed": 2, "chunks": 5})
        + " · "
        + _t(page, "sources.memory_dirs.status_held", {"count": 5})
    )
