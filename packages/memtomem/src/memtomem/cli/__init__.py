"""memtomem CLI — unified command-line interface."""

from __future__ import annotations

import io
import sys
from collections.abc import Iterator, MutableMapping
from importlib import import_module

import click


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}

# Subcommand name -> (module, attribute). Command modules import what their
# commands need (config, storage, embedding, search), so each one is imported
# only when its command is looked up: ``mm version`` does not pay for ``mm index``.
_LAZY_SUBCOMMANDS: dict[str, tuple[str, str]] = {
    "activity": ("memtomem.cli.session_cmd", "activity"),
    "add": ("memtomem.cli.memory", "add"),
    "agent": ("memtomem.cli.agent_cmd", "agent"),
    "config": ("memtomem.cli.config_cmd", "config"),
    "context": ("memtomem.cli.context_cmd", "context"),
    "doctor": ("memtomem.cli.doctor_cmd", "doctor"),
    "embedding-reset": ("memtomem.cli.embedding_cmd", "embedding_reset"),
    "gc": ("memtomem.cli.gc_cmd", "gc"),
    "index": ("memtomem.cli.indexing", "index"),
    "ingest": ("memtomem.cli.ingest_cmd", "ingest"),
    "init": ("memtomem.cli.init_cmd", "init"),
    "mem": ("memtomem.cli.mem_cmd", "mem"),
    "memory": ("memtomem.cli.memory_doctor_cmd", "memory"),
    "pinned": ("memtomem.cli.pinned_cmd", "pinned"),
    "purge": ("memtomem.cli.purge_cmd", "purge"),
    "quality": ("memtomem.cli.quality_cmd", "quality"),
    "recall": ("memtomem.cli.memory", "recall"),
    "reset": ("memtomem.cli.reset_cmd", "reset"),
    "review": ("memtomem.cli.review_cmd", "review"),
    "schedule": ("memtomem.cli.schedule_cmd", "schedule"),
    "search": ("memtomem.cli.search", "search"),
    "serve": ("memtomem.cli.serve_cmd", "serve"),
    "session": ("memtomem.cli.session_cmd", "session"),
    "shell": ("memtomem.cli.shell", "shell"),
    "status": ("memtomem.cli.status_cmd", "status"),
    "sync-doctor": ("memtomem.cli.sync_doctor_cmd", "sync_doctor"),
    "tags": ("memtomem.cli.tags_cmd", "tags"),
    "uninstall": ("memtomem.cli.uninstall_cmd", "uninstall"),
    "upgrade": ("memtomem.cli.upgrade_cmd", "upgrade"),
    "warmup": ("memtomem.cli.warmup_cmd", "warmup"),
    "watchdog": ("memtomem.cli.watchdog_cmd", "watchdog"),
    "web": ("memtomem.cli.web", "web"),
    "wiki": ("memtomem.cli.wiki_cmd", "wiki"),
}


class _LazyCommands(MutableMapping[str, click.Command]):
    """``Group.commands`` that imports a subcommand's module on first lookup.

    The names are known up front, so membership, iteration and ``len`` never
    import anything; only reading a command does. ``mm --help`` still imports
    every module, because it renders each command's short help.
    """

    def __init__(self, specs: dict[str, tuple[str, str]]) -> None:
        self._specs = dict(specs)
        self._loaded: dict[str, click.Command] = {}

    def __getitem__(self, name: str) -> click.Command:
        if name not in self._loaded:
            module_path, attr = self._specs[name]
            self._loaded[name] = getattr(import_module(module_path), attr)
        return self._loaded[name]

    def __setitem__(self, name: str, command: click.Command) -> None:
        self._loaded[name] = command

    def __delitem__(self, name: str) -> None:
        if name not in self:
            raise KeyError(name)
        self._loaded.pop(name, None)
        self._specs.pop(name, None)

    # ``Mapping.__contains__`` would call ``__getitem__`` and import the module.
    def __contains__(self, name: object) -> bool:
        return name in self._loaded or name in self._specs

    def __iter__(self) -> Iterator[str]:
        yield from self._specs
        yield from (name for name in self._loaded if name not in self._specs)

    def __len__(self) -> int:
        return len(self._specs.keys() | self._loaded.keys())


@click.group(context_settings=CONTEXT_SETTINGS, commands=_LazyCommands(_LAZY_SUBCOMMANDS))
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
