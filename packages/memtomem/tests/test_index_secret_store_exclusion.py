"""Secret-store directories are excluded on every indexing surface, in any case.

``.aws`` and ``.gnupg`` used to be listed only in ``_EXCLUDED_DIRS``, which only
the recursive walk consults, and by exact name. ``index_file`` (the watcher and
single-file ``mem_index``) and every writer's ``is_excluded`` guard read the
built-in exclude spec instead, so a file under ``.aws/`` was indexed there in
any case, and the walk itself admitted ``.AWS`` (#2734). The spec matches
case-insensitively, as ``.ssh`` already showed.
"""

from __future__ import annotations

import pytest

STORES = [".aws", ".AWS", ".gnupg", ".GnuPG", ".ssh", ".SSH"]


@pytest.fixture
async def secret_file(bm25_only_components, request):
    comp, mem_dir = bm25_only_components
    path = mem_dir / request.param / "sso" / "notes.md"
    path.parent.mkdir(parents=True)
    path.write_text("## Session\n\nsecret-store body\n", encoding="utf-8")
    return comp, mem_dir, path


@pytest.mark.parametrize("secret_file", STORES, indirect=True)
async def test_secret_store_file_is_excluded(secret_file):
    comp, _, path = secret_file
    assert comp.index_engine.is_excluded(path)


@pytest.mark.parametrize("secret_file", STORES, indirect=True)
async def test_secret_store_file_is_not_indexed_directly(secret_file):
    comp, _, path = secret_file
    stats = await comp.index_engine.index_file(path, already_scanned=True)
    assert stats.indexed_chunks == 0
    assert await comp.storage.list_chunks_by_source(path, limit=None) == []


@pytest.mark.parametrize("secret_file", STORES, indirect=True)
async def test_secret_store_file_is_not_walked(secret_file):
    comp, mem_dir, path = secret_file
    assert path not in comp.index_engine._discover_files(mem_dir, recursive=True)
