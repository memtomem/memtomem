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

import os
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


_FIXTURE_ROOT = f"{os.sep}__rescan_fixture__"


def _fx(*parts: str) -> str:
    """Build a platform-native fixture path string.

    Stored ``source_file`` values come from ``norm_path`` →
    ``Path.resolve()`` which emits ``\\`` on Windows and ``/`` on POSIX.
    The production audit anchor likewise uses ``os.sep``. Building
    fixture paths with ``os.sep`` keeps the test contract aligned with
    real-world storage on every platform — Windows CI on PR #905
    surfaced what happens when the test seeded ``/``-paths but the
    production anchor used ``\\`` (no rows matched).
    """
    return _FIXTURE_ROOT + os.sep + os.sep.join(parts)


class TestSourcePrefixCaseSensitivity:
    """Codex #905 P2-a pin: ``--source docs`` must NOT match ``DOCS/...``.

    Both the seeded paths and the filter prefix bypass ``norm_path``
    (via ``monkeypatch.setattr``) and are built with ``os.sep`` so the
    test stays platform-independent:

    - macOS's default APFS is case-insensitive, so passing real paths
      under ``tmp_path`` through ``Path.resolve()`` would fold
      ``docs`` and ``DOCS`` to the same canonical form and hide the
      SQL-layer bug.
    - On Windows, ``Path.resolve()`` rewrites ``/...`` into
      ``<drive>:\\...``; without the monkey-patch the stored
      POSIX-form strings never matched the Windows-form anchor that
      production builds. Both halves go through the patch so the
      test is portable.
    """

    @pytest.mark.asyncio
    async def test_prefix_does_not_match_uppercase_sibling(self, backend, monkeypatch):
        from memtomem.storage import sqlite_backend

        prefix_value = _fx("docs")
        monkeypatch.setattr(sqlite_backend, "norm_path", lambda p: prefix_value)
        lower_id = _seed(backend, source_file=_fx("docs", "inside.md"))
        _seed(backend, source_file=_fx("DOCS", "inside.md"))

        rows = await _collect(backend, scope="user", source_prefix=Path("ignored"))
        assert [r.chunk_id for r in rows] == [lower_id]

    @pytest.mark.asyncio
    async def test_prefix_does_not_match_sibling_with_extra_suffix(self, backend, monkeypatch):
        """Component-aware: ``docs`` must not match ``docsuite``."""
        from memtomem.storage import sqlite_backend

        prefix_value = _fx("docs")
        monkeypatch.setattr(sqlite_backend, "norm_path", lambda p: prefix_value)
        inside_id = _seed(backend, source_file=_fx("docs", "a.md"))
        _seed(backend, source_file=_fx("docsuite", "b.md"))

        rows = await _collect(backend, scope="user", source_prefix=Path("ignored"))
        assert [r.chunk_id for r in rows] == [inside_id]

    @pytest.mark.asyncio
    async def test_prefix_matches_nested_descendants(self, backend, monkeypatch):
        from memtomem.storage import sqlite_backend

        prefix_value = _fx("docs")
        monkeypatch.setattr(sqlite_backend, "norm_path", lambda p: prefix_value)
        a_id = _seed(backend, source_file=_fx("docs", "a.md"))
        b_id = _seed(backend, source_file=_fx("docs", "sub", "deep", "b.md"))

        rows = await _collect(backend, scope="user", source_prefix=Path("ignored"))
        assert {r.chunk_id for r in rows} == {a_id, b_id}


class TestSourcePrefixSeparator:
    """Codex #905 P2-b pin: prefix anchor uses ``os.sep``.

    On Windows ``norm_path`` (and the indexer that wrote the stored rows)
    emits paths with ``\\``. The anchor that the audit enumerator builds
    must use the same separator or ``substr`` equality matches nothing
    and a directory-scoped audit silently reports zero scanned chunks.

    The test simulates a Windows runtime by monkey-patching ``os.sep``
    AND ``norm_path`` — ``Path.resolve()`` on the host (POSIX in CI)
    always emits ``/`` regardless of how ``os.sep`` is patched, so the
    helper that the production code calls must also be patched to
    return a Windows-shape string. Both halves of the simulation are
    required per ``feedback_pin_test_mutation_validation`` (cross-
    platform constants need both ``monkeypatch.setattr(os, ...)`` and a
    fake input shape).
    """

    @pytest.mark.asyncio
    async def test_windows_shape_prefix_matches_backslash_stored(self, backend, monkeypatch):
        import os as os_mod
        from memtomem.storage import sqlite_backend

        monkeypatch.setattr(os_mod, "sep", "\\")
        monkeypatch.setattr(
            sqlite_backend,
            "norm_path",
            lambda p: "C:\\repo\\docs",
        )

        # Stored paths use the Windows separator.
        inside_id = _seed(backend, source_file="C:\\repo\\docs\\inside.md")
        # Sibling tree must NOT match: ``docs2`` shares the ``docs``
        # bytes but is a different directory. The anchor's trailing
        # separator is what filters it out.
        _seed(backend, source_file="C:\\repo\\docs2\\sibling.md")

        # ``Path("ignored")`` flows through the monkey-patched
        # ``norm_path``, which returns the canonical Windows-shape
        # ``C:\repo\docs`` regardless of input.
        rows = await _collect(backend, scope="user", source_prefix=Path("ignored"))
        assert [r.chunk_id for r in rows] == [inside_id]

    @pytest.mark.asyncio
    async def test_mixed_separator_input_strips_both_forms(self, backend, monkeypatch):
        """A caller on Windows may pass ``--source docs/sub`` (POSIX-style
        slashes inside the value); the anchor still uses the platform sep
        regardless of which trailing form the caller stripped or didn't.
        """
        import os as os_mod
        from memtomem.storage import sqlite_backend

        monkeypatch.setattr(os_mod, "sep", "\\")
        # Caller's resolved prefix carries a trailing ``/`` even though
        # stored paths use ``\``. The rstrip("/\\") + os.sep dance must
        # collapse that into a Windows-shape anchor.
        monkeypatch.setattr(
            sqlite_backend,
            "norm_path",
            lambda p: "C:\\repo\\docs/",
        )

        inside_id = _seed(backend, source_file="C:\\repo\\docs\\inside.md")

        rows = await _collect(backend, scope="user", source_prefix=Path("ignored"))
        assert [r.chunk_id for r in rows] == [inside_id]


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
