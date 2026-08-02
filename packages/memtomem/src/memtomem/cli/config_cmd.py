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
from memtomem.secret_masking import is_secret_key, mask_secrets


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
    data = mask_secrets(cfg.model_dump())

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
        for line in _rejection_lines(key, section_name, field_name, allowed):
            click.echo(line)
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
    try:
        save_config_overrides(cfg)
    except ValueError as e:
        click.echo(click.style(f"{key}: {e}", fg="red"))
        raise SystemExit(1)
    except TimeoutError:
        click.echo(
            click.style(
                "Could not lock config.json: another process is writing it. Retry in a moment.",
                fg="red",
            )
        )
        raise SystemExit(1) from None

    old_show = old_val
    new_show = coerced
    if is_secret_key(field_name):
        old_show = "***" if old_val else ""
        new_show = "***" if coerced else ""
    click.echo(f"{key}: {old_show} -> {new_show}")

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

    # A field name that is an exact fragment of a canonical one beats edit
    # distance: 'search.top_k' lives inside 'search.default_top_k', while
    # difflib ranks the shorter 'search.rrf_k' higher (#1993).
    section, _, field = key.partition(".")
    if len(field) >= 3:
        # Sort by name as well as length: `canonical` is a set, so equal-length
        # matches would otherwise pick a hash-order winner
        # (`indexing.chunk_tokens` → min_ vs max_chunk_tokens).
        contained = sorted(
            (c for c in canonical if c.startswith(f"{section}.") and field in c.split(".", 1)[1]),
            key=lambda c: (len(c), c),
        )
        if contained:
            return contained[0]

    match = difflib.get_close_matches(key, list(canonical), n=1, cutoff=0.7)
    return match[0] if match else None


def _rejection_lines(key: str, section_name: str, field_name: str, allowed: set[str]) -> list[str]:
    """Build the ``config set`` rejection message for a non-mutable KEY.

    A bare "not a mutable field" is a dead end: the same value is spelled
    ``--top-k`` on ``mm init``, ``Top-K`` in ``mm status``, and
    ``search.default_top_k`` here (issue #1993). Name the near miss, the
    section's settable fields, and where to read current values.
    """
    lines = []
    if field_name in _EXTRA_MUTATION_FIELDS.get(section_name, set()):
        lines.append(
            click.style(
                f"{key}: not a mutable field — it is managed via "
                f"'mm memory-dirs add/remove', not 'mm config set'.",
                fg="red",
            )
        )
        return lines

    settable = {f"{sec}.{f}" for sec, fs in MUTABLE_FIELDS.items() for f in fs}
    suggestion = _suggest_key(key, settable)
    if suggestion is not None:
        lines.append(
            click.style(f"{key}: not a mutable field (did you mean '{suggestion}'?)", fg="red")
        )
    else:
        lines.append(click.style(f"{key}: not a mutable field", fg="red"))

    if allowed:
        lines.append(f"Mutable fields in [{section_name}]: {', '.join(sorted(allowed))}")
    else:
        lines.append(f"Mutable sections: {', '.join(sorted(MUTABLE_FIELDS))}")
    lines.append("Run 'mm config show' to see current values.")
    return lines


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
    from memtomem.config import (
        _atomic_write_json,
        _config_write_lock,
        _override_path,
        _relativize_config_paths_in_place,
    )

    canonical = _canonical_unset_keys()
    path = _override_path()

    lines: list[str] = []
    removed_extra_mutation = False
    any_skip = False

    # Hold the sidecar lock across read→merge→write (incl. the empty-file
    # removal) so a concurrent writer can't clobber our unset (issue #1567).
    try:
        with _config_write_lock(path):
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
                _relativize_config_paths_in_place(existing)
                _atomic_write_json(path, existing)
            elif path.exists():
                path.unlink()
                lines.append("Note: config.json now empty, file removed.")
    except TimeoutError:
        click.echo(
            click.style(
                f"Could not lock {path}: another process is writing config. Retry in a moment.",
                fg="red",
            )
        )
        raise SystemExit(1) from None

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
