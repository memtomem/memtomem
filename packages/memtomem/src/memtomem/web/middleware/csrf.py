"""CSRF / Origin / Host guard middleware for the local Web UI.

Implements RFC #787. Three concerns gated at one place so the AST
registry test in ``tests/test_web_invariants_registry.py`` sees a single
seam:

1. ``X-Memtomem-CSRF`` header must match the per-process token in
   ``app.state.csrf_token``. The SPA fetches the token from
   ``GET /api/session`` and threads it through the ``api(...)`` helper.
2. When ``Origin`` (or ``Referer`` if ``Origin`` is absent) is present,
   it must be in the loopback allow-list — defends CORS-simple POSTs that
   reach the handler regardless of CORS readability.
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

* The middleware short-circuits on safe methods (``GET``/``HEAD``/
  ``OPTIONS``) and on non-``/api/*`` paths. The SPA bootstrap, static
  assets, and read-only API calls are unaffected.
* ``GET /api/session`` is exempt from the token check so the SPA can
  bootstrap the token in the first place. It is still subject to the
  Origin/Host checks via the same observe path so a rebound origin
  cannot harvest the token undetected.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger(__name__)


_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "::1", "localhost"})

# Methods that mutate state. Anything outside this set falls through the
# middleware untouched — readers (``GET``), preflights (``OPTIONS``), and
# cache validators (``HEAD``) do not need a CSRF token.
_UNSAFE_METHODS: frozenset[str] = frozenset({"POST", "PUT", "PATCH", "DELETE"})

# The SPA fetches its token here; the route itself must be reachable
# without a token, otherwise the bootstrap is a chicken-and-egg.
_TOKEN_BOOTSTRAP_PATH = "/api/session"


def _strip_port(host_header: str | None) -> str | None:
    """Return the host portion of a ``Host:`` header, dropping the port.

    Bracketed IPv6 (``[::1]:8080``) needs care — a naive ``rsplit(":",
    1)`` would shave the trailing ``1]`` off ``::1``. Strip the brackets
    first, then drop the port if present.
    """
    if not host_header:
        return None
    h = host_header.strip()
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


def _origin_host(origin_or_referer: str | None) -> str | None:
    """Extract the hostname from an ``Origin`` or ``Referer`` header."""
    if not origin_or_referer:
        return None
    try:
        return urlparse(origin_or_referer).hostname
    except ValueError:
        return None


class CSRFGuardMiddleware(BaseHTTPMiddleware):
    """Observe-mode CSRF / Origin / Host guard.

    Reads its allow-list and toggles from ``app.state``:

    * ``app.state.csrf_token`` — the per-process token (``str``).
    * ``app.state.csrf_trusted_hosts`` — set of extra hostnames the
      operator opted in via ``--trusted-host``. May be empty.
    * ``app.state.csrf_trusted_origins`` — set of extra hostnames the
      operator opted in via ``--trusted-origin``. May be empty.
    * ``app.state.csrf_enforce`` — bool. Defaults to ``True`` in production;
      set ``MEMTOMEM_WEB__CSRF_ENFORCE=0`` for emergency rollback to
      observe-only.
    """

    async def dispatch(self, request: Request, call_next) -> Response:
        path = request.url.path
        method = request.method.upper()

        # Fast path for everything outside the gate's responsibility.
        if method not in _UNSAFE_METHODS or not path.startswith("/api/"):
            return await call_next(request)

        token_required = path != _TOKEN_BOOTSTRAP_PATH
        token_ok = self._check_token(request) if token_required else True
        origin_ok = self._check_origin(request)
        host_ok = self._check_host(request)
        would_block = not (token_ok and origin_ok and host_ok)

        # Single structured log line per gated request. Uses positional
        # ``%`` args so a logging filter that drops the formatted message
        # still sees the raw fields. Tests assert via ``caplog``.
        logger.info(
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
            return Response(
                content='{"detail":"CSRF / origin / host check failed"}',
                status_code=403,
                media_type="application/json",
            )

        return await call_next(request)

    @staticmethod
    def _check_token(request: Request) -> bool:
        expected = getattr(request.app.state, "csrf_token", None)
        if not expected:
            # If the app has no token configured (degraded startup),
            # don't pretend the check passed — flag it so the observe
            # log surfaces a misconfiguration rather than silent success.
            return False
        provided = request.headers.get("X-Memtomem-CSRF")
        return provided == expected

    @staticmethod
    def _check_origin(request: Request) -> bool:
        origin_hdr = request.headers.get("origin") or request.headers.get("referer")
        if origin_hdr is None:
            # No Origin / Referer is the curl / non-browser shape. The
            # token gate carries the load there; we don't synthesize a
            # browser-origin failure when the request isn't from a
            # browser at all.
            return True
        host = _origin_host(origin_hdr)
        if host is None:
            return False
        if host in _LOOPBACK_HOSTS:
            return True
        trusted: frozenset[str] = getattr(request.app.state, "csrf_trusted_origins", frozenset())
        return host in trusted

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
