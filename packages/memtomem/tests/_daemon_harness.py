"""Run ``memtomem.server.daemon`` with test-only instrumentation (#3381).

``tests/test_server_daemon.py`` starts this file as the daemon process. The
production module carries no test hooks; everything here is a patch applied
before ``main`` runs, selected by the JSON in ``MMD_HARNESS``:

* ``drain_s``: how long teardown waits for owed answers (default 10 s).
* ``status_file``: write :meth:`Daemon.status` there every 50 ms.
* ``fail_watcher_start_once``: the first ``FileWatcher.start`` raises, so the
  owner's first initialization rolls back.
* ``gate_init``: ``create_components`` waits until this file exists.
* ``gate_rebuild``: first initialization's config rebuild waits until this
  file exists (it blocks the event loop; only the warmup test uses it).
* ``gate_close``: the owner's ``close`` waits, shielded, until this file exists.
* ``gate_status``: ``collect_status_report`` (``mem_status``) waits until this
  file exists.
* ``gate_session``: a connection whose preamble cwd is named ``gated`` waits,
  shielded, until this file exists before its session starts.
* ``fail_engine_release``: releasing the engine raises.
* ``fail_owner_close``: the owner's ``close`` raises.
* ``fail_watcher_stop``: ``FileWatcher.stop`` raises.
* ``fail_supervise``: the supervisor raises after half a second.
* ``raise_in_session``: a session whose preamble cwd is named ``boom`` raises.
* ``cancel_at`` with ``cancel_gate``: the supervisor waits for the gate file in
  place of its work, then teardown starts with whatever sessions are open, and a
  cancellation is raised at one step: ``supervise`` (the supervisor itself, at
  the gate), or ``stop_sessions``, ``engine_release`` or ``listener_close``,
  each after the step's own work. The owner's ``close`` touches
  ``<cancel_gate>.closed`` when it returns, so a test can see that the engine
  was released, which the process exit would otherwise hide.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import anyio

import memtomem.server.daemon as daemon_mod

OPTIONS = json.loads(os.environ.get("MMD_HARNESS", "{}"))


async def _wait_for(path: str, *, shield: bool = False) -> None:
    Path(path + ".entered").touch()  # lets the test act only once the gate is reached
    with anyio.CancelScope(shield=shield):
        while not Path(path).exists():
            await anyio.sleep(0.02)


def _install() -> None:
    if (drain := OPTIONS.get("drain_s")) is not None:
        daemon_mod.CONNECTION_DRAIN_S = float(drain)

    if status_file := OPTIONS.get("status_file"):
        original_serve = daemon_mod.Daemon.serve

        async def serve(self: daemon_mod.Daemon, *args: object) -> int:
            target = Path(status_file)

            async def writer() -> None:
                seq = 0  # lets a test wait for a snapshot taken after a given moment
                while True:
                    seq += 1
                    tmp = target.with_suffix(".tmp")
                    tmp.write_text(json.dumps({**self.status(), "seq": seq}))
                    os.replace(tmp, target)
                    await anyio.sleep(0.05)

            async with anyio.create_task_group() as tg:
                tg.start_soon(writer)
                try:
                    return await original_serve(self, *args)
                finally:
                    tg.cancel_scope.cancel()

        daemon_mod.Daemon.serve = serve  # type: ignore[method-assign]

    if OPTIONS.get("fail_watcher_start_once"):
        from memtomem.indexing.watcher import FileWatcher

        original_start = FileWatcher.start
        failed = {"done": False}

        async def start(self: FileWatcher) -> None:
            if not failed["done"]:
                failed["done"] = True
                raise RuntimeError("injected watcher start failure")
            await original_start(self)

        FileWatcher.start = start  # type: ignore[method-assign]

    if init_gate := OPTIONS.get("gate_init"):
        import memtomem.server.component_factory as factory

        original_create = factory.create_components

        async def create_components(*args: object, **kwargs: object) -> object:
            await _wait_for(init_gate, shield=True)  # as uncancellable as a thread offload
            return await original_create(*args, **kwargs)

        factory.create_components = create_components  # type: ignore[assignment]

    if rebuild_gate := OPTIONS.get("gate_rebuild"):
        import time

        import memtomem.server.context as context

        original_build = context.build_fresh_config

        def build_fresh_config(*args: object, **kwargs: object) -> object:
            # Only first initialization's rebuild migrates; the handshake's does not.
            if kwargs.get("migrate"):
                Path(rebuild_gate + ".entered").touch()
                while not Path(rebuild_gate).exists():
                    time.sleep(0.02)
            return original_build(*args, **kwargs)

        context.build_fresh_config = build_fresh_config  # type: ignore[assignment]

    if status_gate := OPTIONS.get("gate_status"):
        import memtomem.server.tools.status_config as status_config

        original_report = status_config.collect_status_report

        async def collect_status_report(app: object) -> dict:
            # Shielded: a handler that does not end when it is cancelled, which
            # is what teardown must still wait for.
            await _wait_for(status_gate, shield=True)
            return await original_report(app)

        status_config.collect_status_report = collect_status_report  # type: ignore[assignment]

    if session_gate := OPTIONS.get("gate_session"):
        original_run = daemon_mod.Daemon._run_session

        async def _run_session(self: daemon_mod.Daemon, conn: object, buf: object, cwd: Path):
            if cwd.name == "gated":
                await _wait_for(session_gate, shield=True)
            return await original_run(self, conn, buf, cwd)

        daemon_mod.Daemon._run_session = _run_session  # type: ignore[method-assign]

    if close_gate := OPTIONS.get("gate_close"):
        from memtomem.server.context import AppContext

        original_close = AppContext.close

        async def close(self: AppContext) -> None:
            if self._runtime_owner is None:  # the owner, not a connection's facade
                await _wait_for(close_gate, shield=True)
            await original_close(self)

        AppContext.close = close  # type: ignore[method-assign]

    if OPTIONS.get("fail_engine_release"):
        import memtomem.server.lifespan as lifespan

        async def _release_server_runtime(state: object) -> None:
            raise RuntimeError("injected engine release failure")

        lifespan._release_server_runtime = _release_server_runtime  # type: ignore[assignment]

    if OPTIONS.get("fail_owner_close"):
        from memtomem.server.context import AppContext

        async def close(self: AppContext) -> None:
            raise RuntimeError("injected owner close failure")

        AppContext.close = close  # type: ignore[method-assign]

    if OPTIONS.get("fail_watcher_stop"):
        from memtomem.indexing.watcher import FileWatcher

        async def stop(self: FileWatcher) -> None:
            raise RuntimeError("injected watcher stop failure")

        FileWatcher.stop = stop  # type: ignore[method-assign]

    if OPTIONS.get("fail_supervise"):

        async def _supervise(self: daemon_mod.Daemon) -> None:
            await anyio.sleep(0.5)
            raise RuntimeError("injected supervisor failure")

        daemon_mod.Daemon._supervise = _supervise  # type: ignore[method-assign]

    if OPTIONS.get("raise_in_session"):
        original_session = daemon_mod.Daemon._run_session

        async def _run_session(self: daemon_mod.Daemon, conn: object, buf: object, cwd: Path):
            if cwd.name == "boom":
                raise RuntimeError("injected session failure")
            return await original_session(self, conn, buf, cwd)

        daemon_mod.Daemon._run_session = _run_session  # type: ignore[method-assign]

    if step := OPTIONS.get("cancel_at"):
        cancelled = asyncio.CancelledError  # anyio.run uses the asyncio backend
        cancel_gate = OPTIONS["cancel_gate"]

        async def _supervise(self: daemon_mod.Daemon) -> None:
            await _wait_for(cancel_gate)
            if step == "supervise":
                raise cancelled()

        daemon_mod.Daemon._supervise = _supervise  # type: ignore[method-assign]

        from memtomem.server.context import AppContext

        original_owner_close = AppContext.close

        async def _close_and_mark(self: AppContext) -> None:
            await original_owner_close(self)
            Path(cancel_gate + ".closed").touch()

        AppContext.close = _close_and_mark  # type: ignore[method-assign]
        if step == "stop_sessions":
            original_stop = daemon_mod.Daemon._stop_sessions

            async def _stop_sessions(self: daemon_mod.Daemon) -> None:
                await original_stop(self)
                raise cancelled()

            daemon_mod.Daemon._stop_sessions = _stop_sessions  # type: ignore[method-assign]
        elif step == "engine_release":
            import memtomem.server.lifespan as lifespan

            original_release = lifespan._release_server_runtime

            async def _release(state: object) -> None:
                await original_release(state)  # type: ignore[arg-type]
                raise cancelled()

            lifespan._release_server_runtime = _release  # type: ignore[assignment]
        elif step == "listener_close":
            original_close = daemon_mod.Daemon._close_listener

            async def _close_listener(self: daemon_mod.Daemon, listener: object) -> None:
                await original_close(self, listener)
                raise cancelled()

            daemon_mod.Daemon._close_listener = _close_listener  # type: ignore[method-assign]
        elif step != "supervise":
            raise SystemExit(f"unknown cancel_at step {step!r}")


if __name__ == "__main__":
    _install()
    sys.exit(daemon_mod.main(sys.argv[1:]))
