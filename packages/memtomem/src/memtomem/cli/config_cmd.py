"""CLI: memtomem config show / memtomem config set."""

from __future__ import annotations

import json

import click

from memtomem.config import FIELD_CONSTRAINTS, MUTABLE_FIELDS, coerce_and_validate


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
    from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

    cfg = Mem2MemConfig()
    load_config_d(cfg)
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
    allowed = MUTABLE_FIELDS.get(section_name, set())
    if field_name not in allowed:
        click.echo(click.style(f"{key}: not a mutable field", fg="red"))
        raise SystemExit(1)

    constraint = FIELD_CONSTRAINTS.get(key)
    try:
        coerced = coerce_and_validate(value, constraint)
    except ValueError as e:
        click.echo(click.style(f"{key}: {e}", fg="red"))
        raise SystemExit(1)

    cfg = Mem2MemConfig()
    load_config_overrides(cfg)

    section_obj = getattr(cfg, section_name)
    old_val = getattr(section_obj, field_name)
    setattr(section_obj, field_name, coerced)

    save_config_overrides(cfg)
    click.echo(f"{key}: {old_val} -> {coerced}")

    # Rebuild FTS index when tokenizer changes (matches Web UI / MCP behaviour)
    if key == "search.tokenizer":
        from memtomem.storage.fts_tokenizer import set_tokenizer

        assert isinstance(coerced, str)
        set_tokenizer(coerced)

        from memtomem.storage.factory import create_storage

        storage = create_storage(cfg)
        count = storage.rebuild_fts()
        click.echo(f"FTS index rebuilt ({count} chunks).")
