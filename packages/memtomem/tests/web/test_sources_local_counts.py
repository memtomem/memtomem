"""Browser tests: Sources badges leave out project_local drafts too (#2567).

``GET /api/sources`` hides held or pending sources and, by default, the
``project_local`` tier. ``/api/memory-dirs/status`` reports the union once
(``hidden_*``) and each reason whole (``held_*``, ``project_local_*``); a held
draft is in both reasons. The badges subtract only the union. The note leads
with the union and names each reason; with no ``project_local`` part it is
the #2561 sentence unchanged. ``test_sources_held_counts.py`` keeps covering a
#2561 server, which sends ``held_*`` without ``hidden_*``.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from .conftest import goto_after_i18n_init, install_default_stubs

pytestmark = pytest.mark.browser

_BASE = "/tmp/mm-2567"
_MIXED = f"{_BASE}/mixed"
_ALL_LOCAL = f"{_BASE}/all-local"
_HELD_ONLY = f"{_BASE}/held-only"


def _dir(path: str, **counts: int) -> dict[str, object]:
    return {
        "path": path,
        "exists": True,
        "provider": "user",
        "category": "user",
        "kind": "general",
        "tier": "user",
        "delete_chunk_count": 0,
        **counts,
    }


# ``_MIXED``: listed a.md (2 chunks), a draft (3), a held draft (4) and a
# held file (5). Hidden = 3 sources / 12 chunks; held = 2 / 9; local = 2 / 7.
# ``_ALL_LOCAL``: two drafts (6 chunks), nothing listed.
# ``_HELD_ONLY``: a current server with no drafts, 1 listed (1) + 1 held (2).
_STATUS = [
    _dir(
        _MIXED,
        file_count=4,
        source_file_count=4,
        chunk_count=14,
        hidden_source_file_count=3,
        hidden_chunk_count=12,
        held_source_file_count=2,
        held_chunk_count=9,
        project_local_source_file_count=2,
        project_local_chunk_count=7,
    ),
    _dir(
        _ALL_LOCAL,
        file_count=2,
        source_file_count=2,
        chunk_count=6,
        hidden_source_file_count=2,
        hidden_chunk_count=6,
        held_source_file_count=0,
        held_chunk_count=0,
        project_local_source_file_count=2,
        project_local_chunk_count=6,
    ),
    _dir(
        _HELD_ONLY,
        file_count=1,
        source_file_count=2,
        chunk_count=3,
        hidden_source_file_count=1,
        hidden_chunk_count=2,
        held_source_file_count=1,
        held_chunk_count=2,
        project_local_source_file_count=0,
        project_local_chunk_count=0,
    ),
]


def _source(path: str, memory_dir: str, chunks: int) -> dict[str, object]:
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


_SOURCES = [_source(f"{_MIXED}/a.md", _MIXED, 2), _source(f"{_HELD_ONLY}/h.md", _HELD_ONLY, 1)]


def _payload(url: str) -> dict[str, object]:
    path = urlparse(url).path
    if path == "/api/system/ui-mode":
        return {"mode": "prod"}
    if path == "/api/system/model-readiness":
        return {"ready": True}
    if path == "/api/config":
        return {"indexing": {"memory_dirs": [_MIXED, _ALL_LOCAL, _HELD_ONLY]}}
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
            statsTitle: stats ? stats.title : null,
            note: note ? note.textContent : null,
            noteIsFirstLine: !!note && header.nextElementSibling === note,
            open: g.open,
            noteVisible: !!note && note.checkVisibility(),
          };
        }""",
        directory,
    )


def _t(page, key: str, params: dict[str, object] | None = None) -> str:
    return page.evaluate("([k, p]) => t(k, p)", [key, params or {}])


def _mixed_sentence(page) -> str:
    return " ".join(
        [
            _t(page, "sources.memory_dirs.status_hidden_title", {"count": 3, "chunks": 12}),
            _t(page, "sources.memory_dirs.status_hidden_held", {"count": 2}),
            _t(page, "sources.memory_dirs.status_hidden_local", {"count": 2}),
            _t(page, "sources.memory_dirs.status_hidden_overlap"),
        ]
    )


