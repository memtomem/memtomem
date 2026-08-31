"""Host-home pinning for context work handed to a worker thread.

``asyncio.to_thread`` cannot be cancelled. A dispatcher whose
``asyncio.timeout`` fires returns 503 while the worker keeps running, and any
user-scope path that worker resolves *afterwards* is resolved against
whatever ``$HOME`` says at that moment — not the one its caller chose. #2211
caught that with the settings engine: a cancelled sync wrote to a developer's
real ``~/.claude/settings.json`` mid-suite, blamed on whichever unrelated test
happened to be running.

The pin closes it. A dispatcher enters :func:`pinned_host_homes` immediately
before the hand-off; ``asyncio.to_thread`` copies the caller's context, so the
worker keeps the snapshot even after the caller — or the whole request — is
gone. Every layer that turns a ``~``-anchored path into an absolute one reads
the snapshot through :func:`host_home`, :func:`host_kimi_home`, or
:func:`pin_expanduser` instead of the environment.

The twin of ``context/_abandon.py``, and entered at the same place. The abort
flag decides **whether** a late write happens; the pin decides **where** it
lands. Both are needed: the abort is cooperative by design, so the write that
outruns the last checkpoint is exactly the one the pin exists for.

Extracted from ``context/settings.py`` (#2250) when the skills engines needed
it — the resolvers they go through (``scope_resolver``, ``_runtime_targets``)
must not import the settings module to learn where ``$HOME`` is. ``settings``
re-exports every name, so existing imports and the
``test_settings_dispatch_pin_guard`` rule are unchanged.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from memtomem.context._kimi_home import kimi_code_home


@dataclass(frozen=True)
class HostHomes:
    """The host directories a context sync may write to, resolved once.

    Every user-scope target is anchored on ``$HOME`` (and Kimi additionally on
    ``$KIMI_CODE_HOME``), and both are read from the *ambient process
    environment*. That is safe on a caller's own thread and unsafe the moment
    the work moves to ``asyncio.to_thread``: cancelling the awaiting task
    cannot stop the worker, so a worker that resolves its target later reads
    whatever the environment says *then* — which is how a cancelled sync came
    to write a settings file outside the home its caller intended (#2211).
    """

    home: Path
    kimi_home: Path

    @classmethod
    def capture(cls) -> HostHomes:
        """Snapshot the current environment's host homes."""
        return cls(home=Path.home(), kimi_home=kimi_code_home())


#: Kept under its original name so a context captured by one release and read
#: by another still matches (the variable is looked up by object identity, but
#: the name is what shows up in debugger dumps and in #2211's prose).
_pinned_homes: ContextVar[HostHomes | None] = ContextVar(
    "memtomem_settings_host_homes", default=None
)


@contextmanager
def pinned_host_homes(homes: HostHomes | None = None) -> Iterator[HostHomes]:
    """Pin the host homes for everything run inside this context.

    Dispatchers that hand context work to a worker thread enter this
    *before* the hand-off. ``asyncio.to_thread`` copies the caller's
    ``contextvars`` context into the worker, so the worker keeps the pinned
    snapshot even after the caller's ``with`` block — or the whole request —
    is gone. The token reset keeps the pin scoped to this context rather than
    leaking into whatever the loop runs next.
    """
    token = _pinned_homes.set(homes or HostHomes.capture())
    try:
        yield _pinned_homes.get()  # type: ignore[misc]
    finally:
        _pinned_homes.reset(token)


def host_home() -> Path:
    """The pinned ``$HOME``, or the live one when nothing pinned it.

    The live fallback keeps synchronous callers (CLI, detectors) reading the
    environment exactly as before; only the threaded dispatch paths pin.
    """
    pinned = _pinned_homes.get()
    return pinned.home if pinned is not None else Path.home()


def host_kimi_home() -> Path:
    """The pinned Kimi home, or the live one when nothing pinned it.

    Snapshotted separately because ``kimi_code_home`` consults
    ``$KIMI_CODE_HOME`` first, so deriving it from :func:`host_home` would
    miss an override and resolve a different directory than the caller saw.
    """
    pinned = _pinned_homes.get()
    return pinned.kimi_home if pinned is not None else kimi_code_home()


def pin_expanduser(path: Path) -> Path:
    """``Path.expanduser`` that honours the pin for a bare ``~`` anchor.

    The resolvers hold user-scope targets as ``~``-anchored literals
    (``Path("~/.claude/skills")``, ``DEFAULT_USER_ARTIFACT_BASE``) and used to
    absolutise them with :meth:`Path.expanduser`, which reads ``$HOME`` at
    call time — the exact hazard #2211 found in the settings engine and #2250
    found here.

    Only a leading bare ``~`` is redirected. A ``~user`` anchor names a
    specific account rather than "the caller's home", so there is nothing to
    pin and it falls through to :meth:`Path.expanduser`; so does an already
    absolute or relative path, leaving every non-``~`` caller byte-identical.

    With no pin in scope this *is* ``path.expanduser()``, so synchronous
    callers (CLI, detectors, tests) are unchanged — the same fallback contract
    as :func:`host_home`.
    """
    parts = path.parts
    if not parts or parts[0] != "~":
        return path.expanduser()
    return host_home().joinpath(*parts[1:])


__all__ = [
    "HostHomes",
    "host_home",
    "host_kimi_home",
    "pin_expanduser",
    "pinned_host_homes",
]
