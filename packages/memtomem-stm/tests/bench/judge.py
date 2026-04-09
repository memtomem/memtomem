"""Rule-based quality judge for benchmark scoring."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .harness import BenchTask


def _normalize(text: str) -> str:
    """Normalize whitespace and punctuation for fuzzy matching."""
    return re.sub(r"[\s,;:_\-]+", " ", text.lower()).strip()


def _fuzzy_contains(answer: str, text: str) -> bool:
    """Check if answer appears in text with fuzzy matching.

    1. Exact substring match (fast path)
    2. Normalized match (whitespace/punctuation collapsed)
    3. Word-boundary match (answer words appear in sequence)
    """
    answer_lower = answer.lower()
    text_lower = text.lower()

    # Fast path: exact substring
    if answer_lower in text_lower:
        return True

    # Normalized match
    norm_answer = _normalize(answer)
    norm_text = _normalize(text)
    if norm_answer in norm_text:
        return True

    # Word sequence match: all words of answer appear in order within a window
    answer_words = norm_answer.split()
    if len(answer_words) >= 2:
        text_words = norm_text.split()
        for i in range(len(text_words) - len(answer_words) + 1):
            if text_words[i : i + len(answer_words)] == answer_words:
                return True

    return False


class RuleBasedJudge:
    """Deterministic quality scoring based on keyword/structure preservation.

    Scoring:
    - Start at 10.0
    - -2.0 per missing expected keyword (weighted if keyword_weights provided)
    - -1.0 if heading count below expected
    - -1.0 if code block count below expected
    - +0.5 if JSON is valid when content_type is "json"
    - Clamped to [0.0, 10.0]
    """

    def score(self, task: BenchTask, response: str) -> float:
        s = 10.0
        lower = response.lower()

        # Keyword preservation (with optional weights)
        weights = task.keyword_weights
        for i, kw in enumerate(task.expected_keywords):
            if kw.lower() not in lower:
                w = weights[i] if weights and i < len(weights) else 1.0
                s -= 2.0 * w

        # Heading preservation
        if task.expect_headings > 0:
            heading_count = len(re.findall(r"^#{1,6}\s", response, re.MULTILINE))
            if heading_count < task.expect_headings:
                s -= 1.0

        # Code block preservation
        if task.expect_code_blocks > 0:
            code_count = response.count("```")
            block_count = code_count // 2
            if block_count < task.expect_code_blocks:
                s -= 1.0

        # JSON validity bonus
        if task.content_type == "json":
            try:
                json.loads(response)
                s += 0.5
            except (json.JSONDecodeError, ValueError):
                pass

        return max(0.0, min(10.0, s))

    def keyword_report(self, task: BenchTask, response: str) -> dict[str, bool]:
        """Return per-keyword presence report (with fuzzy matching)."""
        return {kw: _fuzzy_contains(kw, response) for kw in task.expected_keywords}

    def qa_score(self, task: BenchTask, response: str) -> dict:
        """Score response based on QA pairs — can specific questions be answered?

        Uses fuzzy matching: normalized whitespace/punctuation, word sequence.

        Returns:
            {
                "answerable": int,      # QA pairs whose answer is in the response
                "total": int,
                "score": float,         # answerable / total (0-1)
                "details": [{"question": str, "answerable": bool, "source": str}]
            }
        """
        details = []
        answerable = 0
        for qa in task.qa_pairs:
            found = _fuzzy_contains(qa.answer, response)
            if found:
                answerable += 1
            details.append({
                "question": qa.question,
                "answerable": found,
                "source": qa.source,
            })
        total = len(task.qa_pairs)
        return {
            "answerable": answerable,
            "total": total,
            "score": answerable / total if total else 1.0,
            "details": details,
        }

    def qa_by_source(self, task: BenchTask, response: str) -> dict:
        """Score QA pairs grouped by source (content vs memory).

        Useful for measuring: did surfacing add answerable questions?
        """
        content_total = content_found = 0
        memory_total = memory_found = 0
        for qa in task.qa_pairs:
            found = _fuzzy_contains(qa.answer, response)
            if qa.source == "memory":
                memory_total += 1
                if found:
                    memory_found += 1
            else:
                content_total += 1
                if found:
                    content_found += 1
        return {
            "content": {"answerable": content_found, "total": content_total},
            "memory": {"answerable": memory_found, "total": memory_total},
        }
