"""Browser tests: a project root whose sources are all held gets a group (#2565).

Project roots enter the Sources tree through their ``/api/sources`` rows, so
the hidden-by-default ``memories.local`` tier stays hidden (#2522). A root
whose sources are all held has no rows, so it used to get no group and no
line saying why. The tree now also adds a project root from its
``/api/memory-dirs/status`` entry when it hides a source that is not a
``project_local`` draft (``hidden_source_file_count`` minus
``project_local_source_file_count``), so it renders like a user-tier
all-held root. A ``memories.local`` root, and an entry without those fields,
still add nothing.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from memtomem.config import memory_dir_kind

pytestmark = pytest.mark.browser

_BASE = "/tmp/mm-2565"
_USER = f"{_BASE}/user"
_SHARED = f"{_BASE}/proj/.memtomem/memories"
_LOCAL = f"{_BASE}/proj/.memtomem/memories.local"
_OLD = f"{_BASE}/old/.memtomem/memories"


def _dir(path: str, tier: str, **counts: int) -> dict[str, object]:
    return {
        "path": path,
        "exists": True,
        "provider": "user",
        "category": "user",
        # What the server reports: ``memory`` for ``.memtomem/memories``.
        "kind": memory_dir_kind(path),
        "tier": tier,
        **counts,
    }


_USER_STATUS = _dir(
    _USER,
    "user",
    file_count=1,
    source_file_count=1,
    chunk_count=2,
    hidden_source_file_count=0,
    hidden_chunk_count=0,
    held_source_file_count=0,
    held_chunk_count=0,
    project_local_source_file_count=0,
    project_local_chunk_count=0,
    delete_chunk_count=2,
)
# Every source held, none of them a draft.
_SHARED_STATUS = _dir(
    _SHARED,
    "project",
    file_count=0,
    source_file_count=2,
    chunk_count=4,
    hidden_source_file_count=2,
    hidden_chunk_count=4,
    held_source_file_count=2,
    held_chunk_count=4,
    project_local_source_file_count=0,
    project_local_chunk_count=0,
    delete_chunk_count=0,
)
# Every source is a held draft: hidden once, for both reasons.
_LOCAL_STATUS = _dir(
    _LOCAL,
    "project",
    file_count=0,
    source_file_count=2,
    chunk_count=4,
    hidden_source_file_count=2,
    hidden_chunk_count=4,
    held_source_file_count=2,
    held_chunk_count=4,
    project_local_source_file_count=2,
    project_local_chunk_count=4,
    delete_chunk_count=0,
)
# A #2561 server: held counts, no ``hidden_*`` / ``project_local_*``. The
# held sources could be drafts, so the root stays out.
_OLD_STATUS = _dir(
    _OLD,
    "project",
    file_count=0,
    source_file_count=2,
    chunk_count=4,
    held_source_file_count=2,
    held_chunk_count=4,
    delete_chunk_count=0,
)

_USER_SOURCE = {
    "path": f"{_USER}/a.md",
    "chunk_count": 2,
    "last_indexed_at": "2026-09-27T00:00:00Z",
    "file_size": 64,
    "namespaces": ["default"],
    "avg_tokens": 10,
    "min_tokens": 10,
    "max_tokens": 10,
    "memory_dir": _USER,
    "kind": "general",
    "target_scope": "user",
    "title": None,
    "excerpt": None,
    "ai_summary": None,
    "ai_summary_language": None,
}

# Folder mode needs two indexed roots in the category: the user root and the
# shared project root. Grouped mode gets only the project roots, so the
# shared one is the vendor's single indexed root.
_FIXTURES: dict[str, dict[str, object]] = {
    "folder": {
        "memory_dirs": [_USER],
        "status": [_USER_STATUS, _SHARED_STATUS, _LOCAL_STATUS, _OLD_STATUS],
        "sources": [_USER_SOURCE],
    },
    "grouped": {
        "memory_dirs": [],
        "status": [_SHARED_STATUS, _LOCAL_STATUS, _OLD_STATUS],
        "sources": [],
    },
}


def _payload(url: str, fixture: dict[str, object]) -> dict[str, object]:
    path = urlparse(url).path
    if path == "/api/system/ui-mode":
        return {"mode": "prod"}
    if path == "/api/system/model-readiness":
        return {"ready": True}
    if path == "/api/config":
        return {"indexing": {"memory_dirs": fixture["memory_dirs"]}}
    if path == "/api/memory-dirs/status":
        return {"dirs": fixture["status"]}
    if path == "/api/sources":
        sources = fixture["sources"]
        return {"sources": sources, "total": len(sources), "offset": 0, "limit": 10000}
    return {}


def _open_sources_tab(page, base_url: str, layout: str) -> None:
    fixture = _FIXTURES[layout]
    page.route(
        "**/api/**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_payload(r.request.url, fixture)),
        ),
    )
    page.goto(base_url, wait_until="domcontentloaded")
    page.locator('.tab-btn[data-tab="sources"]').click()
    page.wait_for_function(
        'dir => !!document.querySelector(`#sources-list details.source-group[data-dir="${dir}"]`)',
        arg=_SHARED,
        timeout=5_000,
    )


def _group(page, directory: str) -> dict[str, object] | None:
    return page.evaluate(
        """dir => {
          const g = document.querySelector(`#sources-list details.source-group[data-dir="${dir}"]`);
          if (!g) return null;
          const header = g.querySelector(':scope > summary');
          const note = g.querySelector(':scope > .source-group-held-note');
          return {
            note: note ? note.textContent : null,
            noteIsFirstLine: !!note && header.nextElementSibling === note,
            noteVisible: !!note && note.checkVisibility(),
            open: g.open,
            inFolderTree: !!g.closest('.source-dir-tree-node'),
          };
        }""",
        directory,
    )


def _t(page, key: str, params: dict[str, object]) -> str:
    return page.evaluate("([k, p]) => t(k, p)", [key, params])


@pytest.mark.parametrize("layout", ["folder", "grouped"])
def test_all_held_shared_project_root_gets_a_group_with_the_note(
    page, mm_web_url: str, layout: str
) -> None:
    _open_sources_tab(page, mm_web_url, layout)

    shared = _group(page, _SHARED)
    assert shared is not None
    # The fixture reaches the layout it names.
    assert shared["inFolderTree"] is (layout == "folder")
    assert shared["note"] == _t(
        page, "sources.memory_dirs.status_held_title", {"count": 2, "chunks": 4}
    )
    assert shared["noteIsFirstLine"] is True
    assert shared["open"] is True and shared["noteVisible"] is True

    # A draft root stays hidden even when every draft is held, and so does
    # an entry that cannot tell drafts from shared sources.
    assert _group(page, _LOCAL) is None
    assert _group(page, _OLD) is None


@pytest.mark.parametrize("layout", ["filter", "chunks", "size", "recent"])
def test_headerless_layouts_list_nothing_for_an_all_held_project_root(
    page, mm_web_url: str, layout: str
) -> None:
    _open_sources_tab(page, mm_web_url, "folder")
    if layout == "filter":
        page.locator("#sources-filter").fill("a.md")
    else:
        page.evaluate("(by) => { STATE.sourcesSortBy = by; loadSources(); }", layout)
    page.wait_for_function(
        """() => !document.querySelector('#sources-list details.source-group')
            && !!document.querySelector('#sources-list .source-item')""",
        timeout=5_000,
    )
    listed = page.evaluate(
        "() => [...document.querySelectorAll('#sources-list .source-item')].map(e => e.title)"
    )
    assert listed == [f"{_USER}/a.md"]
