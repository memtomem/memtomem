"""Shared helper functions for server tools."""

from __future__ import annotations

import re

from memtomem.config import Mem2MemConfig

#: The two partial-period spellings ``_parse_recall_date`` accepts. Matched
#: against the whole value so a suffix cannot ride along unread.
_YEAR_RE = re.compile(r"\d{1,4}")
_YEAR_MONTH_RE = re.compile(r"(?P<year>\d{1,4})-(?P<month>\d{1,2})")

#: ``date.fromisoformat`` accepts more than the documented ``YYYY-MM-DD``:
#: an ISO **week** date (``2026-W15``) and the compact form (``20260406``).
#: A week bound is the dangerous one — it parses as that week's Monday, so
#: ``until="2026-W15"`` would cover a single day while reading like seven.
#: Neither is documented, so both are refused rather than given a meaning.
_CALENDAR_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _parse_recall_date(s: str, *, end_of_period: bool = False):
    """Parse a partial or full ISO date string into a UTC datetime.

    For *since* (end_of_period=False): pad to start of period.
    For *until* (end_of_period=True): advance to start of next period so the
    bound is used as an exclusive upper bound (``created_at < until``).

    Supported formats: ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, full ISO datetime.

    The result is always in UTC. ``chunks.created_at`` is stored as a UTC
    ISO-8601 string and the bound is compared against it **lexically** in SQL,
    so an offset-carrying bound left as-is would sort by its printed digits
    rather than by the instant it denotes: ``2026-01-01T00:00:00+09:00``
    (= ``2025-12-31T15:00Z``) would exclude a row written at
    ``2025-12-31T16:00Z``, which is after it.
    """
    from datetime import date, datetime, timedelta, timezone

    s = s.strip()

    try:
        # A partial period has to be the *whole* value. Routing on
        # ``s.split("T")[0]`` instead let a suffix ride along unread, so
        # ``2026Tgarbage`` was accepted as the year 2026 and
        # ``2026-04T14:30`` as the month of April — neither a documented
        # partial date nor a value ``fromisoformat`` would accept.
        if _YEAR_RE.fullmatch(s):
            year = int(s)
            return datetime(year + (1 if end_of_period else 0), 1, 1, tzinfo=timezone.utc)

        year_month = _YEAR_MONTH_RE.fullmatch(s)
        if year_month:
            year, month = int(year_month["year"]), int(year_month["month"])
            if end_of_period:
                if month == 12:
                    return datetime(year + 1, 1, 1, tzinfo=timezone.utc)
                return datetime(year, month + 1, 1, tzinfo=timezone.utc)
            return datetime(year, month, 1, tzinfo=timezone.utc)

        # YYYY-MM-DD or full ISO datetime. Only a date-only bound names a
        # whole period to advance past; one carrying a time is already an
        # instant. Decide that by parsing rather than by looking for a ``T``:
        # ``fromisoformat`` also accepts a space or a lowercase ``t`` as the
        # separator, and those spellings were being advanced by a day they
        # had not asked for.
        date_only = bool(_CALENDAR_DATE_RE.fullmatch(s))
        if not date_only:
            try:
                date.fromisoformat(s)
            except ValueError:
                pass  # carries a time — an instant, handled below
            else:
                # Parses as a date but not in the documented shape: an ISO
                # week or the compact form. Refuse rather than assign it a
                # period silently.
                raise ValueError(f"undocumented date-only spelling: {s!r}")

        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        else:
            dt = dt.astimezone(timezone.utc)
        if end_of_period and date_only:
            dt = dt + timedelta(days=1)
        return dt

    # OverflowError joins the list because converting to UTC can push a bound
    # at the very edge of the representable range past it
    # (``9999-12-31T23:30:00-01:00`` is year 10000 in UTC). That is a bound
    # this function cannot express, which is what its ValueError means —
    # letting it escape as OverflowError would reach callers as an internal
    # error instead of the documented validation failure.
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError(
            f"Invalid date: {s!r}. Use YYYY, YYYY-MM, YYYY-MM-DD or ISO datetime."
        ) from exc


def _check_embedding_mismatch(app: object) -> str | None:
    """Return an error message if embedding config mismatches DB, else None.

    Used by mem_index / mem_import to block operations when dimensions differ.
    """
    mismatch = getattr(getattr(app, "storage", None), "embedding_mismatch", None)
    if mismatch is None:
        return None
    stored = mismatch["stored"]
    configured = mismatch["configured"]
    return (
        f"Embedding mismatch detected — indexing blocked.\n"
        f"  DB stored:  {stored['provider']}/{stored['model']} ({stored['dimension']}d)\n"
        f"  Config:     {configured['provider']}/{configured['model']} ({configured['dimension']}d)\n"
        f"Run 'mm embedding-reset --mode apply-current' (CLI) "
        f'or mem_embedding_reset(mode="apply_current") (MCP) to reset DB.\n'
        f"See docs/guides/configuration.md#reset-flow for the full flow."
    )


