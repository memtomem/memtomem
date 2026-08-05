"""Focused output-contract tests for the MCP ``mem_index`` tool."""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from memtomem.models import IndexingStats
from memtomem.server.tools.indexing import mem_index


def _install_index_result(monkeypatch: pytest.MonkeyPatch, stats: IndexingStats) -> SimpleNamespace:
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
    )
    monkeypatch.setattr(
        "memtomem.server.tools.indexing._get_app_initialized",
        AsyncMock(return_value=app),
    )
    monkeypatch.setattr(
        "memtomem.server.tools.indexing.capture_session_and_namespace",
        AsyncMock(return_value=(None, None)),
    )
    monkeypatch.setattr("memtomem.server.tools.indexing.record_write_provenance", AsyncMock())
    monkeypatch.setattr(
        "memtomem.server.tools.indexing._check_embedding_mismatch", lambda _app: None
    )
    return app


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
