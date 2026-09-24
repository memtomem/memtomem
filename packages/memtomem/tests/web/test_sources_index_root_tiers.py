"""Browser regression tests: project and read-only roots in the Sources tree.

Before #2522 the Sources routes attributed files to ``memory_dirs`` roots only,
so everything under a read-only or project root landed in "Other
(unregistered)". The routes now return the owning root for every tier; these
specs pin what the tree does with it. Each root renders as its own group, and
``tier`` from ``/api/memory-dirs/status`` adds a pill and withholds "Remove
from memory_dirs", which only a user-tier root can honour. A read-only root
with nothing indexed yet still shows up (Discovered), and only a file no root
owns stays in the orphan bucket.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

pytestmark = pytest.mark.browser

_BASE = "/tmp/mm-2522"
_USER = f"{_BASE}/mem"
_VAULT = f"{_BASE}/vault"
_PENDING = f"{_BASE}/pending-vault"
_PROJECT = f"{_BASE}/proj/.memtomem/memories"


def _dir(path: str, tier: str, *, files: int, indexed: int, chunks: int) -> dict[str, object]:
    return {
        "path": path,
        "exists": True,
        "provider": "user",
        "category": "user",
        "kind": "general",
        "file_count": files,
        "source_file_count": indexed,
        "chunk_count": chunks,
        "tier": tier,
    }


def _source(path: str, memory_dir: str | None, scope: str = "user") -> dict[str, object]:
    return {
        "path": path,
        "chunk_count": 1,
        "last_indexed_at": "2026-09-23T00:00:00Z",
        "file_size": 64,
        "namespaces": ["default"],
        "avg_tokens": 10,
        "min_tokens": 10,
        "max_tokens": 10,
        "memory_dir": memory_dir,
        "kind": "general" if memory_dir else None,
        "target_scope": scope,
        "title": None,
        "excerpt": None,
        "ai_summary": None,
        "ai_summary_language": None,
    }


_SOURCES = [
    _source(f"{_USER}/u.md", _USER),
    _source(f"{_VAULT}/a.md", _VAULT),
    _source(f"{_PROJECT}/p.md", _PROJECT, "project_shared"),
    _source("/home/u/.memtomem/uploads/stray.md", None),
]


def _payload(url: str) -> dict[str, object]:
    path = urlparse(url).path
    if path == "/api/system/ui-mode":
        return {"mode": "prod"}
    if path == "/api/system/model-readiness":
        return {"ready": True}
    if path == "/api/config":
        return {"indexing": {"memory_dirs": [_USER, f"{_BASE}/other"]}}
    if path == "/api/memory-dirs/status":
        return {
            "dirs": [
                _dir(_USER, "user", files=1, indexed=1, chunks=1),
                _dir(f"{_BASE}/other", "user", files=0, indexed=0, chunks=0),
                _dir(_PROJECT, "project", files=1, indexed=1, chunks=1),
                _dir(_VAULT, "read_only", files=1, indexed=1, chunks=1),
                _dir(_PENDING, "read_only", files=3, indexed=0, chunks=0),
            ]
        }
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
        arg=_VAULT,
        timeout=5_000,
    )


def _group_facts(page, directory: str) -> dict[str, object]:
    return page.evaluate(
        """dir => {
          const g = document.querySelector(`#sources-list details.source-group[data-dir="${dir}"]`);
          if (!g) return null;
          const header = g.querySelector(':scope > summary');
          const pill = header.querySelector('.source-group-tier-pill');
          return {
            tier: pill ? pill.dataset.tier : null,
            remove: !!header.querySelector('.source-group-remove'),
            open: !!header.querySelector('.source-group-actions button'),
            discovered: !!g.closest('.source-vendor-discovered'),
          };
        }""",
        directory,
    )


def test_each_tier_renders_as_its_own_group(page, mm_web_url: str) -> None:
    _open_sources_tab(page, mm_web_url)

    facts = {d: _group_facts(page, d) for d in (_USER, _VAULT, _PROJECT, _PENDING)}

    assert facts == {
        _USER: {"tier": None, "remove": True, "open": True, "discovered": False},
        _VAULT: {"tier": "read_only", "remove": False, "open": True, "discovered": False},
        _PROJECT: {"tier": "project", "remove": False, "open": True, "discovered": False},
        # Configured but not yet indexed: reachable, so its Index button is too.
        _PENDING: {"tier": "read_only", "remove": False, "open": True, "discovered": True},
    }


def test_orphan_bucket_holds_only_unowned_files(page, mm_web_url: str) -> None:
    """Preservation: the bucket is keyed on ``memory_dir`` alone, so owned
    files of any tier stay out and a truly unowned file still lands in it."""
    _open_sources_tab(page, mm_web_url)

    orphan_paths = page.evaluate(
        """() => Array.from(document.querySelectorAll(
              '#sources-list details.source-vendor-orphan .source-item'))
            .map(el => el.dataset.path || el.getAttribute('title') || el.textContent)"""
    )

    assert len(orphan_paths) == 1
    assert "stray.md" in orphan_paths[0]
