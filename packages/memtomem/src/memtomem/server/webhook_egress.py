"""Webhook delivery transport that enforces the egress policy where the
socket opens (#2728).

The policy runs inside an httpcore network backend, below the connection
pool, rather than above httpx: the request keeps its hostname URL, so TLS
server-name indication, certificate verification, the ``Host`` header and
pool keying stay stock, and there is no window between a DNS answer and the
connect for a rebinding to slip through — the addresses classified are the
ones the socket connects to.

Imports httpx, httpcore and anyio at module level, so ``webhooks.py`` loads
this lazily from ``_get_client``: a handshake-only server session never
builds a client.
"""

from __future__ import annotations

import socket
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import anyio
import httpcore
import httpx

from memtomem.egress_policy import blocked_address_reason
from memtomem.config import WebhookConfig

# anyio's own ``connect_tcp`` stagger: the next address is tried once the
# previous attempt has been running this long without finishing.
HAPPY_EYEBALLS_DELAY = 0.25

# httpx's ``DEFAULT_LIMITS``; a bare ``httpcore.AsyncConnectionPool`` defaults
# differently, so the swapped pool spells them out.
_MAX_CONNECTIONS = 100
_MAX_KEEPALIVE = 20
_KEEPALIVE_EXPIRY = 5.0

Resolver = Callable[..., Awaitable[Sequence[tuple[Any, ...]]]]


def _ordered_addresses(infos: Sequence[tuple[Any, ...]]) -> list[str]:
    """Unique addresses from a ``getaddrinfo`` answer, in anyio's race order.

    anyio's ``connect_tcp`` puts the first IPv6 address first and the first
    IPv4 address second, so a stalled IPv6 answer costs one stagger before
    IPv4 is tried; the rest keep resolver order. Resolver order alone can
    list every AAAA before the A record, and three stalled ones would eat a
    short connect timeout before IPv4 was reached. A zone index is dropped.
    """
    seen: set[str] = set()
    v6: list[str] = []
    v4: list[str] = []
    rest: list[str] = []
    for info in infos:
        addr = str(info[4][0]).split("%", 1)[0]
        if addr in seen:
            continue
        seen.add(addr)
        if info[0] == socket.AF_INET6 and not v6:
            v6.append(addr)
        elif info[0] == socket.AF_INET and not v4:
            v4.append(addr)
        else:
            rest.append(addr)
    return v6 + v4 + rest


class WebhookEgressRefused(httpcore.ConnectError):
    """The webhook host resolved to an address the policy refuses.

    A ``ConnectError`` subclass so httpx wraps it like any other connect
    failure (``httpx.ConnectError`` raised ``from`` this), and the manager
    can tell a refusal from a network failure through ``__cause__``.
    """

    def __init__(self, host: str, address: str, reason: str) -> None:
        super().__init__(f"webhook host {host!r} resolves to {address} ({reason}); not delivered")
        self.host = host
        self.address = address
        self.reason = reason


