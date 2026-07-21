"""Shared argument validators for MCP tool surfaces.

Helpers here exist because a tool argument reaches the body through two
different doors: the direct FastMCP call (which builds a *lax* pydantic
model from the annotations and will happily coerce ``1`` / ``"true"``)
and the ``mem_do`` dispatcher (which forwards the caller's ``params``
dict unvalidated). Anything gating a destructive or consent-shaped
action has to hold on both.
"""

from __future__ import annotations


def strict_bool(value: object, field: str) -> bool:
    """Accept ONLY a literal ``True`` / ``False``; reject anything else.

    The MCP twin of the web ``_only_literal_true`` validator, generalized to
    both polarities because a *falsy-looking* string is the dangerous direction
    here: ``"false"`` is truthy in Python, so a coercing implementation would
    turn a declined consent into a write. Fails closed with a crisp message
    instead of silently normalizing, so a malformed agent call is corrected
    rather than acted on. ``1`` / ``0`` are rejected too (``1 is True`` is
    ``False`` — ints are not booleans).
    """
    if value is True or value is False:
        return value
    raise ValueError(
        f"{field} must be a literal boolean (true/false), got "
        f"{type(value).__name__} {value!r} — a stringified boolean is refused "
        "because 'false' would read as true."
    )
