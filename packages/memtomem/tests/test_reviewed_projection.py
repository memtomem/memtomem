import hashlib
import json

import pytest

from memtomem import privacy
from memtomem.config import IndexingConfig
from memtomem.indexing.engine import IndexEngine
from memtomem.indexing.reviewed_projection import prepare_reviewed_projection


def fixture_manifest(tmp_path):
    path = tmp_path / "secret.md"
    text = "# Document\n\npassword: first\n  continued-value\n\nPublic text.\n"
    path.write_text(text)
    manifest = tmp_path / "mask.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": {
                    str(path.resolve()): {
                        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "spans": [[3, 4]],
                    }
                },
            }
        )
    )
    return path, text, manifest


def test_masks_complete_reviewed_block_and_preserves_source(tmp_path):
    path, text, manifest = fixture_manifest(tmp_path)
    result = prepare_reviewed_projection(path, text, str(manifest))
    assert "continued-value" not in result.content
    assert "Public text." in result.content
    assert not privacy.scan(result.content)
    assert result.redaction_count == 1
    assert result.content.count("\n") == text.count("\n")
    assert path.read_text() == text


@pytest.mark.parametrize("change", ["source", "path", "shared", "missing", "invalid", "residual"])
def test_refuses_unreviewed_or_invalid_projection(tmp_path, change):
    path, text, manifest = fixture_manifest(tmp_path)
    scope = "user"
    if change == "source":
        text += "new private material\n"
    if change == "path":
        path = tmp_path / "other.md"
    if change == "shared":
        scope = "project_shared"
    if change == "missing":
        manifest = tmp_path / "missing.json"
    if change == "invalid":
        manifest.write_text("{}")
    if change == "residual":
        payload = json.loads(manifest.read_text())
        payload["sources"][str(path.resolve())]["spans"] = [[4, 4]]
        manifest.write_text(json.dumps(payload))
    result = prepare_reviewed_projection(path, text, str(manifest), scope=scope)
    assert result.content == text
    assert result.guard_content == text
    assert result.redaction_count == 0


def test_engine_preview_and_chunks_use_same_read_only_projection(tmp_path):
    path, text, manifest = fixture_manifest(tmp_path)
    engine = IndexEngine(None, None, IndexingConfig(index_masking_manifest_path=str(manifest)))
    assert engine.preview_redaction_decision(path, text) == "pass"
    chunks = engine.chunk_content(path, text)
    assert chunks
    assert all(c.metadata.source_read_only and c.metadata.source_span_hash is None for c in chunks)
    assert all("continued-value" not in c.retrieval_content for c in chunks)
    assert engine.preview_redaction_decision(path, text + "\nChanged") == "blocked"


async def test_reindex_refuses_to_remove_linked_chunk_identity(components, tmp_path):
    engine = components.index_engine
    path = tmp_path / "linked.md"
    path.write_text("# Original\n\nOriginal reference material.\n")
    await engine.index_file(path, path_scope="explicit")
    old = await components.storage.list_chunks_by_source(path, limit=None)
    assert old
    db = components.storage._get_db()
    db.execute(
        "INSERT INTO chunk_links(source_id,target_id,namespace_target,created_at) VALUES(?,?,?,?)",
        (str(old[0].id), str(old[0].id), "default", "2026-09-09"),
    )
    db.commit()
    path.write_text("# Replacement\n\nCompletely different replacement material.\n")
    with pytest.raises(ValueError, match="linked chunk identities"):
        await engine.index_file(path, force=True, path_scope="explicit")
    after = await components.storage.list_chunks_by_source(path, limit=None)
    assert [(c.id, c.content) for c in old] == [(c.id, c.content) for c in after]
    assert db.execute("SELECT count(*) FROM chunk_links").fetchone()[0] == 1
