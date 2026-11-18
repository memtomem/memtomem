"""FTS5 pre-tokenizer with pluggable backends.

Supports two tokenizer backends:
- ``unicode61`` (default): FTS5's built-in tokenizer. Zero dependencies.
- ``kiwipiepy``: Korean morphological analyzer. Requires ``pip install kiwipiepy``.

Nothing here selects a backend. Each caller names one: a storage backend
carries its tokenizer as an instance attribute (``SqliteBackend.fts_tokenizer``,
from ``search.tokenizer``) and passes it on every call, so two component stacks
in one process tokenize their own databases their own way (#3018). The module
keeps only what is genuinely process-wide: the loaded ``Kiwi`` analyser, and
whether ``kiwipiepy`` failed to import here.
"""

from __future__ import annotations

import logging
import re

_log = logging.getLogger(__name__)

# FTS5 barewords are intentionally narrow. Quote any query token containing
# punctuation so paths, URLs, YAML, code spans, and future FTS5 operators stay
# literal instead of being parsed as query syntax.
_FTS5_SPECIAL_RE = re.compile(r"[^\w]", re.UNICODE)

# FTS5 reads these barewords as operators, and only in uppercase (``and``,
# ``Or``, ``NEAR*`` are plain terms). A bare ``NOT*`` is an operator followed
# by a stray ``*`` and fails the whole MATCH (#2576), so they are quoted.
_FTS5_KEYWORDS = frozenset({"AND", "OR", "NOT"})

TOKENIZERS = ("unicode61", "kiwipiepy")

# The loaded analyser, shared by every backend that tokenizes with kiwipiepy:
# it is expensive to build and holds no per-database state.
_kiwi_instance = None

# Set once ``kiwipiepy`` fails to import. Import availability belongs to the
# process (one ``sys.path``), and it is sticky like the fallback it reports
# (#2647): installing the extra later does not undo rows already written.
_kiwi_unavailable = False


def validate_tokenizer(name: str) -> str:
    """Return *name* if it names a tokenizer backend, else raise ``ValueError``."""
    if name not in TOKENIZERS:
        raise ValueError(f"Unknown tokenizer: {name!r}. Use 'unicode61' or 'kiwipiepy'.")
    return name


def effective_tokenizer(name: str) -> str:
    """The backend a call naming *name* actually tokenizes with in this process.

    ``kiwipiepy`` reads as ``unicode61`` once its import has failed here. Before
    the first kiwipiepy call nothing has tried the import, so it still reads as
    ``kiwipiepy`` — the "pending" state ``mem_status`` reports separately.
    """
    if name == "kiwipiepy" and _kiwi_unavailable:
        return "unicode61"
    return name


def _get_kiwi():
    """Lazy-load the shared Kiwi instance; ``None`` if kiwipiepy cannot be imported."""
    global _kiwi_instance, _kiwi_unavailable
    if _kiwi_unavailable:
        return None
    if _kiwi_instance is None:
        try:
            from kiwipiepy import Kiwi

            _kiwi_instance = Kiwi()
            _log.info("kiwipiepy tokenizer loaded successfully")
        except ImportError:
            _log.warning(
                "kiwipiepy not installed — falling back to unicode61. Install the "
                "korean extra where memtomem runs (memtomem[korean]) and restart; "
                "mem_status shows how to rebuild the rows written meanwhile"
            )
            _kiwi_unavailable = True
            return None
    return _kiwi_instance


def _kiwi_tokenize(text: str) -> list[str]:
    """Tokenize text using kiwipiepy morphological analysis (whitespace split without it)."""
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
    tokenizer: str = "unicode61",
) -> str:
    """Tokenize *text* for FTS5 insertion or query.

    - ``unicode61`` backend: returns text unchanged (insertion) or with
      prefix wildcards (query).
    - ``kiwipiepy`` backend: morphological analysis for both insertion
      and query. Produces space-separated token sequence.

    When *use_or* is True, query terms are joined with ``OR`` instead of
    the default AND (implicit space) so partial-match queries still return
    results.

    *tokenizer* names the backend; production callers always pass it (a
    guard in ``test_fts_tokenizer_instance.py`` pins that), so the default
    only serves direct calls in tests and notebooks. A ``kiwipiepy`` call in
    a process where kiwipiepy cannot be imported tokenizes as ``unicode61``.
    """
    if not text:
        return text

    validate_tokenizer(tokenizer)
    if tokenizer == "kiwipiepy" and _get_kiwi() is not None:
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
    if word in _FTS5_KEYWORDS:
        # Quoted, the keyword is a literal term; the trailing ``*`` keeps the
        # same prefix match its lowercase spelling gets.
        return f'"{word}"*'
    return word + "*"
