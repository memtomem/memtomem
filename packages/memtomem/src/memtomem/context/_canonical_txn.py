"""Canonical-mutation lock primitives (ADR-0030 §6, PR-B2a).

Every first-party write to a canonical context artifact — reverse import
(Pull), web CRUD, wiki install/update, cross-scope transfer, flat→dir migrate,
and the version/label operations — must serialize against every other such
write on the *same artifact* across processes. Before PR-B2a only the skills
reverse-import path held a destination sidecar lock; agents/commands reverse
imports, web canonical CRUD (the in-process ``_gateway_lock`` only), wiki
install/update, and the version-create snapshot read all wrote/read canonicals
unguarded, and ``migrate``/``transfer`` locked the *file path* (``.foo.md.lock``
for flat, ``<name>/.<file>.lock`` for dir) — which does not serialize against a
name-keyed lock on the same artifact.

This module is the one authority for the canonical lock. Its identity is
**name-keyed and layout-independent**: ``<canonical_root>/.{name}.lock`` guards
the flat file ``<root>/<name>.md`` and the dir working file
``<root>/<name>/<agent|command>.md`` (and the skills tree ``<root>/<name>/``)
alike, so the two layouts of one name — and a Pull racing a flat→dir migrate —
can never write concurrently. This is byte-identical to the lock the skills
importer already takes (``_lock_path_for(canonical_root / skill)``).

**Lock order (normative, ADR-0030 §6).** Within the context-artifact domain:

    1. the canonical name-keyed sidecar lock(s) — ``<root>/.{name}.lock``,
       sorted by ``str`` when more than one is taken at once, then
    2. the child sidecar the operation needs — ``versions.json`` (version /
       label ops, via :func:`create_version` etc.) or the wiki ``lock.json``
       (install/update). The two children are parallel, never nested with each
       other.

Never acquire a canonical sidecar while holding a child lock. This module
imports :mod:`memtomem.context.versioning`, never the reverse. This is a
different domain from the memory-file lock order in
:mod:`memtomem.context._atomic` (L0–L4) and never nests with it.

**Event-loop callers.** :func:`_file_lock` is a *blocking* ``LOCK_EX`` — calling
it synchronously on an event loop can deadlock (a suspended lock-holder task can
never release; see the L2 note in :mod:`memtomem.context._atomic`). Async web
handlers run these helpers in a worker thread (``asyncio.to_thread``) with the
in-process ``_gateway_lock`` held; ``_gateway_lock`` is what serializes
in-process callers so the worker-thread flock never self-contends.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator, Literal, TypeVar

from memtomem.context._atomic import _file_lock, _lock_path_for, atomic_write_bytes
from memtomem.context._names import Layout

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

__all__ = [
    "acquire_canonical_locks",
    "canonical_lock_path",
    "canonical_lock_shared_budget",
    "canonical_sidecar_lock",
    "new_lock_budget",
    "versioning_op_locked",
    "write_canonical_locked",
]

# Whole-call acquisition budget (seconds) for a canonical sidecar lock. Mirrors
# ``skills._SKILLS_LOCK_BUDGET_S`` so a stuck holder surfaces as a typed
# ``lock_timeout`` skip / HTTP 503 rather than an unbounded wait. Monkeypatchable
# by dotted path in tests, matching the ``_atomic`` budget convention.
_CANONICAL_LOCK_BUDGET_S: float = 30.0

# Outcome of a locked canonical write, mapped by callers onto their existing
# imported / skipped result rows. (PR-B2b adds the snapshot behind
# ``"overwritten"``.)
WriteOutcome = Literal["created", "overwritten", "exists"]


def new_lock_budget() -> Callable[[], float]:
    """Return a whole-call remaining-budget closure for canonical sidecar locks.

    One monotonic deadline of ``_CANONICAL_LOCK_BUDGET_S`` for the WHOLE extract
    call — the closure returns the seconds left on each acquisition, so a batch
    import of N artifacts is bounded by one budget, not N·budget (the
    ``skills._SKILLS_LOCK_BUDGET_S`` shape; #1145 orphan-thread guard). A
    thread-offloaded web/MCP caller can never be wedged past its route timeout
    by a stuck cross-process holder.
    """
    deadline = time.monotonic() + _CANONICAL_LOCK_BUDGET_S

    def _remaining() -> float:
        return max(0.0, deadline - time.monotonic())

    return _remaining


def canonical_lock_path(canonical_root: Path, name: str) -> Path:
    """Sidecar-lock path for the canonical artifact *name* under *canonical_root*.

    Name-keyed and **layout-independent**: ``<canonical_root>/.{name}.lock``.
    Byte-identical to the lock the skills importer already takes
    (``_lock_path_for(canonical_root / skill)``), so every writer of one
    artifact name — flat file, dir working file, transfer, migrate — contends
    on exactly one lock.

    *canonical_root* is ``.resolve()``d here so callers that pass a
    non-normalized root (e.g. the wiki installer's
    ``project_root/.memtomem/<type>`` vs ``scope_resolver.canonical_artifact_dir``'s
    already-resolved path) still land on the SAME lock file — two callers
    computing different lock paths for one artifact would silently fail to
    serialize. ``resolve()`` is idempotent for the already-resolved callers.
    """
    return _lock_path_for(canonical_root.resolve() / name)


@contextmanager
def canonical_sidecar_lock(
    canonical_root: Path, name: str, *, timeout: float | None = None
) -> Iterator[None]:
    """Hold the cross-process canonical sidecar lock for *name*.

    The base primitive: every first-party canonical mutation runs inside this.
    ``timeout=None`` blocks indefinitely (CLI default, Ctrl-C-able); an async
    web caller offloading to a worker thread passes a bound and maps the
    resulting ``TimeoutError`` to a typed skip / 503.
    """
    with _file_lock(canonical_lock_path(canonical_root, name), timeout=timeout):
        yield


@contextmanager
def acquire_canonical_locks(
    specs: Sequence[tuple[Path, str]], *, timeout: float | None = None
) -> Iterator[None]:
    """Hold canonical sidecar locks for several ``(canonical_root, name)`` at once.

    Locks are taken in ``sorted(key=str)`` order and de-duplicated, so every
    caller acquires an overlapping set in one global sequence — no lock-order
    inversion (the ``migrate._acquire_pair_lock`` discipline, generalized to
    name identity and N locks). *timeout* is one WHOLE-CALL monotonic budget
    shared across all acquisitions (the second lock gets whatever the first
    left), matching ``_acquire_pair_lock`` — a caller bounding its worst-case
    wait at N seconds must not discover it can stall for k·N. ``None`` blocks
    indefinitely. On expiry ``_file_lock`` raises ``TimeoutError`` having
    acquired nothing further; locks already held unwind via the stack.

    Used by cross-scope transfer (two canonical roots) and any future multi-
    artifact canonical transaction; single-artifact callers use
    :func:`canonical_sidecar_lock`.
    """
    lock_paths = sorted({canonical_lock_path(root, name) for root, name in specs}, key=str)
    deadline = None if timeout is None else time.monotonic() + timeout

    def _remaining() -> float | None:
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    with ExitStack() as stack:
        for lock_path in lock_paths:
            stack.enter_context(_file_lock(lock_path, timeout=_remaining()))
        yield


@contextmanager
def canonical_lock_shared_budget(
    canonical_root: Path, name: str, *, timeout: float | None
) -> Iterator[Callable[[], float | None]]:
    """Hold the canonical name lock and yield a remaining-budget callable.

    For a multi-step canonical mutation that also takes a child sidecar lock
    downstream — the wiki installer's ``copy → reconcile → Lockfile.upsert``.
    Acquires the canonical lock (C0) with *timeout*, then yields a ``remaining()``
    the caller forwards as the child lock's (``lock.json``) ``lock_timeout`` so
    ONE monotonic deadline spans both acquisitions (the M1 nested-budget hazard:
    two independent 30s budgets under a 60s route timeout can orphan the worker).
    ``timeout=None`` blocks indefinitely (CLI) and ``remaining()`` returns
    ``None`` (the child also blocks).
    """
    deadline = None if timeout is None else time.monotonic() + timeout

    def _remaining() -> float | None:
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    with canonical_sidecar_lock(canonical_root, name, timeout=_remaining()):
        yield _remaining


def versioning_op_locked(
    artifact_dir: Path,
    *,
    timeout: float | None,
    op: Callable[[float | None], _T],
) -> _T:
    """Run a versioning op under the canonical name lock (ADR-0030 §6 order).

    Acquires the canonical sidecar lock for this artifact
    (``artifact_dir.parent`` / ``artifact_dir.name`` — dir layout, which every
    versioning op requires) FIRST, then calls *op* with the remaining shared
    budget to forward to the versioning call's own ``lock_timeout`` — so the
    order is canonical sidecar → ``versions.json``, and one *timeout* budget
    (seconds; ``None`` blocks — CLI default) spans both acquisitions rather than
    granting each a fresh allowance (the M1 nested-budget hazard).

    ``create_version`` needs this because its snapshot read of the working file
    must serialize against a concurrent Pull/CRUD; ``promote_label`` /
    ``delete_label`` need it because a bare ``versions.json`` mutation can race a
    transfer that moves the whole artifact directory out from under them
    (ADR-0030 §6 / Codex B4).
    """
    deadline = None if timeout is None else time.monotonic() + timeout

    def _remaining() -> float | None:
        return None if deadline is None else max(0.0, deadline - time.monotonic())

    with canonical_sidecar_lock(artifact_dir.parent, artifact_dir.name, timeout=_remaining()):
        return op(_remaining())


def write_canonical_locked(
    canonical_root: Path,
    name: str,
    content_bytes: bytes,
    *,
    resolve_target: Callable[[], tuple[Path, Layout]],
    overwrite: bool,
    lock_timeout: float | None = None,
) -> tuple[WriteOutcome, Path, Layout]:
    """Resolve the canonical destination and write *content_bytes* under the lock.

    One transaction under ``canonical_root/.{name}.lock``. The destination is
    resolved **inside** the lock via *resolve_target* (typically
    ``resolve_artifact_extract_target``) and returned, so a concurrent
    flat→dir migrate that converts the layout while this call waits on the lock
    cannot make the caller write a stale (now-divergent) path — the resolution
    observes the post-migrate layout. Returns ``(outcome, dst, layout)``:

    - ``dst`` absent → ``mkdir`` + atomic write → ``("created", dst, layout)``.
    - ``dst`` present and not *overwrite* → no write → ``("exists", …)`` (the
      caller emits its existing ``canonical_exists`` skip).
    - ``dst`` present and *overwrite* → atomic write → ``("overwritten", …)``.

    PR-B2a only serializes the write (behavior otherwise unchanged — overwrite
    clobbers). PR-B2b inserts the pre-image snapshot on the overwrite branch.

    ``TimeoutError`` (sidecar lock unavailable within *lock_timeout*) propagates
    to the caller, which maps it to a typed ``lock_timeout`` skip / 503.
    """
    with canonical_sidecar_lock(canonical_root, name, timeout=lock_timeout):
        dst, layout = resolve_target()
        if dst.exists():
            if not overwrite:
                return "exists", dst, layout
            atomic_write_bytes(dst, content_bytes)
            return "overwritten", dst, layout
        dst.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_bytes(dst, content_bytes)
        return "created", dst, layout
