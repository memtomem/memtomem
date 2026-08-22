"""Tests for memtomem CLI commands.

Covers command registration, help text, argument parsing, and
basic config operations with mocked components.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.config import (
    FIELD_CONSTRAINTS,
    Mem2MemConfig,
    coerce_and_validate,
    load_config_overrides,
    save_config_overrides,
)


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _mm_commands_named(text: str) -> list[tuple[str, ...]]:
    """Every ``'mm <cmd> [sub]`` path quoted in TEXT, as token tuples."""
    import re

    return [tuple(m.split()) for m in re.findall(r"'mm ((?:[a-z][a-z-]*)(?: [a-z][a-z-]*)*)", text)]


def _resolves_to_command(path: tuple[str, ...]) -> bool:
    """True when PATH walks to a real command, subcommands included.

    Checking only the first token would let 'mm mem nonexistent' pass because
    the ``mem`` group exists — the point of the guard is that the *whole*
    invocation we print is runnable.
    """
    import click

    node: click.Command = cli
    for token in path:
        if not isinstance(node, click.Group):
            return False
        child = node.commands.get(token)
        if child is None:
            return False
        node = child
    return True


# ── Top-level CLI ───────────────────────────────────────────────────────


class TestCLIGroup:
    """Test root CLI group registration and help."""

    def test_help_returns_zero(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0

    def test_help_shows_description(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--help"])
        assert "markdown-first memory infrastructure" in result.output

    def test_short_help_flag(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["-h"])
        assert result.exit_code == 0
        assert "markdown-first memory infrastructure" in result.output

    def test_version_flag_prints_package_version(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["--version"])
        assert result.exit_code == 0
        assert result.output.strip().startswith("memtomem ")

    def test_version_subcommand_matches_flag(self, runner: CliRunner) -> None:
        flag = runner.invoke(cli, ["--version"])
        sub = runner.invoke(cli, ["version"])
        assert flag.exit_code == 0
        assert sub.exit_code == 0
        assert flag.output.strip() == sub.output.strip()

    def test_registered_subcommands(self, runner: CliRunner) -> None:
        """All expected subcommands appear in help output."""
        result = runner.invoke(cli, ["--help"])
        for cmd in (
            "search",
            "add",
            "recall",
            "index",
            "config",
            "context",
            "embedding-reset",
            "reset",
            "web",
            "shell",
            "init",
            "mem",
            "pinned",
            "review",
            "version",
        ):
            assert cmd in result.output, f"'{cmd}' not found in help output"

    def test_unknown_command(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["nonexistent"])
        assert result.exit_code != 0


# ── Search command ──────────────────────────────────────────────────────


class TestSearchCLI:
    """Test search subcommand argument parsing and help."""

    def test_search_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "Search the knowledge base" in result.output

    def test_search_options_listed(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["search", "--help"])
        for opt in ("--top-k", "--source-filter", "--tag-filter", "--namespace", "--format"):
            assert opt in result.output, f"'{opt}' not found in search help"

    def test_search_missing_query(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["search"])
        assert result.exit_code != 0

    def test_search_json_reranked_carries_scale_and_model(
        self, runner: CliRunner, monkeypatch
    ) -> None:
        """#1767: on the "rerank" scale the JSON items must also carry the
        model ID — rerank score ranges are model-dependent, so the scale
        alone can't calibrate a threshold (parity with the MCP payload)."""
        import json
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock
        from uuid import uuid4

        from memtomem.models import Chunk, ChunkMetadata, SearchResult
        from memtomem.search.pipeline import RetrievalStats

        chunk = Chunk(
            content="reranked hit",
            metadata=ChunkMetadata(source_file=Path("/tmp/hit.md")),
            id=uuid4(),
            embedding=[],
        )
        results = [SearchResult(chunk=chunk, score=1.0928, rank=1, source="reranked")]
        stats = RetrievalStats(
            final_total=1, score_scale="rerank", reranker_model="test-reranker-v1"
        )

        comp = MagicMock()
        comp.search_pipeline.search = AsyncMock(return_value=(results, stats))

        @asynccontextmanager
        async def fake_components():
            yield comp

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", lambda: fake_components())

        result = runner.invoke(cli, ["search", "--format", "json", "anything"])
        assert result.exit_code == 0, result.output
        items = json.loads(result.output)
        assert items[0]["score_scale"] == "rerank"
        assert items[0]["reranker"] == "test-reranker-v1"

    def test_search_json_carries_chunk_id(self, runner: CliRunner, monkeypatch) -> None:
        """#2064: the JSON payload must carry the chunk UUID under the same
        key the MCP structured payload uses, as the canonical string form
        ``mm agent share`` parses back with ``UUID(...)``."""
        import json
        from contextlib import asynccontextmanager
        from unittest.mock import AsyncMock
        from uuid import UUID, uuid4

        from memtomem.models import Chunk, ChunkMetadata, SearchResult
        from memtomem.search.pipeline import RetrievalStats

        chunk_id = uuid4()
        chunk = Chunk(
            content="shareable hit",
            metadata=ChunkMetadata(source_file=Path("/tmp/hit.md")),
            id=chunk_id,
            embedding=[],
        )
        results = [SearchResult(chunk=chunk, score=1.0, rank=1, source="bm25")]
        stats = RetrievalStats(final_total=1)

        comp = MagicMock()
        comp.search_pipeline.search = AsyncMock(return_value=(results, stats))

        @asynccontextmanager
        async def fake_components():
            yield comp

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", lambda: fake_components())

        result = runner.invoke(cli, ["search", "--format", "json", "anything"])
        assert result.exit_code == 0, result.output
        items = json.loads(result.output)
        assert items[0]["chunk_id"] == str(chunk_id)
        # ``mm agent share`` parses the value with ``UUID(...)`` — pin the
        # round trip, not just the presence of a key.
        assert UUID(items[0]["chunk_id"]) == chunk_id


# ── Config commands ─────────────────────────────────────────────────────


class TestConfigCLI:
    """Test config show/set subcommands."""

    def test_config_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "--help"])
        assert result.exit_code == 0
        assert "View or modify" in result.output

    def test_config_show_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "show", "--help"])
        assert result.exit_code == 0
        assert "--format" in result.output

    def test_config_set_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "set", "--help"])
        assert result.exit_code == 0
        assert "KEY" in result.output
        assert "VALUE" in result.output

    @patch("memtomem.config.load_config_overrides")
    @patch("memtomem.config.Mem2MemConfig")
    def test_config_show_table(self, mock_cfg_cls, mock_load, runner: CliRunner) -> None:
        mock_cfg = MagicMock()
        mock_cfg.model_dump.return_value = {
            "search": {"default_top_k": 10},
            "embedding": {"provider": "ollama", "api_key": "sk-secret"},
        }
        mock_cfg_cls.return_value = mock_cfg

        result = runner.invoke(cli, ["config", "show"])
        assert result.exit_code == 0
        assert "[search]" in result.output
        assert "default_top_k" in result.output
        # API key should be masked
        assert "***" in result.output
        assert "sk-secret" not in result.output

    @patch("memtomem.config.load_config_overrides")
    @patch("memtomem.config.Mem2MemConfig")
    def test_config_show_json(self, mock_cfg_cls, mock_load, runner: CliRunner) -> None:
        mock_cfg = MagicMock()
        mock_cfg.model_dump.return_value = {"search": {"default_top_k": 10}}
        mock_cfg_cls.return_value = mock_cfg

        result = runner.invoke(cli, ["config", "show", "--format", "json"])
        assert result.exit_code == 0
        assert '"default_top_k": 10' in result.output

    @patch("memtomem.config.load_config_overrides")
    @patch("memtomem.config.Mem2MemConfig")
    def test_config_show_json_flag_matches_format_json(
        self, mock_cfg_cls, mock_load, runner: CliRunner
    ) -> None:
        """--json is a documented alias for --format json (CONTRIBUTING "CLI
        output convention"). Both paths must emit identical output so the
        alias can't quietly diverge."""
        mock_cfg = MagicMock()
        mock_cfg.model_dump.return_value = {"search": {"default_top_k": 10}}
        mock_cfg_cls.return_value = mock_cfg

        flag = runner.invoke(cli, ["config", "show", "--json"])
        fmt = runner.invoke(cli, ["config", "show", "--format", "json"])
        assert flag.exit_code == 0
        assert fmt.exit_code == 0
        assert flag.output == fmt.output

    def test_config_set_bad_key_format(self, runner: CliRunner) -> None:
        """Key without a dot separator is rejected."""
        result = runner.invoke(cli, ["config", "set", "noperiod", "10"])
        assert result.exit_code != 0

    def test_config_set_tokenizer_triggers_fts_rebuild(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """Changing search.tokenizer via CLI must trigger set_tokenizer + FTS rebuild.

        ``rebuild_fts`` and ``initialize`` are ``AsyncMock``, not ``MagicMock``:
        the bug in #2112 was a bare call to an ``async def``, and a plain
        ``MagicMock`` returns a value for that shape, so it certified the
        broken code. ``assert_awaited`` is the part that cannot regress.
        """
        monkeypatch.setattr("memtomem.config._override_path", lambda: tmp_path / "config.json")

        mock_storage = MagicMock()
        mock_storage.initialize = AsyncMock()
        mock_storage.rebuild_fts = AsyncMock(return_value=42)
        mock_storage.close = AsyncMock()

        with (
            patch("memtomem.storage.fts_tokenizer.set_tokenizer") as mock_set_tok,
            # ``set_tokenizer`` is mocked, so the module global never moves —
            # pin the read-back too, or the command correctly reports a
            # fallback that only the mock created.
            patch("memtomem.storage.fts_tokenizer.get_tokenizer", return_value="kiwipiepy"),
            patch("memtomem.storage.factory.create_storage", return_value=mock_storage),
        ):
            result = runner.invoke(cli, ["config", "set", "search.tokenizer", "kiwipiepy"])
            assert result.exit_code == 0, result.output

            mock_set_tok.assert_called_once_with("kiwipiepy")
            mock_storage.rebuild_fts.assert_awaited_once()
            mock_storage.close.assert_awaited_once()

        # The printed count is the awaited return value, not a coroutine repr.
        assert "FTS index rebuilt with kiwipiepy (42 chunks)." in result.output
        assert "coroutine" not in result.output

    def test_config_set_tokenizer_rebuild_uses_effective_tokenizer(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """When an env var outranks config.json, the index is built for the env value.

        Building it for the requested value would leave every subsequent search
        in the process tokenizing one way against an index built the other —
        the exact mismatch the rebuild exists to prevent (#2112).
        """
        monkeypatch.setattr("memtomem.config._override_path", lambda: tmp_path / "config.json")
        monkeypatch.setenv("MEMTOMEM_SEARCH__TOKENIZER", "unicode61")

        mock_storage = MagicMock()
        mock_storage.initialize = AsyncMock()
        mock_storage.rebuild_fts = AsyncMock(return_value=7)
        mock_storage.close = AsyncMock()

        with (
            patch("memtomem.storage.fts_tokenizer.set_tokenizer") as mock_set_tok,
            patch("memtomem.storage.factory.create_storage", return_value=mock_storage),
        ):
            result = runner.invoke(cli, ["config", "set", "search.tokenizer", "kiwipiepy"])
            assert result.exit_code == 0, result.output

            mock_set_tok.assert_called_once_with("unicode61")
            mock_storage.rebuild_fts.assert_awaited_once()

        assert "MEMTOMEM_SEARCH__TOKENIZER is set and takes precedence" in result.output
        assert "rebuilt with unicode61 (7 chunks)" in result.output
        assert "not the requested kiwipiepy" in result.output

    def test_config_set_tokenizer_reports_the_tokenizer_actually_built(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """kiwipiepy missing: the success line names the fallback, not the request.

        ``_get_kiwi`` reverts the module-global to unicode61 mid-rebuild when
        the package is absent, so announcing the requested name would be a
        second false claim on top of the one #2112 fixed.
        """
        monkeypatch.setattr("memtomem.config._override_path", lambda: tmp_path / "config.json")

        mock_storage = MagicMock()
        mock_storage.initialize = AsyncMock()
        mock_storage.rebuild_fts = AsyncMock(return_value=5)
        mock_storage.close = AsyncMock()

        with (
            patch("memtomem.storage.fts_tokenizer.set_tokenizer"),
            patch("memtomem.storage.fts_tokenizer.get_tokenizer", return_value="unicode61"),
            patch("memtomem.storage.factory.create_storage", return_value=mock_storage),
        ):
            result = runner.invoke(cli, ["config", "set", "search.tokenizer", "kiwipiepy"])
            assert result.exit_code == 0, result.output

        assert "FTS index rebuilt with unicode61 (5 chunks)" in result.output
        assert "kiwipiepy is unavailable" in result.output
        assert "rebuilt with kiwipiepy" not in result.output

    def test_config_set_tokenizer_rebuild_failure_is_reported(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """A failed rebuild must not exit 0 — the value is saved but search is stale."""
        monkeypatch.setattr("memtomem.config._override_path", lambda: tmp_path / "config.json")

        mock_storage = MagicMock()
        mock_storage.initialize = AsyncMock(side_effect=RuntimeError("db is locked"))
        mock_storage.rebuild_fts = AsyncMock(return_value=1)
        mock_storage.close = AsyncMock()

        with (
            patch("memtomem.storage.fts_tokenizer.set_tokenizer"),
            patch("memtomem.storage.factory.create_storage", return_value=mock_storage),
        ):
            result = runner.invoke(cli, ["config", "set", "search.tokenizer", "kiwipiepy"])

        assert result.exit_code != 0
        assert "FTS index rebuild failed: db is locked" in result.output
        assert "mm config set search.tokenizer kiwipiepy" in result.output
        # The write is not rolled back — say so rather than implying it was.
        assert "search.tokenizer was saved" in result.output

    def test_config_set_tokenizer_rebuilds_a_real_index(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """End-to-end against a real backend: the count is real and search still works.

        The mocked tests above pin the call shape; this one pins that the shape
        drives an actual rebuild — a `MagicMock`-only suite is what let #2112
        ship.
        """
        import asyncio

        from helpers import make_chunk
        from memtomem.config import StorageConfig
        from memtomem.storage.sqlite_backend import SqliteBackend

        db_path = tmp_path / "memtomem.db"
        monkeypatch.setattr("memtomem.config._override_path", lambda: tmp_path / "config.json")
        monkeypatch.setenv("MEMTOMEM_STORAGE__SQLITE_PATH", str(db_path))
        monkeypatch.setenv("MEMTOMEM_EMBEDDING__DIMENSION", "1024")

        async def _seed() -> None:
            backend = SqliteBackend(StorageConfig(sqlite_path=db_path), dimension=1024)
            await backend.initialize()
            try:
                await backend.upsert_chunks(
                    [
                        make_chunk(content=f"unique giraffe content {i}", source=f"g{i}.md")
                        for i in range(3)
                    ]
                )
            finally:
                await backend.close()

        asyncio.run(_seed())

        # unicode61 is the default, so this is a no-change set — the rebuild is
        # unconditional on purpose (it is also the retry path after a failure),
        # and it keeps the test off the optional kiwipiepy dependency.
        result = runner.invoke(cli, ["config", "set", "search.tokenizer", "unicode61"])
        assert result.exit_code == 0, result.output
        assert "FTS index rebuilt with unicode61 (3 chunks)." in result.output

        async def _search() -> list:
            backend = SqliteBackend(StorageConfig(sqlite_path=db_path), dimension=1024)
            await backend.initialize()
            try:
                return await backend.bm25_search("giraffe", top_k=5)
            finally:
                await backend.close()

        assert len(asyncio.run(_search())) == 3

    def test_config_set_non_tokenizer_no_fts_rebuild(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """Non-tokenizer config changes must NOT trigger FTS rebuild."""
        monkeypatch.setattr("memtomem.config._override_path", lambda: tmp_path / "config.json")

        with patch("memtomem.storage.fts_tokenizer.set_tokenizer") as mock_set_tok:
            result = runner.invoke(cli, ["config", "set", "search.default_top_k", "20"])
            assert result.exit_code == 0, result.output
            mock_set_tok.assert_not_called()

    def test_config_set_immutable_field(self, runner: CliRunner) -> None:
        """Attempting to set a non-mutable field is rejected."""
        result = runner.invoke(cli, ["config", "set", "search.nonexistent_field", "10"])
        assert result.exit_code != 0
        assert "not a mutable field" in result.output

    def test_config_set_rejection_suggests_near_miss(self, runner: CliRunner) -> None:
        """The `mm status` spelling of top-k points at the config key (#1993)."""
        result = runner.invoke(cli, ["config", "set", "search.top_k", "5"])
        assert result.exit_code != 0
        assert "did you mean 'search.default_top_k'" in result.output

    def test_config_set_rejection_lists_section_fields(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "set", "search.nonexistent_field", "10"])
        assert "Mutable fields in [search]:" in result.output
        assert "default_top_k" in result.output

    def test_config_set_rejection_points_at_config_show(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "set", "search.nonexistent_field", "10"])
        assert "mm config show" in result.output

    def test_config_set_rejection_memory_dirs_names_its_surfaces(self, runner: CliRunner) -> None:
        """memory_dirs is managed elsewhere; say where instead of listing keys."""
        result = runner.invoke(cli, ["config", "set", "indexing.memory_dirs", "/tmp/x"])
        assert result.exit_code != 0
        assert "mm init" in result.output
        assert "mm config unset indexing.memory_dirs" in result.output

    def test_config_set_rejection_only_names_real_commands(self, runner: CliRunner) -> None:
        """Guidance that names a command the CLI doesn't have is a dead end."""
        import re

        result = runner.invoke(cli, ["config", "set", "indexing.memory_dirs", "/tmp/x"])
        named = set(re.findall(r"'mm ([a-z][a-z-]*)", result.output))
        assert named, result.output
        assert named <= set(cli.commands), sorted(named - set(cli.commands))

    def test_config_set_rejection_restart_required_names_remedy(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """A real-but-restart-required key gets the path that changes it (#2062)."""
        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        result = runner.invoke(cli, ["config", "set", "embedding.provider", "onnx"])
        assert result.exit_code != 0
        assert "embedding.provider: not settable at runtime" in result.output
        assert "re-run 'mm init'" in result.output
        assert "mm embedding-reset" in result.output
        assert str(config_file) in result.output
        # The key is spelled correctly — a near-miss guess would be wrong.
        assert "did you mean" not in result.output
        assert not config_file.exists()

    def test_config_set_rejection_couples_the_embedding_triple(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """Editing only `provider` lands provider=onnx/model=""/dimension=0 (#2062).

        `mm embedding-reset` calls that tuple "in sync" because DB and config
        are equally broken, so the file-edit remedy has to say all three move
        together.
        """
        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        coupling = "provider, model and dimension must be set together"
        for key in ("embedding.provider", "embedding.model", "embedding.dimension"):
            result = runner.invoke(cli, ["config", "set", key, "x"])
            assert coupling in result.output, key

        # A field that moves on its own must not carry the coupling warning.
        result = runner.invoke(cli, ["config", "set", "embedding.max_sequence_tokens", "512"])
        assert coupling not in result.output

    def test_config_set_rejection_exact_key_beats_fuzzy_suggestion(self, runner: CliRunner) -> None:
        """embedding.api_key used to be answered with 'did you mean batch_size' (#2062)."""
        result = runner.invoke(cli, ["config", "set", "embedding.api_key", "sk-x"])
        assert result.exit_code != 0
        assert "embedding.api_key: not settable at runtime" in result.output
        assert "did you mean" not in result.output

    def test_config_set_rejection_no_wizard_key_names_no_command(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """`mm init` never writes llm.api_key, so the remedy must not name it (#2062)."""
        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        result = runner.invoke(cli, ["config", "set", "llm.api_key", "sk-x"])
        assert result.exit_code != 0
        assert "llm.api_key: not settable at runtime" in result.output
        assert f"edit {config_file} and restart" in result.output
        assert "mm init" not in result.output

    def test_config_set_rejection_project_memory_dirs_names_mem_init(
        self, runner: CliRunner
    ) -> None:
        """project_memory_dirs is written by `mm mem init`, not `mm init` (#2062)."""
        result = runner.invoke(cli, ["config", "set", "indexing.project_memory_dirs", "/tmp/x"])
        assert result.exit_code != 0
        assert "run 'mm mem init' in the project" in result.output

    def test_config_set_rejection_unknown_field_keeps_generic_message(
        self, runner: CliRunner
    ) -> None:
        """A typo is not a real field; it keeps the generic rejection (#2062)."""
        result = runner.invoke(cli, ["config", "set", "embedding.provdier", "onnx"])
        assert result.exit_code != 0
        assert "not a mutable field" in result.output
        assert "not settable at runtime" not in result.output

    def test_config_set_rejection_restart_required_names_real_commands(
        self, runner: CliRunner
    ) -> None:
        """Same real-command guard as #1993, on the restart-required branch."""
        for key in ("embedding.provider", "indexing.project_memory_dirs"):
            result = runner.invoke(cli, ["config", "set", key, "x"])
            named = _mm_commands_named(result.output)
            assert named, result.output
            for path in named:
                assert _resolves_to_command(path), path

    def test_config_set_rejection_deprecated_field_names_successor(self, runner: CliRunner) -> None:
        """rerank.top_k lives on the model but min_pool is the live knob (#2062)."""
        result = runner.invoke(cli, ["config", "set", "rerank.top_k", "50"])
        assert result.exit_code != 0
        assert "rerank.top_k: deprecated and not settable" in result.output
        assert "mm config set rerank.min_pool" in result.output
        # The file-edit remedy would pin the user to the deprecated spelling.
        assert "not settable at runtime" not in result.output

    def test_deprecated_replacements_point_at_settable_keys(self) -> None:
        """A successor that `mm config set` also rejects is a dead end."""
        from memtomem.cli.config_cmd import _DEPRECATED_REPLACEMENTS
        from memtomem.config import MUTABLE_FIELDS, Mem2MemConfig

        for key, replacement in _DEPRECATED_REPLACEMENTS.items():
            section, _, field = key.partition(".")
            assert field in getattr(
                Mem2MemConfig.model_fields[section].annotation, "model_fields", {}
            ), key
            new_section, _, new_field = replacement.partition(".")
            assert new_field in MUTABLE_FIELDS.get(new_section, set()), replacement

    def test_config_set_warns_when_env_var_owns_the_key(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """The write lands in config.json but env still wins (#2108)."""
        import json

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"search": {"default_top_k": 33}}))
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.setenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", "7")

        result = runner.invoke(cli, ["config", "set", "search.default_top_k", "44"])
        assert result.exit_code == 0, result.output
        assert "MEMTOMEM_SEARCH__DEFAULT_TOP_K is set and takes precedence" in result.output
        assert "the effective value is still 7" in result.output
        # The write itself is legitimate — it applies once the variable is gone.
        assert json.loads(config_file.read_text())["search"]["default_top_k"] == 44

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="os.environ normalises keys on Windows: two spellings cannot coexist",
    )
    def test_config_set_warns_about_a_lowercase_env_var_too(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """A lowercase spelling wins the same way, and the advice says so (#2109).

        The warning names the spelling in effect, but unsetting only that one
        hands the key to the next spelling rather than to config.json — so the
        remedy has to be stated over all of them.
        """
        import json

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"search": {"default_top_k": 33}}))
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.setenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", "11")
        monkeypatch.setenv("memtomem_search__default_top_k", "7")

        result = runner.invoke(cli, ["config", "set", "search.default_top_k", "44"])
        assert result.exit_code == 0, result.output
        assert "memtomem_search__default_top_k is set and takes precedence" in result.output
        assert "the effective value is still 7" in result.output
        assert "once no case spelling of that name is set" in result.output

    def test_config_set_reports_pin_it_pruned(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """Setting the value env already supplies drops the pin (#2108).

        The delta-only write is deliberate (PR #256 — env values must not
        drag-pin into config.json), but it removes an entry the user did not
        ask to remove, and `33 -> 33` alone would hide that.
        """
        import json

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"search": {"default_top_k": 33}}))
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.setenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", "7")

        result = runner.invoke(cli, ["config", "set", "search.default_top_k", "7"])
        assert result.exit_code == 0, result.output
        assert "config.json does not pin search.default_top_k" in result.output
        assert "it held 33" in result.output
        assert "mm config unset search.default_top_k" in result.output
        assert "default_top_k" not in json.loads(config_file.read_text()).get("search", {})

    def test_config_set_refuses_a_cross_field_invalid_write(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """An invalid combination used to persist and be reverted on every load (#2108).

        `setattr` skips the section's `model_validator(mode="after")`, so
        `max_chunk_tokens` below `min_chunk_tokens` reached config.json and was
        then dropped by each load — a pin that never applied and never said why.
        """
        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        result = runner.invoke(cli, ["config", "set", "indexing.max_chunk_tokens", "64"])
        assert result.exit_code == 1
        assert "must be <= max_chunk_tokens" in result.output
        assert "indexing.max_chunk_tokens was not saved." in result.output
        assert "Nothing written" not in result.output
        assert not config_file.exists()

    def test_refusal_claims_only_that_the_value_was_not_saved(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """Loading a legacy config migrates it, so "unchanged" was a false promise.

        The rejected command still leaves the auto_discover migration's write
        behind — the message may only speak for the requested value (#2108 review).
        """
        import json

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"indexing": {"auto_discover": True}}))
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        result = runner.invoke(cli, ["config", "set", "indexing.max_chunk_tokens", "64"])
        assert result.exit_code == 1
        # The migration may have rewritten the file; the message must not
        # speak for anything but the requested value.
        assert "config.json is unchanged" not in result.output
        assert "Nothing written" not in result.output
        assert "max_chunk_tokens" not in json.loads(config_file.read_text())["indexing"]

    def test_config_set_survives_warnings_as_errors(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """The pre-save check must not re-fire a legacy field's deprecation (#2108 review).

        `rerank.top_k` is defaulted and deprecated; validating a full dump
        re-triggers its `mode="before"` migration, which is a traceback under
        `PYTHONWARNINGS=error`.
        """
        import warnings

        monkeypatch.setattr("memtomem.config._override_path", lambda: tmp_path / "config.json")

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            result = runner.invoke(
                cli, ["config", "set", "rerank.min_pool", "30"], catch_exceptions=False
            )
        assert result.exit_code == 0, result.output
        assert "rerank.min_pool" in result.output

    def test_effect_check_does_not_write_to_config_json(self, tmp_path, monkeypatch) -> None:
        """The effect check reloads config, and a migrating reload rewrites the file (#2108).

        `auto_discover` is the migrating key: a reporting pass that let the
        legacy migration run would rewrite the very file it is reporting on.
        """
        import json

        from memtomem.cli.config_cmd import _effective_value

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"indexing": {"auto_discover": True}}))
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        before = config_file.read_bytes()

        _effective_value("indexing", "auto_discover")
        assert config_file.read_bytes() == before

    def test_config_set_pruned_note_does_not_claim_the_value_is_unchanged(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """Pruning to the default *does* change the effective value (#2108 review)."""
        import json

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"search": {"default_top_k": 33}}))
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.delenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", raising=False)

        result = runner.invoke(cli, ["config", "set", "search.default_top_k", "10"])
        assert result.exit_code == 0, result.output
        assert "search.default_top_k: 33 -> 10" in result.output
        assert "effective value is unchanged" not in result.output

    def test_config_set_reports_nothing_stored_on_a_clean_file(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """No prior pin to displace, but still nothing stored (#2108 review).

        With the env var supplying the same value, the write has no delta.
        Unsetting the variable later drops to the default, not to the value
        the caller just asked for — so the report cannot stay silent.
        """
        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.setenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", "7")

        result = runner.invoke(cli, ["config", "set", "search.default_top_k", "7"])
        assert result.exit_code == 0, result.output
        assert "config.json does not pin search.default_top_k" in result.output
        assert "it had no entry for it" in result.output
        assert "MEMTOMEM_SEARCH__DEFAULT_TOP_K" in result.output

    def test_config_set_reports_pin_pruned_by_default_match(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """Same pruning with no env var in play: setting the default drops the pin."""
        import json

        config_file = tmp_path / "config.json"
        config_file.write_text(json.dumps({"search": {"default_top_k": 33}}))
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.delenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", raising=False)

        result = runner.invoke(cli, ["config", "set", "search.default_top_k", "10"])
        assert result.exit_code == 0, result.output
        assert "config.json does not pin search.default_top_k" in result.output
        assert "it held 33" in result.output
        # No env var to blame — the note must not invent one.
        assert "MEMTOMEM_" not in result.output

    def test_config_set_stays_quiet_without_a_higher_precedence_source(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """No env var, no fragment — the plain `old -> new` line stands (#2108)."""
        import json

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.delenv("MEMTOMEM_SEARCH__DEFAULT_TOP_K", raising=False)

        result = runner.invoke(cli, ["config", "set", "search.default_top_k", "21"])
        assert result.exit_code == 0, result.output
        assert "search.default_top_k: 10 -> 21" in result.output
        assert "warning:" not in result.output
        assert "note:" not in result.output
        assert json.loads(config_file.read_text())["search"]["default_top_k"] == 21

    def test_config_set_env_warning_masks_a_secret_value(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """The warning quotes the effective value — mask it for secrets (#2108)."""
        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.setenv("MEMTOMEM_SESSION_TRACE__LANGFUSE_SECRET_KEY", "sk-from-env")

        result = runner.invoke(
            cli, ["config", "set", "session_trace.langfuse_secret_key", "sk-from-cli"]
        )
        assert result.exit_code == 0, result.output
        assert "MEMTOMEM_SESSION_TRACE__LANGFUSE_SECRET_KEY is set" in result.output
        assert "sk-from-env" not in result.output
        assert "sk-from-cli" not in result.output

    def test_command_path_guard_rejects_fake_paths(self) -> None:
        """The guard is only worth running if it fails on a fake path."""
        assert _resolves_to_command(("init",))
        assert _resolves_to_command(("mem", "init"))
        assert not _resolves_to_command(("nope",))
        assert not _resolves_to_command(("mem", "nonexistent"))
        assert not _resolves_to_command(("embedding-reset", "bogus"))

    def test_dedicated_remedies_name_real_fields_and_commands(self, runner: CliRunner) -> None:
        """The remedy table is hand-maintained; pin it to the model and the CLI."""
        from memtomem.cli.config_cmd import _DEDICATED_REMEDIES, _RESET_AFTER_CHANGE
        from memtomem.config import MUTABLE_FIELDS, Mem2MemConfig

        # Exact pin, not a superset check: only keys whose value `mm init`
        # actually prompts for belong here. base_url / storage.backend /
        # rerank.provider are hardcoded by the wizard and dimension is derived
        # from the model choice, so re-running it cannot apply a user's value.
        assert set(_DEDICATED_REMEDIES) == {
            "embedding.provider",
            "embedding.model",
            "embedding.api_key",
            "storage.sqlite_path",
            "indexing.project_memory_dirs",
            "rerank.model",
        }

        for key, remedy in _DEDICATED_REMEDIES.items():
            section, _, field = key.partition(".")
            section_field = Mem2MemConfig.model_fields.get(section)
            assert section_field is not None, key
            assert field in getattr(section_field.annotation, "model_fields", {}), key
            # A mutable key never reaches this branch — a stale entry here is dead code.
            assert field not in MUTABLE_FIELDS.get(section, set()), key
            for path in _mm_commands_named(remedy):
                assert _resolves_to_command(path), path

        # Not a subset of _DEDICATED_REMEDIES: embedding.max_sequence_tokens
        # flips the ONNX policy fingerprint but no command writes it.
        for key in _RESET_AFTER_CHANGE:
            section, _, field = key.partition(".")
            section_field = Mem2MemConfig.model_fields.get(section)
            assert section_field is not None, key
            assert field in getattr(section_field.annotation, "model_fields", {}), key
            assert field not in MUTABLE_FIELDS.get(section, set()), key

    def test_config_set_rejection_policy_field_names_reset(
        self, tmp_path, monkeypatch, runner: CliRunner
    ) -> None:
        """max_sequence_tokens has no wizard command but still strands vectors (#2062)."""
        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        result = runner.invoke(cli, ["config", "set", "embedding.max_sequence_tokens", "512"])
        assert result.exit_code != 0
        assert "embedding.max_sequence_tokens: not settable at runtime" in result.output
        assert str(config_file) in result.output
        assert "mm embedding-reset" in result.output
        assert "mm init" not in result.output

    def test_config_set_exclude_patterns_json_array_persists(
        self, tmp_path, monkeypatch, runner: CliRunner
    ):
        """End-to-end: a JSON array reaches config.json as a real list (#1993)."""
        import json

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        result = runner.invoke(
            cli, ["config", "set", "indexing.exclude_patterns", '["*node_modules*"]']
        )
        assert result.exit_code == 0, result.output

        data = json.loads(config_file.read_text())
        assert data["indexing"]["exclude_patterns"] == ["*node_modules*"]

    def test_config_set_namespace_rules_json(self, tmp_path, monkeypatch, runner: CliRunner):
        """End-to-end: `mm config set namespace.rules '[...]'` persists + reloads."""
        import json

        from memtomem.config import Mem2MemConfig, load_config_overrides

        config_file = tmp_path / "config.json"
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)

        payload = '[{"path_glob": "docs/**/*.md", "namespace": "docs"}]'
        result = runner.invoke(cli, ["config", "set", "namespace.rules", payload])
        assert result.exit_code == 0, result.output

        data = json.loads(config_file.read_text())
        assert data["namespace"]["rules"] == [{"path_glob": "docs/**/*.md", "namespace": "docs"}]

        # Round-trip via load path: raw dict survives setattr + model_validate.
        fresh = Mem2MemConfig()
        load_config_overrides(fresh)
        from memtomem.config import NamespacePolicyRule

        rule = NamespacePolicyRule.model_validate(fresh.namespace.rules[0])
        assert rule.path_glob == "docs/**/*.md"
        assert rule.namespace == "docs"

    def test_config_set_namespace_rules_rejects_malformed(self, runner: CliRunner) -> None:
        """Malformed JSON for namespace.rules surfaces as CLI error."""
        result = runner.invoke(cli, ["config", "set", "namespace.rules", "[not valid json"])
        assert result.exit_code != 0
        assert "cannot parse JSON" in result.output


# ── Config validation helpers ───────────────────────────────────────────


class TestCoerceAndValidate:
    """Test the coerce_and_validate helper directly."""

    def test_none_constraint(self) -> None:
        assert coerce_and_validate("hello", None) == "hello"

    def test_int_coercion(self) -> None:
        constraint = {"type": int, "min": 1, "max": 100}
        assert coerce_and_validate("42", constraint) == 42

    def test_int_below_min(self) -> None:
        constraint = {"type": int, "min": 1, "max": 100}
        with pytest.raises(ValueError, match=">= 1"):
            coerce_and_validate("0", constraint)

    def test_int_above_max(self) -> None:
        constraint = {"type": int, "min": 1, "max": 100}
        with pytest.raises(ValueError, match="<= 100"):
            coerce_and_validate("200", constraint)

    def test_int_not_numeric(self) -> None:
        constraint = {"type": int}
        with pytest.raises(ValueError, match="cannot convert"):
            coerce_and_validate("abc", constraint)

    def test_bool_true_variants(self) -> None:
        constraint = {"type": bool}
        for v in ("true", "1", "yes", True):
            assert coerce_and_validate(v, constraint) is True

    def test_bool_false_variants(self) -> None:
        constraint = {"type": bool}
        for v in ("false", "0", "no", False):
            assert coerce_and_validate(v, constraint) is False

    def test_bool_invalid(self) -> None:
        constraint = {"type": bool}
        with pytest.raises(ValueError, match="cannot convert"):
            coerce_and_validate("maybe", constraint)

    def test_float_coercion(self) -> None:
        constraint = {"type": float, "min": 0.0, "max": 1.0}
        assert coerce_and_validate("0.5", constraint) == 0.5

    def test_allowed_constraint(self) -> None:
        constraint = {"type": str, "allowed": {"a", "b"}}
        assert coerce_and_validate("a", constraint) == "a"
        with pytest.raises(ValueError, match="must be one of"):
            coerce_and_validate("c", constraint)

    def test_list_float_coercion_from_string(self) -> None:
        """CSV string should be coerced to list[float]."""
        constraint = {"type": list, "item_type": float, "length": 2}
        result = coerce_and_validate("1.5,0.8", constraint)
        assert result == [1.5, 0.8]

    def test_list_float_coercion_from_list(self) -> None:
        """Passing an actual list should work (Web UI path)."""
        constraint = {"type": list, "item_type": float, "length": 2}
        result = coerce_and_validate([1.5, 0.8], constraint)
        assert result == [1.5, 0.8]

    def test_list_float_wrong_length(self) -> None:
        constraint = {"type": list, "item_type": float, "length": 2}
        with pytest.raises(ValueError, match="length 2"):
            coerce_and_validate("1.0,2.0,3.0", constraint)

    def test_list_float_invalid_element(self) -> None:
        constraint = {"type": list, "item_type": float, "length": 2}
        with pytest.raises(ValueError, match="cannot convert"):
            coerce_and_validate("abc,1.0", constraint)

    def test_rrf_weights_has_constraint(self) -> None:
        """search.rrf_weights must be registered in FIELD_CONSTRAINTS."""
        assert "search.rrf_weights" in FIELD_CONSTRAINTS

    def test_rrf_weights_rejects_values_fusion_cannot_honor(self) -> None:
        """#2094: negative / non-finite / all-zero pairs are refused at
        every mutation surface (they all funnel through this constraint),
        with the field named in the error."""
        constraint = FIELD_CONSTRAINTS["search.rrf_weights"]
        for raw in ("-1,1", "nan,1", "inf,1", "0,0"):
            with pytest.raises(ValueError, match="rrf_weights"):
                coerce_and_validate(raw, constraint)
        # An int too large for float overflows inside coercion; it must
        # surface as the same ValueError shape (callers prefix the field
        # name), never OverflowError (#2094 review).
        with pytest.raises(ValueError, match="cannot convert"):
            coerce_and_validate([10**400, 1.0], constraint)
        # Booleans must not coerce to 1.0/0.0 ahead of the validator
        # (#2094 review) — JSON true/false in a numeric pair is a type
        # error on every surface that funnels through this coercer.
        with pytest.raises(ValueError, match="cannot convert"):
            coerce_and_validate([True, False], constraint)

    def test_rrf_weights_accepts_valid_pairs(self) -> None:
        constraint = FIELD_CONSTRAINTS["search.rrf_weights"]
        assert coerce_and_validate("0,1", constraint) == [0.0, 1.0]
        assert coerce_and_validate("2,3", constraint) == [2.0, 3.0]

    def test_list_str_from_json_array(self) -> None:
        """A JSON array is parsed, not stored as one literal item (#1993)."""
        constraint = FIELD_CONSTRAINTS["indexing.exclude_patterns"]
        assert coerce_and_validate('["*node_modules*"]', constraint) == ["*node_modules*"]

    def test_list_float_from_json_array(self) -> None:
        constraint = {"type": list, "item_type": float, "length": 2}
        assert coerce_and_validate("[1.5, 0.8]", constraint) == [1.5, 0.8]

    def test_list_str_glob_character_class_kept_literal(self) -> None:
        """A leading '[' that isn't JSON stays a pattern (gitignore char class)."""
        constraint = FIELD_CONSTRAINTS["indexing.exclude_patterns"]
        assert coerce_and_validate("[abc]*.log", constraint) == ["[abc]*.log"]

    def test_list_str_quoted_glob_character_class_kept_literal(self) -> None:
        """`["abc]*.log` is a pathspec char class, not botched JSON."""
        constraint = FIELD_CONSTRAINTS["indexing.exclude_patterns"]
        assert coerce_and_validate('["abc]*.log', constraint) == ['["abc]*.log']

    def test_list_str_json_wins_the_ambiguous_spelling(self) -> None:
        """`["a"]` reads as both a JSON array and a char class; JSON wins."""
        constraint = FIELD_CONSTRAINTS["indexing.exclude_patterns"]
        assert coerce_and_validate('["a"]', constraint) == ["a"]
        # ...and the escape hatch for meaning it literally.
        assert coerce_and_validate('["[\\"a\\"]"]', constraint) == ['["a"]']

    def test_list_str_malformed_json_rejected_with_both_syntaxes(self) -> None:
        constraint = FIELD_CONSTRAINTS["indexing.exclude_patterns"]
        with pytest.raises(ValueError, match="not valid JSON") as exc:
            coerce_and_validate('["*a*",]', constraint)
        assert "comma list" in str(exc.value)

    def test_list_str_json_object_item_rejected(self) -> None:
        constraint = FIELD_CONSTRAINTS["indexing.exclude_patterns"]
        with pytest.raises(ValueError, match=r"item\[0\]"):
            coerce_and_validate('[{"a": 1}]', constraint)

    # ── list[BaseModel] coercion (namespace.rules) ─────────────────

    def test_namespace_rules_from_json_string(self) -> None:
        """CLI path: `mm config set namespace.rules '[{...}]'` passes a JSON string."""
        from memtomem.config import NamespacePolicyRule

        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        raw = '[{"path_glob": "docs/**/*.md", "namespace": "docs"}]'
        result = coerce_and_validate(raw, constraint)
        assert isinstance(result, list) and len(result) == 1
        assert isinstance(result[0], NamespacePolicyRule)
        assert result[0].path_glob == "docs/**/*.md"
        assert result[0].namespace == "docs"

    def test_namespace_rules_from_list_of_dicts_pr253_regression(self) -> None:
        """Web UI path: PATCH /api/config sends a parsed list of dicts.

        Regression guard for PR #253: before this fix, ``coerce_and_validate``
        did not handle ``list[BaseModel]``, so PATCH /api/config and
        ``mm config set namespace.rules ...`` stored raw dicts in
        ``cfg.namespace.rules``. That broke ``indexing/engine.py:121`` which
        accesses ``rule.path_glob`` on each entry — AttributeError on a dict.
        This test locks in that the mutation path produces model instances.
        """
        from memtomem.config import NamespacePolicyRule

        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        payload = [
            {"path_glob": "docs/**/*.md", "namespace": "docs"},
            {"path_glob": "work/**/*.md", "namespace": "work"},
        ]
        result = coerce_and_validate(payload, constraint)
        assert len(result) == 2
        assert all(isinstance(r, NamespacePolicyRule) for r in result)
        # Critical: the exact attribute access that was failing pre-PR.
        assert result[0].path_glob == "docs/**/*.md"
        assert [r.namespace for r in result] == ["docs", "work"]

    def test_namespace_rules_passthrough_for_model_instances(self) -> None:
        """Already-validated instances survive coercion unchanged."""
        from memtomem.config import NamespacePolicyRule

        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        rule = NamespacePolicyRule(path_glob="x/**", namespace="x")
        result = coerce_and_validate([rule], constraint)
        assert result == [rule]

    def test_namespace_rules_empty_list(self) -> None:
        """Empty list is valid (matches default_factory=list)."""
        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        assert coerce_and_validate([], constraint) == []
        assert coerce_and_validate("[]", constraint) == []

    def test_namespace_rules_rejects_malformed_json(self) -> None:
        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        with pytest.raises(ValueError, match="cannot parse JSON"):
            coerce_and_validate("[not json", constraint)

    def test_namespace_rules_rejects_non_list_json(self) -> None:
        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        with pytest.raises(ValueError, match="to list"):
            coerce_and_validate('{"path_glob": "x", "namespace": "y"}', constraint)

    def test_namespace_rules_rejects_scalar_entry(self) -> None:
        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        with pytest.raises(ValueError, match="item\\[0\\]: expected dict"):
            coerce_and_validate(["just-a-string"], constraint)

    def test_namespace_rules_propagates_model_validation_error(self) -> None:
        """Pydantic validator errors (e.g. empty path_glob) surface as ValueError."""
        constraint = FIELD_CONSTRAINTS["namespace.rules"]
        with pytest.raises(ValueError, match="item\\[0\\]"):
            coerce_and_validate([{"path_glob": "", "namespace": "x"}], constraint)

    def test_field_constraints_are_well_formed(self) -> None:
        """Sanity: every declared constraint has a type and consistent bounds."""
        for key, c in FIELD_CONSTRAINTS.items():
            assert "type" in c, f"{key} missing type"
            # When both min and max are present, min must be < max
            if "min" in c and "max" in c:
                assert c["min"] < c["max"], f"{key} min >= max"


# ── save_config_overrides persistence ──────────────────────────────────


class TestSaveConfigOverrides:
    """Verify delta-only save semantics: persisted values equal cfg minus the
    comparand (defaults + env + config.d/ fragments + env-dependent factories).
    """

    @pytest.fixture
    def isolated(self, tmp_path, monkeypatch):
        """Isolate config.json, config.d/, and provider-dir discovery from the
        dev machine.

        Without isolation, ``build_comparand`` reads the developer's real
        ``~/.memtomem/config.d/`` fragments and the legacy auto_discover
        migration could pull in ``~/.claude/projects`` etc., producing
        per-machine comparand differences that mask the intended behavior.
        Provider-dir detection is stubbed to ``[]`` so tests that exercise
        ``memory_dirs`` delta semantics behave identically regardless of
        what AI tools the dev machine has installed.
        """
        config_file = tmp_path / "config.json"
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.setattr("memtomem.config._config_d_path", lambda: config_d)
        monkeypatch.setattr("memtomem.config._canonical_provider_dirs", lambda: [])
        monkeypatch.setattr(
            "memtomem.config._detect_provider_dirs",
            lambda: {"claude-memory": [], "claude-plans": [], "codex": []},
        )
        return {"config_file": config_file, "config_d": config_d, "tmp_path": tmp_path}

    # ── Load-path defensive tests (unrelated to delta semantic) ────────

    def test_memory_dirs_survives_save_load(self, isolated):
        """User-added memory_dirs (distinct from factory) must survive save→load.

        Compare via ``Path.expanduser().resolve()`` rather than raw string
        equality. Phase 1 (#836) persists home-rooted paths in ``~/...``
        portable form, so the loaded list won't match the raw input string
        verbatim when ``tmp_path`` happens to land under ``$HOME`` (true on
        Windows CI under ``C:\\Users\\runneradmin\\AppData\\Local\\Temp``).
        """
        tmp_path = isolated["tmp_path"]

        cfg = Mem2MemConfig()
        cfg.indexing.memory_dirs = [tmp_path / "a", tmp_path / "b"]
        save_config_overrides(cfg)

        fresh = Mem2MemConfig()
        load_config_overrides(fresh)

        loaded_resolved = {Path(p).expanduser().resolve() for p in fresh.indexing.memory_dirs}
        assert (tmp_path / "a").resolve() in loaded_resolved
        assert (tmp_path / "b").resolve() in loaded_resolved

    def test_invalid_value_falls_back_to_default(self, isolated):
        """Invalid values in config.json should be skipped with warning, not crash."""
        import json

        isolated["config_file"].write_text(
            json.dumps({"search": {"default_top_k": -5}})  # violates min=1
        )

        cfg = Mem2MemConfig()
        default_top_k = cfg.search.default_top_k
        load_config_overrides(cfg)

        assert cfg.search.default_top_k == default_top_k

    def test_invalid_value_does_not_block_valid_ones(self, isolated):
        """One bad field must not prevent other valid fields from loading."""
        import json

        isolated["config_file"].write_text(
            json.dumps(
                {
                    "search": {"default_top_k": -5, "rrf_k": 80},
                    "decay": {"enabled": True},
                }
            ),
            encoding="utf-8",
        )

        cfg = Mem2MemConfig()
        load_config_overrides(cfg)

        assert cfg.search.rrf_k == 80
        assert cfg.decay.enabled is True

    def test_existing_memory_dirs_not_clobbered(self, isolated):
        """Saving an unrelated mutable field must not erase pinned memory_dirs."""
        import json

        isolated["config_file"].write_text(
            json.dumps({"indexing": {"memory_dirs": ["/pre/existing"]}})
        )

        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        cfg.search.default_top_k = 42
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "/pre/existing" in [str(p) for p in data["indexing"]["memory_dirs"]]

    # ── Delta semantic (renamed from drop-default) ─────────────────────

    def test_comparand_equal_field_not_persisted(self, isolated):
        """Fields whose current value equals the comparand (default/env/fragment-
        derived) must not be written. Prevents default-flush over config.d/
        fragments — same coverage as pre-Z ``drop-default``, now generalized.
        """
        import json

        cfg = Mem2MemConfig()
        # mmr.enabled default is False — simulate a Web UI "save section"
        # that dumps the whole section without the user touching mmr.
        save_config_overrides(cfg)

        data = (
            json.loads(isolated["config_file"].read_text())
            if isolated["config_file"].exists()
            else {}
        )
        assert "mmr" not in data, (
            f"comparand-equal mmr section must not be persisted; got {data.get('mmr')!r}"
        )

    def test_existing_comparand_equal_entry_pruned(self, isolated):
        """An existing leftover entry that now matches the comparand must be
        removed on the next save, so the key stops shadowing fragments."""
        import json

        isolated["config_file"].write_text(json.dumps({"mmr": {"enabled": False}}))

        cfg = Mem2MemConfig()
        # cfg.mmr.enabled is False (matches comparand) → pruned on save.
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "mmr" not in data

    def test_non_default_value_still_persists(self, isolated):
        """Explicit values that differ from the comparand must still be written."""
        import json

        cfg = Mem2MemConfig()
        cfg.mmr.enabled = True  # default is False
        cfg.search.default_top_k = 42  # default is 10
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert data["mmr"]["enabled"] is True
        assert data["search"]["default_top_k"] == 42

    def test_section_with_only_comparand_equal_fields_dropped(self, isolated):
        """If every mutable key in a section equals the comparand, the whole
        section is omitted (no orphan ``{}`` entries)."""
        import json

        isolated["config_file"].write_text(
            json.dumps({"decay": {"enabled": False, "half_life_days": 30.0}})
        )

        cfg = Mem2MemConfig()  # all defaults = comparand
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "decay" not in data

    # ── New tests for Z design ─────────────────────────────────────────

    def test_fragment_value_not_dragged_to_config_json(self, isolated):
        """Regression guard for fragment drag-in (`project_fragment_dragin_gap.md`).

        Fragment defines ``exclude_patterns``; unrelated field save must not
        copy the fragment value into config.json. Before Z, save persisted
        the full effective value, which silently copied fragment contents
        into the REPLACE layer and froze later fragment edits.
        """
        import json

        (isolated["config_d"] / "noise.json").write_text(
            json.dumps({"indexing": {"exclude_patterns": ["*.tmp", "node_modules/"]}})
        )

        # Match web/MCP in-process flow: fragments already merged.
        cfg = Mem2MemConfig()
        from memtomem.config import load_config_d

        load_config_d(cfg)
        load_config_overrides(cfg)
        assert "*.tmp" in cfg.indexing.exclude_patterns  # fragment visible

        cfg.search.default_top_k = 42  # unrelated mutation
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "exclude_patterns" not in data.get("indexing", {}), (
            f"fragment exclude_patterns must not drag into config.json; got {data}"
        )

    def test_env_value_not_dragged(self, isolated, monkeypatch):
        """Env-sourced values must not copy into config.json on save."""
        import json

        monkeypatch.setenv("MEMTOMEM_MMR__ENABLED", "true")

        cfg = Mem2MemConfig()  # picks up env at construction
        assert cfg.mmr.enabled is True

        cfg.search.default_top_k = 42
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "mmr" not in data, (
            f"env-sourced mmr.enabled must not drag into config.json; got {data}"
        )

    def test_memory_dirs_factory_default_not_persisted(self, isolated):
        """memory_dirs == env-dependent factory output → dropped on save.

        This flips the pre-Z ``_EXTRA_PERSIST_FIELDS`` exemption: the
        factory output is now included in the comparand, so machine-A
        save doesn't pin factory-specific paths into config.json for
        migration to machine-B. Companion of
        ``test_machine_migration_requires_active_reset`` (this test locks
        the *drop* direction, the other locks the *active-reset* one).
        """
        import json

        from memtomem.config import _default_memory_dirs

        cfg = Mem2MemConfig()
        cfg.indexing.memory_dirs = _default_memory_dirs()  # matches factory
        cfg.mmr.enabled = True  # unrelated non-comparand so file is non-empty
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "memory_dirs" not in data.get("indexing", {}), (
            f"factory-default memory_dirs must be dropped on save under Z; got {data}"
        )

    def test_save_is_idempotent(self, isolated):
        """Two consecutive saves produce byte-identical output.

        Guards against order-dependency in comparand build (e.g. glob
        ordering, set iteration) or diff computation.
        """
        cfg = Mem2MemConfig()
        cfg.mmr.enabled = True
        cfg.search.default_top_k = 42

        save_config_overrides(cfg)
        first = isolated["config_file"].read_text()
        save_config_overrides(cfg)
        second = isolated["config_file"].read_text()

        assert first == second, f"idempotent save broken:\n---\n{first}\n---\n{second}"

    def test_machine_migration_requires_active_reset(self, isolated):
        """Machine-A config.json with machine-A-only paths carried to
        machine-C keeps those paths pinned until the user actively resets.

        Z doesn't auto-clean historical leftovers that don't match the
        local comparand (the REPLACE layer semantics preclude that);
        docs must tell users to run ``cfg.memory_dirs = _default_memory_dirs()``
        + save, or (future) ``mm config unset memory_dirs``.
        """
        import json
        from pathlib import Path

        from memtomem.config import _default_memory_dirs

        # Seed a config.json that pins a path not part of the local factory output.
        machine_a_dirs = [str(Path("~/.memtomem/memories").expanduser()), "/machine-a-only"]
        isolated["config_file"].write_text(
            json.dumps({"indexing": {"memory_dirs": machine_a_dirs}})
        )

        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        cfg.search.default_top_k = 99  # unrelated save
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert "/machine-a-only" in [
            str(p) for p in data.get("indexing", {}).get("memory_dirs", [])
        ], "machine-A path must stay pinned on unrelated save (doc: user must reset actively)"

        # Active reset: drops cleanly
        cfg.indexing.memory_dirs = _default_memory_dirs()
        save_config_overrides(cfg)
        data2 = json.loads(isolated["config_file"].read_text())
        assert "memory_dirs" not in data2.get("indexing", {})

    def test_comparand_build_suppresses_warnings(self, isolated, caplog):
        """build_comparand(quiet=True) must not emit WARNING-level logs
        for malformed fragments. Without this, every save on a machine
        with any malformed fragment prints the same warning repeatedly."""
        import logging

        from memtomem.config import build_comparand

        # Malformed fragment that would normally warn on each load.
        (isolated["config_d"] / "bad.json").write_text('{"unknown_section": {"foo": 1}}')

        caplog.set_level(logging.WARNING, logger="memtomem.config")
        build_comparand(quiet=True)
        assert not caplog.records, (
            f"comparand build should not emit warnings; got {[r.message for r in caplog.records]}"
        )

        # Control: quiet=False still emits (proves the suppression is targeted).
        from memtomem.config import load_config_d

        caplog.clear()
        probe = Mem2MemConfig()
        load_config_d(probe, quiet=False)
        assert any(
            "unknown_section" in r.message.lower() or "unknown" in r.message.lower()
            for r in caplog.records
        ), f"quiet=False must still emit warnings; got {[r.message for r in caplog.records]}"

    # ── Unchanged — unrelated to delta semantic ────────────────────────

    def test_legacy_repr_string_in_config_handled_gracefully(self, isolated):
        """Pre-fix installations may have serialized ``namespace.rules`` via
        ``default=str`` → raw ``repr()`` strings in config.json. Loading such
        a file on upgrade must not crash: coerce rejects the shape, load path
        logs + skips, field falls back to its default. No data loss beyond
        the already-corrupt entry.
        """
        import json

        # Case 1: whole value is a repr-ish string (not even JSON).
        isolated["config_file"].write_text(
            json.dumps(
                {"namespace": {"rules": "<NamespacePolicyRule path_glob='x' namespace='y'>"}}
            )
        )
        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        assert cfg.namespace.rules == []

        # Case 2: list of repr strings.
        isolated["config_file"].write_text(
            json.dumps({"namespace": {"rules": ["<legacy repr entry>"]}})
        )
        cfg = Mem2MemConfig()
        load_config_overrides(cfg)
        assert cfg.namespace.rules == []

    def test_save_creates_parent_directory_if_missing(self, tmp_path, monkeypatch) -> None:
        """Structural guard for the ``path.parent.mkdir`` removal in
        ``save_config_overrides``: the helper is now responsible for creating
        the config directory. Every other ``isolated`` test writes into the
        already-existing ``tmp_path``, so without this test a future
        regression (e.g. dropping the helper's ``mkdir`` too) would pass CI.
        """
        nested_dir = tmp_path / "brand" / "new" / ".memtomem"
        config_file = nested_dir / "config.json"
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.setattr("memtomem.config._config_d_path", lambda: config_d)

        assert not nested_dir.exists()

        cfg = Mem2MemConfig()
        cfg.mmr.enabled = True  # force a non-comparand write
        save_config_overrides(cfg)

        assert config_file.exists()
        assert nested_dir.is_dir()

    def test_save_atomic_on_replace_failure(self, isolated, monkeypatch) -> None:
        """``save_config_overrides`` now writes via ``_atomic_write_json``.
        If ``os.replace`` fails mid-write, the existing ``config.json`` must
        stay byte-identical and no ``.config.*.tmp`` orphan should linger.
        Failure-mode complement to the happy-path coverage above.
        """
        import json as _json
        import os as _os

        original = _json.dumps({"mmr": {"enabled": True}}, indent=2)
        isolated["config_file"].write_text(original, encoding="utf-8")

        def fail_replace(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(_os, "replace", fail_replace)

        cfg = Mem2MemConfig()
        cfg.search.default_top_k = 42  # force a non-comparand field to trigger a write

        with pytest.raises(OSError, match="simulated replace failure"):
            save_config_overrides(cfg)

        assert isolated["config_file"].read_text(encoding="utf-8") == original
        orphans = [
            p
            for p in isolated["tmp_path"].iterdir()
            if p.name.startswith(".config.") and p.name.endswith(".tmp")
        ]
        assert not orphans, f"orphan tmp file(s) after failed atomic write: {orphans}"

    def test_namespace_rules_round_trip(self, isolated):
        """list[NamespacePolicyRule] survives save→load via model_dump/validate."""
        import json

        from memtomem.config import NamespacePolicyRule

        cfg = Mem2MemConfig()
        cfg.namespace.rules = [
            NamespacePolicyRule(path_glob="docs/**/*.md", namespace="docs"),
            NamespacePolicyRule(path_glob="work/**/*.md", namespace="work"),
        ]
        save_config_overrides(cfg)

        data = json.loads(isolated["config_file"].read_text())
        assert data["namespace"]["rules"] == [
            {"path_glob": "docs/**/*.md", "namespace": "docs"},
            {"path_glob": "work/**/*.md", "namespace": "work"},
        ]

        fresh = Mem2MemConfig()
        load_config_overrides(fresh)
        assert all(isinstance(r, NamespacePolicyRule) for r in fresh.namespace.rules)
        assert fresh.namespace.rules == cfg.namespace.rules


# ── Config unset ────────────────────────────────────────────────────────


class TestConfigUnset:
    """Output matrix + idempotence + atomic write + fragment-reappearance
    regression coverage for ``mm config unset``.
    """

    @pytest.fixture
    def isolated(self, tmp_path, monkeypatch):
        config_file = tmp_path / "config.json"
        config_d = tmp_path / "config.d"
        config_d.mkdir()
        monkeypatch.setattr("memtomem.config._override_path", lambda: config_file)
        monkeypatch.setattr("memtomem.config._config_d_path", lambda: config_d)
        return {"config_file": config_file, "config_d": config_d, "tmp_path": tmp_path}

    def test_unset_removes_pinned_key(self, isolated, runner: CliRunner) -> None:
        import json as _json

        isolated["config_file"].write_text(
            _json.dumps({"mmr": {"enabled": True, "lambda_param": 0.5}})
        )

        result = runner.invoke(cli, ["config", "unset", "mmr.enabled"])
        assert result.exit_code == 0, result.output
        assert "Removed: mmr.enabled" in result.output

        data = _json.loads(isolated["config_file"].read_text())
        assert "enabled" not in data["mmr"]
        assert data["mmr"]["lambda_param"] == 0.5

    def test_unset_removes_empty_section(self, isolated, runner: CliRunner) -> None:
        import json as _json

        isolated["config_file"].write_text(
            _json.dumps({"mmr": {"enabled": True}, "search": {"default_top_k": 42}})
        )

        result = runner.invoke(cli, ["config", "unset", "mmr.enabled"])
        assert result.exit_code == 0, result.output

        data = _json.loads(isolated["config_file"].read_text())
        assert "mmr" not in data
        assert data["search"]["default_top_k"] == 42

    def test_unset_deletes_empty_config_file(self, isolated, runner: CliRunner) -> None:
        import json as _json

        isolated["config_file"].write_text(_json.dumps({"mmr": {"enabled": True}}))

        result = runner.invoke(cli, ["config", "unset", "mmr.enabled"])
        assert result.exit_code == 0, result.output
        assert not isolated["config_file"].exists()
        assert "config.json now empty, file removed." in result.output

    def test_unset_extra_mutation_field_allowed(self, isolated, runner: CliRunner) -> None:
        """memory_dirs is not in MUTABLE_FIELDS but IS valid for unset."""
        import json as _json

        isolated["config_file"].write_text(
            _json.dumps({"indexing": {"memory_dirs": ["/machine-a-only"]}})
        )

        result = runner.invoke(cli, ["config", "unset", "indexing.memory_dirs"])
        assert result.exit_code == 0, result.output
        assert "Removed: indexing.memory_dirs" in result.output

    def test_unset_memory_dirs_emits_domain_warning(self, isolated, runner: CliRunner) -> None:
        import json as _json

        isolated["config_file"].write_text(
            _json.dumps({"indexing": {"memory_dirs": ["/machine-a-only"]}})
        )

        result = runner.invoke(cli, ["config", "unset", "indexing.memory_dirs"])
        assert result.exit_code == 0, result.output
        assert "mm status" in result.output
        assert "mm index" in result.output

    def test_unset_typo_suggests_similar_canonical_key(self, isolated, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "unset", "mmr.enabld"])
        assert result.exit_code == 1
        assert "Skipped mmr.enabld" in result.output
        assert "did you mean 'mmr.enabled'" in result.output

    def test_unset_ambiguous_fragment_makes_no_suggestion(
        self, isolated, runner: CliRunner
    ) -> None:
        """min_/max_/target_chunk_tokens all contain 'chunk_tokens' — don't pick one."""
        from memtomem.cli.config_cmd import _canonical_unset_keys, _suggest_key

        canonical = _canonical_unset_keys()
        assert _suggest_key("indexing.chunk_tokens", canonical) is None

    def test_unset_unique_fragment_still_suggests(self, isolated, runner: CliRunner) -> None:
        """Declining ambiguous matches must not silence the unambiguous ones."""
        from memtomem.cli.config_cmd import _canonical_unset_keys, _suggest_key

        canonical = _canonical_unset_keys()
        assert _suggest_key("search.top_k", canonical) == "search.default_top_k"

    def test_unset_unknown_key_without_suggestion(self, isolated, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["config", "unset", "completely_unrelated_xyz"])
        assert result.exit_code == 1
        assert "Skipped completely_unrelated_xyz" in result.output
        assert "did you mean" not in result.output

    def test_unset_multiple_keys_best_effort(self, isolated, runner: CliRunner) -> None:
        import json as _json

        isolated["config_file"].write_text(
            _json.dumps({"mmr": {"enabled": True}, "search": {"default_top_k": 42}})
        )

        result = runner.invoke(cli, ["config", "unset", "mmr.enabled", "foo.bar"])
        assert result.exit_code == 1
        assert "Removed: mmr.enabled" in result.output
        assert "Skipped foo.bar" in result.output

        data = _json.loads(isolated["config_file"].read_text())
        assert "mmr" not in data
        assert data["search"]["default_top_k"] == 42

    def test_unset_canonical_already_unset_is_idempotent_success(
        self, isolated, runner: CliRunner
    ) -> None:
        """Canonical key not pinned → exit 0 + ``(already at default)``."""
        # config.json doesn't exist — simulating a fresh install.
        result = runner.invoke(cli, ["config", "unset", "mmr.enabled"])
        assert result.exit_code == 0, result.output
        assert "already at default" in result.output
        assert not isolated["config_file"].exists()

    def test_unset_on_malformed_config_reports_error(self, isolated, runner: CliRunner) -> None:
        isolated["config_file"].write_text("{not valid json")

        result = runner.invoke(cli, ["config", "unset", "mmr.enabled"])
        assert result.exit_code == 1
        assert "malformed" in result.output.lower()
        assert "mm init --fresh" in result.output

    def test_unset_fragment_value_reappears(self, isolated, runner: CliRunner) -> None:
        """End-to-end: fragment mmr.enabled=true shadowed by config.json=false;
        after unset, fragment layer wins on reload."""
        import json as _json

        (isolated["config_d"] / "noise.json").write_text(_json.dumps({"mmr": {"enabled": True}}))
        isolated["config_file"].write_text(_json.dumps({"mmr": {"enabled": False}}))

        # Confirm the shadowing baseline before unset.
        from memtomem.config import load_config_d

        baseline = Mem2MemConfig()
        load_config_d(baseline)
        load_config_overrides(baseline)
        assert baseline.mmr.enabled is False

        result = runner.invoke(cli, ["config", "unset", "mmr.enabled"])
        assert result.exit_code == 0, result.output

        fresh = Mem2MemConfig()
        load_config_d(fresh)
        load_config_overrides(fresh)
        assert fresh.mmr.enabled is True

    def test_atomic_write_preserves_original_on_failure(self, tmp_path, monkeypatch):
        import os as _os

        from memtomem.config import _atomic_write_json

        path = tmp_path / "config.json"
        original = '{"original": true}'
        path.write_text(original)

        def fail_replace(*args, **kwargs):
            raise OSError("simulated replace failure")

        monkeypatch.setattr(_os, "replace", fail_replace)

        with pytest.raises(OSError, match="simulated replace failure"):
            _atomic_write_json(path, {"new": True})

        assert path.read_text() == original
        orphans = [
            p
            for p in tmp_path.iterdir()
            if p.name.startswith(".config.") and p.name.endswith(".tmp")
        ]
        assert not orphans, f"orphan tmp file(s) left behind: {orphans}"

    def test_atomic_write_cleans_up_tmp_on_success(self, tmp_path) -> None:
        import json as _json

        from memtomem.config import _atomic_write_json

        path = tmp_path / "config.json"
        _atomic_write_json(path, {"ok": True})

        assert path.exists()
        assert _json.loads(path.read_text()) == {"ok": True}
        orphans = [
            p
            for p in tmp_path.iterdir()
            if p.name.startswith(".config.") and p.name.endswith(".tmp")
        ]
        assert not orphans


# ── Other subcommands (help text) ───────────────────────────────────────


class TestSubcommandHelp:
    """Verify help text is reachable for remaining subcommands."""

    def test_init_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["init", "--help"])
        assert result.exit_code == 0
        assert "wizard" in result.output.lower()

    def test_index_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["index", "--help"])
        assert result.exit_code == 0
        assert "--recursive" in result.output
        assert "--force" in result.output

    def test_add_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["add", "--help"])
        assert result.exit_code == 0
        assert "--title" in result.output
        assert "--tags" in result.output

    def test_recall_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["recall", "--help"])
        assert result.exit_code == 0
        assert "--since" in result.output
        assert "--until" in result.output
        assert "--format" in result.output

    def test_recall_until_partial_date_is_inclusive_of_the_period(
        self, runner: CliRunner, monkeypatch
    ) -> None:
        """``--until 2025-03`` must cover all of March (exclusive upper bound
        2025-04-01), matching MCP ``mem_recall`` and the flag's own help text —
        without ``end_of_period=True`` it parsed to 2025-03-01 and
        ``--since 2025-03 --until 2025-03`` returned zero rows."""
        from contextlib import asynccontextmanager
        from datetime import datetime, timezone
        from unittest.mock import AsyncMock

        comp = MagicMock()
        comp.storage.recall_chunks = AsyncMock(return_value=[])
        comp.config.search.system_namespace_prefixes = []

        @asynccontextmanager
        async def fake_components():
            yield comp

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", lambda: fake_components())
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root", lambda _comp: None
        )

        result = runner.invoke(
            cli, ["recall", "--since", "2025-03", "--until", "2025-03", "--format", "json"]
        )
        assert result.exit_code == 0, result.output

        kwargs = comp.storage.recall_chunks.call_args.kwargs
        assert kwargs["since"] == datetime(2025, 3, 1, tzinfo=timezone.utc)
        assert kwargs["until"] == datetime(2025, 4, 1, tzinfo=timezone.utc)

    def test_embedding_reset_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["embedding-reset", "--help"])
        assert result.exit_code == 0
        assert "apply-current" in result.output
        assert "revert-to-stored" in result.output

    def test_reset_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["reset", "--help"])
        assert result.exit_code == 0
        assert "Delete ALL data" in result.output
        assert "--yes" in result.output

    def test_context_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["context", "--help"])
        assert result.exit_code == 0
        assert "detect" in result.output
        assert "generate" in result.output

    def test_web_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["web", "--help"])
        assert result.exit_code == 0
        assert "--host" in result.output
        assert "--port" in result.output

    def test_shell_help(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["shell", "--help"])
        assert result.exit_code == 0
        assert "Interactive" in result.output


