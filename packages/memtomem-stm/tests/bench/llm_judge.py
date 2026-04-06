"""LLM-as-Judge — semantic quality scoring for benchmark evaluation.

Uses a small/fast LLM (Claude Haiku or GPT-4o-mini) to judge whether
compressed text preserves the meaning and critical information of the original.

Scoring dimensions:
1. Factual completeness — are key facts preserved?
2. Structural coherence — is the output well-organized?
3. Answer sufficiency — can QA pairs still be answered?

Usage:
    judge = LLMJudge(provider="anthropic", model="claude-haiku-4-5-20251001")
    result = await judge.score(task, compressed_text)
    print(result.overall, result.dimensions)

Cost control:
    - Use @pytest.mark.llm_judge to isolate cost-bearing tests
    - Cache results by content hash to avoid re-scoring identical texts
    - Default to Haiku/mini for <$0.001 per evaluation
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from .harness import BenchTask


# ═══════════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class JudgeDimension:
    """Score for a single evaluation dimension."""

    name: str
    score: float  # 0-10
    reasoning: str


@dataclass
class LLMJudgeResult:
    """Full LLM judge evaluation result."""

    task_id: str
    overall: float  # 0-10 (weighted average of dimensions)
    dimensions: list[JudgeDimension] = field(default_factory=list)
    qa_results: list[dict] = field(default_factory=list)  # per-QA evaluation
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False
    error: str | None = None


@dataclass
class CorrelationResult:
    """Correlation between RuleBasedJudge and LLMJudge scores."""

    n: int  # number of samples
    pearson_r: float  # Pearson correlation coefficient
    spearman_rho: float  # Spearman rank correlation
    mean_abs_diff: float  # mean |rule - llm| score difference
    pairs: list[tuple[float, float]] = field(default_factory=list)  # (rule, llm)


# ═══════════════════════════════════════════════════════════════════════════
# Prompt templates
# ═══════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """You are an expert evaluator assessing whether a compressed version \
of a document preserves critical information.

You will receive:
1. ORIGINAL: the full document before compression
2. COMPRESSED: the document after passing through a compression pipeline
3. TASK: what the document is about

Score each dimension 0-10 and provide brief reasoning.

Respond in JSON only:
{
  "factual_completeness": {"score": <0-10>, "reasoning": "<1-2 sentences>"},
  "structural_coherence": {"score": <0-10>, "reasoning": "<1-2 sentences>"},
  "answer_sufficiency": {"score": <0-10>, "reasoning": "<1-2 sentences>"},
  "overall": <0-10 weighted average>
}"""

_QA_SYSTEM_PROMPT = """You are an expert evaluator. Given a document and a question, \
determine if the document contains enough information to answer the question.

Respond in JSON only:
{"answerable": true/false, "confidence": <0-1>, "reasoning": "<brief>"}"""


def _build_score_prompt(task: BenchTask, compressed: str) -> str:
    """Build the scoring prompt."""
    original_excerpt = task.content[:3000]  # Limit to control cost
    compressed_excerpt = compressed[:3000]

    return f"""TASK: {task.description}
CONTENT TYPE: {task.content_type}

ORIGINAL ({len(task.content)} chars, showing first 3000):
---
{original_excerpt}
---

COMPRESSED ({len(compressed)} chars, showing first 3000):
---
{compressed_excerpt}
---

Score the COMPRESSED version on how well it preserves the ORIGINAL's information."""


def _build_qa_prompt(question: str, text: str) -> str:
    """Build QA evaluation prompt."""
    text_excerpt = text[:3000]
    return f"""DOCUMENT ({len(text)} chars, showing first 3000):
---
{text_excerpt}
---

QUESTION: {question}

Can this question be answered from the document above?"""


# ═══════════════════════════════════════════════════════════════════════════
# LLM providers
# ═══════════════════════════════════════════════════════════════════════════


async def _call_anthropic(
    client: httpx.AsyncClient,
    model: str,
    system: str,
    user_msg: str,
    api_key: str,
) -> tuple[str, int, int]:
    """Call Anthropic Messages API. Returns (content, prompt_tokens, completion_tokens)."""
    resp = await client.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 512,
            "system": system,
            "messages": [{"role": "user", "content": user_msg}],
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["content"][0]["text"]
    usage = data.get("usage", {})
    return content, usage.get("input_tokens", 0), usage.get("output_tokens", 0)


