"""CSRF / Origin / Host guard middleware for the local Web UI.

Implements RFC #787. Three concerns gated at one place so the AST
registry test in ``tests/test_web_invariants_registry.py`` sees a single
seam:

1. ``X-Memtomem-CSRF`` header must match the per-process token in
   ``app.state.csrf_token``. The SPA fetches the token from
   ``GET /api/session`` and threads it through the ``api(...)`` helper.
2. When ``Origin`` (or ``Referer`` if ``Origin`` is absent) is present, it
   must be this server's own origin — the scheme, host and port the request
   itself was addressed to — or name an operator-trusted hostname. A page
   served from another loopback port is another origin; without this rule
   it could send requests that reach the handler. The app answers no CORS
   request, so such a page cannot read a response either.
3. ``Host`` header must be in the loopback or operator-trusted allow-list
   — defends DNS rebinding, where the socket peer is ``127.0.0.1`` but
   the browser's URL bar (and the ``Host:`` header) is attacker-controlled.

Every gated request also emits a structured ``web.csrf.observe`` log
record carrying the four decision flags (``token_ok``, ``origin_ok``,
``host_ok``, ``would_block``) and the request shape (method, path), so
an operator can audit refusals after the fact. Enforcement is governed
by ``app.state.csrf_enforce`` (default ``True`` in production via
``resolve_csrf_enforce_from_env``); setting
``MEMTOMEM_WEB__CSRF_ENFORCE`` to one of ``0`` / ``false`` / ``no`` /
``off`` falls back to observe-only for emergency rollback.

Invariants:

* Host / Origin are validated for **every** ``/api/*`` request, including
  safe-method reads — this is the DNS-rebinding boundary and must not
  depend on the HTTP method. A rebound origin must not be able to read
  ``GET /api/export`` or harvest the token from ``GET /api/session``. The
  middleware short-circuits only on non-``/api/*`` paths (SPA bootstrap,
  static assets). ``OPTIONS`` is checked like any other method.
* The ``X-Memtomem-CSRF`` token is required for every unsafe method
  (``POST``/``PUT``/``PATCH``/``DELETE``) on ``/api/*``, including the
  bootstrap path itself. Safe methods never carry a token; ``GET
  /api/session`` (the bootstrap) is reachable for that reason, but — like
  every ``/api/*`` GET — it is still Origin / Host checked, so a rebound
  origin cannot harvest the token undetected.
"""

from __future__ import annotations

import logging
import secrets
from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger(__name__)


_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

# The port a URL means when it names none. A scheme outside this table has
# no default, so its origin never equals an ``http`` or ``https`` one.
_DEFAULT_PORTS: dict[str, int] = {"http": 80, "https": 443}

# Methods that mutate state and therefore require the CSRF token. Every
# other method still passes through the Host/Origin gate — it only skips
# the token check. Only non-``/api/*`` paths bypass the middleware entirely.
_UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# The SPA fetches its token here via GET. The route is reachable without a
# token only because GET is a safe method (``dispatch`` keys the token check
# on the method, not this path) — an unsafe method to this path is still
# token-gated. Kept as a named protocol constant (the web-invariants registry
# note references it).
_TOKEN_BOOTSTRAP_PATH = "/api/session"


def _strip_port(host_header: str | None) -> str | None:
    """Return the host portion of a ``Host:`` header, dropping the port.

    Bracketed IPv6 (``[::1]:8080``) needs care — a naive ``rsplit(":",
    1)`` would shave the trailing ``1]`` off ``::1``. Strip the brackets
    first, then drop the port if present.
    """
    if not host_header:
        return None
    h = host_header.strip().lower()
    if h.startswith("["):
        # IPv6 literal: ``[::1]`` or ``[::1]:8080``
        end = h.find("]")
        if end == -1:
            return None
        return h[1:end]
    # IPv4 / hostname: split on the last colon iff it looks like a port
    if ":" in h:
        head, tail = h.rsplit(":", 1)
        if tail.isdigit():
            return head
    return h


