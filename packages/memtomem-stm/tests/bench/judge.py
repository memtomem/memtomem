"""Rule-based quality judge for benchmark scoring."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .harness import BenchTask


class RuleBasedJudge:
    """Deterministic quality scoring based on keyword/structure preservation."""

    def score(self, task: BenchTask, response: str) -> float:
        """Score response quality on a 0-10 scale.

        Deductions:
        - -2.0 per missing expected keyword
        - -1.0 if heading count below expected
        - -1.0 if code block count below expected

        Bonuses:
        - +0.5 if JSON is valid when content_type is "json"
        """
        s = 10.0
        lower = response.lower()

        # Keyword preservation
        for kw in task.expected_keywords:
            if kw.lower() not in lower:
                s -= 2.0

        # Heading preservation
        if task.expect_headings > 0:
            heading_count = len(re.findall(r"^#{1,6}\s", response, re.MULTILINE))
            if heading_count < task.expect_headings:
                s -= 1.0

        # Code block preservation
        if task.expect_code_blocks > 0:
            code_count = response.count("```")
            # Each block has open + close = 2 markers
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
