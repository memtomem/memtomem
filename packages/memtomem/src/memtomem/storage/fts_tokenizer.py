"""FTS5 pre-tokenizer with pluggable backends.

Supports two tokenizer backends:
- ``unicode61`` (default): FTS5's built-in tokenizer. Zero dependencies.
- ``kiwipiepy``: Korean morphological analyzer. Requires ``pip install kiwipiepy``.

The tokenizer backend is selected at module level via ``set_tokenizer()``.
"""

from __future__ import annotations

import logging
import re

_log = logging.getLogger(__name__)

# FTS5 barewords are intentionally narrow. Quote any query token containing
# punctuation so paths, URLs, YAML, code spans, and future FTS5 operators stay
# literal instead of being parsed as query syntax.
_FTS5_SPECIAL_RE = re.compile(r"[^\w]", re.UNICODE)

# Active tokenizer backend: "unicode61" or "kiwipiepy"
_active_tokenizer: str = "unicode61"

# Lazy-loaded Kiwi instance
_kiwi_instance = None


def set_tokenizer(name: str) -> None:
    """Set the active tokenizer backend."""
    global _active_tokenizer, _kiwi_instance
    if name not in ("unicode61", "kiwipiepy"):
        raise ValueError(f"Unknown tokenizer: {name!r}. Use 'unicode61' or 'kiwipiepy'.")
    _active_tokenizer = name
    _kiwi_instance = None  # reset lazy instance


def get_tokenizer() -> str:
    """Return the name of the active tokenizer."""
    return _active_tokenizer


def _get_kiwi():
    """Lazy-load a Kiwi instance."""
    global _kiwi_instance
    if _kiwi_instance is None:
        try:
            from kiwipiepy import Kiwi

            _kiwi_instance = Kiwi()
            _log.info("kiwipiepy tokenizer loaded successfully")
        except ImportError:
            _log.warning(
                "kiwipiepy not installed — falling back to unicode61. "
                "Install with: pip install kiwipiepy"
            )
            set_tokenizer("unicode61")
            return None
    return _kiwi_instance


def _kiwi_tokenize(text: str) -> list[str]:
    """Tokenize text using kiwipiepy morphological analysis."""
    kiwi = _get_kiwi()
    if kiwi is None:
        return text.split()

    tokens = []
    for token in kiwi.tokenize(text):
        form = token.form.strip()
        if not form:
            continue
        # Skip punctuation-only tokens
        if all(c in ".,!?;:()[]{}\"'…·-—_/\\@#$%^&*~`<>|" for c in form):
            continue
        tokens.append(form)
    return tokens


def tokenize_for_fts(
    text: str,
    *,
    for_query: bool = False,
    use_or: bool = False,
) -> str:
    """Tokenize *text* for FTS5 insertion or query.

    - ``unicode61`` backend: returns text unchanged (insertion) or with
      prefix wildcards (query).
    - ``kiwipiepy`` backend: morphological analysis for both insertion
      and query. Produces space-separated token sequence.

    When *use_or* is True, query terms are joined with ``OR`` instead of
    the default AND (implicit space) so partial-match queries still return
    results.
    """
    if not text:
        return text

    if _active_tokenizer == "kiwipiepy":
        tokens = _kiwi_tokenize(text)
        if for_query:
            parts = [_format_query_token(t) for t in tokens]
            joiner = " OR " if use_or else " "
            return joiner.join(parts)
        return " ".join(tokens)

    # unicode61: pass-through for insertion, prefix-wildcard for query
    if for_query:
        return _apply_prefix_wildcard(text, use_or=use_or)
    return text


def _apply_prefix_wildcard(text: str, *, use_or: bool = False) -> str:
    """Append ``*`` to each word for FTS5 prefix matching.

    Words containing FTS5 special characters (like hyphens) are wrapped
    in double-quotes to prevent FTS5 from interpreting them as operators.
    """
    parts: list[str] = []
    for word in text.split():
        parts.append(_format_query_token(word))
    joiner = " OR " if use_or else " "
    return joiner.join(parts)


def _format_query_token(word: str) -> str:
    """Return one FTS5-safe query token."""
    if _FTS5_SPECIAL_RE.search(word):
        safe = word.replace('"', '""')
        return f'"{safe}"'
    return word + "*"
