"""TestClient pins for ``CSRFGuardMiddleware`` (RFC #787 stage 1).

Stage 1 is observe-only: the middleware emits a structured log line for
every gated request but never returns 403. The test suite locks both
sides of that contract — pass-through behavior AND the observe events —
so:

* PR2's flip to enforcement is a one-line change in
  ``app.state.csrf_enforce`` plus a copy-paste of the negative cases
  here with the assertion direction inverted.
* A regression that *adds* a 403 in stage 1 fails immediately (would
  break a non-CSRF caller, e.g. an MCP-only smoke that happens to ride
  the FastAPI app).

The tests build their own minimal FastAPI app rather than importing
``create_app`` because the production factory pulls in storage /
embedder / file-watcher dependencies — none of which the middleware
itself touches. Mirroring the shape of the real wiring (token in
``app.state.csrf_token``, etc.) keeps the tests honest without paying
the boot cost.
"""

from __future__ import annotations

import logging
import re

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memtomem.web.middleware.csrf import CSRFGuardMiddleware

_OBSERVE_RE = re.compile(
    r"web\.csrf\.observe method=(?P<method>\S+) path=(?P<path>\S+) "
    r"token_ok=(?P<token_ok>\S+) origin_ok=(?P<origin_ok>\S+) "
    r"host_ok=(?P<host_ok>\S+) would_block=(?P<would_block>\S+)"
)


def _build_app() -> FastAPI:
    app = FastAPI()
    app.state.csrf_token = "test-token-abc"
    app.state.csrf_trusted_hosts = frozenset()
    app.state.csrf_trusted_origins = frozenset()
    app.state.csrf_enforce = False
    app.add_middleware(CSRFGuardMiddleware)

    @app.get("/api/ping")
    async def ping() -> dict[str, str]:
        return {"pong": "ok"}

    @app.post("/api/echo")
    async def echo() -> dict[str, str]:
        return {"echo": "ok"}

    @app.delete("/api/thing/{tid}")
    async def delete_thing(tid: str) -> dict[str, str]:
        return {"deleted": tid}

    @app.get("/api/session")
    async def session() -> dict[str, str]:
        return {"csrf": app.state.csrf_token}

    @app.post("/api/session/echo")
    async def session_post() -> dict[str, str]:
        # Hits the same prefix as ``/api/session`` but is not the
        # bootstrap path — used to assert the bootstrap exemption is
        # exact-equality, not prefix-match.
        return {"prefix": "ok"}

    return app


def _parse_observe(records: list[logging.LogRecord]) -> list[dict[str, str]]:
    parsed: list[dict[str, str]] = []
    for r in records:
        m = _OBSERVE_RE.search(r.getMessage())
        if m:
            parsed.append(m.groupdict())
    return parsed


@pytest.fixture
def caplog_csrf(caplog):
    caplog.set_level(logging.INFO, logger="memtomem.web.middleware.csrf")
    return caplog


def test_get_does_not_emit_observe_event(caplog_csrf) -> None:
    """Safe methods short-circuit the middleware entirely."""
    client = TestClient(_build_app())
    res = client.get("/api/ping")
    assert res.status_code == 200
    assert _parse_observe(caplog_csrf.records) == []


def test_post_without_token_emits_would_block(caplog_csrf) -> None:
    """No CSRF header → ``token_ok=False, would_block=True``,
    but request still succeeds in observe-only mode."""
    client = TestClient(_build_app())
    res = client.post("/api/echo")
    assert res.status_code == 200, "stage 1 must not block"
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    ev = events[0]
    assert ev["method"] == "POST"
    assert ev["path"] == "/api/echo"
    assert ev["token_ok"] == "False"
    assert ev["would_block"] == "True"


def test_post_with_wrong_token_emits_would_block(caplog_csrf) -> None:
    client = TestClient(_build_app())
    res = client.post("/api/echo", headers={"X-Memtomem-CSRF": "wrong"})
    assert res.status_code == 200
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    assert events[0]["token_ok"] == "False"
    assert events[0]["would_block"] == "True"


def test_post_with_right_token_passes_token_check(caplog_csrf) -> None:
    client = TestClient(_build_app())
    res = client.post("/api/echo", headers={"X-Memtomem-CSRF": "test-token-abc"})
    assert res.status_code == 200
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    ev = events[0]
    assert ev["token_ok"] == "True"
    # Default TestClient sends Host: testserver — non-loopback. So
    # host_ok=False and would_block=True even with a valid token.
    assert ev["host_ok"] == "False"
    assert ev["would_block"] == "True"


