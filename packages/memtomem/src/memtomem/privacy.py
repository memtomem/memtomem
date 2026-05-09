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

ADR-0011 outcomes:

- ``blocked_project_shared`` is LTM-only. STM has no scope axis (every
  STM-routed write lands in user-tier-equivalent storage), so this
  outcome does **not** auto-sync upstream. Future maintainers should
  not extend STM's privacy.py with a mirror outcome — keep STM and LTM
  on separate decision vocabularies for this dimension.
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


def _has_unescaped_python_anchor(pat: str) -> bool:
    r"""True iff ``pat`` contains an unescaped ``\A`` or ``\Z`` anchor.

    A run of consecutive backslashes immediately before ``A`` or ``Z`` is
    "active" (the final ``\`` escapes the next char) iff its length is odd.
    Even-length runs are all ``\\`` literal-backslash pairs and leave the
    next char unescaped — so ``r"foo\\Abar"`` (run of 2) is literal text
    ``foo\Abar`` with no anchor, while ``r"foo\\\Abar"`` (run of 3) is a
    literal ``\`` followed by the real ``\A`` anchor.

    Character-class context (e.g. ``[\A]``) is intentionally out of scope
    (issue #594): this helper checks top-level escape state only. Python's
    ``re`` rejects ``[\A]`` at compile time anyway, and a secret-class
    regex putting ``\A`` inside ``[…]`` is implausible.
    """
    i, n = 0, len(pat)
    while i < n:
        if pat[i] != "\\":
            i += 1
            continue
        j = i
        while j < n and pat[j] == "\\":
            j += 1
        if (j - i) % 2 == 1 and j < n and pat[j] in ("A", "Z"):
            return True
        i = j
    return False


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
    if _has_unescaped_python_anchor(pat):
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


# Outcome labels recorded for every gated content scan.
#   blocked                  — at least one hit; ``force_unsafe`` not set; write rejected.
#   pass                     — no hits; write proceeded.
#   bypassed                 — at least one hit; ``force_unsafe=True``; write proceeded.
#   blocked_project_shared   — ``force_unsafe=True`` attempted on a write
#                               whose resolved scope is ``project_shared``.
#                               Hard refusal: the bypass valve does NOT
#                               apply to git-tracked tiers because git
#                               history retracts to no clone, ever.
#                               LTM-only outcome — does NOT auto-sync to
#                               STM (STM has no scope axis); see module
#                               docstring "Sync rule (asymmetric)".
# The four-label split is the audit surface: "blocked" measures guard
# value on user-tier writes, "bypassed" measures escape-hatch usage,
# "blocked_project_shared" measures attempted bypass into git history.
_VALID_OUTCOMES: tuple[str, ...] = ("blocked", "pass", "bypassed", "blocked_project_shared")


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
    """Return all redaction hits in ``text``.

    Scans the entire string. An earlier revision capped at the first 10 K
    chars to mirror STM's compression-side scanner, but at the LTM trust
    boundary that cap is a silent bypass: a secret pasted past the 10 K
    mark wrote through unredacted. The asymmetry with STM is intentional
    and one-directional — STM's window is a compression-routing signal
    (does this block contain anything sensitive enough to skip), while
    the LTM scan is a write-rejection gate. The two contracts diverge,
    and the trust boundary lives here.

    All current ``DEFAULT_PATTERNS`` are simple short regexes (provider
    tokens, PEM headers, etc.); ``re.finditer`` runs in linear time and
    a 1 MB input stays comfortably under the perf ceiling pinned by
    ``test_privacy_long_content``. A future quadratic regression in the
    pattern set or scan implementation fails that test loudly.
    """
    effective = patterns if patterns is not None else DEFAULT_PATTERNS
    if not effective:
        return []
    compiled = _compile(tuple(effective))
    hits: list[RedactionHit] = []
    for idx, pat in enumerate(compiled):
        for m in pat.finditer(text):
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


@dataclass(frozen=True)
class WriteGuardResult:
    """Outcome of a single ``enforce_write_guard`` call.

    ``decision`` is one of ``_VALID_OUTCOMES``. ``hits`` is the raw
    ``scan()`` result so callers can size-quote it in user-facing
    errors (length only, never the matched bytes).
    """

    decision: str
    hits: list[RedactionHit]


_AUDIT_VALUE_MAX_LEN = 200
_AUDIT_REDACTED_MARKER = "<redacted: secret-shape>"


def _sanitize_audit_value(value: object) -> object:
    """Strip secret-shaped substrings from a single audit-context value.

    The helper's audit log emits ``audit_context`` after ``surface=`` so
    operators can correlate a bypass with the request shape (path, key,
    namespace, etc). Several callers pass user-controllable strings —
    file paths, upload filenames, scratch keys — and a bypass that
    happens to embed the same secret in those fields would otherwise
    leak it through the log line.

    Non-string values (``None``, ``int``, ``bool``) pass through. Strings
    are re-scanned with the same ``DEFAULT_PATTERNS`` used for content;
    any hit replaces the value entirely with a fixed marker, and very
    long strings are truncated so a multi-megabyte path can't blow up
    the audit line either.
    """
    if not isinstance(value, str):
        return value
    if scan(value):
        return _AUDIT_REDACTED_MARKER
    if len(value) > _AUDIT_VALUE_MAX_LEN:
        return value[:_AUDIT_VALUE_MAX_LEN] + "...(truncated)"
    return value


