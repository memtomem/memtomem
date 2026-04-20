"""CLI: memtomem config show / memtomem config set / memtomem config unset."""

from __future__ import annotations

import json

import click

from memtomem.config import (
    FIELD_CONSTRAINTS,
    MUTABLE_FIELDS,
    _EXTRA_MUTATION_FIELDS,
    coerce_and_validate,
)


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------


@click.group()
def config() -> None:
    """View or modify memtomem configuration."""


@config.command("show")
@click.option("--format", "fmt", type=click.Choice(["table", "json"]), default="table")
@click.option("--json", "as_json", is_flag=True, help="Shortcut for --format json.")
def config_show(fmt: str, *, as_json: bool = False) -> None:
    """Show current configuration (API keys masked)."""
    from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

    # --json is an alias for --format json (CONTRIBUTING "CLI output
    # convention"); if both are passed, --json wins since it's the more
    # specific intent.
    if as_json:
        fmt = "json"

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


def _canonical_unset_keys() -> set[str]:
    """Union of generic mutable fields and dedicated-endpoint fields.

    ``mm config set`` targets ``MUTABLE_FIELDS`` only (generic mutation
    bypasses the indexing/validation side-effects those endpoints carry).
    ``mm config unset`` additionally covers ``_EXTRA_MUTATION_FIELDS``
    (currently ``indexing.memory_dirs``) — removal is not a mutation and
    is precisely what resolves the machine-migration leftover case.
    """
    canonical = {f"{sec}.{f}" for sec, fs in MUTABLE_FIELDS.items() for f in fs}
    canonical |= {f"{sec}.{f}" for sec, fs in _EXTRA_MUTATION_FIELDS.items() for f in fs}
    return canonical


def _suggest_key(key: str, canonical: set[str]) -> str | None:
    import difflib

    match = difflib.get_close_matches(key, list(canonical), n=1, cutoff=0.7)
    return match[0] if match else None


@config.command("unset")
@click.argument("keys", nargs=-1, required=True)
def config_unset(keys: tuple[str, ...]) -> None:
    """Remove config.json overrides for the given KEYs (e.g., 'mmr.enabled').

    Targeted, idempotent removal: each KEY is ``section.field`` form.
    Canonical keys that aren't currently pinned exit 0 with an
    informational note; unknown keys exit 1 (with a suggestion when close
    to a canonical key). When every override is removed the config file
    itself is deleted. For a wholesale reset of wizard-untouched keys, use
    ``mm init --fresh``.
    """
    from memtomem.config import _atomic_write_json, _override_path

    canonical = _canonical_unset_keys()
    path = _override_path()

    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            click.echo(
                click.style(
                    f"Cannot read {path}: malformed JSON ({exc}). "
                    "Run 'mm init --fresh' or edit the file manually.",
                    fg="red",
                )
            )
            raise SystemExit(1) from None
        if not isinstance(existing, dict):
            click.echo(
                click.style(
                    f"Cannot read {path}: malformed top-level value "
                    "(expected object). Run 'mm init --fresh' or edit the "
                    "file manually.",
                    fg="red",
                )
            )
            raise SystemExit(1)

    lines: list[str] = []
    removed_extra_mutation = False
    any_skip = False

    for key in keys:
        if key not in canonical:
            any_skip = True
            suggestion = _suggest_key(key, canonical)
            if suggestion is not None:
                lines.append(
                    click.style(
                        f"Skipped {key}: not set (did you mean '{suggestion}'?)",
                        fg="yellow",
                    )
                )
            else:
                lines.append(click.style(f"Skipped {key}: not set", fg="yellow"))
            continue

        section, field = key.split(".", 1)
        section_data = existing.get(section)
        if isinstance(section_data, dict) and field in section_data:
            section_data.pop(field)
            if not section_data:
                existing.pop(section, None)
            lines.append(f"Removed: {key}")
            if field in _EXTRA_MUTATION_FIELDS.get(section, set()):
                removed_extra_mutation = True
        else:
            lines.append(f"Unset: {key} (already at default)")

    if existing:
        _atomic_write_json(path, existing)
    elif path.exists():
        path.unlink()
        lines.append("Note: config.json now empty, file removed.")

    for line in lines:
        click.echo(line)

    if removed_extra_mutation:
        click.echo(
            click.style(
                "Warning: indexing.memory_dirs is normally managed via "
                "dedicated endpoints. Run 'mm memory-dirs list' to verify; "
                "run 'mm index' if the directory list changed materially.",
                fg="yellow",
            )
        )

    if any_skip:
        raise SystemExit(1)
