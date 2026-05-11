"""memtomem CLI — unified command-line interface."""

from __future__ import annotations

import io
import sys

import click


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


@click.group(context_settings=CONTEXT_SETTINGS)
@click.version_option(
    package_name="memtomem",
    prog_name="memtomem",
    message="%(prog)s %(version)s",
)
def cli() -> None:
    """memtomem — markdown-first memory infrastructure for AI agents."""
    # Windows console default codepage (cp1252/cp437) can't encode the
    # box-drawing and em-dash glyphs the wizard uses. Reconfigure to UTF-8
    # with `replace` errors so a missing glyph degrades to `?` instead of
    # crashing mid-output. POSIX no-op via the sys.platform guard.
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, io.UnsupportedOperation):
                pass


@cli.command()
def version() -> None:
    """Show the installed memtomem version."""
    from importlib.metadata import version as pkg_version

    click.echo(f"memtomem {pkg_version('memtomem')}")


# Register subcommands (lazy imports to keep startup fast)
def _register() -> None:
    from memtomem.cli.agent_cmd import agent
    from memtomem.cli.config_cmd import config
    from memtomem.cli.context_cmd import context
    from memtomem.cli.embedding_cmd import embedding_reset
    from memtomem.cli.gc_cmd import gc
    from memtomem.cli.indexing import index
    from memtomem.cli.ingest_cmd import ingest
    from memtomem.cli.mem_cmd import mem
    from memtomem.cli.memory import add, recall
    from memtomem.cli.purge_cmd import purge
    from memtomem.cli.reset_cmd import reset
    from memtomem.cli.schedule_cmd import schedule
    from memtomem.cli.search import search
    from memtomem.cli.init_cmd import init
    from memtomem.cli.session_cmd import activity, session
    from memtomem.cli.shell import shell
    from memtomem.cli.status_cmd import status
    from memtomem.cli.sync_doctor_cmd import sync_doctor
    from memtomem.cli.uninstall_cmd import uninstall
    from memtomem.cli.upgrade_cmd import upgrade
    from memtomem.cli.watchdog_cmd import watchdog
    from memtomem.cli.web import web
    from memtomem.cli.wiki_cmd import wiki

    cli.add_command(init)
    cli.add_command(search)
    cli.add_command(add)
    cli.add_command(recall)
    cli.add_command(mem)
    cli.add_command(index)
    cli.add_command(ingest)
    cli.add_command(config)
    cli.add_command(context)
    cli.add_command(embedding_reset)
    cli.add_command(gc)
    cli.add_command(purge)
    cli.add_command(reset)
    cli.add_command(session)
    cli.add_command(activity)
    cli.add_command(status)
    cli.add_command(sync_doctor)
    cli.add_command(watchdog)
    cli.add_command(schedule)
    cli.add_command(web)
    cli.add_command(shell)
    cli.add_command(agent)
    cli.add_command(uninstall)
    cli.add_command(upgrade)
    cli.add_command(wiki)


_register()
