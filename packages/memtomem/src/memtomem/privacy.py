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

import hashlib
import json
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


# ---------------------------------------------------------------------------
# JS-RegExp translation
# ---------------------------------------------------------------------------
#
# The Web UI's compose-mode privacy warning needs to scan textarea content
# client-side using the same patterns the server enforces. Python's ``re``
# and JavaScript's ``RegExp`` diverge on inline flag groups: ``(?i)foo``
# parses in Python but raises ``SyntaxError: Invalid group`` in JS. The
# translator below lifts a position-0 inline flag group into JS-style
# global flags and hard-rejects any construct it can't safely translate,
# so a silent semantic divergence cannot reach the client.

_LEADING_INLINE_FLAGS_RE = re.compile(r"^\(\?([imsux]+)\)")
_INLINE_FLAG_GROUP_RE = re.compile(r"\(\?[imsux]+\)")
_NAMED_GROUP_RE = re.compile(r"\(\?P<")
_INLINE_COMMENT_RE = re.compile(r"\(\?#")
_FLAG_NEGATION_RE = re.compile(r"\(\?[imsux]*-[imsux]+[:)]")
_PYTHON_ANCHOR_RE = re.compile(r"\\[AZ]")

# Map Python inline flag chars to ``re`` module flags so the translated
# body can be sanity-compiled. ``x`` (verbose) is intentionally absent —
# verbose mode strips whitespace + ``#`` comments and has no JS equivalent,
# so it's hard-rejected upstream.
_PY_FLAG_TO_RE = {
    "i": re.IGNORECASE,
    "m": re.MULTILINE,
    "s": re.DOTALL,
    "u": re.UNICODE,
}


def flags_str_to_re_flags(flags: str) -> int:
    out = 0
    for ch in flags:
        out |= _PY_FLAG_TO_RE.get(ch, 0)
    return out


def to_js_pattern(pat: str) -> tuple[str, str]:
    """Translate a Python regex string to a JS-RegExp ``(body, flags)`` pair.

    Translates only what ``DEFAULT_PATTERNS`` actually uses today: a
    position-0 inline flag group like ``(?i)foo`` or ``(?ims)foo`` is
    lifted into a flags string and stripped from the body. Everything
    else passes through unchanged.

    Hard-rejects (raises ``ValueError``) any construct whose JS semantics
    differ or don't exist:

    - **Mid-pattern inline flag groups** (anywhere except position 0).
      In Python, ``foo(?i)bar`` makes ``bar`` case-insensitive while
      ``foo`` stays sensitive — JS has no per-segment flag scope, so a
      naive lift would silently change semantics.
    - **Verbose mode** (``(?x)`` or any leading group containing ``x``).
      Verbose mode strips whitespace + ``#`` comments before matching,
      which the translator does not do.
    - **Inline flag negation** like ``(?-i)`` or ``(?i-m:...)`` —
      same per-segment-scope problem as mid-pattern lifts.
    - **Named groups** ``(?P<name>...)`` — JS uses ``(?<name>...)`` and
      a rewrite is not implemented (none of the current 9 patterns use
      named groups).
    - **Inline comments** ``(?#comment)`` — no JS equivalent.
    - **Python-only anchors** ``\\A`` and ``\\Z`` — use ``^`` / ``$``
      with the ``m`` flag in JS instead.

    Returns ``(body, flags)`` where ``flags`` is a (possibly empty) string
    of distinct chars from ``imsu``. The caller is responsible for
    feeding this into ``new RegExp(body, flags)``.

    Note on fail-loud-at-import: ``JS_PATTERNS`` below calls this for
    every entry in ``DEFAULT_PATTERNS`` at module import. Adding a
    pattern that this translator can't handle will break
    ``from memtomem import privacy`` — and therefore ``mm web`` startup,
    every test that imports privacy, and every MCP ``mem_add`` call.
    This is **intentional**: a silent client-warning bypass would be
    worse than a loud failure that forces the contributor to either
    translate the construct or accept the breakage. If you hit this
    while adding a pattern, extend ``to_js_pattern`` rather than
    suppressing the error.
    """
    if _PYTHON_ANCHOR_RE.search(pat):
        raise ValueError(
            f"Pattern {pat!r} uses Python-only construct: \\A or \\Z anchor "
            "(JS has no equivalent — use ^ / $ with the m flag)"
        )
    if _NAMED_GROUP_RE.search(pat):
        raise ValueError(
            f"Pattern {pat!r} uses Python-only construct: named group (?P<...>) "
            "(JS uses (?<...>); rewrite not implemented)"
        )
    if _INLINE_COMMENT_RE.search(pat):
        raise ValueError(f"Pattern {pat!r} uses Python-only construct: inline comment (?#...)")
    if _FLAG_NEGATION_RE.search(pat):
        raise ValueError(
            f"Pattern {pat!r} uses Python-only construct: inline flag negation "
            "(JS has no per-segment flag scope)"
        )

    body = pat
    flags = ""
    leading = _LEADING_INLINE_FLAGS_RE.match(pat)
    if leading:
        flag_chars = leading.group(1)
        if "x" in flag_chars:
            raise ValueError(
                f"Pattern {pat!r} uses Python-only construct: verbose mode (?x) "
                "(verbose mode strips whitespace and #-comments — JS has no equivalent)"
            )
        flags = "".join(sorted(set(flag_chars)))
        body = pat[leading.end() :]

    # Anything that still looks like an inline flag group is mid-pattern.
    # JS ``RegExp`` flags are global to the regex; lifting a mid-pattern
    # ``(?i)`` to a global flag would change semantics (the unflagged
    # prefix would also become case-insensitive).
    if _INLINE_FLAG_GROUP_RE.search(body):
        raise ValueError(
            f"Pattern {pat!r} uses Python-only construct: mid-pattern inline flag group "
            "(JS RegExp has no per-segment flag scope)"
        )

    # Sanity check: the translated body + lifted flags must still parse
    # as a valid Python regex. This is the translator's own contract,
    # not a JS-runtime check.
    try:
        re.compile(body, flags_str_to_re_flags(flags))
    except re.error as exc:  # pragma: no cover — defensive; current patterns all parse
        raise ValueError(
            f"Pattern {pat!r} translation produced invalid regex {body!r}: {exc}"
        ) from exc

    return body, flags


# Pre-computed JS-shape view of ``DEFAULT_PATTERNS`` and a stable hash over
# it. Both are computed once at import (the pattern tuple is immutable).
JS_PATTERNS: tuple[dict[str, str], ...] = tuple(
    {"pattern": body, "flags": flags}
    for body, flags in (to_js_pattern(p) for p in DEFAULT_PATTERNS)
)
JS_PATTERNS_SHA: str = hashlib.sha256(
    json.dumps(JS_PATTERNS, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


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
