"""Tests for ``mm status`` — terminal mirror of the MCP ``mem_status`` tool (#382)."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import click
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli.status_cmd import _style_status_lines
from memtomem.config import Mem2MemConfig
from memtomem.server.tools.status_config import (
    StatusLine,
    collect_status_report,
    iter_status_lines,
    render_status_report,
)


def _mock_components(
    *,
    total_chunks: int = 0,
    total_sources: int = 0,
    source_files: list[Path] | None = None,
    stored_embedding_info: dict | None = None,
    embedding_mismatch: dict | None = None,
    dense_coverage: dict | None = None,
    config: Mem2MemConfig | None = None,
) -> SimpleNamespace:
    """Build a minimal ``Components``-shaped mock for ``mm status`` tests.

    ``AppContext.from_components`` reads ``config``, ``storage``, and
    ``embedder`` off the container; ``format_status_report`` reads
    ``app.storage.get_stats()`` / ``get_all_source_files()`` plus the two
    optional ``stored_embedding_info`` / ``embedding_mismatch`` attributes.
    A ``SimpleNamespace`` covers all of that without dragging in the real
    ``Components`` dataclass (which would require building a SqliteBackend
    and an embedder).

    ``dense_coverage`` opts in to a stubbed ``get_dense_coverage`` so the
    report's coverage line is exercised. Leaving it ``None`` keeps the
    attribute off the namespace — ``hasattr`` returns False and the
    formatter skips the line, matching older storage doubles.
    """
    storage = SimpleNamespace(
        get_stats=AsyncMock(
            return_value={"total_chunks": total_chunks, "total_sources": total_sources}
        ),
        get_all_source_files=AsyncMock(return_value=list(source_files or [])),
        stored_embedding_info=stored_embedding_info,
        embedding_mismatch=embedding_mismatch,
    )
    if dense_coverage is not None:
        storage.get_dense_coverage = AsyncMock(return_value=dense_coverage)
    return SimpleNamespace(
        config=config or Mem2MemConfig(),
        storage=storage,
        embedder=SimpleNamespace(),
    )


def _patched_cli_components(comp: SimpleNamespace):
    @asynccontextmanager
    async def fake():
        yield comp

    return fake


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


class TestStatusRegistration:
    """``mm status`` is wired into the top-level CLI group."""

    def test_status_in_top_level_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "status" in result.output

    def test_status_help_describes_command(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["status", "--help"])
        assert result.exit_code == 0
        assert "indexing statistics" in result.output
        # Cross-reference to mem_status so users learn the symmetry.
        assert "mem_status" in result.output


class TestStatusOutput:
    """Happy-path rendering matches the MCP ``mem_status`` text shape."""

    def test_basic_output_renders_all_sections(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(total_chunks=42, total_sources=7)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0, result.output

        # Header + stats sections must appear so users recognize the same
        # report they get from ``mem_status``.
        assert "memtomem Status" in result.output
        assert "Index stats" in result.output
        assert "Total chunks:  42" in result.output
        assert "Source files:  7" in result.output
        assert "Immutable fields (set once at init)" in result.output

    def test_orphan_count_appended_when_files_missing(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # 3 indexed sources, only 1 present on disk → 2 orphaned.
        present = tmp_path / "present.md"
        present.write_text("hi")
        missing_a = tmp_path / "missing_a.md"
        missing_b = tmp_path / "missing_b.md"
        comp = _mock_components(
            total_chunks=3,
            total_sources=3,
            source_files=[present, missing_a, missing_b],
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0, result.output
        assert "2 orphaned" in result.output
        assert "mem_cleanup_orphans" in result.output

    def test_dense_coverage_line_emitted_full(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(
            total_chunks=42,
            total_sources=7,
            dense_coverage={"total": 42, "with_dense": 42},
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0, result.output
        assert "Dense vectors: 42/42 (100.0%)" in result.output
        # Full coverage is the happy path — no hint suffix should appear.
        assert "BM25-only" not in result.output
        assert "partial dense coverage" not in result.output

    def test_dense_coverage_line_flags_bm25_only(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The motivating failure: chunks indexed without an embedding
        # row, so dense retrieval returns nothing while BM25 still
        # works. The hint must be loud enough that users connect the
        # dots without reading code.
        comp = _mock_components(
            total_chunks=42,
            total_sources=7,
            dense_coverage={"total": 42, "with_dense": 0},
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0, result.output
        assert "Dense vectors: 0/42 (0.0%)" in result.output
        assert "BM25-only" in result.output
        assert "dense retrieval will return nothing" in result.output

    def test_dense_coverage_line_flags_partial(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(
            total_chunks=42,
            total_sources=7,
            dense_coverage={"total": 42, "with_dense": 21},
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0, result.output
        assert "Dense vectors: 21/42 (50.0%)" in result.output
        assert "partial dense coverage" in result.output

    def test_dense_coverage_line_skipped_when_method_missing(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No ``dense_coverage=`` → helper omits the method on the
        # storage namespace → formatter skips the line entirely.
        comp = _mock_components(total_chunks=42, total_sources=7)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0, result.output
        assert "Dense vectors:" not in result.output

    def test_colored_output_preserves_plain_text(self) -> None:
        # Styling and plain rendering consume the same StatusLine parts,
        # so unstyle(styled) must reproduce render_status_report exactly.
        import asyncio

        from memtomem.server.context import AppContext

        comp = _mock_components(
            total_chunks=53,
            total_sources=25,
            stored_embedding_info={"provider": "onnx", "model": "bge-m3", "dimension": 1024},
            dense_coverage={"total": 53, "with_dense": 53},
        )
        data = asyncio.run(collect_status_report(AppContext.from_components(comp)))

        styled = _style_status_lines(iter_status_lines(data))

        assert click.unstyle(styled) == render_status_report(data)
        assert "\x1b[" in styled
        assert "\x1b[36m" in styled  # cyan title/path/commands
        assert "\x1b[32m" in styled  # full dense coverage
        assert "\x1b[33m" in styled  # immutable guidance

    @pytest.mark.parametrize(
        ("with_dense", "total", "percent", "state", "hint", "ansi_color"),
        [
            (42, 42, 100.0, "full", "", "\x1b[32m"),
            (
                21,
                42,
                50.0,
                "partial",
                "  (partial dense coverage — some chunks BM25-only)",
                "\x1b[33m",
            ),
            (
                0,
                42,
                0.0,
                "none",
                "  (BM25-only — dense retrieval will return nothing)",
                "\x1b[31m",
            ),
            (0, 0, None, "empty", "", "\x1b[33m"),
        ],
    )
    def test_dense_coverage_color_thresholds(
        self,
        with_dense: int,
        total: int,
        percent: float | None,
        state: str,
        hint: str,
        ansi_color: str,
    ) -> None:
        line = StatusLine(
            "dense",
            key="Dense vectors: ",
            value=f"{with_dense}/{total}",
            suffix=f" ({percent}%){hint}" if percent is not None else "",
            meta={"state": state},
        )

        styled = _style_status_lines([line])

        assert click.unstyle(styled) == line.text
        assert ansi_color in styled

    def test_no_color_disables_status_styling(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(total_chunks=1, total_sources=1)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setenv("NO_COLOR", "1")

        # color=True forces click to keep ANSI codes in the captured
        # output, so their absence proves NO_COLOR won, not the non-tty
        # stripping CliRunner does by default.
        result = runner.invoke(cli, ["status"], color=True)

        assert result.exit_code == 0, result.output
        assert "\x1b[" not in result.output

    def test_embedding_mismatch_warning_block_emitted(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(
            embedding_mismatch={
                "stored": {"provider": "ollama", "model": "bge-m3", "dimension": 1024},
                "configured": {"provider": "ollama", "model": "nomic", "dimension": 768},
            },
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0, result.output
        # Pin the full ``Warnings`` block schema, not just `kind` / `fix` —
        # the ``mem_status`` docstring advertises ``stored`` / ``configured``
        # / ``doc`` as stable keys monitoring probes pattern-match on, so
        # silent renames or dropped fields would break uptime dashboards
        # without any test catching it.
        assert "Warnings" in result.output
        assert "kind:       embedding_dim_mismatch" in result.output
        assert "stored:     ollama/bge-m3 (1024d)" in result.output
        assert "configured: ollama/nomic (768d)" in result.output
        assert "fix:        uv run mm embedding-reset --mode apply-current" in result.output
        assert "doc:        docs/guides/configuration.md#reset-flow" in result.output


class TestStatusMcpParity:
    """``mm status`` and the MCP ``mem_status`` tool must render identical text.

    Both go through ``format_status_report`` today, but a future refactor
    that wraps ``mem_status``'s response (e.g. JSON envelope, prefix line)
    or that has the CLI ``.strip()`` the helper output would silently
    diverge the two surfaces — and the README sells them as equivalent.
    Cheap pin: invoke each path with the same mock components and compare
    the rendered string.
    """

    def test_cli_output_matches_mem_status(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Sync test on purpose: the CLI spawns its own ``asyncio.run`` inside
        # the click handler, so an ``async def`` test (asyncio AUTO mode)
        # would nest event loops and fail with ``cannot be called from a
        # running event loop``. Drive the MCP side with its own
        # ``asyncio.run`` call instead.
        import asyncio
        from types import SimpleNamespace as NS

        from memtomem.server.context import AppContext
        from memtomem.server.tools.status_config import mem_status

        comp = _mock_components(total_chunks=11, total_sources=4)

        # MCP path: build a fake ``ctx`` whose ``request_context.lifespan_context``
        # is the AppContext, then call ``mem_status`` directly. Same plumbing
        # FastMCP uses at runtime; ``ensure_initialized`` is a no-op for
        # ``from_components`` contexts (components already populated).
        mcp_ctx = NS(request_context=NS(lifespan_context=AppContext.from_components(comp)))
        mcp_text = asyncio.run(mem_status(mcp_ctx))

        # CLI path: same mock components funneled through ``cli_components``.
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        runner = CliRunner()
        cli_result = runner.invoke(cli, ["status"])
        assert cli_result.exit_code == 0, cli_result.output

        # ``click.echo`` appends a trailing newline; the MCP wrapper does not.
        assert cli_result.output.rstrip("\n") == mcp_text


class TestStatusUnconfigured:
    """Without a ``~/.memtomem/config.json`` the command should fail loudly,
    not silently bootstrap a fresh DB. ``cli_components`` raises a
    ``ClickException`` in that case; the wrapper must let it propagate."""

    def test_missing_config_yields_clickexception(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        # Point the cached module-level config path at an empty tmp dir so
        # the existence check fails deterministically.
        monkeypatch.setattr(
            "memtomem.cli._bootstrap._CONFIG_PATH", tmp_path / "no-such-config.json"
        )

        result = runner.invoke(cli, ["status"])
        assert result.exit_code != 0
        assert "not configured" in result.output
        assert "mm init" in result.output


class TestStatusTextPin:
    """Byte-level pin of the rendered report for a maxed-out fixture.

    The #1615 refactor split ``format_status_report`` into
    ``collect_status_report`` + ``render_status_report``; this literal
    pin (captured from the pre-refactor output) proves the split changed
    no bytes — and from now on it is the canonical text-shape regression
    net for the report. Update it deliberately, never to make a diff
    pass.
    """

    def test_full_report_matches_captured_literal(self, tmp_path: Path) -> None:
        import asyncio

        from memtomem.server.context import AppContext
        from memtomem.server.tools.status_config import format_status_report

        present = tmp_path / "present.md"
        present.write_text("hi")
        comp = _mock_components(
            total_chunks=53,
            total_sources=25,
            source_files=[Path("/nonexistent/a.md"), Path("/nonexistent/b.md"), present],
            stored_embedding_info={"provider": "onnx", "model": "bge-m3", "dimension": 1024},
            embedding_mismatch={
                "stored": {"provider": "ollama", "model": "bge-m3", "dimension": 1024},
                "configured": {"provider": "ollama", "model": "nomic", "dimension": 768},
            },
            dense_coverage={"total": 53, "with_dense": 21},
            config=Mem2MemConfig(
                storage={"sqlite_path": "/opt/mm/memtomem.db"},
                scheduler={"enabled": True},
            ),
        )

        text = asyncio.run(format_status_report(AppContext.from_components(comp)))

        # str(Path(...)) so the DB-path line survives Windows separators.
        expected = f"""\
