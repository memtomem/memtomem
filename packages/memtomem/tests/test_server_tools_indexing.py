"""Focused output-contract tests for the MCP ``mem_index`` tool."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memtomem.models import IndexingStats
from memtomem.server.tools.indexing import mem_index


def _install_index_result(
    monkeypatch: pytest.MonkeyPatch,
    stats: IndexingStats,
    *,
    captured: tuple[str | None, str | None, str | None] = (None, None, None),
) -> SimpleNamespace:
    @asynccontextmanager
    async def _write_in_flight():
        yield

    index_engine = SimpleNamespace(
        index_path=AsyncMock(return_value=stats),
        _is_within_memory_dirs=lambda _path: True,
    )
    app = SimpleNamespace(
        index_engine=index_engine,
        write_in_flight=lambda: _write_in_flight(),
        # #2141: the tool drops the search result cache when the run mutated
        # something, so every stub app needs the attribute.
        search_pipeline=SimpleNamespace(invalidate_cache=MagicMock()),
    )
    monkeypatch.setattr(
        "memtomem.server.tools.indexing._get_app_initialized",
        AsyncMock(return_value=app),
    )
    monkeypatch.setattr(
        "memtomem.server.tools.indexing.capture_session_and_namespace_split",
        AsyncMock(return_value=captured),
    )
    monkeypatch.setattr("memtomem.server.tools.indexing.record_write_provenance", AsyncMock())
    monkeypatch.setattr(
        "memtomem.server.tools.indexing._check_embedding_mismatch", lambda _app: None
    )
    return app


#: A run with nothing to report, for tests that assert on the engine call
#: rather than on the rendered output.
_CLEAN_RUN = IndexingStats(
    total_files=1,
    total_chunks=2,
    indexed_chunks=2,
    skipped_chunks=0,
    deleted_chunks=0,
    duration_ms=1.0,
)


class TestMemIndexRetryableErrors:
    async def test_mixed_errors_are_classified_once_with_retry_guidance(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        permanent = "broken.md: malformed frontmatter"
        retryable = "transient.md: chunk store unavailable"
        _install_index_result(
            monkeypatch,
            IndexingStats(
                total_files=2,
                total_chunks=0,
                indexed_chunks=0,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=1.0,
                errors=(permanent, retryable),
                retryable_errors=(retryable,),
            ),
        )

        output = await mem_index(path=str(tmp_path))  # type: ignore[arg-type]

        assert f"- Errors:\n    {permanent}" in output
        assert f"- Errors (retryable):\n    {retryable}" in output
        assert output.count(retryable) == 1
        assert "Call mem_index again once the chunk store is reachable." in output

    async def test_zero_file_failure_uses_retryable_error_label(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        retryable = "transient.md: chunk store unavailable"
        _install_index_result(
            monkeypatch,
            IndexingStats(
                total_files=0,
                total_chunks=0,
                indexed_chunks=0,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=1.0,
                errors=(retryable,),
                retryable_errors=(retryable,),
            ),
        )

        output = await mem_index(path=str(tmp_path))  # type: ignore[arg-type]

        assert output.startswith(f"Error (retryable): {retryable}")
        assert output.count(retryable) == 1
        assert "Call mem_index again once the chunk store is reachable." in output


class TestMemIndexNamespaceRouting:
    """#2104: the caller's namespace and the session's travel in different
    engine slots, because only the first may move already-indexed rows."""

    async def test_a_session_namespace_goes_to_the_new_source_slot(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _install_index_result(
            monkeypatch,
            _CLEAN_RUN,
            captured=("session-1", None, "agent-runtime:planner"),
        )

        await mem_index(path=str(tmp_path))  # type: ignore[arg-type]

        kwargs = app.index_engine.index_path.await_args.kwargs
        assert kwargs["namespace"] is None
        assert kwargs["new_source_namespace"] == "agent-runtime:planner"

    async def test_a_caller_namespace_stays_explicit(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        app = _install_index_result(
            monkeypatch,
            _CLEAN_RUN,
            captured=("session-1", "pinned", "agent-runtime:planner"),
        )

        await mem_index(path=str(tmp_path), namespace="pinned")  # type: ignore[arg-type]

        kwargs = app.index_engine.index_path.await_args.kwargs
        assert kwargs["namespace"] == "pinned"
        assert kwargs["new_source_namespace"] == "agent-runtime:planner"


class TestMemIndexNamespaceAdvisory:
    """#2061: ``mem_index`` renders its result by hand, so a new
    ``IndexingStats`` field reaches this surface only if it is rendered."""

    async def test_preserved_against_rules_is_reported_with_the_remedy(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_index_result(
            monkeypatch,
            IndexingStats(
                total_files=2,
                total_chunks=4,
                indexed_chunks=4,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=1.0,
                namespaces_preserved_against_rules=2,
            ),
        )

        output = await mem_index(path=str(tmp_path))  # type: ignore[arg-type]

        assert "- Namespaces preserved: 2 file(s)" in output
        assert "mm index --reassign-namespaces" in output

    async def test_quiet_when_nothing_was_preserved_against_the_rules(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_index_result(
            monkeypatch,
            IndexingStats(
                total_files=1,
                total_chunks=1,
                indexed_chunks=1,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=1.0,
            ),
        )

        output = await mem_index(path=str(tmp_path))  # type: ignore[arg-type]

        assert "Namespaces preserved" not in output


class TestMemIndexMissingVectorAdvisory:
    """#2115: same hand-rendered surface, same rule — a field that is not
    rendered here never reaches an MCP caller."""

    async def test_missing_vectors_are_reported_with_the_cli_remedy(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_index_result(
            monkeypatch,
            IndexingStats(
                total_files=1,
                total_chunks=6,
                indexed_chunks=0,
                skipped_chunks=6,
                deleted_chunks=0,
                duration_ms=1.0,
                chunks_missing_vectors=6,
            ),
        )

        output = await mem_index(path=str(tmp_path))  # type: ignore[arg-type]

        assert "- No embedding: 6 unchanged chunk(s) have no vector" in output
        # The remedy names the CLI on purpose: a whole-tree re-embed is a
        # long shell job. Not a safety caveat — since #2104 the MCP call
        # preserves stored namespaces too (ADR-0033).
        assert "mm index --force" in output
        assert "mem_index(force=true)" not in output

    async def test_quiet_when_every_skipped_chunk_has_a_vector(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_index_result(
            monkeypatch,
            IndexingStats(
                total_files=1,
                total_chunks=9,
                indexed_chunks=0,
                skipped_chunks=9,
                deleted_chunks=0,
                duration_ms=1.0,
            ),
        )

        output = await mem_index(path=str(tmp_path))  # type: ignore[arg-type]

        assert "No embedding" not in output


class TestMemIndexDeclaredExemption:
    """#2076: ``mem_index`` has no ``force_unsafe`` parameter, so a
    frontmatter declaration is the only way a pattern-documenting note indexes
    over MCP at all. Same hand-rendered-surface rule as the advisories above —
    an unrendered field never reaches an agent."""

    async def test_exempted_files_are_named(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        note = tmp_path / "redaction-notes.md"
        _install_index_result(
            monkeypatch,
            IndexingStats(
                total_files=1,
                total_chunks=3,
                indexed_chunks=3,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=1.0,
                exempted_files=1,
                exempted_paths=(str(note),),
            ),
        )

        output = await mem_index(path=str(tmp_path))  # type: ignore[arg-type]

        assert "Declared redaction exemption: 1 file(s)" in output
        assert "redaction: documents-patterns" in output
        assert str(note) in output

    async def test_quiet_when_nothing_was_exempted(
        self, tmp_path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _install_index_result(
            monkeypatch,
            IndexingStats(
                total_files=1,
                total_chunks=2,
                indexed_chunks=2,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=1.0,
            ),
        )

        output = await mem_index(path=str(tmp_path))  # type: ignore[arg-type]

        assert "exemption" not in output


class TestMemIndexInvalidatesSearchCache:
    """#2141: the search result TTL cache is keyed on query + filters, never
    on content, so an index run that wrote something must drop it. The gate is
    the engine's ``mutated`` flag, NOT the counters — a tag-only or
    validity-only rewrite is deliberately reported as ``skipped``."""

    @pytest.mark.asyncio
    async def test_mutated_run_invalidates(self, monkeypatch, tmp_path):
        stats = IndexingStats(
            total_files=1,
            total_chunks=2,
            indexed_chunks=2,
            skipped_chunks=0,
            deleted_chunks=0,
            duration_ms=1.0,
            mutated=True,
        )
        app = _install_index_result(monkeypatch, stats)

        await mem_index(path=str(tmp_path), ctx=None)

        app.search_pipeline.invalidate_cache.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_metadata_only_run_invalidates_despite_silent_counters(
        self, monkeypatch, tmp_path
    ):
        """The #2124/#2140 shape: every chunk reported as skipped, yet the tag
        and validity columns search filters on were rewritten."""
        stats = IndexingStats(
            total_files=1,
            total_chunks=2,
            indexed_chunks=0,
            skipped_chunks=2,
            deleted_chunks=0,
            duration_ms=1.0,
            mutated=True,
        )
        app = _install_index_result(monkeypatch, stats)

        await mem_index(path=str(tmp_path), ctx=None)

        app.search_pipeline.invalidate_cache.assert_called_once_with()

    @pytest.mark.asyncio
    async def test_unmutated_run_does_not_invalidate_even_with_counters_set(
        self, monkeypatch, tmp_path
    ):
        """Pins that the gate reads the flag rather than re-deriving it from
        ``indexed_chunks + deleted_chunks``."""
        stats = IndexingStats(
            total_files=1,
            total_chunks=2,
            indexed_chunks=2,
            skipped_chunks=0,
            deleted_chunks=1,
            duration_ms=1.0,
        )
        app = _install_index_result(monkeypatch, stats)

        await mem_index(path=str(tmp_path), ctx=None)

        app.search_pipeline.invalidate_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_steady_state_run_does_not_invalidate(self, monkeypatch, tmp_path):
        stats = IndexingStats(
            total_files=1,
            total_chunks=2,
            indexed_chunks=0,
            skipped_chunks=2,
            deleted_chunks=0,
            duration_ms=1.0,
        )
        app = _install_index_result(monkeypatch, stats)

        await mem_index(path=str(tmp_path), ctx=None)

        app.search_pipeline.invalidate_cache.assert_not_called()
