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
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from memtomem.web.app import resolve_csrf_enforce_from_env
from memtomem.web.middleware.csrf import (
    CSRFGuardMiddleware,
    _origin_of,
    _own_origin,
    _strip_port,
)

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

    @app.put("/api/thing/{tid}")
    async def put_thing(tid: str) -> dict[str, str]:
        return {"put": tid}

    @app.patch("/api/thing/{tid}")
    async def patch_thing(tid: str) -> dict[str, str]:
        return {"patched": tid}

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


def test_options_is_gated_like_any_other_method(caplog_csrf) -> None:
    """Nothing answers preflights, so ``OPTIONS`` has no reason to skip the
    gate: a hostile ``Host`` is refused and the decision is logged. No token
    is asked of it; ``OPTIONS`` is not an unsafe method."""
    client = TestClient(_build_app())
    res = client.options("/api/ping", headers={"Host": "evil.example.com"})
    assert res.status_code == 403
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    assert events[0]["method"] == "OPTIONS"
    assert events[0]["token_ok"] == "True"
    assert events[0]["host_ok"] == "False"


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


# Starlette decodes header bytes as latin-1, so a byte >= 0x80 reaches the
# token check as a non-ASCII ``str`` — which ``secrets.compare_digest`` refuses
# with ``TypeError`` instead of answering "no match" (#2729).
@pytest.mark.parametrize("token", [b"\xff", b"test-token-abc\xe9"], ids=["byte", "suffix"])
@pytest.mark.parametrize("enforce", [True, False], ids=["enforce", "observe"])
def test_post_with_non_ascii_token_is_a_mismatch(caplog_csrf, token: bytes, enforce: bool) -> None:
    client = TestClient(_build_app(enforce=enforce))
    res = client.post(
        "/api/echo",
        headers={
            "X-Memtomem-CSRF": token,
            "Host": "127.0.0.1:8080",
            "Origin": "http://127.0.0.1:8080",
        },
    )
    assert res.status_code == (403 if enforce else 200)
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


_OWN_ORIGIN = {"Host": "127.0.0.1:8080", "Origin": "http://127.0.0.1:8080"}


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/api/echo"),
        ("PUT", "/api/thing/42"),
        ("PATCH", "/api/thing/42"),
        ("DELETE", "/api/thing/42"),
    ],
)
def test_every_unsafe_method_is_refused_for_its_missing_token(
    caplog_csrf, method: str, path: str
) -> None:
    """PUT, PATCH and DELETE — the methods a ``<form>`` cannot send but
    ``fetch`` can — need the token exactly as POST does.

    The request comes from the UI's own origin, so the Host and Origin
    checks pass and the token is the only thing missing. Sent with the
    default ``Host: testserver`` the same request is refused by the Host
    check, whether or not its method is in ``_UNSAFE_METHODS``.
    """
    client = TestClient(_build_app())
    res = client.request(method, path, headers=_OWN_ORIGIN)
    assert res.status_code == 403
    assert res.json().get("code") == "csrf_token_mismatch"
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    ev = events[0]
    assert ev["method"] == method
    assert ev["token_ok"] == "False"
    assert ev["origin_ok"] == "True"
    assert ev["host_ok"] == "True"
    assert ev["would_block"] == "True"

    # The control: the route exists, and the token is all it lacked.
    with_token = client.request(
        method, path, headers={**_OWN_ORIGIN, "X-Memtomem-CSRF": "test-token-abc"}
    )
    assert with_token.status_code == 200


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


@pytest.mark.parametrize(
    ("origin", "trusted_origins", "origin_ok"),
    [
        # A loopback origin is not the origin of a request addressed to
        # ``share.example.com``; the Host check passing does not make it one.
        ("http://127.0.0.1:8080", frozenset(), False),
        ("http://share.example.com", frozenset({"share.example.com"}), True),
    ],
)
def test_trusted_host_allow_list_unblocks_host_check(
    caplog_csrf, origin: str, trusted_origins: frozenset[str], origin_ok: bool
) -> None:
    """Operator-supplied ``--trusted-host`` entries pass the Host check. The
    Origin check is a separate decision."""
    app = _build_app()
    app.state.csrf_trusted_hosts = frozenset({"share.example.com"})
    app.state.csrf_trusted_origins = trusted_origins
    client = TestClient(app)
    res = client.post(
        "/api/echo",
        headers={
            "X-Memtomem-CSRF": "test-token-abc",
            "Host": "share.example.com",
            "Origin": origin,
        },
    )
    assert res.status_code == (200 if origin_ok else 403)
    events = _parse_observe(caplog_csrf.records)
    assert len(events) == 1
    assert events[0]["host_ok"] == "True"
    assert events[0]["origin_ok"] == str(origin_ok)
    assert events[0]["would_block"] == str(not origin_ok)


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