def test_post_with_right_token_and_loopback_host_passes_all(caplog_csrf) -> None:
    client = TestClient(_build_app())
    res = client.post(
        "/api/echo",
        headers={
            "X-Memtomem-CSRF": "test-token-abc",
            "Host": "127.0.0.1:8080",
            "Origin": "http://127.0.0.1:8080",
        },
    )
    assert res.status_code == 200
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    ev = events[0]
    assert ev["token_ok"] == "True"
    assert ev["origin_ok"] == "True"
    assert ev["host_ok"] == "True"
    assert ev["would_block"] == "False"


def test_post_with_right_token_but_attacker_origin_emits_would_block(caplog_csrf) -> None:
    client = TestClient(_build_app())
    res = client.post(
        "/api/echo",
        headers={
            "X-Memtomem-CSRF": "test-token-abc",
            "Host": "127.0.0.1:8080",
            "Origin": "https://evil.example.com",
        },
    )
    assert res.status_code == 200
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    ev = events[0]
    assert ev["origin_ok"] == "False"
    assert ev["would_block"] == "True"


def test_delete_without_token_emits_would_block(caplog_csrf) -> None:
    """DELETE — the ``<form>``-impossible method that ``fetch`` can
    still issue — is gated identically to POST."""
    client = TestClient(_build_app())
    res = client.delete("/api/thing/42")
    assert res.status_code == 200
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    assert events[0]["method"] == "DELETE"
    assert events[0]["would_block"] == "True"


def test_session_bootstrap_get_is_uncovered(caplog_csrf) -> None:
    """``GET /api/session`` is a safe method — middleware doesn't gate."""
    client = TestClient(_build_app())
    res = client.get("/api/session")
    assert res.status_code == 200
    assert _parse_observe(caplog_csrf.records) == []


def test_session_bootstrap_post_is_token_exempt(caplog_csrf) -> None:
    """The token check is skipped only for the exact bootstrap path; the
    Origin / Host parts of the gate still apply."""
    client = TestClient(_build_app())
    res = client.post(
        "/api/session/echo",
        headers={"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080"},
    )
    assert res.status_code == 200
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    # ``/api/session/echo`` is *not* the bootstrap path, so token still
    # required — and missing — would_block stays True.
    assert events[0]["token_ok"] == "False"
    assert events[0]["would_block"] == "True"


def test_enforce_mode_returns_403(caplog_csrf) -> None:
    """When ``app.state.csrf_enforce`` is True, the gate returns 403.
    PR1 leaves the default at False; this test pins the wiring so PR2's
    flip is a default-only change."""
    app = _build_app()
    app.state.csrf_enforce = True
    client = TestClient(app)
    res = client.post("/api/echo")
    assert res.status_code == 403
    body = res.json()
    assert "CSRF" in body["detail"] or "csrf" in body["detail"].lower()


def test_trusted_host_allow_list_unblocks_host_check(caplog_csrf) -> None:
    """Operator-supplied ``--trusted-host`` entries pass the Host check."""
    app = _build_app()
    app.state.csrf_trusted_hosts = frozenset({"share.example.com"})
    client = TestClient(app)
    res = client.post(
        "/api/echo",
        headers={
            "X-Memtomem-CSRF": "test-token-abc",
            "Host": "share.example.com",
            "Origin": "http://127.0.0.1:8080",
        },
    )
    assert res.status_code == 200
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    assert events[0]["host_ok"] == "True"
    assert events[0]["would_block"] == "False"


def test_non_api_paths_pass_through_without_observe(caplog_csrf) -> None:
    """Static-asset paths and non-``/api/*`` routes are not gated."""
    app = FastAPI()
    app.state.csrf_token = "t"
    app.state.csrf_trusted_hosts = frozenset()
    app.state.csrf_trusted_origins = frozenset()
    app.state.csrf_enforce = False
    app.add_middleware(CSRFGuardMiddleware)

    @app.post("/some/other/path")
    async def non_api() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)
    res = client.post("/some/other/path")
    assert res.status_code == 200
    assert _parse_observe(caplog_csrf.records) == []
