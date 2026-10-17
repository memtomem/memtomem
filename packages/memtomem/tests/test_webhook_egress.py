"""Webhook egress policy enforced below the connection pool (#2728).

``_validate_webhook_url`` only ever saw IP literals, so a hostname that
resolves to loopback (``localtest.me``), a numeric form ``ipaddress`` does
not parse (``127.1``, ``2130706433``) or a mixed public+private DNS answer
all reached the socket. The policy now runs in ``EgressBackend.connect_tcp``
on the addresses the socket is about to use.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import socket
import time

import anyio
import httpcore
import httpx
import pytest

from memtomem.config import WebhookConfig
from memtomem.egress_policy import blocked_address_reason
from memtomem.server.webhook_egress import (
    EgressBackend,
    EgressTransport,
    WebhookEgressRefused,
    build_transport,
    egress_refusal,
)
from memtomem.server.webhooks import WebhookManager, _validate_webhook_url

PUBLIC_A = "93.184.216.34"
PUBLIC_B = "93.184.216.35"


def _answer(*ips: str):
    """A resolver whose answer is the given addresses, in order."""

    async def resolver(host, port, *, type=0, **_kw):
        out = []
        for ip in ips:
            if ":" in ip:
                out.append((socket.AF_INET6, socket.SOCK_STREAM, 0, "", (ip, port, 0, 0)))
            else:
                out.append((socket.AF_INET, socket.SOCK_STREAM, 0, "", (ip, port)))
        return out

    return resolver


class _Stream:
    def __init__(self, addr: str) -> None:
        self.addr = addr
        self.closed = False

    async def aclose(self) -> None:
        # Like anyio's ``SocketStream.aclose``, a close has an await in it.
        await anyio.sleep(0)
        self.closed = True


class _Connects:
    """Stand-in for ``AnyIOBackend.connect_tcp``: per-address outcome."""

    def __init__(self, outcomes: dict[str, object]) -> None:
        self.outcomes = outcomes
        self.calls: list[str] = []
        self.streams: dict[str, _Stream] = {}

    async def __call__(self, host, port, timeout=None, local_address=None, socket_options=None):
        self.calls.append(host)
        outcome = self.outcomes.get(host, "ok")
        if outcome == "fail":
            raise httpcore.ConnectError(f"refused by {host}")
        if outcome == "stall":
            await anyio.sleep(30)
        if outcome == "completes-anyway":
            # A connector that has the socket open when the group is cancelled
            # and still returns it: the stream exists before the await, and
            # the cancellation is swallowed so the caller receives it.
            stream = _Stream(host)
            self.streams[host] = stream
            try:
                await anyio.sleep(30)
            except anyio.get_cancelled_exc_class():
                pass
            return stream
        stream = _Stream(host)
        self.streams[host] = stream
        return stream


@pytest.fixture
def connects(monkeypatch):
    def install(outcomes: dict[str, object] | None = None) -> _Connects:
        fake = _Connects(outcomes or {})

        # A function, so it binds as a method: ``super().connect_tcp`` in the
        # backend passes the instance, which a bare callable would swallow.
        async def connect_tcp(_backend, host, port, **kwargs):
            return await fake(host, port, **kwargs)

        monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", connect_tcp)
        return fake

    return install


# ----------------------------------------------------------------------------
# classifier
# ----------------------------------------------------------------------------


class TestBlockedAddressReason:
    @pytest.mark.parametrize(
        "address",
        [
            "0.0.0.1",
            "10.0.0.1",
            "100.64.0.1",  # carrier-grade NAT: no ipaddress flag on 3.12/3.13
            "127.0.0.1",
            "127.1.2.3",
            "169.254.169.254",
            "172.16.0.1",
            "192.0.0.1",
            "192.0.2.1",
            "192.168.1.1",
            "198.18.0.1",
            "198.51.100.1",
            "203.0.113.1",
            "224.0.0.1",
            "240.0.0.1",
            "255.255.255.255",
            "::",
            "::1",
            "100::1",
            "2001:db8::1",
            "fc00::1",
            "fd12::1",
            "fe80::1",
            "fe80::1%eth0",
            "fec0::1",  # site-local: only is_site_local is set
            "ff02::1",
            "::ffff:10.0.0.1",  # IPv4-mapped
            "::ffff:127.0.0.1",
            "2002:7f00:1::",  # 6to4 of 127.0.0.1
            "2002:808:808::",  # 6to4 of 8.8.8.8: the tunnel prefix is blocked outright
            "64:ff9b::7f00:1",  # NAT64 of 127.0.0.1
            "2001:0:7f00:1::",  # Teredo prefix
            "2001:0:4136:e378:8000:63bf:3fff:fdd2",  # Teredo with a public client
        ],
    )
    def test_blocked(self, address):
        assert blocked_address_reason(address) is not None

    @pytest.mark.parametrize(
        "address",
        [
            "8.8.8.8",
            PUBLIC_A,
            "::ffff:8.8.8.8",  # IPv4-mapped: the embedded address decides
            "64:ff9b::808:808",  # NAT64 of 8.8.8.8: likewise
            "2606:4700::1",
        ],
    )
    def test_allowed(self, address):
        """Independent of the ``ipaddress`` flags on the carrier prefix:
        ``::ffff:0:0/96`` is ``is_reserved`` on some CPython patch releases
        and not others (CI's 3.12 and a local 3.12.11 disagreed)."""
        assert blocked_address_reason(address) is None

    def test_accepts_parsed_addresses(self):
        assert blocked_address_reason(ipaddress.ip_address("10.0.0.1")) == "in 10.0.0.0/8"
        assert blocked_address_reason(ipaddress.ip_address("8.8.8.8")) is None

    def test_mem_fetch_shares_the_classifier(self):
        from memtomem.indexing.url_fetcher import _is_blocked_address

        assert _is_blocked_address(ipaddress.ip_address("100.64.0.1")) is True
        assert _is_blocked_address(ipaddress.ip_address("fec0::1")) is True
        assert _is_blocked_address(ipaddress.ip_address(PUBLIC_A)) is False


# ----------------------------------------------------------------------------
# backend
# ----------------------------------------------------------------------------


class TestEgressBackend:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("host", "answer"),
        [
            ("localtest.me", ("127.0.0.1", "::1")),
            ("127.1", ("127.0.0.1",)),  # numeric form the resolver canonicalises
            ("2130706433", ("127.0.0.1",)),
            ("mixed.test", (PUBLIC_A, "10.0.0.1")),  # any blocked address refuses the answer
            ("meta.test", ("169.254.169.254",)),
        ],
    )
    async def test_refuses_blocked_answers_before_connecting(self, connects, host, answer):
        fake = connects()
        backend = EgressBackend(resolver=_answer(*answer))
        with pytest.raises(WebhookEgressRefused) as info:
            await backend.connect_tcp(host, 443, timeout=5.0)
        assert info.value.host == host
        assert fake.calls == []

    @pytest.mark.asyncio
    async def test_connects_to_an_allowed_answer(self, connects):
        fake = connects()
        backend = EgressBackend(resolver=_answer(PUBLIC_A))
        stream = await backend.connect_tcp("hooks.example", 443, timeout=5.0)
        assert isinstance(stream, _Stream) and stream.addr == PUBLIC_A
        assert fake.calls == [PUBLIC_A]

    @pytest.mark.asyncio
    async def test_falls_back_when_the_first_address_refuses(self, connects):
        fake = connects({PUBLIC_A: "fail"})
        backend = EgressBackend(resolver=_answer(PUBLIC_A, PUBLIC_B))
        stream = await backend.connect_tcp("hooks.example", 443, timeout=5.0)
        assert stream.addr == PUBLIC_B
        assert fake.calls == [PUBLIC_A, PUBLIC_B]

    @pytest.mark.asyncio
    async def test_a_stalled_first_address_does_not_block_the_second(self, connects):
        """The stagger: the second candidate starts after 0.25 s and wins; the
        stalled attempt is cancelled rather than running out the deadline."""
        fake = connects({PUBLIC_A: "stall"})
        backend = EgressBackend(resolver=_answer(PUBLIC_A, PUBLIC_B))
        started = time.monotonic()
        stream = await backend.connect_tcp("hooks.example", 443, timeout=10.0)
        elapsed = time.monotonic() - started
        assert stream.addr == PUBLIC_B
        assert fake.calls == [PUBLIC_A, PUBLIC_B]
        assert elapsed < 5.0, f"stalled candidate held the connect for {elapsed:.1f}s"
        assert PUBLIC_A not in fake.streams, "the cancelled attempt never produced a stream"

    @pytest.mark.asyncio
    async def test_ipv4_is_tried_second_when_every_aaaa_record_stalls(self, connects):
        """anyio's order: first IPv6, then first IPv4, then the rest. In
        resolver order the three stalled AAAA answers would take three
        staggers (0.75 s) and the 0.6 s connect timeout would expire before
        the reachable A record was tried."""
        v6 = ["2606:4700::1", "2606:4700::2", "2606:4700::3"]
        fake = connects({a: "stall" for a in v6})
        backend = EgressBackend(resolver=_answer(*v6, PUBLIC_A))
        stream = await backend.connect_tcp("hooks.example", 443, timeout=0.6)
        assert stream.addr == PUBLIC_A
        assert fake.calls == [v6[0], PUBLIC_A]

    @pytest.mark.asyncio
    async def test_a_losing_stream_is_closed_despite_the_cancellation(self, connects):
        """The first candidate's connector hands back a stream after the second
        has already won: that stream must be closed, not left to the GC."""
        fake = connects({PUBLIC_A: "completes-anyway"})
        backend = EgressBackend(resolver=_answer(PUBLIC_A, PUBLIC_B))
        stream = await backend.connect_tcp("hooks.example", 443, timeout=10.0)
        assert stream.addr == PUBLIC_B
        assert fake.streams[PUBLIC_A].closed is True
        assert fake.streams[PUBLIC_B].closed is False

    @pytest.mark.asyncio
    async def test_all_addresses_failing_raises_the_last_error(self, connects):
        connects({PUBLIC_A: "fail", PUBLIC_B: "fail"})
        backend = EgressBackend(resolver=_answer(PUBLIC_A, PUBLIC_B))
        with pytest.raises(httpcore.ConnectError, match=PUBLIC_B):
            await backend.connect_tcp("hooks.example", 443, timeout=5.0)

    @pytest.mark.asyncio
    async def test_slow_resolver_hits_the_connect_timeout(self, connects):
        fake = connects()

        async def slow(host, port, *, type=0, **_kw):
            await anyio.sleep(30)
            return []

        backend = EgressBackend(resolver=slow)
        with pytest.raises(httpcore.ConnectTimeout):
            await backend.connect_tcp("hooks.example", 443, timeout=0.2)
        assert fake.calls == []

    @pytest.mark.asyncio
    async def test_empty_answer_is_a_connect_error(self, connects):
        connects()
        backend = EgressBackend(resolver=_answer())
        with pytest.raises(httpcore.ConnectError, match="no address"):
            await backend.connect_tcp("hooks.example", 443, timeout=5.0)

    @pytest.mark.asyncio
    async def test_resolver_failure_is_a_connect_error_not_a_refusal(self, connects):
        """``gaierror`` is what the stock backend maps to ``ConnectError``
        inside ``anyio.connect_tcp``; resolving ahead of it keeps the mapping."""
        fake = connects()

        async def nxdomain(host, port, *, type=0, **_kw):
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")

        backend = EgressBackend(resolver=nxdomain)
        with pytest.raises(httpcore.ConnectError, match="could not resolve") as info:
            await backend.connect_tcp("nxdomain.test", 443, timeout=5.0)
        assert not isinstance(info.value, WebhookEgressRefused)
        assert isinstance(info.value.__cause__, socket.gaierror)
        assert fake.calls == []


