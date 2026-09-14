"""Tests for ``mm embedding-reset --yes`` (issue #2065).

``--mode apply-current`` is destructive and was the last confirmation
prompt with no non-interactive escape, so cron/CI had to reach for
``yes | mm …`` — which dies with SIGPIPE under ``set -o pipefail``.  The
contract here is the flag's *shape*, not the wipe itself
(``reset_embedding_meta`` is covered in the storage tests):

* ``--mode apply-current --yes`` never reads stdin — proven by running it
  with no input at all, which makes ``click.confirm`` abort (exit 1) if
  the prompt is still reached;
* without ``--yes`` the prompt survives, and answering ``n`` cancels;
* ``--yes`` outside ``apply-current`` is a ``UsageError``, mirroring
  ``mm gc``'s "``--yes`` requires ``--apply``".
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem.cli import cli

from .helpers import make_chunk, set_home


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Tmp HOME + stripped ``MEMTOMEM_*`` env, with the module-bound
    ``_bootstrap._CONFIG_PATH`` repointed (a bare ``HOME`` override leaves
    the real ``~/.memtomem/config.json`` in play — #2103)."""
    from memtomem.cli import _bootstrap

    for var in [k for k in os.environ if k.startswith("MEMTOMEM_")]:
        monkeypatch.delenv(var, raising=False)

    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.chdir(h)
    set_home(monkeypatch, h)
    monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", h / ".memtomem" / "config.json")
    return h


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _install(home, runner: CliRunner) -> None:
    mem_dir = home / "memories"
    mem_dir.mkdir(exist_ok=True)
    (mem_dir / "note.md").write_text("# memo\n\nhello embedding reset\n", encoding="utf-8")
    result = runner.invoke(
        cli,
        [
            "init",
            "--non-interactive",
            "--provider",
            "none",
            "--memory-dir",
            str(mem_dir),
            "--mcp",
            "skip",
        ],
    )
    assert result.exit_code == 0, f"init failed: {result.output}"


def test_apply_current_with_yes_does_not_prompt(home, runner: CliRunner) -> None:
    _install(home, runner)

    result = runner.invoke(cli, ["embedding-reset", "--mode", "apply-current", "--yes"], input="")

    # Empty stdin is the discriminator: a surviving ``click.confirm`` gets
    # EOF and aborts with exit 1, so exit 0 can only mean it was skipped.
    assert result.exit_code == 0, result.output
    assert "DB reset to" in result.output
    assert "Continue?" not in result.output


def test_short_flag_is_accepted(home, runner: CliRunner) -> None:
    _install(home, runner)

    result = runner.invoke(cli, ["embedding-reset", "--mode", "apply-current", "-y"], input="")

    assert result.exit_code == 0, result.output
    assert "DB reset to" in result.output