async def _call_openai(
    client: httpx.AsyncClient,
    model: str,
    system: str,
    user_msg: str,
    api_key: str,
) -> tuple[str, int, int]:
    """Call OpenAI Chat Completions API. Returns (content, prompt_tokens, completion_tokens)."""
    resp = await client.post(
        "https://api.openai.com/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": 512,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            "response_format": {"type": "json_object"},
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


# ═══════════════════════════════════════════════════════════════════════════
# LLMJudge
# ═══════════════════════════════════════════════════════════════════════════


class LLMJudge:
    """Semantic quality judge using LLM evaluation.

    Providers:
        - "anthropic": Claude Haiku (default model: claude-haiku-4-5-20251001)
        - "openai": GPT-4o-mini (default model: gpt-4o-mini)

    Environment variables:
        - ANTHROPIC_API_KEY or OPENAI_API_KEY
        - BENCH_LLM_JUDGE_PROVIDER (override provider)
        - BENCH_LLM_JUDGE_MODEL (override model)
    """

    DEFAULT_MODELS = {
        "anthropic": "claude-haiku-4-5-20251001",
        "openai": "gpt-4o-mini",
    }

    def __init__(
        self,
        provider: str = "anthropic",
        model: str | None = None,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._provider = os.environ.get("BENCH_LLM_JUDGE_PROVIDER", provider)
        self._model = os.environ.get(
            "BENCH_LLM_JUDGE_MODEL",
            model or self.DEFAULT_MODELS.get(self._provider, "claude-haiku-4-5-20251001"),
        )
        self._api_key = api_key or self._resolve_api_key()
        self._client = client  # externally managed client (for testing)
        self._owns_client = client is None
        self._cache: dict[str, LLMJudgeResult] = {}

    def _resolve_api_key(self) -> str:
        if self._provider == "anthropic":
            return os.environ.get("ANTHROPIC_API_KEY", "")
        if self._provider == "openai":
            return os.environ.get("OPENAI_API_KEY", "")
        return ""

    @staticmethod
    def _content_hash(task_id: str, text: str) -> str:
        """Cache key from task_id + text hash."""
        h = hashlib.sha256(f"{task_id}:{text}".encode()).hexdigest()[:16]
        return h

    async def _call_llm(self, system: str, user_msg: str) -> tuple[str, int, int]:
        """Dispatch to the configured provider."""
        client = self._client or httpx.AsyncClient()
        try:
            if self._provider == "anthropic":
                return await _call_anthropic(
                    client, self._model, system, user_msg, self._api_key
                )
            elif self._provider == "openai":
                return await _call_openai(
                    client, self._model, system, user_msg, self._api_key
                )
            else:
                raise ValueError(f"Unknown provider: {self._provider}")
        finally:
            if self._owns_client:
                await client.aclose()

    def _parse_score_response(self, raw: str) -> tuple[float, list[JudgeDimension]]:
        """Parse JSON score response from LLM."""
        # Strip markdown code fences if present
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()

        data = json.loads(text)
        dims = []
        for key in ("factual_completeness", "structural_coherence", "answer_sufficiency"):
            d = data.get(key, {})
            dims.append(
                JudgeDimension(
                    name=key,
                    score=float(d.get("score", 5.0)),
                    reasoning=str(d.get("reasoning", "")),
                )
            )
        overall = float(data.get("overall", sum(d.score for d in dims) / len(dims)))
        return overall, dims

    async def score(self, task: BenchTask, compressed: str) -> LLMJudgeResult:
        """Score compressed text against original using LLM evaluation.

        Returns LLMJudgeResult with dimension scores and optional QA results.
        Results are cached by task_id + content hash.
        """
        cache_key = self._content_hash(task.task_id, compressed)
        if cache_key in self._cache:
            result = self._cache[cache_key]
            result.cached = True
            return result

        try:
            prompt = _build_score_prompt(task, compressed)
            raw, p_tok, c_tok = await self._call_llm(_SYSTEM_PROMPT, prompt)
            overall, dims = self._parse_score_response(raw)

            # QA evaluation (if task has qa_pairs)
            qa_results = []
            total_qa_tokens = (0, 0)
            if task.qa_pairs:
                for qa in task.qa_pairs:
                    qa_prompt = _build_qa_prompt(qa.question, compressed)
                    qa_raw, qp, qc = await self._call_llm(_QA_SYSTEM_PROMPT, qa_prompt)
                    total_qa_tokens = (total_qa_tokens[0] + qp, total_qa_tokens[1] + qc)
                    try:
                        qa_text = qa_raw.strip()
                        if qa_text.startswith("```"):
                            qa_text = qa_text.split("\n", 1)[1] if "\n" in qa_text else qa_text[3:]
                            if qa_text.endswith("```"):
                                qa_text = qa_text[:-3]
                        qa_data = json.loads(qa_text.strip())
                        qa_results.append({
                            "question": qa.question,
                            "answerable": qa_data.get("answerable", False),
                            "confidence": qa_data.get("confidence", 0.0),
                            "reasoning": qa_data.get("reasoning", ""),
                            "source": qa.source,
                        })
                    except (json.JSONDecodeError, KeyError):
                        qa_results.append({
                            "question": qa.question,
                            "answerable": False,
                            "confidence": 0.0,
                            "reasoning": f"Parse error: {qa_raw[:100]}",
                            "source": qa.source,
                        })

            result = LLMJudgeResult(
                task_id=task.task_id,
                overall=overall,
                dimensions=dims,
                qa_results=qa_results,
                model=self._model,
                prompt_tokens=p_tok + total_qa_tokens[0],
                completion_tokens=c_tok + total_qa_tokens[1],
                cached=False,
            )
            self._cache[cache_key] = result
            return result

        except Exception as exc:
            return LLMJudgeResult(
                task_id=task.task_id,
                overall=0.0,
                model=self._model,
                error=str(exc),
            )

    async def score_batch(
        self, tasks: list[tuple[BenchTask, str]]
    ) -> list[LLMJudgeResult]:
        """Score multiple (task, compressed_text) pairs sequentially.

        Sequential to respect rate limits. For parallel, use asyncio.gather externally.
        """
        results = []
        for task, text in tasks:
            r = await self.score(task, text)
            results.append(r)
        return results


# ═══════════════════════════════════════════════════════════════════════════
# Correlation measurement
# ═══════════════════════════════════════════════════════════════════════════


def compute_correlation(
    rule_scores: list[float],
    llm_scores: list[float],
) -> CorrelationResult:
    """Compute correlation between RuleBasedJudge and LLMJudge scores.

    Uses pure Python (no numpy/scipy dependency) for portability.
    Pearson r and Spearman rho measure linear and rank correlation respectively.
    """
    n = len(rule_scores)
    if n != len(llm_scores) or n < 2:
        return CorrelationResult(
            n=n, pearson_r=0.0, spearman_rho=0.0, mean_abs_diff=0.0,
            pairs=list(zip(rule_scores, llm_scores)),
        )

    pairs = list(zip(rule_scores, llm_scores))

    # Mean absolute difference
    mean_abs_diff = sum(abs(r - l) for r, l in pairs) / n

    # Pearson r
    mean_r = sum(rule_scores) / n
    mean_l = sum(llm_scores) / n
    cov = sum((r - mean_r) * (l - mean_l) for r, l in pairs) / n
    std_r = (sum((r - mean_r) ** 2 for r in rule_scores) / n) ** 0.5
    std_l = (sum((l - mean_l) ** 2 for l in llm_scores) / n) ** 0.5
    pearson_r = cov / (std_r * std_l) if std_r > 0 and std_l > 0 else 0.0

    # Spearman rho (rank correlation)
    def _rank(values: list[float]) -> list[float]:
        sorted_vals = sorted(enumerate(values), key=lambda x: x[1])
        ranks = [0.0] * len(values)
        i = 0
        while i < len(sorted_vals):
            j = i
            while j < len(sorted_vals) and sorted_vals[j][1] == sorted_vals[i][1]:
                j += 1
            avg_rank = (i + j - 1) / 2.0 + 1  # 1-based average
            for k in range(i, j):
                ranks[sorted_vals[k][0]] = avg_rank
            i = j
        return ranks

    ranks_r = _rank(rule_scores)
    ranks_l = _rank(llm_scores)
    # Spearman = Pearson on ranks
    mean_rr = sum(ranks_r) / n
    mean_rl = sum(ranks_l) / n
    cov_rank = sum((rr - mean_rr) * (rl - mean_rl) for rr, rl in zip(ranks_r, ranks_l)) / n
    std_rr = (sum((rr - mean_rr) ** 2 for rr in ranks_r) / n) ** 0.5
    std_rl = (sum((rl - mean_rl) ** 2 for rl in ranks_l) / n) ** 0.5
    spearman_rho = cov_rank / (std_rr * std_rl) if std_rr > 0 and std_rl > 0 else 0.0

    return CorrelationResult(
        n=n,
        pearson_r=pearson_r,
        spearman_rho=spearman_rho,
        mean_abs_diff=mean_abs_diff,
        pairs=pairs,
    )
