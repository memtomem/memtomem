"""Conservative, index-only secret projection. Source files are never rewritten.

The shared scanner and all non-file ingress guards remain unchanged. Only the
three generic label rules can be adjudicated here; provider keys, PEM material,
ambiguous syntax and shared-scope files retain their original blocking behavior.

**The rule this module lives or dies by.** ``prepare_index_content`` builds a
``guard_content`` in which every hit it claims to have *accounted for* is erased,
and the write guard then runs over that text. A hit erased there is a hit the
security gate can no longer see. So :func:`_value_span` may return a span only
when it can name the end of the value from syntax it positively recognises —
never by assuming the value ends where the physical line does. Everything else
returns ``None``, the whole file falls back to its original text, and the guard
blocks it exactly as it did before this module existed.

That asymmetry is why the checks below are written as a whitelist of recognised
shapes rather than a blacklist of known-bad ones: a blacklist that misses a
spelling leaks a secret, a whitelist that misses one only refuses a file.

**It is not enough, and the module ships disabled.** Three review rounds each
closed every shape the round found, and each produced new ones from a grammar
the previous round had not considered — shell word continuation, Python
implicit string concatenation across lines, a YAML flow mapping whose scalar
continues at equal indentation, a Markdown code span delimited by three
backticks. They share a cause rather than a spelling: where a value *ends* is a
property of the enclosing grammar, and this module is handed a string with no
idea what language it is in. A character-level rule cannot decide that, so each
round of patching buys one round of shapes.

``PROJECTION_ENABLED`` is therefore ``False``. Every file takes the unchanged
guard path and blocks exactly as it did before this module existed. The
machinery below is kept, and pinned, because it is the right shape for the
version that can be turned on: one that is handed a *parsed* document — JSON,
YAML, TOML — and masks the source span the parser reports for a matched key,
refusing anything it could not parse. Turning this flag on without that parser
re-opens every bypass listed above.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from memtomem import privacy

POLICY_VERSION = "index-mask-v1"
MARKER = "[REDACTED]"

#: Whether :func:`prepare_index_content` may adjudicate anything at all.
#:
#: ``False``: it returns the original text unchanged for every input, so the
#: write guard sees the file exactly as the scanner wrote it and blocks a
#: secret-bearing file the way it always has. See the module docstring for why
#: — the short version is that the value-boundary question is not answerable
#: without the enclosing grammar, and a wrong answer here does not mis-mask, it
#: blinds the guard.
#:
#: The tests reach past this flag deliberately, to keep the grammar below under
#: test while it is dormant. Nothing else should.
PROJECTION_ENABLED = False

#: What may follow a value's closing quote on the same line and still leave the
#: value ended there: punctuation that continues the *container*, or a comment.
#: A closing quote followed by anything else did not end the value —
#: ``password = "" "secret"``, ``password: \'a\'\'secret\'`` and
#: ``"password": "a" + "secret"`` all continue it past the quote this module
#: would otherwise have masked up to.
#: An unquoted value may contain none of these. Each one opens something —
#: a string, a nested container, an escape — whose end is not the end of this
#: line, which is the only end this module can see. ``password = (\n"x"\n)``
#: is valid Python whose value is on the next line; refusing the whole class is
#: the only way to be sure of the ones nobody listed.
_UNQUOTED_FORBIDDEN = frozenset("\"'`\\(){}[]")

#: Punctuation that can close a *mapping member* and so end its value. Accepted
#: only for the quoted-key rule, whose match shape (``"password": "``) is JSON
#: or a Python dict repr. The unquoted rule matches ``password=`` too, which is
#: also how shell writes an assignment — and there ``,`` continues the word
#: rather than ending it (``password="a",secret`` is one argument).
_MEMBER_TERMINATORS = ",;)]}"
_COMMENT_OPENERS = ("#", "//")


def _resolve(patterns: tuple[str, ...]) -> frozenset[int]:
    return frozenset(
        privacy.DEFAULT_PATTERNS.index(p) for p in patterns if p in privacy.DEFAULT_PATTERNS
    )


#: Scanner rules this module may adjudicate, resolved by literal identity
#: rather than by ordinal. ``DEFAULT_PATTERNS`` is synced from memtomem-stm and
#: a resync may reorder it; an ordinal (``pattern_index > 2``) would then
#: silently re-point "maskable" at whatever moved into that slot, so a provider
#: token would get a ``[REDACTED]`` marker and its file would index instead of
#: blocking. ``test_index_privacy_projection`` pins the resolution.
_PROJECTABLE = _resolve(privacy.PROJECTABLE_LABEL_PATTERNS)
_QUOTED = _resolve(privacy.QUOTED_LABEL_PATTERNS)
#: Fail closed: if a sync edited any rule this module names, resolution comes
#: back short and the projection disables itself rather than adjudicating a set
#: it can no longer identify. Every file then takes the unchanged guard path.
_RESOLVED = len(_PROJECTABLE) == len(privacy.PROJECTABLE_LABEL_PATTERNS) and len(_QUOTED) == len(
    privacy.QUOTED_LABEL_PATTERNS
)


@dataclass(frozen=True)
class IndexProjection:
    content: str
    guard_content: str
    redaction_count: int = 0


def _quoted_end(text: str, start: int, quote: str) -> int | None:
    escaped = False
    for end in range(start, len(text)):
        char = text[end]
        if char in "\r\n":
            return None
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == quote:
            return end
    return None


def _line_end(text: str, start: int) -> int:
    end = len(text)
    for delimiter in ("\n", "\r"):
        found = text.find(delimiter, start)
        if found >= 0:
            end = min(end, found)
    return end


def _closes_cleanly(text: str, close: int, *, member: bool, code_span: bool) -> bool:
    """Whether the closing quote at ``close`` is really the end of the value.

    A quote that closes a string in one grammar opens the next fragment of the
    same value in another: shell and TOML concatenate adjacent strings, YAML
    writes an embedded quote as a doubled one, Python joins with ``+``, and a
    shell word simply continues (``password="a",secret``). Masking up to this
    quote would then leave the rest of the value in the indexed body while the
    guard sees the label erased.

    So the answer is yes only for an ending this function can name:

    * the line ends here;
    * a comment starts here — and a comment needs whitespace in front of it,
      because ``password="a"#secret`` is one shell word, not a value and a note;
    * a Markdown inline code span closes here, and only when the label was
      inside one to begin with (``code_span``), so that a backtick is a span
      delimiter rather than shell command substitution;
    * container punctuation follows, and only for the quoted-key rule
      (``member``) whose shape no shell produces.
    """
    rest = text[close + 1 : _line_end(text, close + 1)]
    if rest == "":
        return True
    stripped = rest.lstrip(" \t")
    if stripped != rest and stripped.startswith(_COMMENT_OPENERS):
        return True
    if code_span and rest[0] == "`":
        return True
    return member and rest[0] in _MEMBER_TERMINATORS


def _indent(text: str, line_start: int) -> int:
    width = 0
    while line_start + width < len(text) and text[line_start + width] in " \t":
        width += 1
    return width


def _next_content_line(text: str, pos: int) -> int | None:
    """Start offset of the next non-blank line strictly after ``pos``'s line."""
    cursor = _line_end(text, pos)
    while cursor < len(text):
        if text[cursor] == "\r":
            cursor += 1
            if cursor < len(text) and text[cursor] == "\n":
                cursor += 1
        elif text[cursor] == "\n":
            cursor += 1
        else:  # pragma: no cover - ``_line_end`` leaves us on a break or at EOF
            return cursor
        line_end = _line_end(text, cursor)
        if text[cursor:line_end].strip():
            return cursor
        cursor = line_end
    return None


