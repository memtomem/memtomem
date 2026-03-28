"""Privacy-aware content scanning for proxy compression routing."""

from __future__ import annotations

import logging
import re
from functools import lru_cache

logger = logging.getLogger(__name__)

DEFAULT_PATTERNS = [
    r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]",
    r"(?i)(password|passwd|pwd)\s*[:=]",
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
    r"(?i)(sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{36}|xoxb-[0-9]+-)",
    r"(?i)(BEGIN\s+(RSA|EC|OPENSSH)\s+PRIVATE\s+KEY)",
]


@lru_cache(maxsize=1)
def _compile_patterns(patterns: tuple[str, ...]) -> list[re.Pattern[str]]:
    compiled = []
    for p in patterns:
        try:
            compiled.append(re.compile(p))
        except re.error as exc:
            logger.warning("Invalid privacy pattern %r: %s", p, exc)
    return compiled


def contains_sensitive_content(text: str, patterns: list[str] | None = None) -> bool:
    """Check if text contains any sensitive patterns.

    Scans only the first 10K chars for performance.
    """
    effective = patterns if patterns else DEFAULT_PATTERNS
    if not effective:
        return False
    sample = text[:10_000]
    compiled = _compile_patterns(tuple(effective))
    return any(p.search(sample) for p in compiled)
