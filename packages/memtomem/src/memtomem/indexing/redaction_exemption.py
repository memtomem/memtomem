"""Per-file redaction exemption declared in Markdown frontmatter (#2076).

ADR-0006 Axis E.5. A note that *documents* the redaction patterns — writing
``api_key=`` or ``password:`` in prose — trips the guard's keyword-anchored
label rules and is silently skipped by the indexer, leaving its chunks frozen
at the last successful run. ``--force-unsafe`` is an invocation-scoped valve
that the watcher, the debounce drain and ``mem_index`` cannot reach at all.

The middle ground is a declaration the file itself carries::

    ---
    redaction: documents-patterns
    ---

It is deliberately *narrow* on three axes:

- **Markdown only.** The declaration lives in the chunker's frontmatter block,
  and only ``.md`` files have one. A ``.yaml`` config whose first lines happen
  to look like frontmatter gets nothing.
- **Exact literal, top-level, fail closed.** See :func:`declared_exemption`.
- **Label hits only.** :func:`memtomem.privacy.exemption_covers` decides what
  the declaration may waive; a real token or PEM block re-blocks the file even
  when declared. That bound is what keeps a *standing* exemption from becoming
  a silent ingest path for a secret pasted in later.

``project_shared`` files are hard-refused regardless (ADR-0011 §5).
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from memtomem import privacy
from memtomem.chunking.markdown import _FRONT_MATTER_RE

logger = logging.getLogger(__name__)

#: Suffixes whose chunker owns a frontmatter block.
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

#: The one recognised declaration. Anchored per line with ``re.MULTILINE`` and
#: **unindented** on purpose: ``line.strip()`` would accept the key nested in a
#: block scalar (``description: |`` + two spaces + ``redaction: ...``) or in a
#: nested mapping, letting arbitrary prose inside another field forge the
#: declaration. Only a real top-level YAML key can match.
_DECLARATION_RE = re.compile(
    r"^redaction:[ \t]*" + re.escape(privacy.DECLARED_EXEMPTION_DOCUMENTS_PATTERNS) + r"[ \t]*$",
    re.MULTILINE,
)

#: Any top-level ``redaction:`` key, recognised so an unusable value can be
#: reported rather than ignored in silence, and so duplicates fail closed.
_ANY_DECLARATION_RE = re.compile(r"^redaction:[ \t]*(?P<value>.*?)[ \t]*$", re.MULTILINE)


def declared_exemption(path: Path, content: str) -> str | None:
    """Return the exemption ``path`` declares, or ``None``.

    ``None`` — the fail-closed answer — is returned for every case that is
    not exactly one unindented top-level ``redaction: documents-patterns``
    line inside the leading frontmatter block: a non-Markdown suffix, no
    frontmatter, an indented or quoted or commented key, a key nested under
    another field, an unrecognised value, or two ``redaction:`` keys
    (ambiguous — YAML's last-wins is not a rule to guess at when the answer
    gates a secret scan).

    An unrecognised value is logged, but only as fixed text plus lengths:
    the value could itself be a credential, and this module must not become
    the one place a secret reaches the log.
    """
    if path.suffix.lower() not in _MARKDOWN_SUFFIXES:
        return None
    # Normalise the two shapes the frontmatter regex would otherwise miss.
    # Anything beyond a BOM and CRLF is left alone: tolerating more here
    # would widen what counts as a declaration.
    normalized = content.lstrip("﻿").replace("\r\n", "\n")
    match = _FRONT_MATTER_RE.match(normalized)
    if match is None:
        return None
    block = match.group(1)
    keys = _ANY_DECLARATION_RE.findall(block)
    if not keys:
        return None
    if len(keys) > 1:
        logger.warning(
            "redaction: %d top-level declarations in %s — ambiguous, ignored",
            len(keys),
            privacy.sanitize_audit_value(str(path)),
        )
        return None
    if _DECLARATION_RE.search(block) is None:
        logger.warning(
            "redaction: unrecognised declaration in %s (value_chars=%d) — ignored; "
            "the only recognised value is %r",
            privacy.sanitize_audit_value(str(path)),
            len(keys[0]),
            privacy.DECLARED_EXEMPTION_DOCUMENTS_PATTERNS,
        )
        return None
    return privacy.DECLARED_EXEMPTION_DOCUMENTS_PATTERNS


__all__ = ["declared_exemption"]
