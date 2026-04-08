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
        f'or mem_embedding_reset(mode="apply_current") (MCP) to reset DB.'
    )


def _set_config_key(config: Mem2MemConfig, key: str, value: str) -> str:
    """Set a dot-notation config key to a new string value (runtime only).

    Only ``section.field`` format (exactly one dot) is supported.
    Scalar types (bool, int, float, str) are coerced from the string value.
    Complex types (list, set, frozenset, Path …) are rejected.

    Returns a human-readable confirmation or error message.
    """
    parts = key.split(".")
    if len(parts) != 2:
        return f"Key must be in 'section.field' format (e.g. 'search.default_top_k'). Got: '{key}'"

    section_name, field_name = parts
    section = getattr(config, section_name, None)
    if section is None:
        return f"Section '{section_name}' not found in configuration."

    if not hasattr(section, field_name):
        return f"Field '{field_name}' not found in section '{section_name}'."

    current = getattr(section, field_name)

    # Coerce string → target type
    try:
        if isinstance(current, bool):
            coerced: object = value.lower() in ("true", "1", "yes")
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
        return f"Invalid value '{value}' for {type(current).__name__} field '{key}': {exc}"

    setattr(section, field_name, coerced)
    return f"Set {key} = {coerced!r} (runtime only — not persisted)"