def _has_continuation(text: str, line_start: int, value_end: int) -> bool:
    """Whether a later line is indented under the label's line.

    A plain YAML scalar folds a following more-indented line into its value, so
    ``password: first\n  hunter2`` has a two-line value and masking only the
    label's line leaves the second one verbatim. Blank lines do **not** end such
    a scalar — they fold in as a newline — so the search skips them instead of
    stopping at the first one.
    """
    nxt = _next_content_line(text, value_end)
    return nxt is not None and _indent(text, nxt) > _indent(text, line_start)


def _value_span(text: str, hit: privacy.RedactionHit) -> tuple[int, int] | None:
    """The span of the value ``hit`` labels, or ``None`` if it cannot be named.

    ``None`` is the safe answer and the default: see the module docstring for
    why a wrong span here blinds the write guard rather than merely mis-masking.
    """
    start, stop = hit.span
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    in_code_span = prefix.count("`") % 2 == 1

    # The quoted-key rule matched through the value's opening quote, so the
    # value starts at ``stop`` and the quote character is the one before it.
    if hit.pattern_index in _QUOTED:
        quote = text[stop - 1]
        if text[stop : stop + 2] == quote * 2:
            # ``"password": """secret"""`` — a triple-quote opener, whose value
            # is the block after it. ``_quoted_end`` would read the second and
            # third quotes as an empty string and account for nothing.
            return None
        end = _quoted_end(text, stop, quote)
        if end is None or not _closes_cleanly(text, end, member=True, code_span=in_code_span):
            return None
        return stop, end

    cursor = stop
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1

    # A label-only inline example: `password:`. The backtick must close an
    # existing inline code span, not introduce a value after the label.
    if cursor < len(text) and text[cursor] == "`" and in_code_span:
        if _has_continuation(text, line_start, cursor):
            return None
        return cursor, cursor

    # `env ... API_KEY= command` is a shell token with an empty assignment.
    # Checked before the end-of-line rule below because a shell assignment has
    # no continuation syntax: ``env FOO= `` really is the empty string. Do not
    # reinterpret arbitrary prose ``api_key = secret`` as empty.
    if text[stop - 1] == "=" and cursor > stop:
        code_start = max(line_start, text.rfind("`", line_start, start) + 1)
        segment = text[code_start:stop].lstrip()
        if segment.startswith("env ") and re.search(r"\b[A-Za-z_]\w*=$", segment):
            return stop, stop

    if cursor == len(text) or text[cursor] in "\r\n":
        # Nothing follows the label on this line, and "no value here" is not
        # "no value": YAML puts it on the next line, as does a block scalar.
        return None

    if text[cursor] in "\"'":
        quote = text[cursor]
        if text[cursor + 1 : cursor + 3] == quote * 2:
            return None  # a TOML/Python triple-quote opener; see above
        end = _quoted_end(text, cursor + 1, quote)
        if end is None or not _closes_cleanly(text, end, member=False, code_span=in_code_span):
            return None
        if _has_continuation(text, line_start, _line_end(text, end)):
            return None
        return cursor + 1, end

    # Unquoted values run to an explicit line or inline-code boundary. Mask the
    # whole bounded value rather than guessing where a password ends.
    end = _line_end(text, cursor)
    if in_code_span:
        close = text.find("`", cursor, end)
        if close < 0:
            return None
        end = close
    value = text[cursor:end].rstrip()
    if not value:
        return None
    if value[0] in "|>":
        # A YAML block scalar opener (``|``, ``>``, ``|-``, ``>+``, ``|2``, and
        # any of them followed by a comment) is not a value: it announces that
        # the value is the indented block on the lines that follow.
        return None
    # ``MARKER`` is this module's own literal and carries brackets. The
    # re-validation pass in ``prepare_index_content`` re-reads an already
    # projected value and has to recognise it.
    if value != MARKER and not _UNQUOTED_FORBIDDEN.isdisjoint(value):
        return None
    if not in_code_span and _has_continuation(text, line_start, end):
        return None
    return cursor, cursor + len(value)


