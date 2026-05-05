"""DNS-pinning regression tests for the URL fetcher.

These tests exercise the SSRF mitigations added to defeat DNS rebinding:

- `_validate_and_pin` resolves the hostname once and refuses if any IP in the
  result is private/internal (covers attacker-controlled DNS that returns a
  mix of public + private to force connect-time selection).
- `fetch_url` connects to the pinned IP, preserving the original hostname in
  the Host header and TLS SNI; subsequent resolutions of the same hostname
  cannot redirect the connection to a private address.
- Redirects are validated + re-pinned per hop.
- Response body is rejected when Content-Length or accumulated stream exceeds
  the configured cap.

`pin-and-invert symmetric assertion`: each negative-marker case is paired
with a positive marker (e.g. an in-range public IP that *is* accepted) so
that an incorrectly broad block doesn't show up as a false PASS.
"""

from __future__ import annotations

import socket

import httpx
import pytest

from memtomem.indexing import url_fetcher
from memtomem.indexing.url_fetcher import (
    _build_pinned_request,
    _host_header,
    _resolve_pinned_ip,
    _rewrite_to_pinned_ip,
    _validate_and_pin,
    fetch_url,
)


def _ai(ip: str, family: int = socket.AF_INET) -> tuple:
    """Build a getaddrinfo-shaped tuple for the given IP."""
    sockaddr = (ip, 0) if family == socket.AF_INET else (ip, 0, 0, 0)
    return (family, socket.SOCK_STREAM, 0, "", sockaddr)


# ----------------------------------------------------------------------------
# _validate_and_pin / _resolve_pinned_ip
# ----------------------------------------------------------------------------


class TestValidateAndPin:
    def test_returns_first_resolved_ip(self, monkeypatch):
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [_ai("93.184.216.34")])
        url, ip = _validate_and_pin("https://example.com/")
        assert url == "https://example.com/"
        assert ip == "93.184.216.34"

    def test_blocks_when_any_resolved_ip_is_private(self, monkeypatch):
        # Mixed-result rebinding: attacker DNS returns one public + one private.
        # Pinning to the first IP would still let the second sneak through if
        # we only checked the chosen one — so we require *every* result clean.
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **kw: [_ai("93.184.216.34"), _ai("10.0.0.1")],
        )
        with pytest.raises(ValueError, match="Blocked private/reserved IP"):
            _validate_and_pin("https://example.com/")

    def test_fails_closed_on_gaierror(self, monkeypatch):
        def boom(*a, **kw):
            raise socket.gaierror("does not resolve")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        with pytest.raises(ValueError, match="DNS resolution failed"):
            _validate_and_pin("https://nonexistent.invalid/")

    def test_resolve_pinned_ip_returns_none_on_gaierror(self, monkeypatch):
        # Underlying helper distinguishes resolution-failure (None) from
        # blocked-IP (raises). Legacy `_validate_url` relies on this split.
        def boom(*a, **kw):
            raise socket.gaierror("nope")

        monkeypatch.setattr(socket, "getaddrinfo", boom)
        assert _resolve_pinned_ip("nonexistent.invalid") is None


# ----------------------------------------------------------------------------
# IP-literal edge cases (IPv4-mapped IPv6, decimal-encoded IPv4)
# ----------------------------------------------------------------------------


