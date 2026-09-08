"""Real CPU model smoke; downloads only the pinned public E5 artifact."""

import asyncio
import math

from memtomem.chunking.bounded import TokenBudget
from memtomem.config import EmbeddingConfig, Mem2MemConfig
from memtomem.embedding.onnx import OnnxEmbedder
from memtomem.errors import EmbeddingError


async def main():
    config = Mem2MemConfig(embedding=EmbeddingConfig(provider="onnx", threads=1))
    budget = TokenBudget(config.indexing)
    embedder = OnnxEmbedder(config.embedding)
    try:
        vectors = await embedder.embed_texts(
            ["한국어 CPU 검색 테스트", "English CPU retrieval test"]
        )
        query = await embedder.embed_query("한국어 CPU 검색 테스트")
        assert len(query) == 384 and all(len(v) == 384 for v in vectors)
        assert all(abs(sum(x * x for x in v) - 1) < 0.0001 for v in [*vectors, query])
        assert all(math.isfinite(x) for v in vectors for x in v)
        assert query != vectors[0], "Query and document prefixes must differ"
        oversized = "한국어 검증 " * 1000
        assert budget.count(oversized, special=True) > 512
        try:
            await embedder.embed_texts([oversized])
        except EmbeddingError:
            pass
        else:
            raise AssertionError("Oversized E5 input was silently truncated")
        print("PASS: CPU, 384 dimensions, normalized bilingual vectors, roles, overflow refusal")
    finally:
        await embedder.close()


if __name__ == "__main__":
    asyncio.run(main())
