from unittest.mock import AsyncMock

import pytest

from memtomem import privacy
from memtomem.config import IndexingConfig
from memtomem.indexing.engine import IndexEngine
from memtomem.indexing.privacy_projection import prepare_index_content


@pytest.mark.parametrize(
    "text",
    [
        '# Example: `"password": "secret"` and `password: required`\n',
        'API_KEY="a private value"\n',
        "password: some private words\n",
        "{'password': 'value-with-escaped-\\'quote'}\n",
    ],
)
def test_masked_values_never_reach_guard(text):
    projection = prepare_index_content(text)
    assert projection.redaction_count > 0
    assert "[REDACTED]" in projection.content
    assert not privacy.scan(projection.guard_content)
    assert projection.content.count("\n") == text.count("\n")
    assert privacy.scan(text)  # raw scanner contract remains strict
    repeated = prepare_index_content(projection.content)
    assert repeated.content == projection.content
    assert not privacy.scan(repeated.guard_content)


@pytest.mark.parametrize(
    "text",
    [
        "`env -u OPENAI_API_KEY OPENAI_API_KEY= nbconvert`",
        "`password:` is a label",
        'api_key=""\n',
    ],
)
def test_empty_values_and_bare_examples(text):
    projection = prepare_index_content(text)
    assert projection.content == text
    assert projection.redaction_count == 0
    assert not privacy.scan(projection.guard_content)


@pytest.mark.parametrize(
    "text",
    [
        '"password": "unterminated\n',
        "password: `unknown syntax`",
        '"password": "nested password: other"',
        "password=demo\n-----BEGIN RSA PRIVATE KEY-----\nAAAA\n-----END RSA PRIVATE KEY-----\n",
    ],
)
def test_ambiguous_or_specific_material_remains_blocked(text):
    projection = prepare_index_content(text)
    assert projection.content == projection.guard_content == text
    assert privacy.scan(projection.guard_content)


def test_shared_scope_never_acquires_an_exception():
    text = '"password": "secret"'
    projection = prepare_index_content(text, scope="project_shared")
    assert projection.guard_content == text
    assert (
        privacy.enforce_write_guard(
            projection.guard_content, surface="test", scope="project_shared", record_outcome=False
        ).decision
        == "blocked"
    )


@pytest.mark.asyncio
async def test_file_projection_persists_but_source_and_ingress_guard_stay_strict(storage, tmp_path):
    path = tmp_path / "example.md"
    original = '# Example\n\n`"password": "a private value"`\n'
    path.write_text(original)
    embedder = AsyncMock()
    embedder.dimension = 0
    embedder.model_name = "none"
    engine = IndexEngine(storage, embedder, IndexingConfig(memory_dirs=[tmp_path]))
    assert engine.preview_redaction_decision(path, original) == "pass"
    result = await engine.index_file(path)
    assert not result.errors
    chunks = await storage.list_chunks_by_source(path, limit=None)
    assert chunks and all(c.metadata.redaction_count for c in chunks)
    assert all("a private value" not in c.retrieval_content for c in chunks)
    assert path.read_text() == original
    assert (
        privacy.enforce_write_guard(original, surface="mem_add", record_outcome=False).decision
        == "blocked"
    )


@pytest.mark.asyncio
async def test_masked_projection_cannot_overwrite_original_via_mem_edit(bm25_only_components):
    from helpers import StubCtx
    from memtomem.server.context import AppContext
    from memtomem.server.tools.memory_crud import mem_edit

    comp, directory = bm25_only_components
    source = directory / "masked.md"
    original = '# Example\n\n`"password": "original private value"`\n'
    source.write_text(original)
    await comp.index_engine.index_file(source)
    chunks = await comp.storage.list_chunks_by_source(source, limit=None)
    assert chunks[0].metadata.redaction_count > 0
    response = await mem_edit(
        chunk_id=str(chunks[0].id),
        new_content="replacement",
        ctx=StubCtx(AppContext.from_components(comp)),
    )
    assert "masked_projection_read_only" in response
    assert source.read_text() == original