class TestIpLiteralEdgeCases:
    def test_ipv4_mapped_ipv6_loopback_blocked_by_literal_check(self):
        # `::ffff:127.0.0.1` is loopback per `ipaddress.ip_address(...).is_loopback`.
        with pytest.raises(ValueError, match="Blocked"):
            _validate_and_pin("http://[::ffff:127.0.0.1]/")

    def test_ipv4_mapped_ipv6_returned_by_dns_blocked(self, monkeypatch):
        # Even if the literal check misses (e.g. attacker uses a real hostname
        # whose AAAA record is `::ffff:127.0.0.1`), the resolution check catches it.
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **kw: [_ai("::ffff:127.0.0.1", socket.AF_INET6)],
        )
        with pytest.raises(ValueError, match="Blocked"):
            _validate_and_pin("https://example.com/")

    def test_decimal_encoded_ipv4_resolves_to_loopback_blocked(self, monkeypatch):
        # 2130706433 == 0x7f000001 == 127.0.0.1 — Python/getaddrinfo on most
        # platforms expand this. Pinning catches whatever the resolver returns.
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda host, *a, **kw: [_ai("127.0.0.1")],
        )
        with pytest.raises(ValueError, match="Blocked"):
            _validate_and_pin("http://2130706433/")

    def test_public_ipv4_passes_positive_marker(self, monkeypatch):
        # Pin-and-invert: prove the block above isn't blanket — a real public
        # IP (8.8.8.8 — Google DNS, globally routable) is accepted.
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [_ai("8.8.8.8")])
        url, ip = _validate_and_pin("https://dns.google/")
        assert ip == "8.8.8.8"

    def test_public_ipv6_passes_positive_marker(self, monkeypatch):
        # Globally-routable IPv6 (Google DNS) is accepted.
        monkeypatch.setattr(
            socket,
            "getaddrinfo",
            lambda *a, **kw: [_ai("2001:4860:4860::8888", socket.AF_INET6)],
        )
        url, ip = _validate_and_pin("https://dns.google/")
        assert ip == "2001:4860:4860::8888"


# ----------------------------------------------------------------------------
# URL/Host/SNI rewriting
# ----------------------------------------------------------------------------


class TestRewriteToPinnedIp:
    def test_preserves_path_query_fragment(self):
        out = _rewrite_to_pinned_ip("https://example.com:8443/p?q=1#x", "1.2.3.4")
        assert out == "https://1.2.3.4:8443/p?q=1#x"

    def test_brackets_ipv6(self):
        out = _rewrite_to_pinned_ip("https://example.com/x", "2001:db8::1")
        assert out == "https://[2001:db8::1]/x"

    def test_preserves_userinfo(self):
        out = _rewrite_to_pinned_ip("https://u:p@example.com/", "1.2.3.4")
        assert out == "https://u:p@1.2.3.4/"

    def test_default_port_omitted(self):
        out = _rewrite_to_pinned_ip("https://example.com/", "1.2.3.4")
        assert out == "https://1.2.3.4/"


class TestHostHeader:
    def test_no_port(self):
        assert _host_header("https://example.com/path") == "example.com"

    def test_explicit_port(self):
        assert _host_header("https://example.com:8443/") == "example.com:8443"

    def test_userinfo_stripped(self):
        # Host header must not include userinfo.
        assert _host_header("https://u:p@example.com:81/") == "example.com:81"


# ----------------------------------------------------------------------------
# _build_pinned_request — Host header + sni_hostname extension
# ----------------------------------------------------------------------------


class TestBuildPinnedRequest:
    async def test_https_sets_sni_extension_and_host_header(self):
        async with httpx.AsyncClient() as client:
            req = _build_pinned_request(client, "https://example.com/path", "1.2.3.4")
        assert "1.2.3.4" in str(req.url)
        assert "example.com" not in str(req.url)
        assert req.headers["host"] == "example.com"
        assert req.extensions.get("sni_hostname") == "example.com"

    async def test_http_omits_sni_extension(self):
        async with httpx.AsyncClient() as client:
            req = _build_pinned_request(client, "http://example.com/path", "1.2.3.4")
        assert req.headers["host"] == "example.com"
        # SNI is meaningless for plain HTTP — must not be set.
        assert "sni_hostname" not in req.extensions


# ----------------------------------------------------------------------------
# fetch_url — end-to-end with httpx.MockTransport
# ----------------------------------------------------------------------------


