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

**Why this reads YAML events.** Two earlier revisions were both too generous,
in the same way: they judged the declaration by what it *resolved to* rather
than by what was *written*.

A line pattern (``^redaction: documents-patterns$`` with ``re.MULTILINE``)
assumed an unindented line cannot be nested. It can — a multi-line
double-quoted scalar carries its continuation lines at column zero::

    description: "first line
    redaction: documents-patterns
    last line"
    password: hunter2

There is no top-level ``redaction`` key there at all, yet the pattern accepted
it, letting prose inside another field forge a declaration for its own file.

Reading the composed node tree fixed that and introduced the mirror problem:
``compose`` resolves aliases and escapes before anything can look, so
``keyname: &k redaction`` + ``*k: documents-patterns``, ``redaction: *v``
pointing at a value defined elsewhere, and ``"\x72edaction"`` all arrived
looking exactly like the literal.

So the declaration is recognised from the **parser event stream**, where an
alias is an ``AliasEvent`` and a quoted scalar carries its style. The contract
is lexical, which is the point: the declaration has to be the one shape a
reviewer opening the file can see, not any shape that happens to evaluate to
it.
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

#: Opening delimiter. The *closing* one is not a regex: see
#: :func:`_frontmatter_block`.
_OPEN_RE = re.compile(r"\A---[ \t]*\n")

_KEY = "redaction"


def _frontmatter_block(text: str) -> str | None:
    """Return the frontmatter body, or ``None`` if there isn't one.

    The closing rule is the chunker's, deliberately: ``chunking/markdown.py``
    ends the block at the **first** line beginning with ``---``, so
    ``---body: value`` closes it there. A reader that instead scanned on to
    the next *exact* ``---`` would disagree with the rest of the codebase
    about where the file's frontmatter stops — and everything between the two
    boundaries is body text as far as every other reader is concerned, which
    is precisely where a declaration must not be honoured.

    Where this parser is stricter: that first ``---``-prefixed line must be a
    complete delimiter. The chunker accepts ``---anything`` as a close because
    recovering what it can is right for chunking; here it would let a file
    with no real frontmatter declare, so a non-canonical close means no block.
    """
    if _OPEN_RE.match(text) is None:
        return None
    lines = text.split("\n")[1:]
    for i, line in enumerate(lines):
        if line.startswith("---"):
            if line.rstrip(" \t") != "---":
                return None
            return "\n".join(lines[:i])
    return None


def _is_bare_plain_scalar(event: object, value: str) -> bool:
    """True when ``event`` is the literal ``value``, written plainly.

    "Plainly" is four conditions, and dropping any one of them re-opens a
    forgery this module has already shipped a fix for: a plain ``style`` (no
    quoting or escapes), no explicit ``tag`` (``!!str redaction`` resolves to
    the same string), no ``anchor`` (an anchored scalar can be aliased into
    the declaration position from elsewhere in the block), and the exact
    value.
    """
    return (
        isinstance(event, yaml.ScalarEvent)
        and event.style is None
        and event.tag is None
        and event.anchor is None
        and event.value == value
    )


def _is_single_valid_mapping_document(block: str) -> bool:
    """True when ``block`` is exactly one YAML document holding a mapping.

    ``yaml.parse`` checks syntax only: an alias with no anchor
    (``meta: *missing``), a merge key pointing at nothing, or a duplicated
    anchor all yield a clean event stream and would leave the walker happily
    reading a declaration out of a document no loader could construct. The
    documented contract is that malformed frontmatter declares nothing, so
    composition — which resolves anchors and would reject all three — is the
    thing that decides whether the block counts as well-formed.
    """
    try:
        documents = list(yaml.compose_all(block, Loader=yaml.SafeLoader))
    except (yaml.YAMLError, RecursionError):
        return False
    return len(documents) == 1 and isinstance(documents[0], yaml.MappingNode)


