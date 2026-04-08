"""CLI: memtomem config show / memtomem config set."""

from __future__ import annotations

import json

import click


# ---------------------------------------------------------------------------
# Mutable field definitions and validation (used by `config set`)
# ---------------------------------------------------------------------------

_MUTABLE_FIELDS: dict[str, set[str]] = {
    "search": {"default_top_k", "bm25_candidates", "dense_candidates", "rrf_k"},
    "indexing": {"max_chunk_tokens"},
    "embedding": {"batch_size"},
}

_FIELD_CONSTRAINTS: dict[str, dict] = {
    "search.default_top_k": {"type": int, "min": 1, "max": 500},
    "search.bm25_candidates": {"type": int, "min": 1, "max": 1000},
    "search.dense_candidates": {"type": int, "min": 1, "max": 1000},
    "search.rrf_k": {"type": int, "min": 1, "max": 1000},
    "indexing.max_chunk_tokens": {"type": int, "min": 64, "max": 8192},
    "embedding.batch_size": {"type": int, "min": 1, "max": 1024},
}


def _coerce_and_validate(value, constraint: dict | None):
    """Coerce value to expected type and validate constraints."""
    if constraint is None:
        return value

    expected_type = constraint["type"]

    if expected_type is bool:
        if isinstance(value, bool):
            coerced = value
        elif isinstance(value, str):
            low = value.lower()
            if low in ("true", "1", "yes"):
                coerced = True
            elif low in ("false", "0", "no"):
                coerced = False
            else:
                raise ValueError(f"cannot convert '{value}' to bool")
        elif isinstance(value, (int, float)):
            coerced = bool(value)
        else:
            raise ValueError(f"cannot convert to bool: {value}")
    elif expected_type is int:
        try:
            coerced = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"cannot convert '{value}' to int")
    elif expected_type is float:
        try:
            coerced = float(value)
        except (TypeError, ValueError):
            raise ValueError(f"cannot convert '{value}' to float")
    elif expected_type is str:
        coerced = str(value)
    else:
        coerced = value

    if "min" in constraint and coerced < constraint["min"]:
        raise ValueError(f"must be >= {constraint['min']}")
    if "max" in constraint and coerced > constraint["max"]:
        raise ValueError(f"must be <= {constraint['max']}")
    if "allowed" in constraint and coerced not in constraint["allowed"]:
        raise ValueError(f"must be one of {constraint['allowed']}")

    return coerced


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group()
def config() -> None:
    """View or modify memtomem configuration."""


@config.command("show")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
def config_show(fmt: str) -> None:
    """Show current configuration (API keys masked)."""
    from memtomem.config import Mem2MemConfig, load_config_overrides

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)
    data = cfg.model_dump()

    # Mask sensitive fields
    if data.get("embedding", {}).get("api_key"):
        data["embedding"]["api_key"] = "***"

    if fmt == "json":
        click.echo(json.dumps(data, indent=2, default=str))
    else:
        for section, values in data.items():
            click.echo(click.style(f"\n[{section}]", bold=True))
            if isinstance(values, dict):
                for k, v in values.items():
                    click.echo(f"  {k} = {v}")
            else:
                click.echo(f"  {values}")


@config.command("set")
@click.argument("key")
@click.argument("value")
def config_set(key: str, value: str) -> None:
    """Set a config field (e.g., 'search.default_top_k 20'). Persists to ~/.memtomem/config.json."""
    from memtomem.config import Mem2MemConfig, load_config_overrides, save_config_overrides

    parts = key.split(".", 1)
    if len(parts) != 2:
        click.echo(click.style("Key must be section.field (e.g., search.default_top_k)", fg="red"))
        raise SystemExit(1)

    section_name, field_name = parts
    allowed = _MUTABLE_FIELDS.get(section_name, set())
    if field_name not in allowed:
        click.echo(click.style(f"{key}: not a mutable field", fg="red"))
        raise SystemExit(1)

    constraint = _FIELD_CONSTRAINTS.get(key)
    try:
        coerced = _coerce_and_validate(value, constraint)
    except ValueError as e:
        click.echo(click.style(f"{key}: {e}", fg="red"))
        raise SystemExit(1)

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)

    section_obj = getattr(cfg, section_name)
    old_val = getattr(section_obj, field_name)
    setattr(section_obj, field_name, coerced)

    save_config_overrides(cfg, _MUTABLE_FIELDS)
    click.echo(f"{key}: {old_val} -> {coerced}")
