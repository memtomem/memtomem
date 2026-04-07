"""Relevance scorers for query-aware compression.

Protocol + implementations: BM25 (default, zero-latency) and Embedding
(semantic, requires Ollama/OpenAI). Switching strategy: use embedding
when available, fall back to BM25.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Protocol

logger = logging.getLogger(__name__)


class RelevanceScorer(Protocol):
    """Scores sections by relevance to a query. Higher = more relevant."""

    def score_sections(self, query: str, sections: list[tuple[str, str]]) -> list[float]: ...


# ── BM25 Scorer ───────────────────────────────────────────────────────


class BM25Scorer:
    """BM25-like section relevance scoring with heading weighting.

    Headings are weighted 3× over body text. Case-insensitive with
    basic suffix stemming. Zero external dependencies.
    """

    _TOKEN_RE = re.compile(r"[a-zA-Z0-9\uac00-\ud7a3\u3040-\u309f\u30a0-\u30ff\u4e00-\u9fff_]+")
    _SUFFIX_RE = re.compile(r"(ing|ed|ly|tion|ness|ment|ies|es|s)$")
    _HEADING_WEIGHT = 3.0

    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b

    def score_sections(self, query: str, sections: list[tuple[str, str]]) -> list[float]:
        query_terms = self._tokenize(query)
        if not query_terms or not sections:
            return [0.0] * len(sections)

        # Pre-compute per-section TF (heading-weighted)
        doc_tfs: list[dict[str, float]] = []
        doc_lens: list[float] = []
        for title, body in sections:
            heading_tokens = self._tokenize(title)
            body_tokens = self._tokenize(body)
            tf: dict[str, float] = {}
            for t in heading_tokens:
                tf[t] = tf.get(t, 0.0) + self._HEADING_WEIGHT
            for t in body_tokens:
                tf[t] = tf.get(t, 0.0) + 1.0
            doc_tfs.append(tf)
            doc_lens.append(len(heading_tokens) * self._HEADING_WEIGHT + len(body_tokens))

        avgdl = sum(doc_lens) / len(doc_lens) if doc_lens else 1.0

        # IDF per query term
        n = len(sections)
        idfs: dict[str, float] = {}
        for t in set(query_terms):
            df = sum(1 for tfs in doc_tfs if t in tfs)
            idfs[t] = math.log((n - df + 0.5) / (df + 0.5) + 1.0)

        # BM25 score per section
        scores: list[float] = []
        for i in range(n):
            total = 0.0
            for t in query_terms:
                tf = doc_tfs[i].get(t, 0.0)
                idf = idfs.get(t, 0.0)
                num = tf * (self._k1 + 1.0)
                den = tf + self._k1 * (1.0 - self._b + self._b * doc_lens[i] / avgdl)
                total += idf * num / den if den > 0 else 0.0
            scores.append(total)
        return scores

    def _tokenize(self, text: str) -> list[str]:
        tokens = self._TOKEN_RE.findall(text.lower())
        return [self._stem(t) for t in tokens]

    def _stem(self, token: str) -> str:
        if len(token) > 4:
            return self._SUFFIX_RE.sub("", token)
        return token


# ── Embedding Scorer ──────────────────────────────────────────────────


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class EmbeddingScorer:
    """Semantic relevance scoring via embedding cosine similarity.

    Uses sync httpx to call Ollama or OpenAI embedding API.
    Falls back to BM25Scorer on any error (network, timeout, model not loaded).
    """

    def __init__(
        self,
        provider: str = "ollama",
        model: str = "nomic-embed-text",
        base_url: str = "http://localhost:11434",
        timeout: float = 10.0,
    ) -> None:
        self._provider = provider
        self._model = model
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout
        self._fallback = BM25Scorer()

    def score_sections(self, query: str, sections: list[tuple[str, str]]) -> list[float]:
        if not query or not sections:
            return [0.0] * len(sections)

        try:
            return self._score_via_embedding(query, sections)
        except Exception:
            logger.debug("EmbeddingScorer failed, falling back to BM25", exc_info=True)
            return self._fallback.score_sections(query, sections)

    def _score_via_embedding(self, query: str, sections: list[tuple[str, str]]) -> list[float]:
        # Build texts: query + each section (title + body truncated)
        texts = [query]
        for title, body in sections:
            # Truncate body to ~500 chars to limit embedding cost
            section_text = f"{title}\n{body[:500]}"
            texts.append(section_text)

        embeddings = self._embed_batch(texts)
        query_emb = embeddings[0]
        return [_cosine_similarity(query_emb, emb) for emb in embeddings[1:]]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        try:
            import httpx
        except ImportError:
            raise RuntimeError("httpx required for EmbeddingScorer")

        if self._provider == "ollama":
            return self._embed_ollama(httpx, texts)
        elif self._provider == "openai":
            return self._embed_openai(httpx, texts)
        else:
            raise ValueError(f"Unknown embedding provider: {self._provider}")

    def _embed_ollama(self, httpx_mod: object, texts: list[str]) -> list[list[float]]:
        import httpx as _httpx

        resp = _httpx.post(
            f"{self._base_url}/api/embed",
            json={"model": self._model, "input": texts},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        return resp.json()["embeddings"]

    def _embed_openai(self, httpx_mod: object, texts: list[str]) -> list[list[float]]:
        import os

        import httpx as _httpx

        api_key = os.environ.get("OPENAI_API_KEY", "")
        url = self._base_url + "/v1/embeddings"
        resp = _httpx.post(
            url,
            json={"model": self._model, "input": texts, "encoding_format": "float"},
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=self._timeout,
        )
        resp.raise_for_status()
        data = resp.json()["data"]
        # Sort by index to ensure order matches input
        data.sort(key=lambda x: x["index"])
        return [d["embedding"] for d in data]


# ── Factory ───────────────────────────────────────────────────────────


def create_scorer(
    scorer_type: str = "bm25",
    provider: str = "ollama",
    model: str = "nomic-embed-text",
    base_url: str = "http://localhost:11434",
    timeout: float = 10.0,
) -> RelevanceScorer:
    """Create a relevance scorer from config.

    Args:
        scorer_type: "bm25" (default) or "embedding"
        provider: "ollama" or "openai" (only for embedding)
        model: embedding model name
        base_url: embedding API base URL
        timeout: embedding API timeout in seconds
    """
    if scorer_type == "embedding":
        return EmbeddingScorer(provider=provider, model=model, base_url=base_url, timeout=timeout)
    return BM25Scorer()