def emit_bypass_audit(
    *,
    surface: str,
    content_chars: int,
    hits: int,
    audit_context: dict[str, object] | None = None,
) -> None:
    """Emit a structured ``redaction bypass`` warning with sanitized context.

    Public seam for callers that pre-scan their own content and cannot
    route through :func:`enforce_write_guard` — currently
    ``mem_batch_add``'s transactional path, which decides per-item
    bypass after a whole-batch hit-collection pass and therefore
    can't take :class:`WriteGuardResult` 's single-content shape.
    Funnelling that callsite through this helper instead of an ad-hoc
    ``logger.warning`` keeps the "matched bytes never reach logs"
    invariant in one place: every audit value passes through
    :func:`_sanitize_audit_value` first.

    The corresponding counter (:func:`record`) is intentionally **not**
    bumped here — callers that have their own per-item bookkeeping
    (again, ``mem_batch_add``) own the counter contract and would
    double-count if this helper also recorded.
    """
    if audit_context:
        sanitized = {k: _sanitize_audit_value(v) for k, v in audit_context.items()}
        ctx_pairs = ", " + ", ".join(f"{k}={v!r}" for k, v in sanitized.items())
    else:
        ctx_pairs = ""
    logger.warning(
        "redaction bypass via force_unsafe=True (surface=%s%s, content_chars=%d, hits=%d)",
        surface,
        ctx_pairs,
        content_chars,
        hits,
    )


def enforce_write_guard(
    content: str,
    *,
    surface: str,
    force_unsafe: bool = False,
    scope: str = "user",
    audit_context: dict[str, object] | None = None,
    record_outcome: bool = True,
) -> WriteGuardResult:
    """Trust-boundary content scan + counter increment + audit log.

    Centralises the redaction guard so every ingress surface (MCP
    ``mem_add`` / ``mem_edit``, Web ``POST /api/add`` / ``POST /upload``
    / ``PATCH /chunks/{id}`` / scratch promote, CLI ``mm add`` / shell
    ``add`` / agent share, and the LangGraph integration) shares one
    scan-decide-log shape. ``surface`` is the audit identifier passed
    to ``record()``.

    On a hit:

    - ``force_unsafe=True`` records ``"bypassed"`` and emits a
      structured audit log line. Extra request shape (namespace, file,
      route, item_idx, …) is rendered from ``audit_context`` as
      ``key=value`` pairs **after** the ``surface=`` field. Each
      audit value is run through ``_sanitize_audit_value`` first so a
      secret embedded in a user-controlled string field (path,
      filename, key) cannot leak through the bypass log either —
      matched bytes never reach error messages, audit lines, or
      response bodies.
    - ``force_unsafe=False`` records ``"blocked"`` and returns a
      result whose ``decision == "blocked"``. The caller picks the
      user-facing error message (HTTP 403 / CLI exception / MCP error
      string) so each surface keeps its native error shape.

    Scope-aware refusal (ADR-0011 §5 Gate A):

    - When ``scope == "project_shared"`` and ``force_unsafe=True``,
      the bypass valve is hard-refused: the call records
      ``"blocked_project_shared"`` and returns a result whose
      ``decision == "blocked_project_shared"``. The audit log is
      still emitted (so SOC pipelines can alert on the attempt) but
      the write does NOT proceed. Rationale: ``project_shared``
      content goes into git history; even an instant ``git rm``
      cannot retract it from any clone or reflog, so the trust
      boundary moves from the user's machine to every clone of the
      repo forever. The user-facing path to write a force-unsafe
      memory is ``scope="project_local"`` (gitignored) or
      ``scope="user"`` (private to the user).

    Transactional callers (``mem_batch_add``) pass
    ``record_outcome=False`` so the per-entry decisions can be
    collected without committing counters; the caller then runs
    :func:`record` once it has decided whether to commit the whole
    batch. This preserves the "no pass record on rejected batch"
    invariant the batch path relies on for clean audit semantics.
    """
    hits = scan(content)
    if not hits:
        if record_outcome:
            record("pass", surface)
        return WriteGuardResult("pass", [])
    if force_unsafe and scope == "project_shared":
        if record_outcome:
            record("blocked_project_shared", surface)
            emit_bypass_audit(
                surface=surface,
                content_chars=len(content),
                hits=len(hits),
                audit_context={**(audit_context or {}), "blocked_scope": "project_shared"},
            )
        return WriteGuardResult("blocked_project_shared", hits)
    if force_unsafe:
        if record_outcome:
            record("bypassed", surface)
            emit_bypass_audit(
                surface=surface,
                content_chars=len(content),
                hits=len(hits),
                audit_context=audit_context,
            )
        return WriteGuardResult("bypassed", hits)
    if record_outcome:
        record("blocked", surface)
    return WriteGuardResult("blocked", hits)
