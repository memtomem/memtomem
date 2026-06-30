"""TestClient pins for ``CSRFGuardMiddleware`` (RFC #787).

The middleware always emits a structured ``web.csrf.observe`` log line
per gated request. Whether a would-block decision becomes a real 403 is
governed by ``app.state.csrf_enforce``:

* In production (``create_app`` default), this is ``True`` — the gate
  enforces. ``MEMTOMEM_WEB__CSRF_ENFORCE`` ∈ {0, false, no, off} falls
  back to observe-only for emergency rollback.
* These tests build their own minimal FastAPI app rather than importing
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

from memtomem.web.app import resolve_csrf_enforce_from_env
from memtomem.web.middleware.csrf import CSRFGuardMiddleware

_OBSERVE_RE = re.compile(
    r"web\.csrf\.observe method=(?P<method>\S+) path=(?P<path>\S+) "
    r"token_ok=(?P<token_ok>\S+) origin_ok=(?P<origin_ok>\S+) "
    r"host_ok=(?P<host_ok>\S+) would_block=(?P<would_block>\S+)"
)


def _build_app(*, enforce: bool = True) -> FastAPI:
    """Build a minimal FastAPI app wired with the CSRF middleware.

    ``enforce`` defaults to ``True`` to mirror the production default.
    Pass ``enforce=False`` to exercise the observe-only rollback path.
    """
    app = FastAPI()
    app.state.csrf_token = "test-token-abc"
    app.state.csrf_trusted_hosts = frozenset()
    app.state.csrf_trusted_origins = frozenset()
    app.state.csrf_enforce = enforce
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


def test_get_api_with_hostile_host_is_gated_and_403(caplog_csrf) -> None:
    """Safe-method reads are Host/Origin gated now (RFC #787). The default
    TestClient sends ``Host: testserver`` (non-loopback), so a GET /api
    read is blocked — closing the DNS-rebinding read exposure. The token
    is not required for a GET, so the 403 is attributable to the host."""
    client = TestClient(_build_app())
    res = client.get("/api/ping")
    assert res.status_code == 403
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    ev = events[0]
    assert ev["method"] == "GET"
    assert ev["path"] == "/api/ping"
    assert ev["token_ok"] == "True"  # token not required for safe methods
    assert ev["host_ok"] == "False"
    assert ev["would_block"] == "True"


def test_get_api_with_loopback_host_passes(caplog_csrf) -> None:
    """A GET /api read from a loopback Host/Origin passes the gate.

    Passing safe-method reads log at DEBUG (the high-volume common case),
    so capture at DEBUG to see the observe line."""
    caplog_csrf.set_level(logging.DEBUG, logger="memtomem.web.middleware.csrf")
    client = TestClient(_build_app())
    res = client.get(
        "/api/ping",
        headers={"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080"},
    )
    assert res.status_code == 200
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    ev = events[0]
    assert ev["host_ok"] == "True"
    assert ev["origin_ok"] == "True"
    assert ev["would_block"] == "False"


def test_options_preflight_is_not_gated(caplog_csrf) -> None:
    """``OPTIONS`` preflights are owned by the CORS middleware and skip the
    gate entirely — no observe line, no 403 from the CSRF guard."""
    client = TestClient(_build_app())
    res = client.options("/api/ping", headers={"Host": "evil.example.com"})
    assert res.status_code != 403
    assert _parse_observe(caplog_csrf.records) == []


def test_post_without_token_returns_403(caplog_csrf) -> None:
    """No CSRF header → ``token_ok=False``; enforce mode returns 403."""
    client = TestClient(_build_app())
    res = client.post("/api/echo")
    assert res.status_code == 403
    body = res.json()
    assert "CSRF" in body["detail"] or "csrf" in body["detail"].lower()
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    ev = events[0]
    assert ev["method"] == "POST"
    assert ev["path"] == "/api/echo"
    assert ev["token_ok"] == "False"
    assert ev["would_block"] == "True"


def test_post_with_wrong_token_returns_403(caplog_csrf) -> None:
    client = TestClient(_build_app())
    res = client.post("/api/echo", headers={"X-Memtomem-CSRF": "wrong"})
    assert res.status_code == 403
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    assert events[0]["token_ok"] == "False"
    assert events[0]["would_block"] == "True"


def test_post_with_right_token_but_non_loopback_host_returns_403(caplog_csrf) -> None:
    """Default TestClient sends ``Host: testserver`` — non-loopback. A
    valid token isn't enough; the Host check still has to pass."""
    client = TestClient(_build_app())
    res = client.post("/api/echo", headers={"X-Memtomem-CSRF": "test-token-abc"})
    assert res.status_code == 403
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    ev = events[0]
    assert ev["token_ok"] == "True"
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


def test_post_with_right_token_but_attacker_origin_returns_403(caplog_csrf) -> None:
    client = TestClient(_build_app())
    res = client.post(
        "/api/echo",
        headers={
            "X-Memtomem-CSRF": "test-token-abc",
            "Host": "127.0.0.1:8080",
            "Origin": "https://evil.example.com",
        },
    )
    assert res.status_code == 403
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    ev = events[0]
    assert ev["origin_ok"] == "False"
    assert ev["would_block"] == "True"


def test_delete_without_token_returns_403(caplog_csrf) -> None:
    """DELETE — the ``<form>``-impossible method that ``fetch`` can
    still issue — is gated identically to POST."""
    client = TestClient(_build_app())
    res = client.delete("/api/thing/42")
    assert res.status_code == 403
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    assert events[0]["method"] == "DELETE"
    assert events[0]["would_block"] == "True"


def test_session_bootstrap_get_is_host_origin_checked(caplog_csrf) -> None:
    """``GET /api/session`` is token-exempt (the bootstrap) but is still
    Host/Origin gated, so a rebound origin cannot harvest the token. A
    hostile Host (default ``testserver``) 403s even though token-exempt."""
    client = TestClient(_build_app())
    res = client.get("/api/session")
    assert res.status_code == 403
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    ev = events[0]
    assert ev["token_ok"] == "True"  # bootstrap is token-exempt
    assert ev["host_ok"] == "False"
    assert ev["would_block"] == "True"


def test_session_bootstrap_get_loopback_returns_token(caplog_csrf) -> None:
    """From a loopback Host the bootstrap returns the token as before.

    Passing safe-method reads log at DEBUG, so capture at DEBUG."""
    caplog_csrf.set_level(logging.DEBUG, logger="memtomem.web.middleware.csrf")
    client = TestClient(_build_app())
    res = client.get(
        "/api/session",
        headers={"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080"},
    )
    assert res.status_code == 200
    assert res.json()["csrf"] == "test-token-abc"
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    assert events[0]["would_block"] == "False"


def test_post_to_session_subpath_requires_token(caplog_csrf) -> None:
    """A sibling of the bootstrap path (``/api/session/echo``) is a normal
    unsafe route — it requires the token like any other write."""
    client = TestClient(_build_app())
    res = client.post(
        "/api/session/echo",
        headers={"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080"},
    )
    assert res.status_code == 403
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    assert events[0]["token_ok"] == "False"
    assert events[0]["would_block"] == "True"


def test_post_to_exact_bootstrap_path_requires_token(caplog_csrf) -> None:
    """The bootstrap exemption is GET-only: an unsafe method to the *exact*
    bootstrap path must still present the token (no path carve-out). Guards
    against a future POST /api/session route silently bypassing CSRF."""
    client = TestClient(_build_app())
    res = client.post(
        "/api/session",
        headers={"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080"},
    )
    assert res.status_code == 403
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    assert events[0]["token_ok"] == "False"
    assert events[0]["would_block"] == "True"


def test_observe_mode_passes_through_with_log_line(caplog_csrf) -> None:
    """Emergency rollback path: ``csrf_enforce=False`` makes the gate
    observe-only — would-block decisions reach the handler with a
    structured log line for an operator to grep."""
    client = TestClient(_build_app(enforce=False))
    res = client.post("/api/echo")
    assert res.status_code == 200, "observe mode must not block"
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    ev = events[0]
    assert ev["token_ok"] == "False"
    assert ev["would_block"] == "True"


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
    app.state.csrf_enforce = True
    app.add_middleware(CSRFGuardMiddleware)

    @app.post("/some/other/path")
    async def non_api() -> dict[str, str]:
        return {"ok": "yes"}

    client = TestClient(app)
    res = client.post("/some/other/path")
    assert res.status_code == 200
    assert _parse_observe(caplog_csrf.records) == []


# ---------------------------------------------------------------------------
# MEMTOMEM_WEB__CSRF_ENFORCE env override
# ---------------------------------------------------------------------------


def test_env_override_default_is_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing env var → enforce-on. Default-safe."""
    monkeypatch.delenv("MEMTOMEM_WEB__CSRF_ENFORCE", raising=False)
    assert resolve_csrf_enforce_from_env() is True


@pytest.mark.parametrize("disable_value", ["0", "false", "FALSE", "no", "off", " False "])
def test_env_override_disable_tokens(monkeypatch: pytest.MonkeyPatch, disable_value: str) -> None:
    """Explicit disable tokens fall back to observe-only. Whitespace and
    case variants normalize."""
    monkeypatch.setenv("MEMTOMEM_WEB__CSRF_ENFORCE", disable_value)
    assert resolve_csrf_enforce_from_env() is False


@pytest.mark.parametrize("on_value", ["1", "true", "yes", "on", "ture", "anything-else"])
def test_env_override_unknown_values_keep_enforce(
    monkeypatch: pytest.MonkeyPatch, on_value: str
) -> None:
    """A typo'd or unrecognized value fails safe: enforce stays on.
    Only the explicit disable tokens turn it off."""
    monkeypatch.setenv("MEMTOMEM_WEB__CSRF_ENFORCE", on_value)
    assert resolve_csrf_enforce_from_env() is True


# ---------------------------------------------------------------------------
# Production-posture end-to-end pin
# ---------------------------------------------------------------------------
#
# The autouse fixture in ``conftest.py`` defaults the rest of the suite to
# observe-only so route-unit tests don't have to thread a token. That
# fixture hides exactly the regression class flagged in PR #958 code
# review — an SPA mutator bypassing ``ensureCsrfToken()`` would still go
# green. This test bypasses the fixture by clearing the env explicitly,
# builds the *real* app via ``create_app``, and asserts the middleware
# returns 403 for an unsafe ``/api/...`` request without a token. It is
# the canonical "does the production wiring actually enforce" pin.


def test_production_create_app_enforces_csrf_without_token(
    monkeypatch: pytest.MonkeyPatch, caplog_csrf
) -> None:
    """``create_app`` wires the middleware in enforce mode by default;
    an unsafe ``/api/...`` request without the token returns 403, and
    the observe log proves the 403 came from the **token** check (not
    a host/origin fallback that would also 403 the same request).
    """
    from memtomem.web.app import create_app

    monkeypatch.delenv("MEMTOMEM_WEB__CSRF_ENFORCE", raising=False)
    app = create_app(lifespan=None, mode="prod")
    assert app.state.csrf_enforce is True, (
        "create_app must default to enforce mode when MEMTOMEM_WEB__CSRF_ENFORCE is unset"
    )

    client = TestClient(app)
    # Send loopback Host + Origin so the host/origin checks pass — this
    # isolates the 403 to the token check. Otherwise TestClient's default
    # ``Host: testserver`` would 403 the request via ``host_ok=False`` even
    # if token validation were silently bypassed.
    res = client.post(
        "/api/csrf-production-pin",
        headers={"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080"},
    )
    assert res.status_code == 403, (
        "Production-posture create_app should 403 unsafe /api requests "
        "without a CSRF token. The autouse conftest fixture must not be "
        "leaking into this test."
    )
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1, f"expected one observe event, got {events}"
    ev = events[0]
    assert ev["token_ok"] == "False", "403 must be attributable to the token check"
    assert ev["host_ok"] == "True", "loopback host must pass the host check"
    assert ev["origin_ok"] == "True", "loopback origin must pass the origin check"
    assert ev["would_block"] == "True"
