"""Content redaction guard at the LTM trust boundary.

The pattern set is duplicated from memtomem-stm
``proxy/privacy.py:DEFAULT_PATTERNS`` as of commit ``a98636e``,
**secrets-only subset**. STM's full pattern set includes patterns whose
semantic category is PII (e.g., email addresses) rather than secret-class.
Those are excluded by design here because PII false positives on prose
ingress would be unworkable; redaction at the LTM ingress is the trust
boundary where blocking semantics demand a tight false-positive profile.

Sync rule (asymmetric):

- STM additions of secret-class patterns (provider tokens, key formats,
  PEM-style headers, etc.) require sync into this module + a SHA bump in
  this docstring.
- STM additions of PII-class patterns (email, phone, name, address, etc.)
  do NOT auto-sync. Including any new PII-class pattern here requires a
  separate decision pass — the false-positive profile in prose ingress is
  fundamentally different from STM's compression-routing use, and a PII
  block default would force ``force_unsafe=True`` on most legitimate
  contact / meeting / conversation notes.

This module is the LTM-side trust boundary. STM's content scanner is a
routing signal only; if STM is bypassed (direct agent → LTM call), the
redaction guard here still applies. STM-bypass is not safety-bypass.
"""

from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock

logger = logging.getLogger(__name__)

# Patterns are secret-class only. See module docstring for sync rule.
DEFAULT_PATTERNS: tuple[str, ...] = (
    r"(?i)(api[_-]?key|secret[_-]?key|access[_-]?token)\s*[:=]",
    r"(?i)(password|passwd|pwd)\s*[:=]",
    # Provider-prefixed token formats. Anchored by prefix so false positives
    # on arbitrary high-entropy strings are rare.
    r"(?i)(sk-[a-zA-Z0-9]{20,}|ghp_[a-zA-Z0-9]{36}|xox[bps]-[0-9A-Za-z-]+)",
    r"github_pat_[A-Za-z0-9_]{20,}",
    r"(?:(?:sk|pk|rk)_(?:live|test)|whsec)_[A-Za-z0-9]{20,}",
    r"\bnpm_[A-Za-z0-9]{20,}\b",
    r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    # JWT-ish: three base64url segments separated by dots, anchored to the
    # canonical ``eyJ`` header prefix to limit false positives on arbitrary
    # dotted identifiers.
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b",
    r"(?i)(BEGIN\s+(RSA|EC|OPENSSH|DSA|PGP)\s+PRIVATE\s+KEY)",
)

_SCAN_WINDOW = 10_000

# Outcome labels recorded for every gated content scan.
#   blocked  — at least one hit; ``force_unsafe`` not set; write rejected.
#   pass     — no hits; write proceeded.
#   bypassed — at least one hit; ``force_unsafe`` was set; write proceeded.
# The three-label split is the audit surface: "blocked" measures guard
# value, "bypassed" measures escape-hatch usage.
_VALID_OUTCOMES: tuple[str, ...] = ("blocked", "pass", "bypassed")


@dataclass(frozen=True)
class RedactionHit:
    """A single matched span.

    Original matched bytes are intentionally not retained — error messages
    and audit records must never echo secret content back to the caller.
    """

    pattern_index: int
    span: tuple[int, int]


@lru_cache(maxsize=1)
def _compile(patterns: tuple[str, ...]) -> tuple[re.Pattern[str], ...]:
    compiled: list[re.Pattern[str]] = []
    for p in patterns:
        try:
            compiled.append(re.compile(p))
        except re.error as exc:
            logger.warning("Invalid privacy pattern %r: %s", p, exc)
    return tuple(compiled)


def scan(text: str, patterns: tuple[str, ...] | None = None) -> list[RedactionHit]:
    """Return all redaction hits in the first ``_SCAN_WINDOW`` chars of ``text``.

    The 10 K-char window matches STM's compression-side scanner so the two
    views of "is this content sensitive" stay aligned at the floor.
    """
    effective = patterns if patterns is not None else DEFAULT_PATTERNS
    if not effective:
        return []
    sample = text[:_SCAN_WINDOW]
    compiled = _compile(tuple(effective))
    hits: list[RedactionHit] = []
    for idx, pat in enumerate(compiled):
        for m in pat.finditer(sample):
            hits.append(RedactionHit(pattern_index=idx, span=(m.start(), m.end())))
    return hits


_lock = Lock()
_outcomes: dict[str, int] = {o: 0 for o in _VALID_OUTCOMES}
_by_tool: dict[str, dict[str, int]] = defaultdict(lambda: {o: 0 for o in _VALID_OUTCOMES})


def record(outcome: str, tool: str) -> None:
    """Increment the outcome counter for ``tool``.

    ``outcome`` must be one of ``_VALID_OUTCOMES``. Unknown values are
    dropped with a warning so adding a new outcome name without updating
    the validator surfaces loudly rather than silently.
    """
    if outcome not in _VALID_OUTCOMES:
        logger.warning("privacy.record: unknown outcome %r (tool=%r); skipped", outcome, tool)
        return
    with _lock:
        _outcomes[outcome] += 1
        _by_tool[tool][outcome] += 1


def snapshot() -> dict[str, object]:
    """Return a deep-copied counter snapshot.

    Safe to mutate or serialise without affecting the live counters.
    """
    with _lock:
        return {
            "outcomes": dict(_outcomes),
            "by_tool": {tool: dict(counts) for tool, counts in _by_tool.items()},
        }


def reset_for_tests() -> None:
    """Zero all counters. Production code does not call this."""
    with _lock:
        for o in _VALID_OUTCOMES:
            _outcomes[o] = 0
        _by_tool.clear()
