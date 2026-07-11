"""Fetch a URL and convert to markdown for indexing."""

from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import ParseResult, urljoin, urlparse, urlunparse

from memtomem.context._atomic import atomic_write_text
from memtomem.privacy import enforce_write_guard

if TYPE_CHECKING:
    import httpx

# Hosts blocked at the syntactic level — never resolved further.
_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}

# Hard cap on body size, applied while streaming so an attacker cannot exhaust
# memory by serving a chunked response of unbounded length.
_MAX_RESPONSE_BYTES = 50 * 1024 * 1024  # 50 MB

_MAX_REDIRECTS = 5
_REQUEST_TIMEOUT = 30.0


class FetchPrivacyError(ValueError):
    """Fetched content was rejected before persistence."""


def _is_blocked_address(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True for any IP we refuse to connect to.

    `is_reserved` is IPv4-only; `is_private` covers most ranges of interest on
    IPv6. The combination here is intentionally broad — anything that isn't
    obviously a globally-routed unicast address gets blocked.
    """
    return (
        addr.is_private
        or addr.is_loopback
        or addr.is_link_local
        or addr.is_reserved
        or addr.is_multicast
        or addr.is_unspecified
    )


def _check_syntax(parsed: ParseResult) -> str:
    """Validate scheme + hostname presence + IP-literal-policy. Returns the
    lowercased hostname so callers don't re-call .lower() / .hostname.

    Split out from `_resolve_pinned_ip` so the legacy `_validate_url` and the
    new `_validate_and_pin` share the same gates without duplicating logic.
    """
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r}. Only http/https allowed.")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname.")

    hostname = parsed.hostname.lower()
    if hostname in _BLOCKED_HOSTS:
        raise ValueError(f"Blocked host: {hostname}")
    if hostname.endswith(".local") or hostname.endswith(".internal"):
        raise ValueError(f"Blocked internal host: {hostname}")

    # If the hostname is itself an IP literal, reject it pre-DNS so platform
    # quirks (decimal-encoded IPv4, IPv4-mapped IPv6, etc.) can't slip through.
    try:
        literal = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        literal = None
    if literal is not None and _is_blocked_address(literal):
        raise ValueError(f"Blocked private/reserved IP: {literal}")

    return hostname


def _resolve_pinned_ip(hostname: str) -> str | None:
    """Resolve hostname, validate every returned IP, return the first.

    Returns None if the resolver fails (gaierror) so callers can choose between
    fail-open (legacy `_validate_url`) and fail-closed (`_validate_and_pin`).
    Refuses mixed results: if any returned IP is blocked, the whole resolution
    is rejected — otherwise an attacker who controls DNS could return one
    public + one private and rely on connect-time selection.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        return None
    if not infos:
        return None

    pinned: str | None = None
    for info in infos:
        # info[4] is the sockaddr tuple; the first element is always the
        # address string for AF_INET / AF_INET6. mypy widens to `str | int`
        # (because `getaddrinfo` is typed for the AF_PACKET case too) so we
        # narrow explicitly. IPv6 may carry a scope id (`fe80::1%eth0`).
        raw = info[4][0]
        assert isinstance(raw, str)
        ip_str = raw.split("%", 1)[0]
        addr = ipaddress.ip_address(ip_str)
        if _is_blocked_address(addr):
            raise ValueError(f"Blocked private/reserved IP: {addr} (resolved from {hostname!r})")
        if pinned is None:
            pinned = ip_str
    return pinned


def _hostname_for_resolver(hostname: str) -> str:
    """Return the form of `hostname` to pass to `socket.getaddrinfo`.

    IP literals pass through unchanged. DNS names are IDNA-encoded — Python's
    socket module is inconsistent across platforms about whether it auto-IDNA
    encodes Unicode hostnames, so we do it ourselves to make IDN URLs work
    portably (and to match what we'll later put on the wire).
    """
    if _is_ip_literal(hostname):
        return hostname
    return _ascii_hostname(hostname)


def _validate_url(url: str) -> str:
    """Validate URL: require http(s), block internal/private IPs.

    Legacy entry point kept for callers that don't need the pinned IP. Failure
    to resolve DNS is silently allowed here (the actual httpx connect will
    surface the error) — for SSRF-sensitive paths use `_validate_and_pin`,
    which fails closed and supplies the pinned IP for a connect-time pin.
    """
    hostname = _check_syntax(urlparse(url))
    # Resolve as a side-effect so any private-IP DNS hit raises here.
    _resolve_pinned_ip(_hostname_for_resolver(hostname))
    return url


def _validate_and_pin(url: str) -> tuple[str, str]:
    """Validate URL and return (url, pinned_ip).

    The pinned_ip is the IP returned by getaddrinfo at validation time. The
    actual connection is made to that IP with the original hostname preserved
    in the Host header (and SNI for TLS). This closes the validate-then-connect
    TOCTOU window an attacker can use to swing DNS to a private address between
    the two calls (CWE-918 SSRF via DNS rebinding).
    """
    hostname = _check_syntax(urlparse(url))
    pinned = _resolve_pinned_ip(_hostname_for_resolver(hostname))
    if pinned is None:
        raise ValueError(f"DNS resolution failed for {hostname!r}; refusing to fetch.")
    return url, pinned


def _rewrite_to_pinned_ip(url: str, pinned_ip: str) -> str:
    """Replace the hostname in url with pinned_ip; preserve port/userinfo/path."""
    parsed = urlparse(url)
    host_for_netloc = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
    netloc = host_for_netloc
    if parsed.port is not None:
        netloc = f"{host_for_netloc}:{parsed.port}"
    if parsed.username:
        userinfo = parsed.username
        if parsed.password:
            userinfo = f"{userinfo}:{parsed.password}"
        netloc = f"{userinfo}@{netloc}"
    return urlunparse(parsed._replace(netloc=netloc))


def _is_ip_literal(host: str) -> bool:
    """True if `host` is a bare IPv4/IPv6 address (no DNS lookup possible)."""
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


def _ascii_hostname(host: str) -> str:
    """IDNA-encode a DNS hostname to ASCII; pass already-ASCII hosts through.

    HTTP headers and TLS SNI are ASCII-only — `bücher.example` must go on the
    wire as `xn--bcher-kva.example`. `str.encode("idna")` is the stdlib path
    (RFC 3490). We only call this for hostnames we've already classified as
    DNS names (not IP literals).
    """
    try:
        return host.encode("idna").decode("ascii")
    except UnicodeError:
        # `idna` codec rejects empty / overly-long / disallowed-codepoint
        # labels. Fall back to the raw value — `_check_syntax` has already
        # vetted the host shape, and a downstream encode error here is
        # preferable to silently hiding a malformed hostname.
        return host


def _host_header(url: str) -> str:
    """Return the Host header value (host[:port], no userinfo) from url.

    Rules:
    - IPv6 literal hosts keep their brackets (RFC 7230 §5.4) — `urlparse`
      strips them, so re-add for any host that parses as an IP and contains
      a colon. (DNS hostnames cannot contain `:`.)
    - DNS hostnames are IDNA-encoded so a Unicode IDN like `bücher.example`
      doesn't trip httpx's ASCII header encoder.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if _is_ip_literal(host):
        if ":" in host:
            host = f"[{host}]"
    else:
        host = _ascii_hostname(host)
    if parsed.port is None:
        return host
    return f"{host}:{parsed.port}"


def _sni_hostname(url: str) -> str | None:
    """Return the SNI hostname for HTTPS URLs, or None when SNI must be omitted.

    RFC 6066 §3: SNI must be a DNS hostname, NOT an IP literal. When the user
    supplied an IP-literal URL, return None so httpcore falls back to the
    rewritten origin (= the pinned IP) — that matches what httpx does natively
    for `https://1.2.3.4/` and the cert is validated against the IP. For DNS
    hostnames we override SNI with the IDNA-encoded original so the TLS cert
    is validated against the user-specified hostname rather than the pinned IP.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return None
    host = parsed.hostname
    if _is_ip_literal(host):
        return None
    return _ascii_hostname(host)


def _build_pinned_request(client: httpx.AsyncClient, url: str, pinned_ip: str) -> httpx.Request:
    """Build a GET request that connects to pinned_ip with the original SNI/Host.

    The TCP connection goes to the IP literal in the rewritten URL. The Host
    header carries the original hostname so the upstream server still routes
    correctly (vhosting). For HTTPS DNS URLs, the `sni_hostname` extension
    forces the TLS handshake to use the original hostname for SNI + cert
    validation — httpcore wires this into `start_tls(server_hostname=...)`.
    For HTTPS IP-literal URLs we omit the override (RFC 6066 forbids IP-as-SNI).
    """
    connect_url = _rewrite_to_pinned_ip(url, pinned_ip)
    headers = {"Host": _host_header(url)}
    extensions: dict[str, str] = {}
    sni = _sni_hostname(url)
    if sni is not None:
        extensions["sni_hostname"] = sni
    return client.build_request("GET", connect_url, headers=headers, extensions=extensions)


async def _send_pinned_chain(
    client: httpx.AsyncClient, url: str, pinned_ip: str
) -> tuple[bytes, httpx.Response, str]:
    """Send a pinned request, follow redirects (re-pinning each hop), enforce size cap.

    Returns (body_bytes, final_response, final_url). Caller owns the client
    lifecycle. Streams the body so an over-cap response is rejected mid-flight
    rather than after a full download.
    """
    redirects = 0
    body_bytes = b""
    final_resp: httpx.Response | None = None
    request = _build_pinned_request(client, url, pinned_ip)
    while True:
        resp = await client.send(request, stream=True)
        # `is_redirect` is True for ALL 3xx (incl. 304) per httpx 0.28; use
        # `has_redirect_location` to distinguish followable redirects (status
        # in {301,302,303,307,308} with Location header) from 304/malformed.
        # Anything that lacks a usable Location falls through to the final-
        # response branch so `raise_for_status()` surfaces the 3xx as an error
        # instead of silently writing an empty markdown file.
        if resp.has_redirect_location:
            if redirects >= _MAX_REDIRECTS:
                await resp.aclose()
                raise ValueError(f"Too many redirects (>{_MAX_REDIRECTS})")
            location = resp.headers["location"]
            await resp.aclose()
            next_url = urljoin(url, location)
            next_url, next_ip = _validate_and_pin(next_url)
            url = next_url
            pinned_ip = next_ip
            request = _build_pinned_request(client, url, pinned_ip)
            redirects += 1
            continue
        try:
            resp.raise_for_status()
            cl_header = resp.headers.get("content-length", "")
            try:
                cl = int(cl_header) if cl_header else 0
            except ValueError:
                cl = 0
            if cl > _MAX_RESPONSE_BYTES:
                raise ValueError(
                    f"Response Content-Length {cl} exceeds size cap ({_MAX_RESPONSE_BYTES} bytes)"
                )
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes():
                total += len(chunk)
                if total > _MAX_RESPONSE_BYTES:
                    raise ValueError(
                        f"Response body exceeds size cap ({_MAX_RESPONSE_BYTES} bytes)"
                    )
                chunks.append(chunk)
            body_bytes = b"".join(chunks)
            final_resp = resp
        finally:
            await resp.aclose()
        break

    assert final_resp is not None
    return body_bytes, final_resp, url


async def fetch_url(
    url: str,
    output_dir: Path,
    *,
    client: httpx.AsyncClient | None = None,
    force_unsafe: bool = False,
    scope: str = "user",
) -> Path:
    """Fetch a URL, convert HTML to markdown, and save to a file.

    DNS-pinning: hostname is resolved once at validation time and the resulting
    IP is used for the actual TCP connection. Each redirect hop is re-validated
    + re-pinned. Original hostname is preserved in the Host header and TLS SNI
    so vhosted/TLS-terminated servers still see a normal request. This defeats
    DNS rebinding between validate and connect (CWE-918).

    Args:
        url: The URL to fetch.
        output_dir: Directory to save the markdown file.
        client: Optional pre-built `httpx.AsyncClient`, intended for tests
            that inject a `MockTransport`. The caller is responsible for
            closing it; production code should leave this `None`.

    Returns:
        Path to the saved markdown file.

    Raises:
        ValueError: If the URL targets a private/internal address, any redirect
            hop does, DNS resolution fails, or the response exceeds the size cap.
    """
    import httpx

    original_url, pinned_ip = _validate_and_pin(url)
    saved_url = original_url

    if client is None:
        # Disable connection keepalive: the pool keys on (scheme, host, port).
        # With an IP literal as host, two requests to different hostnames that
        # happen to resolve to the same IP would reuse a TLS session negotiated
        # with the first SNI — silently serving the second host's traffic over
        # the wrong cert. A fresh TCP+TLS per request eliminates the leak.
        limits = httpx.Limits(max_keepalive_connections=0, max_connections=10)
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=_REQUEST_TIMEOUT, limits=limits
        ) as managed:
            body_bytes, final_resp, saved_url = await _send_pinned_chain(
                managed, original_url, pinned_ip
            )
    else:
        body_bytes, final_resp, saved_url = await _send_pinned_chain(
            client, original_url, pinned_ip
        )

    encoding = final_resp.encoding or "utf-8"
    body = body_bytes.decode(encoding, errors="replace")
    content_type = final_resp.headers.get("content-type", "")

    if "text/html" in content_type or body.strip().startswith("<"):
        markdown = _html_to_markdown(body)
    elif "text/markdown" in content_type or "text/plain" in content_type:
        markdown = body
    else:
        markdown = f"```\n{body}\n```"

    slug = _url_to_slug(saved_url)
    file_path = output_dir / f"{slug}.md"

    header = f"---\nsource: {saved_url}\n---\n\n"
    final = header + markdown
    guard = enforce_write_guard(
        final,
        surface="mcp_fetch",
        force_unsafe=force_unsafe,
        scope=scope,
        audit_context={"host": urlparse(saved_url).hostname or ""},
    )
    if guard.decision.startswith("blocked"):
        raise FetchPrivacyError(
            "Fetched content was blocked by the redaction guard before persistence"
        )
    atomic_write_text(file_path, final, mode=0o600)

    return file_path


