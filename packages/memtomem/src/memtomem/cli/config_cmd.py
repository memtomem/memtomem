"""CLI: memtomem config show / memtomem config set / memtomem config unset."""

from __future__ import annotations

import asyncio
import json

import click

from memtomem.config import (
    FIELD_CONSTRAINTS,
    MUTABLE_FIELDS,
    Mem2MemConfig,
    SaveReceipt,
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
    from memtomem.config import (
        Mem2MemConfig,
        assign_section_fields,
        load_config_d,
        load_config_overrides,
        save_config_overrides,
    )

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
    load_config_d(cfg, quiet=True)
    load_config_overrides(cfg)

    section_obj = getattr(cfg, section_name)
    # ``assign_section_fields`` re-runs the section's cross-field
    # ``@model_validator(mode="after")``, which the bare ``setattr`` path skips
    # (sub-configs don't set ``validate_assignment``). Without it an invalid
    # combination (e.g. max_chunk_tokens below min_chunk_tokens) is written to
    # config.json and then silently reverted by every subsequent load — a pin
    # that never takes effect and never explains itself (#2108).
    try:
        old_val = assign_section_fields(section_obj, {field_name: coerced})[field_name]
    except ValueError as e:
        click.echo(click.style(f"{key}: {e}", fg="red"))
        # Not "nothing written": loading the file above may have run the
        # legacy auto_discover migration, which writes. Only the requested
        # value is guaranteed absent.
        click.echo(f"{key} was not saved.")
        raise SystemExit(1) from None

    try:
        receipt = save_config_overrides(cfg)
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

    def _show(value: object) -> object:
        if is_secret_key(field_name):
            return "***" if value else ""
        return value

    # One measurement, shared by the report and the FTS rebuild below: two
    # separate loads could disagree if another process writes in between, and
    # then the index would be built for a tokenizer the user was never told
    # about.
    effective = _effective_value(section_name, field_name)

    click.echo(f"{key}: {_show(old_val)} -> {_show(coerced)}")
    for line in _effect_lines(key, section_name, field_name, coerced, receipt, effective):
        click.echo(line)

    # Rebuild the FTS index on every ``search.tokenizer`` set — including one
    # that changes nothing, which is what makes re-running the command a
    # working retry after a failed rebuild (see ``_rebuild_fts``).
    if key == "search.tokenizer":
        assert isinstance(coerced, str)
        _rebuild_fts(cfg, requested=coerced, effective=str(effective))


def _rebuild_fts(cfg: Mem2MemConfig, *, requested: str, effective: str) -> None:
    """Rebuild the FTS index so stored terms match the tokenizer queries use.

    Two things this has to get right, both of which the previous version got
    wrong (issue #2112):

    * ``SqliteBackend.rebuild_fts`` is ``async``, and the backend needs
      ``initialize()`` before it has a connection. Calling it bare discarded a
      coroutine and printed its ``repr`` as the row count — the user was told
      the index had been rebuilt when nothing had run at all.
    * The index has to agree with *search*, and search tokenizes with the
      effective value, not the one that was just written. ``config.json`` is
      outranked by ``MEMTOMEM_SEARCH__TOKENIZER`` (#2108/#2111), so building
      the index for the requested value would guarantee the mismatch this
      rebuild exists to prevent. We always rebuild — the effective tokenizer
      is what the index must match either way, and an unconditional rebuild is
      what makes re-running the command a working retry after a failure.
    """
    from memtomem.storage.factory import create_storage
    from memtomem.storage.fts_tokenizer import get_tokenizer, set_tokenizer

    set_tokenizer(effective)
    storage = create_storage(cfg)

    async def _run() -> int:
        # ``initialize`` is inside the try: it opens the connection partway
        # through its own work, so a failure there still leaves a handle for
        # ``close`` (which is written to tolerate a half-open backend) to
        # release.
        try:
            await storage.initialize()
            return await storage.rebuild_fts()
        finally:
            await storage.close()

    try:
        count = asyncio.run(_run())
    except Exception as e:  # noqa: BLE001 — the value is saved; report, don't traceback
        click.echo(
            click.style(
                f"error: search.tokenizer was saved, but the FTS index rebuild failed: {e}",
                fg="red",
            )
        )
        click.echo(
            click.style(
                f"Searches tokenize with {effective} against an index built the old way "
                f"until a rebuild succeeds. Re-run 'mm config set search.tokenizer "
                f"{requested}' to retry.",
                fg="red",
            )
        )
        raise SystemExit(1) from None

    # ``set_tokenizer`` is not the last word: tokenizing with kiwipiepy when it
    # is not installed silently reverts to unicode61 mid-rebuild, so the name
    # we asked for is not necessarily the name the rows were built under. Read
    # back what actually ran, and name *that* in the success line — announcing
    # the requested name and then retracting it two lines later is the same
    # class of false claim this whole fix is about.
    built = get_tokenizer()
    if built != effective:
        click.echo(
            click.style(
                f"warning: FTS index rebuilt with {built} ({count} chunks) — {effective} "
                f"is unavailable (not installed), so the rebuild and every query in this "
                f"process fall back to {built}. Install it and re-run this command for a "
                f"{effective} index.",
                fg="yellow",
            )
        )
    elif effective != requested:
        click.echo(
            click.style(
                f"FTS index rebuilt with {effective} ({count} chunks) — the effective "
                f"tokenizer, not the requested {requested}.",
                fg="yellow",
            )
        )
    else:
        click.echo(f"FTS index rebuilt with {effective} ({count} chunks).")


def _effective_value(section_name: str, field_name: str) -> object:
    """The value a fresh load would put in effect for SECTION.FIELD.

    ``migrate=False``: this is a read-only inspection pass, and the legacy
    ``auto_discover`` migration writes to disk — a reporting call must not
    mutate the file it is reporting on.
    """
    from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

    cfg = Mem2MemConfig()
    load_config_d(cfg, quiet=True)
    load_config_overrides(cfg, migrate=False)
    return getattr(getattr(cfg, section_name), field_name)


def _effect_lines(
    key: str,
    section_name: str,
    field_name: str,
    coerced: object,
    receipt: SaveReceipt,
    effective: object,
) -> list[str]:
    """Report what the write actually achieved (issue #2108).

    ``config set`` writes ``config.json``, which env vars outrank, and
    ``save_config_overrides`` prunes any value matching the lower layers on
    purpose (PR #256, so env-sourced values don't drag-pin into the file).
    Both behaviours are correct and both make a bare ``old -> new`` line a
    claim the command cannot back up. Say which one happened instead.

    Facts only: the before/after pins come from the save receipt (captured
    under the write lock), and ``effective`` is measured by a fresh load
    (``_effective_value``) rather than inferred from which layer we think
    supplied it. It is passed in rather than measured here so the caller's FTS
    rebuild acts on the same reading this report describes.
    """
    from memtomem.config import MISSING, env_var_owning

    env_var = env_var_owning(section_name, field_name)
    pruned = receipt.pruned(section_name, field_name)
    pinned_before = receipt.pinned_before(section_name, field_name)

    lines: list[str] = []
    if effective != coerced and env_var is not None:
        # Name the variable, never read it: the actionable part is which knob
        # to unset. Quoted values go through the same mask as `old -> new`.
        lines.append(
            click.style(
                f"warning: {env_var} is set and takes precedence — the effective value is "
                f"still {_masked(field_name, effective)}. config.json holds your value and "
                f"it applies once no case spelling of that name is set.",
                fg="yellow",
            )
        )
    elif effective != coerced:
        # No higher-precedence layer to blame: the value did not survive a
        # reload. Report that, rather than inventing a source.
        lines.append(
            click.style(
                f"warning: a fresh load does not put {key} at "
                f"{_masked(field_name, coerced)} — the effective value is "
                f"{_masked(field_name, effective)}. Run 'mm config show' and check the "
                f"log for the reason.",
                fg="yellow",
            )
        )

    if receipt.pinned_after(section_name, field_name) is MISSING:
        # Nothing was stored. Whether or not a pin was displaced, the caller's
        # value now rests on a layer they did not set, and unsetting that layer
        # takes it away — so say it even on a clean file.
        where = f"{env_var} or a lower layer" if env_var else "a lower layer (default or config.d)"
        displaced = (
            f" (it held {_masked(field_name, pinned_before)})"
            if pruned
            else " (it had no entry for it)"
        )
        lines.append(
            click.style(
                f"note: config.json does not pin {key}{displaced} — "
                f"{_masked(field_name, coerced)} already comes from {where}, so the "
                f"delta-only write had nothing to store. Use 'mm config unset {key}' "
                f"when removal is the goal.",
                fg="yellow",
            )
        )
    return lines


def _masked(field_name: str, value: object) -> object:
    return ("***" if value else "") if is_secret_key(field_name) else value


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
        contained = [
            c for c in canonical if c.startswith(f"{section}.") and field in c.split(".", 1)[1]
        ]
        # Only a unique fragment match is a suggestion. 'indexing.chunk_tokens'
        # sits inside min_/max_/target_chunk_tokens equally, so naming one would
        # be a confident guess at which the user meant — and falling through to
        # edit distance just launders the same guess. Say nothing instead; the
        # `config set` caller lists the section's fields either way.
        if contained:
            return contained[0] if len(contained) == 1 else None

    # ``canonical`` is a set, so sort before ranking — difflib keeps the first
    # of equally-close candidates, which would otherwise be a hash-order winner.
    match = difflib.get_close_matches(key, sorted(canonical), n=1, cutoff=0.7)
    return match[0] if match else None


# Non-mutable keys whose value a command actually *asks the user for*. That is
# a narrower bar than "the wizard writes this key": ``mm init`` also emits
# ``embedding.base_url``, ``storage.backend`` and ``rerank.provider``, but
# hardcodes them — telling someone to re-run the wizard to reach a value it
# will never prompt for is a wasted trip, and pointing at it for a key it
# never touches at all (``llm.api_key``) is worse. Everything else recognized
# but non-mutable gets the file-edit remedy instead of a slogan.
_DEDICATED_REMEDIES: dict[str, str] = {
    "embedding.provider": "re-run 'mm init'",
    "embedding.model": "re-run 'mm init'",
    "embedding.api_key": "re-run 'mm init'",
    "storage.sqlite_path": "re-run 'mm init'",
    "indexing.project_memory_dirs": "run 'mm mem init' in the project",
    "rerank.model": "re-run 'mm init'",
}

# Changing these after content is indexed strands the stored vectors, so the
# remedy is two steps, not one (docs/guides/embeddings.md). The membership is
# the union of every input the mismatch check reads: the stored
# dimension/provider/model metadata compared in the storage layer, plus
# ``embedding_policy_fingerprint`` (config.py:172), which is what puts
# ``max_sequence_tokens`` here even though no command writes it — so this set
# is not a subset of ``_DEDICATED_REMEDIES``.
_RESET_AFTER_CHANGE = {
    "embedding.provider",
    "embedding.model",
    "embedding.dimension",
    "embedding.max_sequence_tokens",
}

# A hand edit that moves only one of these lands a half-configured embedder
# (``provider=onnx`` with ``model=""``/``dimension=0``) that no gate calls a
# mismatch — ``mm embedding-reset`` compares DB against config and reports
# "in sync" because both are the broken tuple. The wizard sets all three from
# one model choice, which is why it stays the first remedy.
_COUPLED_EMBEDDING_FIELDS = {
    "embedding.provider",
    "embedding.model",
    "embedding.dimension",
}

# Non-mutable keys that a live, settable key has replaced. Sending someone to
# the config file for one of these is a dead end: the override loader only
# warns about the deprecated spelling, it does not carry the value over to the
# successor (measured — ``rerank.top_k: 42`` in config.json leaves
# ``min_pool`` at its default). Name the successor instead.
_DEPRECATED_REPLACEMENTS: dict[str, str] = {"rerank.top_k": "rerank.min_pool"}


def _is_config_model_field(section_name: str, field_name: str) -> bool:
    """True when SECTION.FIELD is a real config field that just isn't mutable.

    Distinguishes a restart-required field (``embedding.provider``) from a typo
    (``embedding.provdier``) so the caller can name the right remedy instead
    of a fuzzy guess.
    """
    from memtomem.config import Mem2MemConfig

    section_field = Mem2MemConfig.model_fields.get(section_name)
    if section_field is None:
        return False
    section_model = section_field.annotation
    model_fields = getattr(section_model, "model_fields", None)
    return isinstance(model_fields, dict) and field_name in model_fields


def _rejection_lines(key: str, section_name: str, field_name: str, allowed: set[str]) -> list[str]:
    """Build the ``config set`` rejection message for a non-mutable KEY.

    A bare "not a mutable field" is a dead end: the same value is spelled
    ``--top-k`` on ``mm init``, ``Top-K`` in ``mm status``, and
    ``search.default_top_k`` here (issue #1993). Name the near miss, the
    section's settable fields, and where to read current values.

    A field that exists on the model but is restart-required
    (``embedding.provider``, ``llm.api_key``) needs the opposite of a near
    miss: the key is spelled correctly and the user needs the path that *does*
    change it — a dedicated command where one exists, the config file
    otherwise (issue #2062).
    """
    lines = []
    if field_name in _EXTRA_MUTATION_FIELDS.get(section_name, set()):
        lines.append(
            click.style(
                f"{key}: not a mutable field — re-run 'mm init', set "
                f"MEMTOMEM_INDEXING__MEMORY_DIRS, or add the path from the Web UI "
                f"(Sources). 'mm config unset {key}' clears it.",
                fg="red",
            )
        )
        return lines

    replacement = _DEPRECATED_REPLACEMENTS.get(key)
    if replacement is not None:
        lines.append(
            click.style(
                f"{key}: deprecated and not settable — use "
                f"'mm config set {replacement} <value>' instead.",
                fg="red",
            )
        )
        return lines

    if _is_config_model_field(section_name, field_name):
        from memtomem.config import _override_path

        remedy = _DEDICATED_REMEDIES.get(key)
        edit = f"edit {_override_path()}"
        if key in _COUPLED_EMBEDDING_FIELDS:
            edit += " (provider, model and dimension must be set together)"
        detail = edit if remedy is None else f"{remedy}, or {edit}"
        detail += " and restart"
        if key in _RESET_AFTER_CHANGE:
            # Bare ``embedding-reset`` is ``--mode status`` — non-destructive
            # with respect to stored vectors (it does open the DB) — and
            # prints the destructive follow-up itself when there is a
            # mismatch — the safe entry point, so name it and stop there.
            detail += "; 'mm embedding-reset' then reports whether indexed content needs a reset"
        lines.append(click.style(f"{key}: not settable at runtime — {detail}.", fg="red"))
        if allowed:
            lines.append(f"Mutable fields in [{section_name}]: {', '.join(sorted(allowed))}")
        lines.append("Run 'mm config show' to see current values.")
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
                "dedicated endpoints. Run 'mm status' and check 'User sources' "
                "to verify; run 'mm index' if the list changed materially.",
                fg="yellow",
            )
        )

    if any_skip:
        raise SystemExit(1)