@pytest.mark.parametrize(
    ("host", "origin", "classified"),
    [
        ("localhost", "http://localhost", True),
        ("localhost", "https://evil.example", False),
        ("evil.example", "http://localhost", False),
        ("evil.example", "https://evil.example", False),
    ],
)
def test_recovery_signal_requires_valid_host_and_origin(host, origin, classified):
    app = _build_app()
    applied = []

    @app.post("/api/mutate")
    async def mutate():
        applied.append(True)
        return {"ok": True}

    with TestClient(app, base_url=f"http://{host}") as client:
        result = client.post("/api/mutate", headers={"Origin": origin, "X-Memtomem-CSRF": "old"})
    assert result.status_code == 403
    assert (result.json().get("code") == "csrf_token_mismatch") is classified
    assert applied == []


def test_rotated_token_refuses_before_handler_then_accepts_explicit_retry():
    app = _build_app()
    applied = []

    @app.post("/api/mutate")
    async def mutate():
        applied.append(True)
        return {"ok": True}

    with TestClient(app, base_url="http://localhost") as client:
        old = client.get("/api/session").json()["csrf"]
        app.state.csrf_token = "rotated-token"
        refused = client.post("/api/mutate", headers={"X-Memtomem-CSRF": old})
        assert refused.json()["code"] == "csrf_token_mismatch"
        assert not applied
        fresh = client.get("/api/session").json()["csrf"]
        assert not applied
        assert client.post("/api/mutate", headers={"X-Memtomem-CSRF": fresh}).status_code == 200
        assert applied == [True]


# Direct contract tests for the host parsing helpers (#2984).


class TestStripPort:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("[::1]:8080", "::1"),
            ("[::1]", "::1"),
            ("[::1", None),
            ("", None),
            ("  Example.COM:443  ", "example.com"),
            ("a:b:8080", "a:b"),
        ],
    )
    def test_host_values(self, value: str, expected: str | None) -> None:
        assert _strip_port(value) == expected


class TestOriginOf:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("http://[::1]:8080", ("http", "::1", 8080)),
            ("HTTP://LocalHost", ("http", "localhost", 80)),
            ("https://localhost", ("https", "localhost", 443)),
            ("https://localhost:8443/page?q=1", ("https", "localhost", 8443)),
            ("tauri://localhost", ("tauri", "localhost", None)),
            ("null", None),
            ("", None),
            (None, None),
            ("http://[bad", None),
            ("http://localhost:abc", None),
            ("http://localhost:99999", None),
        ],
    )
    def test_origin_values(self, value: str | None, expected: tuple | None) -> None:
        assert _origin_of(value) == expected


class TestOwnOrigin:
    @pytest.mark.parametrize(
        ("scheme", "headers", "expected"),
        [
            ("http", [(b"host", b"127.0.0.1:8080")], ("http", "127.0.0.1", 8080)),
            ("https", [(b"host", b"localhost")], ("https", "localhost", 443)),
            # No ``Host``: no origin, so no loopback ``Origin`` equals it.
            ("http", [], None),
            ("http", [(b"host", b"")], None),
            ("http", [(b"host", b"localhost:abc")], None),
        ],
    )
    def test_request_values(
        self, scheme: str, headers: list[tuple[bytes, bytes]], expected: tuple | None
    ) -> None:
        scope = {
            "type": "http",
            "scheme": scheme,
            "path": "/api/session",
            "server": ("127.0.0.1", 8080),
            "headers": headers,
        }
        assert _own_origin(Request(scope)) == expected