# ----------------------------------------------------------------------------
# transport / client
# ----------------------------------------------------------------------------


class TestEgressTransport:
    @pytest.mark.asyncio
    async def test_refusal_surfaces_through_the_client(self, connects):
        """Pins that the swapped pool is the one the transport uses: an httpx
        release that renames ``_pool`` would deliver unguarded, and this test
        would connect to a loopback-answering name instead of refusing."""
        connects()
        transport = EgressTransport(resolver=_answer("127.0.0.1"))
        async with httpx.AsyncClient(transport=transport, timeout=2.0) as client:
            with pytest.raises(httpx.ConnectError) as info:
                await client.post("http://localtest.me/hook", content="{}")
        assert isinstance(info.value.__cause__, WebhookEgressRefused)
        assert egress_refusal(info.value) is info.value.__cause__

    @pytest.mark.asyncio
    async def test_resolver_failure_surfaces_as_httpx_connect_error(self, connects):
        connects()

        async def nxdomain(host, port, *, type=0, **_kw):
            raise socket.gaierror(socket.EAI_NONAME, "nodename nor servname provided")

        transport = EgressTransport(resolver=nxdomain)
        async with httpx.AsyncClient(transport=transport, timeout=2.0) as client:
            with pytest.raises(httpx.ConnectError) as info:
                await client.post("http://nxdomain.test/hook", content="{}")
        assert egress_refusal(info.value) is None

    def test_build_transport_by_policy(self):
        ctx = WebhookConfig(enabled=True, url="https://hooks.example/h")
        assert isinstance(build_transport(ctx), EgressTransport)

        proxied = WebhookConfig(
            enabled=True, url="https://hooks.example/h", proxy="http://proxy.test:3128"
        )
        transport = build_transport(proxied)
        assert type(transport) is httpx.AsyncHTTPTransport
        assert isinstance(transport._pool, httpcore.AsyncHTTPProxy)

        local = WebhookConfig(enabled=True, url="http://localhost:5678/h", allow_private_hosts=True)
        transport = build_transport(local)
        assert type(transport) is httpx.AsyncHTTPTransport
        assert isinstance(transport._pool, httpcore.AsyncConnectionPool)
        assert not isinstance(transport._pool, httpcore.AsyncHTTPProxy)

    def test_egress_refusal_ignores_network_failures(self):
        assert egress_refusal(httpx.ConnectError("refused")) is None
        wrapped = httpx.ConnectError("x")
        wrapped.__cause__ = httpcore.ConnectError("y")
        assert egress_refusal(wrapped) is None


