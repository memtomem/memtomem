from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from memtomem.config import Mem2MemConfig
from memtomem.indexing import budget_migration as migration
from memtomem.models import Chunk, ChunkMetadata


@pytest.fixture
async def reviewed(storage, tmp_path, monkeypatch):
    source = tmp_path / "source.md"
    source.write_text("# Heading\n\nReplacement body.\n")
    old = Chunk(
        content="Previous body.",
        metadata=ChunkMetadata(
            source_file=source,
            start_line=1,
            end_line=3,
            tags=("retained",),
            namespace="private",
            valid_from_unix=100,
            valid_to_unix=200,
        ),
    )
    await storage.upsert_chunks([old])
    config = Mem2MemConfig()
    config.indexing.memory_dirs = [tmp_path]
    monkeypatch.setattr(migration, "build_fresh_config", lambda **kw: config)
    manifest = {
        "manifest_id": "test",
        "config_hash": migration.digest(config.model_dump(mode="python")),
        "reindex": [
            {
                "source": str(source),
                "source_sha256": migration.source_hash(source),
                "state_hash": migration.state_hash(storage._get_db(), source),
            }
        ],
    }
    embedder = AsyncMock()
    embedder.dimension = 0
    embedder.model_name = "none"
    engine = migration.ReviewedEngine(storage, embedder, config.indexing, manifest=manifest)
    return engine, source, old, manifest


@pytest.mark.asyncio
async def test_receipt_and_metadata_commit_together(storage, reviewed):
    engine, source, old, manifest = reviewed
    result = await engine.index_file(source, force=True, path_scope="explicit")
    assert not result.errors
    after = await storage.list_chunks_by_source(source, limit=None)
    assert after
    assert all(c.metadata.namespace == "private" and "retained" in c.metadata.tags for c in after)
    assert all(c.metadata.valid_from_unix == 100 and c.metadata.valid_to_unix == 200 for c in after)
    assert storage._get_meta(engine.receipt_key(source)) == migration.state_hash(
        storage._get_db(), source
    )
    assert source.read_text() == "# Heading\n\nReplacement body.\n"


@pytest.mark.asyncio
async def test_stale_source_and_metadata_refused(storage, reviewed):
    engine, source, old, manifest = reviewed
    old.metadata = replace(old.metadata, tags=("concurrent-edit",))
    await storage.upsert_chunks([old])
    before = migration.state_hash(storage._get_db(), source)
    with pytest.raises(ValueError):
        await engine.index_file(source, force=True, path_scope="explicit")
    assert migration.state_hash(storage._get_db(), source) == before
    assert storage._get_meta(engine.receipt_key(source)) is None


@pytest.mark.asyncio
async def test_receipt_failure_rolls_back_chunk_replacement(storage, reviewed, monkeypatch):
    engine, source, old, manifest = reviewed
    before = migration.state_hash(storage._get_db(), source)

    async def fail(path):
        raise ValueError("simulated receipt failure")

    monkeypatch.setattr(engine, "_record_source_commit", fail)
    with pytest.raises(ValueError):
        await engine.index_file(source, force=True, path_scope="explicit")
    assert migration.state_hash(storage._get_db(), source) == before
    assert storage._get_meta(engine.receipt_key(source)) is None


def test_ambiguous_metadata_is_not_flattened():
    old = [
        Chunk(content="a", metadata=ChunkMetadata(source_file=Path("a"), namespace=n))
        for n in ["one", "two"]
    ]
    with pytest.raises(ValueError, match="ambiguous"):
        migration.preserve_metadata([old[0]], old)


def test_configuration_hash_is_stable_across_process_hash_seeds():
    import os
    import subprocess
    import sys

    code = (
        "from memtomem.config import Mem2MemConfig; "
        "from memtomem.indexing.budget_migration import digest; "
        "print(digest(Mem2MemConfig().model_dump(mode='python')))"
    )
    results = [
        subprocess.check_output(
            [sys.executable, "-P", "-c", code],
            env={**os.environ, "PYTHONHASHSEED": str(seed)},
            text=True,
        )
        for seed in (1, 2, 17)
    ]
    assert len(set(results)) == 1


@pytest.fixture
def symbolic_budget_config(tmp_path, monkeypatch):
    from memtomem.embedding import profiles

    tokenizers = pytest.importorskip("tokenizers")
    tokenizer = tokenizers.Tokenizer(tokenizers.models.WordLevel({"[UNK]": 0}, unk_token="[UNK]"))
    path = tmp_path / "resolved-tokenizer.json"
    tokenizer.save(str(path))
    config = Mem2MemConfig(
        embedding={"provider": "onnx", "model": "multilingual-e5-small", "dimension": 384}
    )
    monkeypatch.setattr(profiles, "resolve_tokenizer", lambda identifier: path)
    return config, path


async def test_audit_uses_resolved_symbolic_tokenizer(storage, symbolic_budget_config):
    from memtomem.indexing.budget_audit import audit

    config, path = symbolic_budget_config
    db_path = Path(storage._get_db().execute("PRAGMA database_list").fetchone()[2])
    report = audit(db_path, config.indexing, set())
    assert report["tokenizer_sha256"] == migration.source_hash(path)
    assert report["budgets"]["model"] == 512


async def test_migration_rejects_changed_resolved_tokenizer_before_backup(
    symbolic_budget_config, tmp_path
):
    config, path = symbolic_budget_config
    plan = {
        "policy": migration.POLICY_VERSION,
        "config_hash": migration.digest(config.model_dump(mode="python")),
        "tokenizer_sha256": "0" * 64,
    }
    plan["manifest_id"] = migration.digest(plan)
    with pytest.raises(ValueError, match="tokenizer changed"):
        await migration.apply_plan(config, plan, tmp_path / "report.json")
    assert not list(tmp_path.glob("before-budget-migration-*.db"))


async def test_migration_resolves_symbolic_tokenizer_before_database_validation(
    symbolic_budget_config, tmp_path
):
    config, path = symbolic_budget_config
    plan = {
        "policy": migration.POLICY_VERSION,
        "config_hash": migration.digest(config.model_dump(mode="python")),
        "tokenizer_sha256": migration.source_hash(path),
        "db_path": "changed-database",
    }
    plan["manifest_id"] = migration.digest(plan)
    with pytest.raises(ValueError, match="database path changed"):
        await migration.apply_plan(config, plan, tmp_path / "report.json")
    assert not list(tmp_path.glob("before-budget-migration-*.db"))


async def test_audit_cli_accepts_explicit_e5_budget_and_symbolic_tokenizer(
    storage, symbolic_budget_config, tmp_path, monkeypatch
):
    import json
    from memtomem.indexing import budget_audit

    config, tokenizer = symbolic_budget_config
    db_path = storage._get_db().execute("PRAGMA database_list").fetchone()[2]
    report = tmp_path / "audit.json"
    monkeypatch.setattr(
        "sys.argv",
        [
            "budget_audit",
            "--db",
            db_path,
            "--tokenizer",
            config.indexing.chunk_tokenizer_path,
            "--report",
            str(report),
            "--body-tokens",
            "384",
            "--context-tokens",
            "96",
            "--model-tokens",
            "512",
            "--input-prefix",
            "passage: ",
        ],
    )
    budget_audit.main()
    payload = json.loads(report.read_text())
    assert payload["budgets"] == {"body": 384, "context": 96, "model": 512}
    assert payload["tokenizer_sha256"] == migration.source_hash(tokenizer)