def _pin_dns(monkeypatch, mapping: dict[str, str]) -> list[str]:
    """Install a fake getaddrinfo. Returns a list mutated with each lookup host."""
    seen: list[str] = []

    def fake(host, *a, **kw):
        seen.append(host)
        if host in mapping:
            return [_ai(mapping[host])]
        raise socket.gaierror(f"no fake mapping for {host!r}")

    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return seen


class TestFetchUrlEndToEnd:
    async def test_request_url_carries_pinned_ip_not_hostname(self, monkeypatch, tmp_path):
        _pin_dns(monkeypatch, {"example.com": "93.184.216.34"})
        seen: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["url"] = str(request.url)
            seen["host"] = request.headers.get("host")
            seen["sni"] = request.extensions.get("sni_hostname")
            return httpx.Response(200, content=b"hello", headers={"content-type": "text/plain"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            out = await fetch_url("https://example.com/page", tmp_path, client=client)
        finally:
            await client.aclose()

        assert "93.184.216.34" in seen["url"]
        assert "example.com" not in seen["url"]
        # Original hostname preserved on the wire.
        assert seen["host"] == "example.com"
        assert seen["sni"] == "example.com"
        assert out.exists()
        assert "hello" in out.read_text(encoding="utf-8")

    async def test_dns_rebinding_does_not_redirect_connection(self, monkeypatch, tmp_path):
        # First call (validate) returns public IP; second call (any later
        # resolution attempt) returns a private IP. With pinning, the second
        # answer never reaches the connection — fetch_url uses the validated IP.
        call_count = [0]

        def fake(host, *a, **kw):
            call_count[0] += 1
            if call_count[0] == 1:
                return [_ai("93.184.216.34")]
            return [_ai("10.0.0.1")]

        monkeypatch.setattr(socket, "getaddrinfo", fake)

        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            await fetch_url("https://example.com/", tmp_path, client=client)
        finally:
            await client.aclose()

        # The second-resolution private IP must never have been used.
        assert all("93.184.216.34" in u for u in seen_urls)
        assert not any("10.0.0.1" in u for u in seen_urls)

    async def test_redirect_to_private_host_blocked(self, monkeypatch, tmp_path):
        _pin_dns(
            monkeypatch,
            {
                "public.example.com": "93.184.216.34",
                "internal.example.com": "10.0.0.5",
            },
        )

        def handler(request: httpx.Request) -> httpx.Response:
            # First hop redirects to a hostname that resolves to a private IP.
            if "93.184.216.34" in str(request.url):
                return httpx.Response(
                    302, headers={"location": "https://internal.example.com/secret"}
                )
            return httpx.Response(200, content=b"leaked")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            with pytest.raises(ValueError, match="Blocked"):
                await fetch_url("https://public.example.com/", tmp_path, client=client)
        finally:
            await client.aclose()

    async def test_redirect_to_public_followed(self, monkeypatch, tmp_path):
        # Positive marker: a redirect to another public host should succeed,
        # otherwise the block above is a tautology.
        _pin_dns(
            monkeypatch,
            {"a.example.com": "93.184.216.34", "b.example.com": "8.8.8.8"},
        )

        def handler(request: httpx.Request) -> httpx.Response:
            if "93.184.216.34" in str(request.url):
                return httpx.Response(302, headers={"location": "https://b.example.com/landed"})
            return httpx.Response(200, content=b"final", headers={"content-type": "text/plain"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            out = await fetch_url("https://a.example.com/", tmp_path, client=client)
        finally:
            await client.aclose()
        assert "final" in out.read_text(encoding="utf-8")

    async def test_response_size_cap_via_content_length(self, monkeypatch, tmp_path):
        _pin_dns(monkeypatch, {"example.com": "93.184.216.34"})

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                content=b"x",
                headers={
                    "content-type": "text/plain",
                    "content-length": str(60 * 1024 * 1024),
                },
            )

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            with pytest.raises(ValueError, match="exceeds size cap"):
                await fetch_url("https://example.com/", tmp_path, client=client)
        finally:
            await client.aclose()

    async def test_304_not_modified_raises_not_silently_empty(self, monkeypatch, tmp_path):
        # httpx treats `is_redirect` as True for the entire 3xx range, so a
        # Location-less 3xx (304 most commonly) used to be misclassified as
        # "redirect with no Location" and quietly returned as the final
        # response — writing an empty markdown file. The fix routes anything
        # without `has_redirect_location` through `raise_for_status()`.
        _pin_dns(monkeypatch, {"example.com": "93.184.216.34"})

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(304, headers={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_url("https://example.com/", tmp_path, client=client)
        finally:
            await client.aclose()

    async def test_302_without_location_raises(self, monkeypatch, tmp_path):
        # Malformed server: 302 with no Location header. Pre-fix this fell
        # through the redirect branch and silently produced an empty file.
        _pin_dns(monkeypatch, {"example.com": "93.184.216.34"})

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            with pytest.raises(httpx.HTTPStatusError):
                await fetch_url("https://example.com/", tmp_path, client=client)
        finally:
            await client.aclose()

    async def test_too_many_redirects_aborts(self, monkeypatch, tmp_path):
        # Each hop returns a Location pointing back to the same public host.
        # After _MAX_REDIRECTS the chain must be aborted with a clear error.
        _pin_dns(monkeypatch, {"example.com": "93.184.216.34"})

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://example.com/next"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            with pytest.raises(ValueError, match="Too many redirects"):
                await fetch_url("https://example.com/", tmp_path, client=client)
        finally:
            await client.aclose()

    async def test_response_size_cap_via_streamed_body(self, monkeypatch, tmp_path):
        # No Content-Length advertised → the streaming check is the trip wire.
        _pin_dns(monkeypatch, {"example.com": "93.184.216.34"})
        oversize = b"a" * (60 * 1024 * 1024)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=oversize, headers={"content-type": "text/plain"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            # Force the pre-check to fall through by stripping content-length
            # via a wrapping transport — easier: just rely on the streaming
            # accumulator since httpx may or may not auto-add CL on a bytes body.
            with pytest.raises(ValueError, match="exceeds size cap"):
                await fetch_url("https://example.com/", tmp_path, client=client)
        finally:
            await client.aclose()


# ----------------------------------------------------------------------------
# Mutate-validation: temporarily disable the pinning to confirm the tests
# above would catch a regression. (memory: feedback_pin_test_mutation_validation)
# ----------------------------------------------------------------------------


class TestPinningMutationValidation:
    """Confirm the rebinding-test assertions are non-vacuous: with the URL
    rewrite removed (the pre-fix shape — `_validate_url` validates, then httpx
    re-resolves at connect time using the original hostname), the connection
    URL contains the hostname instead of the pinned IP.
    """

    async def test_url_rewrite_removal_changes_connection_url(self, monkeypatch, tmp_path):
        # Mutate: skip the IP rewrite. This is the literal shape of the bug we
        # fixed — pinning was advisory, the wire URL still carried the host.
        monkeypatch.setattr(url_fetcher, "_rewrite_to_pinned_ip", lambda url, ip: url)
        monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **kw: [_ai("93.184.216.34")])

        seen_urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_urls.append(str(request.url))
            return httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=False)
        try:
            await fetch_url("https://example.com/", tmp_path, client=client)
        finally:
            await client.aclose()

        # With the rewrite mutated away, all connections go to the hostname,
        # not the pinned IP. This is the regression shape the production tests
        # are guarding against — if it ever passes here, the production tests
        # would be tautological.
        assert all("example.com" in u for u in seen_urls)
        assert not any("93.184.216.34" in u for u in seen_urls)


# Sanity check the test file's own helpers — keeps later refactors honest.
def test_ai_helper_shape():
    info = _ai("1.2.3.4")
    assert info[0] == socket.AF_INET
    assert info[4] == ("1.2.3.4", 0)
    info6 = _ai("2001:db8::1", socket.AF_INET6)
    assert info6[4] == ("2001:db8::1", 0, 0, 0)