memtomem Status
==============
Storage:   sqlite
DB path:   {Path("/opt/mm/memtomem.db").expanduser()}
Embedding: onnx / bge-m3
Dimension: 1024
Top-K:     10
RRF k:     60

Index stats
-----------
Total chunks:  53
Source files:  25 (2 orphaned — run mem_cleanup_orphans)
Dense vectors: 21/53 (39.6%)  (partial dense coverage — some chunks BM25-only)

Immutable fields (set once at init)
------------------------------------
embedding.provider:  none
embedding.model:     (unset)
embedding.dimension: 0
search.tokenizer:    unicode61
storage.backend:     sqlite
  -> To change: re-run `mm init` for provider/tokenizer/backend, or `mm embedding-reset` to switch embedder (re-index required).

Warnings
--------
- kind:       scheduler_watchdog_disabled
  detail:     scheduler.enabled=True but health_watchdog.enabled=False
  fix:        set health_watchdog.enabled=True (scheduler rides its tick)
- kind:       embedding_dim_mismatch
  stored:     ollama/bge-m3 (1024d)
  configured: ollama/nomic (768d)
  fix:        uv run mm embedding-reset --mode apply-current
  doc:        docs/guides/configuration.md#reset-flow"""
        assert text == expected


class TestStatusJson:
    """``mm status --format json`` / ``--json`` — CONTRIBUTING read-command
    contract: stable payload keys, ``{"error": ...}`` + exit 0 on handled
    failure, and byte-parity between the alias and the long form."""

    def test_json_payload_has_stable_keys(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(
            total_chunks=42,
            total_sources=7,
            embedding_mismatch={
                "stored": {"provider": "ollama", "model": "bge-m3", "dimension": 1024},
                "configured": {"provider": "ollama", "model": "nomic", "dimension": 768},
            },
            dense_coverage={"total": 42, "with_dense": 21},
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output

        data = json.loads(result.output)
        assert set(data) == {"config", "index", "immutable", "warnings"}
        assert data["index"]["total_chunks"] == 42
        assert data["index"]["dense_coverage"] == {
            "with_dense": 21,
            "total": 42,
            "percent": 50.0,
            "state": "partial",
        }
        # Warnings keep the stable key schema advertised by mem_status,
        # with stored/configured as structured sub-objects.
        (warning,) = data["warnings"]
        assert warning["kind"] == "embedding_dim_mismatch"
        assert warning["stored"] == {"provider": "ollama", "model": "bge-m3", "dimension": 1024}
        assert warning["configured"] == {"provider": "ollama", "model": "nomic", "dimension": 768}
        assert warning["fix"] == "uv run mm embedding-reset --mode apply-current"
        assert warning["doc"] == "docs/guides/configuration.md#reset-flow"

    def test_json_flag_matches_format_json(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(total_chunks=3, total_sources=2)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        via_flag = runner.invoke(cli, ["status", "--json"])
        via_format = runner.invoke(cli, ["status", "--format", "json"])

        assert via_flag.exit_code == 0, via_flag.output
        assert via_flag.output == via_format.output

    def test_json_output_is_never_styled(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(total_chunks=1, total_sources=1)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status", "--json"], color=True)

        assert result.exit_code == 0, result.output
        assert "\x1b[" not in result.output

    def test_json_dense_coverage_null_when_method_missing(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(total_chunks=1, total_sources=1)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["index"]["dense_coverage"] is None

    def test_json_error_shape_when_unconfigured(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(
            "memtomem.cli._bootstrap._CONFIG_PATH", tmp_path / "no-such-config.json"
        )

        result = runner.invoke(cli, ["status", "--json"])

        # Handled failure rides the JSON body, not the exit code, so
        # `mm status --json | jq` pipelines keep working.
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert set(data) == {"error"}
        assert "not configured" in data["error"]

    def test_scheduler_warning_keys_have_no_doc(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # ``doc`` is optional per the mem_status docstring — this warning
        # kind ships without one, and JSON consumers must not assume it.
        comp = _mock_components(
            total_chunks=1,
            total_sources=1,
            config=Mem2MemConfig(scheduler={"enabled": True}),
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status", "--json"])

        assert result.exit_code == 0, result.output
        (warning,) = json.loads(result.output)["warnings"]
        assert set(warning) == {"kind", "detail", "fix"}
        assert warning["kind"] == "scheduler_watchdog_disabled"

    def test_json_unexpected_error_keeps_nonzero_exit(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Only CLI-classified failures (ClickException) become the exit-0
        # {"error": ...} shape; programmer errors must stay loud so
        # scripts/CI don't read a crash as a successful status report
        # (CONTRIBUTING: "Unhandled exceptions ... should still surface
        # through Click").
        comp = _mock_components(total_chunks=1, total_sources=1)
        comp.storage.get_stats = AsyncMock(side_effect=RuntimeError("boom"))
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status", "--json"])

        assert result.exit_code != 0
        assert "boom" in result.output