# ----------------------------------------------------------------------------
# startup validation
# ----------------------------------------------------------------------------


class TestStartupPolicy:
    @pytest.mark.parametrize("url", ["http://[fec0::1]/hook", "http://100.64.0.1/hook"])
    def test_new_ranges_rejected_as_literals(self, url):
        err = _validate_webhook_url(url)
        assert err is not None and "private/reserved" in err

    def test_allow_private_hosts_keeps_scheme_and_syntax_checks(self):
        assert (
            _validate_webhook_url("http://localhost:5678/hook", enforce_host_policy=False) is None
        )
        assert _validate_webhook_url("http://127.0.0.1/hook", enforce_host_policy=False) is None
        err = _validate_webhook_url("file:///etc/passwd", enforce_host_policy=False)
        assert err is not None and "scheme" in err

    def test_manager_with_local_receiver_stays_enabled(self):
        mgr = WebhookManager(
            WebhookConfig(enabled=True, url="http://localhost:5678/hook", allow_private_hosts=True)
        )
        assert mgr._config.enabled is True

    @pytest.mark.parametrize(
        "url", ["http://hooks.example:bad/h", "http://hooks.example:123:456/h", "http://h:70000/"]
    )
    def test_unparseable_port_is_malformed(self, url):
        """``urlparse`` parses the port lazily, so a hostname check alone
        passes these; httpx raises ``InvalidURL`` when the client is built."""
        err = _validate_webhook_url(url)
        assert err is not None and "malformed" in err and "port" in err

    @pytest.mark.parametrize("proxy", ["http://proxy.test:bad", "http://proxy.test:123:456"])
    def test_proxy_with_unparseable_port_disables_webhooks(self, proxy, caplog):
        with caplog.at_level(logging.WARNING, logger="memtomem.server.webhooks"):
            mgr = WebhookManager(
                WebhookConfig(enabled=True, url="https://hooks.example/h", proxy=proxy)
            )
        assert mgr._config.enabled is False
        assert "proxy" in caplog.text and "port" in caplog.text

    @pytest.mark.parametrize(
        ("url", "proxy"),
        [
            ("https://hooks.example/h", "http://operator:hunter2@proxy.test:bad"),
            ("http://operator:hunter2@hooks.example:bad/h", ""),
        ],
    )
    def test_rejection_log_never_carries_userinfo(self, url, proxy, caplog):
        """The rejection reason reaches the warning log; a URL's authority may
        carry credentials, so the reason names the host, never the authority."""
        with caplog.at_level(logging.WARNING, logger="memtomem.server.webhooks"):
            mgr = WebhookManager(WebhookConfig(enabled=True, url=url, proxy=proxy))
        assert mgr._config.enabled is False
        assert "port" in caplog.text
        assert "hunter2" not in caplog.text and "operator" not in caplog.text

    @pytest.mark.parametrize("url", ["http://10.0.0.1/h", "http://receiver.internal/h"])
    def test_proxy_mode_delegates_the_host_policy_at_startup_too(self, url):
        """With a proxy the proxy connects to the receiver and the address
        policy is its to enforce; startup must not refuse what delivery
        allows. Scheme, syntax and the proxy URL are still checked."""
        mgr = WebhookManager(
            WebhookConfig(enabled=True, url=url, proxy="http://proxy.example:3128")
        )
        assert mgr._config.enabled is True
        for bad in ("file:///etc/passwd", "http://receiver.internal:bad/h"):
            mgr = WebhookManager(
                WebhookConfig(enabled=True, url=bad, proxy="http://proxy.example:3128")
            )
            assert mgr._config.enabled is False

    def test_bad_proxy_url_disables_webhooks(self, caplog):
        with caplog.at_level(logging.WARNING, logger="memtomem.server.webhooks"):
            mgr = WebhookManager(
                WebhookConfig(enabled=True, url="https://hooks.example/h", proxy="ftp://proxy")
            )
        assert mgr._config.enabled is False
        assert "proxy" in caplog.text