class EgressBackend(httpcore.AnyIOBackend):
    """``AnyIOBackend`` that resolves, classifies, then races the allowed
    addresses under one deadline.

    ``connect_tcp`` is the only override. The stock backend hands the
    hostname to ``anyio.connect_tcp``, which resolves and races inside; this
    one resolves first so every answer can be classified, refuses the whole
    answer when any address is blocked (a mixed answer is how a rebinding
    attack picks the private one at connect time), and then runs the same
    staggered race anyio would over the allowed addresses.
    """

    def __init__(self, resolver: Resolver | None = None) -> None:
        self._resolver: Resolver = anyio.getaddrinfo if resolver is None else resolver

    async def connect_tcp(  # type: ignore[override]
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            # One deadline for the resolve and every candidate: httpx's
            # connect timeout, which bounded both when anyio did the resolving.
            with anyio.fail_after(timeout):
                try:
                    infos = await self._resolver(host, port, type=socket.SOCK_STREAM)
                except OSError as exc:
                    # ``socket.gaierror`` and friends: the stock backend maps
                    # these inside ``anyio.connect_tcp``; the resolve now
                    # happens before that call, so map here.
                    raise httpcore.ConnectError(f"could not resolve {host!r}: {exc}") from exc
                addrs = _ordered_addresses(infos)
                if not addrs:
                    raise httpcore.ConnectError(f"no address for {host!r}")
                for addr in addrs:
                    reason = blocked_address_reason(addr)
                    if reason is not None:
                        raise WebhookEgressRefused(host, addr, reason)
                return await self._race(addrs, port, local_address, socket_options)
        except TimeoutError as exc:
            raise httpcore.ConnectTimeout(f"connecting to {host!r} timed out") from exc

    async def _race(
        self,
        addrs: Sequence[str],
        port: int,
        local_address: str | None,
        socket_options: Any,
    ) -> httpcore.AsyncNetworkStream:
        parent_connect = super().connect_tcp
        winner: httpcore.AsyncNetworkStream | None = None
        errors: list[httpcore.ConnectError] = []

        async def attempt(addr: str, done: anyio.Event) -> None:
            nonlocal winner
            try:
                stream = await parent_connect(
                    addr,
                    port,
                    timeout=None,
                    local_address=local_address,
                    socket_options=socket_options,
                )
            except httpcore.ConnectError as exc:
                errors.append(exc)
            else:
                if winner is None:
                    winner = stream
                    tg.cancel_scope.cancel()
                else:
                    # A loser that still produced a stream: the group is being
                    # cancelled, and the cancellation would land on this await
                    # and leave the socket to the garbage collector. (On
                    # asyncio a cancelled attempt never gets its stream — the
                    # error is thrown at its connect await — so this is for a
                    # connector or backend that completes the connect anyway.)
                    with anyio.CancelScope(shield=True):
                        await stream.aclose()
            finally:
                done.set()

        async with anyio.create_task_group() as tg:
            for addr in addrs:
                done = anyio.Event()
                tg.start_soon(attempt, addr, done)
                with anyio.move_on_after(HAPPY_EYEBALLS_DELAY):
                    await done.wait()

        if winner is None:
            if errors:
                raise errors[-1]
            raise httpcore.ConnectError("no address could be connected")
        return winner


class EgressTransport(httpx.AsyncHTTPTransport):
    """``AsyncHTTPTransport`` whose pool opens sockets through ``EgressBackend``.

    ``AsyncHTTPTransport`` does not expose the backend, so the pool it built
    is replaced with one that does. The SSL context is httpx's own, built
    with ``trust_env=True`` so ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` still
    apply. ``_pool`` is private to httpx; ``test_webhook_egress.py`` pins
    that a refused connect surfaces through ``client.post``, so an httpx
    release that renames it fails there instead of delivering unguarded.
    """

    def __init__(self, *, resolver: Resolver | None = None) -> None:
        ssl_context = httpx.create_ssl_context(trust_env=True)
        super().__init__(verify=ssl_context)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=ssl_context,
            network_backend=EgressBackend(resolver),
            http1=True,
            http2=False,
            max_connections=_MAX_CONNECTIONS,
            max_keepalive_connections=_MAX_KEEPALIVE,
            keepalive_expiry=_KEEPALIVE_EXPIRY,
        )


def build_transport(
    config: WebhookConfig, *, resolver: Resolver | None = None
) -> httpx.AsyncBaseTransport:
    """The transport the manager delivers through, by policy.

    Every branch passes an explicit transport to ``httpx.AsyncClient``, which
    turns off httpx's environment-proxy discovery: webhooks never pick up an
    ambient ``HTTP(S)_PROXY``. A proxy is configured, not inherited.

    - ``proxy`` set: operator-trusted mode. The proxy resolves and connects,
      so destination policy is its job; the proxy's own address is not
      classified (corporate proxies live on private ranges).
    - ``allow_private_hosts``: a receiver on this machine or LAN; no address
      policy.
    - otherwise: resolve → classify → race → connect, direct only.
    """
    if config.proxy:
        return httpx.AsyncHTTPTransport(
            verify=httpx.create_ssl_context(trust_env=True), proxy=config.proxy
        )
    if config.allow_private_hosts:
        return httpx.AsyncHTTPTransport(verify=httpx.create_ssl_context(trust_env=True))
    return EgressTransport(resolver=resolver)


def egress_refusal(exc: BaseException) -> WebhookEgressRefused | None:
    """The refusal behind an httpx error, or ``None`` for a network failure."""
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, WebhookEgressRefused):
            return seen
        seen = seen.__cause__
    return None
