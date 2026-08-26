"""``POST /api/embedding-reset`` tells the truth about auto-indexing (#2181).

The MCP server recovers from a degraded start in-process: its reset starts the
file watcher and the background services that startup skipped. ``mm web``
cannot — a degraded web startup never constructs a watcher, so there is nothing
for the reset to start. Before this, the endpoint answered a plain "ok" and the
user had no way to know that files they added would sit unindexed until they
restarted the server.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from memtomem.web.app import create_app


def _make_app(*, mismatch: dict | None, watcher_suppressed: bool | None = None):
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
    # The lifespan records this; default it from the mismatch so callers that
    # only care about the startup shape don't have to spell both out.
    app.state.watcher_suppressed_at_startup = (
        mismatch is not None if watcher_suppressed is None else watcher_suppressed
    )
    app.dependency_overrides[require_configured] = lambda: None
    # TestClient's peer host is "testclient", which the loopback gate rejects;
    # that gate has its own coverage in test_qa_audit_pins.py.
    app.dependency_overrides[_require_localhost] = lambda: None
    return app, storage


@pytest.mark.parametrize(
    "mismatch, expects_restart_hint",
    [
        (
            {
                "stored": {"provider": "none", "model": "", "dimension": 0},
                "configured": {"provider": "onnx", "model": "bge-m3", "dimension": 1024},
            },
            True,
        ),
        (None, False),
    ],
    ids=["degraded-start", "healthy-start"],
)
def test_reset_names_the_restart_only_when_the_watcher_is_missing(
    mismatch: dict | None, expects_restart_hint: bool
) -> None:
    app, storage = _make_app(mismatch=mismatch)

    # Loopback base URL + the per-process token: this is an unsafe method on
    # ``/api/*``, so ``CSRFGuardMiddleware`` blocks it otherwise.
    with TestClient(app, base_url="http://127.0.0.1") as client:
        resp = client.post(
            "/api/embedding-reset",
            headers={"X-Memtomem-CSRF": app.state.csrf_token},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    storage.reset_embedding_meta.assert_awaited_once()
    # A healthy server has a running watcher, so the hint would be wrong there
    # — this must read the pre-reset state, not just always append the caveat.
    assert ("restart `mm web`" in body["message"]) is expects_restart_hint


def test_restart_caveat_survives_a_second_reset() -> None:
    """The caveat is about this process having no watcher, which the reset
    cannot change. Deriving it from the live mismatch would drop it on the
    second call — the first reset clears the mismatch while the watcher stays
    exactly as absent as it was."""
    app, storage = _make_app(
        mismatch={
            "stored": {"provider": "none", "model": "", "dimension": 0},
            "configured": {"provider": "onnx", "model": "bge-m3", "dimension": 1024},
        }
    )

    with TestClient(app, base_url="http://127.0.0.1") as client:
        headers = {"X-Memtomem-CSRF": app.state.csrf_token}
        first = client.post("/api/embedding-reset", headers=headers)
        # What a real reset does: the mismatch is gone on the next call.
        storage.embedding_mismatch = None
        second = client.post("/api/embedding-reset", headers=headers)

    assert "restart `mm web`" in first.json()["message"]
    assert "restart `mm web`" in second.json()["message"]