class TestHelpEpilog:
    """#1667: ``mm search`` / ``mm add`` --help should surface usage examples.

    The assertions are line-based on purpose. Substring checks against the whole
    output pass even when Click rewraps the epilog into a run-on paragraph, so
    they cannot tell a copy-pasteable example from a flattened one.
    """

    def test_search_help_shows_examples(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        lines = [line.strip() for line in result.output.splitlines()]
        assert "Examples:" in lines
        assert 'mm search "payment timeout"' in lines
        assert 'mm search "onboarding flow" --tag-filter onboarding --top-k 5' in lines
        assert 'mm search "incident" --scope project_shared --format context' in lines

    def test_add_help_shows_examples(self, runner: CliRunner) -> None:
        result = runner.invoke(cli, ["add", "--help"])
        assert result.exit_code == 0
        lines = [line.strip() for line in result.output.splitlines()]
        assert "Examples:" in lines
        assert 'mm add "Quick note to self" --json' in lines
        assert 'mm add "Canary deploy froze at 14:02Z." --tags incident,postmortem' in lines
        scoped_example = (
            'mm add "Standardize on uv" --scope project_shared --confirm-project-shared'
        )
        assert scoped_example in lines

    def test_epilog_examples_fit_an_80_column_terminal(self, runner: CliRunner) -> None:
        """Raw paragraphs are exempt from Click's wrapping, so width is on us.

        A `\\b` block is emitted verbatim: an example longer than the terminal is
        left for the terminal to soft-wrap, which breaks it mid-flag on screen.
        Nothing else in the help output can regress this, so pin it here.
        """
        for command in ("search", "add"):
            result = runner.invoke(cli, [command, "--help"])
            assert result.exit_code == 0
            examples = [
                line for line in result.output.splitlines() if line.strip().startswith("mm ")
            ]
            assert examples, f"no examples rendered for {command}"
            over_width = [line for line in examples if len(line) > 80]
            assert not over_width, f"{command} examples exceed 80 columns: {over_width}"
