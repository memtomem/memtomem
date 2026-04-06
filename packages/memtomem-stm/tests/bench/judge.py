"""Rule-based quality judge for benchmark scoring."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .harness import BenchTask


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
