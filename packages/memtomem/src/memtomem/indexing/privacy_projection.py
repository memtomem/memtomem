"""Conservative, index-only secret projection. Source files are never rewritten.

The shared scanner and all non-file ingress guards remain unchanged. Only the
three generic label rules can be adjudicated here; provider keys, PEM material,
ambiguous syntax and shared-scope files retain their original blocking behavior.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from memtomem import privacy

POLICY_VERSION = "index-mask-v1"
MARKER = "[REDACTED]"


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


def _value_span(text: str, hit: privacy.RedactionHit) -> tuple[int, int] | None:
    start, stop = hit.span
    if hit.pattern_index == 2:
        end = _quoted_end(text, stop, text[stop - 1])
        return (stop, end) if end is not None else None
    cursor = stop
    while cursor < len(text) and text[cursor] in " \t":
        cursor += 1
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    # A label-only inline example: `password:`. The backtick must close an
    # existing inline code span, not introduce a value after the label.
    if cursor < len(text) and text[cursor] == "`" and prefix.count("`") % 2:
        return cursor, cursor
    if cursor == len(text) or text[cursor] in "\r\n":
        return cursor, cursor
    # `env ... API_KEY= command` is a shell token with an empty assignment.
    # Do not reinterpret arbitrary prose `api_key = secret` as empty.
    if text[stop - 1] == "=" and cursor > stop:
        code_start = max(line_start, text.rfind("`", line_start, start) + 1)
        segment = text[code_start:stop].lstrip()
        if segment.startswith("env ") and re.search(r"\b[A-Za-z_]\w*=$", segment):
            return stop, stop
    if text[cursor] in "\"'":
        end = _quoted_end(text, cursor + 1, text[cursor])
        return (cursor + 1, end) if end is not None else None
    # Unquoted values require an explicit line or inline-code boundary. Mask
    # the whole bounded value rather than guessing where a password ends.
    end = len(text)
    for delimiter in ("\n", "\r"):
        found = text.find(delimiter, cursor)
        if found >= 0:
            end = min(end, found)
    if prefix.count("`") % 2:
        close = text.find("`", cursor, end)
        if close < 0:
            return None
        end = close
    value = text[cursor:end].rstrip()
    if not value or any(char in value for char in ('"', "'", "`", "\\")):
        return None
    return cursor, cursor + len(value)


def prepare_index_content(content: str, *, scope: str = "user") -> IndexProjection:
    """Return a verified projection, or unchanged text for the normal guard.

    guard_content erases ONLY scanner hits whose entire value was accounted
    for. It is never stored or embedded. A second full scan must be clean.
    Returned content preserves key names and every physical newline.
    """
    original = IndexProjection(content, content)
    hits = privacy.scan(content)
    if not hits or scope == "project_shared" or any(h.pattern_index > 2 for h in hits):
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
        if hit.pattern_index > 2:
            return original
        span = _value_span(projected, hit)
        if span is None or projected[span[0] : span[1]] not in {"", MARKER}:
            return original
    return IndexProjection(projected, guard_content, len(replacements))