# ----------------------------------------------------------------------------
# manager delivery
# ----------------------------------------------------------------------------


async def _drain(mgr: WebhookManager) -> None:
    await asyncio.gather(*mgr._pending_tasks, return_exceptions=True)


class TestManagerDelivery:
    @pytest.mark.asyncio
    async def test_refused_resolution_is_not_retried(self, connects, caplog):
        fake = connects()
        calls = 0

        async def resolver(host, port, *, type=0, **_kw):
            nonlocal calls
            calls += 1
            return [(socket.AF_INET, socket.SOCK_STREAM, 0, "", ("127.0.0.1", port))]

        mgr = WebhookManager(
            WebhookConfig(enabled=True, url="http://blocked.test/hook", events=["add"]),
            resolver=resolver,
        )
        try:
            with caplog.at_level(logging.DEBUG, logger="memtomem.server.webhooks"):
                await mgr.fire("add", {"file": "/a.md"})
                await _drain(mgr)
                await mgr.fire("add", {"file": "/b.md"})
                await _drain(mgr)
        finally:
            await mgr.close()
        assert calls == 2, "one resolution per event — no retry loop on a refusal"
        assert fake.calls == []
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1 and "not delivered" in warnings[0].getMessage()
        assert mgr._config.enabled is True

    @pytest.mark.asyncio
    async def test_local_receiver_delivers_and_ignores_ambient_proxy(self, monkeypatch):
        """``allow_private_hosts`` reaches a loopback receiver, and the POST
        goes direct: an ``ALL_PROXY`` pointing at a dead port would otherwise
        swallow it. Fails if env-proxy discovery is ever restored."""
        received: dict[str, object] = {}
        got = asyncio.Event()

        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            headers = {k.lower(): v for k, v in (ln.split(": ", 1) for ln in lines[1:] if ln)}
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            received["request_line"] = lines[0]
            received["headers"] = headers
            received["body"] = body
            writer.write(
                b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            writer.close()
            got.set()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
        monkeypatch.delenv("NO_PROXY", raising=False)
        monkeypatch.delenv("no_proxy", raising=False)
        mgr = WebhookManager(
            WebhookConfig(
                enabled=True,
                url=f"http://127.0.0.1:{port}/hook",
                events=["add"],
                secret="s3",
                allow_private_hosts=True,
            )
        )
        try:
            await mgr.fire("add", {"file": "/a.md"})
            await asyncio.wait_for(got.wait(), 10)
            await _drain(mgr)
        finally:
            await mgr.close()
            server.close()
            await server.wait_closed()
        assert received["request_line"] == "POST /hook HTTP/1.1"
        assert json.loads(received["body"]) == {"event": "add", "data": {"file": "/a.md"}}
        assert received["headers"]["x-webhook-signature"].startswith("sha256=")
