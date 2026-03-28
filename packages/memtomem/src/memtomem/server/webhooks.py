"""Memory lifecycle webhook manager."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memtomem.config import WebhookConfig

logger = logging.getLogger(__name__)


class WebhookManager:
    """Fires HTTP webhooks on memory lifecycle events."""

    def __init__(self, config: WebhookConfig):
        self._config = config
        self._client = None

    def _get_client(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(timeout=self._config.timeout_seconds)
        return self._client

    async def fire(self, event: str, payload: dict) -> None:
        """Fire webhook if enabled and event is in the configured list."""
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

        asyncio.create_task(self._send_with_retry(body, headers))

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
                logger.debug("Webhook failed (attempt %d/%d): %s", attempt + 1, attempts, exc)
            if attempt < attempts - 1:
                await asyncio.sleep(1.0 * (attempt + 1))

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
