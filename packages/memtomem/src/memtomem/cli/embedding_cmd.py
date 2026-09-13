"""CLI: mm embedding-reset — resolve embedding model/dimension mismatches."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import click

from memtomem.embedding.identity import (
    embedding_identity_complete,
    embedding_identity_label,
    require_complete_embedding_identity,
)

if TYPE_CHECKING:
    from memtomem.config import Mem2MemConfig
    from memtomem.storage.sqlite_backend import SqliteBackend


@click.command("embedding-reset")
@click.option(
    "--mode",
    type=click.Choice(["status", "apply-current", "revert-to-stored"]),
    default="status",
    help="status: show mismatch info, apply-current: reset DB (destructive), "
    "revert-to-stored: show stored settings and recovery instructions",
)
@click.option(
    "--yes",
    "-y",
    "assume_yes",
    is_flag=True,
    help="Skip the --mode apply-current confirmation prompt (for cron/CI).",
)
def embedding_reset(mode: str, assume_yes: bool) -> None:
    """Check or resolve embedding configuration mismatches.

    \b
    Modes:
      status          Show DB stored values vs current config (default)
      apply-current   Reset DB to current config — deletes all vectors, re-index required
      revert-to-stored  Show stored settings and recovery instructions

    Status and revert-to-stored do not write config.json. All modes initialize
    storage and may create or initialize the database.

    ``--mode apply-current --yes`` is the non-interactive form.
    """
    # --yes names a prompt, and only apply-current has one.  Refusing it
    # elsewhere is the ``mm gc --yes requires --apply`` precedent: a bare
    # ``mm embedding-reset --yes`` reads like "reset without asking" but would
    # silently print status instead, which is the confusion the flag exists to
    # remove.
    if assume_yes and mode != "apply-current":
        raise click.UsageError("--yes requires --mode apply-current.")
    asyncio.run(_run(mode, assume_yes=assume_yes))


async def _run(mode: str, *, assume_yes: bool = False) -> None:
    from memtomem.config import (
        Mem2MemConfig,
        embedding_policy_fingerprint,
        load_config_d,
        load_config_overrides,
    )
    from memtomem.storage.sqlite_backend import SqliteBackend

    cfg = Mem2MemConfig()
    load_config_d(cfg)
    # Reporting modes must not migrate config.json just to show guidance.
    # Storage initialization is retained; these are not read-only DB queries.
    load_config_overrides(cfg, migrate=(mode == "apply-current"))

    # Relaxed mode: this CLI is explicitly the recovery tool for the
    # dim=0 / real-provider mismatch that ``create_tables`` fails-fast on.
    # Without ``strict_dim_check=False`` the user could not run
    # ``embedding-reset`` to fix the state the gate is flagging.
    storage = SqliteBackend(
        cfg.storage,
        dimension=cfg.embedding.dimension,
        embedding_provider=cfg.embedding.provider,
        embedding_model=cfg.embedding.model,
        embedding_policy_fingerprint=embedding_policy_fingerprint(cfg.embedding),
        embedding_max_sequence_tokens=cfg.embedding.max_sequence_tokens,
        strict_dim_check=False,
    )
    await storage.initialize()
    try:
        await _run_initialized(storage, cfg, mode, assume_yes=assume_yes)
    finally:
        await storage.close()


async def _run_initialized(
    storage: SqliteBackend, cfg: Mem2MemConfig, mode: str, *, assume_yes: bool
) -> None:
    from memtomem.config import embedding_policy_fingerprint

    mismatch = getattr(storage, "embedding_mismatch", None)
    stored = getattr(storage, "stored_embedding_info", None)

    if mode == "status":
        click.echo(click.style("Embedding Status", bold=True))
        if stored:
            click.echo(
                f"  DB stored:  {embedding_identity_label(stored['provider'], stored['model'])} "
                f"({stored['dimension']}d)"
            )
            if stored.get("max_sequence_tokens") is not None:
                click.echo(f"  DB max sequence tokens: {stored['max_sequence_tokens']}")
        click.echo(
            f"  Config:     {cfg.embedding.provider}/{cfg.embedding.model} "
            f"({cfg.embedding.dimension}d)"
        )
        click.echo(f"  Config max sequence tokens: {cfg.embedding.max_sequence_tokens}")
        if mismatch is None:
            click.echo(click.style("\nNo mismatch — DB and config are in sync.", fg="green"))
        else:
            click.echo(click.style("\nMismatch detected!", fg="yellow"))
            click.echo(
                "  mm embedding-reset --mode apply-current    # reset DB (destructive, re-index needed)"
            )
            if stored and not embedding_identity_complete(stored["provider"], stored["model"]):
                click.echo("  Revert-to-stored is unavailable: the stored identity is incomplete.")
            else:
                click.echo(
                    "  mm embedding-reset --mode revert-to-stored # show stored settings "
                    "and recovery instructions"
                )
        return

    # Convert CLI kebab-case to internal snake_case
    internal_mode = mode.replace("-", "_")

    if internal_mode == "apply_current":
        if not assume_yes and not click.confirm(
            f"This will DELETE all vectors and reset DB to "
            f"{cfg.embedding.provider}/{cfg.embedding.model} ({cfg.embedding.dimension}d). "
            f"Re-indexing will be required. Continue?",
            default=False,
        ):
            click.echo("Cancelled.")
            return

        await storage.reset_embedding_meta(
            dimension=cfg.embedding.dimension,
            provider=cfg.embedding.provider,
            model=cfg.embedding.model,
            policy_fingerprint=embedding_policy_fingerprint(cfg.embedding),
            max_sequence_tokens=cfg.embedding.max_sequence_tokens,
        )
        click.echo(
            click.style(
                f"DB reset to {cfg.embedding.provider}/{cfg.embedding.model} "
                f"({cfg.embedding.dimension}d).",
                fg="green",
            )
        )
        click.echo("All vectors deleted — run 'mm index --force <path>' to re-index.")

    elif internal_mode == "revert_to_stored":
        click.echo(
            "Guidance only: this command does not change embedding settings or a running server."
        )
        click.echo("Storage initialization is retained and may create or initialize the database.")
        if mismatch is None:
            click.echo("No mismatch — nothing to revert.")
        else:
            # Read the recorded identity directly. The mismatch summary can
            # carry configured fields when that individual check was skipped
            # (for example a configured ``none`` provider with an empty model).
            s = storage.stored_embedding_info
            try:
                require_complete_embedding_identity(s["provider"], s["model"])
            except ValueError as exc:
                raise click.ClickException(str(exc)) from exc
            click.echo(
                f"DB stored: {embedding_identity_label(s['provider'], s['model'])} "
                f"({s['dimension']}d)."
            )
            if s.get("policy_fingerprint"):
                click.echo(f"  DB policy fingerprint: {s['policy_fingerprint']}")
            click.echo("Mismatch remains. Choose a recovery method:")
            click.echo("  1. Update the embedding section in ~/.memtomem/config.json:")
            for key in ("provider", "model", "dimension", "max_sequence_tokens"):
                if s.get(key) is not None:
                    click.echo(f"       embedding.{key} = {s[key]!r}")
            click.echo(
                "     Check overriding MEMTOMEM_* environment variables and embedding policy "
                "settings; identity fields alone may not resolve a policy mismatch."
            )
            click.echo(
                "     Restart affected servers with the corrected settings, then run "
                "'mm embedding-reset --mode status' to verify."
            )
            click.echo(
                '  2. On the running MCP server, call mem_embedding_reset(mode="revert_to_stored").'
            )
            click.echo(
                "     This switches only the server handling the call; "
                "it does not persist settings to config.json."
            )
