"""The web UI's own origin is the only loopback origin it accepts.

A loopback hostname identifies the machine, not the application: a dev
server, a notebook or any other local web app is also ``localhost`` on some
port. The guard therefore compares an ``Origin`` or ``Referer`` on a loopback
name with the origin the request itself was addressed to (the connection's
scheme, and the host and port of ``Host``), and the app answers no CORS
request, so a page on another origin cannot read ``GET /api/session``.

Everything here runs against the real ``create_app()`` with enforcement on.
The suite's autouse fixture turns enforcement off, so the ``app`` fixture
turns it back on.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
import uvicorn
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memtomem.web.app import create_app

from .helpers import set_home

_HEADERS = ("Origin", "Referer")


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    monkeypatch.delenv("MEMTOMEM_WEB__CSRF_ENFORCE", raising=False)
    application = create_app(lifespan=None)
    assert application.state.csrf_enforce is True
    return application


def _as_header(header: str, origin: str) -> str:
    """The value a browser sends: a bare origin, or a page URL as ``Referer``."""
    if header == "Origin" or "://" not in origin:
        return origin
    return origin + "/some/page?tab=search"


def _get_session(app: FastAPI, *, scheme: str, host: str, headers: dict[str, str]):
    client = TestClient(app, base_url=f"{scheme}://{host}")
    return client.get("/api/session", headers={"Host": host, **headers})


def _cors_headers(response) -> dict[str, str]:
    return {k: v for k, v in response.headers.items() if k.lower().startswith("access-control-")}


# (scheme of the connection, Host header, origin the browser names)
_OWN_ORIGIN = [
    pytest.param("http", "127.0.0.1:8080", "http://127.0.0.1:8080", id="default-bind"),
    pytest.param("http", "localhost:8080", "http://localhost:8080", id="localhost-name"),
    pytest.param("http", "localhost:9000", "http://localhost:9000", id="forwarded-port"),
    pytest.param("http", "[::1]:8080", "http://[::1]:8080", id="ipv6"),
    pytest.param("http", "LocalHost:8080", "http://localhost:8080", id="host-case"),
    pytest.param("http", "localhost", "http://localhost", id="http-default-port"),
    pytest.param("http", "localhost:80", "http://localhost", id="http-80-in-host-only"),
    pytest.param("http", "localhost", "http://localhost:80", id="http-80-in-origin-only"),
    pytest.param("https", "127.0.0.1:8443", "https://127.0.0.1:8443", id="https"),
    pytest.param("https", "localhost:443", "https://localhost", id="https-443-in-host-only"),
    pytest.param("https", "localhost", "https://localhost:443", id="https-443-in-origin-only"),
]


@pytest.mark.parametrize("header", _HEADERS)
@pytest.mark.parametrize(("scheme", "host", "origin"), _OWN_ORIGIN)
def test_own_origin_reads_the_session(
    app: FastAPI, scheme: str, host: str, origin: str, header: str
) -> None:
    res = _get_session(app, scheme=scheme, host=host, headers={header: _as_header(header, origin)})
    assert res.status_code == 200, res.text
    assert res.json()["csrf"] == app.state.csrf_token


def test_a_request_naming_no_origin_reads_the_session(app: FastAPI) -> None:
    """curl and other non-browser clients send neither header; the token
    gate, not the origin check, is what stands in front of their writes."""
    res = _get_session(app, scheme="http", host="127.0.0.1:8080", headers={})
    assert res.status_code == 200, res.text
    assert res.json()["csrf"] == app.state.csrf_token


# Origins that are not ``http://127.0.0.1:8080``. The first group shares a
# loopback hostname with it, which used to be enough.
_OTHER_LOOPBACK_ORIGIN = [
    pytest.param("http://127.0.0.1:5173", id="other-port"),
    pytest.param("http://localhost:3000", id="other-name-other-port"),
    pytest.param("http://localhost:8080", id="other-name-same-port"),
    pytest.param("http://[::1]:8080", id="other-family-same-port"),
    pytest.param("https://127.0.0.1:8080", id="other-scheme"),
    pytest.param("http://127.0.0.1", id="default-port"),
    pytest.param("http://127.0.0.1:80", id="port-80"),
    pytest.param("tauri://localhost", id="app-scheme"),
    pytest.param("tauri://127.0.0.1:8080", id="app-scheme-same-authority"),
    pytest.param("http://127.0.0.1:abc", id="port-not-a-number"),
    pytest.param("http://127.0.0.1:99999", id="port-out-of-range"),
]
_FOREIGN_ORIGIN = [
    pytest.param("null", id="null"),
    pytest.param("https://evil.example", id="foreign-host"),
    pytest.param("http://evil.example:8080", id="foreign-host-same-port"),
    pytest.param("http://127.0.0.1:8080@evil.example", id="own-origin-as-userinfo"),
    pytest.param("http://127.0.0.1.evil.example:8080", id="own-host-as-subdomain"),
]


@pytest.mark.parametrize("header", _HEADERS)
@pytest.mark.parametrize("origin", _OTHER_LOOPBACK_ORIGIN + _FOREIGN_ORIGIN)
def test_another_origin_does_not_read_the_session(app: FastAPI, origin: str, header: str) -> None:
    res = _get_session(
        app,
        scheme="http",
        host="127.0.0.1:8080",
        headers={header: _as_header(header, origin)},
    )
    assert res.status_code == 403, res.text
    assert app.state.csrf_token not in res.text
    assert _cors_headers(res) == {}


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("http://127.0.0.1:8080", id="own"),
        pytest.param("http://localhost:3000", id="other-loopback"),
        pytest.param("https://evil.example", id="foreign"),
    ],
)
def test_no_response_carries_a_cors_header(app: FastAPI, origin: str) -> None:
    """Not even for the UI's own origin: it never needs one, and a header
    echoed for it would be the first step back to echoing it for others."""
    res = _get_session(app, scheme="http", host="127.0.0.1:8080", headers={"Origin": origin})
    assert _cors_headers(res) == {}


_PREFLIGHT = {
    "Access-Control-Request-Method": "POST",
    "Access-Control-Request-Headers": "content-type,x-memtomem-csrf",
}


@pytest.mark.parametrize(
    ("path", "origin", "status"),
    [
        pytest.param("/api/add", "http://127.0.0.1:8080", 405, id="api-own-origin"),
        pytest.param("/api/add", "http://localhost:3000", 403, id="api-other-loopback"),
        pytest.param("/api/add", "https://evil.example", 403, id="api-foreign"),
        pytest.param("/openapi.json", "http://localhost:3000", 405, id="outside-api"),
    ],
)
def test_a_preflight_is_not_answered(app: FastAPI, path: str, origin: str, status: int) -> None:
    client = TestClient(app, base_url="http://127.0.0.1:8080")
    res = client.options(path, headers={"Origin": origin, **_PREFLIGHT})
    assert res.status_code == status, res.text
    assert _cors_headers(res) == {}


def test_options_with_a_hostile_host_is_refused(app: FastAPI) -> None:
    client = TestClient(app, base_url="http://evil.example:8080")
    res = client.options("/api/session")
    assert res.status_code == 403, res.text


# ── Operator-named hostnames ─────────────────────────────────────────────


def test_a_trusted_loopback_hostname_passes_on_any_port_and_gets_no_cors_header(
    app: FastAPI,
) -> None:
    """``--trusted-origin localhost`` keeps a loopback front proxy that
    rewrites ``Host`` working. It is a pass through the guard only: the
    response still carries nothing that lets another origin read it."""
    app.state.csrf_trusted_origins = frozenset({"localhost"})
    res = _get_session(
        app, scheme="http", host="127.0.0.1:8080", headers={"Origin": "http://localhost:3000"}
    )
    assert res.status_code == 200, res.text
    assert _cors_headers(res) == {}


def test_trusting_one_loopback_name_does_not_trust_the_others(app: FastAPI) -> None:
    app.state.csrf_trusted_origins = frozenset({"localhost"})
    res = _get_session(
        app, scheme="http", host="127.0.0.1:8080", headers={"Origin": "http://127.0.0.1:3000"}
    )
    assert res.status_code == 403, res.text


@pytest.mark.parametrize(
    ("trusted_origins", "status"),
    [
        pytest.param(frozenset(), 403, id="trusted-host-only"),
        pytest.param(frozenset({"share.example.com"}), 200, id="trusted-host-and-origin"),
    ],
)
def test_a_remote_origin_equal_to_its_host_still_needs_trusted_origin(
    app: FastAPI, trusted_origins: frozenset[str], status: int
) -> None:
    """Equality with ``Host`` is how a loopback origin is recognised as the
    UI's own. It is not a second way in for a hostname the operator did not
    name with ``--trusted-origin``."""
    app.state.csrf_trusted_hosts = frozenset({"share.example.com"})
    app.state.csrf_trusted_origins = trusted_origins
    res = _get_session(
        app,
        scheme="http",
        host="share.example.com:8080",
        headers={"Origin": "http://share.example.com:8080"},
    )
    assert res.status_code == status, res.text


# ── Behind a TLS-terminating proxy ───────────────────────────────────────


def _as_uvicorn_serves_it(app: FastAPI, monkeypatch: pytest.MonkeyPatch):
    """Wrap ``app`` the way ``mm web`` hands it to uvicorn: default proxy
    headers, which trust ``X-Forwarded-Proto`` from ``127.0.0.1`` only."""
    monkeypatch.delenv("FORWARDED_ALLOW_IPS", raising=False)
    config = uvicorn.Config(app, host="127.0.0.1", port=8080, log_config=None, ws="none")
    config.load()
    return config.loaded_app


@pytest.mark.parametrize(
    ("peer", "forwarded_proto", "trusted_origins", "status"),
    [
        pytest.param("127.0.0.1", "https", frozenset(), 200, id="scheme-forwarded"),
        pytest.param("127.0.0.1", None, frozenset(), 403, id="scheme-not-forwarded"),
        pytest.param("::1", "https", frozenset(), 403, id="proxy-uvicorn-does-not-trust"),
        pytest.param("127.0.0.1", None, frozenset({"localhost"}), 200, id="hostname-trusted"),
    ],
)
def test_https_origin_through_a_loopback_proxy(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    peer: str,
    forwarded_proto: str | None,
    trusted_origins: frozenset[str],
    status: int,
) -> None:
    """The browser is on ``https://localhost:8443``; the proxy preserves
    ``Host`` and connects over plain HTTP. The request's own origin is
    ``https`` only when uvicorn accepts the forwarded scheme."""
    app.state.csrf_trusted_origins = trusted_origins
    client = TestClient(
        _as_uvicorn_serves_it(app, monkeypatch),
        base_url="http://localhost:8443",
        client=(peer, 50000),
    )
    headers = {"Host": "localhost:8443", "Origin": "https://localhost:8443"}
    if forwarded_proto is not None:
        headers["X-Forwarded-Proto"] = forwarded_proto
    res = client.get("/api/session", headers=headers)
    assert res.status_code == status, res.text


# ── The write a page on another port could make ──────────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    set_home(monkeypatch, home_dir)
    return home_dir


@pytest.fixture
def gateway_app(app: FastAPI, home: Path, tmp_path: Path) -> FastAPI:
    project = tmp_path / "project"
    (project / ".claude").mkdir(parents=True)
    app.state.project_root = project
    app.state.storage = AsyncMock()
    app.state.config = None
    app.state.search_pipeline = None
    app.state.index_engine = None
    app.state.embedder = None
    app.state.dedup_scanner = None
    app.state.last_reload_error = None
    return app


_SKILL = {
    "name": "from-a-page",
    "content": "---\nname: from-a-page\ndescription: marker\n---\nbody\n",
    "allow_host_writes": True,
}


def _installed_skills(home: Path) -> list[str]:
    return sorted(p.relative_to(home).as_posix() for p in home.rglob("SKILL.md"))


@pytest.mark.parametrize(
    "origin",
    [
        pytest.param("http://localhost:3000", id="other-name-other-port"),
        pytest.param("http://127.0.0.1:5173", id="other-port"),
    ],
)
def test_a_page_on_another_port_cannot_write_a_user_skill(
    gateway_app: FastAPI, home: Path, origin: str
) -> None:
    """Even holding the live token (which it can no longer read), a request
    naming another loopback origin is refused before the handler runs."""
    client = TestClient(gateway_app, base_url="http://127.0.0.1:8080")
    res = client.post(
        "/api/context/skills",
        params={"target_scope": "user"},
        headers={"Origin": origin, "X-Memtomem-CSRF": gateway_app.state.csrf_token},
        json=_SKILL,
    )
    assert res.status_code == 403, res.text
    assert "code" not in res.json(), "an origin refusal must not invite a token refresh"
    assert _installed_skills(home) == []


def test_the_ui_itself_can_write_a_user_skill(gateway_app: FastAPI, home: Path) -> None:
    """The control: same request, the UI's own origin."""
    client = TestClient(gateway_app, base_url="http://127.0.0.1:8080")
    own = {"Origin": "http://127.0.0.1:8080"}
    token = client.get("/api/session", headers=own).json()["csrf"]
    res = client.post(
        "/api/context/skills",
        params={"target_scope": "user"},
        headers={**own, "X-Memtomem-CSRF": token},
        json=_SKILL,
    )
    assert res.status_code == 200, res.text
    assert _installed_skills(home) == [".memtomem/skills/from-a-page/SKILL.md"]
