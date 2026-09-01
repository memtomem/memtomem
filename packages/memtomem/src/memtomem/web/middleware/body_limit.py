"""Request-body limits applied before multipart/JSON parsing."""

from __future__ import annotations

import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send

UPLOAD_REQUEST_LIMIT = 201 * 1024 * 1024
JSON_REQUEST_LIMIT = 4 * 1024 * 1024
_MULTIPART_PATHS = frozenset({"/api/upload", "/api/export/import"})


class _BodyTooLarge(Exception):
    pass


class UploadBodyLimitMiddleware:
    """Fail oversized API bodies before framework parsers allocate them."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = {k.lower(): v for k, v in scope.get("headers", [])}
        path = scope.get("path", "")
        content_type = headers.get(b"content-type", b"").split(b";", 1)[0].strip().lower()
        if path in _MULTIPART_PATHS:
            limit = UPLOAD_REQUEST_LIMIT
            detail = "Upload request too large"
        elif path.startswith("/api/") and (
            not content_type
            or content_type == b"application/json"
            or (content_type.startswith(b"application/") and content_type.endswith(b"+json"))
        ):
            limit = JSON_REQUEST_LIMIT
            detail = "JSON request too large"
        else:
            await self.app(scope, receive, send)
            return

        raw_length = headers.get(b"content-length")
        if raw_length:
            try:
                if int(raw_length) > limit:
                    await self._reject(send, detail)
                    return
            except ValueError:
                await self._reject(send, detail)
                return

        seen = 0

        async def limited_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._reject(send, detail)

    @staticmethod
    async def _reject(send: Send, detail: str) -> None:
        body = json.dumps({"detail": detail}, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})
