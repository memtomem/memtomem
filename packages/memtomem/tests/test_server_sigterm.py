"""Test ``_install_sigterm_handler`` (issue #387).

Python's default SIGTERM behavior bypasses ``atexit``, so the
``.server.pid`` unlink registered in ``main()`` never fires when the
server is killed via SIGTERM (the signal ``pkill`` and supervisord send
by default). The handler installed by ``_install_sigterm_handler`` calls
``sys.exit(0)``, which *does* run ``atexit``.
"""

from __future__ import annotations

import signal

import pytest

from memtomem.server import _install_sigterm_handler


def test_install_sigterm_handler_registers_for_sigterm(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, h: captured.setdefault(sig, h))

    _install_sigterm_handler()

    assert signal.SIGTERM in captured, "_install_sigterm_handler must bind SIGTERM"


def test_sigterm_handler_exits_cleanly_so_atexit_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The handler must call sys.exit(0) — that's what runs atexit.

    Returning normally from the handler would leave the interpreter
    blocked exactly where it was when the signal arrived, defeating the
    point. Calling os._exit (or non-zero exit) would skip atexit. Only
    ``sys.exit(0)`` triggers the cleanup chain.
    """
    captured: dict[int, object] = {}
    monkeypatch.setattr(signal, "signal", lambda sig, h: captured.setdefault(sig, h))

    _install_sigterm_handler()

    handler = captured[signal.SIGTERM]
    with pytest.raises(SystemExit) as exc_info:
        handler(signal.SIGTERM, None)  # type: ignore[operator]

    assert exc_info.value.code == 0, (
        "SIGTERM handler must exit with code 0 so atexit unlinks .server.pid"
    )
