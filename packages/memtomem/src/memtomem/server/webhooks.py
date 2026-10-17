"""Memory lifecycle webhook manager."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
from urllib.parse import urlparse
from typing import TYPE_CHECKING

from memtomem.egress_policy import blocked_address_reason

if TYPE_CHECKING:
    from httpx import AsyncClient

    from memtomem.config import WebhookConfig
    from memtomem.server.webhook_egress import Resolver

logger = logging.getLogger(__name__)


def webhook_error_cb(task: asyncio.Task) -> None:
    """Log errors from fire-and-forget webhook tasks."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc:
        logger.warning("Webhook fire failed: %s", exc)


def _validate_webhook_url(url: str, *, enforce_host_policy: bool = True) -> str | None:
    """Return an error message if the URL is unsafe, or None if valid.

    Scheme and syntax are always checked. The host policy — local-only
    hostnames and IP literals the egress policy refuses — is the startup
    fast path of the same policy ``EgressBackend`` enforces at connect time;
    ``allow_private_hosts`` turns it off here as there.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return "malformed URL"
    if parsed.scheme not in ("http", "https"):
        return f"unsupported scheme '{parsed.scheme}' (must be http or https)"
    hostname = parsed.hostname or ""
    host_lower = hostname.lower().rstrip(".")
    if not host_lower:
        return "malformed URL: missing hostname"
    try:
        # ``urlparse`` defers the port parse to attribute access, so a URL
        # like ``http://host:bad`` has a hostname and no usable authority;
        # httpx raises ``InvalidURL`` on it when the client is built. The
        # message names the host only: the authority may carry userinfo,
        # and this string reaches the warning log.
        parsed.port
    except ValueError:
        return f"malformed URL: invalid port for host '{hostname}'"
    if not enforce_host_policy:
        return None
    if host_lower == "localhost" or host_lower.endswith((".localhost", ".local", ".internal")):
        return f"private/reserved host '{hostname}' not allowed"
    try:
        addr = ipaddress.ip_address(hostname)
    except ValueError:
        return None  # a DNS name: classified when it resolves, at connect time
    reason = blocked_address_reason(addr)
    if reason is not None:
        return f"private/reserved IP '{hostname}' not allowed ({reason})"
    return None


def _validate_proxy_url(url: str) -> str | None:
    """Return an error message if the proxy URL cannot be used, or None."""
    err = _validate_webhook_url(url, enforce_host_policy=False)
    return None if err is None else f"proxy: {err}"


class WebhookManager:
    """Fires HTTP webhooks on memory lifecycle events."""

    def __init__(self, config: WebhookConfig, *, resolver: Resolver | None = None):
        self._config = config
        self._client: AsyncClient | None = None
        self._pending_tasks: set[asyncio.Task] = set()
        self._closed = False
        # Tests inject a resolver; production uses the backend's default.
        self._resolver = resolver
        # A refused resolution is a configuration problem, not a transient
        # one: the first refusal warns, later ones go to debug.
        self._egress_refused_logged = False
        if config.url:
            # With a proxy, the proxy connects to the receiver and the address
            # policy is its to enforce (``build_transport``), so the startup
            # host check would refuse what the delivery path then allows.
            err = _validate_webhook_url(
                config.url,
                enforce_host_policy=not (config.allow_private_hosts or config.proxy),
            )
            if err is None and config.proxy:
                err = _validate_proxy_url(config.proxy)
            if err:
                logger.warning("Webhook URL rejected: %s — webhooks disabled", err)
                self._config = config.model_copy(update={"enabled": False})

    def _get_client(self) -> AsyncClient:
        if self._client is None:
            import httpx

            from memtomem.server.webhook_egress import build_transport

            self._client = httpx.AsyncClient(
                transport=build_transport(self._config, resolver=self._resolver),
                timeout=self._config.timeout_seconds,
            )
        return self._client

    async def fire(self, event: str, payload: dict) -> None:
        """Fire webhook if enabled and event is in the configured list."""
        if self._closed:
            # The lifespan closes webhooks before storage teardown, so a fire
            # arriving after that would spawn a task nothing tracks or cancels
            # and rebuild an ``AsyncClient`` nobody closes (#2185).
            logger.debug("Webhook manager closed; dropping %s event", event)
            return
        if not self._config.enabled or not self._config.url:
            return
        if event not in self._config.events:
            return

        body = json.dumps({"event": event, "data": payload}, default=str)
        headers = {"Content-Type": "application/json"}

        if self._config.secret:
            sig = hmac.new(
                self._config.secret.encode(),
                body.encode(),
                hashlib.sha256,
            ).hexdigest()
            headers["X-Webhook-Signature"] = f"sha256={sig}"

        task = asyncio.create_task(
            self._send_with_retry(body, headers), name=f"memtomem-webhook-{event}"
        )
        self._pending_tasks.add(task)
        task.add_done_callback(webhook_error_cb)
        task.add_done_callback(self._pending_tasks.discard)

    async def _send_with_retry(self, body: str, headers: dict, attempts: int = 3) -> None:
        client = self._get_client()
        for attempt in range(attempts):
            try:
                resp = await client.post(self._config.url, content=body, headers=headers)
                if resp.status_code < 400:
                    return
                logger.warning(
                    "Webhook returned %d (attempt %d/%d)", resp.status_code, attempt + 1, attempts
                )
            except Exception as exc:
                from memtomem.server.webhook_egress import egress_refusal

                refused = egress_refusal(exc)
                if refused is not None:
                    # Retrying would re-resolve the same name; the manager
                    # stays enabled so a corrected answer delivers next time.
                    level = logging.DEBUG if self._egress_refused_logged else logging.WARNING
                    self._egress_refused_logged = True
                    logger.log(level, "Webhook not delivered: %s", refused)
                    return
                logger.debug("Webhook failed (attempt %d/%d): %s", attempt + 1, attempts, exc)
            if attempt < attempts - 1:
                await asyncio.sleep(1.0 * (attempt + 1))

    async def close(self) -> None:
        self._closed = True
        if self._pending_tasks:
            for task in self._pending_tasks:
                task.cancel()
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)
            self._pending_tasks.clear()
        if self._client:
            await self._client.aclose()
            self._client = None