def _dim_mismatch_hint(app: object) -> str | None:
    """Return a short, user-facing hint when the DB/runtime embedding mismatch.

    Short-form counterpart to :func:`_check_embedding_mismatch` — suitable
    for appending to a successful ``mem_add`` / ``mem_search`` result rather
    than blocking an operation. Returns ``None`` when there is no mismatch.
    """
    mismatch = getattr(getattr(app, "storage", None), "embedding_mismatch", None)
    if mismatch is None:
        return None
    stored = mismatch["stored"]
    configured = mismatch["configured"]
    return (
        f"Note: embedding dimension mismatch — DB stored "
        f"{stored['provider']}/{stored['model']} ({stored['dimension']}d), "
        f"config uses {configured['provider']}/{configured['model']} "
        f"({configured['dimension']}d). Semantic search falls back to BM25 "
        f"until resolved. Fix: `uv run mm embedding-reset --mode apply-current` "
        f"(see docs/guides/configuration.md#reset-flow)."
    )


async def _announce_dim_mismatch_once(app: object) -> str | None:
    """Return a one-shot dim-mismatch hint for ``mem_add`` / ``mem_search``.

    Uses the AppContext ``_dim_mismatch_announced`` gate (protected by
    ``_config_lock``) so repeated calls within the same MCP session do not
    spam the same notice. Returns ``None`` when there is no mismatch or
    the hint has already been emitted in this session.
    """
    if getattr(getattr(app, "storage", None), "embedding_mismatch", None) is None:
        return None
    lock = getattr(app, "_config_lock", None)
    if lock is None:
        return None
    async with lock:
        if getattr(app, "_dim_mismatch_announced", False):
            return None
        app._dim_mismatch_announced = True  # type: ignore[attr-defined]
    return _dim_mismatch_hint(app)


def _set_config_key(config: Mem2MemConfig, key: str, value: str) -> str:
    """Set a dot-notation config key to a new string value.

    Only ``section.field`` format (exactly one dot) is supported.
    Uses :func:`~memtomem.config.coerce_and_validate` for type coercion
    and constraint checking (min/max/allowed) when the field has a
    registered constraint in :data:`~memtomem.config.FIELD_CONSTRAINTS`.

    Returns a human-readable confirmation or error message.
    """
    from memtomem.config import (
        FIELD_CONSTRAINTS,
        MUTABLE_FIELDS,
        assign_section_fields,
        coerce_and_validate,
    )

    parts = key.split(".")
    if len(parts) != 2:
        return f"Key must be in 'section.field' format (e.g. 'search.default_top_k'). Got: '{key}'"

    section_name, field_name = parts
    section = getattr(config, section_name, None)
    if section is None:
        return f"Section '{section_name}' not found in configuration."

    if not hasattr(section, field_name):
        return f"Field '{field_name}' not found in section '{section_name}'."

    allowed = MUTABLE_FIELDS.get(section_name, set())
    if field_name not in allowed:
        return f"'{key}' is not mutable at runtime (read-only). Use 'mm init' to change it."

    constraint = FIELD_CONSTRAINTS.get(key)
    if constraint:
        try:
            coerced = coerce_and_validate(value, constraint)
        except ValueError as exc:
            return f"Invalid value '{value}' for '{key}': {exc}"
    else:
        # Fallback for fields without explicit constraints — coerce by current type
        current = getattr(section, field_name)
        try:
            if isinstance(current, bool):
                coerced = value.lower() in ("true", "1", "yes")
            elif isinstance(current, int):
                coerced = int(value)
            elif isinstance(current, float):
                coerced = float(value)
            elif isinstance(current, str):
                coerced = value
            else:
                return (
                    f"Cannot set '{key}': unsupported field type "
                    f"'{type(current).__name__}'. Only bool/int/float/str fields "
                    f"can be changed at runtime."
                )
        except (ValueError, TypeError) as exc:
            return f"Invalid value '{value}' for '{key}': {exc}"

    # Re-run the section's cross-field ``@model_validator(mode="after")``, which
    # a bare ``setattr`` skips: an invalid combination used to be accepted here
    # and then persisted by ``mem_config(persist=True)``, only for every later
    # load to drop the whole section back to defaults (#2110). The returned
    # message does not start with "Set ", so the caller neither persists nor
    # fans out, and the runtime config is already rolled back.
    try:
        assign_section_fields(section, {field_name: coerced})
    except ValueError as exc:
        return f"Cannot set '{key}': {exc}"

    show_val = coerced
    if field_name == "langfuse_secret_key" or field_name == "api_key":
        show_val = "***" if coerced else ""
    return f"Set {key} = {show_val!r}"
