"""``POST /api/embedding-reset`` restarts file watching, and says what happened.

A degraded ``mm web`` start leaves the file watcher stopped, so auto-indexing
is off. The MCP server has recovered from that in-process since #2181; ``mm
web`` only told the user to restart, because its degraded startup never built a
watcher for the reset to start (#2188). It builds one now, and this reset
starts it.

The message is the contract here: a user who is told auto-indexing is back must
actually have it, and one whose recovery failed must be told to restart rather
than left believing a silent success.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from memtomem.web.app import create_app

DEGRADED = {
    "stored": {"provider": "none", "model": "", "dimension": 0},
    "configured": {"provider": "onnx", "model": "bge-m3", "dimension": 1024},
}


def _make_app(*, mismatch: dict | None, watcher=None, started: bool | None = None):
    from memtomem.web.deps import require_configured
    from memtomem.web.routes.system import _require_localhost

    storage = SimpleNamespace(
        embedding_mismatch=mismatch,
        reset_embedding_meta=AsyncMock(),
    )
    config = SimpleNamespace(
        embedding=SimpleNamespace(
            provider="onnx",
            model="bge-m3",
            dimension=1024,
            max_sequence_tokens=8192,
        )
    )
    app = create_app(lifespan=None, mode="prod")
    app.state.storage = storage
    app.state.config = config
    # What the lifespan publishes: an instance in every mode, started only when
    # the embedding was healthy.
    app.state.file_watcher = watcher
    app.state.file_watcher_started = (mismatch is None) if started is None else started
    app.state.file_watcher_resume_blocked = False
    app.dependency_overrides[require_configured] = lambda: None
    # TestClient's peer host is "testclient", which the loopback gate rejects;
    # that gate has its own coverage in test_qa_audit_pins.py.
    app.dependency_overrides[_require_localhost] = lambda: None
    return app, storage


def _post(app, times: int = 1):
    with TestClient(app, base_url="http://127.0.0.1") as client:
        # Loopback base URL + the per-process token: this is an unsafe method
        # on ``/api/*``, so ``CSRFGuardMiddleware`` blocks it otherwise.
        headers = {"X-Memtomem-CSRF": app.state.csrf_token}
        responses = []
        for _ in range(times):
            resp = client.post("/api/embedding-reset", headers=headers)
            assert resp.status_code == 200
            assert resp.json()["ok"] is True
            responses.append(resp.json()["message"])
            # What a real reset does: the mismatch is gone on the next call.
            app.state.storage.embedding_mismatch = None
    return responses


def test_a_healthy_server_is_told_nothing_about_watching() -> None:
    """The watcher is already running, so there is no news to report."""
    watcher = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    app, storage = _make_app(mismatch=None, watcher=watcher)

    (message,) = _post(app)

    storage.reset_embedding_meta.assert_awaited_once()
    assert "File watching" not in message
    assert "restart `mm web`" not in message
    watcher.start.assert_not_awaited()


def test_a_degraded_server_starts_watching_again() -> None:
    """The fix itself: recovery, not a restart instruction.

    Before #2188 this branch could only apologise — there was no watcher
    instance in the process to start.
    """
    watcher = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    app, _storage = _make_app(mismatch=DEGRADED, watcher=watcher)

    (message,) = _post(app)

    watcher.start.assert_awaited_once()
    assert "File watching is on again" in message
    assert "restart `mm web`" not in message
    assert app.state.file_watcher_started is True


def test_a_second_reset_does_not_start_a_duplicate_watcher() -> None:
    """Recovery is idempotent.

    ``FileWatcher.start`` overwrites its observer and task handles, so a
    second start would strand the first pair with nothing able to stop them.
    """
    watcher = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    app, _storage = _make_app(mismatch=DEGRADED, watcher=watcher)

    first, second = _post(app, times=2)

    watcher.start.assert_awaited_once()
    assert "File watching is on again" in first
    assert "File watching" not in second


def test_a_failed_start_is_reported_and_stays_retryable() -> None:
    """A reset whose recovery failed must not read as a success.

    The reset the user asked for did happen — search is repaired — so the call
    succeeds; the sentence is what carries the rest.
    """
    watcher = SimpleNamespace(start=AsyncMock(side_effect=OSError("no inotify")), stop=AsyncMock())
    app, _storage = _make_app(mismatch=DEGRADED, watcher=watcher)

    (message,) = _post(app)

    assert "still off" in message
    assert "run this reset again" in message
    assert app.state.file_watcher_started is False
    assert app.state.file_watcher_resume_blocked is False
    # Whatever the failed start left running was stopped, not left behind.
    watcher.stop.assert_awaited_once()


def test_a_retry_after_a_failed_start_can_succeed() -> None:
    """The retry the previous test promises is real."""
    watcher = SimpleNamespace(
        start=AsyncMock(side_effect=[OSError("no inotify"), None]), stop=AsyncMock()
    )
    app, _storage = _make_app(mismatch=DEGRADED, watcher=watcher)

    first, second = _post(app, times=2)

    assert "run this reset again" in first
    assert "File watching is on again" in second
    assert app.state.file_watcher_started is True


def test_a_start_whose_cleanup_failed_bars_further_attempts() -> None:
    """A watcher that could not be stopped must not be started over.

    ``stop`` clears the observer and task handles only on the way out, so a
    failed cleanup leaves both live where a retry's ``start`` would replace
    them — and nothing would ever stop what the first attempt left running.
    Only a restart recovers, and the message says so.
    """
    watcher = SimpleNamespace(
        start=AsyncMock(side_effect=OSError("no inotify")),
        stop=AsyncMock(side_effect=OSError("observer thread wedged")),
    )
    app, _storage = _make_app(mismatch=DEGRADED, watcher=watcher)

    first, second = _post(app, times=2)

    assert "could not be cleaned up" in first
    assert "restart `mm web`" in first
    assert app.state.file_watcher_resume_blocked is True
    # The second reset must not call ``start`` again on the barred instance.
    assert watcher.start.await_count == 1
    assert "cannot be started here" in second
    assert "restart `mm web`" in second


@pytest.mark.parametrize("publishes_state", [True, False], ids=["stated", "absent"])
def test_a_server_holding_no_watcher_still_asks_for_a_restart(publishes_state: bool) -> None:
    """The pre-#2188 shape, and any future one that publishes no instance.

    There is nothing to start, so the honest answer is the old one. The
    ``absent`` case drops the state attributes entirely: a caller that never
    published them must not be read as "already watching" and silently told
    everything is fine.
    """
    app, _storage = _make_app(mismatch=DEGRADED, watcher=None, started=False)
    if not publishes_state:
        del app.state.file_watcher_started
        del app.state.file_watcher_resume_blocked

    (message,) = _post(app)

    assert "cannot be started here" in message
    assert "restart `mm web`" in message
