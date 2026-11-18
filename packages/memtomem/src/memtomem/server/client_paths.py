"""Where a relative path argument of a server tool starts.

A server that one client launched shares that client's working directory, and
a relative argument resolves against it with no help. A server that serves
several clients does not: its working directory belongs to none of them. Such
a server puts each client's directory on the connection, as
``AppContext.client_cwd``, and the functions here start a relative argument
there.

With ``client_cwd`` unset, which is every shipped transport, they run the
expression their call sites ran before this module existed,
``Path(raw).expanduser()`` and ``Path(raw).expanduser().resolve()``. The
process working directory is then read where it always was, inside
``resolve()``.

Three tools need the directory before they open the store, or without ever
opening it: ``mem_ingest`` resolves its argument ahead of its dry-run exit,
and the two context tools only read the filesystem. They use the ``ctx``
forms below, which read the field off the connection's context and do not
initialize it. A tool module never holds an uninitialized context itself
(``test_handlers_use_get_app_initialized``).
"""

from __future__ import annotations

from pathlib import Path

from memtomem.server.context import AppContext, CtxType, _get_app
from memtomem.storage.sqlite_helpers import join_path_base


def client_cwd_of(app: object) -> Path | None:
    """The client's working directory on this connection, or ``None``.

    ``None`` for no context, and for a stand-in that has no such attribute:
    the suite calls tools with a bare namespace for a context, and the CLI
    hands the shared helpers its own components object.
    """
    return getattr(app, "client_cwd", None)


def connection_client_cwd(ctx: CtxType) -> Path | None:
    """:func:`client_cwd_of` for the connection behind ``ctx``, which is not initialized.

    ``None`` when a tool is called with no ``ctx``, as the suite calls some.
    """
    return client_cwd_of(_get_app(ctx)) if ctx is not None else None


def anchor_client_path(app: AppContext | None, raw: str) -> Path:
    """``raw`` with ``~`` expanded, started at the client's directory if relative.

    Not resolved: for a caller whose own helper resolves in its own way. The
    Windows shapes that are neither absolute nor plainly relative are
    :func:`~memtomem.storage.sqlite_helpers.join_path_base`'s.
    """
    path = Path(raw).expanduser()
    client_cwd = client_cwd_of(app)
    if client_cwd is None or path.is_absolute():
        return path
    return Path(join_path_base(client_cwd, path, raw))


def resolve_client_path(app: AppContext | None, raw: str) -> Path:
    """``raw`` as an absolute, resolved path, relative to the client's directory."""
    return anchor_client_path(app, raw).resolve()


def resolve_connection_path(ctx: CtxType, raw: str) -> Path:
    """:func:`resolve_client_path` from ``ctx``, without initializing its context."""
    return resolve_client_path(_get_app(ctx) if ctx is not None else None, raw)