def _top_level_declaration_values(block: str) -> list[object] | None:
    """Return the value events of every top-level ``redaction`` key.

    ``None`` means the block is not a mapping this module will read at all
    (malformed YAML, a scalar or sequence document, or a parser that gave up).
    An empty list means a well-formed mapping with no such key.

    Walks ``yaml.parse`` rather than ``yaml.compose`` so the *written* form
    survives: an alias is an ``AliasEvent`` here instead of silently resolving
    to the node it points at, and a quoted or escaped scalar keeps its
    ``style``. Only depth-1 entries are considered, and the key must start at
    column 0 — which is what the documented "top-level, unindented" contract
    says, and is stricter than YAML alone (a uniformly indented block mapping
    is also root-level to YAML, but is not the shape this feature documents).
    """
    try:
        events = list(yaml.parse(block, Loader=yaml.SafeLoader))
    except yaml.YAMLError:
        return None
    except RecursionError:
        # Deeply nested frontmatter blows PyYAML's recursive parser before it
        # can raise a YAMLError. A gate must answer "no", not propagate and
        # fail the whole indexing run for a file it merely could not read.
        logger.warning("redaction: frontmatter too deeply nested to parse — ignored")
        return None

    stream = iter(events)
    for event in stream:
        if isinstance(event, (yaml.ScalarEvent, yaml.AliasEvent, yaml.SequenceStartEvent)):
            return None  # a scalar or sequence document declares nothing
        if isinstance(event, yaml.MappingStartEvent):
            break
    else:
        return None

    values: list[object] = []
    depth = 1
    expect_key = True
    container_role: str | None = None
    pending_is_declaration = False

    for event in stream:
        if isinstance(event, (yaml.MappingStartEvent, yaml.SequenceStartEvent)):
            depth += 1
            if depth == 2:
                # Remember whether this collection sits in key or value
                # position so the depth-1 alternation resumes correctly when
                # it closes.
                container_role = "key" if expect_key else "value"
                if not expect_key:
                    pending_is_declaration = False
            continue
        if isinstance(event, (yaml.MappingEndEvent, yaml.SequenceEndEvent)):
            depth -= 1
            if depth == 1:
                # A collection that sat in *value* position is followed by the
                # next key; one that sat in key position is followed by its
                # own value. Getting this backwards shifts the whole depth-1
                # alternation, which silently misreads every entry after the
                # first ``tags: [...]`` a real note carries.
                expect_key = container_role == "value"
                container_role = None
            elif depth == 0:
                break
            continue
        if not isinstance(event, (yaml.ScalarEvent, yaml.AliasEvent)):
            continue  # document/stream markers
        if depth != 1:
            continue
        if expect_key:
            pending_is_declaration = (
                _is_bare_plain_scalar(event, _KEY) and event.start_mark.column == 0
            )
            expect_key = False
        else:
            if pending_is_declaration:
                values.append(event)
            pending_is_declaration = False
            expect_key = True
    return values


def declared_exemption(path: Path, content: str) -> str | None:
    """Return the exemption ``path`` declares, or ``None``.

    ``None`` — the fail-closed answer — is returned unless the leading
    frontmatter block is a YAML mapping carrying exactly one top-level,
    column-0, plain (unquoted) ``redaction`` key whose value is the plain
    scalar ``documents-patterns``, written literally. Other top-level keys
    (``tags``, ``valid_from``, …) are fine alongside it; real notes have them,
    and they cannot affect this entry.

    Everything else declares nothing: a non-Markdown suffix, a missing or
    unterminated block, malformed or unparseably deep YAML, a key nested under
    another field or inside a quoted scalar, an indented key, a quoted or
    escaped key or value, an **alias** on either side, a duplicated key, or
    any other value.

    Quoted and aliased spellings are refused even though YAML makes them
    equivalent. The declaration's whole job is to be the one shape a reviewer
    opening the file can recognise; accepting every construction that
    *evaluates* to it defeats that for no gain.

    An unrecognised value is logged, but only as fixed text plus lengths: the
    value could itself be a credential, and this module must not become the
    one place a secret reaches the log.
    """
    if path.suffix.lower() not in _MARKDOWN_SUFFIXES:
        return None
    # Normalise the two shapes that are encoding rather than content. Anything
    # further is left alone: tolerating more here widens what can declare.
    normalized = content.lstrip("\ufeff").replace("\r\n", "\n")
    block = _frontmatter_block(normalized)
    if block is None or not _is_single_valid_mapping_document(block):
        return None

    values = _top_level_declaration_values(block)
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

    event = values[0]
    if _is_bare_plain_scalar(event, privacy.DECLARED_EXEMPTION_DOCUMENTS_PATTERNS):
        return privacy.DECLARED_EXEMPTION_DOCUMENTS_PATTERNS

    logger.warning(
        "redaction: unrecognised declaration in %s (value_chars=%d) — ignored; "
        "the only recognised value is the plain literal %r",
        privacy.sanitize_audit_value(str(path)),
        len(event.value) if isinstance(event, yaml.ScalarEvent) else 0,
        privacy.DECLARED_EXEMPTION_DOCUMENTS_PATTERNS,
    )
    return None


__all__ = ["declared_exemption"]