def _origin_of(url: str | None) -> tuple[str, str, int | None] | None:
    """Return ``(scheme, hostname, port)`` of an ``Origin`` or ``Referer`` value.

    The port is the explicit one, or the scheme's default when the URL names
    none. ``None`` when the value has no hostname (``null``) or does not
    parse, which includes a port that is not a number.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if not host:
        return None
    scheme = parts.scheme.lower()
    return scheme, host, port if port is not None else _DEFAULT_PORTS.get(scheme)


def _own_origin(request: Request) -> tuple[str, str, int | None] | None:
    """Return the origin this request was addressed to, or ``None``.

    The scheme is the connection's (``https`` when a proxy uvicorn trusts
    forwarded it), and the host and port are the ``Host`` header's. Nothing
    here depends on the bind address, so a forwarded port is still the
    server's own origin as the browser sees it. A request with no ``Host``
    has none.
    """
    return _origin_of(f"{request.url.scheme}://{request.headers.get('host', '')}")


def request_carries_csrf_token(request: Request) -> bool:
    """Whether ``request`` carries this process's CSRF token.

    The middleware asks it of every unsafe method. A safe-method route whose
    answer is harmless to give but which also records something (a search's
    history) asks it to decide whether to record.
    """
    expected = getattr(request.app.state, "csrf_token", None)
    if not expected:
        # If the app has no token configured (degraded startup),
        # don't pretend the check passed — flag it so the observe
        # log surfaces a misconfiguration rather than silent success.
        return False
    provided = request.headers.get("X-Memtomem-CSRF")
    if not provided:
        return False
    # Compare bytes: Starlette decodes header values as latin-1, and
    # ``compare_digest`` raises ``TypeError`` on a non-ASCII ``str`` rather
    # than reporting the mismatch. ``latin-1`` restores the bytes as sent.
    return secrets.compare_digest(provided.encode("latin-1"), expected.encode())


class CSRFGuardMiddleware(BaseHTTPMiddleware):
    """Observe-mode CSRF / Origin / Host guard.

    Reads its allow-list and toggles from ``app.state``:

    * ``app.state.csrf_token`` — the per-process token (``str``).
    * ``app.state.csrf_trusted_hosts`` — set of extra hostnames the
      operator opted in via ``--trusted-host``. May be empty.
    * ``app.state.csrf_trusted_origins`` — set of hostnames the operator
      opted in via ``--trusted-origin``; an origin on one of them passes
      whatever its port. May be empty.
    * ``app.state.csrf_enforce`` — bool. Defaults to ``True`` in production;
      set ``MEMTOMEM_WEB__CSRF_ENFORCE=0`` for emergency rollback to
      observe-only.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        method = request.method.upper()

        # Only ``/api/*`` is gated. Non-API paths (SPA bootstrap, static
        # assets) fall through untouched.
        if not path.startswith("/api/"):
            return await call_next(request)

        # Host / Origin are the DNS-rebinding boundary: validate them on
        # every ``/api/*`` request, including safe-method reads, so a
        # rebound origin cannot read ``GET /api/export`` or harvest the
        # token from ``GET /api/session``. The CSRF token is required for
        # every unsafe method — including the bootstrap path itself. The
        # ``GET /api/session`` bootstrap is token-exempt only because GET is
        # a safe method (no path carve-out), so an unsafe method to that path
        # is still token-gated.
        token_required = method in _UNSAFE_METHODS
        token_ok = self._check_token(request) if token_required else True
        origin_ok = self._check_origin(request)
        host_ok = self._check_host(request)
        would_block = not (token_ok and origin_ok and host_ok)

        # Single structured log line per gated request. Uses positional
        # ``%`` args so a logging filter that drops the formatted message
        # still sees the raw fields. Tests assert via ``caplog``.
        #
        # Unsafe methods and any would-block decision log at INFO (observe-mode
        # rollback + audit trail). Passing safe-method reads (``GET``/``HEAD``)
        # are the high-volume common case for the SPA, so they log at DEBUG to
        # avoid flooding operator logs.
        level = logging.INFO if (method in _UNSAFE_METHODS or would_block) else logging.DEBUG
        logger.log(
            level,
            "web.csrf.observe method=%s path=%s token_ok=%s origin_ok=%s host_ok=%s would_block=%s",
            method,
            path,
            token_ok,
            origin_ok,
            host_ok,
            would_block,
        )

        enforce = bool(getattr(request.app.state, "csrf_enforce", False))
        if would_block and enforce:
            detail = {"detail": "CSRF / origin / host check failed"}
            # Only a token refusal after the browser-origin boundary passes
            # permits client recovery. The handler has not run; Host/Origin
            # failures and observe-only requests never carry this signal.
            if token_required and not token_ok and origin_ok and host_ok:
                detail["code"] = "csrf_token_mismatch"
            return JSONResponse(content=detail, status_code=403)

        return await call_next(request)

    @staticmethod
    def _check_token(request: Request) -> bool:
        return request_carries_csrf_token(request)

    @staticmethod
    def _check_origin(request: Request) -> bool:
        origin_hdr = request.headers.get("origin") or request.headers.get("referer")
        if origin_hdr is None:
            # No Origin / Referer is the curl / non-browser shape. The
            # token gate carries the load there; we don't synthesize a
            # browser-origin failure when the request isn't from a
            # browser at all.
            return True
        origin = _origin_of(origin_hdr)
        if origin is None:
            return False
        host = origin[1]
        trusted: frozenset[str] = getattr(request.app.state, "csrf_trusted_origins", frozenset())
        if host in trusted:
            # The operator named this hostname; any port and scheme of it.
            return True
        if host not in _LOOPBACK_HOSTS:
            return False
        # A loopback name is not an identity: every local server has one.
        # Only the origin this request was addressed to is the UI's own.
        return origin == _own_origin(request)

    @staticmethod
    def _check_host(request: Request) -> bool:
        host = _strip_port(request.headers.get("host"))
        if host is None:
            # No Host header is HTTP/1.0 or a malformed proxy; treat as
            # untrusted rather than passing.
            return False
        if host in _LOOPBACK_HOSTS:
            return True
        trusted: frozenset[str] = getattr(request.app.state, "csrf_trusted_hosts", frozenset())
        return host in trusted
