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

- **Markdown only.** The declaration lives in a frontmatter block, and only
  Markdown files have one. A ``.yaml`` config whose first lines happen to look
  like frontmatter gets nothing.
- **Exact literal, top-level, fail closed.** See :func:`declared_exemption`.
- **Label hits only.** :func:`memtomem.privacy.exemption_covers` decides what
  the declaration may waive; a real token or PEM block re-blocks the file even
  when declared. That bound is what keeps a *standing* exemption from becoming
  a silent ingest path for a secret pasted in later.

``project_shared`` files are hard-refused regardless (ADR-0011 §5).

**Why this parses YAML instead of matching lines.** The first revision matched
``^redaction: documents-patterns$`` with ``re.MULTILINE``, reasoning that an
unindented line cannot be nested. It can: a multi-line double-quoted scalar
carries its continuation lines at column zero, so

.. code-block:: yaml

    description: "first line
    redaction: documents-patterns
    last line"
    password: hunter2

has **no** top-level ``redaction`` key, and the regex accepted it anyway —
letting arbitrary prose inside another field forge a declaration for the file
containing it. That is a recognition problem against a specified grammar, and
a pattern will keep losing to the spec; the block is YAML, so YAML reads it.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import yaml

from memtomem import privacy

logger = logging.getLogger(__name__)

#: Suffixes whose chunker owns a frontmatter block.
_MARKDOWN_SUFFIXES = frozenset({".md", ".markdown"})

#: The frontmatter block: an opening ``---`` line at the very start, and a
#: closing line that is exactly ``---`` or ``...``. Deliberately *not* the
#: chunker's ``_FRONT_MATTER_RE``, which does not anchor the closing
#: delimiter to end-of-line and so accepts ``---not-a-delimiter`` as a close.
#: A permissive reader is right for chunking (recover what you can) and wrong
#: here (a file with no real frontmatter must not be able to declare).
_FRONT_MATTER_RE = re.compile(
    r"\A---[ \t]*\n(?P<body>.*?)^(?:---|\.\.\.)[ \t]*(?:\n|\Z)", re.DOTALL | re.MULTILINE
)

_KEY = "redaction"


def declared_exemption(path: Path, content: str) -> str | None:
    """Return the exemption ``path`` declares, or ``None``.

    ``None`` — the fail-closed answer — is returned unless the leading
    frontmatter block parses as a YAML mapping carrying exactly one top-level
    ``redaction`` key whose value is the plain (unquoted) scalar
    ``documents-patterns``. Everything else declares nothing: a non-Markdown
    suffix, a missing or unterminated block, malformed YAML, a key nested
    under another field or inside a quoted scalar, a quoted value, a
    duplicated key, or any other value.

    Quoted values are refused on purpose. Nothing about ``"documents-patterns"``
    is less valid as YAML — but a declaration should be one exact literal, and
    accepting its quoted spellings widens the shapes a reviewer has to
    recognise for no gain.

    An unrecognised value is logged, but only as fixed text plus lengths: the
    value could itself be a credential, and this module must not become the
    one place a secret reaches the log.
    """
    if path.suffix.lower() not in _MARKDOWN_SUFFIXES:
        return None
    # Normalise the two shapes that are encoding rather than content. Anything
    # further is left alone: tolerating more here widens what can declare.
    normalized = content.lstrip("﻿").replace("\r\n", "\n")
    match = _FRONT_MATTER_RE.match(normalized)
    if match is None:
        return None
    try:
        # ``compose`` rather than ``safe_load``: the node tree keeps the two
        # facts a loaded dict throws away — how many times a key appears
        # (PyYAML silently keeps the last) and whether a scalar was quoted.
        root = yaml.compose(match.group("body"), Loader=yaml.SafeLoader)
    except yaml.YAMLError:
        # Malformed frontmatter declares nothing. The chunker still indexes
        # such a file; it just does not get an exemption.
        return None
    if not isinstance(root, yaml.MappingNode):
        return None

    values = [
        value for key, value in root.value if isinstance(key, yaml.ScalarNode) and key.value == _KEY
    ]
    if not values:
        return None
    if len(values) > 1:
        # PyYAML's last-wins is not a rule worth inheriting when the answer
        # gates a secret scan.
        logger.warning(
            "redaction: %d top-level declarations in %s — ambiguous, ignored",
            len(values),
            privacy.sanitize_audit_value(str(path)),
        )
        return None

    node = values[0]
    plain_scalar = isinstance(node, yaml.ScalarNode) and node.style is None
    if plain_scalar and node.value == privacy.DECLARED_EXEMPTION_DOCUMENTS_PATTERNS:
        return privacy.DECLARED_EXEMPTION_DOCUMENTS_PATTERNS

    logger.warning(
        "redaction: unrecognised declaration in %s (value_chars=%d) — ignored; "
        "the only recognised value is the plain scalar %r",
        privacy.sanitize_audit_value(str(path)),
        len(node.value) if isinstance(node, yaml.ScalarNode) else 0,
        privacy.DECLARED_EXEMPTION_DOCUMENTS_PATTERNS,
    )
    return None


__all__ = ["declared_exemption"]