def test_apply_current_without_yes_still_prompts(home, runner: CliRunner) -> None:
    _install(home, runner)

    result = runner.invoke(cli, ["embedding-reset", "--mode", "apply-current"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Continue?" in result.output
    assert "Cancelled." in result.output
    assert "DB reset to" not in result.output


@pytest.mark.parametrize("mode", ["status", "revert-to-stored"])
def test_yes_outside_apply_current_is_a_usage_error(home, runner: CliRunner, mode: str) -> None:
    _install(home, runner)

    result = runner.invoke(cli, ["embedding-reset", "--mode", mode, "--yes"], input="")

    assert result.exit_code == 2, result.output
    assert "--yes requires --mode apply-current" in result.stderr


def test_bare_yes_does_not_silently_run_status(home, runner: CliRunner) -> None:
    """``--mode`` defaults to ``status``, so a bare ``--yes`` must refuse
    rather than print status — the exact "reset without asking" misread the
    flag exists to remove."""
    _install(home, runner)

    result = runner.invoke(cli, ["embedding-reset", "--yes"], input="")

    assert result.exit_code == 2, result.output
    assert "Embedding Status" not in result.output


def test_help_documents_the_non_interactive_form(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["embedding-reset", "--help"])

    assert result.exit_code == 0
    assert "--yes" in result.output
    assert "-y" in result.output


def test_help_describes_guidance_and_storage_initialization(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["embedding-reset", "--help"])

    assert result.exit_code == 0
    output = " ".join(result.output.split())
    assert "Show stored settings and recovery instructions" in output
    assert "do not write config.json" in output
    assert "may create or initialize the database" in output
    assert "Switch runtime embedder" not in output


@pytest.mark.parametrize("scenario", ["model", "policy", "none"])
def test_revert_guidance_preserves_mismatch_config_and_data(home, runner, scenario) -> None:
    from memtomem.config import EmbeddingConfig, StorageConfig, embedding_policy_fingerprint
    from memtomem.storage.sqlite_backend import SqliteBackend

    stored = EmbeddingConfig(
        provider="onnx", model="bge-small-en-v1.5", dimension=384, max_sequence_tokens=1024
    )
    configured = stored.model_copy(deep=True)
    if scenario == "model":
        configured = EmbeddingConfig(provider="none", model="", dimension=0)
    elif scenario == "policy":
        configured.max_sequence_tokens = 128
    else:
        stored = EmbeddingConfig(provider="none", model="", dimension=0)

    config_path = home / ".memtomem" / "config.json"
    config_path.parent.mkdir()
    # Deliberately legacy: an accidental migration would change bytes and mtime.
    config_path.write_text(json.dumps({"embedding": configured.model_dump()}), encoding="utf-8")
    config_before = config_path.read_bytes()
    mtime_before = config_path.stat().st_mtime_ns
    db_path = config_path.with_name("memtomem.db")
    vector = [1.0] + [0.0] * (stored.dimension - 1) if stored.dimension else []

    def backend(embedding):
        return SqliteBackend(
            StorageConfig(sqlite_path=db_path),
            dimension=embedding.dimension,
            embedding_provider=embedding.provider,
            embedding_model=embedding.model,
            embedding_policy_fingerprint=embedding_policy_fingerprint(embedding),
            embedding_max_sequence_tokens=embedding.max_sequence_tokens,
            strict_dim_check=False,
        )

    async def seed():
        storage = backend(stored)
        await storage.initialize()
        try:
            await storage.upsert_chunks(
                [make_chunk("preserved guidance evidence", embedding=vector)]
            )
        finally:
            await storage.close()

    asyncio.run(seed())
    with sqlite3.connect(db_path) as db:
        meta_before = db.execute("SELECT * FROM _memtomem_meta ORDER BY key").fetchall()

    result = runner.invoke(cli, ["embedding-reset", "--mode", "revert-to-stored"], color=True)

    assert result.exit_code == 0, result.output
    assert "Guidance only" in result.output
    assert "does not change embedding settings or a running server" in result.output
    assert "may create or initialize the database" in result.output
    assert "Mismatch remains" in result.output
    assert f"DB stored: {stored.provider}/{stored.model} ({stored.dimension}d)" in result.output
    for key in ("provider", "model", "dimension", "max_sequence_tokens"):
        assert f"embedding.{key} = {getattr(stored, key)!r}" in result.output
    assert embedding_policy_fingerprint(stored) in result.output
    assert "~/.memtomem/config.json" in result.output
    assert "MEMTOMEM_*" in result.output
    assert "policy mismatch" in result.output
    assert "Restart affected servers" in result.output
    assert 'mem_embedding_reset(mode="revert_to_stored")' in result.output
    assert "does not persist settings to config.json" in result.output
    assert "Reverted" not in result.output
    assert "\x1b[32m" not in result.output
    assert config_path.read_bytes() == config_before
    assert config_path.stat().st_mtime_ns == mtime_before
    assert not config_path.with_name(".config.json.lock").exists()

    status = runner.invoke(cli, ["embedding-reset", "--mode", "status"])
    assert status.exit_code == 0, status.output
    assert "Mismatch detected" in status.output
    assert "# show stored settings and recovery instructions" in status.output
    assert "# match DB settings" not in status.output

    async def verify():
        storage = backend(configured)
        await storage.initialize()
        try:
            assert storage.embedding_mismatch is not None
            if scenario == "policy":
                assert storage.embedding_mismatch["policy_mismatch"]
            assert await storage.get_dense_coverage() == {
                "total": 1,
                "with_dense": int(bool(vector)),
            }
            hits = await storage.bm25_search("preserved")
            assert [hit.chunk.content for hit in hits] == ["preserved guidance evidence"]
            if vector:
                hits = await storage.dense_search(vector, top_k=1)
                assert [hit.chunk.content for hit in hits] == ["preserved guidance evidence"]
        finally:
            await storage.close()

    asyncio.run(verify())
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT * FROM _memtomem_meta ORDER BY key").fetchall() == meta_before


@pytest.mark.parametrize(
    ("provider", "model"),
    [("ollama", None), (None, "legacy-model"), ("ollama", ""), ("", "legacy-model")],
)
def test_partial_stamp_status_and_refused_revert_close_storage(
    home, runner, monkeypatch, provider, model
) -> None:
    from memtomem.storage.sqlite_backend import SqliteBackend

    _install(home, runner)
    assert runner.invoke(cli, ["embedding-reset"]).exit_code == 0
    db_path = home / ".memtomem" / "memtomem.db"
    with sqlite3.connect(db_path) as db:
        db.execute(
            "DELETE FROM _memtomem_meta WHERE key IN ('embedding_provider', 'embedding_model')"
        )
        for key, value in (("embedding_provider", provider), ("embedding_model", model)):
            if value is not None:
                db.execute("INSERT INTO _memtomem_meta VALUES (?, ?)", (key, value))
        before = db.execute("SELECT * FROM _memtomem_meta ORDER BY key").fetchall()
    closed = []
    real_close = SqliteBackend.close

    async def close(storage):
        await real_close(storage)
        closed.append(storage)

    monkeypatch.setattr(SqliteBackend, "close", close)
    reset = AsyncMock(side_effect=AssertionError("refused revert must not reset"))
    monkeypatch.setattr(SqliteBackend, "reset_embedding_meta", reset)
    result = runner.invoke(cli, ["embedding-reset"])
    assert result.exit_code == 0, result.output
    assert "unknown" in result.output and "unavailable" in result.output
    result = runner.invoke(cli, ["embedding-reset", "--mode", "revert-to-stored"])
    assert result.exit_code == 1, result.output
    assert "Cannot revert" in result.output and "unknown" in result.output
    assert "apply-current" in result.output
    assert "Reverted runtime" not in result.output
    assert len(closed) == 2 and all(storage._db is None for storage in closed)
    reset.assert_not_awaited()
    with sqlite3.connect(db_path) as db:
        assert db.execute("SELECT * FROM _memtomem_meta ORDER BY key").fetchall() == before
