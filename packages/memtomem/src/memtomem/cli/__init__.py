"""memtomem CLI — unified command-line interface."""

from __future__ import annotations

import click


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=CONTEXT_SETTINGS)
def cli() -> None:
    """memtomem — markdown-first memory infrastructure for AI agents."""


# Register subcommands (lazy imports to keep startup fast)
def _register() -> None:
    from memtomem.cli.config_cmd import config
    from memtomem.cli.context_cmd import context
    from memtomem.cli.embedding_cmd import embedding_reset
    from memtomem.cli.indexing import index
    from memtomem.cli.ingest_cmd import ingest
    from memtomem.cli.memory import add, recall
    from memtomem.cli.purge_cmd import purge
    from memtomem.cli.reset_cmd import reset
    from memtomem.cli.search import search
    from memtomem.cli.init_cmd import init
    from memtomem.cli.session_cmd import activity, session
    from memtomem.cli.shell import shell
    from memtomem.cli.watchdog_cmd import watchdog
    from memtomem.cli.web import web

    cli.add_command(init)
    cli.add_command(search)
    cli.add_command(add)
    cli.add_command(recall)
    cli.add_command(index)
    cli.add_command(ingest)
    cli.add_command(config)
    cli.add_command(context)
    cli.add_command(embedding_reset)
    cli.add_command(purge)
    cli.add_command(reset)
    cli.add_command(session)
    cli.add_command(activity)
    cli.add_command(watchdog)
    cli.add_command(web)
    cli.add_command(shell)


_register()
