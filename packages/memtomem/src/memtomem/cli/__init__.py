"""memtomem CLI — unified command-line interface."""

from __future__ import annotations

import io
import sys

import click


CONTEXT_SETTINGS = {"help_option_names": ["-h", "--help"]}


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


class _LazyCommandDict(dict[str, click.Command]):
    """Dictionary of Click commands that imports lazy subcommands on demand."""

    def __init__(
        self,
        lazy_specs: dict[str, tuple[str, str]],
        initial: dict[str, click.Command] | None = None,
    ) -> None:
        super().__init__(initial or {})
        self._lazy_specs = dict(lazy_specs)

    def _load(self, name: str) -> click.Command:
        from importlib import import_module

        module_path, attr_name = self._lazy_specs[name]
        module = import_module(module_path)
        cmd = getattr(module, attr_name)
        super().__setitem__(name, cmd)
        return cmd

    def _load_all(self) -> None:
        for name in list(self._lazy_specs):
            if not super().__contains__(name):
                self._load(name)

    def __contains__(self, key: object) -> bool:
        return super().__contains__(key) or key in self._lazy_specs

    def __getitem__(self, key: str) -> click.Command:
        if super().__contains__(key):
            return super().__getitem__(key)
        if key in self._lazy_specs:
            return self._load(key)
        raise KeyError(key)

    def get(self, key: str, default: object = None) -> object:
        if super().__contains__(key):
            return super().__getitem__(key)
        if key in self._lazy_specs:
            return self._load(key)
        return default

    def __len__(self) -> int:
        return len(set(super().keys()) | set(self._lazy_specs.keys()))

    def __iter__(self):
        return iter(sorted(set(super().keys()) | set(self._lazy_specs.keys())))

    def keys(self):  # type: ignore[override]
        return (set(super().keys()) | set(self._lazy_specs.keys())).__iter__()

    def values(self):  # type: ignore[override]
        self._load_all()
        return super().values()

    def items(self):  # type: ignore[override]
        self._load_all()
        return super().items()


class _LazyGroup(click.Group):
    """Click Group that resolves subcommands lazily on first access."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.commands = _LazyCommandDict(_LAZY_SUBCOMMANDS, self.commands)

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(set(dict.keys(self.commands)) | set(_LAZY_SUBCOMMANDS.keys()))

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        return self.commands.get(cmd_name)


@click.group(cls=_LazyGroup, context_settings=CONTEXT_SETTINGS)
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