def _url_to_slug(url: str) -> str:
    """Convert a URL to a filesystem-safe slug."""
    # Remove protocol
    slug = re.sub(r"^https?://", "", url)
    # Replace non-alphanumeric with hyphens
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", slug)
    # Trim and limit length
    slug = slug.strip("-")[:80]
    return slug or "fetched"


def _html_to_markdown(html: str) -> str:
    """Simple HTML to markdown conversion without external dependencies."""
    import html as html_mod

    text = html

    # Remove script/style blocks
    text = re.sub(r"<script[^>]*>.*?</script>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<nav[^>]*>.*?</nav>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<footer[^>]*>.*?</footer>", "", text, flags=re.DOTALL | re.IGNORECASE)

    # Headers — loop from h6 down so inner tags render before their
    # enclosing outer tag consumes them.
    for i in range(6, 0, -1):

        def _replace_header(m: re.Match[str], lvl: int = i) -> str:
            return f"\n{'#' * lvl} {m.group(1).strip()}\n"

        text = re.sub(
            rf"<h{i}[^>]*>(.*?)</h{i}>",
            _replace_header,
            text,
            flags=re.DOTALL | re.IGNORECASE,
        )

    # Bold/italic
    text = re.sub(r"<(strong|b)>(.*?)</\1>", r"**\2**", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<(em|i)>(.*?)</\1>", r"*\2*", text, flags=re.DOTALL | re.IGNORECASE)

    # Links
    text = re.sub(
        r'<a[^>]+href="([^"]*)"[^>]*>(.*?)</a>',
        r"[\2](\1)",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )

    # Code blocks
    text = re.sub(
        r"<pre[^>]*><code[^>]*>(.*?)</code></pre>",
        r"\n```\n\1\n```\n",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    text = re.sub(
        r"<pre[^>]*>(.*?)</pre>", r"\n```\n\1\n```\n", text, flags=re.DOTALL | re.IGNORECASE
    )
    text = re.sub(r"<code>(.*?)</code>", r"`\1`", text, flags=re.DOTALL | re.IGNORECASE)

    # Lists
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"\n- \1", text, flags=re.DOTALL | re.IGNORECASE)

    # Paragraphs and breaks
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\n\1\n", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<br\s*/?>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<hr\s*/?>", "\n---\n", text, flags=re.IGNORECASE)

    # Remove remaining tags
    text = re.sub(r"<[^>]+>", "", text)

    # Decode HTML entities
    text = html_mod.unescape(text)

    # Clean up whitespace
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    lines = [line.strip() for line in text.split("\n")]
    text = "\n".join(lines)

    return text.strip()