def test_badges_subtract_the_union_once_and_name_each_reason(page, mm_web_url: str) -> None:
    _open_sources_tab(page, mm_web_url)

    mixed = _group(page, _MIXED)
    assert mixed is not None
    # 4 − 3 hidden files, 14 − 12 hidden chunks: the one listed a.md. Held (2)
    # plus local (2) would subtract the held draft twice.
    assert mixed["stats"] == _t(
        page, "sources.memory_dirs.status_group", {"files": 4, "indexed": 1, "chunks": 2}
    )
    sentence = _mixed_sentence(page)
    assert mixed["note"] == sentence
    assert mixed["noteIsFirstLine"] is True
    assert mixed["statsTitle"] == sentence

    # Only drafts: no held part, no overlap line, and the group opens so the
    # explanation is on screen.
    all_local = _group(page, _ALL_LOCAL)
    assert all_local is not None
    assert all_local["stats"] == _t(
        page, "sources.memory_dirs.status_group", {"files": 2, "indexed": 0, "chunks": 0}
    )
    assert all_local["note"] == " ".join(
        [
            _t(page, "sources.memory_dirs.status_hidden_title", {"count": 2, "chunks": 6}),
            _t(page, "sources.memory_dirs.status_hidden_local", {"count": 2}),
        ]
    )
    assert all_local["open"] is True and all_local["noteVisible"] is True

    # No drafts: the #2561 sentence, byte for byte.
    held_only = _group(page, _HELD_ONLY)
    assert held_only is not None
    assert held_only["stats"] == _t(
        page, "sources.memory_dirs.status_group", {"files": 1, "indexed": 1, "chunks": 1}
    )
    assert held_only["note"] == _t(
        page, "sources.memory_dirs.status_held_title", {"count": 1, "chunks": 2}
    )


def test_memory_dirs_panel_counts_the_union(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    page.route(
        "**/api/memory-dirs/status",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps({"dirs": _STATUS})
        ),
    )
    goto_after_i18n_init(page, mm_web_url, probe_key="sources.memory_dirs.status_hidden_local")
    page.evaluate(
        """(dirs) => {
          const host = document.createElement('div');
          host.id = 'test-md-panel';
          document.body.appendChild(host);
          host.appendChild(_buildMemoryDirsPanel(dirs));
        }""",
        [_MIXED, _ALL_LOCAL, _HELD_ONLY],
    )
    page.wait_for_function(
        """() => document.querySelectorAll('#test-md-panel .memory-dirs-item-meta').length === 3
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
        for d in (_MIXED, _ALL_LOCAL, _HELD_ONLY):
            if d in label:
                by_dir[d] = meta
    hidden = lambda n: _t(page, "sources.memory_dirs.status_held", {"count": n})  # noqa: E731
    group = lambda f, i, c: _t(  # noqa: E731
        page, "sources.memory_dirs.status_group", {"files": f, "indexed": i, "chunks": c}
    )
    assert by_dir[_MIXED].endswith(f"{group(4, 1, 2)} · {hidden(3)}")
    assert by_dir[_ALL_LOCAL].endswith(f"{group(2, 0, 0)} · {hidden(2)}")
    assert by_dir[_HELD_ONLY].endswith(f"{group(1, 1, 1)} · {hidden(1)}")

    badge = page.locator("#test-md-panel .memory-dirs-status-group").first
    # Listed 1+0+1 of 4+2+1 files on disk, listed chunks 2+0+1, hidden 3+2+1.
    assert badge.text_content() == f"{group(7, 2, 3)} · {hidden(6)}"
    # The tooltip names each summed reason: held 2+0+1, local 2+2+0.
    assert badge.get_attribute("title") == " ".join(
        [
            _t(page, "sources.memory_dirs.status_hidden_title", {"count": 6, "chunks": 20}),
            _t(page, "sources.memory_dirs.status_hidden_held", {"count": 3}),
            _t(page, "sources.memory_dirs.status_hidden_local", {"count": 4}),
            _t(page, "sources.memory_dirs.status_hidden_overlap"),
        ]
    )
