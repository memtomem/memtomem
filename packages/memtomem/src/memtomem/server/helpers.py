"""Shared helper functions for server tools."""

from __future__ import annotations

from memtomem.config import Mem2MemConfig


def _parse_recall_date(s: str, *, end_of_period: bool = False):
    """Parse a partial or full ISO date string into a UTC datetime.

    For *since* (end_of_period=False): pad to start of period.
    For *until* (end_of_period=True): advance to start of next period so the
    bound is used as an exclusive upper bound (``created_at < until``).

    Supported formats: ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD``, full ISO datetime.
    """
    from datetime import datetime, timedelta, timezone

    s = s.strip()
    date_part = s.split("T")[0]
    has_time = "T" in s
    parts = date_part.split("-")

    try:
        if len(parts) == 1:
            year = int(parts[0])
            return datetime(year + (1 if end_of_period else 0), 1, 1, tzinfo=timezone.utc)

        if len(parts) == 2:
            year, month = int(parts[0]), int(parts[1])
            if end_of_period:
                if month == 12:
                    return datetime(year + 1, 1, 1, tzinfo=timezone.utc)
                return datetime(year, month + 1, 1, tzinfo=timezone.utc)
            return datetime(year, month, 1, tzinfo=timezone.utc)

        # YYYY-MM-DD or full ISO datetime
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if end_of_period and not has_time:
            dt = dt + timedelta(days=1)
        return dt

    except (ValueError, TypeError) as exc:
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
    from memtomem.config import FIELD_CONSTRAINTS, MUTABLE_FIELDS, coerce_and_validate

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

    setattr(section, field_name, coerced)
    return f"Set {key} = {coerced!r}"
