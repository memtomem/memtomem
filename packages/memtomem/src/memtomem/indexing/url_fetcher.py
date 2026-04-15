"""Fetch a URL and convert to markdown for indexing."""

from __future__ import annotations

import functools
import ipaddress
import re
import socket
from pathlib import Path
from urllib.parse import urlparse


def _validate_url(url: str) -> str:
    """Validate URL: require http(s), block internal/private IPs."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"Unsupported URL scheme: {parsed.scheme!r}. Only http/https allowed.")
    if not parsed.hostname:
        raise ValueError("URL must include a hostname.")

    hostname = parsed.hostname.lower()

    # Block obviously internal hostnames
    _BLOCKED_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "[::1]"}
    if hostname in _BLOCKED_HOSTS:
        raise ValueError(f"Blocked host: {hostname}")
    if hostname.endswith(".local") or hostname.endswith(".internal"):
        raise ValueError(f"Blocked internal host: {hostname}")

    # Resolve DNS and block private/reserved IPs
    try:
        for info in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM):
            addr = ipaddress.ip_address(info[4][0])
            if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved:
                raise ValueError(f"Blocked private/reserved IP: {addr} (resolved from {hostname})")
    except socket.gaierror:
        pass  # DNS resolution failed — httpx will handle the error

    return url


async def fetch_url(url: str, output_dir: Path) -> Path:
    """Fetch a URL, convert HTML to markdown, and save to a file.

    Args:
        url: The URL to fetch.
        output_dir: Directory to save the markdown file.

    Returns:
        Path to the saved markdown file.

    Raises:
        ValueError: If the URL targets a private/internal address.
    """
    import httpx

    url = _validate_url(url)

    async with httpx.AsyncClient(follow_redirects=False, timeout=30.0) as client:
        resp = await client.get(url)
        # Handle redirects manually to validate each hop
        redirects = 0
        while resp.is_redirect and redirects < 5:
            if resp.next_request is None:
                break
            location = resp.headers.get("location", "")
            if location:
                _validate_url(str(resp.next_request.url))
            resp = await client.send(resp.next_request)
            redirects += 1
        resp.raise_for_status()

    content_type = resp.headers.get("content-type", "")
    body = resp.text

    if "text/html" in content_type or body.strip().startswith("<"):
        markdown = _html_to_markdown(body)
    elif "text/markdown" in content_type or "text/plain" in content_type:
        markdown = body
    else:
        markdown = f"```\n{body}\n```"

    # Generate filename from URL
    slug = _url_to_slug(url)
    output_dir.mkdir(parents=True, exist_ok=True)
    file_path = output_dir / f"{slug}.md"

    # Add source metadata
    header = f"---\nsource: {url}\n---\n\n"
    file_path.write_text(header + markdown, encoding="utf-8")

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

    # Headers
    def _replace_heading(m: re.Match[str], lvl: int) -> str:
        return f"\n{'#' * lvl} {m.group(1).strip()}\n"

    for i in range(6, 0, -1):
        text = re.sub(
            rf"<h{i}[^>]*>(.*?)</h{i}>",
            functools.partial(_replace_heading, lvl=i),
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
