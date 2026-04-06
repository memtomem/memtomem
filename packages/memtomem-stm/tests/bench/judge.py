"""Rule-based quality judge for benchmark scoring."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .harness import BenchTask, QAPair


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
        """Return per-keyword presence report."""
        lower = response.lower()
        return {kw: kw.lower() in lower for kw in task.expected_keywords}

    def qa_score(self, task: BenchTask, response: str) -> dict:
        """Score response based on QA pairs — can specific questions be answered?

        Returns:
            {
                "answerable": int,      # QA pairs whose answer is in the response
                "total": int,
                "score": float,         # answerable / total (0-1)
                "details": [{"question": str, "answerable": bool, "source": str}]
            }
        """
        lower = response.lower()
        details = []
        answerable = 0
        for qa in task.qa_pairs:
            found = qa.answer.lower() in lower
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
        lower = response.lower()
        content_total = content_found = 0
        memory_total = memory_found = 0
        for qa in task.qa_pairs:
            found = qa.answer.lower() in lower
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