def prepare_index_content(content: str, *, scope: str = "user") -> IndexProjection:
    """Return a verified projection, or unchanged text for the normal guard.

    Returns the original unchanged unless :data:`PROJECTION_ENABLED`, which is
    off — read the module docstring before turning it on.

    When enabled: guard_content erases ONLY scanner hits whose entire value was
    accounted for. It is never stored or embedded. A second full scan must be
    clean. Returned content preserves key names and every physical newline.
    """
    original = IndexProjection(content, content)
    if not PROJECTION_ENABLED:
        return original
    hits = privacy.scan(content)
    if not hits or scope == "project_shared" or not _RESOLVED:
        return original
    if any(h.pattern_index not in _PROJECTABLE for h in hits):
        return original
    replacements: list[tuple[int, int, str]] = []
    guarded: list[tuple[int, int]] = []
    for hit in hits:
        span = _value_span(content, hit)
        if span is None:
            return original
        left, right = span
        guarded.append((hit.span[0], max(hit.span[1], right)))
        if left < right and content[left:right] != MARKER:
            replacements.append((left, right, MARKER))
    # Overlapping labels or values can be malformed nested data. Refuse to
    # guess and never hide another hit inside an accepted replacement.
    guarded.sort()
    if any(a[1] > b[0] for a, b in zip(guarded, guarded[1:])):
        return original
    projected = content
    for left, right, value in sorted(replacements, reverse=True):
        projected = projected[:left] + value + projected[right:]
    guard_content = content
    for left, right in reversed(guarded):
        guard_content = guard_content[:left] + "[checked]" + guard_content[right:]
    if privacy.scan(guard_content):
        return original
    # Validate projected values separately; generic key labels intentionally
    # remain visible, but every detected value must now be empty or our marker.
    for hit in privacy.scan(projected):
        if hit.pattern_index not in _PROJECTABLE:
            return original
        span = _value_span(projected, hit)
        if span is None or projected[span[0] : span[1]] not in {"", MARKER}:
            return original
    return IndexProjection(projected, guard_content, len(replacements))
