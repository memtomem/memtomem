"""Storage-level tests for ``SqliteBackend.iter_chunks_for_audit``.

The CLI tests in ``test_mem_rescan.py`` use a mock storage and therefore
cannot exercise the SQL contract directly. This file pins:

- Scope filter narrows the enumeration.
- ``source_exact`` is an equality match (case-sensitive).
- ``source_prefix`` is a component-aware, **case-sensitive** descendant
  match — Codex review on #905 P2: SQLite ``LIKE`` is case-insensitive
  for ASCII by default and ``COLLATE BINARY`` does not override it, so
  the original ``LIKE '<prefix>/%'`` would have matched ``DOCS/...``
  under a ``--source docs`` filter on case-sensitive filesystems and
  reported false-positive violations.
- Mutually-exclusive contract on ``source_exact`` / ``source_prefix``.
- Streaming pagination yields every row across multiple batches.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.config import StorageConfig
from memtomem.storage.sqlite_backend import SqliteBackend
from memtomem.storage.sqlite_helpers import norm_path


@pytest.fixture
async def backend(tmp_path):
    cfg = StorageConfig(sqlite_path=tmp_path / "audit.db")
    be = SqliteBackend(
        config=cfg,
        dimension=0,
        embedding_provider="none",
        embedding_model="",
    )
    await be.initialize()
    yield be
    await be.close()


def _seed(
    backend: SqliteBackend,
    *,
    source_file: str,
    content: str = "body",
    scope: str = "user",
    project_root: str | None = None,
) -> str:
    """Insert a chunks row with the requested (already-normalized) fields.

    Returns the generated chunk id so the test can pin row identity in
    the iteration order.
    """
    chunk_id = str(uuid4())
    db = backend._get_db()
    db.execute(
        "INSERT INTO chunks ("
        "id, content, content_hash, source_file, namespace, tags, "
        "created_at, updated_at, scope, project_root"
        ") VALUES (?, ?, ?, ?, 'default', '[]', "
        "'2026-01-01T00:00:00', '2026-01-01T00:00:00', ?, ?)",
        (chunk_id, content, chunk_id, source_file, scope, project_root),
    )
    db.commit()
    return chunk_id


async def _collect(backend, **kwargs):
    rows = []
    async for r in backend.iter_chunks_for_audit(**kwargs):
        rows.append(r)
    return rows


class TestScopeFilter:
    @pytest.mark.asyncio
    async def test_only_requested_scope_is_returned(self, backend, tmp_path):
        user_path = norm_path(tmp_path / "u.md")
        shared_path = norm_path(tmp_path / "s.md")
        _seed(backend, source_file=user_path, scope="user")
        shared_id = _seed(backend, source_file=shared_path, scope="project_shared")

        rows = await _collect(backend, scope="project_shared")
        assert [r.chunk_id for r in rows] == [shared_id]
        assert rows[0].source == Path(shared_path)
        assert rows[0].scope == "project_shared"


class TestSourceExact:
    @pytest.mark.asyncio
    async def test_exact_match_is_case_sensitive(self, backend, tmp_path):
        lower = norm_path(tmp_path / "docs" / "a.md")
        upper = norm_path(tmp_path / "DOCS" / "a.md")
        lower_id = _seed(backend, source_file=lower)
        _seed(backend, source_file=upper)

        rows = await _collect(backend, scope="user", source_exact=Path(lower))
        assert [r.chunk_id for r in rows] == [lower_id]


class TestSourcePrefixCaseSensitivity:
    """Codex #905 P2 pin: ``--source docs`` must NOT match ``DOCS/...``.

    Uses **non-existent absolute fixture paths** so ``norm_path``'s
    ``Path.resolve(strict=False)`` returns the input unchanged. macOS's
    default APFS is case-insensitive — if the paths actually existed on
    disk, the FS would fold ``docs`` and ``DOCS`` to the same canonical
    form on resolve and the SQL-layer regression would be undetectable
    on that platform. The contract under test lives at the SQL layer;
    the fixture only needs the stored ``source_file`` and the filter
    prefix to differ by case after normalisation.
    """

    @pytest.mark.asyncio
    async def test_prefix_does_not_match_uppercase_sibling(self, backend):
        lower_path = "/__rescan_fixture__/docs/inside.md"
        upper_path = "/__rescan_fixture__/DOCS/inside.md"
        lower_id = _seed(backend, source_file=lower_path)
        _seed(backend, source_file=upper_path)

        rows = await _collect(
            backend,
            scope="user",
            source_prefix=Path("/__rescan_fixture__/docs"),
        )
        assert [r.chunk_id for r in rows] == [lower_id]

    @pytest.mark.asyncio
    async def test_prefix_does_not_match_sibling_with_extra_suffix(self, backend):
        """Component-aware: ``docs`` must not match ``docsuite``."""
        inside = "/__rescan_fixture__/docs/a.md"
        sibling = "/__rescan_fixture__/docsuite/b.md"
        inside_id = _seed(backend, source_file=inside)
        _seed(backend, source_file=sibling)

        rows = await _collect(
            backend,
            scope="user",
            source_prefix=Path("/__rescan_fixture__/docs"),
        )
        assert [r.chunk_id for r in rows] == [inside_id]

    @pytest.mark.asyncio
    async def test_prefix_matches_nested_descendants(self, backend):
        a = "/__rescan_fixture__/docs/a.md"
        b = "/__rescan_fixture__/docs/sub/deep/b.md"
        a_id = _seed(backend, source_file=a)
        b_id = _seed(backend, source_file=b)

        rows = await _collect(
            backend,
            scope="user",
            source_prefix=Path("/__rescan_fixture__/docs"),
        )
        assert {r.chunk_id for r in rows} == {a_id, b_id}


class TestExclusiveContract:
    @pytest.mark.asyncio
    async def test_both_filters_at_once_raises(self, backend, tmp_path):
        with pytest.raises(ValueError, match="mutually exclusive"):
            await _collect(
                backend,
                scope="user",
                source_exact=tmp_path / "a.md",
                source_prefix=tmp_path,
            )


class TestPagination:
    @pytest.mark.asyncio
    async def test_streams_all_rows_across_batches(self, backend, tmp_path):
        # ``batch_size=3`` forces the keyset cursor to roll over twice
        # before draining the seeded rows.
        for i in range(7):
            _seed(backend, source_file=norm_path(tmp_path / f"p{i}.md"))

        rows = await _collect(backend, scope="user", batch_size=3)
        assert len(rows) == 7
