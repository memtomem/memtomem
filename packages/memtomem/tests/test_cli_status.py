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
from memtomem.indexing.watcher import effective_watcher_backend
from memtomem.server.tools.status_config import (
    StatusLine,
    _shorten_status_path,
    _status_source_lines,
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
        assert "mm gc orphan-sources" in result.output

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


class TestStatusSourceRendering:
    """Source roots stay useful when provider discovery registers many dirs."""

    def test_empty_source_tier_renders_count_and_none(self) -> None:
        lines = _status_source_lines("Project sources", [], group_providers=False)

        assert [line.text for line in lines] == ["Project sources: 0 (none)"]

    def test_single_source_uses_indented_short_home_path(self) -> None:
        source = Path.home() / ".memtomem" / "memories"
        lines = _status_source_lines("User sources", [str(source)], group_providers=True)

        assert [line.text for line in lines] == [
            "User sources:    1",
            f"  - {Path('~') / '.memtomem' / 'memories'}",
        ]

    def test_repeated_provider_dirs_are_grouped_in_first_seen_order(self) -> None:
        home = Path.home()
        sources = [
            str(home / ".memtomem" / "memories"),
            str(home / ".claude" / "plans"),
            *(str(home / ".claude" / "projects" / f"project-{i}" / "memory") for i in range(55)),
            str(home / ".codex" / "memories"),
        ]

        lines = _status_source_lines("User sources", sources, group_providers=True)

        assert [line.text for line in lines] == [
            "User sources:    58",
            f"  - {Path('~') / '.memtomem' / 'memories'}",
            f"  - {Path('~') / '.claude' / 'plans'}",
            "  - Claude project memories (55 dirs)",
            f"  - {Path('~') / '.codex' / 'memories'}",
            "  … (use `mm status --json` for full paths)",
        ]

    def test_exact_cap_has_no_hint_and_cap_plus_one_reports_remainder(self) -> None:
        exact = [f"/opt/memtomem/source-{i}" for i in range(8)]
        capped = _status_source_lines("Project sources", exact, group_providers=False)
        overflow = _status_source_lines(
            "Project sources", [*exact, "/opt/memtomem/source-8"], group_providers=False
        )

        assert len(capped) == 9  # header + eight paths
        assert all("…" not in line.text for line in capped)
        assert [line.text for line in overflow[1:9]] == [f"  - {path}" for path in exact]
        assert overflow[-1].text == "  … (+1 more; use `mm status --json`)"

    def test_remainder_counts_dirs_represented_by_hidden_group(self) -> None:
        paths = [f"/opt/memtomem/custom-{i}" for i in range(8)]
        paths.extend(rf"C:\Users\alice\.claude\projects\project-{i}\memory" for i in range(3))

        lines = _status_source_lines("User sources", paths, group_providers=True)

        assert lines[-1].text == "  … (+3 more; use `mm status --json`)"

    def test_current_project_source_is_prioritized_before_cap(self) -> None:
        current_root = "/work/current"
        current_source = f"{current_root}/.memtomem/memories.local"
        project_sources = [
            *(f"/work/project-{i}/.memtomem/memories.local" for i in range(8)),
            current_source,
        ]
        data = {
            "config": {
                "storage_backend": "sqlite",
                "db_path": "/opt/mm/memtomem.db",
                "embedding": {"provider": "none", "model": None, "dimension": 0},
                "top_k": 10,
                "rrf_k": 60,
                "watcher_backend": "native",
                "memory_dirs": [],
                "project_memory_dirs": project_sources,
            },
            "runtime": {"cwd": current_root, "project_context_root": current_root},
            "index": {
                "total_chunks": 0,
                "total_sources": 0,
                "orphaned_sources": 0,
                "dense_coverage": None,
            },
            "immutable": {},
            "warnings": [],
        }

        rendered = [line.text for line in iter_status_lines(data)]
        project_header = rendered.index("Project sources: 9")

        assert rendered[project_header + 1] == f"  - {current_source}"
        assert rendered[project_header + 9] == "  … (+1 more; use `mm status --json`)"

    def test_windows_current_project_match_is_case_insensitive(self) -> None:
        current_source = r"C:\Users\Alice\project\.memtomem\memories.local"
        sources = [
            *(rf"D:\work\project-{i}\.memtomem\memories.local" for i in range(8)),
            current_source,
        ]

        lines = _status_source_lines(
            "Project sources",
            sources,
            group_providers=False,
            priority_root=r"c:\users\alice\project",
        )

        assert lines[1].text == f"  - {current_source}"
        assert lines[-1].text == "  … (+1 more; use `mm status --json`)"

    @pytest.mark.parametrize(
        ("path", "home", "expected"),
        [
            ("/Users/alice/notes", "/Users/alice", "~/notes"),
            ("/Users/alice2/notes", "/Users/alice", "/Users/alice2/notes"),
            (
                r"C:\Users\Alice\Notes",
                r"c:\users\alice",
                r"~\Notes",
            ),
            (r"D:\Notes", r"C:\Users\alice", r"D:\Notes"),
        ],
    )
    def test_home_contraction_is_boundary_and_windows_safe(
        self, path: str, home: str, expected: str
    ) -> None:
        assert _shorten_status_path(path, home=home) == expected


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
    ``collect_status_report`` + ``render_status_report``. This literal is
    the canonical text-shape regression net for the report, including the
    intentional compact source layout. Update it deliberately, never just
    to make a diff pass.
    """

    def test_full_report_matches_captured_literal(self, tmp_path: Path) -> None:
        import asyncio

        from memtomem.server.context import AppContext
        from memtomem.server.tools.status_config import format_status_report

        present = tmp_path / "present.md"
        present.write_text("hi")
        project_source = tmp_path / "project" / ".memtomem" / "memories.local"
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
                indexing={"project_memory_dirs": [project_source]},
            ),
        )

        text = asyncio.run(format_status_report(AppContext.from_components(comp)))

        # Resolve exactly like collect_status_report, then apply the same
        # presentation-only home contraction as the human report.
        cwd_display = _shorten_status_path(str(Path.cwd().resolve()))
        expected = f"""\
memtomem Status
==============
Storage:   sqlite
DB path:   {Path("/opt/mm/memtomem.db").expanduser().resolve()}
Embedding: onnx / bge-m3
Dimension: 1024
Top-K:     10
RRF k:     60
Watcher:   {effective_watcher_backend(comp.config.indexing)}

Runtime context
---------------
CWD:             {cwd_display}
Project root:    (none registered for CWD)
User sources:    1
  - {Path("~") / ".memtomem" / "memories"}
Project sources: 1
  - {project_source.resolve()}

Index stats
-----------
Total chunks:  53
Source files:  25 (2 orphaned — run `mm gc orphan-sources`)
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
    contract: stable payload keys, ``{"error": ...}`` + exit 1 on handled
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
        assert set(data) == {"config", "runtime", "index", "immutable", "warnings"}
        assert set(data["runtime"]) == {"cwd", "project_context_root"}
        assert data["runtime"]["cwd"] == str(Path.cwd().resolve())
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

    def test_json_keeps_every_absolute_source_path(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        user_sources = [tmp_path / f"user-{i}" for i in range(10)]
        project_sources = [tmp_path / f"project-{i}" for i in range(3)]
        comp = _mock_components(
            config=Mem2MemConfig(
                indexing={
                    "memory_dirs": user_sources,
                    "project_memory_dirs": project_sources,
                }
            )
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status", "--json"])

        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["config"]["memory_dirs"] == [str(path.resolve()) for path in user_sources]
        assert data["config"]["project_memory_dirs"] == [
            str(path.resolve()) for path in project_sources
        ]

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

        assert result.exit_code == 1, result.output
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

    def test_mmr_without_dense_emits_warning(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # #1619: explicitly enabled MMR with dense retrieval off silently
        # disables diversity re-ranking — mem_status must say so. Also
        # covers text rendering: the generic warning renderer needs no
        # changes for a new kind.
        comp = _mock_components(
            total_chunks=1,
            total_sources=1,
            config=Mem2MemConfig(mmr={"enabled": True}, search={"enable_dense": False}),
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        (warning,) = json.loads(result.output)["warnings"]
        assert set(warning) == {"kind", "detail", "fix"}
        assert warning["kind"] == "mmr_disabled_no_dense"
        assert "search.enable_dense=False" in warning["detail"]
        assert "mmr.enabled=False" in warning["fix"]

        text = runner.invoke(cli, ["status"])
        assert text.exit_code == 0, text.output
        assert "- kind:       mmr_disabled_no_dense" in text.output

    def test_mmr_enabled_with_dense_no_warning(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(
            total_chunks=1,
            total_sources=1,
            config=Mem2MemConfig(mmr={"enabled": True}),
        )
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))

        result = runner.invoke(cli, ["status", "--json"])

        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["warnings"] == []

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


class TestConcurrentWriters:
    """#1935: ``concurrent_server_writers`` warning from the instance registry.

    The registry seams (``_store_digest_for`` / ``_enumerate_live_instances``)
    are monkeypatched — the real registry's cross-process behavior is covered
    by ``test_instance_registry.py``; these tests pin the warning contract:
    procid grouping, scalar-only values, no paths/digests in the payload,
    fail-open silence, and the CLI/MCP-shared rendering.
    """

    @staticmethod
    def _seed(monkeypatch, instances, *, complete: bool = True, digest: str | None = "d" * 16):
        from memtomem._instance_registry import EnumerationResult
        from memtomem.server.tools import status_config

        monkeypatch.setattr(status_config, "_store_digest_for", lambda _p: digest)
        monkeypatch.setattr(
            status_config,
            "_enumerate_live_instances",
            lambda _d: EnumerationResult(tuple(instances), complete),
        )

    @staticmethod
    def _info(pid: int, ppid: int, procid: str):
        from memtomem._instance_registry import InstanceInfo

        return InstanceInfo(
            pid=pid, ppid=ppid, digest="d" * 16, procid=procid, path=Path(f"/x/{pid}")
        )

    def test_two_processes_emit_warning_with_stable_keys(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(total_chunks=1, total_sources=1)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        self._seed(monkeypatch, [self._info(100, 1, "aaaaaaaa"), self._info(200, 2, "bbbbbbbb")])

        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        (warning,) = json.loads(result.output)["warnings"]
        assert set(warning) == {"kind", "detail", "fix", "doc"}
        assert warning["kind"] == "concurrent_server_writers"
        assert "pids 100, 200" in warning["detail"]
        assert "multiple editor sessions" in warning["detail"]
        assert warning["doc"] == "docs/guides/mcp-clients.md#one-server-at-a-time"
        # Induced gates (#1929): scalar-only values (the generic text
        # renderer KeyErrors on non-embedding dicts), and no filesystem
        # paths / store digests on the wire.
        assert all(isinstance(v, str) for v in warning.values())
        assert "/" not in warning["detail"]
        assert "d" * 16 not in json.dumps(warning)

        text = runner.invoke(cli, ["status"])
        assert text.exit_code == 0, text.output
        assert "- kind:       concurrent_server_writers" in text.output

    def test_unanimous_ppid_adds_same_parent_observation(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(total_chunks=1, total_sources=1)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        self._seed(monkeypatch, [self._info(100, 7, "aaaaaaaa"), self._info(200, 7, "bbbbbbbb")])

        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        (warning,) = json.loads(result.output)["warnings"]
        assert set(warning) == {"kind", "detail", "fix", "doc", "same_parent"}
        assert warning["same_parent"] == "true"  # scalar string, not bool
        assert warning["detail"].endswith("All entries recorded the same parent PID.")
        # Observation only — the issue-title cause never appears as a
        # machine value (it would accuse legitimate multi-session users).
        assert "duplicate_client_registration" not in json.dumps(warning)

        text = runner.invoke(cli, ["status"])
        assert text.exit_code == 0, text.output
        assert "same_parent: true" in text.output

    def test_equal_pids_different_procids_are_two_processes(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # pid-namespace collision: same recorded pid, distinct procids —
        # grouping is procid-based, and the pid list keeps one entry per
        # group (repeats allowed).
        comp = _mock_components(total_chunks=1, total_sources=1)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        self._seed(monkeypatch, [self._info(123, 1, "aaaaaaaa"), self._info(123, 2, "bbbbbbbb")])

        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        (warning,) = json.loads(result.output)["warnings"]
        assert "2 live memtomem-server processes (pids 123, 123)" in warning["detail"]

    def test_one_process_two_registrations_no_warning(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Two flagged contexts in one process share a procid → one group.
        comp = _mock_components(total_chunks=1, total_sources=1)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        self._seed(monkeypatch, [self._info(100, 1, "aaaaaaaa"), self._info(100, 1, "aaaaaaaa")])

        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["warnings"] == []

    def test_single_instance_no_warning(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        comp = _mock_components(total_chunks=1, total_sources=1)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        self._seed(monkeypatch, [self._info(100, 1, "aaaaaaaa")])

        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["warnings"] == []

    def test_incomplete_enumeration_fails_open(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # complete=False is a lower bound, not evidence — never warn on it.
        comp = _mock_components(total_chunks=1, total_sources=1)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        self._seed(
            monkeypatch,
            [self._info(100, 1, "aaaaaaaa"), self._info(200, 2, "bbbbbbbb")],
            complete=False,
        )

        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["warnings"] == []

    def test_missing_store_digest_skips_probe(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memtomem.server.tools import status_config

        comp = _mock_components(total_chunks=1, total_sources=1)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr(status_config, "_store_digest_for", lambda _p: None)

        def _boom(_digest):
            raise AssertionError("enumeration must not run without a digest")

        monkeypatch.setattr(status_config, "_enumerate_live_instances", _boom)

        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        assert json.loads(result.output)["warnings"] == []

    def test_probe_failure_leaves_report_intact(
        self, runner: CliRunner, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from memtomem.server.tools import status_config

        comp = _mock_components(total_chunks=1, total_sources=1)
        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _patched_cli_components(comp))
        monkeypatch.setattr(status_config, "_store_digest_for", lambda _p: "d" * 16)

        def _boom(_digest):
            raise RuntimeError("registry exploded")

        monkeypatch.setattr(status_config, "_enumerate_live_instances", _boom)

        result = runner.invoke(cli, ["status", "--json"])
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)
        assert data["warnings"] == []
        assert data["index"]["total_chunks"] == 1
